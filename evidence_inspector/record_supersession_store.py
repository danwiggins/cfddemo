"""Durable D04 record supersession and derived-comparison invalidation.

The ledger is provider-local, append-only, and bound to one live
``ProviderLinkageStore``.  It contains opaque identifiers and content digests
only.  A reanalysis is a technical descendant of an existing record and never
creates a biological collection or denominator contribution.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import os
import secrets
import sqlite3
import stat
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, StringConstraints, TypeAdapter, model_validator
from pydantic_core import TzInfo

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
    ApprovalPurpose,
    AuthorizedLinkageRevision,
    IssuerStatus,
    LinkageId,
    LinkageRevision,
    ProviderApprovalPayload,
    ProviderNamespace,
    ProviderRole,
    SignedProviderApproval,
    approval_payload_bytes,
    linkage_revision_sha256,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    ActiveLinkageSnapshot,
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    ProviderLinkageStoreError,
    committed_linkage_receipt_sha256,
)

SCHEMA_VERSION = 1
MAX_RECORDS = 100_000
MAX_COMPARISONS = 100_000
MAX_COMPARISON_MEMBERS = 1_000
MAX_HISTORY_PAGE_RECORDS = 1_000
MAX_HISTORY_COMPARISON_WARNINGS = 1_000

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
_PINNED_FENCED_ACTIVE_SNAPSHOT = ProviderLinkageStore.fenced_active_snapshot
_PINNED_AUTHORIZED_HISTORY_IN_FENCE = ProviderLinkageStore.authorized_history_in_fence
_PINNED_AUTHORITY_TIME_IN_FENCE = ProviderLinkageStore.authority_time_in_fence
_PINNED_ACTIVATION_RECEIPT_HISTORY_IN_FENCE = (
    ProviderLinkageStore.activation_receipt_history_in_fence
)
_PINNED_STORE_CALLABLES = {
    name: getattr(ProviderLinkageStore, name)
    for name in vars(ProviderLinkageStore)
    if callable(getattr(ProviderLinkageStore, name))
}
_SQLITE_OPEN_LOCK = threading.RLock()
_PYDANTIC_UTC = TzInfo(0)
_PLATFORM_PATH_TYPE = type(Path())
_MAX_STORAGE_PATH_CHARS = 4096
_MAX_STORAGE_PATH_PARTS = 256
_SQLITE_WORKER_TIMEOUT_SECONDS = 10.0


def _load_pthread_fchdir():
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).pthread_fchdir_np
        function.argtypes = (ctypes.c_int,)
        function.restype = ctypes.c_int
    except (AttributeError, OSError):
        return None
    return function


_PINNED_PTHREAD_FCHDIR = _load_pthread_fchdir()


class RecordSupersessionError(RuntimeError):
    """Sanitized durable-ledger failure."""


class RecordSupersessionConflict(RecordSupersessionError):
    pass


class RecordSupersessionUnsafe(RecordSupersessionError):
    pass


class RecordLineageRole(StrEnum):
    PRIMARY_ANALYSIS = "primary_analysis"
    REANALYSIS = "reanalysis"


class SupersessionReason(StrEnum):
    METHOD_REANALYSIS = "method_reanalysis"
    PIPELINE_CORRECTION = "pipeline_correction"
    QUALITY_REPROCESSING = "quality_reprocessing"


def _captured_storage_root(root: str | Path) -> Path:
    if type(root) is str:
        if len(root) > _MAX_STORAGE_PATH_CHARS or "\0" in root:
            raise RecordSupersessionUnsafe("record ledger root is unsafe")
        return Path(root).absolute()
    if type(root) is not _PLATFORM_PATH_TYPE:
        raise TypeError("record ledger root must be an exact local path")
    try:
        raw_parts = object.__getattribute__(root, "_parts")
    except AttributeError:
        raise RecordSupersessionUnsafe("record ledger root is unsafe") from None
    if type(raw_parts) is not list or len(raw_parts) > _MAX_STORAGE_PATH_PARTS:
        raise RecordSupersessionUnsafe("record ledger root is unsafe")
    parts = tuple(raw_parts.copy())
    if (
        any(type(part) is not str or "\0" in part for part in parts)
        or sum(len(part) for part in parts) > _MAX_STORAGE_PATH_CHARS
    ):
        raise RecordSupersessionUnsafe("record ledger root is unsafe")
    return Path(*parts).absolute()


def _root_descriptor_is_valid(
    descriptor: int, expected_identity: tuple[int, int]
) -> bool:
    metadata = _safe_fstat(descriptor)
    return bool(
        metadata is not None
        and stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == expected_identity
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
    )


def _open_darwin_anchored_sqlite_connection(
    descriptor: int, expected_identity: tuple[int, int]
) -> tuple[sqlite3.Connection, None]:
    function = _PINNED_PTHREAD_FCHDIR
    if function is None:
        os.close(descriptor)
        raise RecordSupersessionUnsafe(
            "thread-local record ledger anchoring is unavailable"
        )
    result: Queue[sqlite3.Connection | None] = Queue(maxsize=1)
    abandoned = threading.Event()
    publication_lock = threading.Lock()

    def open_on_fresh_thread() -> None:
        connection: sqlite3.Connection | None = None
        try:
            if not _root_descriptor_is_valid(descriptor, expected_identity):
                raise OSError("record ledger root descriptor changed")
            if function(descriptor) != 0:
                raise OSError("thread-local directory anchor failed")
            if not _root_descriptor_is_valid(descriptor, expected_identity):
                raise OSError("record ledger root descriptor changed")
            connection = sqlite3.connect(
                "record-supersession.sqlite3",
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            if not _root_descriptor_is_valid(descriptor, expected_identity):
                raise OSError("record ledger root descriptor changed")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            if not _root_descriptor_is_valid(descriptor, expected_identity):
                raise OSError("record ledger root descriptor changed")
        except BaseException:  # noqa: BLE001 - worker must always publish or clean up
            if connection is not None:
                connection.close()
            connection = None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                if connection is not None:
                    connection.close()
                connection = None
        with publication_lock:
            if abandoned.is_set():
                if connection is not None:
                    connection.close()
                return
            try:
                result.put_nowait(connection)
            except Full:
                if connection is not None:
                    connection.close()

    worker = threading.Thread(
        target=open_on_fresh_thread,
        name="record-ledger-sqlite-open",
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:  # noqa: BLE001 - duplicated descriptor must not leak
        os.close(descriptor)
        raise RecordSupersessionUnsafe(
            "record ledger connection worker could not start"
        ) from None
    try:
        connection = result.get(timeout=_SQLITE_WORKER_TIMEOUT_SECONDS)
    except Empty:
        with publication_lock:
            abandoned.set()
            try:
                late_connection = result.get_nowait()
            except Empty:
                late_connection = None
            if late_connection is not None:
                late_connection.close()
        raise RecordSupersessionUnsafe(
            "record ledger connection initialization timed out"
        ) from None
    worker.join(timeout=1.0)
    if worker.is_alive() or connection is None:
        if connection is not None:
            connection.close()
        raise RecordSupersessionUnsafe("record ledger connection initialization failed")
    return connection, None


def _open_linux_anchored_sqlite_connection(
    descriptor: int, expected_identity: tuple[int, int]
) -> tuple[sqlite3.Connection, int]:
    connection: sqlite3.Connection | None = None
    try:
        if not _root_descriptor_is_valid(descriptor, expected_identity):
            raise OSError("record ledger root descriptor changed")
        proc_root = f"/proc/self/fd/{descriptor}"
        proc_metadata = os.stat(proc_root, follow_symlinks=True)
        if (
            not stat.S_ISDIR(proc_metadata.st_mode)
            or (proc_metadata.st_dev, proc_metadata.st_ino) != expected_identity
        ):
            raise OSError("proc descriptor anchor changed")
        connection = sqlite3.connect(
            f"{proc_root}/record-supersession.sqlite3",
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        if not _root_descriptor_is_valid(descriptor, expected_identity):
            raise OSError("record ledger root descriptor changed")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        if not _root_descriptor_is_valid(descriptor, expected_identity):
            raise OSError("record ledger root descriptor changed")
        return connection, descriptor
    except BaseException:  # noqa: BLE001 - owned descriptor must always close
        if connection is not None:
            try:
                connection.close()
            except BaseException:  # noqa: BLE001,S110 - preserve sanitized failure
                pass
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise RecordSupersessionUnsafe(
            "Linux record ledger connection initialization failed"
        ) from None


def _open_anchored_sqlite_connection(
    descriptor: int, expected_identity: tuple[int, int]
) -> tuple[sqlite3.Connection, int | None]:
    if sys.platform == "darwin":
        return _open_darwin_anchored_sqlite_connection(descriptor, expected_identity)
    if sys.platform == "linux":
        return _open_linux_anchored_sqlite_connection(descriptor, expected_identity)
    os.close(descriptor)
    raise RecordSupersessionUnsafe(
        "record ledger connection anchoring is unsupported on this platform"
    )


class SupersessionStatement(RegistryContract):
    schema_version: Literal["traceback.supersession-statement.v1"] = (
        "traceback.supersession-statement.v1"
    )
    provider_namespace: ProviderNamespace
    analysis_record_id: AnalysisRecordId
    result_id: ResultId
    result_sha256: Sha256
    bundle_sha256: Sha256
    linkage_id: LinkageId
    linkage_revision: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
    linkage_revision_sha256: Sha256
    activation_receipt_sha256: Sha256
    source_record_id: RecordId
    lineage_role: Literal[RecordLineageRole.REANALYSIS]
    reason: SupersessionReason


class ComparisonState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class InvalidationReason(StrEnum):
    RECORD_SUPERSEDED = "record_superseded"
    LINKAGE_AUTHORITY_ADVANCED = "linkage_authority_advanced"
    LINKAGE_CHANGED_OR_TOMBSTONED = "linkage_changed_or_tombstoned"


class RecordHistoryState(StrEnum):
    """One immutable record's current role in the global ledger."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    AUTHORITY_INVALID = "authority_invalid"


