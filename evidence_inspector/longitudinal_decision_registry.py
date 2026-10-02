"""Protected append-only registry for live-replayed D03 series decisions.

The registry is provider-local.  Callers never supply a decision: registration
evaluates the D03 series itself from exact records, policy, and pins while the
linkage authority fence is held, and stores those inputs beside the decision.
Every protected read replays the stored decision against the live linkage
store before returning it.  The separate selector projection carries only
opaque selectors, digests, counts, and authority state.
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
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.longitudinal_compatibility as d03_module
from evidence_inspector.cohort_manifest import (
    capture_expected_trust_pins,
    trust_pins_sha256,
)
from evidence_inspector.longitudinal_compatibility import (
    MAX_SERIES_MEMBERS,
    LongitudinalAnchorPolicy,
    LongitudinalDecisionReplayError,
    LongitudinalOutcome,
    LongitudinalRecord,
    LongitudinalSeriesDecision,
    decide_longitudinal_series,
    longitudinal_anchor_policy_sha256,
    replay_longitudinal_series_decision,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.registry_storage import (
    begin_staged_root as _begin_staged_root,
    bound_name as _bound_name,
    commit_staged_root as _commit_staged_root,
    commit_staging_directory as _commit_staging_directory,
    discard_staged_root as _discard_staged_root,
    make_staging_directory as _make_staging_directory,
    recover_torn_journal_tail as _recover_torn_journal_tail,
    remove_owned_temporaries as _remove_owned_temporaries,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_REGISTERED_SERIES = 10_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 64
MAX_OBJECT_GRAPH_NODES = 4_000_000
MAX_OBJECT_COLLECTION_ITEMS = 2 * MAX_SERIES_MEMBERS + 2
MAX_OBJECT_STRING_BYTES = 1024 * 1024
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
_PINNED_DECIDE_SERIES = decide_longitudinal_series
_PINNED_REPLAY_SERIES = replay_longitudinal_series_decision
_PINNED_INVALID_SERIES_BYTES = d03_module._INVALID_INPUT_SERIES_DECISION_BYTES

RegistryId = Annotated[str, StringConstraints(pattern=r"^d03_registry_[0-9a-f]{32}$")]
SeriesSelectorId = Annotated[
    str, StringConstraints(pattern=r"^d03_series_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class LongitudinalDecisionRegistryError(RuntimeError):
    """Sanitized registry failure."""


class LongitudinalDecisionRegistryConflict(LongitudinalDecisionRegistryError):
    pass


class LongitudinalDecisionRegistryStale(LongitudinalDecisionRegistryConflict):
    """A registered decision no longer replays against live authority."""


class LongitudinalDecisionRegistryUnsafe(LongitudinalDecisionRegistryError):
    pass


class SeriesAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class LongitudinalDecisionRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.d03-decision-registry-metadata.v1"] = (
        "traceback.d03-decision-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_trust_pins_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    LongitudinalDecisionRegistryMetadata
)


class RegisteredSeriesObject(RegistryContract):
    """Protected stored inputs plus the decision the registry derived from them."""

    schema_version: Literal["traceback.d03-registered-series-object.v1"] = (
        "traceback.d03-registered-series-object.v1"
    )
    anchor: LongitudinalRecord
    members: tuple[LongitudinalRecord, ...] = Field(
        min_length=1, max_length=MAX_SERIES_MEMBERS
    )
    policy: LongitudinalAnchorPolicy
    expected_policy_sha256: Sha256
    expected_authority_head_sha256: Sha256
    decision: LongitudinalSeriesDecision

    @model_validator(mode="after")
    def exact_bindings(self) -> RegisteredSeriesObject:
        if self.decision.anchor_result_id != self.anchor.measurement.result_id:
            raise ValueError("registered series anchor does not match its decision")
        if self.decision.member_result_ids != tuple(
            sorted(member.measurement.result_id for member in self.members)
        ):
            raise ValueError("registered series members do not match its decision")
        if self.decision.policy_sha256 != longitudinal_anchor_policy_sha256(
            self.policy
        ):
            raise ValueError("registered series policy does not match its decision")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(RegisteredSeriesObject)


class LongitudinalDecisionJournalEntry(RegistryContract):
    schema_version: Literal["traceback.d03-decision-journal-entry.v1"] = (
        "traceback.d03-decision-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_SERIES, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class SeriesRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.d03-series-registration-receipt.v1"] = (
        "traceback.d03-series-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_SERIES)
    state_head_sha256: Sha256
    selector_id: SeriesSelectorId
    object_sha256: Sha256
    decision_sha256: Sha256

    @model_validator(mode="after")
    def exact_selector(self) -> SeriesRegistrationReceipt:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("series receipt selector does not match its object")
        return self


class RegisteredLongitudinalSeriesDecision(RegistryContract):
    """Protected decision that replayed exactly against live linkage authority."""

    schema_version: Literal["traceback.d03-registered-series-decision.v1"] = (
        "traceback.d03-registered-series-decision.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_SERIES)
    state_head_sha256: Sha256
    selector_id: SeriesSelectorId
    object_sha256: Sha256
    decision_sha256: Sha256
    decision: LongitudinalSeriesDecision
    replayed_against_live_linkage: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredLongitudinalSeriesDecision:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("registered series selector does not match its object")
        if self.decision_sha256 != _series_decision_sha256(self.decision):
            raise ValueError("registered series decision digest is invalid")
        return self


class SeriesOutcomeCount(RegistryContract):
    outcome: LongitudinalOutcome
    count: int = Field(ge=0, le=MAX_SERIES_MEMBERS, strict=True)


class SeriesSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.d03-series-selector-record.v1"] = (
        "traceback.d03-series-selector-record.v1"
    )
    selector_id: SeriesSelectorId
    object_sha256: Sha256
    decision_sha256: Sha256
    policy_sha256: Sha256
    anchor_key_sha256: Sha256
    authority_state: SeriesAuthorityState
    member_count: int = Field(ge=1, le=MAX_SERIES_MEMBERS, strict=True)
    outcome_counts: tuple[SeriesOutcomeCount, ...] = Field(
        min_length=len(LongitudinalOutcome), max_length=len(LongitudinalOutcome)
    )
    delta_allowed_count: int = Field(ge=0, le=MAX_SERIES_MEMBERS, strict=True)

    @model_validator(mode="after")
    def exact_counts(self) -> SeriesSelectorRecord:
        if tuple(item.outcome for item in self.outcome_counts) != tuple(
            LongitudinalOutcome
        ):
            raise ValueError("series outcome counts must use canonical order")
        if sum(item.count for item in self.outcome_counts) != self.member_count:
            raise ValueError("series outcome counts must reconcile")
        if self.delta_allowed_count > self.member_count:
            raise ValueError("series delta count exceeds membership")
        return self


class SeriesSelectorPage(RegistryContract):
    schema_version: Literal["traceback.d03-series-selector-page.v1"] = (
        "traceback.d03-series-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_SERIES)
    state_head_sha256: Sha256
    records: tuple[SeriesSelectorRecord, ...] = Field(max_length=MAX_SELECTOR_PAGE)
    next_after_selector_id: SeriesSelectorId | None


class LongitudinalDecisionBackupObject(RegistryContract):
    schema_version: Literal["traceback.d03-decision-backup-object.v1"] = (
        "traceback.d03-decision-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class LongitudinalDecisionBackup(RegistryContract):
    schema_version: Literal["traceback.d03-decision-backup.v1"] = (
        "traceback.d03-decision-backup.v1"
    )
    metadata: LongitudinalDecisionRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_SERIES)
    state_head_sha256: Sha256
    journal: tuple[LongitudinalDecisionJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_SERIES
    )
    objects: tuple[LongitudinalDecisionBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_SERIES
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(
    LongitudinalDecisionBackup
)


def registered_series_object_bytes(value: RegisteredSeriesObject) -> bytes:
    """Return exact bounded canonical bytes for one stored series object."""

    return exact_model_bytes(
        value,
        RegisteredSeriesObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_series_object_from_bytes(content: bytes) -> RegisteredSeriesObject:
    try:
        # The bounded parse enforces every structural budget before validation;
        # strict D03 contracts then validate in JSON mode from the same bytes.
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredSeriesObject.model_validate_json(content)
        if registered_series_object_bytes(value) != content:
            raise ValueError("registered series object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered series object is not canonical") from None


def _canonical_backup_bytes(backup: LongitudinalDecisionBackup) -> bytes:
    return exact_model_bytes(
        backup,
        LongitudinalDecisionBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_SERIES,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("D03 decision registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("D03 decision registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "D03 decision registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("D03 decision registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("D03 decision registry path is invalid")
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry object exceeds its bound"
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry object digest is invalid"
        )
    return content


def _selector_id(epoch: str, object_sha256: str) -> str:
    digest = hashlib.sha256(
        b"traceback-d03-series-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + object_sha256.encode("ascii")
    ).hexdigest()
    return f"d03_series_{digest[:40]}"


def _series_decision_sha256(decision: LongitudinalSeriesDecision) -> str:
    return hashlib.sha256(canonical_contract_bytes(decision)).hexdigest()


def _journal_entry_sha256(entry: LongitudinalDecisionJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-d03-decision-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: LongitudinalDecisionRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-d03-decision-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> LongitudinalDecisionJournalEntry:
    placeholder = LongitudinalDecisionJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return LongitudinalDecisionJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_backup(backup: LongitudinalDecisionBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            registered_series_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup digest is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup state is invalid"
        )


def longitudinal_decision_backup_from_bytes(
    content: bytes,
) -> LongitudinalDecisionBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_SERIES,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = LongitudinalDecisionBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("D03 decision registry backup is not canonical")
    except (TypeError, ValueError):
        raise LongitudinalDecisionRegistryConflict(
            "D03 decision registry backup is invalid"
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


def _outcome_counts(
    decision: LongitudinalSeriesDecision,
) -> tuple[SeriesOutcomeCount, ...]:
    return tuple(
        SeriesOutcomeCount(
            outcome=outcome,
            count=sum(item.outcome is outcome for item in decision.decisions),
        )
        for outcome in LongitudinalOutcome
    )


def _registry_instance_snapshot(
    registry: LongitudinalDecisionRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_linkage_store",
        "_trust_pins",
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
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            LongitudinalDecisionRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry authority state changed"
            )
    pins = instance["_trust_pins"]
    if type(pins) is not dict:
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        )
    try:
        captured_pins = capture_expected_trust_pins(pins)
    except (TypeError, ValueError):
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        ) from None
    return (
        id(instance["root"]),
        id(instance["_linkage_store"]),
        tuple(sorted(captured_pins.items())),
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


def _seal_registry_instance(registry: LongitudinalDecisionRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class LongitudinalDecisionRegistry:
    """Descriptor-relative immutable D03 series publication with live replay."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "backup_bytes",
            "close",
            "list_selectors",
            "register_series",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("D03 decision registry requires the exact linkage store type")
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
                or not expected_registry_id.startswith("d03_registry_")
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_registry_id[13:]
                )
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry expected identity or head is invalid"
                )
        self.root = _snapshot_path(root)
        self._linkage_store = linkage_store
        self._trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        if _PINNED_ACTIVE_SNAPSHOT(
            self._linkage_store
        ).trust_pins_sha256 != trust_pins_sha256(self._trust_pins):
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry trust pins are invalid"
            )
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        final_root = self.root
        staged_root: Path | None = None
        try:
            # A new root is built in a hidden sibling and published with one
            # rename, so an interrupted creation never leaves a half-built
            # root at the final path.
            staged_root = _begin_staged_root(final_root)
            root_created = staged_root is not None
            if staged_root is not None:
                self.root = staged_root
            root_lstat = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or stat.S_IMODE(root_lstat.st_mode) != 0o700
                or root_lstat.st_uid != os.geteuid()
            ):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry root must be private"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry root changed"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry objects are unsafe"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry lock is unsafe"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
                with _DR_LOCK(self, exclusive=True):
                    self._metadata = _DR_LOAD_OR_CREATE_METADATA(
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
                    _DR_RECOVER_TEMPORARY_OBJECTS(self)
                    _, head = _DR_LOAD_STATE(self, check_trusted_head=False)
                    if root_created:
                        if any(item is not None for item in expected_values):
                            raise LongitudinalDecisionRegistryUnsafe(
                                "new D03 decision registry cannot inherit an "
                                "expected identity"
                            )
                    elif any(item is None for item in expected_values):
                        raise LongitudinalDecisionRegistryUnsafe(
                            "D03 decision registry expected identity and head are "
                            "required"
                        )
                    if not root_created and expected_values != (
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                        head,
                    ):
                        raise LongitudinalDecisionRegistryUnsafe(
                            "D03 decision registry expected identity or head is invalid"
                        )
                    if staged_root is not None:
                        _commit_staged_root(staged_root, final_root, self._root_fd)
                        self.root = final_root
                    self._trusted_head_sha256 = head
                    _DR_ACCEPT_OBSERVED_HEAD(
                        self, _DR_LOAD_JOURNAL(self), head, check_instance=False
                    )
                    _seal_registry_instance(self)
                    # Until here a failure still removes the new root by inode.
                    staged_root = None
        except BaseException:
            if staged_root is not None:
                _discard_staged_root(staged_root, final_root, self._root_fd)
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

    def __enter__(self) -> LongitudinalDecisionRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _DR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _DR_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry is closed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _DR_VALIDATE_STORAGE(self)
                yield
                _DR_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise LongitudinalDecisionRegistryUnsafe("D03 decision registry is closed")
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry storage changed"
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry storage changed"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry storage changed"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root or objects directory is always unlinked under the exclusive
        # lock; a directory under that name makes unlink fail, so recovery
        # fails closed.
        if self._root_fd is None or self._objects_fd is None:
            raise LongitudinalDecisionRegistryUnsafe("D03 decision registry is closed")
        try:
            for directory_fd in (self._root_fd, self._objects_fd):
                _remove_owned_temporaries(directory_fd)
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> LongitudinalDecisionRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry metadata is missing"
                ) from None
            snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
            metadata = LongitudinalDecisionRegistryMetadata(
                registry_id=f"d03_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                linkage_store_id=snapshot.store_id,
                linkage_store_epoch_sha256=snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=snapshot.storage_identity_sha256,
                linkage_trust_pins_sha256=snapshot.trust_pins_sha256,
            )
            try:
                _DR_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _DR_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                LongitudinalDecisionRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry metadata is invalid"
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
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry linkage authority changed"
            )
        return metadata

    def _load_journal(self) -> tuple[LongitudinalDecisionJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise LongitudinalDecisionRegistryUnsafe("D03 decision registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry journal is incomplete"
            )
        entries: list[LongitudinalDecisionJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_SERIES:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    LongitudinalDecisionJournalEntry, line
                )
            except Exception:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: LongitudinalDecisionJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise LongitudinalDecisionRegistryUnsafe("D03 decision registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException as error:
            # Remove any torn suffix so the committed chain stays readable; the
            # object it named remains an uncommitted remnant for later cleanup.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            # An interrupt or other non-OS failure keeps its own type.
            if not isinstance(error, OSError):
                raise
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[LongitudinalDecisionJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredSeriesObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise LongitudinalDecisionRegistryUnsafe("D03 decision registry is closed")
        journal = _DR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_SERIES + 1:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredSeriesObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry journal binding is invalid"
                )
            try:
                value = registered_series_object_from_bytes(content)
            except ValueError:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry object is invalid"
                ) from None
            loaded[entry.object_sha256] = (value, content)
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _DR_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry state rollback detected"
                )
        return loaded, head

    def _replay_in_fence(self, value: RegisteredSeriesObject) -> LongitudinalSeriesDecision:
        try:
            return _PINNED_REPLAY_SERIES(
                value.decision,
                value.anchor,
                value.members,
                value.policy,
                expected_policy_sha256=value.expected_policy_sha256,
                expected_authority_head_sha256=value.expected_authority_head_sha256,
                expected_linkage_trust_snapshot_sha256_by_provider=dict(
                    self._trust_pins
                ),
                linkage_store=self._linkage_store,
            )
        except LongitudinalDecisionReplayError:
            raise LongitudinalDecisionRegistryStale(
                "D03 series decision no longer replays against live authority"
            ) from None
        except Exception:
            raise LongitudinalDecisionRegistryStale(
                "D03 series decision no longer replays against live authority"
            ) from None

    def register_series(
        self,
        anchor: LongitudinalRecord,
        members: tuple[LongitudinalRecord, ...],
        policy: LongitudinalAnchorPolicy,
        *,
        expected_policy_sha256: str,
        expected_authority_head_sha256: str,
    ) -> SeriesRegistrationReceipt:
        """Evaluate one D03 series under live authority and publish it immutably.

        The caller supplies exact records, policy, and pins only.  The decision is
        always derived here, so a caller cannot register a decision D03 did not
        produce for these inputs.
        """

        _require_registry_integrity(self)
        if not _is_sha256(expected_policy_sha256) or not _is_sha256(
            expected_authority_head_sha256
        ):
            raise LongitudinalDecisionRegistryConflict(
                "D03 series registration pins are invalid"
            )
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            decision = _PINNED_DECIDE_SERIES(
                anchor,
                members,
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
                raise LongitudinalDecisionRegistryConflict(
                    "D03 series inputs are not a valid series"
                ) from None
            if decision_bytes == _PINNED_INVALID_SERIES_BYTES:
                raise LongitudinalDecisionRegistryConflict(
                    "D03 series inputs are not a valid series"
                )
            try:
                captured = RegisteredSeriesObject(
                    anchor=anchor,
                    members=members,
                    policy=policy,
                    expected_policy_sha256=expected_policy_sha256,
                    expected_authority_head_sha256=expected_authority_head_sha256,
                    decision=decision,
                )
                content = registered_series_object_bytes(captured)
                captured = registered_series_object_from_bytes(content)
            except Exception:
                raise LongitudinalDecisionRegistryConflict(
                    "D03 series inputs are not exact canonical contracts"
                ) from None
            digest = hashlib.sha256(content).hexdigest()
            _DR_REPLAY_IN_FENCE(self, captured)
            with _DR_LOCK(self, exclusive=True):
                _DR_RECOVER_TEMPORARY_OBJECTS(self)
                loaded, head = _DR_LOAD_STATE(self)
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
                        raise LongitudinalDecisionRegistryConflict(
                            "D03 series object digest conflicts"
                        )
                else:
                    if len(loaded) >= MAX_REGISTERED_SERIES:
                        raise LongitudinalDecisionRegistryConflict(
                            "D03 decision registry is full"
                        )
                    if (
                        sum(len(item[1]) for item in loaded.values()) + len(content)
                        > MAX_TOTAL_OBJECT_BYTES
                    ):
                        raise LongitudinalDecisionRegistryConflict(
                            "D03 decision registry byte bound would be exceeded"
                        )
                    try:
                        _DR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                    except FileExistsError:
                        if _read_exact_object(self._objects_fd, digest) != content:
                            raise LongitudinalDecisionRegistryConflict(
                                "D03 series publication conflicts"
                            ) from None
                    _DR_APPEND_JOURNAL(
                        self,
                        _build_journal_entry(
                            sequence=len(loaded) + 1,
                            previous_entry_sha256=head,
                            object_sha256=digest,
                            object_bytes=len(content),
                        ),
                    )
                final, final_head = _DR_LOAD_STATE(self)
                if digest not in final or final[digest][1] != content:
                    raise LongitudinalDecisionRegistryUnsafe(
                        "D03 series publication is unproven"
                    )
                return _DR_RECEIPT(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(final),
                    state_head_sha256=final_head,
                    selector_id=_DR_SELECTOR_ID(
                        self._metadata.registry_epoch_sha256, digest
                    ),
                    object_sha256=digest,
                    decision_sha256=_DR_DECISION_SHA256(captured.decision),
                )

    def resolve(self, selector_id: str) -> RegisteredLongitudinalSeriesDecision:
        """Return one registered decision only after it replays against live linkage."""

        _require_registry_integrity(self)
        if (
            type(selector_id) is not str
            or len(selector_id) != 51
            or not selector_id.startswith("d03_series_")
            or any(character not in "0123456789abcdef" for character in selector_id[11:])
        ):
            raise LongitudinalDecisionRegistryConflict("D03 series selector is invalid")
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _DR_LOCK(self, exclusive=False):
                loaded, head = _DR_LOAD_STATE(self)
                matches = [
                    (digest, value)
                    for digest, (value, _) in loaded.items()
                    if _DR_SELECTOR_ID(self._metadata.registry_epoch_sha256, digest)
                    == selector_id
                ]
                if len(matches) != 1:
                    raise LongitudinalDecisionRegistryConflict(
                        "D03 series selector is unavailable"
                    )
                digest, value = matches[0]
                decision = _DR_REPLAY_IN_FENCE(self, value)
                return _DR_RESOLVED(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    selector_id=selector_id,
                    object_sha256=digest,
                    decision_sha256=_DR_DECISION_SHA256(decision),
                    decision=decision,
                )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        limit: int = 50,
    ) -> SeriesSelectorPage:
        """Return one bounded privacy-safe page with live authority state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise LongitudinalDecisionRegistryConflict(
                "D03 series selector page bound is invalid"
            )
        if after_selector_id is not None and (
            type(after_selector_id) is not str
            or len(after_selector_id) != 51
            or not after_selector_id.startswith("d03_series_")
            or any(
                character not in "0123456789abcdef"
                for character in after_selector_id[11:]
            )
        ):
            raise LongitudinalDecisionRegistryConflict(
                "D03 series selector cursor is invalid"
            )
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _DR_LOCK(self, exclusive=False):
                loaded, head = _DR_LOAD_STATE(self)
                ordered = sorted(
                    (
                        _DR_SELECTOR_ID(self._metadata.registry_epoch_sha256, digest),
                        digest,
                        value,
                    )
                    for digest, (value, _) in loaded.items()
                )
                if after_selector_id is not None:
                    ordered = [item for item in ordered if item[0] > after_selector_id]
                selected = ordered[:limit]
                rows: list[SeriesSelectorRecord] = []
                for selector_id, digest, value in selected:
                    try:
                        _DR_REPLAY_IN_FENCE(self, value)
                    except LongitudinalDecisionRegistryStale:
                        authority_state = SeriesAuthorityState.STALE
                    else:
                        authority_state = SeriesAuthorityState.CURRENT
                    rows.append(
                        _DR_SELECTOR_RECORD(
                            selector_id=selector_id,
                            object_sha256=digest,
                            decision_sha256=_DR_DECISION_SHA256(value.decision),
                            policy_sha256=value.decision.policy_sha256,
                            anchor_key_sha256=value.decision.anchor_key_sha256,
                            authority_state=authority_state,
                            member_count=len(value.decision.decisions),
                            outcome_counts=_outcome_counts(value.decision),
                            delta_allowed_count=sum(
                                item.delta_allowed for item in value.decision.decisions
                            ),
                        )
                    )
                more = len(ordered) > len(selected)
                return _DR_SELECTOR_PAGE(
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
        with _DR_LOCK(self, exclusive=False):
            loaded, head = _DR_LOAD_STATE(self)
            backup = LongitudinalDecisionBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_DR_LOAD_JOURNAL(self),
                objects=tuple(
                    LongitudinalDecisionBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise LongitudinalDecisionRegistryConflict(
                    "D03 decision registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> LongitudinalDecisionRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("D03 decision registry requires the exact linkage store type")
        backup = longitudinal_decision_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup expected head is invalid"
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
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry backup authority is invalid"
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
        staging_name = target.name
        completed = False
        try:
            parent_lstat = os.stat(parent, follow_symlinks=False)
            parent_fd = os.open(parent, directory_flags)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry restore parent changed"
                )
            staging_name = _make_staging_directory(parent_fd, target.name)
            created = True
            root_lstat = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or (root_lstat.st_dev, root_lstat.st_ino)
                != (root_bound.st_dev, root_bound.st_ino)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry restore root changed"
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
                raise LongitudinalDecisionRegistryUnsafe(
                    "D03 decision registry restore objects changed"
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
            # The staged root becomes the target only once it is complete.
            _commit_staging_directory(parent_fd, staging_name, target.name, root_fd)
            staging_name = target.name
            # Reopen through the normal checks before the restore counts as
            # complete, so a target that cannot open is removed, not left to
            # block a retry.
            restored = _DR_CONSTRUCT(
                target,
                linkage_store=linkage_store,
                expected_trust_snapshot_sha256_by_provider=pins,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise LongitudinalDecisionRegistryConflict(
                "D03 decision registry restore target already exists"
            ) from None
        except OSError:
            raise LongitudinalDecisionRegistryUnsafe(
                "D03 decision registry restore failed"
            ) from None
        finally:
            if (
                created
                and not completed
                and root_fd is not None
                and parent_fd is not None
            ):
                staging_name = _bound_name(
                    parent_fd, root_fd, staging_name, target.name
                )
            if created and not completed:
                if root_fd is not None:
                    _remove_partial_restore(
                        parent_fd, staging_name, root_fd, objects_fd
                    )
                elif parent_fd is not None:
                    try:
                        os.rmdir(staging_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            for descriptor in (objects_fd, root_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return restored

    @classmethod
    def recover_torn_journal_tail(
        cls,
        root: str | Path,
        *,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> int:
        """Operator maintenance: remove an unterminated trailing journal line.

        Reopening a registry whose journal ends in a torn line fails closed,
        and nothing repairs it automatically.  This explicit entry point takes
        the exclusive registry lock without waiting (a registry in use is
        refused) and truncates only the bytes after the
        last newline, and only when every complete line chains to exactly the
        retained head under the retained identity.  It returns the number of
        bytes removed (``0`` when there is no torn tail); then reopen with the
        same retained values.
        """

        _require_registry_class_integrity(cls)
        return _recover_torn_journal_tail(
            _snapshot_path(root),
            expected_registry_id=expected_registry_id,
            expected_registry_epoch_sha256=expected_registry_epoch_sha256,
            expected_state_head_sha256=expected_state_head_sha256,
            parse_metadata=lambda content: contract_from_canonical_bytes(
                LongitudinalDecisionRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                LongitudinalDecisionJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=4 * 1024 * 1024,
            max_entries=MAX_REGISTERED_SERIES,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=LongitudinalDecisionRegistryUnsafe,
            label="D03 decision registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: LongitudinalDecisionRegistry.__dict__[name]
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
            "_replay_in_fence",
            "register_series",
            "resolve",
            "list_selectors",
            "backup_bytes",
            "restore",
            "recover_torn_journal_tail",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not LongitudinalDecisionRegistry or any(
        LongitudinalDecisionRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry callable changed"
        )


def _require_registry_integrity(registry: LongitudinalDecisionRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry callable changed"
        )
    authority_sources = {
        "_PINNED_AUTHORITY_READ_FENCE": ProviderLinkageStore.authority_read_fence,
        "_PINNED_ACTIVE_SNAPSHOT": ProviderLinkageStore.active_snapshot,
        "_PINNED_DECIDE_SERIES": d03_module.decide_longitudinal_series,
        "_PINNED_REPLAY_SERIES": d03_module.replay_longitudinal_series_decision,
        "_PINNED_INVALID_SERIES_BYTES": (
            d03_module._INVALID_INPUT_SERIES_DECISION_BYTES
        ),
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority callable changed"
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
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise LongitudinalDecisionRegistryUnsafe(
            "D03 decision registry authority state changed"
        )


_DR_CONSTRUCT = LongitudinalDecisionRegistry
_DR_CLOSE = LongitudinalDecisionRegistry.close
_DR_LOCK = LongitudinalDecisionRegistry._lock
_DR_VALIDATE_STORAGE = LongitudinalDecisionRegistry._validate_storage
_DR_PUBLISH = LongitudinalDecisionRegistry._publish
_DR_RECOVER_TEMPORARY_OBJECTS = LongitudinalDecisionRegistry._recover_temporary_objects
_DR_LOAD_OR_CREATE_METADATA = LongitudinalDecisionRegistry._load_or_create_metadata
_DR_LOAD_JOURNAL = LongitudinalDecisionRegistry._load_journal
_DR_APPEND_JOURNAL = LongitudinalDecisionRegistry._append_journal
_DR_ACCEPT_OBSERVED_HEAD = LongitudinalDecisionRegistry._accept_observed_head
_DR_LOAD_STATE = LongitudinalDecisionRegistry._load_state
_DR_REPLAY_IN_FENCE = LongitudinalDecisionRegistry._replay_in_fence
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another series' decision.
_DR_RECEIPT = SeriesRegistrationReceipt
_DR_RESOLVED = RegisteredLongitudinalSeriesDecision
_DR_SELECTOR_RECORD = SeriesSelectorRecord
_DR_SELECTOR_PAGE = SeriesSelectorPage
_DR_SELECTOR_ID = _selector_id
_DR_DECISION_SHA256 = _series_decision_sha256
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_AUTHORITY_READ_FENCE": _PINNED_AUTHORITY_READ_FENCE,
        "_PINNED_ACTIVE_SNAPSHOT": _PINNED_ACTIVE_SNAPSHOT,
        "_PINNED_DECIDE_SERIES": _PINNED_DECIDE_SERIES,
        "_PINNED_REPLAY_SERIES": _PINNED_REPLAY_SERIES,
        "_PINNED_INVALID_SERIES_BYTES": _PINNED_INVALID_SERIES_BYTES,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_DR_CONSTRUCT",
            "_DR_CLOSE",
            "_DR_LOCK",
            "_DR_VALIDATE_STORAGE",
            "_DR_PUBLISH",
            "_DR_RECOVER_TEMPORARY_OBJECTS",
            "_DR_LOAD_OR_CREATE_METADATA",
            "_DR_LOAD_JOURNAL",
            "_DR_APPEND_JOURNAL",
            "_DR_ACCEPT_OBSERVED_HEAD",
            "_DR_LOAD_STATE",
            "_DR_REPLAY_IN_FENCE",
            "_DR_RECEIPT",
            "_DR_RESOLVED",
            "_DR_SELECTOR_RECORD",
            "_DR_SELECTOR_PAGE",
            "_DR_SELECTOR_ID",
            "_DR_DECISION_SHA256",
        )
    }
)


__all__ = [
    "LongitudinalDecisionBackup",
    "LongitudinalDecisionBackupObject",
    "LongitudinalDecisionJournalEntry",
    "LongitudinalDecisionRegistry",
    "LongitudinalDecisionRegistryConflict",
    "LongitudinalDecisionRegistryError",
    "LongitudinalDecisionRegistryMetadata",
    "LongitudinalDecisionRegistryStale",
    "LongitudinalDecisionRegistryUnsafe",
    "RegisteredLongitudinalSeriesDecision",
    "RegisteredSeriesObject",
    "SeriesAuthorityState",
    "SeriesOutcomeCount",
    "SeriesRegistrationReceipt",
    "SeriesSelectorPage",
    "SeriesSelectorRecord",
    "longitudinal_decision_backup_from_bytes",
    "registered_series_object_bytes",
    "registered_series_object_from_bytes",
]
