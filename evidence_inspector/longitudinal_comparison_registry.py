"""Protected durable registry for saved E12 longitudinal comparisons.

The registry stores one immutable, content-addressed
``SavedLongitudinalComparisonV1`` per (opaque selector, comparison version).
It is append-only and hash-chained.  Publication never overwrites: an exact
retry is idempotent, and the same selector/version with other bytes, or a
retry after any dependency authority moved, is a conflict.

Every operation that depends on other stores runs inside a caller-supplied
``SavedComparisonDependencyFence``.  The production fence is
``evidence_inspector.composite_authority_fence.CompositeAuthorityFence``: its
``hold()`` acquires every dependency store's read fence in the documented
global order before this registry takes its own lock (it is last in that
order), and its ``read_heads`` serves heads captured under those held fences,
so reading heads inside this registry's lock takes no other store lock.

A fence that re-reads dependency heads through the stores' own public reads
(the retired ``direct_head_reread`` kind) would take every other store's lock
*inside* this registry's lock, the reverse of the global order, and can
deadlock a composite hold.  Every operation refuses that kind.

Publication writes a durable candidate/recovery record before the object,
then the object, then one journal entry (the commit point).  Startup and the
next operation recover an interrupted publication from that record alone:
exact committed bytes are adopted; otherwise only the candidate's own object
and torn journal suffix are removed.

Reopen returns the immutable saved bytes plus ``current`` or ``stale``.  A
saved digest or receipt is never current authority: current workspace values
and segments always need a fresh E12 replay.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.cohort_import import (
    CohortRecordAvailability,
)
from evidence_inspector.cohort_manifest import MemberLineageRole
from evidence_inspector.longitudinal_compatibility import LongitudinalOutcome
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.projection_policy_registry import (
    _STATISTIC_UNIT as _PROJECTION_STATISTIC_UNIT,
)
from evidence_inspector.projection_policy_registry import (
    MAX_FINITE_COMPONENTS,
    CellOriginStatistic,
    CnaChromosomeStatistic,
    CnaSegmentStatistic,
    FragmentStatistic,
    ProjectionFamily,
    ProjectionSelectionRule,
    StatisticUnit,
)
from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
)
from evidence_inspector.result_view import NormalizedResultFilters
from evidence_inspector.result_view_source_registry import (
    MAX_SOURCE_VERSIONS,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

# Bounds from the E12 plan ("Durable saved-comparison registry prerequisite").
MAX_SAVED_COMPARISONS = 1_000
MAX_OBJECT_BYTES = 512 * 1024
MAX_RECOVERY_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_SELECTOR_PAGE = 100
MAX_BACKUP_BYTES = 520 * 1024 * 1024
MAX_METADATA_BYTES = 4096
MAX_COHORT_MEMBERS = 1_000
MAX_COHORT_VERSION = 100_000
MAX_SELECTOR_VERSION = 100_000
MAX_TIMEPOINT_ORDINAL = MAX_COHORT_MEMBERS - 1
# Explicit pre-serialization limits for one saved object.
MAX_OBJECT_GRAPH_DEPTH = 16
MAX_OBJECT_GRAPH_NODES = 200_000
MAX_OBJECT_COLLECTION_ITEMS = MAX_COHORT_MEMBERS
MAX_OBJECT_STRING_BYTES = 256
MAX_JOURNAL_ENTRY_GRAPH_NODES = 512
MAX_BACKUP_HEADER_BYTES = MAX_JOURNAL_BYTES + 512 * 1024
MAX_BACKUP_HEADER_NODES = MAX_SAVED_COMPARISONS * MAX_JOURNAL_ENTRY_GRAPH_NODES + 4096
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256

_BACKUP_MAGIC = b"traceback-saved-comparison-backup-v1\n"
_REGISTRY_ID_PREFIX = "saved_comparison_registry_"
_SELECTOR_PREFIX = "saved_comparison_"
_CANDIDATE_NAME = "publication-candidate.json"
_METADATA_NAME = "registry-metadata.json"
_JOURNAL_NAME = "registry-journal.jsonl"
_LOCK_NAME = ".registry.lock"
_OBJECTS_NAME = "objects"
_ROOT_NAMES = frozenset(
    {_LOCK_NAME, _METADATA_NAME, _JOURNAL_NAME, _OBJECTS_NAME, _CANDIDATE_NAME}
)

_REGISTRY_PROCESS_LOCK = threading.RLock()
# Keyed by registry identity alone, not by root inode: a restored copy of the
# same registry in another directory is held to the same forward-only head.
_REGISTRY_PROCESS_HEADS: dict[tuple[str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_LOCK_DEPTH = threading.local()

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RegistryId = Annotated[
    str, StringConstraints(pattern=r"^saved_comparison_registry_[0-9a-f]{32}$")
]
SavedSelectorId = Annotated[
    str, StringConstraints(pattern=r"^saved_comparison_[0-9a-f]{40}$")
]
DependencyRegistryId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,40}_[0-9a-f]{32}$")
]
CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
AnchorPolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_policy_[0-9a-f]{40}$")
]
AnchorCandidateSelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_candidate_[0-9a-f]{40}$")
]
ProjectionPolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^projection_policy_[0-9a-f]{40}$")
]
D09PolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^d09_policy_[0-9a-f]{40}$")
]
E06SourceSelectorId = Annotated[
    str, StringConstraints(pattern=r"^e06_source_[0-9a-f]{40}$")
]


class LongitudinalComparisonRegistryError(RuntimeError):
    """Sanitized saved-comparison registry failure with a controlled code."""

    code = "storage_failure"


class LongitudinalComparisonRegistryConflict(LongitudinalComparisonRegistryError):
    """The request conflicts with committed state or is not exact."""

    code = "invalid_request"


class LongitudinalComparisonRegistryStale(LongitudinalComparisonRegistryConflict):
    """Dependency authority moved; nothing was presented as current."""

    code = "authority_stale"


class LongitudinalComparisonRegistryReadConflict(LongitudinalComparisonRegistryError):
    """Authority or state moved during one read; retry the whole read."""

    code = "read_conflict"


class LongitudinalComparisonRegistryUnsafe(LongitudinalComparisonRegistryError):
    """Storage, recovery, rollback or process-integrity failure; fail closed."""

    code = "integrity_failure"


class SavedComparisonAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class DependencyFenceKind(StrEnum):
    """How the dependency heads were fenced during one operation.

    ``composite_authority_fence`` is the only kind production accepts.
    ``direct_head_reread`` is retired and always refused: re-reading heads
    through the stores' public reads takes their locks inside this registry's
    lock, the reverse of the global order.  ``test_only_unfenced`` exists
    only for this registry's own unit tests and is refused unless
    ``_TEST_ONLY_FENCE_ALLOWED`` is set (tests only; in-process mutation is
    outside the threat model).
    """

    DIRECT_HEAD_REREAD = "direct_head_reread"
    COMPOSITE_AUTHORITY_FENCE = "composite_authority_fence"
    TEST_ONLY_UNFENCED = "test_only_unfenced"


# Never set outside tests.
_TEST_ONLY_FENCE_ALLOWED = False


class DependencySlot(StrEnum):
    """Closed dependency slots, in the plan's global lock order."""

    D01_LINKAGE = "d01_linkage"
    D04_HISTORY = "d04_history"
    D05_COHORT = "d05_cohort"
    READER_AUTHORIZATION = "reader_authorization"
    D06_RECORD_CATALOG = "d06_record_catalog"
    E04_CATALOG = "e04_catalog"
    RESULT_TRUST = "result_trust"
    E06_SOURCE = "e06_source"
    D03_DECISION = "d03_decision"
    D07_COMPARISON = "d07_comparison"
    D09_SUMMARY = "d09_summary"
    D10_CONTEXT = "d10_context"
    FAMILY_SOURCE = "family_source"
    ANCHOR_POLICY = "anchor_policy"
    PROJECTION_POLICY = "projection_policy"


# Exact registry-ID prefix per slot, from each merged store's own ID type.
# ``e04_catalog_`` is derived here (E04 exposes no registry ID): the storage
# identity prefix.  The family-source slot is optional in v1 and accepts any
# ``<prefix>_<32 hex>`` ID.
_SLOT_ID_PREFIXES: MappingProxyType[DependencySlot, str | None] = MappingProxyType(
    {
        DependencySlot.D01_LINKAGE: "store_",
        DependencySlot.D04_HISTORY: "ledger_",
        DependencySlot.D05_COHORT: "cohort_registry_",
        DependencySlot.READER_AUTHORIZATION: "reader_registry_",
        DependencySlot.D06_RECORD_CATALOG: "cohort_registry_",
        DependencySlot.E04_CATALOG: "e04_catalog_",
        DependencySlot.RESULT_TRUST: "result_trust_registry_",
        DependencySlot.E06_SOURCE: "e06_registry_",
        DependencySlot.D03_DECISION: "d03_registry_",
        DependencySlot.D07_COMPARISON: "d07_registry_",
        DependencySlot.D09_SUMMARY: "d09_registry_",
        DependencySlot.D10_CONTEXT: "d10_registry_",
        DependencySlot.FAMILY_SOURCE: None,
        DependencySlot.ANCHOR_POLICY: "anchor_registry_",
        DependencySlot.PROJECTION_POLICY: "projection_registry_",
    }
)


class DependencyHeadV1(RegistryContract):
    """One dependency store's exact ID, epoch and head at one read.

    Field names are short on purpose: 1,000 journal entries, each carrying the
    whole vector, must fit the 4 MiB journal bound.
    """

    id: DependencyRegistryId
    epoch: Sha256
    head: Sha256


class SavedComparisonDependencyHeadsV1(RegistryContract):
    """Closed dependency-head vector, one slot per authority in the lock order.

    ``family_source`` is the only optional slot: the family-source artifact
    registry is not merged yet.  Schema v1 accepts ``None`` there; when that
    registry merges, the live fence fills the slot and every vector read
    before then compares unequal, so older saves reopen as ``stale``.
    """

    schema_version: Literal["traceback.saved-comparison-dependency-heads.v1"] = (
        "traceback.saved-comparison-dependency-heads.v1"
    )
    d01_linkage: DependencyHeadV1
    d04_history: DependencyHeadV1
    d05_cohort: DependencyHeadV1
    reader_authorization: DependencyHeadV1
    d06_record_catalog: DependencyHeadV1
    e04_catalog: DependencyHeadV1
    result_trust: DependencyHeadV1
    e06_source: DependencyHeadV1
    d03_decision: DependencyHeadV1
    d07_comparison: DependencyHeadV1
    d09_summary: DependencyHeadV1
    d10_context: DependencyHeadV1
    family_source: DependencyHeadV1 | None = None
    anchor_policy: DependencyHeadV1
    projection_policy: DependencyHeadV1

    @model_validator(mode="after")
    def exact_slots(self) -> SavedComparisonDependencyHeadsV1:
        for slot, prefix in _SLOT_ID_PREFIXES.items():
            value = getattr(self, slot.value)
            if value is None:
                if slot is not DependencySlot.FAMILY_SOURCE:
                    raise ValueError("dependency head slot is missing")
                continue
            if prefix is not None and not (
                value.id.startswith(prefix) and len(value.id) == len(prefix) + 32
            ):
                raise ValueError("dependency head identity does not match its slot")
        # D06 status is scoped to one D05 cohort registry.
        if (self.d06_record_catalog.id, self.d06_record_catalog.epoch) != (
            self.d05_cohort.id,
            self.d05_cohort.epoch,
        ):
            raise ValueError("D06 head is not bound to the D05 cohort registry")
        if self.e04_catalog.id != "e04_catalog_" + self.e04_catalog.epoch[:32]:
            raise ValueError("E04 catalog identity is not derived from its storage")
        return self