class SupersedingRecord(RegistryContract):
    """One immutable result identity and its exact biological authority."""

    schema_version: Literal["traceback.superseding-record.v2"] = (
        "traceback.superseding-record.v2"
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
    supersession_reason: SupersessionReason | None = None
    supersession_authorization: SignedProviderApproval | None = None
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
        if derived != (self.supersession_reason is not None):
            raise ValueError("reanalysis must include a controlled reason")
        if derived != (self.supersession_authorization is not None):
            raise ValueError("reanalysis must include signed authority")
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

    schema_version: Literal["traceback.derived-comparison.v2"] = (
        "traceback.derived-comparison.v2"
    )
    comparison_id: ComparisonId
    member_record_ids: tuple[RecordId, ...] = Field(
        min_length=2, max_length=MAX_COMPARISON_MEMBERS
    )
    derived_artifact_sha256: Sha256
    provider_namespace: ProviderNamespace
    linkage_store_id: LinkageStoreId
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=0, le=MAX_REVISIONS)
    linkage_state_head_sha256: Sha256
    authority: SignedProviderApproval

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


class ComparisonAuthorityStatement(RegistryContract):
    schema_version: Literal["traceback.comparison-authority-statement.v1"] = (
        "traceback.comparison-authority-statement.v1"
    )
    comparison_id: ComparisonId
    member_record_ids: tuple[RecordId, ...]
    derived_artifact_sha256: Sha256
    provider_namespace: ProviderNamespace
    linkage_store_id: LinkageStoreId
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=0, le=MAX_REVISIONS)
    linkage_state_head_sha256: Sha256


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


class RecordHistoryEntry(RegistryContract):
    """One immutable record with its original authority proof and warnings."""

    schema_version: Literal["traceback.record-history-entry.v1"] = (
        "traceback.record-history-entry.v1"
    )
    record: SupersedingRecord
    record_sha256: Sha256
    activation_receipt: CommittedLinkageReceipt
    state: RecordHistoryState
    successor_record_id: RecordId | None = None
    affected_comparisons: tuple[ComparisonStatus, ...] = Field(
        max_length=MAX_HISTORY_COMPARISON_WARNINGS
    )

    @model_validator(mode="after")
    def exact_bindings(self) -> RecordHistoryEntry:
        if self.record_sha256 != record_sha256(self.record):
            raise ValueError("record history digest is invalid")
        receipt = self.activation_receipt
        if (
            receipt.provider_namespace != self.record.provider_namespace
            or receipt.linkage_id != self.record.linkage_id
            or receipt.revision != self.record.linkage_revision
            or receipt.linkage_revision_sha256 != self.record.linkage_revision_sha256
            or committed_linkage_receipt_sha256(receipt)
            != self.record.activation_receipt_sha256
        ):
            raise ValueError("record history activation receipt is invalid")
        comparison_ids = tuple(item.comparison_id for item in self.affected_comparisons)
        if comparison_ids != tuple(sorted(set(comparison_ids))):
            raise ValueError("record history comparison warnings are not canonical")
        if any(
            item.state is not ComparisonState.STALE
            or self.record.record_id not in item.member_record_ids
            for item in self.affected_comparisons
        ):
            raise ValueError("record history comparison warning is invalid")
        return self


class RecordHistorySnapshot(RegistryContract):
    """One canonical bounded page of the protected global record history."""

    schema_version: Literal["traceback.record-history-snapshot.v1"] = (
        "traceback.record-history-snapshot.v1"
    )
    ledger_id: LedgerId
    ledger_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_RECORDS + MAX_COMPARISONS * 4)
    state_head_sha256: Sha256
    linkage_store_id: LinkageStoreId
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=0, le=MAX_REVISIONS)
    linkage_state_head_sha256: Sha256
    after_record_id: RecordId | None = None
    limit: int = Field(ge=1, le=MAX_HISTORY_PAGE_RECORDS, strict=True)
    records: tuple[RecordHistoryEntry, ...] = Field(max_length=MAX_HISTORY_PAGE_RECORDS)
    next_after_record_id: RecordId | None = None

    @model_validator(mode="after")
    def canonical_page(self) -> RecordHistorySnapshot:
        record_ids = tuple(item.record.record_id for item in self.records)
        if record_ids != tuple(sorted(set(record_ids))):
            raise ValueError("record history page is not canonical")
        if len(record_ids) > self.limit:
            raise ValueError("record history page exceeds requested limit")
        if self.after_record_id is not None and any(
            record_id <= self.after_record_id for record_id in record_ids
        ):
            raise ValueError("record history cursor is invalid")
        if self.next_after_record_id is not None and (
            len(record_ids) != self.limit or self.next_after_record_id != record_ids[-1]
        ):
            raise ValueError("record history next cursor is invalid")
        if (
            sum(len(item.affected_comparisons) for item in self.records)
            > MAX_HISTORY_COMPARISON_WARNINGS
        ):
            raise ValueError("record history comparison warning bound exceeded")
        for item in self.records:
            if (
                item.activation_receipt.store_id != self.linkage_store_id
                or item.activation_receipt.store_epoch_sha256
                != self.linkage_store_epoch_sha256
                or item.activation_receipt.storage_identity_sha256
                != self.linkage_storage_identity_sha256
            ):
                raise ValueError("record history linkage store binding is invalid")
            if (
                item.successor_record_id is not None
                and item.state is not RecordHistoryState.SUPERSEDED
            ):
                raise ValueError("record history supersession state is invalid")
            if (
                item.successor_record_id is None
                and item.state is RecordHistoryState.SUPERSEDED
            ):
                raise ValueError("record history leaf state is invalid")
        return self


@dataclass(frozen=True)
class _LinkageAuthorityView:
    snapshot: ActiveLinkageSnapshot
    history: tuple[AuthorizedLinkageRevision, ...]
    evaluated_at: datetime
    activation_receipts: tuple[CommittedLinkageReceipt, ...]


def _digest(domain: bytes, content: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + content).hexdigest()


def supersession_statement_sha256(record: SupersedingRecord) -> str:
    _require_exact_record_shape(record, error_type=ValueError)
    if (
        record.lineage_role != RecordLineageRole.REANALYSIS
        or record.supersedes_record_id is None
        or record.supersession_reason is None
    ):
        raise ValueError("supersession statement is invalid")
    statement = SupersessionStatement(
        provider_namespace=record.provider_namespace,
        analysis_record_id=record.analysis_record_id,
        result_id=record.result_id,
        result_sha256=record.result_sha256,
        bundle_sha256=record.bundle_sha256,
        linkage_id=record.linkage_id,
        linkage_revision=record.linkage_revision,
        linkage_revision_sha256=record.linkage_revision_sha256,
        activation_receipt_sha256=record.activation_receipt_sha256,
        source_record_id=record.supersedes_record_id,
        lineage_role=RecordLineageRole.REANALYSIS,
        reason=record.supersession_reason,
    )
    return hashlib.sha256(canonical_contract_bytes(statement)).hexdigest()


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
    try:
        provider_namespace = _PROVIDER_NAMESPACE.validate_python(
            provider_namespace, strict=True
        )
        analysis_record_id = _ANALYSIS_RECORD_ID.validate_python(
            analysis_record_id, strict=True
        )
        result_id = _RESULT_ID.validate_python(result_id, strict=True)
        result_sha256 = _SHA256.validate_python(result_sha256, strict=True)
        bundle_sha256 = _SHA256.validate_python(bundle_sha256, strict=True)
    except (TypeError, ValueError):
        raise ValueError("record identity inputs are invalid") from None
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
    try:
        member_record_ids = tuple(
            _RECORD_ID.validate_python(item, strict=True) for item in member_record_ids
        )
        derived_artifact_sha256 = _SHA256.validate_python(
            derived_artifact_sha256, strict=True
        )
    except (TypeError, ValueError):
        raise ValueError("comparison identity inputs are invalid") from None
    content = b"\0".join(
        [item.encode("ascii") for item in member_record_ids]
        + [derived_artifact_sha256.encode("ascii")]
    )
    return _COMPARISON_ID.validate_python(
        f"comparison_{_digest(b'traceback-comparison-id-v1', content)[:40]}",
        strict=True,
    )


