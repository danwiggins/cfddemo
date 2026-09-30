"""Protected transactional storage for provider-local linkage authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    MAX_REVISIONS,
    AuthorizedLinkageRevision,
    LinkageId,
    LinkageOperation,
    LinkageRevision,
    ProviderNamespace,
    authorize_linkage_revision,
    linkage_revision_sha256,
    provider_trust_snapshot_sha256,
    validate_linkage_history,
)

SCHEMA_VERSION = 1
MAX_PROVIDER_TRUST_PINS = 256
_SQLITE_OPEN_LOCK = threading.RLock()
_PROVIDER_NAMESPACE = TypeAdapter(ProviderNamespace)
_SHA256 = TypeAdapter(Sha256)


class ProviderLinkageStoreError(RuntimeError):
    """Sanitized protected-store failure."""


class ProviderLinkageStoreConflict(ProviderLinkageStoreError):
    pass


class ProviderLinkageStoreUnsafe(ProviderLinkageStoreError):
    pass


class ProviderLinkageStoreSchemaError(ProviderLinkageStoreError):
    pass


class CommittedLinkageReceipt(RegistryContract):
    """Receipt that must be rechecked against the live protected store."""

    schema_version: Literal["traceback.committed-linkage-receipt.v1"] = (
        "traceback.committed-linkage-receipt.v1"
    )
    provider_namespace: ProviderNamespace
    linkage_id: LinkageId
    revision: int = Field(ge=1, le=MAX_REVISIONS)
    linkage_revision_sha256: Sha256
    state_version: int = Field(ge=1)
    state_head_sha256: Sha256


class ActiveLinkageSnapshot(RegistryContract):
    schema_version: Literal["traceback.active-linkage-snapshot.v1"] = (
        "traceback.active-linkage-snapshot.v1"
    )
    state_version: int = Field(ge=0)
    state_head_sha256: Sha256
    revisions: tuple[LinkageRevision, ...] = Field(max_length=MAX_REVISIONS)
    receipts: tuple[CommittedLinkageReceipt, ...] = Field(max_length=MAX_REVISIONS)

    @model_validator(mode="after")
    def exact_parallel_order(self) -> ActiveLinkageSnapshot:
        revision_keys = [
            (item.provider_namespace, item.linkage_id, item.revision)
            for item in self.revisions
        ]
        receipt_keys = [
            (item.provider_namespace, item.linkage_id, item.revision)
            for item in self.receipts
        ]
        if revision_keys != receipt_keys or revision_keys != sorted(revision_keys):
            raise ValueError("active linkage snapshot ordering is invalid")
        return self


def _normalize_schema_sql(statement: str) -> str:
    return "".join(statement.split()).casefold()


_SCHEMA_SQL = {
    ("table", "metadata"): (
        "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    ),
    ("table", "trust_pins"): """CREATE TABLE trust_pins(
        provider_namespace TEXT PRIMARY KEY,
        trust_snapshot_sha256 TEXT NOT NULL
    )""",
    ("table", "linkage_revisions"): """CREATE TABLE linkage_revisions(
        provider_namespace TEXT NOT NULL,
        linkage_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        revision_sha256 TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        record_json BLOB NOT NULL,
        PRIMARY KEY(provider_namespace, linkage_id, revision)
    )""",
    ("table", "approval_consumptions"): """CREATE TABLE approval_consumptions(
        provider_namespace TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        revision_sha256 TEXT NOT NULL REFERENCES linkage_revisions(revision_sha256),
        trust_snapshot_sha256 TEXT NOT NULL,
        PRIMARY KEY(provider_namespace, approval_id),
        UNIQUE(provider_namespace, nonce)
    )""",
    ("index", "linkage_revision_order"): """CREATE INDEX linkage_revision_order
        ON linkage_revisions(provider_namespace, linkage_id, revision)""",
}
_SCHEMA_SIGNATURE = {
    key: _normalize_schema_sql(value) for key, value in _SCHEMA_SQL.items()
}


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _descriptor_identity(value: os.stat_result) -> tuple[int, int, int]:
    return stat.S_IFMT(value.st_mode), value.st_dev, value.st_ino


def _open_descriptor_identities() -> dict[int, tuple[int, int, int]]:
    identities: dict[int, tuple[int, int, int]] = {}
    try:
        names = os.listdir("/dev/fd")
    except OSError:
        return identities
    for name in names:
        if not name.isdigit():
            continue
        descriptor = int(name)
        try:
            identities[descriptor] = _descriptor_identity(os.fstat(descriptor))
        except OSError:
            continue
    return identities


def _record_bytes(record: AuthorizedLinkageRevision) -> bytes:
    return canonical_contract_bytes(record)


class ProviderLinkageStore:
    """SQLite-backed approval consumption and immutable linkage history."""

    def __init__(
        self,
        root: str | Path,
        *,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            0
            < len(expected_trust_snapshot_sha256_by_provider)
            <= MAX_PROVIDER_TRUST_PINS
        ):
            raise ProviderLinkageStoreUnsafe("provider trust pins are required")
        requested_root = Path(root)
        if not requested_root.is_absolute():
            raise ProviderLinkageStoreUnsafe("linkage store root must be absolute")
        self.root = requested_root
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise ProviderLinkageStoreUnsafe("linkage store root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError:
            raise ProviderLinkageStoreUnsafe("linkage store root is unsafe") from None
        self._root_identity = _inode_identity(os.fstat(self._root_fd))
        self.database = self.root / "linkage.sqlite3"
        self._database_identity: tuple[int, int] | None = None
        self._database_fd: int | None = None
        self._sqlite_database_fd: int | None = None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        try:
            self._trust_pins = {
                _PROVIDER_NAMESPACE.validate_python(provider): _SHA256.validate_python(
                    digest
                )
                for provider, digest in expected_trust_snapshot_sha256_by_provider.items()
            }
        except ValueError:
            raise ProviderLinkageStoreUnsafe("provider trust pins are invalid") from None
        self._clock = clock or (lambda: datetime.now(UTC).replace(microsecond=0))
        try:
            metadata = os.stat(
                "linkage.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(metadata.st_mode):
                self.close()
                raise ProviderLinkageStoreUnsafe("linkage store database is unsafe")
            self._database_identity = _inode_identity(metadata)
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock, _SQLITE_OPEN_LOCK:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                try:
                    connection.close()
                except (sqlite3.Error, TypeError, AttributeError):
                    pass
                self._connection = None
                self._sqlite_database_fd = None
            for attribute in ("_database_fd", "_root_fd"):
                descriptor = getattr(self, attribute, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except (OSError, TypeError, AttributeError):
                        pass
                    setattr(self, attribute, None)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_storage(self) -> None:
        try:
            root = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root.st_mode)
                or _inode_identity(root) != self._root_identity
            ):
                raise ProviderLinkageStoreUnsafe("linkage store root changed")
            if self._database_identity is not None:
                database = os.stat(
                    "linkage.sqlite3",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(database.st_mode)
                    or _inode_identity(database) != self._database_identity
                ):
                    raise ProviderLinkageStoreUnsafe("linkage store database changed")
                for descriptor in (self._database_fd, self._sqlite_database_fd):
                    if descriptor is not None and (
                        _inode_identity(os.fstat(descriptor))
                        != self._database_identity
                    ):
                        raise ProviderLinkageStoreUnsafe(
                            "linkage store database changed"
                        )
        except ProviderLinkageStoreError:
            raise
        except (OSError, TypeError):
            raise ProviderLinkageStoreUnsafe("linkage store storage changed") from None

    def _bind_database_descriptor(self) -> None:
        if self._database_identity is None or self._database_fd is not None:
            return
        descriptor = os.open(
            "linkage.sqlite3",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._root_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _inode_identity(metadata) != self._database_identity
        ):
            os.close(descriptor)
            raise ProviderLinkageStoreUnsafe("linkage store database changed")
        self._database_fd = descriptor

    def _open_connection(self) -> sqlite3.Connection:
        self._validate_storage()
        self._bind_database_descriptor()
        before = _open_descriptor_identities()
        try:
            connection = sqlite3.connect(
                self.database,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            metadata = os.stat(
                "linkage.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
            observed = _inode_identity(metadata)
            if not stat.S_ISREG(metadata.st_mode) or (
                self._database_identity is not None
                and observed != self._database_identity
            ):
                raise ProviderLinkageStoreUnsafe("linkage store database changed")
            matches = [
                descriptor
                for descriptor, identity in _open_descriptor_identities().items()
                if before.get(descriptor) != identity
                and identity == (stat.S_IFREG, *observed)
            ]
            if len(matches) != 1:
                raise ProviderLinkageStoreUnsafe(
                    "linkage store connection identity is unproven"
                )
            self._sqlite_database_fd = matches[0]
            if self._database_identity is None:
                self._database_identity = observed
                self._bind_database_descriptor()
            self._validate_storage()
        except BaseException as error:
            if "connection" in locals():
                connection.close()
            if isinstance(error, ProviderLinkageStoreError):
                raise
            raise ProviderLinkageStoreUnsafe("linkage store database changed") from None
        for path in (
            self.database,
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        ):
            if path.exists():
                path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                with _SQLITE_OPEN_LOCK:
                    self._connection = self._open_connection()
            self._validate_storage()
            try:
                yield self._connection
            finally:
                self._validate_storage()

    def _initialize(self) -> None:
        with self._lock, _SQLITE_OPEN_LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                objects = frozenset(
                    (row[0], row[1])
                    for row in connection.execute(
                        """SELECT type, name FROM sqlite_master
                           WHERE name NOT LIKE 'sqlite_%'
                           ORDER BY type, name"""
                    )
                )
                if not objects:
                    for statement in _SCHEMA_SQL.values():
                        connection.execute(statement)
                    connection.executemany(
                        "INSERT INTO metadata VALUES(?, ?)",
                        (
                            ("schema_version", str(SCHEMA_VERSION)),
                            ("state_version", "0"),
                            ("state_head_sha256", hashlib.sha256(b"").hexdigest()),
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO trust_pins VALUES(?, ?)",
                        sorted(self._trust_pins.items()),
                    )
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
                        (self._state_head(connection),),
                    )
                self._validate_schema(connection)
                observed_pins = dict(
                    connection.execute(
                        "SELECT provider_namespace, trust_snapshot_sha256 FROM trust_pins"
                    )
                )
                if observed_pins != self._trust_pins:
                    raise ProviderLinkageStoreConflict(
                        "linkage store trust pins do not match"
                    )
                connection.commit()
            except BaseException as error:
                connection.rollback()
                if isinstance(error, ProviderLinkageStoreError):
                    raise
                if isinstance(error, sqlite3.DatabaseError):
                    raise ProviderLinkageStoreSchemaError(
                        "linkage store schema is unsupported"
                    ) from None
                raise
            os.chmod(self.database, 0o600)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            schema = {
                (row[0], row[1]): _normalize_schema_sql(row[2])
                for row in connection.execute(
                    """SELECT type, name, sql FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_%'
                       ORDER BY type, name"""
                )
                if isinstance(row[2], str)
            }
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        except sqlite3.DatabaseError:
            raise ProviderLinkageStoreSchemaError(
                "linkage store schema is unsupported"
            ) from None
        if (
            schema != _SCHEMA_SIGNATURE
            or set(metadata) != {
                "schema_version",
                "state_version",
                "state_head_sha256",
            }
            or metadata["schema_version"] != str(SCHEMA_VERSION)
        ):
            raise ProviderLinkageStoreSchemaError(
                "linkage store schema is unsupported"
            )
        try:
            state_version = int(metadata["state_version"])
            _SHA256.validate_python(metadata["state_head_sha256"])
        except (ValueError, TypeError):
            raise ProviderLinkageStoreSchemaError(
                "linkage store metadata is invalid"
            ) from None
        if state_version < 0 or str(state_version) != metadata["state_version"]:
            raise ProviderLinkageStoreSchemaError(
                "linkage store metadata is invalid"
            )

    @staticmethod
    def _load_records(
        connection: sqlite3.Connection,
    ) -> tuple[AuthorizedLinkageRevision, ...]:
        rows = connection.execute(
            """SELECT record_json FROM linkage_revisions
               ORDER BY provider_namespace, linkage_id, revision"""
        ).fetchall()
        if len(rows) > MAX_REVISIONS:
            raise ProviderLinkageStoreSchemaError("linkage history exceeds its bound")
        try:
            return tuple(
                AuthorizedLinkageRevision.model_validate_json(bytes(row[0]))
                for row in rows
            )
        except (ValueError, TypeError):
            raise ProviderLinkageStoreSchemaError(
                "linkage store record is invalid"
            ) from None

    @staticmethod
    def _state_head(connection: sqlite3.Connection) -> str:
        payload = {
            "trust_pins": [
                tuple(row)
                for row in connection.execute(
                    """SELECT provider_namespace, trust_snapshot_sha256
                       FROM trust_pins ORDER BY provider_namespace"""
                )
            ],
            "revisions": [
                tuple(row)
                for row in connection.execute(
                    """SELECT provider_namespace, linkage_id, revision,
                              revision_sha256
                       FROM linkage_revisions
                       ORDER BY provider_namespace, linkage_id, revision"""
                )
            ],
            "consumptions": [
                tuple(row)
                for row in connection.execute(
                    """SELECT provider_namespace, approval_id, nonce,
                              revision_sha256, trust_snapshot_sha256
                       FROM approval_consumptions
                       ORDER BY provider_namespace, approval_id"""
                )
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(b"traceback-linkage-state-v1\0" + encoded).hexdigest()

    def commit_authorized_revision(
        self,
        record: AuthorizedLinkageRevision,
    ) -> CommittedLinkageReceipt:
        """Atomically consume approvals and commit one immutable revision."""

        revision = record.revision
        revision_sha256 = linkage_revision_sha256(revision)
        serialized = _record_bytes(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_current_authority(record)
                existing = connection.execute(
                    """SELECT revision_sha256, record_json
                       FROM linkage_revisions
                       WHERE provider_namespace=? AND linkage_id=? AND revision=?""",
                    (
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                    ),
                ).fetchone()
                if existing is not None:
                    if existing[0] != revision_sha256 or bytes(existing[1]) != serialized:
                        raise ProviderLinkageStoreConflict(
                            "linkage revision conflicts with committed state"
                        )
                    self._verify_consumptions(connection, record, revision_sha256)
                    receipt = self._receipt(connection, revision)
                    connection.commit()
                    return receipt

                if revision.revision == 1:
                    previous = connection.execute(
                        """SELECT 1 FROM linkage_revisions
                           WHERE provider_namespace=? AND linkage_id=? LIMIT 1""",
                        (revision.provider_namespace, revision.linkage_id),
                    ).fetchone()
                    if previous is not None:
                        raise ProviderLinkageStoreConflict(
                            "linkage create conflicts with committed state"
                        )
                else:
                    previous = connection.execute(
                        """SELECT revision_sha256, record_json
                           FROM linkage_revisions
                           WHERE provider_namespace=? AND linkage_id=?
                           ORDER BY revision DESC LIMIT 1""",
                        (revision.provider_namespace, revision.linkage_id),
                    ).fetchone()
                    if (
                        previous is None
                        or revision.revision
                        != AuthorizedLinkageRevision.model_validate_json(
                            bytes(previous[1])
                        ).revision.revision
                        + 1
                        or revision.previous_revision_sha256 != previous[0]
                    ):
                        raise ProviderLinkageStoreConflict(
                            "linkage revision does not extend committed state"
                        )

                connection.execute(
                    """INSERT INTO linkage_revisions VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                        revision_sha256,
                        revision.operation.value,
                        serialized,
                    ),
                )
                trust_sha256 = provider_trust_snapshot_sha256(record.trust_snapshot)
                for approval in record.approvals:
                    connection.execute(
                        """INSERT INTO approval_consumptions
                           VALUES(?, ?, ?, ?, ?)""",
                        (
                            revision.provider_namespace,
                            approval.payload.approval_id,
                            approval.payload.nonce,
                            revision_sha256,
                            trust_sha256,
                        ),
                    )
                records = self._load_records(connection)
                validate_linkage_history(
                    records,
                    expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                )
                state_version = int(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='state_version'"
                    ).fetchone()[0]
                ) + 1
                state_head = self._state_head(connection)
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='state_version'",
                    (str(state_version),),
                )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
                    (state_head,),
                )
                receipt = CommittedLinkageReceipt(
                    provider_namespace=revision.provider_namespace,
                    linkage_id=revision.linkage_id,
                    revision=revision.revision,
                    linkage_revision_sha256=revision_sha256,
                    state_version=state_version,
                    state_head_sha256=state_head,
                )
                connection.commit()
                return receipt
            except BaseException as error:
                connection.rollback()
                if isinstance(error, ProviderLinkageStoreError):
                    raise
                if isinstance(error, sqlite3.IntegrityError):
                    raise ProviderLinkageStoreConflict(
                        "linkage approval or identity conflicts with committed state"
                    ) from None
                if isinstance(error, ValueError):
                    raise ProviderLinkageStoreConflict(
                        "linkage history conflicts with committed state"
                    ) from None
                if isinstance(error, sqlite3.DatabaseError):
                    raise ProviderLinkageStoreSchemaError(
                        "linkage store transaction failed"
                    ) from None
                raise

    def _validate_current_authority(
        self,
        record: AuthorizedLinkageRevision,
    ) -> None:
        expected_trust = self._trust_pins.get(record.revision.provider_namespace)
        if expected_trust is None:
            raise ProviderLinkageStoreConflict(
                "linkage provider trust pin is absent"
            )
        try:
            decision = authorize_linkage_revision(
                record.revision,
                previous_revision=record.previous_revision,
                approvals=record.approvals,
                trust_snapshot=record.trust_snapshot,
                expected_trust_snapshot_sha256=expected_trust,
                evaluated_at=self._clock(),
            )
        except (TypeError, ValueError):
            raise ProviderLinkageStoreUnsafe(
                "linkage store clock is invalid"
            ) from None
        if not decision.linkage_authorized:
            raise ProviderLinkageStoreConflict(
                "linkage authority is not current"
            )

    @staticmethod
    def _verify_consumptions(
        connection: sqlite3.Connection,
        record: AuthorizedLinkageRevision,
        revision_sha256: str,
    ) -> None:
        trust_sha256 = provider_trust_snapshot_sha256(record.trust_snapshot)
        for approval in record.approvals:
            row = connection.execute(
                """SELECT nonce, revision_sha256, trust_snapshot_sha256
                   FROM approval_consumptions
                   WHERE provider_namespace=? AND approval_id=?""",
                (record.revision.provider_namespace, approval.payload.approval_id),
            ).fetchone()
            if row is None or tuple(row) != (
                approval.payload.nonce,
                revision_sha256,
                trust_sha256,
            ):
                raise ProviderLinkageStoreConflict(
                    "linkage approval consumption does not match committed state"
                )

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        revision: LinkageRevision,
    ) -> CommittedLinkageReceipt:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        return CommittedLinkageReceipt(
            provider_namespace=revision.provider_namespace,
            linkage_id=revision.linkage_id,
            revision=revision.revision,
            linkage_revision_sha256=linkage_revision_sha256(revision),
            state_version=int(metadata["state_version"]),
            state_head_sha256=metadata["state_head_sha256"],
        )

    def active_snapshot(self) -> ActiveLinkageSnapshot:
        """Read and revalidate one current, deterministic active projection."""

        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                records = self._load_records(connection)
                validate_linkage_history(
                    records,
                    expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                )
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if metadata["state_head_sha256"] != self._state_head(connection):
                    raise ProviderLinkageStoreSchemaError(
                        "linkage store state head is invalid"
                    )
                latest: dict[tuple[str, str], LinkageRevision] = {}
                latest_records: dict[
                    tuple[str, str], AuthorizedLinkageRevision
                ] = {}
                for record in records:
                    key = (
                        record.revision.provider_namespace,
                        record.revision.linkage_id,
                    )
                    latest[key] = record.revision
                    latest_records[key] = record
                for key, latest_record in latest_records.items():
                    if latest[key].operation != LinkageOperation.TOMBSTONE:
                        self._validate_current_authority(latest_record)
                revisions = tuple(
                    sorted(
                        (
                            revision
                            for revision in latest.values()
                            if revision.operation != LinkageOperation.TOMBSTONE
                        ),
                        key=lambda item: (
                            item.provider_namespace,
                            item.linkage_id,
                            item.revision,
                        ),
                    )
                )
                state_version = int(metadata["state_version"])
                state_head = metadata["state_head_sha256"]
                snapshot = ActiveLinkageSnapshot(
                    state_version=state_version,
                    state_head_sha256=state_head,
                    revisions=revisions,
                    receipts=tuple(
                        CommittedLinkageReceipt(
                            provider_namespace=item.provider_namespace,
                            linkage_id=item.linkage_id,
                            revision=item.revision,
                            linkage_revision_sha256=linkage_revision_sha256(item),
                            state_version=state_version,
                            state_head_sha256=state_head,
                        )
                        for item in revisions
                    ),
                )
                connection.commit()
                return snapshot
            except BaseException:
                connection.rollback()
                raise

    def verify_current_receipt(self, receipt: CommittedLinkageReceipt) -> None:
        """Require an exact receipt for the current active store head."""

        snapshot = self.active_snapshot()
        if (
            receipt.state_version != snapshot.state_version
            or receipt.state_head_sha256 != snapshot.state_head_sha256
            or receipt not in snapshot.receipts
        ):
            raise ProviderLinkageStoreConflict(
                "linkage receipt is not current and active"
            )


__all__ = [
    "ActiveLinkageSnapshot",
    "CommittedLinkageReceipt",
    "ProviderLinkageStore",
    "ProviderLinkageStoreConflict",
    "ProviderLinkageStoreError",
    "ProviderLinkageStoreSchemaError",
    "ProviderLinkageStoreUnsafe",
]
