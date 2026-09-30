"""Durable D04 record supersession and derived-comparison invalidation.

The ledger is provider-local, append-only, and bound to one live
``ProviderLinkageStore``.  It contains opaque identifiers and content digests
only.  A reanalysis is a technical descendant of an existing record and never
creates a biological collection or denominator contribution.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from evidence_inspector.method_registry import (
    RegistryContract,
    RegistryIdentityError,
    Sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage import (
    MAX_REVISIONS,
    AnalysisRecordId,
    LinkageId,
    ProviderNamespace,
    linkage_revision_sha256,
)
from evidence_inspector.provider_linkage_store import (
    ActiveLinkageSnapshot,
    ProviderLinkageStore,
    ProviderLinkageStoreError,
    committed_linkage_receipt_sha256,
)

SCHEMA_VERSION = 1
MAX_RECORDS = 100_000
MAX_COMPARISONS = 100_000
MAX_COMPARISON_MEMBERS = 1_000

RecordId = Annotated[str, StringConstraints(pattern=r"^record_[0-9a-f]{40}$")]
ComparisonId = Annotated[str, StringConstraints(pattern=r"^comparison_[0-9a-f]{40}$")]
ResultId = Annotated[str, StringConstraints(pattern=r"^result_[0-9a-f]{40}$")]
LedgerId = Annotated[str, StringConstraints(pattern=r"^ledger_[0-9a-f]{32}$")]
LinkageStoreId = Annotated[str, StringConstraints(pattern=r"^store_[0-9a-f]{32}$")]

_RECORD_ID = TypeAdapter(RecordId)
_COMPARISON_ID = TypeAdapter(ComparisonId)
_RESULT_ID = TypeAdapter(ResultId)
_PROVIDER_NAMESPACE = TypeAdapter(ProviderNamespace)
_ANALYSIS_RECORD_ID = TypeAdapter(AnalysisRecordId)
_SHA256 = TypeAdapter(Sha256)
_LINKAGE_STORE_ID = TypeAdapter(LinkageStoreId)
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_STORE_CALLABLES = {
    name: getattr(ProviderLinkageStore, name)
    for name in vars(ProviderLinkageStore)
    if callable(getattr(ProviderLinkageStore, name))
}
_SQLITE_OPEN_LOCK = threading.RLock()


class RecordSupersessionError(RuntimeError):
    """Sanitized durable-ledger failure."""


class RecordSupersessionConflict(RecordSupersessionError):
    pass


class RecordSupersessionUnsafe(RecordSupersessionError):
    pass


class RecordLineageRole(StrEnum):
    PRIMARY_ANALYSIS = "primary_analysis"
    REANALYSIS = "reanalysis"


class ComparisonState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class InvalidationReason(StrEnum):
    RECORD_SUPERSEDED = "record_superseded"
    LINKAGE_AUTHORITY_ADVANCED = "linkage_authority_advanced"
    LINKAGE_CHANGED_OR_TOMBSTONED = "linkage_changed_or_tombstoned"


class SupersedingRecord(RegistryContract):
    """One immutable result identity and its exact biological authority."""

    schema_version: Literal["traceback.superseding-record.v1"] = (
        "traceback.superseding-record.v1"
    )
    record_id: RecordId
    provider_namespace: ProviderNamespace
    analysis_record_id: AnalysisRecordId
    result_id: ResultId
    result_sha256: Sha256
    bundle_sha256: Sha256
    linkage_id: LinkageId
    linkage_revision: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
    linkage_revision_sha256: Sha256
    activation_receipt_sha256: Sha256
    lineage_role: RecordLineageRole
    reanalysis_of_record_id: RecordId | None = None
    supersedes_record_id: RecordId | None = None
    biological_timepoint_contribution: Literal[False] = False

    @model_validator(mode="after")
    def coherent_role(self) -> SupersedingRecord:
        derived = self.lineage_role == RecordLineageRole.REANALYSIS
        if derived != (self.reanalysis_of_record_id is not None):
            raise ValueError("reanalysis role must bind its source record")
        if derived != (self.supersedes_record_id is not None):
            raise ValueError("reanalysis must supersede its source record")
        if self.record_id in {self.reanalysis_of_record_id, self.supersedes_record_id}:
            raise ValueError("a record cannot supersede or derive from itself")
        expected = make_record_id(
            provider_namespace=self.provider_namespace,
            analysis_record_id=self.analysis_record_id,
            result_id=self.result_id,
            result_sha256=self.result_sha256,
            bundle_sha256=self.bundle_sha256,
        )
        if self.record_id != expected:
            raise ValueError("record identity is not derived from exact content")
        return self


class DerivedComparison(RegistryContract):
    """A persisted derived artifact over exact active record identities."""

    schema_version: Literal["traceback.derived-comparison.v1"] = (
        "traceback.derived-comparison.v1"
    )
    comparison_id: ComparisonId
    member_record_ids: tuple[RecordId, ...] = Field(
        min_length=2, max_length=MAX_COMPARISON_MEMBERS
    )
    derived_artifact_sha256: Sha256

    @model_validator(mode="after")
    def canonical_identity(self) -> DerivedComparison:
        if self.member_record_ids != tuple(sorted(set(self.member_record_ids))):
            raise ValueError("comparison members must be uniquely sorted")
        expected = make_comparison_id(
            self.member_record_ids, self.derived_artifact_sha256
        )
        if self.comparison_id != expected:
            raise ValueError("comparison identity is not derived from exact content")
        return self


class RecordCommitReceipt(RegistryContract):
    schema_version: Literal["traceback.record-commit-receipt.v1"] = (
        "traceback.record-commit-receipt.v1"
    )
    ledger_id: LedgerId
    ledger_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    record_id: RecordId
    record_sha256: Sha256


class ComparisonCommitReceipt(RegistryContract):
    schema_version: Literal["traceback.comparison-commit-receipt.v1"] = (
        "traceback.comparison-commit-receipt.v1"
    )
    ledger_id: LedgerId
    ledger_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    comparison_id: ComparisonId
    comparison_sha256: Sha256


class ComparisonStatus(RegistryContract):
    schema_version: Literal["traceback.derived-comparison-status.v1"] = (
        "traceback.derived-comparison-status.v1"
    )
    comparison_id: ComparisonId
    state: ComparisonState
    reasons: tuple[InvalidationReason, ...]
    member_record_ids: tuple[RecordId, ...]
    derived_artifact_sha256: Sha256
    linkage_state_version: int = Field(ge=0, le=MAX_REVISIONS)
    linkage_state_head_sha256: Sha256

    @model_validator(mode="after")
    def coherent_state(self) -> ComparisonStatus:
        if self.reasons != tuple(sorted(set(self.reasons), key=str)):
            raise ValueError("comparison invalidation reasons must be uniquely sorted")
        if (self.state == ComparisonState.STALE) != bool(self.reasons):
            raise ValueError("stale comparison state must have reasons")
        return self


class ActiveRecordSnapshot(RegistryContract):
    schema_version: Literal["traceback.active-record-snapshot.v1"] = (
        "traceback.active-record-snapshot.v1"
    )
    ledger_id: LedgerId
    ledger_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_RECORDS + MAX_COMPARISONS * 4)
    state_head_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=0, le=MAX_REVISIONS)
    linkage_state_head_sha256: Sha256
    records: tuple[SupersedingRecord, ...] = Field(max_length=MAX_RECORDS)

    @model_validator(mode="after")
    def canonical_records(self) -> ActiveRecordSnapshot:
        keys = tuple(item.record_id for item in self.records)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("active records must be uniquely sorted")
        return self


def _digest(domain: bytes, content: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + content).hexdigest()


def make_record_id(
    *,
    provider_namespace: str,
    analysis_record_id: str,
    result_id: str,
    result_sha256: str,
    bundle_sha256: str,
) -> str:
    raw_values = (
        provider_namespace,
        analysis_record_id,
        result_id,
        result_sha256,
        bundle_sha256,
    )
    if any(type(item) is not str for item in raw_values):
        raise ValueError("record identity inputs are invalid")
    provider_namespace = _PROVIDER_NAMESPACE.validate_python(
        provider_namespace, strict=True
    )
    analysis_record_id = _ANALYSIS_RECORD_ID.validate_python(
        analysis_record_id, strict=True
    )
    result_id = _RESULT_ID.validate_python(result_id, strict=True)
    result_sha256 = _SHA256.validate_python(result_sha256, strict=True)
    bundle_sha256 = _SHA256.validate_python(bundle_sha256, strict=True)
    content = (
        provider_namespace.encode("ascii")
        + b"\0"
        + analysis_record_id.encode("ascii")
        + b"\0"
        + result_id.encode("ascii")
        + b"\0"
        + result_sha256.encode("ascii")
        + b"\0"
        + bundle_sha256.encode("ascii")
    )
    return _RECORD_ID.validate_python(
        f"record_{_digest(b'traceback-record-id-v1', content)[:40]}", strict=True
    )


def make_comparison_id(
    member_record_ids: Sequence[str], derived_artifact_sha256: str
) -> str:
    if (
        type(member_record_ids) is not tuple
        or not 2 <= len(member_record_ids) <= MAX_COMPARISON_MEMBERS
        or any(type(item) is not str for item in member_record_ids)
        or type(derived_artifact_sha256) is not str
    ):
        raise ValueError("comparison identity inputs are invalid")
    member_record_ids = tuple(
        _RECORD_ID.validate_python(item, strict=True) for item in member_record_ids
    )
    derived_artifact_sha256 = _SHA256.validate_python(
        derived_artifact_sha256, strict=True
    )
    content = b"\0".join(
        [item.encode("ascii") for item in member_record_ids]
        + [derived_artifact_sha256.encode("ascii")]
    )
    return _COMPARISON_ID.validate_python(
        f"comparison_{_digest(b'traceback-comparison-id-v1', content)[:40]}",
        strict=True,
    )


def record_sha256(record: SupersedingRecord) -> str:
    return hashlib.sha256(canonical_contract_bytes(record)).hexdigest()


def comparison_sha256(comparison: DerivedComparison) -> str:
    return hashlib.sha256(canonical_contract_bytes(comparison)).hexdigest()


_SCHEMA = (
    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE records(
        sequence INTEGER PRIMARY KEY,
        record_id TEXT NOT NULL UNIQUE,
        provider_namespace TEXT NOT NULL,
        analysis_record_id TEXT NOT NULL,
        result_id TEXT NOT NULL UNIQUE,
        supersedes_record_id TEXT UNIQUE,
        record_sha256 TEXT NOT NULL UNIQUE,
        record_json BLOB NOT NULL,
        UNIQUE(provider_namespace, analysis_record_id),
        FOREIGN KEY(supersedes_record_id) REFERENCES records(record_id)
    )""",
    """CREATE TABLE comparisons(
        sequence INTEGER PRIMARY KEY,
        comparison_id TEXT NOT NULL UNIQUE,
        comparison_sha256 TEXT NOT NULL UNIQUE,
        linkage_state_version INTEGER NOT NULL,
        linkage_state_head_sha256 TEXT NOT NULL,
        comparison_json BLOB NOT NULL
    )""",
    """CREATE TABLE invalidations(
        sequence INTEGER PRIMARY KEY,
        comparison_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        observed_linkage_head_sha256 TEXT NOT NULL,
        UNIQUE(comparison_id, reason),
        FOREIGN KEY(comparison_id) REFERENCES comparisons(comparison_id)
    )""",
    "CREATE INDEX records_provider_analysis ON records(provider_namespace, analysis_record_id)",
    "CREATE INDEX invalidations_comparison ON invalidations(comparison_id, reason)",
)


