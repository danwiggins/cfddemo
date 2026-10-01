"""Protected transactional storage for provider-local linkage authority."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    MAX_REVISIONS,
    AuthorizedLinkageRevision,
    LinkageAuthorizationDecision,
    LinkageId,
    LinkageOperation,
    LinkageRevision,
    ProviderNamespace,
    authorize_linkage_revision,
    linkage_revision_sha256,
    provider_trust_snapshot_sha256,
    validate_linkage_history,
)

SCHEMA_VERSION = 3
MAX_PROVIDER_TRUST_PINS = 256
_SQLITE_OPEN_LOCK = threading.RLock()
_RLOCK_TYPE = type(threading.RLock())
_PROVIDER_NAMESPACE = TypeAdapter(ProviderNamespace)
_SHA256 = TypeAdapter(Sha256)
_PINNED_AUTHORIZE_LINKAGE_REVISION = authorize_linkage_revision
StoreId = Annotated[str, StringConstraints(pattern=r"^store_[0-9a-f]{32}$")]
_STORE_ID = TypeAdapter(StoreId)


def capture_expected_trust_pins(
    value: Mapping[str, str],
) -> dict[str, str]:
    """Capture one bounded mapping pass without trusting its size or views."""

    try:
        iterator = iter(value)
    except Exception:  # noqa: BLE001 - normalize hostile mapping hooks
        raise ProviderLinkageStoreUnsafe("provider trust pins are invalid") from None
    captured: dict[str, str] = {}
    try:
        for index in range(MAX_PROVIDER_TRUST_PINS + 1):
            try:
                raw_provider = next(iterator)
            except StopIteration:
                break
            if index == MAX_PROVIDER_TRUST_PINS:
                raise ProviderLinkageStoreUnsafe("provider trust pin count is invalid")
            if type(raw_provider) is not str:
                raise ProviderLinkageStoreUnsafe("provider trust pins are invalid")
            provider = _PROVIDER_NAMESPACE.validate_python(raw_provider, strict=True)
            if provider != raw_provider or provider in captured:
                raise ProviderLinkageStoreUnsafe("provider trust pins are invalid")
            raw_digest = value[raw_provider]
            if type(raw_digest) is not str:
                raise ProviderLinkageStoreUnsafe("provider trust pins are invalid")
            digest = _SHA256.validate_python(raw_digest, strict=True)
            if digest != raw_digest:
                raise ProviderLinkageStoreUnsafe("provider trust pins are invalid")
            captured[provider] = digest
    except ProviderLinkageStoreUnsafe:
        raise
    except Exception:  # noqa: BLE001 - normalize hostile mapping hooks
        raise ProviderLinkageStoreUnsafe("provider trust pins are invalid") from None
    if not captured:
        raise ProviderLinkageStoreUnsafe("provider trust pins are required")
    return captured


class ProviderLinkageStoreError(RuntimeError):
    """Sanitized protected-store failure."""


class ProviderLinkageStoreConflict(ProviderLinkageStoreError):
    pass


class ProviderLinkageStoreUnsafe(ProviderLinkageStoreError):
    pass


class ProviderLinkageStoreSchemaError(ProviderLinkageStoreError):
    pass


_AUTHORITY_TIME_SOURCE_TOKEN = object()


def _system_authority_time(
    _now: Callable[[object], datetime] = datetime.now,
    _utc: object = UTC,
) -> datetime:
    return _now(_utc).replace(microsecond=0)


class AuthorityTimeSource:
    """Exact package-owned clock with no caller callback execution."""

    __slots__ = ("_current", "_failure", "_lock", "_mode")

    def __init__(
        self,
        token: object,
        *,
        mode: Literal["system", "fixed"],
        current: datetime | None,
    ) -> None:
        if token is not _AUTHORITY_TIME_SOURCE_TOKEN:
            raise TypeError("authority time sources use package constructors")
        if mode == "fixed":
            if current is None:
                raise ValueError("fixed authority time is required")
            _authority_time_text(current)
        elif mode != "system" or current is not None:
            raise ValueError("authority time source mode is invalid")
        self._lock = threading.RLock()
        self._mode = mode
        self._current = current
        self._failure: Literal["runtime", "interrupt"] | None = None

    @classmethod
    def system(cls) -> AuthorityTimeSource:
        return cls(_AUTHORITY_TIME_SOURCE_TOKEN, mode="system", current=None)

    @classmethod
    def fixed(cls, current: datetime) -> AuthorityTimeSource:
        return cls(_AUTHORITY_TIME_SOURCE_TOKEN, mode="fixed", current=current)

    def advance_to(self, current: datetime) -> None:
        """Advance a fixed test source; rollback is never accepted."""

        _authority_time_text(current)
        lock = self._lock
        if type(lock) is not _RLOCK_TYPE:
            raise ProviderLinkageStoreUnsafe("authority time source is invalid")
        with lock:
            if self._mode != "fixed" or self._current is None:
                raise ValueError("only a fixed authority time source can advance")
            if current < self._current:
                raise ValueError("authority time source cannot move backwards")
            self._current = current

    def set_failure(
        self,
        failure: Literal["runtime", "interrupt"] | None,
    ) -> None:
        """Set a deterministic package-owned failure mode for boundary tests."""

        if failure not in {None, "runtime", "interrupt"}:
            raise ValueError("authority time source failure mode is invalid")
        lock = self._lock
        if type(lock) is not _RLOCK_TYPE:
            raise ProviderLinkageStoreUnsafe("authority time source is invalid")
        with lock:
            self._failure = failure

    def read(self) -> datetime:
        lock = self._lock
        if type(lock) is not _RLOCK_TYPE:
            raise ProviderLinkageStoreUnsafe("authority time source is invalid")
        with lock:
            mode = self._mode
            current = self._current
            failure = self._failure
        if failure == "runtime":
            raise RuntimeError("authority time source failed")
        if failure == "interrupt":
            raise KeyboardInterrupt
        if mode == "system":
            return _system_authority_time()
        if mode == "fixed" and current is not None:
            return current
        raise ProviderLinkageStoreUnsafe("authority time source is invalid")


class CommittedLinkageReceipt(RegistryContract):
    """Receipt that must be rechecked against the live protected store."""

    schema_version: Literal["traceback.committed-linkage-receipt.v1"] = (
        "traceback.committed-linkage-receipt.v1"
    )
    provider_namespace: ProviderNamespace
    store_id: StoreId
    store_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    trust_pins_sha256: Sha256
    linkage_id: LinkageId
    revision: int = Field(ge=1, le=MAX_REVISIONS)
    linkage_revision_sha256: Sha256
    authorized_record_sha256: Sha256
    state_version: int = Field(ge=1)
    state_head_sha256: Sha256


class ActiveLinkageSnapshot(RegistryContract):
    schema_version: Literal["traceback.active-linkage-snapshot.v1"] = (
        "traceback.active-linkage-snapshot.v1"
    )
    state_version: int = Field(ge=0)
    state_head_sha256: Sha256
    store_id: StoreId
    store_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    trust_pins_sha256: Sha256
    revisions: tuple[LinkageRevision, ...] = Field(max_length=MAX_REVISIONS)
    receipts: tuple[CommittedLinkageReceipt, ...] = Field(max_length=MAX_REVISIONS)
    activation_receipts: tuple[CommittedLinkageReceipt, ...] = Field(
        max_length=MAX_REVISIONS
    )

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
        activation_keys = [
            (item.provider_namespace, item.linkage_id, item.revision)
            for item in self.activation_receipts
        ]
        if (
            revision_keys != receipt_keys
            or revision_keys != activation_keys
            or revision_keys != sorted(revision_keys)
        ):
            raise ValueError("active linkage snapshot ordering is invalid")
        return self


def committed_linkage_receipt_sha256(receipt: CommittedLinkageReceipt) -> str:
    return hashlib.sha256(canonical_contract_bytes(receipt)).hexdigest()


def _normalize_schema_sql(statement: str) -> str:
    return "".join(statement.split()).casefold()


_SCHEMA_V2_SQL = {
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
        authorized_record_sha256 TEXT NOT NULL UNIQUE,
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
        PRIMARY KEY(approval_id),
        UNIQUE(nonce)
    )""",
    ("index", "linkage_revision_order"): """CREATE INDEX linkage_revision_order
        ON linkage_revisions(provider_namespace, linkage_id, revision)""",
}
_SCHEMA_V2_SIGNATURE = {
    key: _normalize_schema_sql(value) for key, value in _SCHEMA_V2_SQL.items()
}
_SCHEMA_SQL = dict(_SCHEMA_V2_SQL)
_SCHEMA_SQL[("table", "linkage_revisions")] = """CREATE TABLE linkage_revisions(
        provider_namespace TEXT NOT NULL,
        linkage_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        revision_sha256 TEXT NOT NULL UNIQUE,
        authorized_record_sha256 TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        record_json BLOB NOT NULL,
        activation_state_version INTEGER NOT NULL DEFAULT 0,
        activation_state_head_sha256 TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
        PRIMARY KEY(provider_namespace, linkage_id, revision)
    )"""
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


