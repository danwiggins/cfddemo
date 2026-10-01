"""Protected append-only registry for live-replayed D07 repeatability comparisons.

The registry is provider-local.  Callers never supply a comparison or a D03
decision: registration derives the D03 member decision and the D07 comparison
itself from exact records, policy, signed observations, envelope, and pins,
under one held linkage authority fence, and stores those inputs beside the
derived comparison.  Every protected read re-runs the pinned D07 evaluator on
the stored inputs against the live linkage store and the live result trust
document, and returns nothing unless the output is byte-identical.  The
separate selector projection carries only opaque selectors, digests, states,
and authority state.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.longitudinal_compatibility as d03_module
import evidence_inspector.repeatability_comparison as d07_module
from evidence_inspector.cohort_manifest import (
    capture_expected_trust_pins,
    trust_pins_sha256,
)
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalAnchorPolicy,
    LongitudinalMemberDecision,
    LongitudinalOutcome,
    LongitudinalRecord,
    decide_longitudinal_member,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
    longitudinal_record_sha256,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage_store import (
    ProviderLinkageStore,
    ProviderLinkageStoreConflict,
    ProviderLinkageStoreSchemaError,
    ProviderLinkageStoreUnsafe,
)
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    ComparisonObservation,
    RepeatabilityClassification,
    RepeatabilityComparison,
    RepeatabilityEnvelope,
    compare_repeatability_in_fence,
    repeatability_comparison_sha256,
    repeatability_envelope_sha256,
    result_trust_document_sha256,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)
from traceback_runner.signing import DevelopmentTrustDocument

MAX_REGISTERED_COMPARISONS = 10_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 64
MAX_OBJECT_GRAPH_NODES = 100_000
MAX_OBJECT_COLLECTION_ITEMS = 256
MAX_OBJECT_STRING_BYTES = 64 * 1024
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_AUTHORITY_READ_FENCE = ProviderLinkageStore.authority_read_fence
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_AUTHORITY_TIME_IN_FENCE = ProviderLinkageStore.authority_time_in_fence
_PINNED_DECIDE_MEMBER = decide_longitudinal_member
_PINNED_INVALID_MEMBER_BYTES = d03_module._INVALID_INPUT_MEMBER_DECISION_BYTES
_PINNED_COMPARE_IN_FENCE = compare_repeatability_in_fence
_PINNED_COMPARISON_SHA256 = repeatability_comparison_sha256
_PINNED_RESULT_TRUST_SHA256 = result_trust_document_sha256

RegistryId = Annotated[str, StringConstraints(pattern=r"^d07_registry_[0-9a-f]{32}$")]
ComparisonSelectorId = Annotated[
    str, StringConstraints(pattern=r"^d07_comparison_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_SELECTOR_PREFIX = "d07_comparison_"
_SELECTOR_LENGTH = len(_SELECTOR_PREFIX) + 40


class RepeatabilityComparisonRegistryError(RuntimeError):
    """Sanitized registry failure."""


class RepeatabilityComparisonRegistryConflict(RepeatabilityComparisonRegistryError):
    pass


class RepeatabilityComparisonRegistryStale(RepeatabilityComparisonRegistryConflict):
    """A registered comparison no longer replays against live authority."""


class RepeatabilityComparisonRegistryUnsafe(RepeatabilityComparisonRegistryError):
    pass


class ComparisonAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class RepeatabilityComparisonRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-registry-metadata.v1"] = (
        "traceback.d07-comparison-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_trust_pins_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    RepeatabilityComparisonRegistryMetadata
)


def _utc_instant(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


class RegisteredComparisonObject(RegistryContract):
    """Protected stored inputs plus the comparison the registry derived from them.

    ``decision`` is the D03 member decision the registry derived; it is stored
    only because the pinned D07 evaluator takes it and replays it against live
    linkage on every evaluation.  ``comparison.evaluated_at`` is the linkage
    store's authority time when the registry derived the comparison.
    """

    schema_version: Literal["traceback.d07-registered-comparison-object.v1"] = (
        "traceback.d07-registered-comparison-object.v1"
    )
    anchor: LongitudinalRecord
    member: LongitudinalRecord
    policy: LongitudinalAnchorPolicy
    expected_policy_sha256: Sha256
    expected_authority_head_sha256: Sha256
    decision: LongitudinalMemberDecision
    anchor_observation: ComparisonObservation
    member_observation: ComparisonObservation
    envelope: RepeatabilityEnvelope
    expected_envelope_sha256: Sha256
    expected_evidence_sha256: Sha256
    expected_protocol_sha256: Sha256
    expected_repeatability_authority_sha256: Sha256
    comparison: RepeatabilityComparison

    @model_validator(mode="after")
    def exact_bindings(self) -> RegisteredComparisonObject:
        comparison = self.comparison
        if comparison.anchor_record_sha256 != longitudinal_record_sha256(self.anchor):
            raise ValueError("registered comparison anchor does not match its inputs")
        if comparison.member_record_sha256 != longitudinal_record_sha256(self.member):
            raise ValueError("registered comparison member does not match its inputs")
        if comparison.anchor_policy_sha256 != longitudinal_anchor_policy_sha256(
            self.policy
        ):
            raise ValueError("registered comparison policy does not match its inputs")
        if comparison.d03_decision_sha256 != longitudinal_member_decision_sha256(
            self.decision
        ):
            raise ValueError("registered comparison decision does not match its inputs")
        if comparison.repeatability_envelope_sha256 != repeatability_envelope_sha256(
            self.envelope
        ):
            raise ValueError("registered comparison envelope does not match its inputs")
        if not _utc_instant(comparison.evaluated_at):
            raise ValueError("registered comparison evaluation time must be UTC")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredComparisonObject
)


class RepeatabilityComparisonJournalEntry(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-journal-entry.v1"] = (
        "traceback.d07-comparison-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_COMPARISONS, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class ComparisonRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-registration-receipt.v1"] = (
        "traceback.d07-comparison-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_COMPARISONS)
    state_head_sha256: Sha256
    selector_id: ComparisonSelectorId
    object_sha256: Sha256
    comparison_sha256: Sha256
    availability: ComparisonAvailability
    classification: RepeatabilityClassification

    @model_validator(mode="after")
    def exact_selector(self) -> ComparisonRegistrationReceipt:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("comparison receipt selector does not match its object")
        return self


class RegisteredRepeatabilityComparison(RegistryContract):
    """Protected comparison that replayed exactly against live authority.

    ``comparison`` is the stored, byte-identical registration artifact, so its
    ``evaluated_at`` is the registration authority time.  ``replayed_at`` is the
    live authority time at which the same inputs produced the same comparison;
    it is the as-of instant of this read.
    """

    schema_version: Literal["traceback.d07-registered-comparison.v1"] = (
        "traceback.d07-registered-comparison.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_COMPARISONS)
    state_head_sha256: Sha256
    selector_id: ComparisonSelectorId
    object_sha256: Sha256
    comparison_sha256: Sha256
    comparison: RepeatabilityComparison
    replayed_at: datetime
    replayed_against_live_linkage: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredRepeatabilityComparison:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("registered comparison selector does not match its object")
        if self.comparison_sha256 != _comparison_sha256(self.comparison):
            raise ValueError("registered comparison digest is invalid")
        if not _utc_instant(self.replayed_at) or (
            self.replayed_at < self.comparison.evaluated_at
        ):
            raise ValueError("registered comparison replay time is invalid")
        return self


class ComparisonSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-selector-record.v1"] = (
        "traceback.d07-comparison-selector-record.v1"
    )
    selector_id: ComparisonSelectorId
    object_sha256: Sha256
    comparison_sha256: Sha256
    d03_decision_sha256: Sha256
    anchor_policy_sha256: Sha256
    repeatability_envelope_sha256: Sha256
    d03_outcome: LongitudinalOutcome
    availability: ComparisonAvailability
    classification: RepeatabilityClassification
    authority_state: ComparisonAuthorityState


class ComparisonSelectorPage(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-selector-page.v1"] = (
        "traceback.d07-comparison-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_COMPARISONS)
    state_head_sha256: Sha256
    records: tuple[ComparisonSelectorRecord, ...] = Field(max_length=MAX_SELECTOR_PAGE)
    next_after_selector_id: ComparisonSelectorId | None


class RepeatabilityComparisonBackupObject(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-backup-object.v1"] = (
        "traceback.d07-comparison-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class RepeatabilityComparisonBackup(RegistryContract):
    schema_version: Literal["traceback.d07-comparison-backup.v1"] = (
        "traceback.d07-comparison-backup.v1"
    )
    metadata: RepeatabilityComparisonRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_COMPARISONS)
    state_head_sha256: Sha256
    journal: tuple[RepeatabilityComparisonJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_COMPARISONS
    )
    objects: tuple[RepeatabilityComparisonBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_COMPARISONS
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(
    RepeatabilityComparisonBackup
)


def registered_comparison_object_bytes(value: RegisteredComparisonObject) -> bytes:
    """Return exact bounded canonical bytes for one stored comparison object."""

    return exact_model_bytes(
        value,
        RegisteredComparisonObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_comparison_object_from_bytes(content: bytes) -> RegisteredComparisonObject:
    try:
        # The bounded parse enforces every structural budget before validation;
        # strict D03/D07 contracts then validate in JSON mode from the same bytes.
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredComparisonObject.model_validate_json(content)
        if registered_comparison_object_bytes(value) != content:
            raise ValueError("registered comparison object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered comparison object is not canonical") from None


def _canonical_backup_bytes(backup: RepeatabilityComparisonBackup) -> bytes:
    return exact_model_bytes(
        backup,
        RepeatabilityComparisonBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_COMPARISONS,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("D07 comparison registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("D07 comparison registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "D07 comparison registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("D07 comparison registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("D07 comparison registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry object exceeds its bound"
            )
        chunks.append(chunk)


def _publish_file(directory_fd: int, name: str, content: bytes) -> None:
    temporary = f".tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)


def _read_exact_object(directory_fd: int, digest: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            f"{digest}.json",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry object digest is invalid"
        )
    return content


def _selector_id(epoch: str, object_sha256: str) -> str:
    digest = hashlib.sha256(
        b"traceback-d07-comparison-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + object_sha256.encode("ascii")
    ).hexdigest()
    return f"{_SELECTOR_PREFIX}{digest[:40]}"


def _comparison_sha256(comparison: RepeatabilityComparison) -> str:
    return _PINNED_COMPARISON_SHA256(comparison)


def _is_selector(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _SELECTOR_LENGTH
        and value.startswith(_SELECTOR_PREFIX)
        and all(
            character in "0123456789abcdef"
            for character in value[len(_SELECTOR_PREFIX) :]
        )
    )


def _journal_entry_sha256(entry: RepeatabilityComparisonJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-d07-comparison-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: RepeatabilityComparisonRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-d07-comparison-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> RepeatabilityComparisonJournalEntry:
    placeholder = RepeatabilityComparisonJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return RepeatabilityComparisonJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_backup(backup: RepeatabilityComparisonBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            registered_comparison_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup digest is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup state is invalid"
        )


def repeatability_comparison_backup_from_bytes(
    content: bytes,
) -> RepeatabilityComparisonBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_COMPARISONS,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = RepeatabilityComparisonBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("D07 comparison registry backup is not canonical")
    except (TypeError, ValueError):
        raise RepeatabilityComparisonRegistryConflict(
            "D07 comparison registry backup is invalid"
        ) from None
    _validate_backup(backup)
    return backup


def _remove_partial_restore(
    parent_fd: int | None, name: str, root_fd: int, objects_fd: int | None
) -> None:
    """Remove only the files a failed restore created, then its root."""

    try:
        if objects_fd is not None:
            for entry in os.listdir(objects_fd):
                os.unlink(entry, dir_fd=objects_fd)
            os.rmdir("objects", dir_fd=root_fd)
        for entry in os.listdir(root_fd):
            try:
                os.unlink(entry, dir_fd=root_fd)
            except OSError:
                os.rmdir(entry, dir_fd=root_fd)
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _registry_instance_snapshot(
    registry: RepeatabilityComparisonRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_linkage_store",
        "_trust_pins",
        "_result_trust_document",
        "_result_trust_sha256",
        "_root_identity",
        "_objects_identity",
        "_lock_identity",
        "_journal_identity",
        "_metadata_identity",
        "_process_lock",
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    if type(instance) is not dict or any(name not in instance for name in required):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            RepeatabilityComparisonRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            ) from None
        root_identity = (root_observed.st_dev, root_observed.st_ino)
        metadata_identity = (metadata_observed.st_dev, metadata_observed.st_ino)
        derived_head_key = (
            root_identity[0],
            root_identity[1],
            metadata.registry_id,
            metadata.registry_epoch_sha256,
        )
        if (
            instance["_root_identity"] != root_identity
            or instance["_metadata_identity"] != metadata_identity
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"] != derived_head_key
        ):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry authority state changed"
            )
    pins = instance["_trust_pins"]
    if type(pins) is not dict:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )
    try:
        captured_pins = capture_expected_trust_pins(pins)
    except (TypeError, ValueError):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        ) from None
    result_trust_sha256 = instance["_result_trust_sha256"]
    try:
        observed_result_trust_sha256 = _PINNED_RESULT_TRUST_SHA256(
            instance["_result_trust_document"]
        )
    except Exception:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        ) from None
    if (
        not _is_sha256(result_trust_sha256)
        or observed_result_trust_sha256 != result_trust_sha256
    ):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )
    return (
        id(instance["root"]),
        id(instance["_linkage_store"]),
        tuple(sorted(captured_pins.items())),
        id(instance["_result_trust_document"]),
        result_trust_sha256,
        instance["_root_identity"],
        instance["_objects_identity"],
        instance["_lock_identity"],
        instance["_journal_identity"],
        instance["_metadata_identity"],
        id(instance["_process_lock"]),
        metadata_bytes,
        instance["_genesis_head_sha256"],
        instance["_head_key"],
        instance["_trusted_head_sha256"],
    )


def _seal_registry_instance(registry: RepeatabilityComparisonRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class RepeatabilityComparisonRegistry:
    """Descriptor-relative immutable D07 comparison publication with live replay."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "backup_bytes",
            "close",
            "list_selectors",
            "register_comparison",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        result_trust_document: DevelopmentTrustDocument,
        expected_result_trust_sha256: str,
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("D07 comparison registry requires the exact linkage store type")
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values):
            if (
                any(item is None for item in expected_values)
                or type(expected_registry_id) is not str
                or len(expected_registry_id) != 45
                or not expected_registry_id.startswith("d07_registry_")
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_registry_id[13:]
                )
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry expected identity or head is invalid"
                )
        self.root = _snapshot_path(root)
        self._linkage_store = linkage_store
        self._trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        if _PINNED_ACTIVE_SNAPSHOT(
            self._linkage_store
        ).trust_pins_sha256 != trust_pins_sha256(self._trust_pins):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry trust pins are invalid"
            )
        # Result trust is live registry configuration, not stored per object:
        # replacing or revoking it makes every comparison it changes stale.
        try:
            observed_result_trust_sha256 = _PINNED_RESULT_TRUST_SHA256(
                result_trust_document
            )
        except Exception:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry result trust is invalid"
            ) from None
        if (
            not _is_sha256(expected_result_trust_sha256)
            or observed_result_trust_sha256 != expected_result_trust_sha256
        ):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry result trust is invalid"
            )
        self._result_trust_document = result_trust_document
        self._result_trust_sha256 = observed_result_trust_sha256
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        try:
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            except FileExistsError:
                root_created = False
            else:
                root_created = True
            root_lstat = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or stat.S_IMODE(root_lstat.st_mode) != 0o700
                or root_lstat.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry root must be private"
                )
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            self._root_fd = os.open(self.root, flags)
            bound = os.fstat(self._root_fd)
            if (bound.st_dev, bound.st_ino) != (root_lstat.st_dev, root_lstat.st_ino):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry root changed"
                )
            self._root_identity = (bound.st_dev, bound.st_ino)
            if root_created:
                os.mkdir("objects", 0o700, dir_fd=self._root_fd)
            self._objects_fd = os.open("objects", flags, dir_fd=self._root_fd)
            objects = os.fstat(self._objects_fd)
            if (
                not stat.S_ISDIR(objects.st_mode)
                or stat.S_IMODE(objects.st_mode) != 0o700
                or objects.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry objects are unsafe"
                )
            self._objects_identity = (objects.st_dev, objects.st_ino)
            self._lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR
                | (os.O_CREAT if root_created else 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            lock_metadata = os.fstat(self._lock_fd)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
                or lock_metadata.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry lock is unsafe"
                )
            self._lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
            self._journal_fd = os.open(
                "registry-journal.jsonl",
                os.O_RDWR
                | (os.O_CREAT if root_created else 0)
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            journal_metadata = os.fstat(self._journal_fd)
            if (
                not stat.S_ISREG(journal_metadata.st_mode)
                or stat.S_IMODE(journal_metadata.st_mode) != 0o600
                or journal_metadata.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
                with _CR_LOCK(self, exclusive=True):
                    self._metadata = _CR_LOAD_OR_CREATE_METADATA(
                        self, allow_create=root_created
                    )
                    self._genesis_head_sha256 = _metadata_genesis_sha256(
                        self._metadata
                    )
                    self._head_key = (
                        self._root_identity[0],
                        self._root_identity[1],
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                    )
                    _CR_RECOVER_TEMPORARY_OBJECTS(self)
                    _, head = _CR_LOAD_STATE(self, check_trusted_head=False)
                    if root_created:
                        if any(item is not None for item in expected_values):
                            raise RepeatabilityComparisonRegistryUnsafe(
                                "new D07 comparison registry cannot inherit an "
                                "expected identity"
                            )
                    elif any(item is None for item in expected_values):
                        raise RepeatabilityComparisonRegistryUnsafe(
                            "D07 comparison registry expected identity and head are "
                            "required"
                        )
                    if not root_created and expected_values != (
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                        head,
                    ):
                        raise RepeatabilityComparisonRegistryUnsafe(
                            "D07 comparison registry expected identity or head is invalid"
                        )
                    self._trusted_head_sha256 = head
                    _CR_ACCEPT_OBSERVED_HEAD(
                        self, _CR_LOAD_JOURNAL(self), head, check_instance=False
                    )
                    _seal_registry_instance(self)
        except BaseException:
            # Construction has not installed the instance seal yet, so cleanup
            # cannot pass through the public integrity-checked close boundary.
            for name in (
                "_journal_fd",
                "_metadata_fd",
                "_lock_fd",
                "_objects_fd",
                "_root_fd",
            ):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)
            raise

    def close(self) -> None:
        _require_registry_integrity(self)
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            return
        with lock:
            for name in (
                "_journal_fd",
                "_metadata_fd",
                "_lock_fd",
                "_objects_fd",
                "_root_fd",
            ):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)

    def __enter__(self) -> RepeatabilityComparisonRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _CR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _CR_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _CR_VALIDATE_STORAGE(self)
                yield
                _CR_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        try:
            root_path = os.stat(self.root, follow_symlinks=False)
            root_bound = os.fstat(self._root_fd)
            objects_path = os.stat(
                "objects", dir_fd=self._root_fd, follow_symlinks=False
            )
            objects_bound = os.fstat(self._objects_fd)
            lock_path = os.stat(
                ".registry.lock", dir_fd=self._root_fd, follow_symlinks=False
            )
            lock_bound = os.fstat(self._lock_fd)
            journal_path = os.stat(
                "registry-journal.jsonl",
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            journal_bound = os.fstat(self._journal_fd)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (root_bound.st_dev, root_bound.st_ino) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
            or not stat.S_ISDIR(objects_path.st_mode)
            or (objects_path.st_dev, objects_path.st_ino) != self._objects_identity
            or (objects_bound.st_dev, objects_bound.st_ino) != self._objects_identity
            or stat.S_IMODE(objects_bound.st_mode) != 0o700
            or objects_bound.st_uid != os.geteuid()
            or not stat.S_ISREG(lock_path.st_mode)
            or (lock_path.st_dev, lock_path.st_ino) != self._lock_identity
            or (lock_bound.st_dev, lock_bound.st_ino) != self._lock_identity
            or stat.S_IMODE(lock_bound.st_mode) != 0o600
            or lock_bound.st_uid != os.geteuid()
            or not stat.S_ISREG(journal_path.st_mode)
            or (journal_path.st_dev, journal_path.st_ino) != self._journal_identity
            or (journal_bound.st_dev, journal_bound.st_ino) != self._journal_identity
            or stat.S_IMODE(journal_bound.st_mode) != 0o600
            or journal_bound.st_uid != os.geteuid()
        ):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry storage changed"
            )
        if self._metadata_fd is not None:
            try:
                metadata_path = os.stat(
                    "registry-metadata.json",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                metadata_bound = os.fstat(self._metadata_fd)
            except OSError:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry storage changed"
                ) from None
            if (
                not stat.S_ISREG(metadata_path.st_mode)
                or (metadata_path.st_dev, metadata_path.st_ino)
                != self._metadata_identity
                or (metadata_bound.st_dev, metadata_bound.st_ino)
                != self._metadata_identity
                or stat.S_IMODE(metadata_bound.st_mode) != 0o600
                or metadata_bound.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        if self._objects_fd is None:
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry objects are unavailable"
            ) from None
        for name in names:
            if (
                type(name) is str
                and len(name) == 37
                and name.startswith(".tmp-")
                and all(character in "0123456789abcdef" for character in name[5:])
            ):
                try:
                    os.unlink(name, dir_fd=self._objects_fd)
                except OSError:
                    raise RepeatabilityComparisonRegistryUnsafe(
                        "D07 comparison registry recovery is unsafe"
                    ) from None
        os.fsync(self._objects_fd)

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> RepeatabilityComparisonRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry metadata is missing"
                ) from None
            snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
            metadata = RepeatabilityComparisonRegistryMetadata(
                registry_id=f"d07_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                linkage_store_id=snapshot.store_id,
                linkage_store_epoch_sha256=snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=snapshot.storage_identity_sha256,
                linkage_trust_pins_sha256=snapshot.trust_pins_sha256,
            )
            try:
                _CR_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _CR_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                RepeatabilityComparisonRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
        if (
            metadata.linkage_store_id,
            metadata.linkage_store_epoch_sha256,
            metadata.linkage_storage_identity_sha256,
            metadata.linkage_trust_pins_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
        ):
            os.close(descriptor)
            self._metadata_fd = None
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry linkage authority changed"
            )
        return metadata

    def _load_journal(self) -> tuple[RepeatabilityComparisonJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry journal is incomplete"
            )
        entries: list[RepeatabilityComparisonJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_COMPARISONS:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    RepeatabilityComparisonJournalEntry, line
                )
            except Exception:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: RepeatabilityComparisonJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError:
            # Remove any torn suffix so the committed chain stays readable; the
            # object it named remains an uncommitted remnant for later cleanup.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[RepeatabilityComparisonJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredComparisonObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise RepeatabilityComparisonRegistryUnsafe("D07 comparison registry is closed")
        journal = _CR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_COMPARISONS + 1:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredComparisonObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry journal binding is invalid"
                )
            try:
                value = registered_comparison_object_from_bytes(content)
            except ValueError:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry object is invalid"
                ) from None
            loaded[entry.object_sha256] = (value, content)
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _CR_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry state rollback detected"
                )
        return loaded, head

    def _compare_in_fence(
        self,
        value: RegisteredComparisonObject,
        evaluated_at: datetime,
    ) -> RepeatabilityComparison:
        """Run the pinned already-fenced D07 evaluator on exact stored inputs."""

        return _PINNED_COMPARE_IN_FENCE(
            value.anchor,
            value.member,
            value.policy,
            value.decision,
            value.anchor_observation,
            value.member_observation,
            value.envelope,
            evaluated_at=evaluated_at,
            expected_policy_sha256=value.expected_policy_sha256,
            expected_authority_head_sha256=value.expected_authority_head_sha256,
            expected_linkage_trust_snapshot_sha256_by_provider=dict(self._trust_pins),
            linkage_store=self._linkage_store,
            result_trust_document=self._result_trust_document,
            expected_result_trust_sha256=self._result_trust_sha256,
            expected_envelope_sha256=value.expected_envelope_sha256,
            expected_evidence_sha256=value.expected_evidence_sha256,
            expected_protocol_sha256=value.expected_protocol_sha256,
            expected_repeatability_authority_sha256=(
                value.expected_repeatability_authority_sha256
            ),
        )

    def _live_time_in_fence(self) -> datetime:
        """Advance and read the linkage store's authority clock under the fence."""

        try:
            _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
        except ProviderLinkageStoreConflict:
            # Expired or otherwise non-current linkage authority is a stale
            # authority state, not a storage failure.
            raise RepeatabilityComparisonRegistryStale(
                "D07 comparison linkage authority is not current"
            ) from None
        return _PINNED_AUTHORITY_TIME_IN_FENCE(self._linkage_store)

    def _replay_in_fence(
        self, value: RegisteredComparisonObject, live_time: datetime
    ) -> RepeatabilityComparison:
        """Return the stored comparison only if it is exactly reproduced now.

        The pinned D07 evaluator runs once on the stored inputs under the
        caller's held fence, at the live authority time.  Its output, with only
        ``evaluated_at`` set back to the stored registration time, must be
        byte-identical to the stored comparison.  Every other field must match,
        so an envelope that expired or became valid since registration, a D03
        decision that no longer replays, or changed result trust makes the
        comparison stale rather than current.
        """

        stale = RepeatabilityComparisonRegistryStale(
            "D07 comparison no longer replays against live authority"
        )
        try:
            stored_sha256 = _CR_COMPARISON_SHA256(value.comparison)
            registered_time = value.comparison.evaluated_at
            if live_time < registered_time:
                raise stale
            at_live_time = _CR_COMPARE_IN_FENCE(self, value, live_time)
            if (
                _CR_COMPARISON_SHA256(
                    at_live_time.model_copy(update={"evaluated_at": registered_time})
                )
                != stored_sha256
            ):
                raise stale
        except (
            RepeatabilityComparisonRegistryStale,
            RepeatabilityComparisonRegistryUnsafe,
            ProviderLinkageStoreUnsafe,
            ProviderLinkageStoreSchemaError,
        ):
            # Storage and process-integrity failures keep their own type; only
            # a changed authority outcome is reported as stale.
            raise
        except Exception:
            raise RepeatabilityComparisonRegistryStale(
                "D07 comparison no longer replays against live authority"
            ) from None
        return value.comparison

    def register_comparison(
        self,
        anchor: LongitudinalRecord,
        member: LongitudinalRecord,
        policy: LongitudinalAnchorPolicy,
        anchor_observation: ComparisonObservation,
        member_observation: ComparisonObservation,
        envelope: RepeatabilityEnvelope,
        *,
        expected_policy_sha256: str,
        expected_authority_head_sha256: str,
        expected_envelope_sha256: str,
        expected_evidence_sha256: str,
        expected_protocol_sha256: str,
        expected_repeatability_authority_sha256: str,
    ) -> ComparisonRegistrationReceipt:
        """Derive one D07 comparison under live authority and publish it immutably.

        The caller supplies exact records, policy, signed observations, envelope,
        and pins only.  The D03 member decision, the evaluation time, and the
        comparison are always derived here under one held linkage fence, so a
        caller cannot register a comparison, a decision, or a time that D07 and
        D03 did not produce for these inputs.
        """

        _require_registry_integrity(self)
        if not all(
            _is_sha256(item)
            for item in (
                expected_policy_sha256,
                expected_authority_head_sha256,
                expected_envelope_sha256,
                expected_evidence_sha256,
                expected_protocol_sha256,
                expected_repeatability_authority_sha256,
            )
        ):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registration pins are invalid"
            )
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            evaluated_at = _CR_LIVE_TIME_IN_FENCE(self)
            decision = _PINNED_DECIDE_MEMBER(
                anchor,
                member,
                policy,
                expected_policy_sha256=expected_policy_sha256,
                expected_authority_head_sha256=expected_authority_head_sha256,
                expected_linkage_trust_snapshot_sha256_by_provider=dict(
                    self._trust_pins
                ),
                linkage_store=self._linkage_store,
            )
            try:
                decision_bytes = canonical_contract_bytes(decision)
            except Exception:
                raise RepeatabilityComparisonRegistryConflict(
                    "D07 comparison inputs are not a valid D03 member pair"
                ) from None
            if decision_bytes == _PINNED_INVALID_MEMBER_BYTES:
                raise RepeatabilityComparisonRegistryConflict(
                    "D07 comparison inputs are not a valid D03 member pair"
                )
            try:
                provisional = RegisteredComparisonObject.model_construct(
                    anchor=anchor,
                    member=member,
                    policy=policy,
                    expected_policy_sha256=expected_policy_sha256,
                    expected_authority_head_sha256=expected_authority_head_sha256,
                    decision=decision,
                    anchor_observation=anchor_observation,
                    member_observation=member_observation,
                    envelope=envelope,
                    expected_envelope_sha256=expected_envelope_sha256,
                    expected_evidence_sha256=expected_evidence_sha256,
                    expected_protocol_sha256=expected_protocol_sha256,
                    expected_repeatability_authority_sha256=(
                        expected_repeatability_authority_sha256
                    ),
                )
                comparison = _CR_COMPARE_IN_FENCE(self, provisional, evaluated_at)
                captured = RegisteredComparisonObject(
                    anchor=anchor,
                    member=member,
                    policy=policy,
                    expected_policy_sha256=expected_policy_sha256,
                    expected_authority_head_sha256=expected_authority_head_sha256,
                    decision=decision,
                    anchor_observation=anchor_observation,
                    member_observation=member_observation,
                    envelope=envelope,
                    expected_envelope_sha256=expected_envelope_sha256,
                    expected_evidence_sha256=expected_evidence_sha256,
                    expected_protocol_sha256=expected_protocol_sha256,
                    expected_repeatability_authority_sha256=(
                        expected_repeatability_authority_sha256
                    ),
                    comparison=comparison,
                )
                content = registered_comparison_object_bytes(captured)
                captured = registered_comparison_object_from_bytes(content)
            except (ProviderLinkageStoreUnsafe, ProviderLinkageStoreSchemaError):
                raise
            except Exception:
                raise RepeatabilityComparisonRegistryConflict(
                    "D07 comparison inputs are not exact replayable contracts"
                ) from None
            digest = hashlib.sha256(content).hexdigest()
            # The stored bytes, not the caller's objects, must reproduce the
            # comparison before anything is published.
            replayed = _CR_REPLAY_IN_FENCE(self, captured, evaluated_at)
            with _CR_LOCK(self, exclusive=True):
                _CR_RECOVER_TEMPORARY_OBJECTS(self)
                loaded, head = _CR_LOAD_STATE(self)
                assert self._objects_fd is not None
                # The journal is the commit point: an object without an entry is
                # the remnant of an interrupted registration and is never adopted
                # unless its exact bytes are being registered again.
                for name in os.listdir(self._objects_fd):
                    if name[:64] not in loaded and name != f"{digest}.json":
                        _read_exact_object(self._objects_fd, name[:64])
                        os.unlink(name, dir_fd=self._objects_fd)
                os.fsync(self._objects_fd)
                if digest in loaded:
                    if loaded[digest][1] != content:
                        raise RepeatabilityComparisonRegistryConflict(
                            "D07 comparison object digest conflicts"
                        )
                else:
                    if len(loaded) >= MAX_REGISTERED_COMPARISONS:
                        raise RepeatabilityComparisonRegistryConflict(
                            "D07 comparison registry is full"
                        )
                    if (
                        sum(len(item[1]) for item in loaded.values()) + len(content)
                        > MAX_TOTAL_OBJECT_BYTES
                    ):
                        raise RepeatabilityComparisonRegistryConflict(
                            "D07 comparison registry byte bound would be exceeded"
                        )
                    try:
                        _CR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                    except FileExistsError:
                        if _read_exact_object(self._objects_fd, digest) != content:
                            raise RepeatabilityComparisonRegistryConflict(
                                "D07 comparison publication conflicts"
                            ) from None
                    _CR_APPEND_JOURNAL(
                        self,
                        _build_journal_entry(
                            sequence=len(loaded) + 1,
                            previous_entry_sha256=head,
                            object_sha256=digest,
                            object_bytes=len(content),
                        ),
                    )
                final, final_head = _CR_LOAD_STATE(self)
                if digest not in final or final[digest][1] != content:
                    raise RepeatabilityComparisonRegistryUnsafe(
                        "D07 comparison publication is unproven"
                    )
                return _CR_RECEIPT(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(final),
                    state_head_sha256=final_head,
                    selector_id=_CR_SELECTOR_ID(
                        self._metadata.registry_epoch_sha256, digest
                    ),
                    object_sha256=digest,
                    comparison_sha256=_CR_COMPARISON_SHA256(replayed),
                    availability=replayed.availability,
                    classification=replayed.classification,
                )

    def resolve(self, selector_id: str) -> RegisteredRepeatabilityComparison:
        """Return one registered comparison only after it replays exactly now."""

        _require_registry_integrity(self)
        if not _is_selector(selector_id):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison selector is invalid"
            )
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _CR_LOCK(self, exclusive=False):
                loaded, head = _CR_LOAD_STATE(self)
                matches = [
                    (digest, value)
                    for digest, (value, _) in loaded.items()
                    if _CR_SELECTOR_ID(self._metadata.registry_epoch_sha256, digest)
                    == selector_id
                ]
                if len(matches) != 1:
                    raise RepeatabilityComparisonRegistryConflict(
                        "D07 comparison selector is unavailable"
                    )
                digest, value = matches[0]
                live_time = _CR_LIVE_TIME_IN_FENCE(self)
                comparison = _CR_REPLAY_IN_FENCE(self, value, live_time)
                return _CR_RESOLVED(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    selector_id=selector_id,
                    object_sha256=digest,
                    comparison_sha256=_CR_COMPARISON_SHA256(comparison),
                    comparison=comparison,
                    replayed_at=live_time,
                )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        limit: int = 50,
    ) -> ComparisonSelectorPage:
        """Return one bounded privacy-safe page with live authority state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison selector page bound is invalid"
            )
        if after_selector_id is not None and not _is_selector(after_selector_id):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison selector cursor is invalid"
            )
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _CR_LOCK(self, exclusive=False):
                loaded, head = _CR_LOAD_STATE(self)
                ordered = sorted(
                    (
                        _CR_SELECTOR_ID(self._metadata.registry_epoch_sha256, digest),
                        digest,
                        value,
                    )
                    for digest, (value, _) in loaded.items()
                )
                if after_selector_id is not None:
                    ordered = [item for item in ordered if item[0] > after_selector_id]
                selected = ordered[:limit]
                live_time: datetime | None = None
                if selected:
                    try:
                        live_time = _CR_LIVE_TIME_IN_FENCE(self)
                    except RepeatabilityComparisonRegistryStale:
                        live_time = None
                rows: list[ComparisonSelectorRecord] = []
                for selector_id, digest, value in selected:
                    try:
                        if live_time is None:
                            raise RepeatabilityComparisonRegistryStale(
                                "D07 comparison linkage authority is not current"
                            )
                        _CR_REPLAY_IN_FENCE(self, value, live_time)
                    except RepeatabilityComparisonRegistryStale:
                        authority_state = ComparisonAuthorityState.STALE
                    else:
                        authority_state = ComparisonAuthorityState.CURRENT
                    comparison = value.comparison
                    envelope_sha256 = comparison.repeatability_envelope_sha256
                    assert envelope_sha256 is not None
                    rows.append(
                        _CR_SELECTOR_RECORD(
                            selector_id=selector_id,
                            object_sha256=digest,
                            comparison_sha256=_CR_COMPARISON_SHA256(comparison),
                            d03_decision_sha256=comparison.d03_decision_sha256,
                            anchor_policy_sha256=comparison.anchor_policy_sha256,
                            repeatability_envelope_sha256=envelope_sha256,
                            d03_outcome=value.decision.outcome,
                            availability=comparison.availability,
                            classification=comparison.classification,
                            authority_state=authority_state,
                        )
                    )
                more = len(ordered) > len(selected)
                return _CR_SELECTOR_PAGE(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    records=tuple(rows),
                    next_after_selector_id=(rows[-1].selector_id if more and rows else None),
                )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _CR_LOCK(self, exclusive=False):
            loaded, head = _CR_LOAD_STATE(self)
            backup = RepeatabilityComparisonBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_CR_LOAD_JOURNAL(self),
                objects=tuple(
                    RepeatabilityComparisonBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise RepeatabilityComparisonRegistryConflict(
                    "D07 comparison registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        result_trust_document: DevelopmentTrustDocument,
        expected_result_trust_sha256: str,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> RepeatabilityComparisonRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("D07 comparison registry requires the exact linkage store type")
        backup = repeatability_comparison_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup expected head is invalid"
            )
        pins = capture_expected_trust_pins(expected_trust_snapshot_sha256_by_provider)
        snapshot = _PINNED_ACTIVE_SNAPSHOT(linkage_store)
        if snapshot.trust_pins_sha256 != trust_pins_sha256(pins) or (
            backup.metadata.linkage_store_id,
            backup.metadata.linkage_store_epoch_sha256,
            backup.metadata.linkage_storage_identity_sha256,
            backup.metadata.linkage_trust_pins_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
        ):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup authority is invalid"
            )
        try:
            observed_result_trust_sha256 = _PINNED_RESULT_TRUST_SHA256(
                result_trust_document
            )
        except Exception:
            observed_result_trust_sha256 = None
        if (
            not _is_sha256(expected_result_trust_sha256)
            or observed_result_trust_sha256 != expected_result_trust_sha256
        ):
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry backup result trust is invalid"
            )
        target = _snapshot_path(root)
        parent = target.parent
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd: int | None = None
        root_fd: int | None = None
        objects_fd: int | None = None
        created = False
        completed = False
        try:
            parent_lstat = os.stat(parent, follow_symlinks=False)
            parent_fd = os.open(parent, directory_flags)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry restore parent changed"
                )
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            created = True
            root_lstat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(target.name, directory_flags, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or (root_lstat.st_dev, root_lstat.st_ino)
                != (root_bound.st_dev, root_bound.st_ino)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry restore root changed"
                )
            os.mkdir("objects", 0o700, dir_fd=root_fd)
            objects_lstat = os.stat("objects", dir_fd=root_fd, follow_symlinks=False)
            objects_fd = os.open("objects", directory_flags, dir_fd=root_fd)
            objects_bound = os.fstat(objects_fd)
            if (
                not stat.S_ISDIR(objects_lstat.st_mode)
                or (objects_lstat.st_dev, objects_lstat.st_ino)
                != (objects_bound.st_dev, objects_bound.st_ino)
                or stat.S_IMODE(objects_bound.st_mode) != 0o700
                or objects_bound.st_uid != os.geteuid()
            ):
                raise RepeatabilityComparisonRegistryUnsafe(
                    "D07 comparison registry restore objects changed"
                )
            lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            os.close(lock_fd)
            _publish_file(
                root_fd,
                "registry-metadata.json",
                canonical_contract_bytes(backup.metadata),
            )
            for item in backup.objects:
                _publish_file(
                    objects_fd,
                    f"{item.object_sha256}.json",
                    item.object_json.encode("utf-8"),
                )
            _publish_file(
                root_fd,
                "registry-journal.jsonl",
                b"".join(
                    canonical_contract_bytes(entry) + b"\n" for entry in backup.journal
                ),
            )
            os.fsync(objects_fd)
            os.fsync(root_fd)
            os.fsync(parent_fd)
            completed = True
        except FileExistsError:
            raise RepeatabilityComparisonRegistryConflict(
                "D07 comparison registry restore target already exists"
            ) from None
        except OSError:
            raise RepeatabilityComparisonRegistryUnsafe(
                "D07 comparison registry restore failed"
            ) from None
        finally:
            if created and not completed:
                if root_fd is not None:
                    _remove_partial_restore(
                        parent_fd, target.name, root_fd, objects_fd
                    )
                elif parent_fd is not None:
                    try:
                        os.rmdir(target.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            for descriptor in (objects_fd, root_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return _CR_CONSTRUCT(
            target,
            linkage_store=linkage_store,
            expected_trust_snapshot_sha256_by_provider=pins,
            result_trust_document=result_trust_document,
            expected_result_trust_sha256=expected_result_trust_sha256,
            expected_registry_id=expected_registry_id,
            expected_registry_epoch_sha256=expected_registry_epoch_sha256,
            expected_state_head_sha256=expected_state_head_sha256,
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: RepeatabilityComparisonRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_lock",
            "_validate_storage",
            "_publish",
            "_recover_temporary_objects",
            "_load_or_create_metadata",
            "_load_journal",
            "_append_journal",
            "_accept_observed_head",
            "_load_state",
            "_compare_in_fence",
            "_live_time_in_fence",
            "_replay_in_fence",
            "register_comparison",
            "resolve",
            "list_selectors",
            "backup_bytes",
            "restore",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not RepeatabilityComparisonRegistry or any(
        RepeatabilityComparisonRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry callable changed"
        )


def _require_registry_integrity(registry: RepeatabilityComparisonRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry callable changed"
        )
    authority_sources = {
        "_PINNED_AUTHORITY_READ_FENCE": ProviderLinkageStore.authority_read_fence,
        "_PINNED_ACTIVE_SNAPSHOT": ProviderLinkageStore.active_snapshot,
        "_PINNED_AUTHORITY_TIME_IN_FENCE": ProviderLinkageStore.authority_time_in_fence,
        "_PINNED_DECIDE_MEMBER": d03_module.decide_longitudinal_member,
        "_PINNED_INVALID_MEMBER_BYTES": (
            d03_module._INVALID_INPUT_MEMBER_DECISION_BYTES
        ),
        "_PINNED_COMPARE_IN_FENCE": d07_module.compare_repeatability_in_fence,
        "_PINNED_COMPARISON_SHA256": d07_module.repeatability_comparison_sha256,
        "_PINNED_RESULT_TRUST_SHA256": d07_module.result_trust_document_sha256,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority callable changed"
        )
    instance = object.__getattribute__(registry, "__dict__")
    initialized_names = (
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    initialized = tuple(name in instance for name in initialized_names)
    if not any(initialized):
        return
    if not all(initialized):
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise RepeatabilityComparisonRegistryUnsafe(
            "D07 comparison registry authority state changed"
        )


_CR_CONSTRUCT = RepeatabilityComparisonRegistry
_CR_CLOSE = RepeatabilityComparisonRegistry.close
_CR_LOCK = RepeatabilityComparisonRegistry._lock
_CR_VALIDATE_STORAGE = RepeatabilityComparisonRegistry._validate_storage
_CR_PUBLISH = RepeatabilityComparisonRegistry._publish
_CR_RECOVER_TEMPORARY_OBJECTS = (
    RepeatabilityComparisonRegistry._recover_temporary_objects
)
_CR_LOAD_OR_CREATE_METADATA = RepeatabilityComparisonRegistry._load_or_create_metadata
_CR_LOAD_JOURNAL = RepeatabilityComparisonRegistry._load_journal
_CR_APPEND_JOURNAL = RepeatabilityComparisonRegistry._append_journal
_CR_ACCEPT_OBSERVED_HEAD = RepeatabilityComparisonRegistry._accept_observed_head
_CR_LOAD_STATE = RepeatabilityComparisonRegistry._load_state
_CR_COMPARE_IN_FENCE = RepeatabilityComparisonRegistry._compare_in_fence
_CR_LIVE_TIME_IN_FENCE = RepeatabilityComparisonRegistry._live_time_in_fence
_CR_REPLAY_IN_FENCE = RepeatabilityComparisonRegistry._replay_in_fence
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another comparison.
_CR_RECEIPT = ComparisonRegistrationReceipt
_CR_RESOLVED = RegisteredRepeatabilityComparison
_CR_SELECTOR_RECORD = ComparisonSelectorRecord
_CR_SELECTOR_PAGE = ComparisonSelectorPage
_CR_SELECTOR_ID = _selector_id
_CR_COMPARISON_SHA256 = _comparison_sha256
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_AUTHORITY_READ_FENCE": _PINNED_AUTHORITY_READ_FENCE,
        "_PINNED_ACTIVE_SNAPSHOT": _PINNED_ACTIVE_SNAPSHOT,
        "_PINNED_AUTHORITY_TIME_IN_FENCE": _PINNED_AUTHORITY_TIME_IN_FENCE,
        "_PINNED_DECIDE_MEMBER": _PINNED_DECIDE_MEMBER,
        "_PINNED_INVALID_MEMBER_BYTES": _PINNED_INVALID_MEMBER_BYTES,
        "_PINNED_COMPARE_IN_FENCE": _PINNED_COMPARE_IN_FENCE,
        "_PINNED_COMPARISON_SHA256": _PINNED_COMPARISON_SHA256,
        "_PINNED_RESULT_TRUST_SHA256": _PINNED_RESULT_TRUST_SHA256,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_CR_CONSTRUCT",
            "_CR_CLOSE",
            "_CR_LOCK",
            "_CR_VALIDATE_STORAGE",
            "_CR_PUBLISH",
            "_CR_RECOVER_TEMPORARY_OBJECTS",
            "_CR_LOAD_OR_CREATE_METADATA",
            "_CR_LOAD_JOURNAL",
            "_CR_APPEND_JOURNAL",
            "_CR_ACCEPT_OBSERVED_HEAD",
            "_CR_LOAD_STATE",
            "_CR_COMPARE_IN_FENCE",
            "_CR_LIVE_TIME_IN_FENCE",
            "_CR_REPLAY_IN_FENCE",
            "_CR_RECEIPT",
            "_CR_RESOLVED",
            "_CR_SELECTOR_RECORD",
            "_CR_SELECTOR_PAGE",
            "_CR_SELECTOR_ID",
            "_CR_COMPARISON_SHA256",
        )
    }
)


__all__ = [
    "ComparisonAuthorityState",
    "ComparisonRegistrationReceipt",
    "ComparisonSelectorPage",
    "ComparisonSelectorRecord",
    "RegisteredComparisonObject",
    "RegisteredRepeatabilityComparison",
    "RepeatabilityComparisonBackup",
    "RepeatabilityComparisonBackupObject",
    "RepeatabilityComparisonJournalEntry",
    "RepeatabilityComparisonRegistry",
    "RepeatabilityComparisonRegistryConflict",
    "RepeatabilityComparisonRegistryError",
    "RepeatabilityComparisonRegistryMetadata",
    "RepeatabilityComparisonRegistryStale",
    "RepeatabilityComparisonRegistryUnsafe",
    "registered_comparison_object_bytes",
    "registered_comparison_object_from_bytes",
    "repeatability_comparison_backup_from_bytes",
]