def stale_dependency_slots(
    saved: SavedComparisonDependencyHeadsV1, live: SavedComparisonDependencyHeadsV1
) -> tuple[DependencySlot, ...]:
    """Return the slots whose ID, epoch or head differ, in lock order."""

    return tuple(
        slot
        for slot in DependencySlot
        if getattr(saved, slot.value) != getattr(live, slot.value)
    )


class SavedComparisonDependencyScopeV1(RegistryContract):
    """The one cohort version scoped dependency heads (D06, E06) are read for."""

    schema_version: Literal["traceback.saved-comparison-dependency-scope.v1"] = (
        "traceback.saved-comparison-dependency-scope.v1"
    )
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=MAX_COHORT_VERSION, strict=True)


class SavedComparisonRegistryBindingsV1(RegistryContract):
    """Dependency-store identities a registry is bound to for its lifetime."""

    schema_version: Literal["traceback.saved-comparison-registry-bindings.v1"] = (
        "traceback.saved-comparison-registry-bindings.v1"
    )
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    d04_ledger_id: str = Field(pattern=r"^ledger_[0-9a-f]{32}$")
    d04_ledger_epoch_sha256: Sha256
    reader_registry_id: str = Field(pattern=r"^reader_registry_[0-9a-f]{32}$")
    reader_registry_epoch_sha256: Sha256
    d06_catalog_storage_identity_sha256: Sha256
    e06_registry_id: str = Field(pattern=r"^e06_registry_[0-9a-f]{32}$")
    e06_registry_epoch_sha256: Sha256


def _bindings_from_heads(
    heads: SavedComparisonDependencyHeadsV1,
) -> SavedComparisonRegistryBindingsV1:
    return SavedComparisonRegistryBindingsV1(
        cohort_registry_id=heads.d05_cohort.id,
        cohort_registry_epoch_sha256=heads.d05_cohort.epoch,
        d04_ledger_id=heads.d04_history.id,
        d04_ledger_epoch_sha256=heads.d04_history.epoch,
        reader_registry_id=heads.reader_authorization.id,
        reader_registry_epoch_sha256=heads.reader_authorization.epoch,
        d06_catalog_storage_identity_sha256=heads.e04_catalog.epoch,
        e06_registry_id=heads.e06_source.id,
        e06_registry_epoch_sha256=heads.e06_source.epoch,
    )


class LongitudinalComparisonRegistryMetadataV1(RegistryContract):
    schema_version: Literal["traceback.saved-comparison-registry-metadata.v1"] = (
        "traceback.saved-comparison-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    saved_object_schema_version: Literal["traceback.saved-longitudinal-comparison.v1"] = (
        "traceback.saved-longitudinal-comparison.v1"
    )
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    d04_ledger_id: str = Field(pattern=r"^ledger_[0-9a-f]{32}$")
    d04_ledger_epoch_sha256: Sha256
    reader_registry_id: str = Field(pattern=r"^reader_registry_[0-9a-f]{32}$")
    reader_registry_epoch_sha256: Sha256
    d06_catalog_storage_identity_sha256: Sha256
    e06_registry_id: str = Field(pattern=r"^e06_registry_[0-9a-f]{32}$")
    e06_registry_epoch_sha256: Sha256
    creation_sha256: Sha256

    @model_validator(mode="after")
    def exact_creation_digest(self) -> LongitudinalComparisonRegistryMetadataV1:
        if self.creation_sha256 != _metadata_creation_sha256(self):
            raise ValueError("saved comparison registry creation digest is invalid")
        return self

    def bindings(self) -> SavedComparisonRegistryBindingsV1:
        return SavedComparisonRegistryBindingsV1(
            cohort_registry_id=self.cohort_registry_id,
            cohort_registry_epoch_sha256=self.cohort_registry_epoch_sha256,
            d04_ledger_id=self.d04_ledger_id,
            d04_ledger_epoch_sha256=self.d04_ledger_epoch_sha256,
            reader_registry_id=self.reader_registry_id,
            reader_registry_epoch_sha256=self.reader_registry_epoch_sha256,
            d06_catalog_storage_identity_sha256=(
                self.d06_catalog_storage_identity_sha256
            ),
            e06_registry_id=self.e06_registry_id,
            e06_registry_epoch_sha256=self.e06_registry_epoch_sha256,
        )


def _metadata_creation_sha256(
    metadata: LongitudinalComparisonRegistryMetadataV1,
) -> str:
    payload = metadata.model_dump(mode="json", exclude={"creation_sha256"})
    return hashlib.sha256(
        b"traceback-saved-comparison-registry-creation-v1\0"
        + _canonical_json(payload)
    ).hexdigest()


def _canonical_json(payload: object) -> bytes:
    import json

    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class SavedComparisonMeasurementV1(RegistryContract):
    """Requested D02 measurement definition, quantity and unit."""

    measurement_definition_sha256: Sha256
    scope: MeasurementScope


def _canonical_enum_tuple(values: tuple[StrEnum, ...], enum: type[StrEnum]) -> bool:
    order = {item: index for index, item in enumerate(enum)}
    indices = [order[item] for item in values]
    return indices == sorted(set(indices))


class SavedComparisonFiltersV1(RegistryContract):
    """Normalized controlled E12 filters; an empty tuple selects every row."""

    timepoint_ordinals: tuple[
        Annotated[int, Field(ge=0, le=MAX_TIMEPOINT_ORDINAL, strict=True)], ...
    ] = Field(default=(), max_length=MAX_COHORT_MEMBERS)
    lineage_roles: tuple[MemberLineageRole, ...] = Field(
        default=(), max_length=len(MemberLineageRole)
    )
    record_availability: tuple[CohortRecordAvailability, ...] = Field(
        default=(), max_length=len(CohortRecordAvailability)
    )
    compatibility_outcomes: tuple[LongitudinalOutcome, ...] = Field(
        default=(), max_length=len(LongitudinalOutcome)
    )
    result_view_filters: NormalizedResultFilters | None = None

    @model_validator(mode="after")
    def normalized(self) -> SavedComparisonFiltersV1:
        if list(self.timepoint_ordinals) != sorted(set(self.timepoint_ordinals)):
            raise ValueError("timepoint filter must be sorted and unique")
        for values, enum in (
            (self.lineage_roles, MemberLineageRole),
            (self.record_availability, CohortRecordAvailability),
            (self.compatibility_outcomes, LongitudinalOutcome),
        ):
            if not _canonical_enum_tuple(values, enum):
                raise ValueError("enum filters must use canonical order")
        return self


class SavedComparisonSelectionV1(RegistryContract):
    """Immutable canonical selection: registry-scoped selectors only."""

    schema_version: Literal["traceback.saved-comparison-selection.v1"] = (
        "traceback.saved-comparison-selection.v1"
    )
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=MAX_COHORT_VERSION, strict=True)
    anchor_policy_selector_id: AnchorPolicySelectorId
    anchor_policy_version: int = Field(ge=1, le=MAX_SELECTOR_VERSION, strict=True)
    approved_anchor_selector_id: AnchorCandidateSelectorId
    approved_anchor_version: int = Field(ge=1, le=MAX_SELECTOR_VERSION, strict=True)
    projection_policy_selector_id: ProjectionPolicySelectorId
    projection_policy_version: int = Field(ge=1, le=MAX_SELECTOR_VERSION, strict=True)
    d09_policy_selector_id: D09PolicySelectorId
    d09_policy_version: int = Field(ge=1, le=MAX_SELECTOR_VERSION, strict=True)
    measurement: SavedComparisonMeasurementV1
    filters: SavedComparisonFiltersV1


_FAMILY_STATISTICS: MappingProxyType[ProjectionFamily, type[StrEnum]] = (
    MappingProxyType(
        {
            ProjectionFamily.FRAGMENT: FragmentStatistic,
            ProjectionFamily.CELL_ORIGIN: CellOriginStatistic,
            ProjectionFamily.CNA_CHROMOSOME: CnaChromosomeStatistic,
            ProjectionFamily.CNA_SEGMENT: CnaSegmentStatistic,
        }
    )
)


class SavedFamilyProjectionRequestV1(RegistryContract):
    """The exact family source-value projection request the workspace used.

    The family coordinates live in the immutable registered projection
    policy; ``projection_policy_sha256`` pins that exact policy (and with it
    every panel/bin, contributor, chromosome or segment coordinate) at the
    selector/version below.
    """

    schema_version: Literal["traceback.saved-family-projection-request.v1"] = (
        "traceback.saved-family-projection-request.v1"
    )
    family: ProjectionFamily
    selection_rule: ProjectionSelectionRule
    # The distinct statistics the policy projects, in the family's canonical
    # order, and the controlled unit of each (the projection registry's own
    # statistic-to-unit table).
    statistics: tuple[
        FragmentStatistic
        | CellOriginStatistic
        | CnaChromosomeStatistic
        | CnaSegmentStatistic,
        ...,
    ] = Field(min_length=1, max_length=4)
    statistic_units: tuple[StatisticUnit, ...] = Field(min_length=1, max_length=4)
    projection_policy_selector_id: ProjectionPolicySelectorId
    projection_policy_version: int = Field(ge=1, le=MAX_SELECTOR_VERSION, strict=True)
    projection_policy_sha256: Sha256
    # Explicit components: at least one for ``finite_components``; zero for
    # ``canonical_all_components`` (mirrors the projection registry's rule).
    component_count: int = Field(ge=0, le=MAX_FINITE_COMPONENTS, strict=True)

    @model_validator(mode="after")
    def coherent_request(self) -> SavedFamilyProjectionRequestV1:
        family_type = _FAMILY_STATISTICS[self.family]
        if any(type(item) is not family_type for item in self.statistics):
            raise ValueError("projection statistic does not belong to its family")
        order = list(family_type)
        indices = [order.index(item) for item in self.statistics]
        if indices != sorted(set(indices)):
            raise ValueError("projection statistics must use canonical order")
        if self.statistic_units != tuple(
            _PROJECTION_STATISTIC_UNIT[item] for item in self.statistics
        ):
            raise ValueError("projection statistic units do not match")
        if (self.selection_rule is ProjectionSelectionRule.FINITE_COMPONENTS) != (
            self.component_count >= 1
        ):
            raise ValueError("projection component count does not match its rule")
        # Each finite component carries exactly one statistic, so every listed
        # statistic needs at least one component.
        if (
            self.selection_rule is ProjectionSelectionRule.FINITE_COMPONENTS
            and self.component_count < len(self.statistics)
        ):
            raise ValueError("projection statistics exceed their components")
        return self