def _trust_pins_sha256(pins: Mapping[str, str]) -> str:
    encoded = json.dumps(
        sorted(pins.items()), separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(b"traceback-linkage-trust-pins-v1\0" + encoded).hexdigest()


def _authority_time_text(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.utcoffset() != timedelta(0)
        or value.microsecond
    ):
        raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
    return value.isoformat()


def _authority_time_from_text(value: object) -> datetime:
    if type(value) is not str:
        raise ProviderLinkageStoreSchemaError("linkage store authority time is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ProviderLinkageStoreSchemaError(
            "linkage store authority time is invalid"
        ) from None
    try:
        canonical = _authority_time_text(parsed)
    except ProviderLinkageStoreUnsafe:
        raise ProviderLinkageStoreSchemaError(
            "linkage store authority time is invalid"
        ) from None
    if canonical != value:
        raise ProviderLinkageStoreSchemaError("linkage store authority time is invalid")
    return parsed


class ProviderLinkageStore:
    """SQLite-backed approval consumption and immutable linkage history."""

    def __init__(
        self,
        root: str | Path,
        *,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        time_source: AuthorityTimeSource | None = None,
    ) -> None:
        if time_source is None:
            selected_time_source = AuthorityTimeSource.system()
        elif type(time_source) is AuthorityTimeSource:
            selected_time_source = time_source
        else:
            raise ProviderLinkageStoreUnsafe("authority time source type is invalid")
        trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        requested_root = Path(root)
        if not requested_root.is_absolute():
            raise ProviderLinkageStoreUnsafe("linkage store root must be absolute")
        self.root = requested_root
        root_existed = self.root.exists()
        if self.root.is_symlink() or (root_existed and not self.root.is_dir()):
            raise ProviderLinkageStoreUnsafe("linkage store root is unsafe")
        if root_existed:
            root_metadata = os.stat(self.root, follow_symlinks=False)
            if (
                stat.S_IMODE(root_metadata.st_mode) != 0o700
                or root_metadata.st_uid != os.geteuid()
            ):
                raise ProviderLinkageStoreUnsafe("linkage store root is unsafe")
        else:
            self.root.mkdir(parents=True, mode=0o700)
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
        # (pid, thread) of the active authority_read_fence holder, if any.
        self._authority_fence_thread: tuple[int, int] | None = None
        self._trust_pins = trust_pins
        self._time_source = selected_time_source
        _register_store_time_source(self, self._time_source)
        try:
            metadata = os.stat(
                "linkage.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
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
            _unregister_store_time_source(self)

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
                or stat.S_IMODE(root.st_mode) != 0o700
                or root.st_uid != os.geteuid()
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
                    or stat.S_IMODE(database.st_mode) != 0o600
                    or database.st_uid != os.geteuid()
                ):
                    raise ProviderLinkageStoreUnsafe("linkage store database changed")
                for descriptor in (self._database_fd, self._sqlite_database_fd):
                    if descriptor is not None:
                        bound = os.fstat(descriptor)
                        if (
                            _inode_identity(bound) != self._database_identity
                            or not stat.S_ISREG(bound.st_mode)
                            or stat.S_IMODE(bound.st_mode) != 0o600
                            or bound.st_uid != os.geteuid()
                        ):
                            raise ProviderLinkageStoreUnsafe(
                                "linkage store database changed"
                            )
                for suffix in ("-wal", "-shm"):
                    try:
                        sidecar = os.stat(
                            f"linkage.sqlite3{suffix}",
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if (
                        not stat.S_ISREG(sidecar.st_mode)
                        or stat.S_IMODE(sidecar.st_mode) != 0o600
                        or sidecar.st_uid != os.geteuid()
                    ):
                        raise ProviderLinkageStoreUnsafe(
                            "linkage store sidecar is unsafe"
                        )
        except ProviderLinkageStoreError:
            raise
        except (OSError, TypeError):
            raise ProviderLinkageStoreUnsafe("linkage store storage changed") from None

    def _storage_identity_sha256(self) -> str:
        database_descriptor = (
            self._database_fd
            if self._database_fd is not None
            else self._sqlite_database_fd
        )
        if database_descriptor is None:
            raise ProviderLinkageStoreUnsafe(
                "linkage store database identity is absent"
            )
        try:
            root_identity = _inode_identity(os.fstat(self._root_fd))
            database_identity = _inode_identity(os.fstat(database_descriptor))
        except (OSError, TypeError):
            raise ProviderLinkageStoreUnsafe("linkage store storage changed") from None
        framed = b"\0".join(
            (
                b"traceback-linkage-storage-identity-v1",
                str(root_identity[0]).encode("ascii"),
                str(root_identity[1]).encode("ascii"),
                str(database_identity[0]).encode("ascii"),
                str(database_identity[1]).encode("ascii"),
            )
        )
        return hashlib.sha256(framed).hexdigest()

    def _secure_database_files(self) -> None:
        if self._database_fd is not None:
            os.fchmod(self._database_fd, 0o600)
        for name in ("linkage.sqlite3-wal", "linkage.sqlite3-shm"):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ProviderLinkageStoreUnsafe("linkage store sidecar is unsafe")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    def _bind_database_descriptor(self) -> None:
        if self._database_identity is None or self._database_fd is not None:
            return
        descriptor = os.open(
            "linkage.sqlite3",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
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
        validate_storage = _PINNED_VALIDATE_STORAGE
        bind_database_descriptor = _PINNED_BIND_DATABASE_DESCRIPTOR
        secure_database_files = _PINNED_SECURE_DATABASE_FILES
        validate_storage(self)
        bind_database_descriptor(self)
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
                os.fchmod(self._sqlite_database_fd, 0o600)
                self._database_identity = observed
                bind_database_descriptor(self)
            validate_storage(self)
        except BaseException as error:
            if "connection" in locals():
                connection.close()
            if isinstance(error, ProviderLinkageStoreError):
                raise
            raise ProviderLinkageStoreUnsafe("linkage store database changed") from None
        secure_database_files(self)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        open_connection = _PINNED_OPEN_CONNECTION
        validate_storage = _PINNED_VALIDATE_STORAGE
        with self._lock:
            if self._connection is None:
                with _SQLITE_OPEN_LOCK:
                    self._connection = open_connection(self)
            validate_storage(self)
            try:
                yield self._connection
            finally:
                validate_storage(self)

    def _initialize(self) -> None:
        with self._lock, _SQLITE_OPEN_LOCK, _PINNED_CONNECT(self) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            _PINNED_SECURE_DATABASE_FILES(self)
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
                    authority_time = _pinned_time_source_value(self)
                    for statement in _SCHEMA_SQL.values():
                        connection.execute(statement)
                    connection.executemany(
                        "INSERT INTO metadata VALUES(?, ?)",
                        (
                            ("schema_version", str(SCHEMA_VERSION)),
                            ("state_version", "0"),
                            ("state_head_sha256", hashlib.sha256(b"").hexdigest()),
                            ("store_id", f"store_{secrets.token_hex(16)}"),
                            ("store_epoch_sha256", secrets.token_hex(32)),
                            (
                                "storage_identity_sha256",
                                _PINNED_STORAGE_IDENTITY_SHA256(self),
                            ),
                            (
                                "trust_pins_sha256",
                                _trust_pins_sha256(self._trust_pins),
                            ),
                            (
                                "authority_time_floor",
                                _authority_time_text(authority_time),
                            ),
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO trust_pins VALUES(?, ?)",
                        sorted(self._trust_pins.items()),
                    )
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
                        (_PINNED_STATE_HEAD(connection),),
                    )
                else:
                    metadata = dict(
                        connection.execute("SELECT key, value FROM metadata")
                    )
                    v1_keys = {
                        "schema_version",
                        "state_version",
                        "state_head_sha256",
                        "store_id",
                        "store_epoch_sha256",
                        "storage_identity_sha256",
                        "trust_pins_sha256",
                    }
                    if (
                        set(metadata) == v1_keys
                        and metadata.get("schema_version") == "1"
                    ):
                        schema = {
                            (row[0], row[1]): _normalize_schema_sql(row[2])
                            for row in connection.execute(
                                """SELECT type, name, sql FROM sqlite_master
                                   WHERE name NOT LIKE 'sqlite_%'
                                   ORDER BY type, name"""
                            )
                            if isinstance(row[2], str)
                        }
                        if schema == _SCHEMA_SIGNATURE:
                            authority_time = _pinned_time_source_value(self)
                            connection.execute(
                                "INSERT INTO metadata VALUES(?, ?)",
                                (
                                    "authority_time_floor",
                                    _authority_time_text(authority_time),
                                ),
                            )
                            connection.execute(
                                "UPDATE metadata SET value=? WHERE key='schema_version'",
                                (str(SCHEMA_VERSION),),
                            )
                            metadata = dict(
                                connection.execute("SELECT key, value FROM metadata")
                            )
                        elif schema != _SCHEMA_V2_SIGNATURE:
                            raise ProviderLinkageStoreSchemaError(
                                "linkage store schema is unsupported"
                            )
                        else:
                            authority_time = _pinned_time_source_value(self)
                            connection.execute(
                                "INSERT INTO metadata VALUES(?, ?)",
                                (
                                    "authority_time_floor",
                                    _authority_time_text(authority_time),
                                ),
                            )
                            connection.execute(
                                "UPDATE metadata SET value=? WHERE key='schema_version'",
                                ("2",),
                            )
                            metadata = dict(
                                connection.execute("SELECT key, value FROM metadata")
                            )
                    if metadata.get("schema_version") == "2":
                        schema = {
                            (row[0], row[1]): _normalize_schema_sql(row[2])
                            for row in connection.execute(
                                """SELECT type, name, sql FROM sqlite_master
                                   WHERE name NOT LIKE 'sqlite_%'
                                   ORDER BY type, name"""
                            )
                            if isinstance(row[2], str)
                        }
                        if schema != _SCHEMA_V2_SIGNATURE:
                            raise ProviderLinkageStoreSchemaError(
                                "linkage store schema is unsupported"
                            )
                        connection.execute(
                            "ALTER TABLE linkage_revisions ADD COLUMN "
                            "activation_state_version INTEGER NOT NULL DEFAULT 0"
                        )
                        connection.execute(
                            "ALTER TABLE linkage_revisions ADD COLUMN "
                            "activation_state_head_sha256 TEXT NOT NULL DEFAULT "
                            f"'{'0' * 64}'"
                        )
                        rows = connection.execute(
                            "SELECT rowid FROM linkage_revisions ORDER BY rowid"
                        ).fetchall()
                        for state_version, row in enumerate(rows, start=1):
                            connection.execute(
                                """UPDATE linkage_revisions
                                   SET activation_state_version=?,
                                       activation_state_head_sha256=?
                                   WHERE rowid=?""",
                                (
                                    state_version,
                                    _PINNED_STATE_HEAD(connection, int(row[0])),
                                    int(row[0]),
                                ),
                            )
                        connection.execute(
                            "UPDATE metadata SET value=? WHERE key='schema_version'",
                            (str(SCHEMA_VERSION),),
                        )
                _PINNED_VALIDATE_SCHEMA(connection)
                observed_pins = dict(
                    connection.execute(
                        "SELECT provider_namespace, trust_snapshot_sha256 FROM trust_pins"
                    )
                )
                if observed_pins != self._trust_pins:
                    raise ProviderLinkageStoreConflict(
                        "linkage store trust pins do not match"
                    )
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if metadata[
                    "storage_identity_sha256"
                ] != _PINNED_STORAGE_IDENTITY_SHA256(self) or metadata[
                    "trust_pins_sha256"
                ] != _trust_pins_sha256(self._trust_pins):
                    raise ProviderLinkageStoreConflict(
                        "linkage store identity does not match"
                    )
                self._store_id = metadata["store_id"]
                self._store_epoch_sha256 = metadata["store_epoch_sha256"]
                self._storage_identity = metadata["storage_identity_sha256"]
                self._trust_pins_digest = metadata["trust_pins_sha256"]
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
            or set(metadata)
            != {
                "schema_version",
                "state_version",
                "state_head_sha256",
                "store_id",
                "store_epoch_sha256",
                "storage_identity_sha256",
                "trust_pins_sha256",
                "authority_time_floor",
            }
            or metadata["schema_version"] != str(SCHEMA_VERSION)
        ):
            raise ProviderLinkageStoreSchemaError("linkage store schema is unsupported")
        try:
            state_version = int(metadata["state_version"])
            _SHA256.validate_python(metadata["state_head_sha256"])
            _STORE_ID.validate_python(metadata["store_id"])
            _SHA256.validate_python(metadata["store_epoch_sha256"])
            _SHA256.validate_python(metadata["storage_identity_sha256"])
            _SHA256.validate_python(metadata["trust_pins_sha256"])
            _authority_time_from_text(metadata["authority_time_floor"])
        except (ValueError, TypeError):
            raise ProviderLinkageStoreSchemaError(
                "linkage store metadata is invalid"
            ) from None
        if state_version < 0 or str(state_version) != metadata["state_version"]:
            raise ProviderLinkageStoreSchemaError("linkage store metadata is invalid")

    @staticmethod
    def _load_records(
        connection: sqlite3.Connection,
    ) -> tuple[AuthorizedLinkageRevision, ...]:
        rows = connection.execute(
            """SELECT provider_namespace, linkage_id, revision,
                      revision_sha256, authorized_record_sha256, operation,
                      record_json
               FROM linkage_revisions
               ORDER BY provider_namespace, linkage_id, revision"""
        ).fetchall()
        if len(rows) > MAX_REVISIONS:
            raise ProviderLinkageStoreSchemaError("linkage history exceeds its bound")
        try:
            records = []
            for row in rows:
                raw = bytes(row[6])
                if hashlib.sha256(raw).hexdigest() != row[4]:
                    raise ValueError("record digest mismatch")
                record = AuthorizedLinkageRevision.model_validate_json(raw)
                if _record_bytes(record) != raw:
                    raise ValueError("record bytes are not canonical")
                revision = record.revision
                if (
                    tuple(row[:4])
                    != (
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                        linkage_revision_sha256(revision),
                    )
                    or row[5] != revision.operation.value
                ):
                    raise ValueError("record index does not match canonical bytes")
                records.append(record)
            return tuple(records)
        except (ValueError, TypeError):
            raise ProviderLinkageStoreSchemaError(
                "linkage store record is invalid"
            ) from None

    @staticmethod
    def _state_head(
        connection: sqlite3.Connection,
        max_revision_rowid: int | None = None,
    ) -> str:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        payload = {
            "store_id": metadata["store_id"],
            "store_epoch_sha256": metadata["store_epoch_sha256"],
            "storage_identity_sha256": metadata["storage_identity_sha256"],
            "trust_pins_sha256": metadata["trust_pins_sha256"],
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
                              revision_sha256, authorized_record_sha256, operation
                       FROM linkage_revisions
                       WHERE (? IS NULL OR rowid <= ?)
                       ORDER BY provider_namespace, linkage_id, revision""",
                    (max_revision_rowid, max_revision_rowid),
                )
            ],
            "consumptions": [
                tuple(row)
                for row in connection.execute(
                    """SELECT c.provider_namespace, c.approval_id, c.nonce,
                              c.revision_sha256, c.trust_snapshot_sha256
                       FROM approval_consumptions AS c
                       JOIN linkage_revisions AS r
                         ON r.revision_sha256 = c.revision_sha256
                       WHERE (? IS NULL OR r.rowid <= ?)
                       ORDER BY c.provider_namespace, c.approval_id""",
                    (max_revision_rowid, max_revision_rowid),
                )
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(b"traceback-linkage-state-v1\0" + encoded).hexdigest()

    def _validate_committed_state(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[AuthorizedLinkageRevision, ...]:
        """Reject any mutation of already committed append-only state."""

        _PINNED_VALIDATE_SCHEMA(connection)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if (
            metadata["store_id"] != self._store_id
            or metadata["store_epoch_sha256"] != self._store_epoch_sha256
            or metadata["storage_identity_sha256"]
            != _PINNED_STORAGE_IDENTITY_SHA256(self)
            or metadata["trust_pins_sha256"] != _trust_pins_sha256(self._trust_pins)
        ):
            raise ProviderLinkageStoreSchemaError(
                "linkage store identity state is invalid"
            )
        observed_pins = dict(
            connection.execute(
                "SELECT provider_namespace, trust_snapshot_sha256 FROM trust_pins"
            )
        )
        if observed_pins != self._trust_pins:
            raise ProviderLinkageStoreSchemaError(
                "linkage store trust state is invalid"
            )
        records = _PINNED_LOAD_RECORDS(connection)
        activation_rows = connection.execute(
            """SELECT rowid, activation_state_version,
                      activation_state_head_sha256
               FROM linkage_revisions ORDER BY rowid"""
        ).fetchall()
        for expected_version, row in enumerate(activation_rows, start=1):
            if row[1] != expected_version or row[2] != _PINNED_STATE_HEAD(
                connection, int(row[0])
            ):
                raise ProviderLinkageStoreSchemaError(
                    "linkage consumption history or activation proof is invalid"
                )
        validate_linkage_history(
            records,
            expected_trust_snapshot_sha256_by_provider=self._trust_pins,
        )
        expected_consumptions = sorted(
            (
                record.revision.provider_namespace,
                approval.payload.approval_id,
                approval.payload.nonce,
                linkage_revision_sha256(record.revision),
                provider_trust_snapshot_sha256(record.trust_snapshot),
            )
            for record in records
            for approval in record.approvals
        )
        observed_consumptions = sorted(
            tuple(row)
            for row in connection.execute(
                """SELECT provider_namespace, approval_id, nonce,
                          revision_sha256, trust_snapshot_sha256
                   FROM approval_consumptions"""
            )
        )
        if observed_consumptions != expected_consumptions:
            raise ProviderLinkageStoreSchemaError(
                "linkage store consumption history is invalid"
            )
        if int(metadata["state_version"]) != len(records) or metadata[
            "state_head_sha256"
        ] != _PINNED_STATE_HEAD(connection):
            raise ProviderLinkageStoreSchemaError(
                "linkage store committed state is invalid"
            )
        return records

    def commit_authorized_revision(
        self,
        record: AuthorizedLinkageRevision,
    ) -> CommittedLinkageReceipt:
        """Atomically consume approvals and commit one immutable revision."""

        connect_store = _PINNED_CONNECT
        capture_store_now = _capture_pinned_store_now
        require_authority_time_floor = _require_authority_time_floor
        validate_committed_state = _PINNED_VALIDATE_COMMITTED_STATE
        validate_current_authority = _PINNED_VALIDATE_CURRENT_AUTHORITY
        authorize_revision = _PINNED_AUTHORIZE_LINKAGE_REVISION
        revision = record.revision
        revision_sha256 = linkage_revision_sha256(revision)
        serialized = _record_bytes(record)
        authorized_record_sha256 = hashlib.sha256(serialized).hexdigest()
        with connect_store(self) as connection:
            trust_pins = dict(self._trust_pins)
            evaluated_at, authority_time_floor = capture_store_now(self, connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                require_authority_time_floor(connection, authority_time_floor)
                validate_committed_state(self, connection)
                validate_current_authority(
                    self,
                    record,
                    evaluated_at=evaluated_at,
                    expected_trust_by_provider=trust_pins,
                    authorize_revision=authorize_revision,
                )
                existing = connection.execute(
                    """SELECT revision_sha256, authorized_record_sha256, record_json
                       FROM linkage_revisions
                       WHERE provider_namespace=? AND linkage_id=? AND revision=?""",
                    (
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                    ),
                ).fetchone()
                if existing is not None:
                    if (
                        existing[0] != revision_sha256
                        or existing[1] != authorized_record_sha256
                        or bytes(existing[2]) != serialized
                    ):
                        raise ProviderLinkageStoreConflict(
                            "linkage revision conflicts with committed state"
                        )
                    _PINNED_VERIFY_CONSUMPTIONS(connection, record, revision_sha256)
                    receipt = _PINNED_RECEIPT(
                        connection,
                        revision,
                        authorized_record_sha256,
                    )
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

                state_version = (
                    int(
                        connection.execute(
                            "SELECT value FROM metadata WHERE key='state_version'"
                        ).fetchone()[0]
                    )
                    + 1
                )
                connection.execute(
                    """INSERT INTO linkage_revisions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                        revision_sha256,
                        authorized_record_sha256,
                        revision.operation.value,
                        serialized,
                        state_version,
                        "0" * 64,
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
                _PINNED_VERIFY_CONSUMPTIONS(connection, record, revision_sha256)
                records = _PINNED_LOAD_RECORDS(connection)
                validate_linkage_history(
                    records,
                    expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                )
                state_head = _PINNED_STATE_HEAD(connection)
                connection.execute(
                    """UPDATE linkage_revisions
                       SET activation_state_head_sha256=?
                       WHERE provider_namespace=? AND linkage_id=? AND revision=?""",
                    (
                        state_head,
                        revision.provider_namespace,
                        revision.linkage_id,
                        revision.revision,
                    ),
                )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='state_version'",
                    (str(state_version),),
                )
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
                    (state_head,),
                )
                validate_committed_state(self, connection)
                receipt = CommittedLinkageReceipt(
                    provider_namespace=revision.provider_namespace,
                    linkage_id=revision.linkage_id,
                    store_id=self._store_id,
                    store_epoch_sha256=self._store_epoch_sha256,
                    storage_identity_sha256=self._storage_identity,
                    trust_pins_sha256=self._trust_pins_digest,
                    revision=revision.revision,
                    linkage_revision_sha256=revision_sha256,
                    authorized_record_sha256=authorized_record_sha256,
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
        *,
        evaluated_at: datetime,
        expected_trust_by_provider: Mapping[str, str],
        authorize_revision: Callable[..., LinkageAuthorizationDecision],
    ) -> None:
        expected_trust = expected_trust_by_provider.get(
            record.revision.provider_namespace
        )
        if expected_trust is None:
            raise ProviderLinkageStoreConflict("linkage provider trust pin is absent")
        try:
            decision = authorize_revision(
                record.revision,
                previous_revision=record.previous_revision,
                approvals=record.approvals,
                trust_snapshot=record.trust_snapshot,
                expected_trust_snapshot_sha256=expected_trust,
                evaluated_at=evaluated_at,
            )
        except (TypeError, ValueError):
            raise ProviderLinkageStoreUnsafe(
                "linkage authority replay is invalid"
            ) from None
        if not decision.linkage_authorized:
            raise ProviderLinkageStoreConflict("linkage authority is not current")

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
        authorized_record_sha256: str,
    ) -> CommittedLinkageReceipt:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        activation = connection.execute(
            """SELECT activation_state_version, activation_state_head_sha256
               FROM linkage_revisions
               WHERE provider_namespace=? AND linkage_id=? AND revision=?""",
            (revision.provider_namespace, revision.linkage_id, revision.revision),
        ).fetchone()
        if activation is None:
            raise ProviderLinkageStoreSchemaError("linkage activation proof is absent")
        return CommittedLinkageReceipt(
            provider_namespace=revision.provider_namespace,
            store_id=metadata["store_id"],
            store_epoch_sha256=metadata["store_epoch_sha256"],
            storage_identity_sha256=metadata["storage_identity_sha256"],
            trust_pins_sha256=metadata["trust_pins_sha256"],
            linkage_id=revision.linkage_id,
            revision=revision.revision,
            linkage_revision_sha256=linkage_revision_sha256(revision),
            authorized_record_sha256=authorized_record_sha256,
            state_version=int(activation[0]),
            state_head_sha256=activation[1],
        )

    def _active_snapshot_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        evaluated_at: datetime,
        authority_time_floor: str,
    ) -> ActiveLinkageSnapshot:
        """Build an active projection inside a caller-owned transaction."""

        trust_pins = dict(self._trust_pins)
        _require_authority_time_floor(connection, authority_time_floor)
        records = _PINNED_VALIDATE_COMMITTED_STATE(self, connection)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        latest: dict[tuple[str, str], LinkageRevision] = {}
        latest_records: dict[tuple[str, str], AuthorizedLinkageRevision] = {}
        for record in records:
            key = (record.revision.provider_namespace, record.revision.linkage_id)
            latest[key] = record.revision
            latest_records[key] = record
        for key, latest_record in latest_records.items():
            if latest[key].operation != LinkageOperation.TOMBSTONE:
                _PINNED_VALIDATE_CURRENT_AUTHORITY(
                    self,
                    latest_record,
                    evaluated_at=evaluated_at,
                    expected_trust_by_provider=trust_pins,
                    authorize_revision=_PINNED_AUTHORIZE_LINKAGE_REVISION,
                )
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
        activation_coordinates = {
            (row[0], row[1], row[2]): (int(row[3]), row[4])
            for row in connection.execute(
                """SELECT provider_namespace, linkage_id, revision,
                          activation_state_version, activation_state_head_sha256
                   FROM linkage_revisions"""
            )
        }
        return ActiveLinkageSnapshot(
            state_version=state_version,
            state_head_sha256=state_head,
            store_id=metadata["store_id"],
            store_epoch_sha256=metadata["store_epoch_sha256"],
            storage_identity_sha256=metadata["storage_identity_sha256"],
            trust_pins_sha256=metadata["trust_pins_sha256"],
            revisions=revisions,
            receipts=tuple(
                CommittedLinkageReceipt(
                    provider_namespace=item.provider_namespace,
                    store_id=metadata["store_id"],
                    store_epoch_sha256=metadata["store_epoch_sha256"],
                    storage_identity_sha256=metadata["storage_identity_sha256"],
                    trust_pins_sha256=metadata["trust_pins_sha256"],
                    linkage_id=item.linkage_id,
                    revision=item.revision,
                    linkage_revision_sha256=linkage_revision_sha256(item),
                    authorized_record_sha256=hashlib.sha256(
                        _record_bytes(
                            latest_records[(item.provider_namespace, item.linkage_id)]
                        )
                    ).hexdigest(),
                    state_version=state_version,
                    state_head_sha256=state_head,
                )
                for item in revisions
            ),
            activation_receipts=tuple(
                CommittedLinkageReceipt(
                    provider_namespace=item.provider_namespace,
                    store_id=metadata["store_id"],
                    store_epoch_sha256=metadata["store_epoch_sha256"],
                    storage_identity_sha256=metadata["storage_identity_sha256"],
                    trust_pins_sha256=metadata["trust_pins_sha256"],
                    linkage_id=item.linkage_id,
                    revision=item.revision,
                    linkage_revision_sha256=linkage_revision_sha256(item),
                    authorized_record_sha256=hashlib.sha256(
                        _record_bytes(
                            latest_records[(item.provider_namespace, item.linkage_id)]
                        )
                    ).hexdigest(),
                    state_version=activation_coordinates[
                        (item.provider_namespace, item.linkage_id, item.revision)
                    ][0],
                    state_head_sha256=activation_coordinates[
                        (item.provider_namespace, item.linkage_id, item.revision)
                    ][1],
                )
                for item in revisions
            ),
        )

    @contextmanager
    def fenced_active_snapshot(self) -> Iterator[ActiveLinkageSnapshot]:
        """Fence linkage writers while a dependent durable commit completes."""

        connect_store = _PINNED_CONNECT
        capture_store_now = _capture_pinned_store_now
        with connect_store(self) as connection:
            evaluated_at, authority_time_floor = capture_store_now(self, connection)
            nested_transaction = connection.in_transaction
            if nested_transaction:
                connection.execute("SAVEPOINT fenced_active_snapshot")
            else:
                connection.execute("BEGIN IMMEDIATE")
            try:
                snapshot = _PINNED_ACTIVE_SNAPSHOT_IN_TRANSACTION(
                    self,
                    connection,
                    evaluated_at=evaluated_at,
                    authority_time_floor=authority_time_floor,
                )
                yield snapshot
            except BaseException:
                if nested_transaction:
                    connection.execute("ROLLBACK TO SAVEPOINT fenced_active_snapshot")
                    connection.execute("RELEASE SAVEPOINT fenced_active_snapshot")
                else:
                    connection.rollback()
                raise
            else:
                if nested_transaction:
                    connection.execute("RELEASE SAVEPOINT fenced_active_snapshot")
                else:
                    connection.rollback()

    @contextmanager
    def authority_read_fence(self) -> Iterator[None]:
        """Hold one validated SQLite write fence across an authority-bound read."""

        connect_store = _PINNED_CONNECT
        validate_committed_state = _PINNED_VALIDATE_COMMITTED_STATE
        with connect_store(self) as connection:
            if connection.in_transaction:
                raise ProviderLinkageStoreUnsafe(
                    "linkage store authority fence requires an idle connection"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_committed_state(self, connection)
                # Already-fenced callers distinguish this fence from other
                # store transactions (for example fenced_active_snapshot).
                self._authority_fence_thread = (os.getpid(), threading.get_ident())
                try:
                    yield
                finally:
                    self._authority_fence_thread = None
                validate_committed_state(self, connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def active_snapshot(self) -> ActiveLinkageSnapshot:
        """Read and revalidate one current, deterministic active projection."""

        with _PINNED_FENCED_ACTIVE_SNAPSHOT(self) as snapshot:
            return snapshot

    def authorized_history_in_fence(self) -> tuple[AuthorizedLinkageRevision, ...]:
        """Return validated authority history while this store's fence is held."""

        connection = self._connection
        if connection is None or not connection.in_transaction:
            raise ProviderLinkageStoreUnsafe("linkage authority fence is absent")
        return _PINNED_VALIDATE_COMMITTED_STATE(self, connection)

    def authority_time_in_fence(self) -> datetime:
        """Return the pinned authoritative evaluation time under the fence."""

        connection = self._connection
        if connection is None or not connection.in_transaction:
            raise ProviderLinkageStoreUnsafe("linkage authority fence is absent")
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='authority_time_floor'"
        ).fetchone()
        if row is None:
            raise ProviderLinkageStoreUnsafe("linkage authority time is absent")
        return _authority_time_from_text(row[0])

    def activation_receipt_history_in_fence(
        self,
    ) -> tuple[CommittedLinkageReceipt, ...]:
        """Return immutable activation receipts for validated durable history."""

        connection = self._connection
        if connection is None or not connection.in_transaction:
            raise ProviderLinkageStoreUnsafe("linkage authority fence is absent")
        records = _PINNED_VALIDATE_COMMITTED_STATE(self, connection)
        return tuple(
            _PINNED_RECEIPT(
                connection,
                record.revision,
                hashlib.sha256(_record_bytes(record)).hexdigest(),
            )
            for record in records
        )

    def verify_current_receipt(self, receipt: CommittedLinkageReceipt) -> None:
        """Require an exact receipt for the current active store head."""

        snapshot = _PINNED_ACTIVE_SNAPSHOT(self)
        if receipt not in snapshot.receipts:
            raise ProviderLinkageStoreConflict(
                "linkage receipt is not current and active"
            )


_STORE_TIME_SOURCE_LOCK = threading.RLock()
_STORE_TIME_SOURCES: dict[
    int,
    tuple[
        weakref.ReferenceType[ProviderLinkageStore],
        AuthorityTimeSource,
        datetime | None,
    ],
] = {}


def _register_store_time_source(
    store: ProviderLinkageStore, time_source: AuthorityTimeSource
) -> None:
    identity = id(store)

    def discard(reference: weakref.ReferenceType[ProviderLinkageStore]) -> None:
        with _STORE_TIME_SOURCE_LOCK:
            current = _STORE_TIME_SOURCES.get(identity)
            if current is not None and current[0] is reference:
                _STORE_TIME_SOURCES.pop(identity, None)

    reference = weakref.ref(store, discard)
    with _STORE_TIME_SOURCE_LOCK:
        _STORE_TIME_SOURCES[identity] = (reference, time_source, None)


def _unregister_store_time_source(store: ProviderLinkageStore) -> None:
    with _STORE_TIME_SOURCE_LOCK:
        current = _STORE_TIME_SOURCES.get(id(store))
        if current is not None and current[0]() is store:
            _STORE_TIME_SOURCES.pop(id(store), None)


def _registered_store_time_source(
    store: ProviderLinkageStore,
) -> AuthorityTimeSource | None:
    with _STORE_TIME_SOURCE_LOCK:
        current = _STORE_TIME_SOURCES.get(id(store))
        if current is None or current[0]() is not store:
            return None
        return current[1]


def _pinned_time_source_value(store: ProviderLinkageStore) -> datetime:
    with _STORE_TIME_SOURCE_LOCK:
        expected = _STORE_TIME_SOURCES.get(id(store))
        if expected is None or expected[0]() is not store:
            raise ProviderLinkageStoreUnsafe(
                "linkage store time source identity is absent"
            )
        reference, time_source, previous = expected
    if type(time_source) is not AuthorityTimeSource:
        raise ProviderLinkageStoreUnsafe("linkage store time source type is invalid")
    lock = time_source._lock
    if type(lock) is not _RLOCK_TYPE:
        raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
    with lock:
        mode = time_source._mode
        current_time = time_source._current
        failure = time_source._failure
    if failure == "runtime":
        raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
    if failure == "interrupt":
        raise KeyboardInterrupt
    evaluated_at = (
        datetime.now(UTC).replace(microsecond=0)
        if mode == "system"
        else current_time
        if mode == "fixed"
        else None
    )
    if (
        type(evaluated_at) is not datetime
        or evaluated_at.utcoffset() != timedelta(0)
        or evaluated_at.microsecond
    ):
        raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
    with _STORE_TIME_SOURCE_LOCK:
        current = _STORE_TIME_SOURCES.get(id(store))
        if current != expected or current[0]() is not store:
            raise ProviderLinkageStoreUnsafe(
                "linkage store time source identity changed"
            )
        if previous is not None and evaluated_at < previous:
            raise ProviderLinkageStoreUnsafe(
                "linkage store time source moved backwards"
            )
        _STORE_TIME_SOURCES[id(store)] = (reference, time_source, evaluated_at)
    return evaluated_at


def _capture_pinned_store_now(
    store: ProviderLinkageStore,
    connection: sqlite3.Connection,
) -> tuple[datetime, str]:
    nested_transaction = connection.in_transaction
    if nested_transaction:
        connection.execute("SAVEPOINT capture_store_now")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='authority_time_floor'"
        ).fetchone()
        if row is None:
            raise ProviderLinkageStoreSchemaError(
                "linkage store authority time is absent"
            )
        raw_floor = row[0]
        if type(raw_floor) is not str:
            raise ProviderLinkageStoreSchemaError(
                "linkage store authority time is invalid"
            )
        try:
            persisted = datetime.fromisoformat(raw_floor)
        except ValueError:
            raise ProviderLinkageStoreSchemaError(
                "linkage store authority time is invalid"
            ) from None
        if (
            type(persisted) is not datetime
            or persisted.utcoffset() != timedelta(0)
            or persisted.microsecond
            or persisted.isoformat() != raw_floor
        ):
            raise ProviderLinkageStoreSchemaError(
                "linkage store authority time is invalid"
            )

        with _STORE_TIME_SOURCE_LOCK:
            expected = _STORE_TIME_SOURCES.get(id(store))
            if expected is None or expected[0]() is not store:
                raise ProviderLinkageStoreUnsafe(
                    "linkage store time source identity is absent"
                )
            reference, time_source, previous = expected
        if type(time_source) is not AuthorityTimeSource:
            raise ProviderLinkageStoreUnsafe(
                "linkage store time source type is invalid"
            )
        source_lock = time_source._lock
        if type(source_lock) is not _RLOCK_TYPE:
            raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
        with source_lock:
            mode = time_source._mode
            current_time = time_source._current
            failure = time_source._failure
        if failure == "runtime":
            raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
        if failure == "interrupt":
            raise KeyboardInterrupt
        evaluated_at = (
            datetime.now(UTC).replace(microsecond=0)
            if mode == "system"
            else current_time
            if mode == "fixed"
            else None
        )
        if (
            type(evaluated_at) is not datetime
            or evaluated_at.utcoffset() != timedelta(0)
            or evaluated_at.microsecond
        ):
            raise ProviderLinkageStoreUnsafe("linkage store time source is invalid")
        with _STORE_TIME_SOURCE_LOCK:
            current = _STORE_TIME_SOURCES.get(id(store))
            if current != expected or current[0]() is not store:
                raise ProviderLinkageStoreUnsafe(
                    "linkage store time source identity changed"
                )
            if previous is not None and evaluated_at < previous:
                raise ProviderLinkageStoreUnsafe(
                    "linkage store time source moved backwards"
                )
            _STORE_TIME_SOURCES[id(store)] = (
                reference,
                time_source,
                evaluated_at,
            )
        if evaluated_at < persisted:
            raise ProviderLinkageStoreUnsafe(
                "linkage store time source moved backwards"
            )
        expected_floor = evaluated_at.isoformat()
        if evaluated_at > persisted:
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='authority_time_floor'",
                (expected_floor,),
            )
        observed = connection.execute(
            "SELECT value FROM metadata WHERE key='authority_time_floor'"
        ).fetchone()
        if observed is None or observed[0] != expected_floor:
            raise ProviderLinkageStoreUnsafe(
                "linkage store authority time changed during capture"
            )
        if nested_transaction:
            connection.execute("RELEASE SAVEPOINT capture_store_now")
        else:
            connection.commit()
    except BaseException as error:
        if nested_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT capture_store_now")
            connection.execute("RELEASE SAVEPOINT capture_store_now")
        else:
            connection.rollback()
        if isinstance(error, ProviderLinkageStoreError):
            raise
        if isinstance(error, sqlite3.DatabaseError):
            raise ProviderLinkageStoreSchemaError(
                "linkage store authority time update failed"
            ) from None
        raise
    return evaluated_at, expected_floor


