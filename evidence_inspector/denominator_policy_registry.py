"""Protected append-only registry for D09 denominator policies.

The registry is provider-local.  It stores exact canonical
``(CohortDenominatorPolicy, CohortDispositionPolicy)`` pairs, each bound to one
D05 cohort selector/version and manifest digest, under an opaque registry-scoped
D09 policy selector and version.  Callers never supply a summary: registration
derives one through the pinned registered D09 builder to prove the pair binds
the live manifest, and every protected read rebuilds the summary from live
D05/D06 authority before returning it.  The separate selector projection
carries only opaque selectors, digests, aggregate counts, and authority state.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.cohort_registry as d05_module
import evidence_inspector.cohort_summary as d09_module
from evidence_inspector.cohort_import import CohortRecordCatalog
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.cohort_summary import (
    MAX_COHORT_SUMMARY_MEMBERS,
    CohortDenominatorPolicy,
    CohortDispositionPolicy,
    CohortSummaryState,
    RegisteredCohortDenominatorSummary,
    build_registered_cohort_denominator_summary,
    cohort_denominator_policy_sha256,
    cohort_member_exclusion_set_sha256,
    registered_cohort_denominator_summary_bytes,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_REGISTERED_POLICIES = 10_000
MAX_POLICY_VERSION = 100_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 16
MAX_OBJECT_GRAPH_NODES = 2 * MAX_COHORT_SUMMARY_MEMBERS + 256
MAX_OBJECT_COLLECTION_ITEMS = MAX_COHORT_SUMMARY_MEMBERS
MAX_OBJECT_STRING_BYTES = 4096
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_BUILD_SUMMARY = build_registered_cohort_denominator_summary
_PINNED_SUMMARY_BYTES = registered_cohort_denominator_summary_bytes
_PINNED_POLICY_SHA256 = cohort_denominator_policy_sha256
_PINNED_MEMBER_SET_SHA256 = cohort_member_exclusion_set_sha256
_PINNED_COHORT_LIST = CohortRegistry.list_selectors
_PINNED_COHORT_INTEGRITY = d05_module._require_registry_integrity

RegistryId = Annotated[str, StringConstraints(pattern=r"^d09_registry_[0-9a-f]{32}$")]
PolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^d09_policy_[0-9a-f]{40}$")
]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DenominatorPolicyRegistryError(RuntimeError):
    """Sanitized registry failure."""


class DenominatorPolicyRegistryConflict(DenominatorPolicyRegistryError):
    pass


class DenominatorPolicyRegistryStale(DenominatorPolicyRegistryConflict):
    """A registered policy no longer derives a summary from live authority."""


class DenominatorPolicyRegistryUnsafe(DenominatorPolicyRegistryError):
    pass


class PolicyAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class DenominatorPolicyRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.d09-policy-registry-metadata.v1"] = (
        "traceback.d09-policy-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    catalog_storage_identity_sha256: Sha256
    record_catalog_scope_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    DenominatorPolicyRegistryMetadata
)


def _disposition_policy_sha256(policy: CohortDispositionPolicy) -> str:
    return hashlib.sha256(
        b"traceback-d09-disposition-policy-v1\0" + canonical_contract_bytes(policy)
    ).hexdigest()


def _selector_id(
    epoch: str,
    cohort_registry_id: str,
    cohort_selector_id: str,
    cohort_version: int,
    policy_id: str,
) -> str:
    digest = hashlib.sha256(
        b"traceback-d09-policy-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + cohort_registry_id.encode("ascii")
        + b"\0"
        + cohort_selector_id.encode("ascii")
        + b"\0"
        + str(cohort_version).encode("ascii")
        + b"\0"
        + policy_id.encode("ascii")
    ).hexdigest()
    return f"d09_policy_{digest[:40]}"


class RegisteredDenominatorPolicyObject(RegistryContract):
    """Protected stored policy pair and the exact D05 selection it binds."""

    schema_version: Literal["traceback.d09-registered-policy-object.v1"] = (
        "traceback.d09-registered-policy-object.v1"
    )
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    policy: CohortDenominatorPolicy
    disposition_policy: CohortDispositionPolicy

    @model_validator(mode="after")
    def exact_policy_pair(self) -> RegisteredDenominatorPolicyObject:
        if (
            self.policy.inclusion_sha256
            != _PINNED_MEMBER_SET_SHA256(self.disposition_policy.inclusion)
            or self.policy.exclusion_sha256
            != _PINNED_MEMBER_SET_SHA256(self.disposition_policy.exclusion)
            or self.policy.missingness_sha256
            != self.disposition_policy.missingness_sha256
        ):
            raise ValueError("registered D09 policy pair does not bind its rule sets")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredDenominatorPolicyObject
)
_POLICY_MODEL_TYPES, _POLICY_ENUM_TYPES = contract_type_graph(CohortDenominatorPolicy)
_DISPOSITION_MODEL_TYPES, _DISPOSITION_ENUM_TYPES = contract_type_graph(
    CohortDispositionPolicy
)


def _object_selector_id(epoch: str, value: RegisteredDenominatorPolicyObject) -> str:
    return _selector_id(
        epoch,
        value.cohort_registry_id,
        value.cohort_selector_id,
        value.cohort_version,
        value.policy.policy_id,
    )


class DenominatorPolicyJournalEntry(RegistryContract):
    schema_version: Literal["traceback.d09-policy-journal-entry.v1"] = (
        "traceback.d09-policy-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_POLICIES, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class DenominatorPolicyRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.d09-policy-registration-receipt.v1"] = (
        "traceback.d09-policy-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    policy_version: int = Field(ge=1, le=MAX_POLICY_VERSION, strict=True)
    object_sha256: Sha256
    denominator_policy_sha256: Sha256
    disposition_policy_sha256: Sha256
    cohort_manifest_sha256: Sha256


class RegisteredDenominatorPolicySummary(RegistryContract):
    """D09 summary rebuilt from live D05/D06 authority for one policy selector."""

    schema_version: Literal["traceback.d09-registered-policy-summary.v1"] = (
        "traceback.d09-registered-policy-summary.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    policy_version: int = Field(ge=1, le=MAX_POLICY_VERSION, strict=True)
    object_sha256: Sha256
    denominator_policy_sha256: Sha256
    disposition_policy_sha256: Sha256
    summary: RegisteredCohortDenominatorSummary
    rebuilt_against_live_authority: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredDenominatorPolicySummary:
        population = self.summary.population
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256,
            self.summary.registry_id,
            self.summary.selector_id,
            self.summary.cohort_version,
            population.denominator_policy_id,
        ):
            raise ValueError("registered D09 selector does not match its summary")
        if self.denominator_policy_sha256 != population.denominator_policy_sha256:
            raise ValueError("registered D09 policy digest does not match its summary")
        return self


class DenominatorPolicySelectorRecord(RegistryContract):
    schema_version: Literal["traceback.d09-policy-selector-record.v1"] = (
        "traceback.d09-policy-selector-record.v1"
    )
    selector_id: PolicySelectorId
    policy_version: int = Field(ge=1, le=MAX_POLICY_VERSION, strict=True)
    object_sha256: Sha256
    denominator_policy_sha256: Sha256
    disposition_policy_sha256: Sha256
    cohort_manifest_sha256: Sha256
    authority_state: PolicyAuthorityState
    summary_sha256: Sha256 | None
    summary_state: CohortSummaryState | None
    declared_members: int | None = Field(ge=1, le=MAX_COHORT_SUMMARY_MEMBERS)
    included_members: int | None = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS)
    excluded_members: int | None = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS)
    unavailable_members: int | None = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS)
    declared_denominator_units: int | None = Field(
        ge=1, le=MAX_COHORT_SUMMARY_MEMBERS
    )
    included_denominator_units: int | None = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS
    )
    excluded_denominator_units: int | None = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS
    )
    unavailable_denominator_units: int | None = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS
    )

    @model_validator(mode="after")
    def live_counts_only_when_current(self) -> DenominatorPolicySelectorRecord:
        live = (
            self.summary_sha256,
            self.summary_state,
            self.declared_members,
            self.included_members,
            self.excluded_members,
            self.unavailable_members,
            self.declared_denominator_units,
            self.included_denominator_units,
            self.excluded_denominator_units,
            self.unavailable_denominator_units,
        )
        if self.authority_state is PolicyAuthorityState.STALE:
            if any(item is not None for item in live):
                raise ValueError("a stale D09 selector cannot carry live counts")
            return self
        if any(item is None for item in live):
            raise ValueError("a current D09 selector requires live counts")
        if self.declared_members != (
            self.included_members + self.excluded_members + self.unavailable_members
        ) or self.declared_denominator_units != (
            self.included_denominator_units
            + self.excluded_denominator_units
            + self.unavailable_denominator_units
        ):
            raise ValueError("D09 selector counts must reconcile")
        return self


class DenominatorPolicySelectorPage(RegistryContract):
    schema_version: Literal["traceback.d09-policy-selector-page.v1"] = (
        "traceback.d09-policy-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    records: tuple[DenominatorPolicySelectorRecord, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: PolicySelectorId | None
    next_after_policy_version: int | None = Field(
        default=None, ge=1, le=MAX_POLICY_VERSION
    )


class DenominatorPolicyBackupObject(RegistryContract):
    schema_version: Literal["traceback.d09-policy-backup-object.v1"] = (
        "traceback.d09-policy-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class DenominatorPolicyBackup(RegistryContract):
    schema_version: Literal["traceback.d09-policy-backup.v1"] = (
        "traceback.d09-policy-backup.v1"
    )
    metadata: DenominatorPolicyRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    journal: tuple[DenominatorPolicyJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_POLICIES
    )
    objects: tuple[DenominatorPolicyBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_POLICIES
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(DenominatorPolicyBackup)


def registered_policy_object_bytes(value: RegisteredDenominatorPolicyObject) -> bytes:
    """Return exact bounded canonical bytes for one stored policy object."""

    return exact_model_bytes(
        value,
        RegisteredDenominatorPolicyObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_policy_object_from_bytes(
    content: bytes,
) -> RegisteredDenominatorPolicyObject:
    try:
        # The bounded parse enforces every structural budget before validation;
        # strict D09 contracts then validate in JSON mode from the same bytes.
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredDenominatorPolicyObject.model_validate_json(content)
        if registered_policy_object_bytes(value) != content:
            raise ValueError("registered D09 policy object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered D09 policy object is not canonical") from None


def _capture_policy_pair(
    policy: object, disposition_policy: object
) -> tuple[CohortDenominatorPolicy, CohortDispositionPolicy]:
    """Capture caller policy objects as exact canonical bytes, without hooks."""

    policy_content = exact_model_bytes(
        policy,
        CohortDenominatorPolicy,
        model_types=_POLICY_MODEL_TYPES,
        enum_types=_POLICY_ENUM_TYPES,
        max_bytes=64 * 1024,
        max_nodes=256,
        max_depth=8,
        max_collection_items=64,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )
    disposition_content = exact_model_bytes(
        disposition_policy,
        CohortDispositionPolicy,
        model_types=_DISPOSITION_MODEL_TYPES,
        enum_types=_DISPOSITION_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )
    return (
        CohortDenominatorPolicy.model_validate_json(policy_content),
        CohortDispositionPolicy.model_validate_json(disposition_content),
    )


def _canonical_backup_bytes(backup: DenominatorPolicyBackup) -> bytes:
    return exact_model_bytes(
        backup,
        DenominatorPolicyBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_POLICIES,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("D09 policy registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("D09 policy registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "D09 policy registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("D09 policy registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("D09 policy registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_policy_selector(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 51
        and value.startswith("d09_policy_")
        and all(character in "0123456789abcdef" for character in value[11:])
    )


def _is_policy_version(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_POLICY_VERSION


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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry object exceeds its bound"
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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry object digest is invalid"
        )
    return content


def _journal_entry_sha256(entry: DenominatorPolicyJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-d09-policy-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: DenominatorPolicyRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-d09-policy-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> DenominatorPolicyJournalEntry:
    placeholder = DenominatorPolicyJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return DenominatorPolicyJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_selector_versions(
    epoch: str, values: list[RegisteredDenominatorPolicyObject]
) -> None:
    """Each selector's committed policy versions must be exactly 1..N."""

    versions: dict[str, list[int]] = {}
    for value in values:
        if (
            value.cohort_registry_id != values[0].cohort_registry_id
            or value.cohort_registry_epoch_sha256
            != values[0].cohort_registry_epoch_sha256
        ):
            raise ValueError("D09 policy objects bind different cohort registries")
        versions.setdefault(_object_selector_id(epoch, value), []).append(
            value.policy.version
        )
    for items in versions.values():
        if sorted(items) != list(range(1, len(items) + 1)):
            raise ValueError("D09 policy versions are not contiguous")


