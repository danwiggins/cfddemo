"""Protected append-only registry for approved D03 anchor policies and D07 envelopes.

The registry is provider-local.  It stores exact canonical
``(LongitudinalAnchorPolicy, RepeatabilityEnvelope)`` approvals, each bound to
one D05 cohort selector/version and manifest digest and to the exact
registrant-approved anchor-candidate records, under an opaque registry-scoped
selector and approval version.  Callers never supply a candidate page or an
anchor identity at read time: every protected read derives the bounded
candidate page from the stored approval and live D01 linkage and D05 cohort
authority, and resolves an opaque approved-anchor selector only against that
exact page.  Public projections carry only opaque selectors, aliases,
ordinals, offsets, controlled states, and digests.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
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

import evidence_inspector.cohort_registry as d05_module
from evidence_inspector.cohort_manifest import (
    MAX_MEMBERS,
    MAX_TIME_COORDINATE,
    MIN_TIME_COORDINATE,
    capture_expected_trust_pins,
    trust_pins_sha256,
)
from evidence_inspector.cohort_registry import CohortRegistry, CohortRegistryError
from evidence_inspector.longitudinal_compatibility import (
    MAX_SERIES_MEMBERS,
    ComparisonDimension,
    DimensionValueState,
    LongitudinalAnchorPolicy,
    LongitudinalOutcome,
    LongitudinalReason,
    LongitudinalRecord,
    decide_longitudinal_member,
    longitudinal_anchor_policy_sha256,
    longitudinal_comparison_key_sha256,
    longitudinal_record_sha256,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    Version,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage import linkage_revision_sha256
from evidence_inspector.provider_linkage_store import (
    ProviderLinkageStore,
    committed_linkage_receipt_sha256,
)
from evidence_inspector.repeatability_comparison import (
    RepeatabilityEnvelope,
    repeatability_envelope_sha256,
)
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

MAX_ANCHOR_CANDIDATES = MAX_SERIES_MEMBERS
MAX_REGISTERED_APPROVALS = 10_000
MAX_APPROVAL_VERSION = 100_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 64
MAX_OBJECT_GRAPH_NODES = 4_000_000
MAX_OBJECT_COLLECTION_ITEMS = 2 * MAX_ANCHOR_CANDIDATES + 2
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
_PINNED_DECIDE_MEMBER = decide_longitudinal_member
_PINNED_COHORT_LIST = CohortRegistry.list_selectors
_PINNED_COHORT_READ_FENCE = CohortRegistry.authority_read_fence
_PINNED_COHORT_RESOLVE_IN_FENCE = CohortRegistry.resolve_history_in_fence
_PINNED_COHORT_INTEGRITY = d05_module._require_registry_integrity

RegistryId = Annotated[
    str, StringConstraints(pattern=r"^anchor_registry_[0-9a-f]{32}$")
]
PolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_policy_[0-9a-f]{40}$")
]
AnchorSelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_candidate_[0-9a-f]{40}$")
]
CandidateAlias = Annotated[str, StringConstraints(pattern=r"^candidate_[0-9a-f]{12}$")]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AnchorPolicyRegistryError(RuntimeError):
    """Sanitized registry failure."""


class AnchorPolicyRegistryConflict(AnchorPolicyRegistryError):
    pass


class AnchorPolicyRegistryStale(AnchorPolicyRegistryConflict):
    """A registered approval no longer derives a candidate page from live authority."""


class AnchorPolicyRegistryUnsafe(AnchorPolicyRegistryError):
    pass


class AnchorPolicyAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class AnchorEligibilityState(StrEnum):
    """Controlled D03 self-evaluation state of one admitted anchor candidate."""

    ELIGIBLE = "eligible"
    RESULT_STATE_INELIGIBLE = "result_state_ineligible"


class AnchorPolicyRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-registry-metadata.v1"] = (
        "traceback.anchor-policy-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_trust_pins_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    AnchorPolicyRegistryMetadata
)


def _domain_sha256(domain: bytes, *parts: str) -> str:
    digest = hashlib.sha256(domain)
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode("ascii"))
    return digest.hexdigest()


def _selector_id(
    epoch: str,
    cohort_registry_id: str,
    cohort_selector_id: str,
    cohort_version: int,
    policy_id: str,
) -> str:
    digest = _domain_sha256(
        b"traceback-anchor-policy-selector-v1",
        epoch,
        cohort_registry_id,
        cohort_selector_id,
        str(cohort_version),
        policy_id,
    )
    return f"anchor_policy_{digest[:40]}"


def _anchor_selector_id(epoch: str, object_sha256: str, record_sha256: str) -> str:
    digest = _domain_sha256(
        b"traceback-anchor-candidate-selector-v1", epoch, object_sha256, record_sha256
    )
    return f"anchor_candidate_{digest[:40]}"


def _candidate_alias(anchor_selector_id: str) -> str:
    digest = _domain_sha256(b"traceback-anchor-candidate-alias-v1", anchor_selector_id)
    return f"candidate_{digest[:12]}"


def _envelope_binds_anchor_key(
    envelope: RepeatabilityEnvelope, record: LongitudinalRecord
) -> bool:
    """Mirror the D07 anchor-side measurement identity gate for one anchor key."""

    key = record.comparison_key
    dimensions = {item.dimension: item for item in key.dimensions}
    uncertainty = dimensions[ComparisonDimension.UNCERTAINTY_METHOD]
    denominator = dimensions[ComparisonDimension.DENOMINATOR_SEMANTICS]
    return (
        key.method_ref == envelope.method_ref
        and key.method_definition_sha256 == envelope.method_definition_sha256
        and key.quantity_id == envelope.quantity_id
        and key.unit == envelope.unit
        and uncertainty.state == DimensionValueState.KNOWN
        and uncertainty.content_sha256 == envelope.uncertainty_method_sha256
        and denominator.state == DimensionValueState.KNOWN
        and denominator.content_sha256 == envelope.denominator_semantics_sha256
    )


class RegisteredAnchorPolicyObject(RegistryContract):
    """Protected approval: one D03 policy, one D07 envelope, approved candidates."""

    schema_version: Literal["traceback.anchor-policy-object.v1"] = (
        "traceback.anchor-policy-object.v1"
    )
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    approval_version: int = Field(ge=1, le=MAX_APPROVAL_VERSION, strict=True)
    policy: LongitudinalAnchorPolicy
    policy_sha256: Sha256
    envelope: RepeatabilityEnvelope
    envelope_sha256: Sha256
    expected_authority_head_sha256: Sha256
    anchor_candidates: tuple[LongitudinalRecord, ...] = Field(
        min_length=1, max_length=MAX_ANCHOR_CANDIDATES
    )

    @model_validator(mode="after")
    def exact_bindings(self) -> RegisteredAnchorPolicyObject:
        if longitudinal_anchor_policy_sha256(self.policy) != self.policy_sha256:
            raise ValueError("anchor policy digest does not match its policy")
        if repeatability_envelope_sha256(self.envelope) != self.envelope_sha256:
            raise ValueError("anchor policy envelope digest does not match")
        digests = [longitudinal_record_sha256(item) for item in self.anchor_candidates]
        if digests != sorted(digests) or len(digests) != len(set(digests)):
            raise ValueError("anchor candidates must be uniquely sorted by record")
        # One linkage revision is one D05 member; two record snapshots of it
        # (for example differing only in result state) would be two candidates
        # for one member.
        linkages = {
            (
                item.linkage_revision.provider_namespace,
                item.linkage_revision.linkage_id,
                item.linkage_revision.revision,
            )
            for item in self.anchor_candidates
        }
        if len(linkages) != len(self.anchor_candidates):
            raise ValueError("anchor candidates must bind distinct linkage revisions")
        for record in self.anchor_candidates:
            if record.activation_receipt is None:
                raise ValueError("anchor candidates require an activation receipt")
            if (
                longitudinal_comparison_key_sha256(record.comparison_key)
                != self.policy.anchor_key_sha256
            ):
                raise ValueError("anchor candidate is not the policy's anchor key")
            if not _envelope_binds_anchor_key(self.envelope, record):
                raise ValueError("D07 envelope does not bind the policy's anchor key")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredAnchorPolicyObject
)


def _object_selector_id(epoch: str, value: RegisteredAnchorPolicyObject) -> str:
    return _selector_id(
        epoch,
        value.cohort_registry_id,
        value.cohort_selector_id,
        value.cohort_version,
        value.policy.policy_id,
    )


class AnchorPolicyJournalEntry(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-journal-entry.v1"] = (
        "traceback.anchor-policy-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_APPROVALS, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class AnchorPolicyRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-registration-receipt.v1"] = (
        "traceback.anchor-policy-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_APPROVALS)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    approval_version: int = Field(ge=1, le=MAX_APPROVAL_VERSION)
    object_sha256: Sha256
    policy_sha256: Sha256
    envelope_sha256: Sha256
    anchor_key_sha256: Sha256
    candidate_count: int = Field(ge=1, le=MAX_ANCHOR_CANDIDATES)


class AnchorCandidate(RegistryContract):
    """Privacy-safe projection of one live, policy-admitted anchor candidate."""

    schema_version: Literal["traceback.anchor-candidate.v1"] = (
        "traceback.anchor-candidate.v1"
    )
    anchor_selector_id: AnchorSelectorId
    alias: CandidateAlias
    biological_timepoint_ordinal: int = Field(ge=1, le=MAX_MEMBERS, strict=True)
    time_offset_seconds: int = Field(
        ge=0, le=MAX_TIME_COORDINATE - MIN_TIME_COORDINATE, strict=True
    )
    method_version: Version
    eligibility_state: AnchorEligibilityState

    @model_validator(mode="after")
    def exact_alias(self) -> AnchorCandidate:
        if self.alias != _candidate_alias(self.anchor_selector_id):
            raise ValueError("anchor candidate alias does not match its selector")
        return self


def _candidate_order(candidate: AnchorCandidate) -> tuple[int, int, str]:
    return (
        candidate.biological_timepoint_ordinal,
        candidate.time_offset_seconds,
        candidate.anchor_selector_id,
    )


def _candidate_page_sha256(
    *,
    registry_id: str,
    registry_epoch_sha256: str,
    policy_selector_id: str,
    approval_version: int,
    object_sha256: str,
    policy_sha256: str,
    envelope_sha256: str,
    anchor_key_sha256: str,
    cohort_manifest_sha256: str,
    candidates: tuple[AnchorCandidate, ...],
) -> str:
    """Bind page content, never registry state heads, so unrelated appends keep it."""

    payload = {
        "registry_id": registry_id,
        "registry_epoch_sha256": registry_epoch_sha256,
        "policy_selector_id": policy_selector_id,
        "approval_version": approval_version,
        "object_sha256": object_sha256,
        "policy_sha256": policy_sha256,
        "envelope_sha256": envelope_sha256,
        "anchor_key_sha256": anchor_key_sha256,
        "cohort_manifest_sha256": cohort_manifest_sha256,
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }
    return hashlib.sha256(
        b"traceback-anchor-candidate-page-v1\0"
        + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


class AnchorCandidatePage(RegistryContract):
    """Bounded live candidate page for one approval; no protected identity."""

    schema_version: Literal["traceback.anchor-candidate-page.v1"] = (
        "traceback.anchor-candidate-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_APPROVALS)
    state_head_sha256: Sha256
    policy_selector_id: PolicySelectorId
    approval_version: int = Field(ge=1, le=MAX_APPROVAL_VERSION)
    object_sha256: Sha256
    policy_sha256: Sha256
    envelope_sha256: Sha256
    anchor_key_sha256: Sha256
    cohort_manifest_sha256: Sha256
    candidates: tuple[AnchorCandidate, ...] = Field(
        min_length=1, max_length=MAX_ANCHOR_CANDIDATES
    )
    candidate_page_sha256: Sha256
    explicit_selection_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_page(self) -> AnchorCandidatePage:
        keys = [_candidate_order(item) for item in self.candidates]
        selectors = [item.anchor_selector_id for item in self.candidates]
        if keys != sorted(keys) or len(selectors) != len(set(selectors)):
            raise ValueError("anchor candidates must be uniquely ordered")
        if self.candidate_page_sha256 != _candidate_page_sha256(
            registry_id=self.registry_id,
            registry_epoch_sha256=self.registry_epoch_sha256,
            policy_selector_id=self.policy_selector_id,
            approval_version=self.approval_version,
            object_sha256=self.object_sha256,
            policy_sha256=self.policy_sha256,
            envelope_sha256=self.envelope_sha256,
            anchor_key_sha256=self.anchor_key_sha256,
            cohort_manifest_sha256=self.cohort_manifest_sha256,
            candidates=self.candidates,
        ):
            raise ValueError("anchor candidate page digest is invalid")
        return self


class ResolvedApprovedAnchor(RegistryContract):
    """Protected approval plus one explicitly selected live anchor; never public."""

    schema_version: Literal["traceback.resolved-approved-anchor.v1"] = (
        "traceback.resolved-approved-anchor.v1"
    )
    protected_only: Literal[True] = True
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_APPROVALS)
    state_head_sha256: Sha256
    policy_selector_id: PolicySelectorId
    approval_version: int = Field(ge=1, le=MAX_APPROVAL_VERSION)
    object_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    policy: LongitudinalAnchorPolicy
    policy_sha256: Sha256
    envelope: RepeatabilityEnvelope
    envelope_sha256: Sha256
    expected_authority_head_sha256: Sha256
    candidate_page_sha256: Sha256
    candidate_page: AnchorCandidatePage
    candidate: AnchorCandidate
    anchor_record: LongitudinalRecord
    anchor_record_sha256: Sha256
    replayed_against_live_authority: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> ResolvedApprovedAnchor:
        if longitudinal_anchor_policy_sha256(self.policy) != self.policy_sha256:
            raise ValueError("resolved anchor policy digest is invalid")
        if repeatability_envelope_sha256(self.envelope) != self.envelope_sha256:
            raise ValueError("resolved anchor envelope digest is invalid")
        if longitudinal_record_sha256(self.anchor_record) != self.anchor_record_sha256:
            raise ValueError("resolved anchor record digest is invalid")
        if (
            longitudinal_comparison_key_sha256(self.anchor_record.comparison_key)
            != self.policy.anchor_key_sha256
        ):
            raise ValueError("resolved anchor is not the policy's anchor key")
        if not _envelope_binds_anchor_key(self.envelope, self.anchor_record):
            raise ValueError("resolved anchor envelope does not bind its anchor key")
        page = self.candidate_page
        if (
            page.registry_id,
            page.registry_epoch_sha256,
            page.state_version,
            page.state_head_sha256,
            page.policy_selector_id,
            page.approval_version,
            page.object_sha256,
            page.policy_sha256,
            page.envelope_sha256,
            page.anchor_key_sha256,
            page.cohort_manifest_sha256,
            page.candidate_page_sha256,
        ) != (
            self.registry_id,
            self.registry_epoch_sha256,
            self.state_version,
            self.state_head_sha256,
            self.policy_selector_id,
            self.approval_version,
            self.object_sha256,
            self.policy_sha256,
            self.envelope_sha256,
            self.policy.anchor_key_sha256,
            self.cohort_manifest_sha256,
            self.candidate_page_sha256,
        ) or self.candidate not in page.candidates:
            raise ValueError("resolved anchor does not bind its candidate page")
        if self.candidate.anchor_selector_id != _anchor_selector_id(
            self.registry_epoch_sha256, self.object_sha256, self.anchor_record_sha256
        ):
            raise ValueError("resolved anchor selector does not match its record")
        if self.candidate.eligibility_state is not AnchorEligibilityState.ELIGIBLE:
            raise ValueError("resolved anchor must be eligible")
        if self.policy_selector_id != _selector_id(
            self.registry_epoch_sha256,
            self.cohort_registry_id,
            self.cohort_selector_id,
            self.cohort_version,
            self.policy.policy_id,
        ):
            raise ValueError("resolved anchor policy selector is invalid")
        return self


class AnchorPolicySelectorRecord(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-selector-record.v1"] = (
        "traceback.anchor-policy-selector-record.v1"
    )
    selector_id: PolicySelectorId
    approval_version: int = Field(ge=1, le=MAX_APPROVAL_VERSION)
    object_sha256: Sha256
    policy_sha256: Sha256
    envelope_sha256: Sha256
    anchor_key_sha256: Sha256
    cohort_manifest_sha256: Sha256
    authority_state: AnchorPolicyAuthorityState
    candidate_count: int | None = Field(default=None, ge=1, le=MAX_ANCHOR_CANDIDATES)
    eligible_count: int | None = Field(default=None, ge=0, le=MAX_ANCHOR_CANDIDATES)
    candidate_page_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def live_counts_only_when_current(self) -> AnchorPolicySelectorRecord:
        live = (self.candidate_count, self.eligible_count, self.candidate_page_sha256)
        if self.authority_state is AnchorPolicyAuthorityState.CURRENT:
            if any(item is None for item in live):
                raise ValueError("current anchor policy rows require live counts")
            assert self.candidate_count is not None and self.eligible_count is not None
            if self.eligible_count > self.candidate_count:
                raise ValueError("anchor policy eligible count exceeds candidates")
        elif any(item is not None for item in live):
            raise ValueError("stale anchor policy rows carry no live counts")
        return self


class AnchorPolicySelectorPage(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-selector-page.v1"] = (
        "traceback.anchor-policy-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_APPROVALS)
    state_head_sha256: Sha256
    records: tuple[AnchorPolicySelectorRecord, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: PolicySelectorId | None
    next_after_approval_version: int | None = Field(
        default=None, ge=1, le=MAX_APPROVAL_VERSION
    )


class AnchorPolicyBackupObject(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-backup-object.v1"] = (
        "traceback.anchor-policy-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class AnchorPolicyBackup(RegistryContract):
    schema_version: Literal["traceback.anchor-policy-backup.v1"] = (
        "traceback.anchor-policy-backup.v1"
    )
    metadata: AnchorPolicyRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_APPROVALS)
    state_head_sha256: Sha256
    journal: tuple[AnchorPolicyJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_APPROVALS
    )
    objects: tuple[AnchorPolicyBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_APPROVALS
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(AnchorPolicyBackup)


def registered_anchor_policy_object_bytes(value: RegisteredAnchorPolicyObject) -> bytes:
    """Return exact bounded canonical bytes for one stored approval object."""

    return exact_model_bytes(
        value,
        RegisteredAnchorPolicyObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_anchor_policy_object_from_bytes(
    content: bytes,
) -> RegisteredAnchorPolicyObject:
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
        value = RegisteredAnchorPolicyObject.model_validate_json(content)
        if registered_anchor_policy_object_bytes(value) != content:
            raise ValueError("registered anchor policy object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered anchor policy object is not canonical") from None


def _canonical_backup_bytes(backup: AnchorPolicyBackup) -> bytes:
    return exact_model_bytes(
        backup,
        AnchorPolicyBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_APPROVALS,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("anchor policy registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("anchor policy registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "anchor policy registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("anchor policy registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("anchor policy registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_hex_selector(value: object, prefix: str) -> bool:
    return (
        type(value) is str
        and len(value) == len(prefix) + 40
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _is_approval_version(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_APPROVAL_VERSION


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
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry object exceeds its bound"
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
            raise AnchorPolicyRegistryUnsafe("anchor policy registry object is unsafe")
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry object digest is invalid"
        )
    return content


def _journal_entry_sha256(entry: AnchorPolicyJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-anchor-policy-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: AnchorPolicyRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-anchor-policy-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> AnchorPolicyJournalEntry:
    placeholder = AnchorPolicyJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return AnchorPolicyJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_selector_versions(
    epoch: str, values: list[RegisteredAnchorPolicyObject]
) -> None:
    """In journal order, each selector's approval versions must append 1, 2, ... N."""

    counts: dict[str, int] = {}
    for value in values:
        if (
            value.cohort_registry_id != values[0].cohort_registry_id
            or value.cohort_registry_epoch_sha256
            != values[0].cohort_registry_epoch_sha256
        ):
            raise ValueError("anchor policy objects bind different cohort registries")
        selector_id = _object_selector_id(epoch, value)
        expected = counts.get(selector_id, 0) + 1
        if value.approval_version != expected:
            raise ValueError("anchor policy approval versions are not contiguous")
        counts[selector_id] = expected