def _require_authority_time_floor(
    connection: sqlite3.Connection,
    expected_floor: str,
) -> None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='authority_time_floor'"
    ).fetchone()
    if row is None:
        raise ProviderLinkageStoreSchemaError("linkage store authority time is absent")
    if row[0] != expected_floor:
        raise ProviderLinkageStoreUnsafe(
            "linkage store authority time changed before state validation"
        )


_PINNED_AUTHORITY_TIME_SOURCE_READ = AuthorityTimeSource.read
_PINNED_AUTHORITY_TIME_SOURCE_ADVANCE = AuthorityTimeSource.advance_to
_PINNED_AUTHORITY_TIME_SOURCE_SET_FAILURE = AuthorityTimeSource.set_failure
_PINNED_VALIDATE_STORAGE = ProviderLinkageStore._validate_storage
_PINNED_STORAGE_IDENTITY_SHA256 = ProviderLinkageStore._storage_identity_sha256
_PINNED_SECURE_DATABASE_FILES = ProviderLinkageStore._secure_database_files
_PINNED_BIND_DATABASE_DESCRIPTOR = ProviderLinkageStore._bind_database_descriptor
_PINNED_OPEN_CONNECTION = ProviderLinkageStore._open_connection
_PINNED_CONNECT = ProviderLinkageStore._connect
_PINNED_VALIDATE_SCHEMA = ProviderLinkageStore._validate_schema
_PINNED_LOAD_RECORDS = ProviderLinkageStore._load_records
_PINNED_STATE_HEAD = ProviderLinkageStore._state_head
_PINNED_VALIDATE_COMMITTED_STATE = ProviderLinkageStore._validate_committed_state
_PINNED_VALIDATE_CURRENT_AUTHORITY = ProviderLinkageStore._validate_current_authority
_PINNED_VERIFY_CONSUMPTIONS = ProviderLinkageStore._verify_consumptions
_PINNED_RECEIPT = ProviderLinkageStore._receipt
_PINNED_ACTIVE_SNAPSHOT_IN_TRANSACTION = (
    ProviderLinkageStore._active_snapshot_in_transaction
)
_PINNED_FENCED_ACTIVE_SNAPSHOT = ProviderLinkageStore.fenced_active_snapshot
_PINNED_AUTHORIZED_HISTORY_IN_FENCE = ProviderLinkageStore.authorized_history_in_fence
_PINNED_AUTHORITY_TIME_IN_FENCE = ProviderLinkageStore.authority_time_in_fence
_PINNED_ACTIVATION_RECEIPT_HISTORY_IN_FENCE = (
    ProviderLinkageStore.activation_receipt_history_in_fence
)
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PROCESS_INTEGRITY_FUNCTIONS = (
    _authority_time_from_text,
    _authority_time_text,
    _pinned_time_source_value,
    _capture_pinned_store_now,
    _require_authority_time_floor,
    _system_authority_time,
    AuthorityTimeSource.read,
    AuthorityTimeSource.advance_to,
    AuthorityTimeSource.set_failure,
    ProviderLinkageStore.commit_authorized_revision,
    ProviderLinkageStore._validate_current_authority,
    ProviderLinkageStore._active_snapshot_in_transaction,
    ProviderLinkageStore.fenced_active_snapshot,
    ProviderLinkageStore.authorized_history_in_fence,
    ProviderLinkageStore.authority_time_in_fence,
    ProviderLinkageStore.activation_receipt_history_in_fence,
    ProviderLinkageStore.active_snapshot,
    ProviderLinkageStore.authority_read_fence,
    ProviderLinkageStore.authority_read_fence.__wrapped__,
)
_PROCESS_INTEGRITY_FUNCTION_STATES = tuple(
    (
        function.__code__,
        function.__defaults__,
        (
            tuple(sorted(function.__kwdefaults__.items()))
            if function.__kwdefaults__ is not None
            else None
        ),
        (
            tuple((id(cell), id(cell.cell_contents)) for cell in function.__closure__)
            if function.__closure__ is not None
            else None
        ),
    )
    for function in _PROCESS_INTEGRITY_FUNCTIONS
)
_PINNED_STORE_TIME_SOURCES = _STORE_TIME_SOURCES
_PINNED_STORE_TIME_SOURCE_LOCK = _STORE_TIME_SOURCE_LOCK