def _model_values(value: object, expected: tuple[str, ...]) -> dict[str, object] | None:
    try:
        values = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
    except (AttributeError, TypeError):
        return None
    if (
        type(values) is not dict
        or any(type(key) is not str for key in values)
        or set(values) != set(expected)
    ):
        return None
    if extra is not None and (type(extra) is not dict or len(extra) != 0):
        return None
    if private is not None and (type(private) is not dict or len(private) != 0):
        return None
    return values


def _fixed_hex(value: object, prefix: str, digits: int) -> bool:
    if type(value) is not str or len(value) != len(prefix) + digits:
        return False
    return value.startswith(prefix) and all(
        character in "0123456789abcdef" for character in value[len(prefix) :]
    )


def _approval_values(approval: object) -> dict[str, object] | None:
    signed = _model_values(approval, ("payload", "signature_base64"))
    if signed is None or type(signed["payload"]) is not ProviderApprovalPayload:
        return None
    payload = _model_values(
        signed["payload"],
        (
            "schema_version",
            "approval_id",
            "provider_namespace",
            "issuer_id",
            "key_id",
            "principal_id",
            "role",
            "purpose",
            "proposed_revision_sha256",
            "trust_snapshot_id",
            "trust_snapshot_revision",
            "trust_snapshot_sha256",
            "nonce",
            "issued_at",
            "expires_at",
        ),
    )
    if payload is None:
        return None
    if (
        signed["signature_base64"] is None
        or type(signed["signature_base64"]) is not str
        or len(signed["signature_base64"]) != 88
        or type(payload["schema_version"]) is not str
        or payload["schema_version"] != "traceback.provider-linkage-approval.v1"
        or not _fixed_hex(payload["approval_id"], "approval_", 32)
        or not _fixed_hex(payload["provider_namespace"], "provider_", 32)
        or not _fixed_hex(payload["issuer_id"], "issuer_", 32)
        or not _fixed_hex(payload["key_id"], "key_", 32)
        or not _fixed_hex(payload["principal_id"], "principal_", 32)
        or type(payload["role"]) is not ProviderRole
        or type(payload["purpose"]) is not ApprovalPurpose
        or not _fixed_hex(payload["proposed_revision_sha256"], "", 64)
        or not _fixed_hex(payload["trust_snapshot_id"], "trust_", 32)
        or type(payload["trust_snapshot_revision"]) is not int
        or not 1 <= payload["trust_snapshot_revision"] <= MAX_REVISIONS
        or not _fixed_hex(payload["trust_snapshot_sha256"], "", 64)
        or not _fixed_hex(payload["nonce"], "nonce_", 32)
        or type(payload["issued_at"]) is not datetime
        or type(payload["expires_at"]) is not datetime
    ):
        return None
    issued_at = payload["issued_at"]
    expires_at = payload["expires_at"]
    issued_tz = object.__getattribute__(issued_at, "tzinfo")
    expires_tz = object.__getattribute__(expires_at, "tzinfo")
    if (
        not (
            issued_tz is UTC
            or (type(issued_tz) is TzInfo and issued_tz == _PYDANTIC_UTC)
        )
        or not (
            expires_tz is UTC
            or (type(expires_tz) is TzInfo and expires_tz == _PYDANTIC_UTC)
        )
        or object.__getattribute__(issued_at, "microsecond")
        or object.__getattribute__(expires_at, "microsecond")
        or expires_at <= issued_at
    ):
        return None
    return payload