def _validate_backup(backup: AnchorPolicyBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    values: dict[str, RegisteredAnchorPolicyObject] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            value = registered_anchor_policy_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup digest is invalid"
            )
        if (
            value.cohort_registry_id != backup.metadata.cohort_registry_id
            or value.cohort_registry_epoch_sha256
            != backup.metadata.cohort_registry_epoch_sha256
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup binding is invalid"
            )
        sizes[item.object_sha256] = len(content)
        values[item.object_sha256] = value
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup state is invalid"
        )
    try:
        if values:
            _validate_selector_versions(
                backup.metadata.registry_epoch_sha256,
                [values[entry.object_sha256] for entry in backup.journal],
            )
    except ValueError:
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup history is invalid"
        ) from None


def anchor_policy_backup_from_bytes(content: bytes) -> AnchorPolicyBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_APPROVALS,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = AnchorPolicyBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("anchor policy registry backup is not canonical")
    except (TypeError, ValueError):
        raise AnchorPolicyRegistryConflict(
            "anchor policy registry backup is invalid"
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
    linkage_store: object, cohort_registry: object, pins: dict[str, str]
) -> tuple[str, str]:
    """Return the exact D05 registry identity this registry binds, or fail closed."""

    if type(linkage_store) is not ProviderLinkageStore:
        raise TypeError("anchor policy registry requires the exact linkage store type")
    if type(cohort_registry) is not CohortRegistry:
        raise TypeError("anchor policy registry requires the exact cohort registry type")
    try:
        _PINNED_COHORT_INTEGRITY(cohort_registry)
        cohort_state = object.__getattribute__(cohort_registry, "__dict__")
        cohort_pins = capture_expected_trust_pins(cohort_state["_trust_pins"])
    except Exception:
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry D05 authority is invalid"
        ) from None
    if (
        cohort_state.get("_linkage_store") is not linkage_store
        or trust_pins_sha256(cohort_pins) != trust_pins_sha256(pins)
    ):
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry D05 authority does not match"
        )
    page = _PINNED_COHORT_LIST(cohort_registry, limit=1)
    return page.registry_id, page.registry_epoch_sha256