def _normalize_sql(value: str) -> str:
    return "".join(value.split()).casefold()


class RecordSupersessionStore:
    """Append-only local record ledger bound to exact live linkage authority."""

    def __init__(
        self, root: str | Path, *, linkage_store: ProviderLinkageStore
    ) -> None:
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("record ledger requires an exact ProviderLinkageStore")
        if any(
            getattr(ProviderLinkageStore, name, None) is not expected
            for name, expected in _PINNED_STORE_CALLABLES.items()
        ):
            raise RecordSupersessionUnsafe("linkage store implementation changed")
        self.root = Path(root).absolute()
        self.linkage_store = linkage_store
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise RecordSupersessionUnsafe("record ledger root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RecordSupersessionUnsafe("record ledger root must be private")
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        self.database = self.root / "record-supersession.sqlite3"
        self._database_identity: tuple[int, int] | None = None
        try:
            descriptor = os.open(
                self.database,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        database_metadata = self.database.stat(follow_symlinks=False)
        self._database_identity = (
            database_metadata.st_dev,
            database_metadata.st_ino,
        )
        self._initialize()
        database_metadata = self.database.stat(follow_symlinks=False)
        self._database_identity = (
            database_metadata.st_dev,
            database_metadata.st_ino,
        )

    def _validate_storage(self) -> None:
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._root_identity
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RecordSupersessionUnsafe("record ledger storage changed")
        if self.database.is_symlink():
            raise RecordSupersessionUnsafe("record ledger database is unsafe")
        if self.database.exists():
            db = self.database.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(db.st_mode)
                or stat.S_IMODE(db.st_mode) != 0o600
                or (
                    self._database_identity is not None
                    and (db.st_dev, db.st_ino) != self._database_identity
                )
            ):
                raise RecordSupersessionUnsafe("record ledger database must be private")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.database) + suffix)
            if sidecar.exists():
                metadata = sidecar.stat(follow_symlinks=False)
                if (
                    sidecar.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise RecordSupersessionUnsafe("record ledger sidecar is unsafe")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            with _SQLITE_OPEN_LOCK:
                self._validate_storage()
                connection = sqlite3.connect(
                    self.database, timeout=5.0, isolation_level=None
                )
                os.chmod(self.database, 0o600)
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA journal_mode=WAL")
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(self.database) + suffix)
                    if sidecar.exists():
                        sidecar.chmod(0o600)
            yield connection
        finally:
            if connection is not None:
                connection.close()
            self._validate_storage()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                existing_objects = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0]
                )
                if existing_objects == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    linkage = self._linkage_snapshot()
                    ledger_id = f"ledger_{secrets.token_hex(16)}"
                    epoch = secrets.token_hex(32)
                    identity = _digest(
                        b"traceback-record-ledger-storage-v1",
                        (
                            ledger_id
                            + "\0"
                            + linkage.store_id
                            + "\0"
                            + linkage.store_epoch_sha256
                            + "\0"
                            + linkage.storage_identity_sha256
                        ).encode("ascii"),
                    )
                    values = {
                        "schema_version": str(SCHEMA_VERSION),
                        "ledger_id": ledger_id,
                        "ledger_epoch_sha256": epoch,
                        "storage_identity_sha256": identity,
                        "linkage_store_id": linkage.store_id,
                        "linkage_store_epoch_sha256": linkage.store_epoch_sha256,
                        "linkage_storage_identity_sha256": linkage.storage_identity_sha256,
                        "state_version": "0",
                        "state_head_sha256": "0" * 64,
                    }
                    connection.executemany(
                        "INSERT INTO metadata VALUES(?, ?)", values.items()
                    )
                    connection.execute(
                        "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
                        (self._state_head(connection),),
                    )
                self._validate_state(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        os.chmod(self.database, 0o600)
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.database) + suffix)
            if path.exists():
                path.chmod(0o600)

    def _linkage_snapshot(self) -> ActiveLinkageSnapshot:
        try:
            snapshot = _PINNED_ACTIVE_SNAPSHOT(self.linkage_store)
        except (
            ProviderLinkageStoreError,
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
        ):
            raise RecordSupersessionUnsafe(
                "live linkage authority is unavailable"
            ) from None
        if type(snapshot) is not ActiveLinkageSnapshot:
            raise RecordSupersessionUnsafe("live linkage authority is invalid")
        return snapshot

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
        return dict(connection.execute("SELECT key, value FROM metadata"))

    @staticmethod
    def _state_head(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        for table, columns in (
            ("records", "sequence, record_sha256, record_json"),
            (
                "comparisons",
                "sequence, comparison_sha256, linkage_state_version, linkage_state_head_sha256, comparison_json",
            ),
            (
                "invalidations",
                "sequence, comparison_id, reason, observed_linkage_head_sha256",
            ),
        ):
            digest.update(table.encode("ascii") + b"\0")
            for row in connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY sequence"
            ):
                for value in row:
                    raw = (
                        bytes(value)
                        if isinstance(value, bytes)
                        else str(value).encode("ascii")
                    )
                    digest.update(len(raw).to_bytes(8, "big") + raw)
        return digest.hexdigest()

    def _validate_state(self, connection: sqlite3.Connection) -> None:
        expected_schema = {
            (
                "table" if statement.startswith("CREATE TABLE") else "index",
                statement.split()[2].split("(")[0],
            ): _normalize_sql(statement)
            for statement in _SCHEMA
        }
        observed = {
            (row[0], row[1]): _normalize_sql(row[2])
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if observed != expected_schema:
            raise RecordSupersessionUnsafe("record ledger schema is invalid")
        metadata = self._metadata(connection)
        expected_keys = {
            "schema_version",
            "ledger_id",
            "ledger_epoch_sha256",
            "storage_identity_sha256",
            "linkage_store_id",
            "linkage_store_epoch_sha256",
            "linkage_storage_identity_sha256",
            "state_version",
            "state_head_sha256",
        }
        if set(metadata) != expected_keys or metadata["schema_version"] != str(
            SCHEMA_VERSION
        ):
            raise RecordSupersessionUnsafe("record ledger metadata is invalid")
        try:
            TypeAdapter(LedgerId).validate_python(metadata["ledger_id"], strict=True)
            for key in (
                "ledger_epoch_sha256",
                "storage_identity_sha256",
                "linkage_store_epoch_sha256",
                "linkage_storage_identity_sha256",
                "state_head_sha256",
            ):
                _SHA256.validate_python(metadata[key], strict=True)
            _LINKAGE_STORE_ID.validate_python(metadata["linkage_store_id"], strict=True)
            state_version = int(metadata["state_version"])
            if str(state_version) != metadata["state_version"] or state_version < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise RecordSupersessionUnsafe(
                "record ledger metadata is invalid"
            ) from None
        linkage = self._linkage_snapshot()
        if (
            metadata["linkage_store_id"],
            metadata["linkage_store_epoch_sha256"],
            metadata["linkage_storage_identity_sha256"],
        ) != (
            linkage.store_id,
            linkage.store_epoch_sha256,
            linkage.storage_identity_sha256,
        ):
            raise RecordSupersessionUnsafe(
                "record ledger is bound to another linkage store"
            )
        records: list[SupersedingRecord] = []
        for row in connection.execute(
            """SELECT record_id, provider_namespace, analysis_record_id,
                      result_id, supersedes_record_id, record_sha256, record_json
               FROM records ORDER BY sequence"""
        ):
            raw = bytes(row[6])
            try:
                record = contract_from_canonical_bytes(SupersedingRecord, raw)
            except Exception:
                raise RecordSupersessionUnsafe(
                    "record ledger history is invalid"
                ) from None
            if tuple(row[:6]) != (
                record.record_id,
                record.provider_namespace,
                record.analysis_record_id,
                record.result_id,
                record.supersedes_record_id,
                record_sha256(record),
            ):
                raise RecordSupersessionUnsafe(
                    "record ledger history binding is invalid"
                )
            records.append(record)
        if len(records) > MAX_RECORDS:
            raise RecordSupersessionUnsafe("record ledger exceeds its bound")
        self._validate_record_history(records)
        record_ids = {item.record_id for item in records}
        comparison_ids: set[str] = set()
        comparisons = 0
        for row in connection.execute(
            """SELECT comparison_id, comparison_sha256, linkage_state_version,
                      linkage_state_head_sha256, comparison_json
               FROM comparisons ORDER BY sequence"""
        ):
            raw = bytes(row[4])
            try:
                comparison = contract_from_canonical_bytes(DerivedComparison, raw)
            except Exception:
                raise RecordSupersessionUnsafe(
                    "comparison history is invalid"
                ) from None
            try:
                linkage_head = _SHA256.validate_python(row[3], strict=True)
            except (TypeError, ValueError):
                raise RecordSupersessionUnsafe(
                    "comparison history binding is invalid"
                ) from None
            if (
                row[0] != comparison.comparison_id
                or row[1] != comparison_sha256(comparison)
                or type(row[2]) is not int
                or not 0 <= row[2] <= MAX_REVISIONS
                or linkage_head != row[3]
                or not set(comparison.member_record_ids).issubset(record_ids)
            ):
                raise RecordSupersessionUnsafe("comparison history binding is invalid")
            comparison_ids.add(comparison.comparison_id)
            comparisons += 1
        invalidations = 0
        for row in connection.execute(
            """SELECT comparison_id, reason, observed_linkage_head_sha256
               FROM invalidations ORDER BY sequence"""
        ):
            try:
                reason = InvalidationReason(row[1])
                observed = _SHA256.validate_python(row[2], strict=True)
            except (TypeError, ValueError):
                raise RecordSupersessionUnsafe(
                    "comparison invalidation history is invalid"
                ) from None
            if (
                row[0] not in comparison_ids
                or reason.value != row[1]
                or observed != row[2]
            ):
                raise RecordSupersessionUnsafe(
                    "comparison invalidation history is invalid"
                )
            invalidations += 1
        if comparisons > MAX_COMPARISONS or invalidations > MAX_COMPARISONS * 3:
            raise RecordSupersessionUnsafe("comparison history exceeds its bound")
        version = len(records) + comparisons + invalidations
        if state_version != version or metadata[
            "state_head_sha256"
        ] != self._state_head(connection):
            raise RecordSupersessionUnsafe("record ledger state commitment is invalid")

    @staticmethod
    def _validate_record_history(records: Sequence[SupersedingRecord]) -> None:
        by_id: dict[str, SupersedingRecord] = {}
        successor: set[str] = set()
        for record in records:
            if record.record_id in by_id:
                raise RecordSupersessionUnsafe("record identity is duplicated")
            if record.supersedes_record_id is not None:
                source = by_id.get(record.supersedes_record_id)
                if source is None or record.reanalysis_of_record_id != source.record_id:
                    raise RecordSupersessionUnsafe(
                        "record supersession chain is invalid"
                    )
                if source.record_id in successor:
                    raise RecordSupersessionUnsafe("record has multiple successors")
                successor.add(source.record_id)
            by_id[record.record_id] = record
        completed: set[str] = set()
        for start in by_id:
            if start in completed:
                continue
            seen: set[str] = set()
            current: str | None = start
            while current is not None and current not in completed:
                if current in seen:
                    raise RecordSupersessionUnsafe("record supersession cycle detected")
                seen.add(current)
                current = by_id[current].supersedes_record_id
            completed.update(seen)

    @staticmethod
    def _advance(connection: sqlite3.Connection) -> tuple[int, str]:
        version = sum(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("records", "comparisons", "invalidations")
        )
        head = RecordSupersessionStore._state_head(connection)
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_version'", (str(version),)
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'", (head,)
        )
        return version, head

    @staticmethod
    def _active_linkages(
        snapshot: ActiveLinkageSnapshot,
    ) -> dict[tuple[str, str], tuple[object, object]]:
        return {
            (revision.provider_namespace, revision.linkage_id): (revision, receipt)
            for revision, receipt in zip(
                snapshot.revisions, snapshot.receipts, strict=True
            )
        }

    def _validate_record_against_linkage(
        self,
        record: SupersedingRecord,
        snapshot: ActiveLinkageSnapshot,
        *,
        source: SupersedingRecord | None,
    ) -> None:
        item = self._active_linkages(snapshot).get(
            (record.provider_namespace, record.linkage_id)
        )
        if item is None:
            raise RecordSupersessionConflict("record linkage is not active")
        revision, receipt = item
        if (
            record.linkage_revision != revision.revision
            or record.linkage_revision_sha256 != linkage_revision_sha256(revision)
            or record.activation_receipt_sha256
            != committed_linkage_receipt_sha256(receipt)
            or record.analysis_record_id != revision.technical.analysis_record_id
        ):
            raise RecordSupersessionConflict("record does not bind exact live linkage")
        upstream = revision.technical.reanalysis_of.token
        if record.lineage_role == RecordLineageRole.PRIMARY_ANALYSIS:
            if upstream is not None or source is not None:
                raise RecordSupersessionConflict(
                    "primary analysis cannot claim reanalysis lineage"
                )
            return
        if source is None or upstream != source.analysis_record_id:
            raise RecordSupersessionConflict(
                "reanalysis does not bind its source analysis"
            )
        source_item = self._active_linkages(snapshot).get(
            (source.provider_namespace, source.linkage_id)
        )
        if source_item is None:
            raise RecordSupersessionConflict("reanalysis source linkage is not active")
        source_revision = source_item[0]
        if (
            source_revision.revision != source.linkage_revision
            or linkage_revision_sha256(source_revision)
            != source.linkage_revision_sha256
            or source_revision.technical.analysis_record_id != source.analysis_record_id
            or source.provider_namespace != record.provider_namespace
            or source_revision.biological != revision.biological
        ):
            raise RecordSupersessionConflict(
                "reanalysis source is stale or changed biological lineage"
            )

    def commit_record(self, record: SupersedingRecord) -> RecordCommitReceipt:
        """Append one exact record, or return the receipt for an exact retry."""

        if type(record) is not SupersedingRecord:
            raise RecordSupersessionConflict("record contract is invalid")
        self._require_safe_record_shape(record)
        try:
            parsed = contract_from_canonical_bytes(
                SupersedingRecord, canonical_contract_bytes(record)
            )
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict("record contract is invalid") from None
        raw = canonical_contract_bytes(parsed)
        digest = hashlib.sha256(raw).hexdigest()
        linkage = self._linkage_snapshot()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection)
                existing = connection.execute(
                    "SELECT record_sha256, record_json FROM records WHERE record_id=?",
                    (parsed.record_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != digest or bytes(existing[1]) != raw:
                        raise RecordSupersessionConflict(
                            "record identity conflicts with history"
                        )
                    source = self._source(connection, parsed.supersedes_record_id)
                    self._validate_record_against_linkage(
                        parsed, linkage, source=source
                    )
                    receipt = self._record_receipt(connection, parsed, digest)
                    connection.commit()
                    return receipt
                if (
                    int(
                        connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                    )
                    >= MAX_RECORDS
                ):
                    raise RecordSupersessionConflict("record ledger is full")
                source = self._source(connection, parsed.supersedes_record_id)
                self._validate_record_against_linkage(parsed, linkage, source=source)
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM records"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO records VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        parsed.record_id,
                        parsed.provider_namespace,
                        parsed.analysis_record_id,
                        parsed.result_id,
                        parsed.supersedes_record_id,
                        digest,
                        raw,
                    ),
                )
                if parsed.supersedes_record_id is not None:
                    self._invalidate_for_record(
                        connection,
                        parsed.supersedes_record_id,
                        InvalidationReason.RECORD_SUPERSEDED,
                        linkage.state_head_sha256,
                    )
                version, head = self._advance(connection)
                self._validate_state(connection)
                receipt = self._record_receipt(
                    connection, parsed, digest, version=version, head=head
                )
                connection.commit()
                return receipt
            except BaseException as error:
                connection.rollback()
                if isinstance(error, RecordSupersessionError):
                    raise
                if isinstance(error, sqlite3.IntegrityError):
                    raise RecordSupersessionConflict(
                        "record conflicts with durable history"
                    ) from None
                raise

    @staticmethod
    def _source(
        connection: sqlite3.Connection, record_id: str | None
    ) -> SupersedingRecord | None:
        if record_id is None:
            return None
        row = connection.execute(
            "SELECT record_json FROM records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordSupersessionConflict("supersession source is unknown")
        return contract_from_canonical_bytes(SupersedingRecord, bytes(row[0]))

    @staticmethod
    def _invalidate_for_record(
        connection: sqlite3.Connection,
        record_id: str,
        reason: InvalidationReason,
        linkage_head: str,
    ) -> None:
        next_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM invalidations"
            ).fetchone()[0]
        )
        for row in connection.execute(
            "SELECT comparison_id, comparison_json FROM comparisons ORDER BY sequence"
        ):
            comparison = contract_from_canonical_bytes(DerivedComparison, bytes(row[1]))
            if record_id not in comparison.member_record_ids:
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO invalidations VALUES(?, ?, ?, ?)",
                (next_sequence, comparison.comparison_id, reason.value, linkage_head),
            )
            if cursor.rowcount:
                next_sequence += 1

    def register_comparison(
        self, comparison: DerivedComparison
    ) -> ComparisonCommitReceipt:
        if type(comparison) is not DerivedComparison:
            raise RecordSupersessionConflict("comparison contract is invalid")
        if (
            type(comparison.member_record_ids) is not tuple
            or any(type(item) is not str for item in comparison.member_record_ids)
            or type(comparison.comparison_id) is not str
            or type(comparison.derived_artifact_sha256) is not str
        ):
            raise RecordSupersessionConflict("comparison contract is invalid")
        try:
            parsed = contract_from_canonical_bytes(
                DerivedComparison, canonical_contract_bytes(comparison)
            )
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict("comparison contract is invalid") from None
        raw = canonical_contract_bytes(parsed)
        digest = hashlib.sha256(raw).hexdigest()
        linkage = self._linkage_snapshot()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection)
                existing = connection.execute(
                    "SELECT comparison_sha256, comparison_json FROM comparisons WHERE comparison_id=?",
                    (parsed.comparison_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != digest or bytes(existing[1]) != raw:
                        raise RecordSupersessionConflict(
                            "comparison identity conflicts with history"
                        )
                    receipt = self._comparison_receipt(connection, parsed, digest)
                    connection.commit()
                    return receipt
                if (
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM comparisons"
                        ).fetchone()[0]
                    )
                    >= MAX_COMPARISONS
                ):
                    raise RecordSupersessionConflict("comparison ledger is full")
                active = {
                    item.record_id: item
                    for item in self._active_records(connection, linkage)
                }
                if any(
                    record_id not in active for record_id in parsed.member_record_ids
                ):
                    raise RecordSupersessionConflict(
                        "comparison requires exact active records"
                    )
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM comparisons"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO comparisons VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        parsed.comparison_id,
                        digest,
                        linkage.state_version,
                        linkage.state_head_sha256,
                        raw,
                    ),
                )
                version, head = self._advance(connection)
                self._validate_state(connection)
                receipt = self._comparison_receipt(
                    connection, parsed, digest, version=version, head=head
                )
                connection.commit()
                return receipt
            except BaseException as error:
                connection.rollback()
                if isinstance(error, RecordSupersessionError):
                    raise
                if isinstance(error, sqlite3.IntegrityError):
                    raise RecordSupersessionConflict(
                        "comparison conflicts with durable history"
                    ) from None
                raise

    def _active_records(
        self, connection: sqlite3.Connection, linkage: ActiveLinkageSnapshot
    ) -> tuple[SupersedingRecord, ...]:
        records = [
            contract_from_canonical_bytes(SupersedingRecord, bytes(row[0]))
            for row in connection.execute(
                "SELECT record_json FROM records ORDER BY sequence"
            )
        ]
        superseded = {
            item.supersedes_record_id
            for item in records
            if item.supersedes_record_id is not None
        }
        live = self._active_linkages(linkage)
        active: list[SupersedingRecord] = []
        for record in records:
            if record.record_id in superseded:
                continue
            item = live.get((record.provider_namespace, record.linkage_id))
            if item is None:
                continue
            revision = item[0]
            if (
                revision.revision == record.linkage_revision
                and linkage_revision_sha256(revision) == record.linkage_revision_sha256
                and revision.technical.analysis_record_id == record.analysis_record_id
            ):
                active.append(record)
        return tuple(sorted(active, key=lambda item: item.record_id))

    def _refresh_invalidations(
        self, connection: sqlite3.Connection, linkage: ActiveLinkageSnapshot
    ) -> bool:
        changed = False
        active_ids = {
            item.record_id for item in self._active_records(connection, linkage)
        }
        next_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM invalidations"
            ).fetchone()[0]
        )
        for row in connection.execute(
            "SELECT comparison_id, linkage_state_head_sha256, comparison_json FROM comparisons ORDER BY sequence"
        ):
            comparison = contract_from_canonical_bytes(DerivedComparison, bytes(row[2]))
            reasons: set[InvalidationReason] = set()
            if row[1] != linkage.state_head_sha256:
                reasons.add(InvalidationReason.LINKAGE_AUTHORITY_ADVANCED)
            if any(item not in active_ids for item in comparison.member_record_ids):
                reasons.add(InvalidationReason.LINKAGE_CHANGED_OR_TOMBSTONED)
            for reason in sorted(reasons, key=str):
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO invalidations VALUES(?, ?, ?, ?)",
                    (
                        next_sequence,
                        comparison.comparison_id,
                        reason.value,
                        linkage.state_head_sha256,
                    ),
                )
                if cursor.rowcount:
                    next_sequence += 1
                    changed = True
        return changed

    def active_snapshot(self) -> ActiveRecordSnapshot:
        linkage = self._linkage_snapshot()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection)
                if self._refresh_invalidations(connection, linkage):
                    self._advance(connection)
                self._validate_state(connection)
                metadata = self._metadata(connection)
                result = ActiveRecordSnapshot(
                    ledger_id=metadata["ledger_id"],
                    ledger_epoch_sha256=metadata["ledger_epoch_sha256"],
                    storage_identity_sha256=metadata["storage_identity_sha256"],
                    state_version=int(metadata["state_version"]),
                    state_head_sha256=metadata["state_head_sha256"],
                    linkage_store_id=linkage.store_id,
                    linkage_store_epoch_sha256=linkage.store_epoch_sha256,
                    linkage_storage_identity_sha256=linkage.storage_identity_sha256,
                    linkage_state_version=linkage.state_version,
                    linkage_state_head_sha256=linkage.state_head_sha256,
                    records=self._active_records(connection, linkage),
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def comparison_status(self, comparison_id: str) -> ComparisonStatus:
        comparison_id = _COMPARISON_ID.validate_python(comparison_id, strict=True)
        linkage = self._linkage_snapshot()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection)
                changed = self._refresh_invalidations(connection, linkage)
                if changed:
                    self._advance(connection)
                self._validate_state(connection)
                row = connection.execute(
                    "SELECT comparison_json FROM comparisons WHERE comparison_id=?",
                    (comparison_id,),
                ).fetchone()
                if row is None:
                    raise RecordSupersessionConflict("comparison is unknown")
                comparison = contract_from_canonical_bytes(
                    DerivedComparison, bytes(row[0])
                )
                reasons = tuple(
                    InvalidationReason(item[0])
                    for item in connection.execute(
                        "SELECT reason FROM invalidations WHERE comparison_id=? ORDER BY reason",
                        (comparison_id,),
                    )
                )
                status = ComparisonStatus(
                    comparison_id=comparison.comparison_id,
                    state=ComparisonState.STALE if reasons else ComparisonState.CURRENT,
                    reasons=reasons,
                    member_record_ids=comparison.member_record_ids,
                    derived_artifact_sha256=comparison.derived_artifact_sha256,
                    linkage_state_version=linkage.state_version,
                    linkage_state_head_sha256=linkage.state_head_sha256,
                )
                connection.commit()
                return status
            except BaseException:
                connection.rollback()
                raise

    def replay_snapshot(self, snapshot: ActiveRecordSnapshot) -> ActiveRecordSnapshot:
        parsed = contract_from_canonical_bytes(
            ActiveRecordSnapshot, canonical_contract_bytes(snapshot)
        )
        current = self.active_snapshot()
        if parsed != current:
            raise RecordSupersessionConflict("record snapshot is stale or invalid")
        return parsed

    def _record_receipt(
        self,
        connection: sqlite3.Connection,
        record: SupersedingRecord,
        digest: str,
        *,
        version: int | None = None,
        head: str | None = None,
    ) -> RecordCommitReceipt:
        metadata = self._metadata(connection)
        return RecordCommitReceipt(
            ledger_id=metadata["ledger_id"],
            ledger_epoch_sha256=metadata["ledger_epoch_sha256"],
            storage_identity_sha256=metadata["storage_identity_sha256"],
            record_id=record.record_id,
            record_sha256=digest,
        )

    def _comparison_receipt(
        self,
        connection: sqlite3.Connection,
        comparison: DerivedComparison,
        digest: str,
        *,
        version: int | None = None,
        head: str | None = None,
    ) -> ComparisonCommitReceipt:
        metadata = self._metadata(connection)
        return ComparisonCommitReceipt(
            ledger_id=metadata["ledger_id"],
            ledger_epoch_sha256=metadata["ledger_epoch_sha256"],
            storage_identity_sha256=metadata["storage_identity_sha256"],
            comparison_id=comparison.comparison_id,
            comparison_sha256=digest,
        )

    @staticmethod
    def _require_safe_record_shape(record: SupersedingRecord) -> None:
        string_fields = (
            "record_id",
            "provider_namespace",
            "analysis_record_id",
            "result_id",
            "result_sha256",
            "bundle_sha256",
            "linkage_id",
            "linkage_revision_sha256",
            "activation_receipt_sha256",
        )
        if any(type(getattr(record, name)) is not str for name in string_fields):
            raise RecordSupersessionConflict("record contract is invalid")
        optional_fields = ("reanalysis_of_record_id", "supersedes_record_id")
        if any(
            value is not None and type(value) is not str
            for value in (getattr(record, name) for name in optional_fields)
        ):
            raise RecordSupersessionConflict("record contract is invalid")
        if (
            type(record.linkage_revision) is not int
            or type(record.biological_timepoint_contribution) is not bool
            or type(record.lineage_role) is not RecordLineageRole
        ):
            raise RecordSupersessionConflict("record contract is invalid")


__all__ = [
    "ActiveRecordSnapshot",
    "ComparisonCommitReceipt",
    "ComparisonState",
    "ComparisonStatus",
    "DerivedComparison",
    "InvalidationReason",
    "RecordCommitReceipt",
    "RecordLineageRole",
    "RecordSupersessionConflict",
    "RecordSupersessionError",
    "RecordSupersessionStore",
    "RecordSupersessionUnsafe",
    "SupersedingRecord",
    "comparison_sha256",
    "make_comparison_id",
    "make_record_id",
    "record_sha256",
]