def _require_exact_record_shape(record: object, *, error_type: type[Exception]) -> None:
    if type(record) is not SupersedingRecord:
        raise error_type("record contract is invalid")
    values = _model_values(
        record,
        (
            "schema_version",
            "record_id",
            "provider_namespace",
            "analysis_record_id",
            "result_id",
            "result_sha256",
            "bundle_sha256",
            "linkage_id",
            "linkage_revision",
            "linkage_revision_sha256",
            "activation_receipt_sha256",
            "lineage_role",
            "reanalysis_of_record_id",
            "supersedes_record_id",
            "supersession_reason",
            "supersession_authorization",
            "biological_timepoint_contribution",
        ),
    )
    if (
        values is None
        or type(values["schema_version"]) is not str
        or values["schema_version"] != "traceback.superseding-record.v2"
        or not _fixed_hex(values["record_id"], "record_", 40)
        or not _fixed_hex(values["provider_namespace"], "provider_", 32)
        or not _fixed_hex(values["analysis_record_id"], "analysis_", 32)
        or not _fixed_hex(values["result_id"], "result_", 40)
        or not _fixed_hex(values["result_sha256"], "", 64)
        or not _fixed_hex(values["bundle_sha256"], "", 64)
        or not _fixed_hex(values["linkage_id"], "linkage_", 32)
        or type(values["linkage_revision"]) is not int
        or not 1 <= values["linkage_revision"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_revision_sha256"], "", 64)
        or not _fixed_hex(values["activation_receipt_sha256"], "", 64)
        or type(values["biological_timepoint_contribution"]) is not bool
        or values["biological_timepoint_contribution"] is not False
        or type(values["lineage_role"]) is not RecordLineageRole
        or (
            values["reanalysis_of_record_id"] is not None
            and not _fixed_hex(values["reanalysis_of_record_id"], "record_", 40)
        )
        or (
            values["supersedes_record_id"] is not None
            and not _fixed_hex(values["supersedes_record_id"], "record_", 40)
        )
        or (
            values["supersession_reason"] is not None
            and type(values["supersession_reason"]) is not SupersessionReason
        )
        or (
            values["supersession_authorization"] is not None
            and type(values["supersession_authorization"]) is not SignedProviderApproval
        )
    ):
        raise error_type("record contract is invalid")
    authorization = values["supersession_authorization"]
    if authorization is not None and _approval_values(authorization) is None:
        raise error_type("record contract is invalid")


def _require_exact_comparison_shape(
    comparison: object, *, error_type: type[Exception]
) -> None:
    if type(comparison) is not DerivedComparison:
        raise error_type("comparison contract is invalid")
    values = _model_values(
        comparison,
        (
            "schema_version",
            "comparison_id",
            "member_record_ids",
            "derived_artifact_sha256",
            "provider_namespace",
            "linkage_store_id",
            "linkage_store_epoch_sha256",
            "linkage_storage_identity_sha256",
            "linkage_state_version",
            "linkage_state_head_sha256",
            "authority",
        ),
    )
    if values is None:
        raise error_type("comparison contract is invalid")
    members = values["member_record_ids"]
    if (
        type(members) is not tuple
        or not 2 <= len(members) <= MAX_COMPARISON_MEMBERS
        or any(not _fixed_hex(item, "record_", 40) for item in members)
        or type(values["schema_version"]) is not str
        or values["schema_version"] != "traceback.derived-comparison.v2"
        or not _fixed_hex(values["comparison_id"], "comparison_", 40)
        or not _fixed_hex(values["derived_artifact_sha256"], "", 64)
        or not _fixed_hex(values["provider_namespace"], "provider_", 32)
        or not _fixed_hex(values["linkage_store_id"], "store_", 32)
        or not _fixed_hex(values["linkage_store_epoch_sha256"], "", 64)
        or not _fixed_hex(values["linkage_storage_identity_sha256"], "", 64)
        or type(values["linkage_state_version"]) is not int
        or not 0 <= values["linkage_state_version"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_state_head_sha256"], "", 64)
        or type(values["authority"]) is not SignedProviderApproval
        or _approval_values(values["authority"]) is None
    ):
        raise error_type("comparison contract is invalid")


def _require_exact_snapshot_shape(
    snapshot: object, *, error_type: type[Exception]
) -> None:
    if type(snapshot) is not ActiveRecordSnapshot:
        raise error_type("record snapshot is invalid")
    values = _model_values(
        snapshot,
        (
            "schema_version",
            "ledger_id",
            "ledger_epoch_sha256",
            "storage_identity_sha256",
            "state_version",
            "state_head_sha256",
            "linkage_store_id",
            "linkage_store_epoch_sha256",
            "linkage_storage_identity_sha256",
            "linkage_state_version",
            "linkage_state_head_sha256",
            "records",
        ),
    )
    if values is None:
        raise error_type("record snapshot is invalid")
    records = values["records"]
    if (
        type(values["schema_version"]) is not str
        or values["schema_version"] != "traceback.active-record-snapshot.v1"
        or not _fixed_hex(values["ledger_id"], "ledger_", 32)
        or not _fixed_hex(values["ledger_epoch_sha256"], "", 64)
        or not _fixed_hex(values["storage_identity_sha256"], "", 64)
        or type(values["state_version"]) is not int
        or not 0 <= values["state_version"] <= MAX_RECORDS + MAX_COMPARISONS * 4
        or not _fixed_hex(values["state_head_sha256"], "", 64)
        or not _fixed_hex(values["linkage_store_id"], "store_", 32)
        or not _fixed_hex(values["linkage_store_epoch_sha256"], "", 64)
        or not _fixed_hex(values["linkage_storage_identity_sha256"], "", 64)
        or type(values["linkage_state_version"]) is not int
        or not 0 <= values["linkage_state_version"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_state_head_sha256"], "", 64)
        or type(records) is not tuple
        or len(records) > MAX_RECORDS
    ):
        raise error_type("record snapshot is invalid")
    for record in records:
        _require_exact_record_shape(record, error_type=error_type)


def _require_exact_comparison_status_shape(
    status: object, *, error_type: type[Exception]
) -> None:
    if type(status) is not ComparisonStatus:
        raise error_type("comparison status is invalid")
    values = _model_values(
        status,
        (
            "schema_version",
            "comparison_id",
            "state",
            "reasons",
            "member_record_ids",
            "derived_artifact_sha256",
            "linkage_state_version",
            "linkage_state_head_sha256",
        ),
    )
    if values is None:
        raise error_type("comparison status is invalid")
    reasons = values["reasons"]
    members = values["member_record_ids"]
    if (
        values["schema_version"] != "traceback.derived-comparison-status.v1"
        or not _fixed_hex(values["comparison_id"], "comparison_", 40)
        or type(values["state"]) is not ComparisonState
        or type(reasons) is not tuple
        or any(type(item) is not InvalidationReason for item in reasons)
        or type(members) is not tuple
        or not 2 <= len(members) <= MAX_COMPARISON_MEMBERS
        or any(not _fixed_hex(item, "record_", 40) for item in members)
        or not _fixed_hex(values["derived_artifact_sha256"], "", 64)
        or type(values["linkage_state_version"]) is not int
        or not 0 <= values["linkage_state_version"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_state_head_sha256"], "", 64)
    ):
        raise error_type("comparison status is invalid")


def _require_exact_activation_receipt_shape(
    receipt: object, *, error_type: type[Exception]
) -> None:
    if type(receipt) is not CommittedLinkageReceipt:
        raise error_type("record history activation receipt is invalid")
    values = _model_values(
        receipt,
        (
            "schema_version",
            "provider_namespace",
            "store_id",
            "store_epoch_sha256",
            "storage_identity_sha256",
            "trust_pins_sha256",
            "linkage_id",
            "revision",
            "linkage_revision_sha256",
            "authorized_record_sha256",
            "state_version",
            "state_head_sha256",
        ),
    )
    if (
        values is None
        or values["schema_version"] != "traceback.committed-linkage-receipt.v1"
        or not _fixed_hex(values["provider_namespace"], "provider_", 32)
        or not _fixed_hex(values["store_id"], "store_", 32)
        or not _fixed_hex(values["store_epoch_sha256"], "", 64)
        or not _fixed_hex(values["storage_identity_sha256"], "", 64)
        or not _fixed_hex(values["trust_pins_sha256"], "", 64)
        or not _fixed_hex(values["linkage_id"], "linkage_", 32)
        or type(values["revision"]) is not int
        or not 1 <= values["revision"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_revision_sha256"], "", 64)
        or not _fixed_hex(values["authorized_record_sha256"], "", 64)
        or type(values["state_version"]) is not int
        or not 1 <= values["state_version"] <= MAX_REVISIONS
        or not _fixed_hex(values["state_head_sha256"], "", 64)
    ):
        raise error_type("record history activation receipt is invalid")


def _require_exact_history_snapshot_shape(
    snapshot: object, *, error_type: type[Exception]
) -> None:
    if type(snapshot) is not RecordHistorySnapshot:
        raise error_type("record history snapshot is invalid")
    values = _model_values(
        snapshot,
        (
            "schema_version",
            "ledger_id",
            "ledger_epoch_sha256",
            "storage_identity_sha256",
            "state_version",
            "state_head_sha256",
            "linkage_store_id",
            "linkage_store_epoch_sha256",
            "linkage_storage_identity_sha256",
            "linkage_state_version",
            "linkage_state_head_sha256",
            "after_record_id",
            "limit",
            "records",
            "next_after_record_id",
        ),
    )
    if values is None:
        raise error_type("record history snapshot is invalid")
    records = values["records"]
    if (
        values["schema_version"] != "traceback.record-history-snapshot.v1"
        or not _fixed_hex(values["ledger_id"], "ledger_", 32)
        or not _fixed_hex(values["ledger_epoch_sha256"], "", 64)
        or not _fixed_hex(values["storage_identity_sha256"], "", 64)
        or type(values["state_version"]) is not int
        or not 0 <= values["state_version"] <= MAX_RECORDS + MAX_COMPARISONS * 4
        or not _fixed_hex(values["state_head_sha256"], "", 64)
        or not _fixed_hex(values["linkage_store_id"], "store_", 32)
        or not _fixed_hex(values["linkage_store_epoch_sha256"], "", 64)
        or not _fixed_hex(values["linkage_storage_identity_sha256"], "", 64)
        or type(values["linkage_state_version"]) is not int
        or not 0 <= values["linkage_state_version"] <= MAX_REVISIONS
        or not _fixed_hex(values["linkage_state_head_sha256"], "", 64)
        or (
            values["after_record_id"] is not None
            and not _fixed_hex(values["after_record_id"], "record_", 40)
        )
        or type(values["limit"]) is not int
        or not 1 <= values["limit"] <= MAX_HISTORY_PAGE_RECORDS
        or type(records) is not tuple
        or len(records) > MAX_HISTORY_PAGE_RECORDS
        or len(records) > values["limit"]
        or (
            values["next_after_record_id"] is not None
            and not _fixed_hex(values["next_after_record_id"], "record_", 40)
        )
    ):
        raise error_type("record history snapshot is invalid")
    warning_count = 0
    for entry in records:
        if type(entry) is not RecordHistoryEntry:
            raise error_type("record history entry is invalid")
        entry_values = _model_values(
            entry,
            (
                "schema_version",
                "record",
                "record_sha256",
                "activation_receipt",
                "state",
                "successor_record_id",
                "affected_comparisons",
            ),
        )
        if entry_values is None:
            raise error_type("record history entry is invalid")
        warnings = entry_values["affected_comparisons"]
        if (
            entry_values["schema_version"] != "traceback.record-history-entry.v1"
            or not _fixed_hex(entry_values["record_sha256"], "", 64)
            or type(entry_values["state"]) is not RecordHistoryState
            or (
                entry_values["successor_record_id"] is not None
                and not _fixed_hex(entry_values["successor_record_id"], "record_", 40)
            )
            or type(warnings) is not tuple
            or len(warnings) > MAX_HISTORY_COMPARISON_WARNINGS
        ):
            raise error_type("record history entry is invalid")
        warning_count += len(warnings)
        if warning_count > MAX_HISTORY_COMPARISON_WARNINGS:
            raise error_type("record history comparison warning bound exceeded")
        _require_exact_record_shape(entry_values["record"], error_type=error_type)
        _require_exact_activation_receipt_shape(
            entry_values["activation_receipt"], error_type=error_type
        )
        for status in warnings:
            _require_exact_comparison_status_shape(status, error_type=error_type)


def record_sha256(record: SupersedingRecord) -> str:
    _require_exact_record_shape(record, error_type=ValueError)
    return hashlib.sha256(canonical_contract_bytes(record)).hexdigest()


def _record_from_canonical_bytes(content: bytes) -> SupersedingRecord:
    try:
        record = SupersedingRecord.model_validate_json(content)
    except (TypeError, ValueError):
        raise RegistryIdentityError("record JSON is invalid") from None
    if canonical_contract_bytes(record) != content:
        raise RegistryIdentityError("record JSON is not canonical")
    return record


def comparison_sha256(comparison: DerivedComparison) -> str:
    _require_exact_comparison_shape(comparison, error_type=ValueError)
    return hashlib.sha256(canonical_contract_bytes(comparison)).hexdigest()


def comparison_authority_statement_sha256(comparison: DerivedComparison) -> str:
    _require_exact_comparison_shape(comparison, error_type=ValueError)
    statement = ComparisonAuthorityStatement(
        comparison_id=comparison.comparison_id,
        member_record_ids=comparison.member_record_ids,
        derived_artifact_sha256=comparison.derived_artifact_sha256,
        provider_namespace=comparison.provider_namespace,
        linkage_store_id=comparison.linkage_store_id,
        linkage_store_epoch_sha256=comparison.linkage_store_epoch_sha256,
        linkage_storage_identity_sha256=comparison.linkage_storage_identity_sha256,
        linkage_state_version=comparison.linkage_state_version,
        linkage_state_head_sha256=comparison.linkage_state_head_sha256,
    )
    return hashlib.sha256(canonical_contract_bytes(statement)).hexdigest()


def _comparison_from_canonical_bytes(content: bytes) -> DerivedComparison:
    try:
        comparison = DerivedComparison.model_validate_json(content)
    except (TypeError, ValueError):
        raise RegistryIdentityError("comparison JSON is invalid") from None
    if canonical_contract_bytes(comparison) != content:
        raise RegistryIdentityError("comparison JSON is not canonical")
    return comparison


def _history_snapshot_from_canonical_bytes(content: bytes) -> RecordHistorySnapshot:
    try:
        snapshot = RecordHistorySnapshot.model_validate_json(content)
    except (TypeError, ValueError):
        raise RegistryIdentityError("record history snapshot JSON is invalid") from None
    _require_exact_history_snapshot_shape(snapshot, error_type=RegistryIdentityError)
    if canonical_contract_bytes(snapshot) != content:
        raise RegistryIdentityError("record history snapshot JSON is not canonical")
    return snapshot


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


def _safe_fstat(descriptor: int) -> os.stat_result | None:
    try:
        return os.fstat(descriptor)
    except OSError:
        return None


def _open_descriptor_identities() -> dict[int, tuple[int, int, int]]:
    names: list[str] | None = None
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            names = os.listdir(directory)
            break
        except OSError:
            continue
    if names is None:
        return {}
    result: dict[int, tuple[int, int, int]] = {}
    for name in names:
        if not name.isdigit():
            continue
        descriptor = int(name)
        metadata = _safe_fstat(descriptor)
        if metadata is not None:
            result[descriptor] = (
                stat.S_IFMT(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
            )
    return result


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
        self.root = _captured_storage_root(root)
        self.linkage_store = linkage_store
        self._closed = False
        try:
            self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            pass
        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except OSError:
            raise RecordSupersessionUnsafe("record ledger root is unsafe") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            raise RecordSupersessionUnsafe("record ledger root must be private")
        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = os.open(self.root, root_flags)
        except OSError:
            raise RecordSupersessionUnsafe("record ledger root is unsafe") from None
        bound_root = os.fstat(self._root_fd)
        if (
            not stat.S_ISDIR(bound_root.st_mode)
            or (bound_root.st_dev, bound_root.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or stat.S_IMODE(bound_root.st_mode) != 0o700
            or bound_root.st_uid != os.geteuid()
        ):
            os.close(self._root_fd)
            raise RecordSupersessionUnsafe("record ledger root changed")
        self._root_identity = (bound_root.st_dev, bound_root.st_ino)
        self.database = self.root / "record-supersession.sqlite3"
        self._database_identity: tuple[int, int] | None = None
        self._database_fd: int | None = None
        try:
            descriptor = os.open(
                "record-supersession.sqlite3",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        database_metadata = os.stat(
            "record-supersession.sqlite3",
            dir_fd=self._root_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(database_metadata.st_mode)
            or stat.S_IMODE(database_metadata.st_mode) != 0o600
            or database_metadata.st_uid != os.geteuid()
        ):
            self.close()
            raise RecordSupersessionUnsafe("record ledger database must be private")
        self._database_identity = (
            database_metadata.st_dev,
            database_metadata.st_ino,
        )
        try:
            self._bind_database_descriptor()
            self._initialize()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with _SQLITE_OPEN_LOCK:
            if getattr(self, "_closed", True):
                return
            self._closed = True
            for attribute in ("_database_fd", "_root_fd"):
                descriptor = getattr(self, attribute, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except (OSError, TypeError):
                        pass
                    setattr(self, attribute, None)

    def __del__(self) -> None:
        self.close()

    def _bind_database_descriptor(self) -> None:
        if self._database_fd is not None:
            return
        try:
            descriptor = os.open(
                "record-supersession.sqlite3",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            metadata = os.fstat(descriptor)
        except OSError:
            raise RecordSupersessionUnsafe("record ledger database is unsafe") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._database_identity
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            os.close(descriptor)
            raise RecordSupersessionUnsafe("record ledger database changed")
        self._database_fd = descriptor

    def _validate_storage(
        self,
        sidecar_bindings: dict[str, tuple[int, tuple[int, int]]] | None = None,
    ) -> None:
        metadata = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._root_identity
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            raise RecordSupersessionUnsafe("record ledger storage changed")
        if self.database.is_symlink():
            raise RecordSupersessionUnsafe("record ledger database is unsafe")
        if self.database.exists():
            db = self.database.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(db.st_mode)
                or stat.S_IMODE(db.st_mode) != 0o600
                or db.st_uid != os.geteuid()
                or (
                    self._database_identity is not None
                    and (db.st_dev, db.st_ino) != self._database_identity
                )
            ):
                raise RecordSupersessionUnsafe("record ledger database must be private")
            if self._database_fd is None:
                raise RecordSupersessionUnsafe("record ledger database is unbound")
            bound = os.fstat(self._database_fd)
            if (
                not stat.S_ISREG(bound.st_mode)
                or (bound.st_dev, bound.st_ino) != self._database_identity
                or stat.S_IMODE(bound.st_mode) != 0o600
                or bound.st_uid != os.geteuid()
            ):
                raise RecordSupersessionUnsafe("record ledger database changed")
        for suffix in ("-wal", "-shm"):
            name = "record-supersession.sqlite3" + suffix
            try:
                metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                if sidecar_bindings is not None and suffix in sidecar_bindings:
                    raise RecordSupersessionUnsafe(
                        "record ledger sidecar changed"
                    ) from None
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise RecordSupersessionUnsafe("record ledger sidecar is unsafe")
            if sidecar_bindings is not None:
                binding = sidecar_bindings.get(suffix)
                if binding is None:
                    raise RecordSupersessionUnsafe("record ledger sidecar changed")
                descriptor, identity = binding
                bound = _safe_fstat(descriptor)
                if (
                    bound is None
                    or not stat.S_ISREG(bound.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != identity
                    or (bound.st_dev, bound.st_ino) != identity
                    or stat.S_IMODE(bound.st_mode) != 0o600
                    or bound.st_uid != os.geteuid()
                ):
                    raise RecordSupersessionUnsafe("record ledger sidecar changed")

    def _bind_sidecars(
        self, before: dict[int, tuple[int, int, int]]
    ) -> dict[str, tuple[int, tuple[int, int]]]:
        opened = _open_descriptor_identities()
        bindings: dict[str, tuple[int, tuple[int, int]]] = {}
        try:
            for suffix in ("-wal", "-shm"):
                name = "record-supersession.sqlite3" + suffix
                metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino)
                sqlite_matches = [
                    descriptor
                    for descriptor, descriptor_identity in opened.items()
                    if before.get(descriptor) != descriptor_identity
                    and descriptor_identity == (stat.S_IFREG, *identity)
                    and descriptor != self._database_fd
                ]
                if not sqlite_matches:
                    raise RecordSupersessionUnsafe(
                        "record ledger sidecar connection is unproven"
                    )
                for sqlite_descriptor in sqlite_matches:
                    os.fchmod(sqlite_descriptor, 0o600)
                    sqlite_metadata = os.fstat(sqlite_descriptor)
                    if (
                        (sqlite_metadata.st_dev, sqlite_metadata.st_ino) != identity
                        or stat.S_IMODE(sqlite_metadata.st_mode) != 0o600
                        or sqlite_metadata.st_uid != os.geteuid()
                    ):
                        raise RecordSupersessionUnsafe(
                            "record ledger sidecar connection changed"
                        )
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
                bindings[suffix] = (descriptor, identity)
                bound = os.fstat(descriptor)
                observed = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(bound.st_mode)
                    or (bound.st_dev, bound.st_ino) != identity
                    or (observed.st_dev, observed.st_ino) != identity
                    or stat.S_IMODE(bound.st_mode) != 0o600
                    or bound.st_uid != os.geteuid()
                ):
                    raise RecordSupersessionUnsafe("record ledger sidecar changed")
        except (OSError, RecordSupersessionUnsafe):
            for descriptor, _ in bindings.values():
                os.close(descriptor)
            raise RecordSupersessionUnsafe("record ledger sidecar is unsafe") from None
        return bindings

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        retained_root_fd: int | None = None
        sidecar_bindings: dict[str, tuple[int, tuple[int, int]]] = {}
        _SQLITE_OPEN_LOCK.acquire()
        try:
            if self._closed or self._root_fd is None:
                raise RecordSupersessionUnsafe("record ledger is closed")
            _RS_VALIDATE_STORAGE(self)
            before = _open_descriptor_identities()
            try:
                operation_root_fd = fcntl.fcntl(self._root_fd, fcntl.F_DUPFD_CLOEXEC, 0)
            except OSError:
                raise RecordSupersessionUnsafe(
                    "record ledger root descriptor is unavailable"
                ) from None
            if not _root_descriptor_is_valid(operation_root_fd, self._root_identity):
                os.close(operation_root_fd)
                raise RecordSupersessionUnsafe("record ledger root descriptor changed")
            connection, retained_root_fd = _open_anchored_sqlite_connection(
                operation_root_fd, self._root_identity
            )
            observed = os.stat(
                "record-supersession.sqlite3",
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            identity = (observed.st_dev, observed.st_ino)
            matches = [
                fd
                for fd, descriptor_identity in _open_descriptor_identities().items()
                if before.get(fd) != descriptor_identity
                and descriptor_identity == (stat.S_IFREG, *identity)
            ]
            if identity != self._database_identity or len(matches) != 1:
                raise RecordSupersessionUnsafe(
                    "record ledger connection identity is unproven"
                )
            sqlite_fd = matches[0]
            os.fchmod(sqlite_fd, 0o600)
            sidecar_bindings = _RS_BIND_SIDECARS(self, before)
            _RS_VALIDATE_STORAGE(self, sidecar_bindings)
            yield connection
        finally:
            try:
                if connection is not None:
                    try:
                        if "sqlite_fd" not in locals() or (
                            (metadata := _safe_fstat(sqlite_fd)) is None
                            or (metadata.st_dev, metadata.st_ino)
                            != self._database_identity
                        ):
                            raise RecordSupersessionUnsafe(
                                "record ledger connection identity changed"
                            )
                        if sidecar_bindings:
                            _RS_VALIDATE_STORAGE(self, sidecar_bindings)
                    finally:
                        try:
                            connection.close()
                        finally:
                            for descriptor, _ in sidecar_bindings.values():
                                os.close(descriptor)
                            if retained_root_fd is not None:
                                os.close(retained_root_fd)
                if not self._closed:
                    _RS_VALIDATE_STORAGE(self)
            finally:
                _SQLITE_OPEN_LOCK.release()

    def _initialize(self) -> None:
        with _RS_LINKAGE_FENCE(self) as authority, _RS_CONNECT(self) as connection:
            linkage = authority.snapshot
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
                self._validate_state(connection, linkage=linkage, authority=authority)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

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

    @contextmanager
    def _linkage_fence(self) -> Iterator[_LinkageAuthorityView]:
        try:
            with _PINNED_FENCED_ACTIVE_SNAPSHOT(self.linkage_store) as snapshot:
                if type(snapshot) is not ActiveLinkageSnapshot:
                    raise RecordSupersessionUnsafe("live linkage authority is invalid")
                history = _PINNED_AUTHORIZED_HISTORY_IN_FENCE(self.linkage_store)
                evaluated_at = _PINNED_AUTHORITY_TIME_IN_FENCE(self.linkage_store)
                activation_receipts = _PINNED_ACTIVATION_RECEIPT_HISTORY_IN_FENCE(
                    self.linkage_store
                )
                yield _LinkageAuthorityView(
                    snapshot=snapshot,
                    history=history,
                    evaluated_at=evaluated_at,
                    activation_receipts=activation_receipts,
                )
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

    def _validate_state(
        self,
        connection: sqlite3.Connection,
        *,
        linkage: ActiveLinkageSnapshot | None = None,
        authority: _LinkageAuthorityView | None = None,
    ) -> None:
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
        metadata = _RS_METADATA(connection)
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
        if linkage is None:
            linkage = _RS_LINKAGE_SNAPSHOT(self)
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
                record = _record_from_canonical_bytes(raw)
            except (RegistryIdentityError, TypeError, ValueError):
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
        _RS_VALIDATE_RECORD_HISTORY(records)
        if authority is not None:
            historical = {
                (
                    item.revision.provider_namespace,
                    item.revision.linkage_id,
                    item.revision.revision,
                ): (item, receipt)
                for item, receipt in zip(
                    authority.history,
                    authority.activation_receipts,
                    strict=True,
                )
            }
            by_id = {item.record_id: item for item in records}
            for record in records:
                item = historical.get(
                    (
                        record.provider_namespace,
                        record.linkage_id,
                        record.linkage_revision,
                    )
                )
                if item is None:
                    raise RecordSupersessionUnsafe(
                        "record ledger authority binding is invalid"
                    )
                authorized, receipt = item
                revision = authorized.revision
                if (
                    record.linkage_revision_sha256 != linkage_revision_sha256(revision)
                    or record.activation_receipt_sha256
                    != committed_linkage_receipt_sha256(receipt)
                    or record.analysis_record_id
                    != revision.technical.analysis_record_id
                ):
                    raise RecordSupersessionUnsafe(
                        "record ledger authority binding is invalid"
                    )
                source = (
                    by_id[record.supersedes_record_id]
                    if record.supersedes_record_id is not None
                    else None
                )
                upstream = revision.technical.reanalysis_of.token
                if record.lineage_role == RecordLineageRole.PRIMARY_ANALYSIS and (
                    upstream is not None or source is not None
                ):
                    raise RecordSupersessionUnsafe(
                        "record ledger authority lineage is invalid"
                    )
                if record.lineage_role == RecordLineageRole.REANALYSIS:
                    if source is None or upstream != source.analysis_record_id:
                        raise RecordSupersessionUnsafe(
                            "record ledger authority lineage is invalid"
                        )
                    source_item = historical.get(
                        (
                            source.provider_namespace,
                            source.linkage_id,
                            source.linkage_revision,
                        )
                    )
                    if (
                        source_item is None
                        or source_item[0].revision.biological != revision.biological
                    ):
                        raise RecordSupersessionUnsafe(
                            "record ledger authority lineage is invalid"
                        )
                    try:
                        _RS_VERIFY_SUPERSESSION_AUTHORIZATION(
                            record,
                            authority,
                            revision,
                            require_current_time=False,
                        )
                    except RecordSupersessionConflict:
                        raise RecordSupersessionUnsafe(
                            "record ledger supersession authority is invalid"
                        ) from None
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
                comparison = _comparison_from_canonical_bytes(raw)
            except (RegistryIdentityError, TypeError, ValueError):
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
                or row[2] != comparison.linkage_state_version
                or row[3] != comparison.linkage_state_head_sha256
                or not set(comparison.member_record_ids).issubset(record_ids)
            ):
                raise RecordSupersessionUnsafe("comparison history binding is invalid")
            if authority is not None:
                if (
                    comparison.linkage_state_version,
                    comparison.linkage_state_head_sha256,
                ) not in {
                    (item.state_version, item.state_head_sha256)
                    for item in authority.activation_receipts
                }:
                    raise RecordSupersessionUnsafe(
                        "comparison authority coordinates are invalid"
                    )
                try:
                    _RS_VERIFY_COMPARISON_AUTHORIZATION(
                        comparison,
                        authority,
                        by_id,
                        require_current_time=False,
                    )
                except RecordSupersessionConflict:
                    raise RecordSupersessionUnsafe(
                        "comparison authority is invalid"
                    ) from None
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
        if state_version != version or metadata["state_head_sha256"] != _RS_STATE_HEAD(
            connection
        ):
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
        head = _RS_STATE_HEAD(connection)
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
                snapshot.revisions, snapshot.activation_receipts, strict=True
            )
        }

    @staticmethod
    def _record_matches_live_linkage(
        record: SupersedingRecord,
        live: dict[tuple[str, str], tuple[object, object]],
        *,
        require_current_receipt: bool,
    ) -> bool:
        item = live.get((record.provider_namespace, record.linkage_id))
        if item is None:
            return False
        revision, receipt = item
        return bool(
            record.linkage_revision == revision.revision
            and record.linkage_revision_sha256 == linkage_revision_sha256(revision)
            and record.analysis_record_id == revision.technical.analysis_record_id
            and (
                not require_current_receipt
                or record.activation_receipt_sha256
                == committed_linkage_receipt_sha256(receipt)
            )
        )

    def _validate_record_against_linkage(
        self,
        record: SupersedingRecord,
        authority: _LinkageAuthorityView,
        *,
        source: SupersedingRecord | None,
        verify_new_action: bool = True,
    ) -> None:
        snapshot = authority.snapshot
        live = self._active_linkages(snapshot)
        item = live.get((record.provider_namespace, record.linkage_id))
        if item is None:
            raise RecordSupersessionConflict("record linkage is not active")
        revision = item[0]
        if not self._record_matches_live_linkage(
            record, live, require_current_receipt=True
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
        source_item = live.get((source.provider_namespace, source.linkage_id))
        if source_item is None:
            raise RecordSupersessionConflict("reanalysis source linkage is not active")
        source_revision = source_item[0]
        if (
            not self._record_matches_live_linkage(
                source, live, require_current_receipt=True
            )
            or source_revision.technical.analysis_record_id != source.analysis_record_id
            or source.provider_namespace != record.provider_namespace
            or source_revision.biological != revision.biological
        ):
            raise RecordSupersessionConflict(
                "reanalysis source is stale or changed biological lineage"
            )
        if verify_new_action:
            self._verify_supersession_authorization(record, authority, revision)

    @staticmethod
    def _verify_supersession_authorization(
        record: SupersedingRecord,
        authority: _LinkageAuthorityView,
        revision: LinkageRevision,
        *,
        require_current_time: bool = True,
    ) -> None:
        approval = record.supersession_authorization
        if approval is None:
            raise RecordSupersessionConflict("supersession authority is absent")
        payload = approval.payload
        authorized = next(
            (
                item
                for item in authority.history
                if item.revision.provider_namespace == record.provider_namespace
                and item.revision.linkage_id == record.linkage_id
                and item.revision.revision == record.linkage_revision
            ),
            None,
        )
        if authorized is None:
            raise RecordSupersessionConflict("supersession authority is unavailable")
        trust = authorized.trust_snapshot
        trust_digest = provider_trust_snapshot_sha256(trust)
        issuer = next(
            (
                item
                for item in trust.issuers
                if item.issuer_id == payload.issuer_id and item.key_id == payload.key_id
            ),
            None,
        )
        if (
            payload.provider_namespace != record.provider_namespace
            or payload.purpose != ApprovalPurpose.SUPERSEDE_RECORD
            or payload.role != ProviderRole.REVIEWER
            or payload.proposed_revision_sha256 != supersession_statement_sha256(record)
            or payload.trust_snapshot_id != trust.snapshot_id
            or payload.trust_snapshot_revision != trust.revision
            or payload.trust_snapshot_sha256 != trust_digest
            or issuer is None
            or issuer.status != IssuerStatus.ACTIVE
            or payload.role not in issuer.allowed_roles
            or payload.purpose not in issuer.allowed_purposes
            or trust.issued_at > payload.issued_at
            or payload.expires_at > trust.expires_at
            or (
                require_current_time
                and not (
                    payload.issued_at <= authority.evaluated_at < payload.expires_at
                )
            )
        ):
            raise RecordSupersessionConflict("supersession authority is invalid")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(issuer.public_key_base64, validate=True)
            )
            public_key.verify(
                base64.b64decode(approval.signature_base64, validate=True),
                approval_payload_bytes(payload),
            )
        except (InvalidSignature, TypeError, ValueError):
            raise RecordSupersessionConflict(
                "supersession authority is invalid"
            ) from None

    def commit_record(self, record: SupersedingRecord) -> RecordCommitReceipt:
        """Append one exact record, or return the receipt for an exact retry."""

        if type(record) is not SupersedingRecord:
            raise RecordSupersessionConflict("record contract is invalid")
        self._require_safe_record_shape(record)
        try:
            parsed = _record_from_canonical_bytes(canonical_contract_bytes(record))
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict("record contract is invalid") from None
        raw = canonical_contract_bytes(parsed)
        digest = hashlib.sha256(raw).hexdigest()
        with _RS_LINKAGE_FENCE(self) as authority, _RS_CONNECT(self) as connection:
            linkage = authority.snapshot
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection, linkage=linkage, authority=authority)
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
                        parsed,
                        authority,
                        source=source,
                        verify_new_action=False,
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
                self._validate_record_against_linkage(parsed, authority, source=source)
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
                self._validate_state(connection, linkage=linkage, authority=authority)
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
        return _record_from_canonical_bytes(bytes(row[0]))

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
            comparison = _comparison_from_canonical_bytes(bytes(row[1]))
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
        _require_exact_comparison_shape(
            comparison, error_type=RecordSupersessionConflict
        )
        try:
            parsed = _comparison_from_canonical_bytes(
                canonical_contract_bytes(comparison)
            )
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict("comparison contract is invalid") from None
        raw = canonical_contract_bytes(parsed)
        digest = hashlib.sha256(raw).hexdigest()
        with self._linkage_fence() as authority, self._connect() as connection:
            linkage = authority.snapshot
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection, linkage=linkage, authority=authority)
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
                if (
                    parsed.linkage_store_id != linkage.store_id
                    or parsed.linkage_store_epoch_sha256 != linkage.store_epoch_sha256
                    or parsed.linkage_storage_identity_sha256
                    != linkage.storage_identity_sha256
                    or parsed.linkage_state_version != linkage.state_version
                    or parsed.linkage_state_head_sha256 != linkage.state_head_sha256
                    or any(
                        item.provider_namespace != parsed.provider_namespace
                        for item in active.values()
                        if item.record_id in parsed.member_record_ids
                    )
                ):
                    raise RecordSupersessionConflict(
                        "comparison authority coordinates are invalid"
                    )
                self._verify_comparison_authorization(parsed, authority, active)
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
                self._validate_state(connection, linkage=linkage, authority=authority)
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

    @staticmethod
    def _verify_comparison_authorization(
        comparison: DerivedComparison,
        authority: _LinkageAuthorityView,
        records: dict[str, SupersedingRecord],
        *,
        require_current_time: bool = True,
    ) -> None:
        first = records[comparison.member_record_ids[0]]
        authorized = next(
            (
                item
                for item in authority.history
                if item.revision.provider_namespace == first.provider_namespace
                and item.revision.linkage_id == first.linkage_id
                and item.revision.revision == first.linkage_revision
            ),
            None,
        )
        if authorized is None:
            raise RecordSupersessionConflict("comparison authority is unavailable")
        payload = comparison.authority.payload
        trust = authorized.trust_snapshot
        issuer = next(
            (
                item
                for item in trust.issuers
                if item.issuer_id == payload.issuer_id and item.key_id == payload.key_id
            ),
            None,
        )
        if (
            payload.provider_namespace != comparison.provider_namespace
            or payload.purpose != ApprovalPurpose.REGISTER_COMPARISON
            or payload.role != ProviderRole.REVIEWER
            or payload.proposed_revision_sha256
            != comparison_authority_statement_sha256(comparison)
            or payload.trust_snapshot_id != trust.snapshot_id
            or payload.trust_snapshot_revision != trust.revision
            or payload.trust_snapshot_sha256 != provider_trust_snapshot_sha256(trust)
            or issuer is None
            or issuer.status != IssuerStatus.ACTIVE
            or payload.role not in issuer.allowed_roles
            or payload.purpose not in issuer.allowed_purposes
            or trust.issued_at > payload.issued_at
            or payload.expires_at > trust.expires_at
            or (
                require_current_time
                and not (
                    payload.issued_at <= authority.evaluated_at < payload.expires_at
                )
            )
        ):
            raise RecordSupersessionConflict("comparison authority is invalid")
        try:
            Ed25519PublicKey.from_public_bytes(
                base64.b64decode(issuer.public_key_base64, validate=True)
            ).verify(
                base64.b64decode(comparison.authority.signature_base64, validate=True),
                approval_payload_bytes(payload),
            )
        except (InvalidSignature, TypeError, ValueError):
            raise RecordSupersessionConflict(
                "comparison authority is invalid"
            ) from None

    def _active_records(
        self, connection: sqlite3.Connection, linkage: ActiveLinkageSnapshot
    ) -> tuple[SupersedingRecord, ...]:
        records = [
            _record_from_canonical_bytes(bytes(row[0]))
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
        active = [
            record
            for record in records
            if record.record_id not in superseded
            and self._record_matches_live_linkage(
                record, live, require_current_receipt=True
            )
        ]
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
            comparison = _comparison_from_canonical_bytes(bytes(row[2]))
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

    @staticmethod
    def _history_records(
        connection: sqlite3.Connection,
    ) -> tuple[SupersedingRecord, ...]:
        """Load the validated global ledger in canonical record-ID order."""

        return tuple(
            sorted(
                (
                    _record_from_canonical_bytes(bytes(row[0]))
                    for row in connection.execute(
                        "SELECT record_json FROM records ORDER BY sequence"
                    )
                ),
                key=lambda item: item.record_id,
            )
        )

    @staticmethod
    def _comparison_warnings_by_record(
        connection: sqlite3.Connection,
        linkage: ActiveLinkageSnapshot,
        record_ids: set[str],
    ) -> dict[str, tuple[ComparisonStatus, ...]]:
        result: dict[str, list[ComparisonStatus]] = {
            record_id: [] for record_id in record_ids
        }
        warning_count = 0
        for row in connection.execute(
            "SELECT comparison_json FROM comparisons ORDER BY comparison_id"
        ):
            comparison = _comparison_from_canonical_bytes(bytes(row[0]))
            selected_members = record_ids.intersection(comparison.member_record_ids)
            if not selected_members:
                continue
            reasons = tuple(
                InvalidationReason(item[0])
                for item in connection.execute(
                    "SELECT reason FROM invalidations WHERE comparison_id=? ORDER BY reason",
                    (comparison.comparison_id,),
                )
            )
            if not reasons:
                continue
            status = ComparisonStatus(
                comparison_id=comparison.comparison_id,
                state=ComparisonState.STALE,
                reasons=reasons,
                member_record_ids=comparison.member_record_ids,
                derived_artifact_sha256=comparison.derived_artifact_sha256,
                linkage_state_version=linkage.state_version,
                linkage_state_head_sha256=linkage.state_head_sha256,
            )
            for record_id in sorted(selected_members):
                warning_count += 1
                if warning_count > MAX_HISTORY_COMPARISON_WARNINGS:
                    raise RecordSupersessionConflict(
                        "record history comparison warnings exceed their bound"
                    )
                result[record_id].append(status)
        return {
            record_id: tuple(sorted(statuses, key=lambda item: item.comparison_id))
            for record_id, statuses in result.items()
        }

    def record_history_snapshot(
        self, *, after_record_id: str | None = None, limit: int = 100
    ) -> RecordHistorySnapshot:
        """Return one canonical bounded page under ledger and linkage fences."""

        if type(self) is not RecordSupersessionStore:
            raise RecordSupersessionUnsafe("record history store is invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE_RECORDS:
            raise RecordSupersessionConflict("record history page bound is invalid")
        if after_record_id is not None:
            if (
                type(after_record_id) is not str
                or len(after_record_id) != 47
                or not after_record_id.startswith("record_")
                or any(
                    character not in "0123456789abcdef"
                    for character in after_record_id[7:]
                )
            ):
                raise RecordSupersessionConflict("record history cursor is invalid")
            try:
                after_record_id = _RECORD_ID.validate_python(
                    after_record_id, strict=True
                )
            except (TypeError, ValueError):
                raise RecordSupersessionConflict(
                    "record history cursor is invalid"
                ) from None
        with _RS_LINKAGE_FENCE(self) as authority, _RS_CONNECT(self) as connection:
            linkage = authority.snapshot
            connection.execute("BEGIN IMMEDIATE")
            try:
                _RS_VALIDATE_STATE(
                    self, connection, linkage=linkage, authority=authority
                )
                records = _RS_HISTORY_RECORDS(connection)
                if after_record_id is not None and all(
                    record.record_id != after_record_id for record in records
                ):
                    raise RecordSupersessionConflict(
                        "record history cursor is unavailable"
                    )
                ordered = tuple(
                    record
                    for record in records
                    if after_record_id is None or record.record_id > after_record_id
                )
                selected = ordered[:limit]
                if _RS_REFRESH_INVALIDATIONS(self, connection, linkage):
                    _RS_ADVANCE(connection)
                _RS_VALIDATE_STATE(
                    self, connection, linkage=linkage, authority=authority
                )
                metadata = _RS_METADATA(connection)
                activation_receipts = {
                    (
                        item.revision.provider_namespace,
                        item.revision.linkage_id,
                        item.revision.revision,
                    ): receipt
                    for item, receipt in zip(
                        authority.history,
                        authority.activation_receipts,
                        strict=True,
                    )
                }
                warnings = _RS_COMPARISON_WARNINGS_BY_RECORD(
                    connection,
                    linkage,
                    {item.record_id for item in selected},
                )
                live = _RS_ACTIVE_LINKAGES(linkage)
                successor_by_id = {
                    item.supersedes_record_id: item.record_id
                    for item in records
                    if item.supersedes_record_id is not None
                }
                entries: list[RecordHistoryEntry] = []
                for record in selected:
                    key = (
                        record.provider_namespace,
                        record.linkage_id,
                        record.linkage_revision,
                    )
                    receipt = activation_receipts.get(key)
                    if receipt is None:
                        raise RecordSupersessionUnsafe(
                            "record history activation receipt is unavailable"
                        )
                    successor_record_id = successor_by_id.get(record.record_id)
                    if successor_record_id is not None:
                        state = RecordHistoryState.SUPERSEDED
                    elif _RS_RECORD_MATCHES_LIVE_LINKAGE(
                        record, live, require_current_receipt=True
                    ):
                        state = RecordHistoryState.ACTIVE
                    else:
                        state = RecordHistoryState.AUTHORITY_INVALID
                    entries.append(
                        RecordHistoryEntry(
                            record=record,
                            record_sha256=record_sha256(record),
                            activation_receipt=receipt,
                            state=state,
                            successor_record_id=successor_record_id,
                            affected_comparisons=warnings[record.record_id],
                        )
                    )
                has_more = len(ordered) > len(selected)
                snapshot = RecordHistorySnapshot(
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
                    after_record_id=after_record_id,
                    limit=limit,
                    records=tuple(entries),
                    next_after_record_id=(
                        entries[-1].record.record_id if has_more and entries else None
                    ),
                )
                # Capture a detached canonical value before either authority fence
                # is released; no internal database or authority object is returned.
                captured = _history_snapshot_from_canonical_bytes(
                    canonical_contract_bytes(snapshot)
                )
                _RS_VALIDATE_STATE(
                    self, connection, linkage=linkage, authority=authority
                )
                connection.commit()
                return captured
            except BaseException:
                connection.rollback()
                raise

    def replay_history_snapshot(
        self, snapshot: RecordHistorySnapshot
    ) -> RecordHistorySnapshot:
        """Re-read the selected chain and reject stale or caller-altered history."""

        if type(self) is not RecordSupersessionStore:
            raise RecordSupersessionUnsafe("record history store is invalid")
        _require_exact_history_snapshot_shape(
            snapshot, error_type=RecordSupersessionConflict
        )
        try:
            parsed = _history_snapshot_from_canonical_bytes(
                canonical_contract_bytes(snapshot)
            )
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict(
                "record history snapshot is invalid"
            ) from None
        current = _RS_RECORD_HISTORY_SNAPSHOT(
            self,
            after_record_id=parsed.after_record_id,
            limit=parsed.limit,
        )
        if parsed != current:
            raise RecordSupersessionConflict(
                "record history snapshot is stale or invalid"
            )
        return parsed

    def active_snapshot(self) -> ActiveRecordSnapshot:
        with self._linkage_fence() as authority, self._connect() as connection:
            linkage = authority.snapshot
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection, linkage=linkage, authority=authority)
                if self._refresh_invalidations(connection, linkage):
                    self._advance(connection)
                self._validate_state(connection, linkage=linkage, authority=authority)
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
        if (
            type(comparison_id) is not str
            or len(comparison_id) != 51
            or not comparison_id.startswith("comparison_")
            or any(
                character not in "0123456789abcdef" for character in comparison_id[11:]
            )
        ):
            raise RecordSupersessionConflict("comparison identity is invalid")
        try:
            comparison_id = _COMPARISON_ID.validate_python(comparison_id, strict=True)
        except (TypeError, ValueError):
            raise RecordSupersessionConflict("comparison identity is invalid") from None
        with self._linkage_fence() as authority, self._connect() as connection:
            linkage = authority.snapshot
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_state(connection, linkage=linkage, authority=authority)
                changed = self._refresh_invalidations(connection, linkage)
                if changed:
                    self._advance(connection)
                self._validate_state(connection, linkage=linkage, authority=authority)
                row = connection.execute(
                    "SELECT comparison_json FROM comparisons WHERE comparison_id=?",
                    (comparison_id,),
                ).fetchone()
                if row is None:
                    raise RecordSupersessionConflict("comparison is unknown")
                comparison = _comparison_from_canonical_bytes(bytes(row[0]))
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
        _require_exact_snapshot_shape(snapshot, error_type=RecordSupersessionConflict)
        try:
            parsed = contract_from_canonical_bytes(
                ActiveRecordSnapshot, canonical_contract_bytes(snapshot)
            )
        except (RegistryIdentityError, TypeError, ValueError):
            raise RecordSupersessionConflict("record snapshot is invalid") from None
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
        _require_exact_record_shape(record, error_type=RecordSupersessionConflict)


# Captured unbound operations keep protected reads on the reviewed
# implementation even if a caller shadows instance or class attributes.
_RS_VALIDATE_STORAGE = RecordSupersessionStore._validate_storage
_RS_BIND_SIDECARS = RecordSupersessionStore._bind_sidecars
_RS_CONNECT = RecordSupersessionStore._connect
_RS_LINKAGE_FENCE = RecordSupersessionStore._linkage_fence
_RS_LINKAGE_SNAPSHOT = RecordSupersessionStore._linkage_snapshot
_RS_METADATA = RecordSupersessionStore._metadata
_RS_STATE_HEAD = RecordSupersessionStore._state_head
_RS_VALIDATE_STATE = RecordSupersessionStore._validate_state
_RS_VALIDATE_RECORD_HISTORY = RecordSupersessionStore._validate_record_history
_RS_VERIFY_SUPERSESSION_AUTHORIZATION = (
    RecordSupersessionStore._verify_supersession_authorization
)
_RS_VERIFY_COMPARISON_AUTHORIZATION = (
    RecordSupersessionStore._verify_comparison_authorization
)
_RS_HISTORY_RECORDS = RecordSupersessionStore._history_records
_RS_REFRESH_INVALIDATIONS = RecordSupersessionStore._refresh_invalidations
_RS_ADVANCE = RecordSupersessionStore._advance
_RS_COMPARISON_WARNINGS_BY_RECORD = (
    RecordSupersessionStore._comparison_warnings_by_record
)
_RS_ACTIVE_LINKAGES = RecordSupersessionStore._active_linkages
_RS_RECORD_MATCHES_LIVE_LINKAGE = RecordSupersessionStore._record_matches_live_linkage
_RS_RECORD_HISTORY_SNAPSHOT = RecordSupersessionStore.record_history_snapshot


__all__ = [
    "ActiveRecordSnapshot",
    "ComparisonCommitReceipt",
    "ComparisonState",
    "ComparisonStatus",
    "DerivedComparison",
    "InvalidationReason",
    "RecordCommitReceipt",
    "RecordHistoryEntry",
    "RecordHistorySnapshot",
    "RecordHistoryState",
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