def _validate_backup(backup: DenominatorPolicyBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    values: list[RegisteredDenominatorPolicyObject] = []
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            value = registered_policy_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup digest is invalid"
            )
        if (
            value.cohort_registry_id != backup.metadata.cohort_registry_id
            or value.cohort_registry_epoch_sha256
            != backup.metadata.cohort_registry_epoch_sha256
        ):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup object is invalid"
            )
        sizes[item.object_sha256] = len(content)
        values.append(value)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup journal is invalid"
        )
    try:
        if values:
            _validate_selector_versions(backup.metadata.registry_epoch_sha256, values)
    except ValueError:
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup history is invalid"
        ) from None
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup state is invalid"
        )


def denominator_policy_backup_from_bytes(content: bytes) -> DenominatorPolicyBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_POLICIES,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = DenominatorPolicyBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("D09 policy registry backup is not canonical")
    except (TypeError, ValueError):
        raise DenominatorPolicyRegistryConflict(
            "D09 policy registry backup is invalid"
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


def _bound_authority_identity(
    cohort_registry: object, record_catalog: object
) -> tuple[str, str, str, str]:
    """Return the exact D05/D06 identity this registry binds, or fail closed."""

    if type(cohort_registry) is not CohortRegistry:
        raise TypeError("D09 policy registry requires the exact cohort registry type")
    if type(record_catalog) is not CohortRecordCatalog:
        raise TypeError("D09 policy registry requires the exact record catalog type")
    try:
        _PINNED_COHORT_INTEGRITY(cohort_registry)
        catalog_state = object.__getattribute__(record_catalog, "__dict__")
    except Exception:
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry D05/D06 authority is invalid"
        ) from None
    if (
        type(catalog_state) is not dict
        or catalog_state.get("_cohort_registry") is not cohort_registry
        or catalog_state.get("_linkage_store")
        is not object.__getattribute__(cohort_registry, "_linkage_store")
    ):
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry D05/D06 authority does not match"
        )
    page = _PINNED_COHORT_LIST(cohort_registry, limit=1)
    storage = catalog_state.get("_catalog_storage_identity_sha256")
    scope = catalog_state.get("_recovery_scope_sha256")
    if (
        catalog_state.get("_cohort_registry_id") != page.registry_id
        or catalog_state.get("_cohort_registry_epoch_sha256")
        != page.registry_epoch_sha256
        or not _is_sha256(storage)
        or not _is_sha256(scope)
    ):
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry D05/D06 authority does not match"
        )
    return page.registry_id, page.registry_epoch_sha256, storage, scope