class SavedComparisonCommitmentsV1(RegistryContract):
    """Exact digests of every authority-derived input the workspace replayed."""

    cohort_manifest_sha256: Sha256
    d03_anchor_policy_sha256: Sha256
    d07_envelope_sha256: Sha256
    approved_anchor_sha256: Sha256
    d03_decision_sha256: Sha256
    d07_comparisons_sha256: Sha256
    d09_summary_sha256: Sha256
    d10_context_sha256: Sha256
    d04_record_history_sha256: Sha256
    source_commitments_sha256: Sha256
    reader_grant_sha256: Sha256


class SavedE06SourceRefV1(RegistryContract):
    selector_id: E06SourceSelectorId
    source_version: int = Field(ge=1, le=MAX_SOURCE_VERSIONS, strict=True)


class SavedLongitudinalComparisonV1(RegistryContract):
    """One immutable saved comparison; never current authority by itself."""

    schema_version: Literal["traceback.saved-longitudinal-comparison.v1"] = (
        "traceback.saved-longitudinal-comparison.v1"
    )
    selection: SavedComparisonSelectionV1
    comparison_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS, strict=True)
    family_projection_request: SavedFamilyProjectionRequestV1
    commitments: SavedComparisonCommitmentsV1
    e06_registry_id: str = Field(pattern=r"^e06_registry_[0-9a-f]{32}$")
    e06_registry_epoch_sha256: Sha256
    e06_state_head_sha256: Sha256
    e06_sources: tuple[SavedE06SourceRefV1, ...] = Field(
        max_length=MAX_COHORT_MEMBERS
    )
    dependency_heads: SavedComparisonDependencyHeadsV1
    workspace_replay_sha256: Sha256
    created_at: datetime
    local_only: Literal[True] = True
    synthetic_only: Literal[True] = True
    product_release_authorized: Literal[False] = False
    release_export_authorized: Literal[False] = False
    diagnostic_interpretation_allowed: Literal[False] = False
    content_sha256: Sha256

    @model_validator(mode="after")
    def exact_bindings(self) -> SavedLongitudinalComparisonV1:
        if (
            self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.created_at.utcoffset().total_seconds() != 0
            or self.created_at.microsecond != 0
        ):
            raise ValueError("saved comparison time must be whole-second UTC")
        keys = [(item.selector_id, item.source_version) for item in self.e06_sources]
        if keys != sorted(set(keys)):
            raise ValueError("E06 sources must be sorted and unique")
        e06 = self.dependency_heads.e06_source
        if (e06.id, e06.epoch, e06.head) != (
            self.e06_registry_id,
            self.e06_registry_epoch_sha256,
            self.e06_state_head_sha256,
        ):
            raise ValueError("E06 source registry head does not match the vector")
        request = self.family_projection_request
        if (
            request.projection_policy_selector_id,
            request.projection_policy_version,
        ) != (
            self.selection.projection_policy_selector_id,
            self.selection.projection_policy_version,
        ):
            raise ValueError("family projection request does not match the selection")
        if self.content_sha256 != saved_comparison_content_sha256(self):
            raise ValueError("saved comparison content digest is invalid")
        return self


def saved_comparison_content_sha256(value: SavedLongitudinalComparisonV1) -> str:
    """Digest of every saved field except ``content_sha256`` itself."""

    payload = value.model_dump(mode="json", exclude={"content_sha256"})
    return hashlib.sha256(
        b"traceback-saved-longitudinal-comparison-v1\0" + _canonical_json(payload)
    ).hexdigest()


def build_saved_longitudinal_comparison(**fields: object) -> SavedLongitudinalComparisonV1:
    """Build one saved comparison and derive its content digest."""

    draft = SavedLongitudinalComparisonV1.model_construct(
        **fields, content_sha256="0" * 64
    )
    return SavedLongitudinalComparisonV1(
        **fields, content_sha256=saved_comparison_content_sha256(draft)
    )


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    SavedLongitudinalComparisonV1
)
_HEADS_MODEL_TYPES, _HEADS_ENUM_TYPES = contract_type_graph(
    SavedComparisonDependencyHeadsV1
)
_BINDINGS_MODEL_TYPES, _BINDINGS_ENUM_TYPES = contract_type_graph(
    SavedComparisonRegistryBindingsV1
)
_SCOPE_MODEL_TYPES, _SCOPE_ENUM_TYPES = contract_type_graph(
    SavedComparisonDependencyScopeV1
)
_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    LongitudinalComparisonRegistryMetadataV1
)


class SavedComparisonJournalEntryV1(RegistryContract):
    schema_version: Literal["traceback.saved-comparison-journal-entry.v1"] = (
        "traceback.saved-comparison-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_SAVED_COMPARISONS, strict=True)
    previous_entry_sha256: Sha256
    selector_id: SavedSelectorId
    comparison_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS, strict=True)
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    dependency_heads: SavedComparisonDependencyHeadsV1
    dependency_fence_kind: DependencyFenceKind
    entry_sha256: Sha256


class SavedComparisonRegistrationReceiptV1(RegistryContract):
    """Returned only after commit, reload and the final dependency recheck."""

    schema_version: Literal["traceback.saved-comparison-registration-receipt.v1"] = (
        "traceback.saved-comparison-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS)
    state_head_sha256: Sha256
    selector_id: SavedSelectorId
    comparison_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS)
    object_sha256: Sha256
    dependency_heads: SavedComparisonDependencyHeadsV1
    dependency_fence_kind: DependencyFenceKind
    applied: bool
    saving_authorizes_export: Literal[False] = False


class SavedComparisonRecoveryRecordV1(RegistryContract):
    """Durable publication intent, written before the object and the entry."""

    schema_version: Literal["traceback.saved-comparison-recovery-record.v1"] = (
        "traceback.saved-comparison-recovery-record.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    base_state_version: int = Field(ge=0, le=MAX_SAVED_COMPARISONS - 1, strict=True)
    base_state_head_sha256: Sha256
    base_journal_bytes: int = Field(ge=0, le=MAX_JOURNAL_BYTES, strict=True)
    entry: SavedComparisonJournalEntryV1
    record_sha256: Sha256

    @model_validator(mode="after")
    def exact_intent(self) -> SavedComparisonRecoveryRecordV1:
        if (
            self.entry.sequence != self.base_state_version + 1
            or self.entry.previous_entry_sha256 != self.base_state_head_sha256
        ):
            raise ValueError("recovery record entry does not extend its base")
        if self.record_sha256 != _recovery_record_sha256(self):
            raise ValueError("recovery record digest is invalid")
        return self


def _recovery_record_sha256(record: SavedComparisonRecoveryRecordV1) -> str:
    payload = record.model_dump(mode="json", exclude={"record_sha256"})
    return hashlib.sha256(
        b"traceback-saved-comparison-recovery-v1\0" + _canonical_json(payload)
    ).hexdigest()


class SavedComparisonSelectorRecordV1(RegistryContract):
    """One privacy-safe selector row: no cohort, provider or record identity."""

    selector_id: SavedSelectorId
    comparison_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS)
    object_sha256: Sha256
    authority_state: SavedComparisonAuthorityState
    stale_dependencies: tuple[DependencySlot, ...] = Field(
        max_length=len(DependencySlot)
    )

    @model_validator(mode="after")
    def coherent_state(self) -> SavedComparisonSelectorRecordV1:
        if (self.authority_state is SavedComparisonAuthorityState.CURRENT) != (
            not self.stale_dependencies
        ):
            raise ValueError("selector authority state does not match its slots")
        return self