def provider_linkage_store_time_source_is_pinned(
    store: ProviderLinkageStore,
) -> bool:
    """Return whether the store retains its exact package-owned time source."""

    try:
        current = vars(store).get("_time_source")
        with _PINNED_STORE_TIME_SOURCE_LOCK:
            entry = _PINNED_STORE_TIME_SOURCES.get(id(store))
        return (
            type(current) is AuthorityTimeSource
            and type(_STORE_TIME_SOURCE_LOCK) is _RLOCK_TYPE
            and _STORE_TIME_SOURCE_LOCK is _PINNED_STORE_TIME_SOURCE_LOCK
            and _STORE_TIME_SOURCES is _PINNED_STORE_TIME_SOURCES
            and entry is not None
            and entry[0]() is store
            and entry[1] is current
            and type(entry[2]) is datetime
            and entry[2].utcoffset() == timedelta(0)
            and entry[2].microsecond == 0
        )
    except (AttributeError, TypeError, LookupError, RuntimeError):
        return False


def provider_linkage_store_process_integrity_is_valid() -> bool:
    """Best-effort detection of mutation in the loaded authority implementation.

    This diagnostic is not an authorization root. A principal able to rewrite
    installed Python code can also rewrite this check or its baseline; deployment
    must establish package and process integrity outside this interpreter.
    """

    try:
        observed_function_states = tuple(
            (
                function.__code__,
                function.__defaults__,
                (
                    tuple(sorted(function.__kwdefaults__.items()))
                    if function.__kwdefaults__ is not None
                    else None
                ),
                (
                    tuple(
                        (id(cell), id(cell.cell_contents))
                        for cell in function.__closure__
                    )
                    if function.__closure__ is not None
                    else None
                ),
            )
            for function in _PROCESS_INTEGRITY_FUNCTIONS
        )
        return (
            _PROCESS_INTEGRITY_FUNCTIONS
            == (
                _authority_time_from_text,
                _authority_time_text,
                _pinned_time_source_value,
                _capture_pinned_store_now,
                _require_authority_time_floor,
                _system_authority_time,
                AuthorityTimeSource.read,
                AuthorityTimeSource.advance_to,
                AuthorityTimeSource.set_failure,
                ProviderLinkageStore.commit_authorized_revision,
                ProviderLinkageStore._validate_current_authority,
                ProviderLinkageStore._active_snapshot_in_transaction,
                ProviderLinkageStore.fenced_active_snapshot,
                ProviderLinkageStore.authorized_history_in_fence,
                ProviderLinkageStore.authority_time_in_fence,
                ProviderLinkageStore.activation_receipt_history_in_fence,
                ProviderLinkageStore.active_snapshot,
                ProviderLinkageStore.authority_read_fence,
                ProviderLinkageStore.authority_read_fence.__wrapped__,
            )
            and observed_function_states == _PROCESS_INTEGRITY_FUNCTION_STATES
            and _PINNED_AUTHORITY_TIME_SOURCE_READ is AuthorityTimeSource.read
            and _PINNED_AUTHORITY_TIME_SOURCE_ADVANCE is AuthorityTimeSource.advance_to
            and _PINNED_AUTHORITY_TIME_SOURCE_SET_FAILURE
            is AuthorityTimeSource.set_failure
            and _PINNED_AUTHORIZE_LINKAGE_REVISION is authorize_linkage_revision
            and _PINNED_VALIDATE_CURRENT_AUTHORITY
            is ProviderLinkageStore._validate_current_authority
        )
    except (AttributeError, TypeError, LookupError, RuntimeError):
        return False


def require_provider_linkage_store_process_integrity() -> None:
    """Raise an explicit diagnostic failure when loaded-code mutation is seen."""

    if not provider_linkage_store_process_integrity_is_valid():
        raise ProviderLinkageStoreUnsafe("linkage store process integrity check failed")


__all__ = [
    "ActiveLinkageSnapshot",
    "AuthorityTimeSource",
    "CommittedLinkageReceipt",
    "ProviderLinkageStore",
    "ProviderLinkageStoreConflict",
    "ProviderLinkageStoreError",
    "ProviderLinkageStoreSchemaError",
    "ProviderLinkageStoreUnsafe",
    "capture_expected_trust_pins",
    "committed_linkage_receipt_sha256",
    "provider_linkage_store_process_integrity_is_valid",
    "provider_linkage_store_time_source_is_pinned",
    "require_provider_linkage_store_process_integrity",
]