def _registry_instance_snapshot(
    registry: DenominatorPolicyRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_cohort_registry",
        "_record_catalog",
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
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            DenominatorPolicyRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry authority state changed"
            )
    if (
        type(instance["_cohort_registry"]) is not CohortRegistry
        or type(instance["_record_catalog"]) is not CohortRecordCatalog
    ):
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        )
    return (
        id(instance["root"]),
        id(instance["_cohort_registry"]),
        id(instance["_record_catalog"]),
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


def _seal_registry_instance(registry: DenominatorPolicyRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class DenominatorPolicyRegistry:
    """Descriptor-relative immutable D09 policy publication with live rebuild."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "backup_bytes",
            "close",
            "list_selectors",
            "register_policy",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        cohort_registry: CohortRegistry,
        record_catalog: CohortRecordCatalog,
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
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
                or not expected_registry_id.startswith("d09_registry_")
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_registry_id[13:]
                )
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry expected identity or head is invalid"
                )
        bound_identity = _bound_authority_identity(cohort_registry, record_catalog)
        self.root = _snapshot_path(root)
        self._cohort_registry = cohort_registry
        self._record_catalog = record_catalog
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry root must be private"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry root changed"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry objects are unsafe"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry lock is unsafe"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PR_LOCK(self, exclusive=True):
                self._metadata = _PR_LOAD_OR_CREATE_METADATA(
                    self, bound_identity, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._root_identity[0],
                    self._root_identity[1],
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _PR_RECOVER_TEMPORARY_OBJECTS(self)
                _, head = _PR_LOAD_STATE(self, check_trusted_head=False)
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise DenominatorPolicyRegistryUnsafe(
                            "new D09 policy registry cannot inherit an expected "
                            "identity"
                        )
                elif any(item is None for item in expected_values):
                    raise DenominatorPolicyRegistryUnsafe(
                        "D09 policy registry expected identity and head are required"
                    )
                if not root_created and expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise DenominatorPolicyRegistryUnsafe(
                        "D09 policy registry expected identity or head is invalid"
                    )
                self._trusted_head_sha256 = head
                _PR_ACCEPT_OBSERVED_HEAD(
                    self, _PR_LOAD_JOURNAL(self), head, check_instance=False
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

    def __enter__(self) -> DenominatorPolicyRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _PR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _PR_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _PR_VALIDATE_STORAGE(self)
                yield
                _PR_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry storage changed"
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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry storage changed"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry storage changed"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        if self._objects_fd is None:
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry objects are unavailable"
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
                    raise DenominatorPolicyRegistryUnsafe(
                        "D09 policy registry recovery is unsafe"
                    ) from None
        os.fsync(self._objects_fd)

    def _load_or_create_metadata(
        self,
        bound_identity: tuple[str, str, str, str],
        *,
        allow_create: bool,
    ) -> DenominatorPolicyRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry metadata is missing"
                ) from None
            metadata = DenominatorPolicyRegistryMetadata(
                registry_id=f"d09_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                cohort_registry_id=bound_identity[0],
                cohort_registry_epoch_sha256=bound_identity[1],
                catalog_storage_identity_sha256=bound_identity[2],
                record_catalog_scope_sha256=bound_identity[3],
            )
            try:
                _PR_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _PR_LOAD_OR_CREATE_METADATA(
                self, bound_identity, allow_create=False
            )
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                DenominatorPolicyRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        if (
            metadata.cohort_registry_id,
            metadata.cohort_registry_epoch_sha256,
            metadata.catalog_storage_identity_sha256,
            metadata.record_catalog_scope_sha256,
        ) != bound_identity:
            os.close(descriptor)
            self._metadata_fd = None
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry D05/D06 authority changed"
            )
        return metadata

    def _load_journal(self) -> tuple[DenominatorPolicyJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry journal is incomplete"
            )
        entries: list[DenominatorPolicyJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_POLICIES:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    DenominatorPolicyJournalEntry, line
                )
            except Exception:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: DenominatorPolicyJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry journal append failed"
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
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[DenominatorPolicyJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredDenominatorPolicyObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise DenominatorPolicyRegistryUnsafe("D09 policy registry is closed")
        journal = _PR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_POLICIES + 1:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredDenominatorPolicyObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry journal binding is invalid"
                )
            try:
                value = registered_policy_object_from_bytes(content)
            except ValueError:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry object is invalid"
                ) from None
            if (
                value.cohort_registry_id != self._metadata.cohort_registry_id
                or value.cohort_registry_epoch_sha256
                != self._metadata.cohort_registry_epoch_sha256
            ):
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry object binding is invalid"
                )
            loaded[entry.object_sha256] = (value, content)
        try:
            if loaded:
                _validate_selector_versions(
                    self._metadata.registry_epoch_sha256,
                    [value for value, _ in loaded.values()],
                )
        except ValueError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry history is invalid"
            ) from None
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _PR_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry state rollback detected"
                )
        return loaded, head

    def _build_live_summary(
        self, value: RegisteredDenominatorPolicyObject
    ) -> RegisteredCohortDenominatorSummary:
        """Derive one summary through the pinned D09 builder and its D05/D06 fence.

        The builder acquires the linkage, D05 registry, D06 catalog and result
        trust fences itself and releases them on return; it cannot run inside a
        caller-held linkage fence.  The D09 registry lock is therefore taken
        outside it.  Any builder failure means the stored binding no longer
        derives from current authority.
        """

        try:
            summary = _PINNED_BUILD_SUMMARY(
                registry=self._cohort_registry,
                selector_id=value.cohort_selector_id,
                cohort_version=value.cohort_version,
                record_catalog=self._record_catalog,
                policy=value.policy,
                disposition_policy=value.disposition_policy,
            )
            summary = RegisteredCohortDenominatorSummary.model_validate_json(
                _PINNED_SUMMARY_BYTES(summary)
            )
        except Exception:
            raise DenominatorPolicyRegistryStale(
                "D09 policy no longer derives a summary from live authority"
            ) from None
        if (
            summary.registry_id != value.cohort_registry_id
            or summary.registry_epoch_sha256 != value.cohort_registry_epoch_sha256
            or summary.selector_id != value.cohort_selector_id
            or summary.cohort_version != value.cohort_version
            or summary.cohort_manifest_sha256 != value.cohort_manifest_sha256
            or summary.population.denominator_policy_sha256
            != _PINNED_POLICY_SHA256(value.policy)
            or summary.population.denominator_policy_id != value.policy.policy_id
        ):
            raise DenominatorPolicyRegistryStale(
                "D09 policy no longer derives a summary from live authority"
            )
        return summary

    def register_policy(
        self,
        cohort_selector_id: str,
        cohort_version: int,
        policy: CohortDenominatorPolicy,
        disposition_policy: CohortDispositionPolicy,
        *,
        expected_cohort_manifest_sha256: str,
    ) -> DenominatorPolicyRegistrationReceipt:
        """Bind one exact D09 policy pair to one live D05 selection immutably.

        The caller supplies the D05 selection, the exact policy pair, and the
        manifest digest it expects.  No summary is accepted: one is derived
        through the pinned builder to prove the pair binds the live manifest.
        """

        _require_registry_integrity(self)
        if (
            type(cohort_selector_id) is not str
            or len(cohort_selector_id) != 56
            or not cohort_selector_id.startswith("cohort_selector_")
            or any(
                character not in "0123456789abcdef"
                for character in cohort_selector_id[16:]
            )
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
            or not _is_sha256(expected_cohort_manifest_sha256)
        ):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registration selection is invalid"
            )
        try:
            captured_policy, captured_disposition = _capture_policy_pair(
                policy, disposition_policy
            )
            captured = RegisteredDenominatorPolicyObject(
                cohort_registry_id=self._metadata.cohort_registry_id,
                cohort_registry_epoch_sha256=(
                    self._metadata.cohort_registry_epoch_sha256
                ),
                cohort_selector_id=cohort_selector_id,
                cohort_version=cohort_version,
                cohort_manifest_sha256=expected_cohort_manifest_sha256,
                policy=captured_policy,
                disposition_policy=captured_disposition,
            )
            content = registered_policy_object_bytes(captured)
            captured = registered_policy_object_from_bytes(content)
        except Exception:
            raise DenominatorPolicyRegistryConflict(
                "D09 policy inputs are not exact canonical contracts"
            ) from None
        digest = hashlib.sha256(content).hexdigest()
        epoch = self._metadata.registry_epoch_sha256
        selector_id = _PR_SELECTOR_ID(
            epoch,
            captured.cohort_registry_id,
            captured.cohort_selector_id,
            captured.cohort_version,
            captured.policy.policy_id,
        )
        with _PR_LOCK(self, exclusive=True):
            try:
                _PR_BUILD_LIVE_SUMMARY(self, captured)
            except DenominatorPolicyRegistryStale:
                raise DenominatorPolicyRegistryConflict(
                    "D09 policy does not derive a summary from the live selection"
                ) from None
            _PR_RECOVER_TEMPORARY_OBJECTS(self)
            loaded, head = _PR_LOAD_STATE(self)
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
                    raise DenominatorPolicyRegistryConflict(
                        "D09 policy object digest conflicts"
                    )
            else:
                versions = sorted(
                    value.policy.version
                    for value, _ in loaded.values()
                    if _object_selector_id(epoch, value) == selector_id
                )
                if captured.policy.version in versions:
                    raise DenominatorPolicyRegistryConflict(
                        "D09 policy version is already registered with other content"
                    )
                if captured.policy.version != len(versions) + 1:
                    raise DenominatorPolicyRegistryConflict(
                        "D09 policy version must extend its selector history"
                    )
                if len(loaded) >= MAX_REGISTERED_POLICIES:
                    raise DenominatorPolicyRegistryConflict(
                        "D09 policy registry is full"
                    )
                if (
                    sum(len(item[1]) for item in loaded.values()) + len(content)
                    > MAX_TOTAL_OBJECT_BYTES
                ):
                    raise DenominatorPolicyRegistryConflict(
                        "D09 policy registry byte bound would be exceeded"
                    )
                try:
                    _PR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                except FileExistsError:
                    if _read_exact_object(self._objects_fd, digest) != content:
                        raise DenominatorPolicyRegistryConflict(
                            "D09 policy publication conflicts"
                        ) from None
                _PR_APPEND_JOURNAL(
                    self,
                    _build_journal_entry(
                        sequence=len(loaded) + 1,
                        previous_entry_sha256=head,
                        object_sha256=digest,
                        object_bytes=len(content),
                    ),
                )
            final, final_head = _PR_LOAD_STATE(self)
            if digest not in final or final[digest][1] != content:
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy publication is unproven"
                )
            return _PR_RECEIPT(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(final),
                state_head_sha256=final_head,
                selector_id=selector_id,
                policy_version=captured.policy.version,
                object_sha256=digest,
                denominator_policy_sha256=_PINNED_POLICY_SHA256(captured.policy),
                disposition_policy_sha256=_PR_DISPOSITION_SHA256(
                    captured.disposition_policy
                ),
                cohort_manifest_sha256=captured.cohort_manifest_sha256,
            )

    def resolve(
        self, selector_id: str, policy_version: int
    ) -> RegisteredDenominatorPolicySummary:
        """Return one summary rebuilt from live D05/D06 authority, never a cache."""

        _require_registry_integrity(self)
        if not _is_policy_selector(selector_id) or not _is_policy_version(
            policy_version
        ):
            raise DenominatorPolicyRegistryConflict("D09 policy selector is invalid")
        with _PR_LOCK(self, exclusive=False):
            loaded, head = _PR_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            matches = [
                (digest, value)
                for digest, (value, _) in loaded.items()
                if _PR_SELECTOR_ID(
                    epoch,
                    value.cohort_registry_id,
                    value.cohort_selector_id,
                    value.cohort_version,
                    value.policy.policy_id,
                )
                == selector_id
                and value.policy.version == policy_version
            ]
            if len(matches) != 1:
                raise DenominatorPolicyRegistryConflict(
                    "D09 policy selector is unavailable"
                )
            digest, value = matches[0]
            summary = _PR_BUILD_LIVE_SUMMARY(self, value)
            return _PR_RESOLVED(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                selector_id=selector_id,
                policy_version=policy_version,
                object_sha256=digest,
                denominator_policy_sha256=_PINNED_POLICY_SHA256(value.policy),
                disposition_policy_sha256=_PR_DISPOSITION_SHA256(
                    value.disposition_policy
                ),
                summary=summary,
            )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        after_policy_version: int | None = None,
        limit: int = 50,
    ) -> DenominatorPolicySelectorPage:
        """Return one bounded privacy-safe page with live authority state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise DenominatorPolicyRegistryConflict(
                "D09 policy selector page bound is invalid"
            )
        if (after_selector_id is None) != (after_policy_version is None):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy selector cursor is incomplete"
            )
        if after_selector_id is not None and (
            not _is_policy_selector(after_selector_id)
            or not _is_policy_version(after_policy_version)
        ):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy selector cursor is invalid"
            )
        with _PR_LOCK(self, exclusive=False):
            loaded, head = _PR_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            ordered = sorted(
                (
                    _PR_SELECTOR_ID(
                        epoch,
                        value.cohort_registry_id,
                        value.cohort_selector_id,
                        value.cohort_version,
                        value.policy.policy_id,
                    ),
                    value.policy.version,
                    digest,
                    value,
                )
                for digest, (value, _) in loaded.items()
            )
            if after_selector_id is not None:
                cursor = (after_selector_id, after_policy_version)
                ordered = [item for item in ordered if item[:2] > cursor]
            selected = ordered[:limit]
            rows: list[DenominatorPolicySelectorRecord] = []
            for selector_id, version, digest, value in selected:
                try:
                    summary = _PR_BUILD_LIVE_SUMMARY(self, value)
                except DenominatorPolicyRegistryStale:
                    summary = None
                population = summary.population if summary is not None else None
                rows.append(
                    _PR_SELECTOR_RECORD(
                        selector_id=selector_id,
                        policy_version=version,
                        object_sha256=digest,
                        denominator_policy_sha256=_PINNED_POLICY_SHA256(value.policy),
                        disposition_policy_sha256=_PR_DISPOSITION_SHA256(
                            value.disposition_policy
                        ),
                        cohort_manifest_sha256=value.cohort_manifest_sha256,
                        authority_state=(
                            PolicyAuthorityState.STALE
                            if summary is None
                            else PolicyAuthorityState.CURRENT
                        ),
                        summary_sha256=(
                            summary.summary_sha256 if summary is not None else None
                        ),
                        summary_state=(
                            population.state if population is not None else None
                        ),
                        **{
                            name: (
                                getattr(population, name)
                                if population is not None
                                else None
                            )
                            for name in (
                                "declared_members",
                                "included_members",
                                "excluded_members",
                                "unavailable_members",
                                "declared_denominator_units",
                                "included_denominator_units",
                                "excluded_denominator_units",
                                "unavailable_denominator_units",
                            )
                        },
                    )
                )
            more = len(ordered) > len(selected)
            return _PR_SELECTOR_PAGE(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                records=tuple(rows),
                next_after_selector_id=(
                    rows[-1].selector_id if more and rows else None
                ),
                next_after_policy_version=(
                    rows[-1].policy_version if more and rows else None
                ),
            )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _PR_LOCK(self, exclusive=False):
            loaded, head = _PR_LOAD_STATE(self)
            backup = DenominatorPolicyBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_PR_LOAD_JOURNAL(self),
                objects=tuple(
                    DenominatorPolicyBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise DenominatorPolicyRegistryConflict(
                    "D09 policy registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        cohort_registry: CohortRegistry,
        record_catalog: CohortRecordCatalog,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> DenominatorPolicyRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        backup = denominator_policy_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup expected head is invalid"
            )
        bound_identity = _bound_authority_identity(cohort_registry, record_catalog)
        if (
            backup.metadata.cohort_registry_id,
            backup.metadata.cohort_registry_epoch_sha256,
            backup.metadata.catalog_storage_identity_sha256,
            backup.metadata.record_catalog_scope_sha256,
        ) != bound_identity:
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry backup authority is invalid"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry restore parent changed"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry restore root changed"
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
                raise DenominatorPolicyRegistryUnsafe(
                    "D09 policy registry restore objects changed"
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
            raise DenominatorPolicyRegistryConflict(
                "D09 policy registry restore target already exists"
            ) from None
        except OSError:
            raise DenominatorPolicyRegistryUnsafe(
                "D09 policy registry restore failed"
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
        return _PR_CONSTRUCT(
            target,
            cohort_registry=cohort_registry,
            record_catalog=record_catalog,
            expected_registry_id=expected_registry_id,
            expected_registry_epoch_sha256=expected_registry_epoch_sha256,
            expected_state_head_sha256=expected_state_head_sha256,
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: DenominatorPolicyRegistry.__dict__[name]
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
            "_build_live_summary",
            "register_policy",
            "resolve",
            "list_selectors",
            "backup_bytes",
            "restore",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not DenominatorPolicyRegistry or any(
        DenominatorPolicyRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise DenominatorPolicyRegistryUnsafe("D09 policy registry callable changed")


def _require_registry_integrity(registry: DenominatorPolicyRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise DenominatorPolicyRegistryUnsafe("D09 policy registry callable changed")
    authority_sources = {
        "_PINNED_BUILD_SUMMARY": d09_module.build_registered_cohort_denominator_summary,
        "_PINNED_SUMMARY_BYTES": (
            d09_module.registered_cohort_denominator_summary_bytes
        ),
        "_PINNED_POLICY_SHA256": d09_module.cohort_denominator_policy_sha256,
        "_PINNED_MEMBER_SET_SHA256": d09_module.cohort_member_exclusion_set_sha256,
        "_PINNED_COHORT_LIST": CohortRegistry.list_selectors,
        "_PINNED_COHORT_INTEGRITY": d05_module._require_registry_integrity,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority callable changed"
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
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise DenominatorPolicyRegistryUnsafe(
            "D09 policy registry authority state changed"
        )


_PR_CONSTRUCT = DenominatorPolicyRegistry
_PR_CLOSE = DenominatorPolicyRegistry.close
_PR_LOCK = DenominatorPolicyRegistry._lock
_PR_VALIDATE_STORAGE = DenominatorPolicyRegistry._validate_storage
_PR_PUBLISH = DenominatorPolicyRegistry._publish
_PR_RECOVER_TEMPORARY_OBJECTS = DenominatorPolicyRegistry._recover_temporary_objects
_PR_LOAD_OR_CREATE_METADATA = DenominatorPolicyRegistry._load_or_create_metadata
_PR_LOAD_JOURNAL = DenominatorPolicyRegistry._load_journal
_PR_APPEND_JOURNAL = DenominatorPolicyRegistry._append_journal
_PR_ACCEPT_OBSERVED_HEAD = DenominatorPolicyRegistry._accept_observed_head
_PR_LOAD_STATE = DenominatorPolicyRegistry._load_state
_PR_BUILD_LIVE_SUMMARY = DenominatorPolicyRegistry._build_live_summary
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another policy's summary.
_PR_RECEIPT = DenominatorPolicyRegistrationReceipt
_PR_RESOLVED = RegisteredDenominatorPolicySummary
_PR_SELECTOR_RECORD = DenominatorPolicySelectorRecord
_PR_SELECTOR_PAGE = DenominatorPolicySelectorPage
_PR_SELECTOR_ID = _selector_id
_PR_DISPOSITION_SHA256 = _disposition_policy_sha256
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_BUILD_SUMMARY": _PINNED_BUILD_SUMMARY,
        "_PINNED_SUMMARY_BYTES": _PINNED_SUMMARY_BYTES,
        "_PINNED_POLICY_SHA256": _PINNED_POLICY_SHA256,
        "_PINNED_MEMBER_SET_SHA256": _PINNED_MEMBER_SET_SHA256,
        "_PINNED_COHORT_LIST": _PINNED_COHORT_LIST,
        "_PINNED_COHORT_INTEGRITY": _PINNED_COHORT_INTEGRITY,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_PR_CONSTRUCT",
            "_PR_CLOSE",
            "_PR_LOCK",
            "_PR_VALIDATE_STORAGE",
            "_PR_PUBLISH",
            "_PR_RECOVER_TEMPORARY_OBJECTS",
            "_PR_LOAD_OR_CREATE_METADATA",
            "_PR_LOAD_JOURNAL",
            "_PR_APPEND_JOURNAL",
            "_PR_ACCEPT_OBSERVED_HEAD",
            "_PR_LOAD_STATE",
            "_PR_BUILD_LIVE_SUMMARY",
            "_PR_RECEIPT",
            "_PR_RESOLVED",
            "_PR_SELECTOR_RECORD",
            "_PR_SELECTOR_PAGE",
            "_PR_SELECTOR_ID",
            "_PR_DISPOSITION_SHA256",
        )
    }
)


__all__ = [
    "DenominatorPolicyBackup",
    "DenominatorPolicyBackupObject",
    "DenominatorPolicyJournalEntry",
    "DenominatorPolicyRegistrationReceipt",
    "DenominatorPolicyRegistry",
    "DenominatorPolicyRegistryConflict",
    "DenominatorPolicyRegistryError",
    "DenominatorPolicyRegistryMetadata",
    "DenominatorPolicyRegistryStale",
    "DenominatorPolicyRegistryUnsafe",
    "DenominatorPolicySelectorPage",
    "DenominatorPolicySelectorRecord",
    "PolicyAuthorityState",
    "RegisteredDenominatorPolicyObject",
    "RegisteredDenominatorPolicySummary",
    "denominator_policy_backup_from_bytes",
    "registered_policy_object_bytes",
    "registered_policy_object_from_bytes",
]