class SavedComparisonSelectorPageV1(RegistryContract):
    schema_version: Literal["traceback.saved-comparison-selector-page.v1"] = (
        "traceback.saved-comparison-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_SAVED_COMPARISONS)
    state_head_sha256: Sha256
    dependency_fence_kind: DependencyFenceKind
    records: tuple[SavedComparisonSelectorRecordV1, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: SavedSelectorId | None
    next_after_version: int | None = Field(
        default=None, ge=1, le=MAX_SAVED_COMPARISONS
    )


class RegisteredSavedComparisonV1(RegistryContract):
    """Immutable saved bytes plus their current/stale status at one read.

    ``current`` means only that every dependency head still equals the saved
    vector.  It never carries current workspace values: those always need a
    fresh E12 replay, and a saved digest is never current authority.
    """

    schema_version: Literal["traceback.registered-saved-comparison.v1"] = (
        "traceback.registered-saved-comparison.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS)
    state_head_sha256: Sha256
    selector_id: SavedSelectorId
    comparison_version: int = Field(ge=1, le=MAX_SAVED_COMPARISONS)
    object_sha256: Sha256
    saved_object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]
    saved: SavedLongitudinalComparisonV1
    publication_fence_kind: DependencyFenceKind
    dependency_fence_kind: DependencyFenceKind
    live_dependency_heads: SavedComparisonDependencyHeadsV1
    authority_state: SavedComparisonAuthorityState
    stale_dependencies: tuple[DependencySlot, ...] = Field(
        max_length=len(DependencySlot)
    )
    saved_digest_is_current_authority: Literal[False] = False
    current_values_require_fresh_replay: Literal[True] = True
    synthetic_only: Literal[True] = True
    product_release_authorized: Literal[False] = False

    @model_validator(mode="after")
    def coherent_reopen(self) -> RegisteredSavedComparisonV1:
        content = self.saved_object_json.encode("utf-8")
        if hashlib.sha256(content).hexdigest() != self.object_sha256:
            raise ValueError("saved comparison bytes do not match their digest")
        if saved_comparison_object_from_bytes(content) != self.saved:
            raise ValueError("saved comparison object does not match its bytes")
        if self.saved.comparison_version != self.comparison_version:
            raise ValueError("saved comparison version does not match")
        expected = stale_dependency_slots(
            self.saved.dependency_heads, self.live_dependency_heads
        )
        if self.stale_dependencies != expected:
            raise ValueError("stale dependency slots are not exact")
        state = (
            SavedComparisonAuthorityState.STALE
            if expected
            else SavedComparisonAuthorityState.CURRENT
        )
        if self.authority_state is not state:
            raise ValueError("a stale saved comparison cannot present as current")
        return self


class SavedComparisonBackupObjectV1(RegistryContract):
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)


class SavedComparisonBackupHeaderV1(RegistryContract):
    """Backup index; the raw object bytes follow it in journal order."""

    schema_version: Literal["traceback.saved-comparison-backup.v1"] = (
        "traceback.saved-comparison-backup.v1"
    )
    metadata: LongitudinalComparisonRegistryMetadataV1
    state_version: int = Field(ge=0, le=MAX_SAVED_COMPARISONS)
    state_head_sha256: Sha256
    journal: tuple[SavedComparisonJournalEntryV1, ...] = Field(
        max_length=MAX_SAVED_COMPARISONS
    )
    objects: tuple[SavedComparisonBackupObjectV1, ...] = Field(
        max_length=MAX_SAVED_COMPARISONS
    )


_ENTRY_MODEL_TYPES, _ENTRY_ENUM_TYPES = contract_type_graph(
    SavedComparisonJournalEntryV1
)
_RECOVERY_MODEL_TYPES, _RECOVERY_ENUM_TYPES = contract_type_graph(
    SavedComparisonRecoveryRecordV1
)
_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(
    SavedComparisonBackupHeaderV1
)


# --- dependency-fence seam ---------------------------------------------------


class HeldSavedComparisonDependencies(ABC):
    """Dependency authority held for one registry operation.

    Implementations must keep returning heads from the same held authority
    until ``hold()`` exits.  ``read_heads`` may be called several times per
    operation (initial check, pre-commit, final recheck); the registry treats
    every returned value as untrusted and re-validates it exactly.
    """

    @property
    @abstractmethod
    def fence_kind(self) -> DependencyFenceKind: ...

    @abstractmethod
    def read_heads(
        self, scope: SavedComparisonDependencyScopeV1
    ) -> SavedComparisonDependencyHeadsV1: ...

    @abstractmethod
    def read_bindings(self) -> SavedComparisonRegistryBindingsV1: ...


class SavedComparisonDependencyFence(ABC):
    """Caller-supplied dependency fence, acquired before the registry lock.

    Lock order: every dependency fence first, then this registry's lock, as
    the plan's global order puts the saved-comparison registry last.
    """

    @abstractmethod
    def hold(self) -> AbstractContextManager[HeldSavedComparisonDependencies]: ...


def _capture_scope(scope: SavedComparisonDependencyScopeV1) -> bytes:
    return exact_model_bytes(
        scope,
        SavedComparisonDependencyScopeV1,
        model_types=_SCOPE_MODEL_TYPES,
        enum_types=_SCOPE_ENUM_TYPES,
        max_bytes=1024,
        max_nodes=32,
        max_depth=4,
        max_collection_items=4,
        max_string_bytes=128,
    )


# --- canonical bytes -----------------------------------------------------------


def saved_comparison_object_bytes(value: SavedLongitudinalComparisonV1) -> bytes:
    """Return exact bounded canonical bytes for one saved comparison."""

    return exact_model_bytes(
        value,
        SavedLongitudinalComparisonV1,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def saved_comparison_object_from_bytes(content: bytes) -> SavedLongitudinalComparisonV1:
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = SavedLongitudinalComparisonV1.model_validate_json(content)
        if saved_comparison_object_bytes(value) != content:
            raise ValueError("saved comparison is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("saved comparison is not canonical") from None


def _heads_bytes(value: object) -> bytes:
    return exact_model_bytes(
        value,
        SavedComparisonDependencyHeadsV1,
        model_types=_HEADS_MODEL_TYPES,
        enum_types=_HEADS_ENUM_TYPES,
        max_bytes=16 * 1024,
        max_nodes=MAX_JOURNAL_ENTRY_GRAPH_NODES,
        max_depth=4,
        max_collection_items=32,
        max_string_bytes=128,
    )


def _captured_heads(value: object) -> SavedComparisonDependencyHeadsV1:
    """Re-validate a fence result from its canonical bytes; never trust it."""

    try:
        content = _heads_bytes(value)
        return contract_from_canonical_bytes(SavedComparisonDependencyHeadsV1, content)
    except Exception:
        raise LongitudinalComparisonRegistryUnsafe(
            "dependency fence returned an invalid head vector"
        ) from None


def _captured_bindings(value: object) -> SavedComparisonRegistryBindingsV1:
    try:
        content = exact_model_bytes(
            value,
            SavedComparisonRegistryBindingsV1,
            model_types=_BINDINGS_MODEL_TYPES,
            enum_types=_BINDINGS_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=4,
            max_collection_items=16,
            max_string_bytes=128,
        )
        return contract_from_canonical_bytes(SavedComparisonRegistryBindingsV1, content)
    except Exception:
        raise LongitudinalComparisonRegistryUnsafe(
            "dependency fence returned invalid bindings"
        ) from None


def _captured_fence_kind(held: object) -> DependencyFenceKind:
    try:
        kind = held.fence_kind  # type: ignore[attr-defined]
    except Exception:
        raise LongitudinalComparisonRegistryUnsafe(
            "dependency fence kind is invalid"
        ) from None
    if type(kind) is not DependencyFenceKind:
        raise LongitudinalComparisonRegistryUnsafe("dependency fence kind is invalid")
    if kind is DependencyFenceKind.DIRECT_HEAD_REREAD or (
        kind is DependencyFenceKind.TEST_ONLY_UNFENCED
        and globals().get("_TEST_ONLY_FENCE_ALLOWED") is not True
    ):
        # Reading heads through public store reads inside this registry's
        # lock inverts the global lock order.
        raise LongitudinalComparisonRegistryUnsafe(
            "dependency fence kind is not a composite authority fence"
        )
    return kind


def _read_heads(
    held: HeldSavedComparisonDependencies, scope: SavedComparisonDependencyScopeV1
) -> SavedComparisonDependencyHeadsV1:
    try:
        value = held.read_heads(scope)
    except LongitudinalComparisonRegistryError:
        raise
    except Exception:
        # A dependency that cannot produce its head is not current authority.
        raise LongitudinalComparisonRegistryStale(
            "dependency authority is unavailable"
        ) from None
    return _captured_heads(value)


def _read_bindings(
    held: HeldSavedComparisonDependencies,
) -> SavedComparisonRegistryBindingsV1:
    try:
        value = held.read_bindings()
    except LongitudinalComparisonRegistryError:
        raise
    except Exception:
        raise LongitudinalComparisonRegistryStale(
            "dependency authority is unavailable"
        ) from None
    return _captured_bindings(value)


def _held(fence: object) -> AbstractContextManager[HeldSavedComparisonDependencies]:
    if not isinstance(fence, SavedComparisonDependencyFence):
        raise TypeError("a SavedComparisonDependencyFence is required")
    return fence.hold()


def _require_held(held: object) -> HeldSavedComparisonDependencies:
    if not isinstance(held, HeldSavedComparisonDependencies):
        raise LongitudinalComparisonRegistryUnsafe("dependency fence hold is invalid")
    return held


def _scope_of(value: SavedLongitudinalComparisonV1) -> SavedComparisonDependencyScopeV1:
    return SavedComparisonDependencyScopeV1(
        cohort_selector_id=value.selection.cohort_selector_id,
        cohort_version=value.selection.cohort_version,
    )


def _entry_bytes(entry: SavedComparisonJournalEntryV1) -> bytes:
    return exact_model_bytes(
        entry,
        SavedComparisonJournalEntryV1,
        model_types=_ENTRY_MODEL_TYPES,
        enum_types=_ENTRY_ENUM_TYPES,
        max_bytes=16 * 1024,
        max_nodes=MAX_JOURNAL_ENTRY_GRAPH_NODES,
        max_depth=6,
        max_collection_items=32,
        max_string_bytes=128,
    )


def _journal_entry_sha256(entry: SavedComparisonJournalEntryV1) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-saved-comparison-journal-v1\0"
        + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(
    metadata: LongitudinalComparisonRegistryMetadataV1,
) -> str:
    return hashlib.sha256(
        b"traceback-saved-comparison-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _selector_id(epoch: str, selection: SavedComparisonSelectionV1) -> str:
    digest = hashlib.sha256(
        b"traceback-saved-comparison-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + canonical_contract_bytes(selection)
    ).hexdigest()
    return f"{_SELECTOR_PREFIX}{digest[:40]}"


def _build_journal_entry(
    *,
    sequence: int,
    previous_entry_sha256: str,
    selector_id: str,
    comparison_version: int,
    object_sha256: str,
    object_bytes: int,
    dependency_heads: SavedComparisonDependencyHeadsV1,
    dependency_fence_kind: DependencyFenceKind,
) -> SavedComparisonJournalEntryV1:
    fields = {
        "sequence": sequence,
        "previous_entry_sha256": previous_entry_sha256,
        "selector_id": selector_id,
        "comparison_version": comparison_version,
        "object_sha256": object_sha256,
        "object_bytes": object_bytes,
        "dependency_heads": dependency_heads,
        "dependency_fence_kind": dependency_fence_kind,
    }
    placeholder = SavedComparisonJournalEntryV1.model_construct(
        **fields, entry_sha256="0" * 64
    )
    return SavedComparisonJournalEntryV1(
        **fields, entry_sha256=_journal_entry_sha256(placeholder)
    )


def _build_recovery_record(**fields: object) -> SavedComparisonRecoveryRecordV1:
    placeholder = SavedComparisonRecoveryRecordV1.model_construct(
        **fields, record_sha256="0" * 64
    )
    return SavedComparisonRecoveryRecordV1(
        **fields, record_sha256=_recovery_record_sha256(placeholder)
    )


def _recovery_bytes(record: SavedComparisonRecoveryRecordV1) -> bytes:
    return exact_model_bytes(
        record,
        SavedComparisonRecoveryRecordV1,
        model_types=_RECOVERY_MODEL_TYPES,
        enum_types=_RECOVERY_ENUM_TYPES,
        max_bytes=MAX_RECOVERY_BYTES,
        max_nodes=MAX_JOURNAL_ENTRY_GRAPH_NODES + 64,
        max_depth=8,
        max_collection_items=32,
        max_string_bytes=128,
    )


def _backup_header_bytes(header: SavedComparisonBackupHeaderV1) -> bytes:
    return exact_model_bytes(
        header,
        SavedComparisonBackupHeaderV1,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_HEADER_BYTES,
        max_nodes=MAX_BACKUP_HEADER_NODES,
        max_depth=8,
        max_collection_items=MAX_SAVED_COMPARISONS,
        max_string_bytes=128,
    )


def _projected_backup_bytes(header_bytes: int, object_bytes: int) -> int:
    return len(_BACKUP_MAGIC) + header_bytes + 1 + object_bytes


# --- filesystem helpers --------------------------------------------------------


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        raw = os.fspath(value)
        if type(raw) is not str:
            raise TypeError("saved comparison registry path is invalid")
    else:
        raise TypeError(
            "saved comparison registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("saved comparison registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("saved comparison registry path is invalid")
    return path


def _is_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_token(value: object, prefix: str, length: int) -> bool:
    return (
        type(value) is str
        and value.startswith(prefix)
        and _is_hex(value[len(prefix) :], length)
    )


def _is_temporary_name(name: object) -> bool:
    return _is_token(name, ".tmp-", 32)


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
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry file exceeds its bound"
            )
        chunks.append(chunk)


def _publish_file(directory_fd: int, name: str, content: bytes) -> None:
    """Write a private temporary file, fsync it, then link it no-overwrite."""

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


def _open_private_file(directory_fd: int, name: str, maximum: int) -> bytes:
    """Read one owner-only, single-link regular file through a bound descriptor."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry file is unsafe"
            )
        return _read_bounded(descriptor, maximum)
    except OSError:
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry file is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_object(objects_fd: int, entry: SavedComparisonJournalEntryV1) -> bytes:
    content = _open_private_file(
        objects_fd, f"{entry.object_sha256}.json", MAX_OBJECT_BYTES
    )
    if (
        len(content) != entry.object_bytes
        or hashlib.sha256(content).hexdigest() != entry.object_sha256
    ):
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison object does not match its journal entry"
        )
    return content


@dataclass(frozen=True)
class _Index:
    journal: tuple[SavedComparisonJournalEntryV1, ...]
    head: str
    journal_bytes: int
    by_digest: dict[str, SavedComparisonJournalEntryV1]
    by_key: dict[tuple[str, int], SavedComparisonJournalEntryV1]
    latest_version: dict[str, int]
    total_object_bytes: int


def _fold_journal(
    journal: tuple[SavedComparisonJournalEntryV1, ...], genesis: str
) -> _Index:
    """Replay the chain and the no-overwrite rules; any violation fails closed."""

    previous = genesis
    by_digest: dict[str, SavedComparisonJournalEntryV1] = {}
    by_key: dict[tuple[str, int], SavedComparisonJournalEntryV1] = {}
    latest: dict[str, int] = {}
    total = 0
    for sequence, entry in enumerate(journal, start=1):
        key = (entry.selector_id, entry.comparison_version)
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_sha256 in by_digest
            or key in by_key
            or entry.comparison_version != latest.get(entry.selector_id, 0) + 1
        ):
            raise ValueError("saved comparison journal chain is invalid")
        by_digest[entry.object_sha256] = entry
        by_key[key] = entry
        latest[entry.selector_id] = entry.comparison_version
        total += entry.object_bytes
        previous = entry.entry_sha256
    return _Index(
        journal=journal,
        head=previous,
        journal_bytes=sum(len(_entry_bytes(entry)) + 1 for entry in journal),
        by_digest=by_digest,
        by_key=by_key,
        latest_version=latest,
        total_object_bytes=total,
    )


def _parse_journal(content: bytes) -> tuple[SavedComparisonJournalEntryV1, ...]:
    if len(content) > MAX_JOURNAL_BYTES:
        raise ValueError("saved comparison journal exceeds its bound")
    if content and not content.endswith(b"\n"):
        raise ValueError("saved comparison journal is incomplete")
    lines = content.splitlines()
    if len(lines) > MAX_SAVED_COMPARISONS:
        raise ValueError("saved comparison journal exceeds its bound")
    return tuple(
        contract_from_canonical_bytes(SavedComparisonJournalEntryV1, line)
        for line in lines
    )


def _verify_object_matches_entry(
    value: SavedLongitudinalComparisonV1,
    entry: SavedComparisonJournalEntryV1,
    epoch: str,
) -> None:
    if (
        _selector_id(epoch, value.selection) != entry.selector_id
        or value.comparison_version != entry.comparison_version
        or value.dependency_heads != entry.dependency_heads
    ):
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison object does not match its journal entry"
        )


# --- registry ------------------------------------------------------------------


def _registry_instance_snapshot(
    registry: LongitudinalComparisonRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
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
    unsafe = LongitudinalComparisonRegistryUnsafe(
        "saved comparison registry authority state changed"
    )
    if type(instance) is not dict or any(name not in instance for name in required):
        raise unsafe
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            LongitudinalComparisonRegistryMetadataV1,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=MAX_METADATA_BYTES,
            max_nodes=64,
            max_depth=4,
            max_collection_items=16,
            max_string_bytes=128,
        )
    except (TypeError, ValueError):
        raise unsafe from None
    names = ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    descriptors = tuple(instance.get(name) for name in names)
    descriptor = instance.get("_metadata_fd")
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise unsafe
    elif type(descriptor) is not int or type(instance.get("_root_fd")) is not int:
        raise unsafe
    else:
        try:
            persisted = os.pread(descriptor, MAX_METADATA_BYTES + 1, 0)
            root_observed = os.fstat(instance["_root_fd"])
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise unsafe from None
        if persisted != metadata_bytes:
            raise unsafe
        if (
            instance["_root_identity"] != (root_observed.st_dev, root_observed.st_ino)
            or instance["_metadata_identity"]
            != (metadata_observed.st_dev, metadata_observed.st_ino)
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"]
            != (metadata.registry_id, metadata.registry_epoch_sha256)
        ):
            raise unsafe
    return (
        id(instance["root"]),
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


def _seal_registry_instance(registry: LongitudinalComparisonRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class LongitudinalComparisonRegistry:
    """Descriptor-relative, append-only, no-overwrite saved-comparison store."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "backup_bytes",
            "close",
            "identity",
            "list_selectors",
            "register",
            "reopen",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        dependency_fence: SavedComparisonDependencyFence,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
        expected_state_head_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values) and (
            any(item is None for item in expected_values)
            or not _is_token(expected_registry_id, _REGISTRY_ID_PREFIX, 32)
            or not _is_hex(expected_registry_epoch_sha256, 64)
            or not _is_hex(expected_state_head_sha256, 64)
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry expected identity or head is invalid"
            )
        self.root = _snapshot_path(root)
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        try:
            with _held(dependency_fence) as held:
                held = _require_held(held)
                _captured_fence_kind(held)
                bindings = _read_bindings(held)
                try:
                    self._open_storage(bindings, expected_values)
                except OSError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison registry storage is unsafe"
                    ) from None
        except BaseException:
            # Construction has not installed the instance seal yet, so cleanup
            # cannot pass through the public integrity-checked close boundary.
            self._close_descriptors()
            raise

    def _open_storage(
        self,
        bindings: SavedComparisonRegistryBindingsV1,
        expected_values: tuple[str | None, str | None, str | None],
    ) -> None:
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
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry root must be private"
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
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry root changed"
            )
        self._root_identity = (bound.st_dev, bound.st_ino)
        if root_created:
            os.mkdir(_OBJECTS_NAME, 0o700, dir_fd=self._root_fd)
        self._objects_fd = os.open(_OBJECTS_NAME, flags, dir_fd=self._root_fd)
        objects = os.fstat(self._objects_fd)
        if (
            not stat.S_ISDIR(objects.st_mode)
            or stat.S_IMODE(objects.st_mode) != 0o700
            or objects.st_uid != os.geteuid()
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry objects are unsafe"
            )
        self._objects_identity = (objects.st_dev, objects.st_ino)
        self._lock_fd = self._open_regular(_LOCK_NAME, create=root_created, append=False)
        self._lock_identity = _identity(os.fstat(self._lock_fd))
        self._journal_fd = self._open_regular(
            _JOURNAL_NAME, create=root_created, append=True
        )
        self._journal_identity = _identity(os.fstat(self._journal_fd))
        with _CR_LOCK(self, exclusive=True):
            self._metadata = _CR_LOAD_OR_CREATE_METADATA(
                self, bindings, allow_create=root_created
            )
            if self._metadata.bindings() != bindings:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry dependency authority changed"
                )
            self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
            self._head_key = (
                self._metadata.registry_id,
                self._metadata.registry_epoch_sha256,
            )
            _CR_RECOVER(self)
            index = _CR_LOAD_INDEX(self, accept_head=False)
            # A publication can commit without returning a receipt (a crash
            # after the journal fsync, or a failed final dependency recheck),
            # leaving the operator holding the predecessor head.  Startup
            # accepts exactly that one committed extension: it is forward
            # movement along the verified chain, never a rollback, and needs
            # no recovery record (which may already have been consumed).
            accepted_heads = {index.head}
            if index.journal:
                accepted_heads.add(index.journal[-1].previous_entry_sha256)
            if root_created:
                if any(item is not None for item in expected_values):
                    raise LongitudinalComparisonRegistryUnsafe(
                        "new saved comparison registry cannot inherit an identity"
                    )
            elif (
                expected_values[:2]
                != (self._metadata.registry_id, self._metadata.registry_epoch_sha256)
                or expected_values[2] not in accepted_heads
            ):
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry expected identity and head are "
                    "required and must match"
                )
            self._trusted_head_sha256 = index.head
            _CR_ACCEPT_OBSERVED_HEAD(self, index, check_instance=False)
            _seal_registry_instance(self)

    def _open_regular(self, name: str, *, create: bool, append: bool) -> int:
        assert self._root_fd is not None
        descriptor = os.open(
            name,
            os.O_RDWR
            | (os.O_CREAT if create else 0)
            | (os.O_APPEND if append else 0)
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._root_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            os.close(descriptor)
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry file is unsafe"
            )
        return descriptor

    def _close_descriptors(self) -> None:
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

    def close(self) -> None:
        _require_registry_integrity(self)
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            return
        with lock:
            self._close_descriptors()

    def __enter__(self) -> LongitudinalComparisonRegistry:
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
        # flock is per open file description and converts in place, so a nested
        # acquisition on one thread would silently upgrade or release the outer
        # lock.  Refuse it instead.
        if getattr(_LOCK_DEPTH, "value", 0):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry lock is not reentrant"
            )
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry is closed"
                )
            _LOCK_DEPTH.value = 1
            try:
                try:
                    fcntl.flock(
                        descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    )
                except OSError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison registry lock is unavailable"
                    ) from None
                try:
                    _CR_VALIDATE_STORAGE(self)
                    yield
                    _CR_VALIDATE_STORAGE(self)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                _LOCK_DEPTH.value = 0

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is closed"
            )
        names = [
            (_OBJECTS_NAME, self._objects_fd, self._objects_identity, True),
            (_LOCK_NAME, self._lock_fd, self._lock_identity, False),
            (_JOURNAL_NAME, self._journal_fd, self._journal_identity, False),
        ]
        if self._metadata_fd is not None:
            names.append(
                (_METADATA_NAME, self._metadata_fd, self._metadata_identity, False)
            )
        try:
            root_path = os.stat(self.root, follow_symlinks=False)
            root_bound = os.fstat(self._root_fd)
            observed = [
                (
                    os.stat(name, dir_fd=self._root_fd, follow_symlinks=False),
                    os.fstat(descriptor),
                    identity,
                    directory,
                )
                for name, descriptor, identity, directory in names
            ]
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or _identity(root_path) != self._root_identity
            or _identity(root_bound) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
            or any(
                (
                    not stat.S_ISDIR(path.st_mode)
                    if directory
                    else not stat.S_ISREG(path.st_mode) or bound.st_nlink != 1
                )
                or _identity(path) != identity
                or _identity(bound) != identity
                or stat.S_IMODE(bound.st_mode) != (0o700 if directory else 0o600)
                or bound.st_uid != os.geteuid()
                for path, bound, identity, directory in observed
            )
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry storage changed"
            )

    def _load_or_create_metadata(
        self, bindings: SavedComparisonRegistryBindingsV1, *, allow_create: bool
    ) -> LongitudinalComparisonRegistryMetadataV1:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                _METADATA_NAME,
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                # Metadata deletion never bootstraps a new identity.
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry metadata is missing"
                ) from None
            draft_fields = {
                "registry_id": f"{_REGISTRY_ID_PREFIX}{secrets.token_hex(16)}",
                "registry_epoch_sha256": secrets.token_hex(32),
                "storage_identity_sha256": hashlib.sha256(
                    b"traceback-saved-comparison-storage-v1\0"
                    + _canonical_json(
                        [list(self._root_identity), list(self._objects_identity)]
                    )
                ).hexdigest(),
                **bindings.model_dump(mode="python", exclude={"schema_version"}),
            }
            draft = LongitudinalComparisonRegistryMetadataV1.model_construct(
                **draft_fields, creation_sha256="0" * 64
            )
            metadata = LongitudinalComparisonRegistryMetadataV1(
                **draft_fields, creation_sha256=_metadata_creation_sha256(draft)
            )
            try:
                _publish_file(
                    self._root_fd, _METADATA_NAME, canonical_contract_bytes(metadata)
                )
            except FileExistsError:
                pass
            return _CR_LOAD_OR_CREATE_METADATA(self, bindings, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
            ):
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, MAX_METADATA_BYTES)
            metadata = contract_from_canonical_bytes(
                LongitudinalComparisonRegistryMetadataV1, content
            )
        except LongitudinalComparisonRegistryUnsafe:
            os.close(descriptor)
            raise
        except Exception:
            os.close(descriptor)
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = _identity(observed)
        return metadata

    def _read_journal_bytes(self) -> bytes:
        descriptor = self._journal_fd
        if descriptor is None:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is closed"
            )
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            return _read_bounded(descriptor, MAX_JOURNAL_BYTES)
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry journal is unavailable"
            ) from None

    def _load_index(self, *, accept_head: bool = True) -> _Index:
        """Load the committed index; extra, missing or altered files fail closed.

        Object files are checked for presence, type, mode, link count and exact
        size here; their bytes are verified by digest whenever they are read.
        """

        if self._objects_fd is None or self._root_fd is None:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is closed"
            )
        content = _CR_READ_JOURNAL_BYTES(self)
        try:
            index = _fold_journal(_parse_journal(content), self._genesis_head_sha256)
        except LongitudinalComparisonRegistryError:
            raise
        except Exception:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry journal is invalid"
            ) from None
        if index.journal_bytes != len(content):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry journal is invalid"
            )
        try:
            root_names = os.listdir(self._root_fd)
            object_names = os.listdir(self._objects_fd)
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is unavailable"
            ) from None
        pending = [
            name
            for name in (*root_names, *object_names)
            if name == _CANDIDATE_NAME or _is_temporary_name(name)
        ]
        if pending:
            # Only an interrupted writer leaves these; recovery runs under the
            # exclusive lock before the next operation.
            raise LongitudinalComparisonRegistryReadConflict(
                "saved comparison registry recovery is pending"
            )
        if set(root_names) != _ROOT_NAMES - {_CANDIDATE_NAME}:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry contains unexpected files"
            )
        expected_objects = {f"{digest}.json" for digest in index.by_digest}
        if len(object_names) > MAX_SAVED_COMPARISONS or set(object_names) != (
            expected_objects
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry objects are inconsistent"
            )
        for digest, entry in index.by_digest.items():
            try:
                observed = os.stat(
                    f"{digest}.json", dir_fd=self._objects_fd, follow_symlinks=False
                )
            except OSError:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry object is missing"
                ) from None
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
                or observed.st_size != entry.object_bytes
            ):
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry object is unsafe"
                )
        if accept_head:
            _CR_ACCEPT_OBSERVED_HEAD(self, index, check_instance=True)
        else:
            chain = {self._genesis_head_sha256, *(e.entry_sha256 for e in index.journal)}
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            if process_head is not None and process_head not in chain:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison registry state rollback detected"
                )
        return index

    def _accept_observed_head(self, index: _Index, *, check_instance: bool) -> None:
        chain = {self._genesis_head_sha256, *(e.entry_sha256 for e in index.journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = index.head
        if check_instance:
            self._trusted_head_sha256 = index.head
            _seal_registry_instance(self)

    def _recovery_pending(self) -> bool:
        """Unlocked hint only; the locked load is authoritative."""

        if self._root_fd is None or self._objects_fd is None:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is closed"
            )
        try:
            names = (*os.listdir(self._root_fd), *os.listdir(self._objects_fd))
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is unavailable"
            ) from None
        return any(name == _CANDIDATE_NAME or _is_temporary_name(name) for name in names)

    def _recover(self) -> None:
        """Resolve one interrupted publication; caller holds the exclusive lock.

        Only the durable candidate record decides; temporary files are never
        evidence.  A ``.tmp-<32 hex>`` name inside the registry's private
        ``0700`` directories is owned by the registry and is always unlinked
        (the merged D05 rule): unlink(2) never follows a symlink and never
        destroys data that has another link, and a directory under that name
        makes unlink fail, so it fails closed.  A committed entry whose exact
        object is present is made durable and adopted.  An uncommitted
        candidate loses only its own object (verified by digest) and its own
        torn journal suffix.  Anything else fails closed.
        """

        assert self._root_fd is not None and self._objects_fd is not None
        try:
            for directory in (self._root_fd, self._objects_fd):
                for name in os.listdir(directory):
                    if _is_temporary_name(name):
                        os.unlink(name, dir_fd=directory)
                os.fsync(directory)
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry recovery is unsafe"
            ) from None
        try:
            os.stat(_CANDIDATE_NAME, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry recovery is unsafe"
            ) from None
        content = _open_private_file(self._root_fd, _CANDIDATE_NAME, MAX_RECOVERY_BYTES)
        try:
            bounded_json_loads(
                content,
                max_bytes=MAX_RECOVERY_BYTES,
                max_depth=8,
                max_nodes=MAX_JOURNAL_ENTRY_GRAPH_NODES + 64,
                max_collection_items=32,
                max_string_bytes=128,
            )
            record = contract_from_canonical_bytes(
                SavedComparisonRecoveryRecordV1, content
            )
            if record.entry.entry_sha256 != _journal_entry_sha256(record.entry):
                raise ValueError("recovery entry digest is invalid")
        except Exception:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison recovery record is invalid"
            ) from None
        if (record.registry_id, record.registry_epoch_sha256) != self._head_key:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison recovery record belongs to another registry"
            )
        journal = _CR_READ_JOURNAL_BYTES(self)
        base = journal[: record.base_journal_bytes]
        try:
            base_index = _fold_journal(_parse_journal(base), self._genesis_head_sha256)
        except Exception:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison recovery base is invalid"
            ) from None
        if (
            len(base) != record.base_journal_bytes
            or len(base_index.journal) != record.base_state_version
            or base_index.head != record.base_state_head_sha256
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison recovery base does not match the journal"
            )
        intended = _entry_bytes(record.entry) + b"\n"
        suffix = journal[record.base_journal_bytes :]
        object_name = f"{record.entry.object_sha256}.json"
        if suffix == intended:
            # Committed: adopt only the exact object bytes the entry names, and
            # make the append durable before the record that proves it goes.
            _read_object(self._objects_fd, record.entry)
            try:
                os.fsync(self._journal_fd)
                os.fsync(self._objects_fd)
            except OSError:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison recovery fsync failed"
                ) from None
        elif intended.startswith(suffix) and record.entry.object_sha256 not in (
            base_index.by_digest
        ):
            # Uncommitted: remove the torn journal suffix, then the candidate's
            # own object if (and only if) it holds exactly the candidate bytes.
            if suffix:
                try:
                    os.ftruncate(self._journal_fd, record.base_journal_bytes)
                    os.fsync(self._journal_fd)
                except OSError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison recovery truncate failed"
                    ) from None
            try:
                os.stat(object_name, dir_fd=self._objects_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison recovery is unsafe"
                ) from None
            else:
                _read_object(self._objects_fd, record.entry)
                try:
                    os.unlink(object_name, dir_fd=self._objects_fd)
                    os.fsync(self._objects_fd)
                except OSError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison recovery is unsafe"
                    ) from None
        else:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison journal diverged from its recovery record"
            )
        try:
            os.unlink(_CANDIDATE_NAME, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison recovery is unsafe"
            ) from None

    def _ensure_recovered(self) -> None:
        if _CR_RECOVERY_PENDING(self):
            with _CR_LOCK(self, exclusive=True):
                _CR_RECOVER(self)

    def _append_journal(self, entry: SavedComparisonJournalEntryV1) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison registry is closed"
            )
        content = _entry_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException as error:
            # Remove any torn suffix so the committed chain stays readable,
            # including when an interrupt lands between partial writes.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            if not isinstance(error, OSError):
                raise
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison journal append failed"
            ) from None

    def _check_bindings(self, heads: SavedComparisonDependencyHeadsV1) -> None:
        if _bindings_from_heads(heads) != self._metadata.bindings():
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison is bound to other dependency stores"
            )

    def _receipt(
        self,
        index: _Index,
        entry: SavedComparisonJournalEntryV1,
        *,
        applied: bool,
    ) -> SavedComparisonRegistrationReceiptV1:
        return _CR_RECEIPT(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=self._metadata.registry_epoch_sha256,
            state_version=len(index.journal),
            state_head_sha256=index.head,
            selector_id=entry.selector_id,
            comparison_version=entry.comparison_version,
            object_sha256=entry.object_sha256,
            dependency_heads=entry.dependency_heads,
            dependency_fence_kind=entry.dependency_fence_kind,
            applied=applied,
        )

    def _publish_locked(
        self,
        held: HeldSavedComparisonDependencies,
        captured: SavedLongitudinalComparisonV1,
        content: bytes,
        heads: SavedComparisonDependencyHeadsV1,
        fence_kind: DependencyFenceKind,
    ) -> SavedComparisonRegistrationReceiptV1:
        assert self._root_fd is not None and self._objects_fd is not None
        scope = _scope_of(captured)
        digest = hashlib.sha256(content).hexdigest()
        _CR_RECOVER(self)
        index = _CR_LOAD_INDEX(self)
        epoch = self._metadata.registry_epoch_sha256
        selector = _CR_SELECTOR_ID(epoch, captured.selection)
        existing = index.by_digest.get(digest)
        if existing is not None:
            if _read_object(self._objects_fd, existing) != content:
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison object digest collision"
                )
            # Exact retry: idempotent only while every dependency head is
            # unchanged; otherwise the saved bytes can no longer be current.
            if _read_heads(held, scope) != existing.dependency_heads:
                raise LongitudinalComparisonRegistryStale(
                    "dependency authority changed since this comparison was saved"
                )
            return self._receipt(index, existing, applied=False)
        if (selector, captured.comparison_version) in index.by_key:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison selector and version already hold other bytes"
            )
        if captured.comparison_version != index.latest_version.get(selector, 0) + 1:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison version must be the next version"
            )
        if len(index.journal) >= MAX_SAVED_COMPARISONS:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison registry is full"
            )
        entry = _build_journal_entry(
            sequence=len(index.journal) + 1,
            previous_entry_sha256=index.head,
            selector_id=selector,
            comparison_version=captured.comparison_version,
            object_sha256=digest,
            object_bytes=len(content),
            dependency_heads=heads,
            dependency_fence_kind=fence_kind,
        )
        entry_line = _entry_bytes(entry) + b"\n"
        if index.journal_bytes + len(entry_line) > MAX_JOURNAL_BYTES:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison journal bound would be exceeded"
            )
        # Cumulative-bytes admission: the exact backup this state would produce.
        projected_header = _backup_header_bytes(
            SavedComparisonBackupHeaderV1(
                metadata=self._metadata,
                state_version=len(index.journal) + 1,
                state_head_sha256=entry.entry_sha256,
                journal=(*index.journal, entry),
                objects=tuple(
                    SavedComparisonBackupObjectV1(
                        object_sha256=item.object_sha256,
                        object_bytes=item.object_bytes,
                    )
                    for item in (*index.journal, entry)
                ),
            )
        )
        if (
            _projected_backup_bytes(
                len(projected_header), index.total_object_bytes + len(content)
            )
            > MAX_BACKUP_BYTES
        ):
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison backup bound would be exceeded"
            )
        record = _build_recovery_record(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=epoch,
            base_state_version=len(index.journal),
            base_state_head_sha256=index.head,
            base_journal_bytes=index.journal_bytes,
            entry=entry,
        )
        record_bytes = _recovery_bytes(record)
        try:
            _CR_PUBLISH(self, self._root_fd, _CANDIDATE_NAME, record_bytes)
            _CR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
            if _read_heads(held, scope) != heads:
                raise LongitudinalComparisonRegistryStale(
                    "dependency authority changed before commit"
                )
            _CR_APPEND_JOURNAL(self, entry)
        except BaseException:
            # Roll an uncommitted candidate back (or adopt a committed one)
            # before releasing the lock; on failure the record stays durable
            # and the next operation or startup recovers it.
            try:
                _CR_RECOVER(self)
            except Exception:
                pass
            raise
        _CR_RECOVER(self)
        final = _CR_LOAD_INDEX(self)
        committed = final.by_digest.get(digest)
        if (
            committed != entry
            or _read_object(self._objects_fd, committed) != content
        ):
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison publication is unproven"
            )
        if _read_heads(held, scope) != heads:
            # Committed, but no receipt: the object reopens as stale and an
            # exact retry is a conflict.
            raise LongitudinalComparisonRegistryStale(
                "dependency authority changed during final return"
            )
        return self._receipt(final, entry, applied=True)

    def register(
        self,
        saved: SavedLongitudinalComparisonV1,
        *,
        dependency_fence: SavedComparisonDependencyFence,
    ) -> SavedComparisonRegistrationReceiptV1:
        """Publish one saved comparison transactionally, never overwriting.

        Input is canonicalized and bounded before any lock is taken.  The
        dependency fence is held across the whole publication; the registry
        lock is taken inside it.  The receipt is returned only after the
        object and journal entry are durable, the complete state reloads, and
        every dependency head still equals the saved vector.
        """

        _require_registry_integrity(self)
        try:
            content = saved_comparison_object_bytes(saved)
            captured = saved_comparison_object_from_bytes(content)
        except (TypeError, ValueError):
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison is not an exact bounded contract"
            ) from None
        scope = _scope_of(captured)
        with _held(dependency_fence) as held:
            held = _require_held(held)
            fence_kind = _captured_fence_kind(held)
            heads = _read_heads(held, scope)
            self._check_bindings(heads)
            if heads != captured.dependency_heads:
                raise LongitudinalComparisonRegistryStale(
                    "dependency authority changed since the comparison was built"
                )
            _CR_ENSURE_RECOVERED(self)
            with _CR_LOCK(self, exclusive=True):
                return _CR_PUBLISH_LOCKED(
                    self, held, captured, content, heads, fence_kind
                )

    def identity(self) -> tuple[str, str, int, str]:
        """Return (registry ID, epoch, state version, head) to retain for reopen."""

        _require_registry_integrity(self)
        _CR_ENSURE_RECOVERED(self)
        with _CR_LOCK(self, exclusive=False):
            index = _CR_LOAD_INDEX(self)
            return (
                self._metadata.registry_id,
                self._metadata.registry_epoch_sha256,
                len(index.journal),
                index.head,
            )

    def resolve(
        self,
        selector_id: str,
        comparison_version: int,
        *,
        dependency_fence: SavedComparisonDependencyFence,
    ) -> RegisteredSavedComparisonV1:
        """Reopen immutable saved bytes with their current/stale status.

        The dependency fence and the shared registry lock are held through
        parse, the live head read, the final head recheck and construction.
        """

        _require_registry_integrity(self)
        if not _is_token(selector_id, _SELECTOR_PREFIX, 40) or (
            type(comparison_version) is not int
            or not 1 <= comparison_version <= MAX_SAVED_COMPARISONS
        ):
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison selector is invalid"
            )
        _CR_ENSURE_RECOVERED(self)
        with _held(dependency_fence) as held:
            held = _require_held(held)
            fence_kind = _captured_fence_kind(held)
            with _CR_LOCK(self, exclusive=False):
                index = _CR_LOAD_INDEX(self)
                entry = index.by_key.get((selector_id, comparison_version))
                if entry is None:
                    raise LongitudinalComparisonRegistryConflict(
                        "saved comparison selector is unavailable"
                    )
                assert self._objects_fd is not None
                content = _read_object(self._objects_fd, entry)
                try:
                    value = saved_comparison_object_from_bytes(content)
                except ValueError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison object is invalid"
                    ) from None
                _verify_object_matches_entry(
                    value, entry, self._metadata.registry_epoch_sha256
                )
                scope = _scope_of(value)
                live = _read_heads(held, scope)
                self._check_bindings(live)
                stale = stale_dependency_slots(entry.dependency_heads, live)
                if _read_heads(held, scope) != live:
                    raise LongitudinalComparisonRegistryReadConflict(
                        "dependency authority changed during reopen"
                    )
                return _CR_RESOLVED(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(index.journal),
                    state_head_sha256=index.head,
                    selector_id=selector_id,
                    comparison_version=comparison_version,
                    object_sha256=entry.object_sha256,
                    saved_object_json=content.decode("utf-8"),
                    saved=value,
                    publication_fence_kind=entry.dependency_fence_kind,
                    dependency_fence_kind=fence_kind,
                    live_dependency_heads=live,
                    authority_state=(
                        SavedComparisonAuthorityState.STALE
                        if stale
                        else SavedComparisonAuthorityState.CURRENT
                    ),
                    stale_dependencies=stale,
                )

    def reopen(
        self,
        selector_id: str,
        comparison_version: int,
        *,
        dependency_fence: SavedComparisonDependencyFence,
    ) -> RegisteredSavedComparisonV1:
        """Alias of ``resolve`` named for the D08 Reopen journey step."""

        return LongitudinalComparisonRegistry.resolve(
            self, selector_id, comparison_version, dependency_fence=dependency_fence
        )

    def list_selectors(
        self,
        *,
        dependency_fence: SavedComparisonDependencyFence,
        after_selector_id: str | None = None,
        after_version: int | None = None,
        limit: int = 50,
    ) -> SavedComparisonSelectorPageV1:
        """Return one bounded privacy-safe page with live current/stale state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison page bound is invalid"
            )
        if (after_selector_id is None) != (after_version is None) or (
            after_selector_id is not None
            and (
                not _is_token(after_selector_id, _SELECTOR_PREFIX, 40)
                or type(after_version) is not int
                or not 1 <= after_version <= MAX_SAVED_COMPARISONS
            )
        ):
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison page cursor is invalid"
            )
        _CR_ENSURE_RECOVERED(self)
        with _held(dependency_fence) as held:
            held = _require_held(held)
            fence_kind = _captured_fence_kind(held)
            with _CR_LOCK(self, exclusive=False):
                index = _CR_LOAD_INDEX(self)
                ordered = sorted(index.by_key)
                if after_selector_id is not None:
                    cursor = (after_selector_id, after_version)
                    ordered = [key for key in ordered if key > cursor]
                selected = ordered[:limit]
                assert self._objects_fd is not None
                live_by_scope: dict[bytes, SavedComparisonDependencyHeadsV1] = {}
                scopes: dict[bytes, SavedComparisonDependencyScopeV1] = {}
                rows: list[SavedComparisonSelectorRecordV1] = []
                for key in selected:
                    entry = index.by_key[key]
                    content = _read_object(self._objects_fd, entry)
                    try:
                        value = saved_comparison_object_from_bytes(content)
                    except ValueError:
                        raise LongitudinalComparisonRegistryUnsafe(
                            "saved comparison object is invalid"
                        ) from None
                    _verify_object_matches_entry(
                        value, entry, self._metadata.registry_epoch_sha256
                    )
                    scope = _scope_of(value)
                    scope_key = canonical_contract_bytes(scope)
                    if scope_key not in live_by_scope:
                        live = _read_heads(held, scope)
                        self._check_bindings(live)
                        live_by_scope[scope_key] = live
                        scopes[scope_key] = scope
                    stale = stale_dependency_slots(
                        entry.dependency_heads, live_by_scope[scope_key]
                    )
                    rows.append(
                        _CR_SELECTOR_RECORD(
                            selector_id=entry.selector_id,
                            comparison_version=entry.comparison_version,
                            object_sha256=entry.object_sha256,
                            authority_state=(
                                SavedComparisonAuthorityState.STALE
                                if stale
                                else SavedComparisonAuthorityState.CURRENT
                            ),
                            stale_dependencies=stale,
                        )
                    )
                for scope_key, scope in scopes.items():
                    if _read_heads(held, scope) != live_by_scope[scope_key]:
                        raise LongitudinalComparisonRegistryReadConflict(
                            "dependency authority changed during the page read"
                        )
                more = len(ordered) > len(selected)
                return _CR_SELECTOR_PAGE(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(index.journal),
                    state_head_sha256=index.head,
                    dependency_fence_kind=fence_kind,
                    records=tuple(rows),
                    next_after_selector_id=rows[-1].selector_id if more else None,
                    next_after_version=rows[-1].comparison_version if more else None,
                )

    def backup_bytes(self) -> bytes:
        """Return one exact, recovery-free backup captured under one lock."""

        _require_registry_integrity(self)
        _CR_ENSURE_RECOVERED(self)
        with _CR_LOCK(self, exclusive=False):
            index = _CR_LOAD_INDEX(self)
            assert self._objects_fd is not None
            objects = [_read_object(self._objects_fd, entry) for entry in index.journal]
            for entry, content in zip(index.journal, objects, strict=True):
                try:
                    value = saved_comparison_object_from_bytes(content)
                except ValueError:
                    raise LongitudinalComparisonRegistryUnsafe(
                        "saved comparison object is invalid"
                    ) from None
                _verify_object_matches_entry(
                    value, entry, self._metadata.registry_epoch_sha256
                )
            header = _backup_header_bytes(
                SavedComparisonBackupHeaderV1(
                    metadata=self._metadata,
                    state_version=len(index.journal),
                    state_head_sha256=index.head,
                    journal=index.journal,
                    objects=tuple(
                        SavedComparisonBackupObjectV1(
                            object_sha256=entry.object_sha256,
                            object_bytes=entry.object_bytes,
                        )
                        for entry in index.journal
                    ),
                )
            )
            content = b"".join((_BACKUP_MAGIC, header, b"\n", *objects))
            if len(content) > MAX_BACKUP_BYTES:
                raise LongitudinalComparisonRegistryConflict(
                    "saved comparison backup exceeds its bound"
                )
            return content

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        dependency_fence: SavedComparisonDependencyFence,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> LongitudinalComparisonRegistry:
        """Restore a verified backup into one new private registry root.

        The target must not exist.  The backup's dependency-store bindings must
        equal the live bindings read through the fence; the restored registry
        reopens through the normal checks, so the process-wide head fence
        rejects restoring a backup older than a head this process saw.
        """

        _require_registry_class_integrity(cls)
        backup, objects = saved_comparison_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or (
                expected_registry_id,
                expected_registry_epoch_sha256,
                expected_state_head_sha256,
            )
            != (
                backup.metadata.registry_id,
                backup.metadata.registry_epoch_sha256,
                backup.state_head_sha256,
            )
        ):
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison backup expected identity or head is invalid"
            )
        with _held(dependency_fence) as held:
            held = _require_held(held)
            _captured_fence_kind(held)
            if _read_bindings(held) != backup.metadata.bindings():
                raise LongitudinalComparisonRegistryConflict(
                    "saved comparison backup is bound to other dependency stores"
                )
        target = _snapshot_path(root)
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
            parent_lstat = os.stat(target.parent, follow_symlinks=False)
            parent_fd = os.open(target.parent, directory_flags)
            if not stat.S_ISDIR(parent_lstat.st_mode) or _identity(
                parent_lstat
            ) != _identity(os.fstat(parent_fd)):
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison restore parent changed"
                )
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            created = True
            root_lstat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(target.name, directory_flags, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or _identity(root_lstat) != _identity(root_bound)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise LongitudinalComparisonRegistryUnsafe(
                    "saved comparison restore root changed"
                )
            os.mkdir(_OBJECTS_NAME, 0o700, dir_fd=root_fd)
            objects_fd = os.open(_OBJECTS_NAME, directory_flags, dir_fd=root_fd)
            lock_fd = os.open(
                _LOCK_NAME,
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
                root_fd, _METADATA_NAME, canonical_contract_bytes(backup.metadata)
            )
            for entry, content in zip(backup.journal, objects, strict=True):
                _publish_file(objects_fd, f"{entry.object_sha256}.json", content)
            _publish_file(
                root_fd,
                _JOURNAL_NAME,
                b"".join(_entry_bytes(entry) + b"\n" for entry in backup.journal),
            )
            os.fsync(objects_fd)
            os.fsync(root_fd)
            os.fsync(parent_fd)
            restored = _CR_CONSTRUCT(
                target,
                dependency_fence=dependency_fence,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise LongitudinalComparisonRegistryConflict(
                "saved comparison restore target already exists"
            ) from None
        except OSError:
            raise LongitudinalComparisonRegistryUnsafe(
                "saved comparison restore failed"
            ) from None
        finally:
            if created and not completed:
                _remove_partial_restore(parent_fd, target.name, root_fd, objects_fd)
            for descriptor in (objects_fd, root_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return restored


def _identity(observed: os.stat_result) -> tuple[int, int]:
    return (observed.st_dev, observed.st_ino)


def _remove_partial_restore(
    parent_fd: int | None, name: str, root_fd: int | None, objects_fd: int | None
) -> None:
    """Remove only the files a failed restore created, then its root."""

    try:
        if objects_fd is not None and root_fd is not None:
            for entry in os.listdir(objects_fd):
                os.unlink(entry, dir_fd=objects_fd)
            os.rmdir(_OBJECTS_NAME, dir_fd=root_fd)
        if root_fd is not None:
            for entry in os.listdir(root_fd):
                os.unlink(entry, dir_fd=root_fd)
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def saved_comparison_backup_from_bytes(
    content: bytes,
) -> tuple[SavedComparisonBackupHeaderV1, tuple[bytes, ...]]:
    """Parse and fully verify one backup; truncated or altered input rejects."""

    invalid = LongitudinalComparisonRegistryConflict(
        "saved comparison backup is invalid"
    )
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise LongitudinalComparisonRegistryConflict(
            "saved comparison backup exceeds its bound"
        )
    if not content.startswith(_BACKUP_MAGIC):
        raise invalid
    end = content.find(
        b"\n", len(_BACKUP_MAGIC), len(_BACKUP_MAGIC) + MAX_BACKUP_HEADER_BYTES + 1
    )
    if end < 0:
        raise invalid
    header_bytes = content[len(_BACKUP_MAGIC) : end]
    try:
        bounded_json_loads(
            header_bytes,
            max_bytes=MAX_BACKUP_HEADER_BYTES,
            max_depth=8,
            max_nodes=MAX_BACKUP_HEADER_NODES,
            max_collection_items=MAX_SAVED_COMPARISONS,
            max_string_bytes=128,
        )
        header = SavedComparisonBackupHeaderV1.model_validate_json(header_bytes)
        if _backup_header_bytes(header) != header_bytes:
            raise ValueError("backup header is not canonical")
        index = _fold_journal(header.journal, _metadata_genesis_sha256(header.metadata))
    except (TypeError, ValueError):
        raise invalid from None
    if (
        header.state_version != len(header.journal)
        or header.state_head_sha256 != index.head
        or index.journal_bytes > MAX_JOURNAL_BYTES
        or tuple((item.object_sha256, item.object_bytes) for item in header.objects)
        != tuple((entry.object_sha256, entry.object_bytes) for entry in header.journal)
    ):
        raise invalid
    body = content[end + 1 :]
    if len(body) != index.total_object_bytes:
        raise invalid
    objects: list[bytes] = []
    offset = 0
    for entry in header.journal:
        item = body[offset : offset + entry.object_bytes]
        offset += entry.object_bytes
        if hashlib.sha256(item).hexdigest() != entry.object_sha256:
            raise invalid
        try:
            value = saved_comparison_object_from_bytes(item)
        except ValueError:
            raise invalid from None
        if (
            _selector_id(header.metadata.registry_epoch_sha256, value.selection)
            != entry.selector_id
            or value.comparison_version != entry.comparison_version
            or value.dependency_heads != entry.dependency_heads
            or _bindings_from_heads(value.dependency_heads)
            != header.metadata.bindings()
        ):
            raise invalid
        objects.append(item)
    return header, tuple(objects)


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: LongitudinalComparisonRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_open_storage",
            "_open_regular",
            "_close_descriptors",
            "_lock",
            "_validate_storage",
            "_load_or_create_metadata",
            "_read_journal_bytes",
            "_load_index",
            "_accept_observed_head",
            "_recovery_pending",
            "_recover",
            "_ensure_recovered",
            "_append_journal",
            "_check_bindings",
            "_receipt",
            "_publish_locked",
            "register",
            "identity",
            "resolve",
            "reopen",
            "list_selectors",
            "backup_bytes",
            "restore",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not LongitudinalComparisonRegistry or any(
        LongitudinalComparisonRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry callable changed"
        )


def _require_registry_integrity(registry: LongitudinalComparisonRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    instance = object.__getattribute__(registry, "__dict__")
    if any(name in instance for name in _REGISTRY_METHOD_SEAL):
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry callable changed"
        )
    if any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry authority callable changed"
        )
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
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise LongitudinalComparisonRegistryUnsafe(
            "saved comparison registry authority state changed"
        )


def _publish_through_registry(
    registry: LongitudinalComparisonRegistry, directory_fd: int, name: str, content: bytes
) -> None:
    _publish_file(directory_fd, name, content)


_CR_CONSTRUCT = LongitudinalComparisonRegistry
_CR_CLOSE = LongitudinalComparisonRegistry.close
_CR_LOCK = LongitudinalComparisonRegistry._lock
_CR_VALIDATE_STORAGE = LongitudinalComparisonRegistry._validate_storage
_CR_LOAD_OR_CREATE_METADATA = LongitudinalComparisonRegistry._load_or_create_metadata
_CR_READ_JOURNAL_BYTES = LongitudinalComparisonRegistry._read_journal_bytes
_CR_LOAD_INDEX = LongitudinalComparisonRegistry._load_index
_CR_ACCEPT_OBSERVED_HEAD = LongitudinalComparisonRegistry._accept_observed_head
_CR_RECOVERY_PENDING = LongitudinalComparisonRegistry._recovery_pending
_CR_RECOVER = LongitudinalComparisonRegistry._recover
_CR_ENSURE_RECOVERED = LongitudinalComparisonRegistry._ensure_recovered
_CR_APPEND_JOURNAL = LongitudinalComparisonRegistry._append_journal
_CR_PUBLISH_LOCKED = LongitudinalComparisonRegistry._publish_locked
# Fault-injection seam for crash-window tests: every file publication inside a
# registration goes through this one sealed alias.
_CR_PUBLISH = _publish_through_registry
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another object's bytes or status.
_CR_RECEIPT = SavedComparisonRegistrationReceiptV1
_CR_RESOLVED = RegisteredSavedComparisonV1
_CR_SELECTOR_RECORD = SavedComparisonSelectorRecordV1
_CR_SELECTOR_PAGE = SavedComparisonSelectorPageV1
_CR_SELECTOR_ID = _selector_id
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_CR_CONSTRUCT",
            "_CR_CLOSE",
            "_CR_LOCK",
            "_CR_VALIDATE_STORAGE",
            "_CR_LOAD_OR_CREATE_METADATA",
            "_CR_READ_JOURNAL_BYTES",
            "_CR_LOAD_INDEX",
            "_CR_ACCEPT_OBSERVED_HEAD",
            "_CR_RECOVERY_PENDING",
            "_CR_RECOVER",
            "_CR_ENSURE_RECOVERED",
            "_CR_APPEND_JOURNAL",
            "_CR_PUBLISH_LOCKED",
            "_CR_PUBLISH",
            "_CR_RECEIPT",
            "_CR_RESOLVED",
            "_CR_SELECTOR_RECORD",
            "_CR_SELECTOR_PAGE",
            "_CR_SELECTOR_ID",
        )
    }
)


__all__ = [
    "MAX_BACKUP_BYTES",
    "MAX_JOURNAL_BYTES",
    "MAX_OBJECT_BYTES",
    "MAX_RECOVERY_BYTES",
    "MAX_SAVED_COMPARISONS",
    "MAX_SELECTOR_PAGE",
    "DependencyFenceKind",
    "DependencyHeadV1",
    "DependencySlot",
    "HeldSavedComparisonDependencies",
    "LongitudinalComparisonRegistry",
    "LongitudinalComparisonRegistryConflict",
    "LongitudinalComparisonRegistryError",
    "LongitudinalComparisonRegistryMetadataV1",
    "LongitudinalComparisonRegistryReadConflict",
    "LongitudinalComparisonRegistryStale",
    "LongitudinalComparisonRegistryUnsafe",
    "RegisteredSavedComparisonV1",
    "SavedComparisonAuthorityState",
    "SavedComparisonBackupHeaderV1",
    "SavedComparisonBackupObjectV1",
    "SavedComparisonCommitmentsV1",
    "SavedComparisonDependencyFence",
    "SavedComparisonDependencyHeadsV1",
    "SavedComparisonDependencyScopeV1",
    "SavedComparisonFiltersV1",
    "SavedComparisonJournalEntryV1",
    "SavedComparisonMeasurementV1",
    "SavedComparisonRecoveryRecordV1",
    "SavedComparisonRegistrationReceiptV1",
    "SavedComparisonRegistryBindingsV1",
    "SavedComparisonSelectionV1",
    "SavedComparisonSelectorPageV1",
    "SavedComparisonSelectorRecordV1",
    "SavedE06SourceRefV1",
    "SavedFamilyProjectionRequestV1",
    "SavedLongitudinalComparisonV1",
    "build_saved_longitudinal_comparison",
    "saved_comparison_backup_from_bytes",
    "saved_comparison_content_sha256",
    "saved_comparison_object_bytes",
    "saved_comparison_object_from_bytes",
    "stale_dependency_slots",
]