def _registry_instance_snapshot(registry: AnchorPolicyRegistry) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_linkage_store",
        "_cohort_registry",
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
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            AnchorPolicyRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
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
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry authority state changed"
            )
    pins = instance["_trust_pins"]
    if (
        type(pins) is not dict
        or type(instance["_linkage_store"]) is not ProviderLinkageStore
        or type(instance["_cohort_registry"]) is not CohortRegistry
    ):
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        )
    try:
        captured_pins = capture_expected_trust_pins(pins)
    except (TypeError, ValueError):
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        ) from None
    return (
        id(instance["root"]),
        id(instance["_linkage_store"]),
        id(instance["_cohort_registry"]),
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


def _seal_registry_instance(registry: AnchorPolicyRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class AnchorPolicyRegistry:
    """Descriptor-relative immutable anchor-policy approvals with live candidates."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "backup_bytes",
            "close",
            "derive_candidate_page",
            "list_selectors",
            "register_policy",
            "resolve_anchor",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        linkage_store: ProviderLinkageStore,
        cohort_registry: CohortRegistry,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
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
                or len(expected_registry_id) != 48
                or not expected_registry_id.startswith("anchor_registry_")
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_registry_id[16:]
                )
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry expected identity or head is invalid"
                )
        self.root = _snapshot_path(root)
        self._trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        cohort_identity = _bound_authority_identity(
            linkage_store, cohort_registry, self._trust_pins
        )
        self._linkage_store = linkage_store
        self._cohort_registry = cohort_registry
        if _PINNED_ACTIVE_SNAPSHOT(
            self._linkage_store
        ).trust_pins_sha256 != trust_pins_sha256(self._trust_pins):
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry trust pins are invalid"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry root must be private"
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
                raise AnchorPolicyRegistryUnsafe("anchor policy registry root changed")
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry objects are unsafe"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry lock is unsafe"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
                with _AP_LOCK(self, exclusive=True):
                    self._metadata = _AP_LOAD_OR_CREATE_METADATA(
                        self, allow_create=root_created, cohort_identity=cohort_identity
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
                    _AP_RECOVER_TEMPORARY_OBJECTS(self)
                    _, head = _AP_LOAD_STATE(self, check_trusted_head=False)
                    if root_created:
                        if any(item is not None for item in expected_values):
                            raise AnchorPolicyRegistryUnsafe(
                                "new anchor policy registry cannot inherit an "
                                "expected identity"
                            )
                    elif any(item is None for item in expected_values):
                        raise AnchorPolicyRegistryUnsafe(
                            "anchor policy registry expected identity and head are "
                            "required"
                        )
                    if not root_created and expected_values != (
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                        head,
                    ):
                        raise AnchorPolicyRegistryUnsafe(
                            "anchor policy registry expected identity or head is "
                            "invalid"
                        )
                    if staged_root is not None:
                        _commit_staged_root(staged_root, final_root, self._root_fd)
                        self.root = final_root
                    self._trusted_head_sha256 = head
                    _AP_ACCEPT_OBSERVED_HEAD(
                        self, _AP_LOAD_JOURNAL(self), head, check_instance=False
                    )
                    _seal_registry_instance(self)
            # Cleared only after the lock and any fence have exited: until
            # here a failure still removes the new root by inode.
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

    def __enter__(self) -> AnchorPolicyRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _AP_CLOSE(self)

    def __del__(self) -> None:
        try:
            _AP_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _AP_VALIDATE_STORAGE(self)
                yield
                _AP_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    @contextmanager
    def _authority_fence(self, *, exclusive: bool) -> Iterator[None]:
        """Acquire D01 linkage, then the D05 cohort lock, then this registry.

        This is the fixed E12 global order.  D05 public reads take the linkage
        fence themselves and cannot nest inside it, so the D05 history is read
        through D05's public in-fence read while its public read fence holds.
        """

        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            _PINNED_COHORT_INTEGRITY(self._cohort_registry)
            with _PINNED_COHORT_READ_FENCE(self._cohort_registry):
                with _AP_LOCK(self, exclusive=exclusive):
                    yield

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
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
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry storage changed"
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
            raise AnchorPolicyRegistryUnsafe("anchor policy registry storage changed")
        if self._metadata_fd is not None:
            try:
                metadata_path = os.stat(
                    "registry-metadata.json",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                metadata_bound = os.fstat(self._metadata_fd)
            except OSError:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry storage changed"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root or objects directory is always unlinked under the exclusive
        # lock; a directory under that name makes unlink fail, so recovery
        # fails closed.
        if self._root_fd is None or self._objects_fd is None:
            raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
        try:
            for directory_fd in (self._root_fd, self._objects_fd):
                _remove_owned_temporaries(directory_fd)
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self, *, allow_create: bool, cohort_identity: tuple[str, str]
    ) -> AnchorPolicyRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry metadata is missing"
                ) from None
            snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
            metadata = AnchorPolicyRegistryMetadata(
                registry_id=f"anchor_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                linkage_store_id=snapshot.store_id,
                linkage_store_epoch_sha256=snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=snapshot.storage_identity_sha256,
                linkage_trust_pins_sha256=snapshot.trust_pins_sha256,
                cohort_registry_id=cohort_identity[0],
                cohort_registry_epoch_sha256=cohort_identity[1],
            )
            try:
                _AP_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _AP_LOAD_OR_CREATE_METADATA(
                self, allow_create=False, cohort_identity=cohort_identity
            )
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                AnchorPolicyRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
        if (
            metadata.linkage_store_id,
            metadata.linkage_store_epoch_sha256,
            metadata.linkage_storage_identity_sha256,
            metadata.linkage_trust_pins_sha256,
            metadata.cohort_registry_id,
            metadata.cohort_registry_epoch_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
            cohort_identity[0],
            cohort_identity[1],
        ):
            os.close(descriptor)
            self._metadata_fd = None
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry linkage or cohort authority changed"
            )
        return metadata

    def _load_journal(self) -> tuple[AnchorPolicyJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry journal is incomplete"
            )
        entries: list[AnchorPolicyJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_APPROVALS:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(AnchorPolicyJournalEntry, line)
            except Exception:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: AnchorPolicyJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry journal append failed"
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
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[AnchorPolicyJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredAnchorPolicyObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise AnchorPolicyRegistryUnsafe("anchor policy registry is closed")
        journal = _AP_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_APPROVALS + 1:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredAnchorPolicyObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry journal binding is invalid"
                )
            try:
                value = registered_anchor_policy_object_from_bytes(content)
            except ValueError:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry object is invalid"
                ) from None
            if (
                value.cohort_registry_id != self._metadata.cohort_registry_id
                or value.cohort_registry_epoch_sha256
                != self._metadata.cohort_registry_epoch_sha256
            ):
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry object binding is invalid"
                )
            loaded[entry.object_sha256] = (value, content)
        try:
            if loaded:
                _validate_selector_versions(
                    self._metadata.registry_epoch_sha256,
                    [loaded[entry.object_sha256][0] for entry in journal],
                )
        except ValueError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry history is invalid"
            ) from None
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _AP_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry state rollback detected"
                )
        return loaded, head

    def _derive_candidates_in_fence(
        self, value: RegisteredAnchorPolicyObject, object_sha256: str
    ) -> tuple[tuple[AnchorCandidate, LongitudinalRecord], ...]:
        """Derive admitted candidates from the approval and live D01/D05 authority.

        The caller must hold this registry's authority fence.  A candidate is
        admitted only when it is exactly one member of the live D05 cohort
        version (linkage revision and committed receipt) and the pinned D03
        evaluator, run with the candidate as its own anchor, rejects neither the
        policy, the anchor identity, nor the live linkage.  The only tolerated
        D03 failure is the result state, which the page exposes as ineligible.
        """

        try:
            history = _PINNED_COHORT_RESOLVE_IN_FENCE(
                self._cohort_registry, value.cohort_selector_id, value.cohort_version
            )
        except CohortRegistryError:
            raise AnchorPolicyRegistryStale(
                "anchor policy cohort selection is not current"
            ) from None
        if (
            history.registry_id != self._metadata.cohort_registry_id
            or history.registry_epoch_sha256
            != self._metadata.cohort_registry_epoch_sha256
            or history.selected_manifest_sha256 != value.cohort_manifest_sha256
        ):
            raise AnchorPolicyRegistryStale(
                "anchor policy cohort selection is not current"
            )
        manifest = history.manifests[-1]
        timepoints = sorted(
            {(item.time_coordinate, item.biological_timepoint_id) for item in manifest.members}
        )
        ordinals = {key: index for index, key in enumerate(timepoints, start=1)}
        origin = timepoints[0][0]
        members: dict[tuple[str, str, int], list[object]] = {}
        for member in manifest.members:
            members.setdefault(
                (member.provider_namespace, member.linkage_id, member.linkage_revision),
                [],
            ).append(member)
        epoch = self._metadata.registry_epoch_sha256
        admitted: list[tuple[AnchorCandidate, LongitudinalRecord]] = []
        for record in value.anchor_candidates:
            revision = record.linkage_revision
            receipt = record.activation_receipt
            assert receipt is not None
            matches = [
                member
                for member in members.get(
                    (revision.provider_namespace, revision.linkage_id, revision.revision),
                    [],
                )
                if member.linkage_revision_sha256 == linkage_revision_sha256(revision)
                and member.committed_receipt_sha256
                == committed_linkage_receipt_sha256(receipt)
            ]
            if len(matches) != 1:
                continue
            member = matches[0]
            record_sha256 = longitudinal_record_sha256(record)
            decision = _PINNED_DECIDE_MEMBER(
                record,
                record,
                value.policy,
                expected_policy_sha256=value.policy_sha256,
                expected_authority_head_sha256=value.expected_authority_head_sha256,
                expected_linkage_trust_snapshot_sha256_by_provider=dict(
                    self._trust_pins
                ),
                linkage_store=self._linkage_store,
            )
            if (
                decision.anchor_record_sha256 != record_sha256
                or decision.member_record_sha256 != record_sha256
                or decision.policy_sha256 != value.policy_sha256
                or decision.anchor_key_sha256 != value.policy.anchor_key_sha256
            ):
                continue
            if decision.outcome is LongitudinalOutcome.EQUIVALENT and (
                decision.reason_codes == (LongitudinalReason.EXACT_MATCH,)
            ):
                eligibility = AnchorEligibilityState.ELIGIBLE
            elif decision.outcome is LongitudinalOutcome.UNKNOWN and (
                decision.reason_codes == (LongitudinalReason.RESULT_STATE_INVALID,)
            ):
                eligibility = AnchorEligibilityState.RESULT_STATE_INELIGIBLE
            else:
                continue
            anchor_selector_id = _AP_ANCHOR_SELECTOR_ID(epoch, object_sha256, record_sha256)
            admitted.append(
                (
                    _AP_CANDIDATE(
                        anchor_selector_id=anchor_selector_id,
                        alias=_candidate_alias(anchor_selector_id),
                        biological_timepoint_ordinal=ordinals[
                            (member.time_coordinate, member.biological_timepoint_id)
                        ],
                        time_offset_seconds=member.time_coordinate - origin,
                        method_version=record.measurement.method.method_ref.version,
                        eligibility_state=eligibility,
                    ),
                    record,
                )
            )
        if not admitted:
            raise AnchorPolicyRegistryStale(
                "anchor policy has no live admitted candidate"
            )
        return tuple(sorted(admitted, key=lambda item: _candidate_order(item[0])))

    def _build_page(
        self,
        selector_id: str,
        digest: str,
        value: RegisteredAnchorPolicyObject,
        candidates: tuple[AnchorCandidate, ...],
        *,
        state_version: int,
        state_head_sha256: str,
    ) -> AnchorCandidatePage:
        identity = {
            "registry_id": self._metadata.registry_id,
            "registry_epoch_sha256": self._metadata.registry_epoch_sha256,
            "policy_selector_id": selector_id,
            "approval_version": value.approval_version,
            "object_sha256": digest,
            "policy_sha256": value.policy_sha256,
            "envelope_sha256": value.envelope_sha256,
            "anchor_key_sha256": value.policy.anchor_key_sha256,
            "cohort_manifest_sha256": value.cohort_manifest_sha256,
        }
        return _AP_CANDIDATE_PAGE(
            **identity,
            state_version=state_version,
            state_head_sha256=state_head_sha256,
            candidates=candidates,
            candidate_page_sha256=_AP_CANDIDATE_PAGE_SHA256(
                **identity, candidates=candidates
            ),
        )

    def register_policy(
        self,
        cohort_selector_id: str,
        cohort_version: int,
        policy: LongitudinalAnchorPolicy,
        envelope: RepeatabilityEnvelope,
        anchor_candidates: tuple[LongitudinalRecord, ...],
        *,
        approval_version: int,
        expected_cohort_manifest_sha256: str,
        expected_policy_sha256: str,
        expected_envelope_sha256: str,
        expected_authority_head_sha256: str,
    ) -> AnchorPolicyRegistrationReceipt:
        """Approve one D03 policy and D07 envelope for one live D05 selection.

        The registrant supplies the D05 selection, the exact policy, envelope,
        approved anchor-candidate records, approval version, and independent
        pins.  Every candidate must be admitted by the policy under live linkage
        and be a member of the live cohort version at registration.
        """

        _require_registry_integrity(self)
        if (
            not _is_hex_selector(cohort_selector_id, "cohort_selector_")
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
            or not _is_approval_version(approval_version)
            or not _is_sha256(expected_cohort_manifest_sha256)
            or not _is_sha256(expected_policy_sha256)
            or not _is_sha256(expected_envelope_sha256)
            or not _is_sha256(expected_authority_head_sha256)
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registration selection or pins are invalid"
            )
        if (
            type(anchor_candidates) is not tuple
            or not 1 <= len(anchor_candidates) <= MAX_ANCHOR_CANDIDATES
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor candidates must be one bounded exact tuple"
            )
        try:
            ordered = tuple(
                sorted(anchor_candidates, key=lambda item: longitudinal_record_sha256(item))
            )
            captured = RegisteredAnchorPolicyObject(
                cohort_registry_id=self._metadata.cohort_registry_id,
                cohort_registry_epoch_sha256=(
                    self._metadata.cohort_registry_epoch_sha256
                ),
                cohort_selector_id=cohort_selector_id,
                cohort_version=cohort_version,
                cohort_manifest_sha256=expected_cohort_manifest_sha256,
                approval_version=approval_version,
                policy=policy,
                policy_sha256=expected_policy_sha256,
                envelope=envelope,
                envelope_sha256=expected_envelope_sha256,
                expected_authority_head_sha256=expected_authority_head_sha256,
                anchor_candidates=ordered,
            )
            content = registered_anchor_policy_object_bytes(captured)
            captured = registered_anchor_policy_object_from_bytes(content)
        except Exception:
            raise AnchorPolicyRegistryConflict(
                "anchor policy inputs are not exact canonical contracts"
            ) from None
        digest = hashlib.sha256(content).hexdigest()
        epoch = self._metadata.registry_epoch_sha256
        selector_id = _AP_SELECTOR_ID(
            epoch,
            captured.cohort_registry_id,
            captured.cohort_selector_id,
            captured.cohort_version,
            captured.policy.policy_id,
        )
        with _AP_AUTHORITY_FENCE(self, exclusive=True):
            try:
                admitted = _AP_DERIVE_CANDIDATES(self, captured, digest)
            except AnchorPolicyRegistryStale:
                raise AnchorPolicyRegistryConflict(
                    "anchor policy is not admitted by the live selection"
                ) from None
            if len(admitted) != len(captured.anchor_candidates):
                raise AnchorPolicyRegistryConflict(
                    "every approved anchor candidate must be live and admitted"
                )
            _AP_RECOVER_TEMPORARY_OBJECTS(self)
            loaded, head = _AP_LOAD_STATE(self)
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
                    raise AnchorPolicyRegistryConflict(
                        "anchor policy object digest conflicts"
                    )
            else:
                versions = sorted(
                    item.approval_version
                    for item, _ in loaded.values()
                    if _object_selector_id(epoch, item) == selector_id
                )
                if captured.approval_version in versions:
                    raise AnchorPolicyRegistryConflict(
                        "anchor policy approval version is already registered with "
                        "other content"
                    )
                if captured.approval_version != len(versions) + 1:
                    raise AnchorPolicyRegistryConflict(
                        "anchor policy approval version must extend its selector history"
                    )
                if len(loaded) >= MAX_REGISTERED_APPROVALS:
                    raise AnchorPolicyRegistryConflict(
                        "anchor policy registry is full"
                    )
                if (
                    sum(len(item[1]) for item in loaded.values()) + len(content)
                    > MAX_TOTAL_OBJECT_BYTES
                ):
                    raise AnchorPolicyRegistryConflict(
                        "anchor policy registry byte bound would be exceeded"
                    )
                try:
                    _AP_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                except FileExistsError:
                    if _read_exact_object(self._objects_fd, digest) != content:
                        raise AnchorPolicyRegistryConflict(
                            "anchor policy publication conflicts"
                        ) from None
                _AP_APPEND_JOURNAL(
                    self,
                    _build_journal_entry(
                        sequence=len(loaded) + 1,
                        previous_entry_sha256=head,
                        object_sha256=digest,
                        object_bytes=len(content),
                    ),
                )
            final, final_head = _AP_LOAD_STATE(self)
            if digest not in final or final[digest][1] != content:
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy publication is unproven"
                )
            return _AP_RECEIPT(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(final),
                state_head_sha256=final_head,
                selector_id=selector_id,
                approval_version=captured.approval_version,
                object_sha256=digest,
                policy_sha256=captured.policy_sha256,
                envelope_sha256=captured.envelope_sha256,
                anchor_key_sha256=captured.policy.anchor_key_sha256,
                candidate_count=len(captured.anchor_candidates),
            )

    def _select_object(
        self,
        loaded: dict[str, tuple[RegisteredAnchorPolicyObject, bytes]],
        selector_id: str,
        approval_version: int,
    ) -> tuple[str, RegisteredAnchorPolicyObject]:
        epoch = self._metadata.registry_epoch_sha256
        matches = [
            (digest, value)
            for digest, (value, _) in loaded.items()
            if value.approval_version == approval_version
            and _AP_SELECTOR_ID(
                epoch,
                value.cohort_registry_id,
                value.cohort_selector_id,
                value.cohort_version,
                value.policy.policy_id,
            )
            == selector_id
        ]
        if len(matches) != 1:
            raise AnchorPolicyRegistryConflict(
                "anchor policy selector or version is unavailable"
            )
        return matches[0]

    def derive_candidate_page(
        self, selector_id: str, approval_version: int
    ) -> AnchorCandidatePage:
        """Return the bounded live candidate page for one approval; never a cache."""

        _require_registry_integrity(self)
        if not _is_hex_selector(selector_id, "anchor_policy_") or not (
            _is_approval_version(approval_version)
        ):
            raise AnchorPolicyRegistryConflict("anchor policy selector is invalid")
        with _AP_AUTHORITY_FENCE(self, exclusive=False):
            loaded, head = _AP_LOAD_STATE(self)
            digest, value = _AP_SELECT_OBJECT(self, loaded, selector_id, approval_version)
            admitted = _AP_DERIVE_CANDIDATES(self, value, digest)
            return _AP_BUILD_PAGE(
                self,
                selector_id,
                digest,
                value,
                tuple(item[0] for item in admitted),
                state_version=len(loaded),
                state_head_sha256=head,
            )

    def resolve_anchor(
        self,
        selector_id: str,
        approval_version: int,
        anchor_selector_id: str,
        *,
        expected_candidate_page_sha256: str,
    ) -> ResolvedApprovedAnchor:
        """Resolve one explicit opaque anchor selector against the exact live page.

        The page is re-derived under the authority fence and must equal the page
        the selection was made from; a changed page, an unknown, injected,
        cross-policy or cross-registry selector, or an ineligible candidate fails
        closed rather than selecting another anchor.
        """

        _require_registry_integrity(self)
        if (
            not _is_hex_selector(selector_id, "anchor_policy_")
            or not _is_approval_version(approval_version)
            or not _is_hex_selector(anchor_selector_id, "anchor_candidate_")
            or not _is_sha256(expected_candidate_page_sha256)
        ):
            raise AnchorPolicyRegistryConflict("anchor selection is invalid")
        with _AP_AUTHORITY_FENCE(self, exclusive=False):
            loaded, head = _AP_LOAD_STATE(self)
            digest, value = _AP_SELECT_OBJECT(self, loaded, selector_id, approval_version)
            admitted = _AP_DERIVE_CANDIDATES(self, value, digest)
            page = _AP_BUILD_PAGE(
                self,
                selector_id,
                digest,
                value,
                tuple(item[0] for item in admitted),
                state_version=len(loaded),
                state_head_sha256=head,
            )
            if page.candidate_page_sha256 != expected_candidate_page_sha256:
                raise AnchorPolicyRegistryStale(
                    "anchor candidate page changed since selection"
                )
            matches = [
                item for item in admitted if item[0].anchor_selector_id == anchor_selector_id
            ]
            if len(matches) != 1:
                raise AnchorPolicyRegistryConflict(
                    "anchor selector is not an approved candidate of this page"
                )
            candidate, record = matches[0]
            if candidate.eligibility_state is not AnchorEligibilityState.ELIGIBLE:
                raise AnchorPolicyRegistryConflict("anchor candidate is not eligible")
            return _AP_RESOLVED(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                state_version=len(loaded),
                state_head_sha256=head,
                policy_selector_id=selector_id,
                approval_version=value.approval_version,
                object_sha256=digest,
                cohort_registry_id=value.cohort_registry_id,
                cohort_registry_epoch_sha256=value.cohort_registry_epoch_sha256,
                cohort_selector_id=value.cohort_selector_id,
                cohort_version=value.cohort_version,
                cohort_manifest_sha256=value.cohort_manifest_sha256,
                policy=value.policy,
                policy_sha256=value.policy_sha256,
                envelope=value.envelope,
                envelope_sha256=value.envelope_sha256,
                expected_authority_head_sha256=value.expected_authority_head_sha256,
                candidate_page_sha256=page.candidate_page_sha256,
                candidate_page=page,
                candidate=candidate,
                anchor_record=record,
                anchor_record_sha256=longitudinal_record_sha256(record),
            )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        after_approval_version: int | None = None,
        limit: int = 50,
    ) -> AnchorPolicySelectorPage:
        """Return one bounded privacy-safe page with live authority state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise AnchorPolicyRegistryConflict(
                "anchor policy selector page bound is invalid"
            )
        if (after_selector_id is None) != (after_approval_version is None):
            raise AnchorPolicyRegistryConflict(
                "anchor policy selector cursor is incomplete"
            )
        if after_selector_id is not None and (
            not _is_hex_selector(after_selector_id, "anchor_policy_")
            or not _is_approval_version(after_approval_version)
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy selector cursor is invalid"
            )
        with _AP_AUTHORITY_FENCE(self, exclusive=False):
            loaded, head = _AP_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            ordered = sorted(
                (
                    _AP_SELECTOR_ID(
                        epoch,
                        value.cohort_registry_id,
                        value.cohort_selector_id,
                        value.cohort_version,
                        value.policy.policy_id,
                    ),
                    value.approval_version,
                    digest,
                    value,
                )
                for digest, (value, _) in loaded.items()
            )
            if after_selector_id is not None:
                ordered = [
                    item
                    for item in ordered
                    if (item[0], item[1]) > (after_selector_id, after_approval_version)
                ]
            selected = ordered[:limit]
            rows: list[AnchorPolicySelectorRecord] = []
            for selector_id, version, digest, value in selected:
                live: dict[str, object] = {}
                try:
                    admitted = _AP_DERIVE_CANDIDATES(self, value, digest)
                except AnchorPolicyRegistryStale:
                    state = AnchorPolicyAuthorityState.STALE
                else:
                    state = AnchorPolicyAuthorityState.CURRENT
                    page = _AP_BUILD_PAGE(
                        self,
                        selector_id,
                        digest,
                        value,
                        tuple(item[0] for item in admitted),
                        state_version=len(loaded),
                        state_head_sha256=head,
                    )
                    live = {
                        "candidate_count": len(page.candidates),
                        "eligible_count": sum(
                            item.eligibility_state is AnchorEligibilityState.ELIGIBLE
                            for item in page.candidates
                        ),
                        "candidate_page_sha256": page.candidate_page_sha256,
                    }
                rows.append(
                    _AP_SELECTOR_RECORD(
                        selector_id=selector_id,
                        approval_version=version,
                        object_sha256=digest,
                        policy_sha256=value.policy_sha256,
                        envelope_sha256=value.envelope_sha256,
                        anchor_key_sha256=value.policy.anchor_key_sha256,
                        cohort_manifest_sha256=value.cohort_manifest_sha256,
                        authority_state=state,
                        **live,
                    )
                )
            more = len(ordered) > len(selected)
            return _AP_SELECTOR_PAGE(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                records=tuple(rows),
                next_after_selector_id=(rows[-1].selector_id if more and rows else None),
                next_after_approval_version=(
                    rows[-1].approval_version if more and rows else None
                ),
            )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _AP_LOCK(self, exclusive=False):
            loaded, head = _AP_LOAD_STATE(self)
            backup = AnchorPolicyBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_AP_LOAD_JOURNAL(self),
                objects=tuple(
                    AnchorPolicyBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise AnchorPolicyRegistryConflict(
                    "anchor policy registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        linkage_store: ProviderLinkageStore,
        cohort_registry: CohortRegistry,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> AnchorPolicyRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        backup = anchor_policy_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup expected head is invalid"
            )
        pins = capture_expected_trust_pins(expected_trust_snapshot_sha256_by_provider)
        cohort_identity = _bound_authority_identity(linkage_store, cohort_registry, pins)
        snapshot = _PINNED_ACTIVE_SNAPSHOT(linkage_store)
        if snapshot.trust_pins_sha256 != trust_pins_sha256(pins) or (
            backup.metadata.linkage_store_id,
            backup.metadata.linkage_store_epoch_sha256,
            backup.metadata.linkage_storage_identity_sha256,
            backup.metadata.linkage_trust_pins_sha256,
            backup.metadata.cohort_registry_id,
            backup.metadata.cohort_registry_epoch_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
            cohort_identity[0],
            cohort_identity[1],
        ):
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry backup authority is invalid"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry restore parent changed"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry restore root changed"
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
                raise AnchorPolicyRegistryUnsafe(
                    "anchor policy registry restore objects changed"
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
            restored = _AP_CONSTRUCT(
                target,
                linkage_store=linkage_store,
                cohort_registry=cohort_registry,
                expected_trust_snapshot_sha256_by_provider=pins,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise AnchorPolicyRegistryConflict(
                "anchor policy registry restore target already exists"
            ) from None
        except OSError:
            raise AnchorPolicyRegistryUnsafe(
                "anchor policy registry restore failed"
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
        the exclusive registry lock without waiting (a registry in use,
        including the caller's own fence, is refused by the non-blocking
        flock) and truncates only the bytes after the
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
                AnchorPolicyRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                AnchorPolicyJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=4 * 1024 * 1024,
            max_entries=MAX_REGISTERED_APPROVALS,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=AnchorPolicyRegistryUnsafe,
            label="anchor policy registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: AnchorPolicyRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_lock",
            "_authority_fence",
            "_validate_storage",
            "_publish",
            "_recover_temporary_objects",
            "_load_or_create_metadata",
            "_load_journal",
            "_append_journal",
            "_accept_observed_head",
            "_load_state",
            "_derive_candidates_in_fence",
            "_build_page",
            "_select_object",
            "register_policy",
            "derive_candidate_page",
            "resolve_anchor",
            "list_selectors",
            "backup_bytes",
            "restore",
            "recover_torn_journal_tail",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not AnchorPolicyRegistry or any(
        AnchorPolicyRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise AnchorPolicyRegistryUnsafe("anchor policy registry callable changed")


def _require_registry_integrity(registry: AnchorPolicyRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise AnchorPolicyRegistryUnsafe("anchor policy registry callable changed")
    authority_sources = {
        "_PINNED_AUTHORITY_READ_FENCE": ProviderLinkageStore.authority_read_fence,
        "_PINNED_ACTIVE_SNAPSHOT": ProviderLinkageStore.active_snapshot,
        "_PINNED_DECIDE_MEMBER": decide_longitudinal_member,
        "_PINNED_COHORT_LIST": CohortRegistry.list_selectors,
        "_PINNED_COHORT_READ_FENCE": CohortRegistry.authority_read_fence,
        "_PINNED_COHORT_RESOLVE_IN_FENCE": CohortRegistry.resolve_history_in_fence,
        "_PINNED_COHORT_INTEGRITY": d05_module._require_registry_integrity,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority callable changed"
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
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise AnchorPolicyRegistryUnsafe(
            "anchor policy registry authority state changed"
        )


_AP_CONSTRUCT = AnchorPolicyRegistry
_AP_CLOSE = AnchorPolicyRegistry.close
_AP_LOCK = AnchorPolicyRegistry._lock
_AP_AUTHORITY_FENCE = AnchorPolicyRegistry._authority_fence
_AP_VALIDATE_STORAGE = AnchorPolicyRegistry._validate_storage
_AP_PUBLISH = AnchorPolicyRegistry._publish
_AP_RECOVER_TEMPORARY_OBJECTS = AnchorPolicyRegistry._recover_temporary_objects
_AP_LOAD_OR_CREATE_METADATA = AnchorPolicyRegistry._load_or_create_metadata
_AP_LOAD_JOURNAL = AnchorPolicyRegistry._load_journal
_AP_APPEND_JOURNAL = AnchorPolicyRegistry._append_journal
_AP_ACCEPT_OBSERVED_HEAD = AnchorPolicyRegistry._accept_observed_head
_AP_LOAD_STATE = AnchorPolicyRegistry._load_state
_AP_DERIVE_CANDIDATES = AnchorPolicyRegistry._derive_candidates_in_fence
_AP_BUILD_PAGE = AnchorPolicyRegistry._build_page
_AP_SELECT_OBJECT = AnchorPolicyRegistry._select_object
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another approval's candidate.
_AP_RECEIPT = AnchorPolicyRegistrationReceipt
_AP_RESOLVED = ResolvedApprovedAnchor
_AP_CANDIDATE = AnchorCandidate
_AP_CANDIDATE_PAGE = AnchorCandidatePage
_AP_CANDIDATE_PAGE_SHA256 = _candidate_page_sha256
_AP_SELECTOR_RECORD = AnchorPolicySelectorRecord
_AP_SELECTOR_PAGE = AnchorPolicySelectorPage
_AP_SELECTOR_ID = _selector_id
_AP_ANCHOR_SELECTOR_ID = _anchor_selector_id
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_AUTHORITY_READ_FENCE": _PINNED_AUTHORITY_READ_FENCE,
        "_PINNED_ACTIVE_SNAPSHOT": _PINNED_ACTIVE_SNAPSHOT,
        "_PINNED_DECIDE_MEMBER": _PINNED_DECIDE_MEMBER,
        "_PINNED_COHORT_LIST": _PINNED_COHORT_LIST,
        "_PINNED_COHORT_READ_FENCE": _PINNED_COHORT_READ_FENCE,
        "_PINNED_COHORT_RESOLVE_IN_FENCE": _PINNED_COHORT_RESOLVE_IN_FENCE,
        "_PINNED_COHORT_INTEGRITY": _PINNED_COHORT_INTEGRITY,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_AP_CONSTRUCT",
            "_AP_CLOSE",
            "_AP_LOCK",
            "_AP_AUTHORITY_FENCE",
            "_AP_VALIDATE_STORAGE",
            "_AP_PUBLISH",
            "_AP_RECOVER_TEMPORARY_OBJECTS",
            "_AP_LOAD_OR_CREATE_METADATA",
            "_AP_LOAD_JOURNAL",
            "_AP_APPEND_JOURNAL",
            "_AP_ACCEPT_OBSERVED_HEAD",
            "_AP_LOAD_STATE",
            "_AP_DERIVE_CANDIDATES",
            "_AP_BUILD_PAGE",
            "_AP_SELECT_OBJECT",
            "_AP_RECEIPT",
            "_AP_RESOLVED",
            "_AP_CANDIDATE",
            "_AP_CANDIDATE_PAGE",
            "_AP_CANDIDATE_PAGE_SHA256",
            "_AP_SELECTOR_RECORD",
            "_AP_SELECTOR_PAGE",
            "_AP_SELECTOR_ID",
            "_AP_ANCHOR_SELECTOR_ID",
        )
    }
)


__all__ = [
    "MAX_ANCHOR_CANDIDATES",
    "AnchorCandidate",
    "AnchorCandidatePage",
    "AnchorEligibilityState",
    "AnchorPolicyAuthorityState",
    "AnchorPolicyBackup",
    "AnchorPolicyBackupObject",
    "AnchorPolicyJournalEntry",
    "AnchorPolicyRegistrationReceipt",
    "AnchorPolicyRegistry",
    "AnchorPolicyRegistryConflict",
    "AnchorPolicyRegistryError",
    "AnchorPolicyRegistryMetadata",
    "AnchorPolicyRegistryStale",
    "AnchorPolicyRegistryUnsafe",
    "AnchorPolicySelectorPage",
    "AnchorPolicySelectorRecord",
    "RegisteredAnchorPolicyObject",
    "ResolvedApprovedAnchor",
    "anchor_policy_backup_from_bytes",
    "registered_anchor_policy_object_bytes",
    "registered_anchor_policy_object_from_bytes",
]
