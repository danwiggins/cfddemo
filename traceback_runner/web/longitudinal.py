"""E12 browser integration: bounded longitudinal routes over the D08 read model.

The loopback server dispatches ``/api/v1/longitudinal/*`` here.  Every route:

1. runs the B01 transport checks (Host, session, Origin and CSRF for POST);
2. re-resolves the session's bound reader grant under the reader-registry
   fence through :meth:`ReaderSessionBinder.reader_authorization` before any
   protected read (a bare B01 session, or any grant failure, gets the same
   bounded ``permission_denied`` shell with no counts, selectors or timing
   detail);
3. reads only through the merged stores' public reads or the D08 builder;
4. renders only public contracts: the D08 ``LongitudinalWorkspaceProjection``
   and the closed response models below, never a protected model;
5. re-authorizes the reader before the response is returned.

Save publishes ``SavedLongitudinalComparisonV1`` through
``LongitudinalComparisonRegistry.register`` with a ``CompositeAuthorityFence``
and shows a receipt only after the transactional publication and a final
composite hold that re-reads the published head vector and re-authorizes
the reader.  Reopen shows the version/authority/policy diff before any
result, rebuilds the workspace from the saved selection, and presents a
saved comparison as current only when the registry reports it current and
the rebuilt replay digest equals the saved one.  Saved bytes are never
rewritten.

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope.  Pins and type
checks detect accidental or naive class and instance replacement only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, NoReturn

from pydantic import Field, ValidationError

import evidence_inspector.longitudinal_comparison_registry as saved_module
from evidence_inspector.anchor_policy_registry import (
    AnchorEligibilityState,
    AnchorPolicyAuthorityState,
    AnchorPolicyRegistry,
)
from evidence_inspector.cohort_import import (
    CohortRecordCatalog,
)
from evidence_inspector.cohort_registry import CohortAuthorityState, CohortRegistry
from evidence_inspector.cohort_summary import CohortSummaryState
from evidence_inspector.composite_authority_fence import (
    CompositeAuthorityCoordinator,
    CompositeAuthorityFence,
)
from evidence_inspector.covariate_context_registry import CovariateContextRegistry
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistry,
    PolicyAuthorityState,
)
from evidence_inspector.longitudinal_comparison_registry import (
    MAX_SAVED_COMPARISONS,
    DependencyFenceKind,
    DependencySlot,
    LongitudinalComparisonRegistry,
    LongitudinalComparisonRegistryConflict,
    LongitudinalComparisonRegistryError,
    LongitudinalComparisonRegistryReadConflict,
    LongitudinalComparisonRegistryStale,
    LongitudinalComparisonRegistryUnsafe,
    RegisteredSavedComparisonV1,
    SavedComparisonAuthorityState,
    SavedComparisonCommitmentsV1,
    SavedComparisonDependencyScopeV1,
    SavedComparisonFiltersV1,
    SavedComparisonMeasurementV1,
    SavedComparisonSelectionV1,
    SavedE06SourceRefV1,
    SavedFamilyProjectionRequestV1,
    SavedLongitudinalComparisonV1,
    build_saved_longitudinal_comparison,
)
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalOutcome,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.longitudinal_workspace import (
    LongitudinalErrorCode,
    LongitudinalMeasurementSelection,
    LongitudinalRemediation,
    LongitudinalSegment,
    LongitudinalSourceRow,
    LongitudinalVersionDiff,
    LongitudinalWorkspace,
    LongitudinalWorkspaceBoundaryError,
    LongitudinalWorkspaceFilters,
    LongitudinalWorkspaceProjection,
    LongitudinalWorkspaceRequest,
    PublicTimeAxis,
    RowCompatibilityState,
    build_longitudinal_workspace,
    derive_version_diff,
    project_longitudinal_workspace,
)
from evidence_inspector.measurement_source_artifact_registry import (
    MeasurementSourceArtifactRegistry,
)
from evidence_inspector.method_registry import (
    MethodFamily,
    RegistryContract,
    canonical_contract_bytes,
)
from evidence_inspector.projection_policy_registry import (
    _STATISTIC_UNIT as _PROJECTION_STATISTIC_UNIT,
)
from evidence_inspector.projection_policy_registry import (
    ProjectionFamily,
    ProjectionPolicyRegistry,
    ProjectionSelectionRule,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
    ReaderAuthorization,
    ReaderAuthorizationDenied,
    ReaderAuthorizationRegistry,
    ReaderGrantBinding,
)
from evidence_inspector.record_supersession_store import RecordSupersessionStore
from evidence_inspector.repeatability_comparison_registry import (
    RepeatabilityComparisonRegistry,
)
from evidence_inspector.result_catalog import ResultCatalog
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from evidence_inspector.result_view_source_registry import ResultViewSourceRegistry

from .auth import BoundaryDenied, BrowserRequest
from .contracts import (
    PROTECTED_PUBLIC_KEYS,
    validate_public_key,
    validate_public_text,
)
from .reader_session import ReaderSessionBinder

ROUTE_PREFIX = "/api/v1/longitudinal/"
MAX_MEASUREMENT_SCOPES = 8
MAX_SCAN_PAGES = 10
MAX_LISTED_OPTIONS = 100
SELECTOR_PAGE_LIMIT = 50
SAVED_PAGE_LIMIT = 20
_PERMISSION_DENIED = {"error": {"code": "permission_denied"}}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COHORT_SELECTOR = re.compile(r"^cohort_selector_[0-9a-f]{40}$")
_ANCHOR_POLICY_SELECTOR = re.compile(r"^anchor_policy_[0-9a-f]{40}$")
_SAVED_SELECTOR = re.compile(r"^saved_comparison_[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[a-z0-9_]{1,96}$")
# Field names of protected D08/registry models.  None may appear in a public
# response at any depth: defence in depth beside the closed public schemas.  The
# shared identifier/session names and the key grammar live in web/contracts.py;
# these are the E12-only additions (E04 ``result_id``/``bundle_id`` are public in
# the explorer, so they are protected here only).
_PROTECTED_KEYS = PROTECTED_PUBLIC_KEYS | frozenset(
    {
        "dependency_heads",
        "live_dependency_heads",
        "committed_receipt_sha256",
        "state_head_sha256_binding",
        "saved_object_json",
        "result_id",
        "bundle_id",
    }
)


# --- closed public response contracts ------------------------------------------------


class SaveState(StrEnum):
    """Why Save is or is not offered; Save never authorizes export."""

    AVAILABLE = "available"
    REGISTRY_ABSENT = "registry_absent"
    REGISTRY_UNHEALTHY = "registry_unhealthy"
    REGISTRY_FULL = "registry_full"


class PublicSaveAvailability(RegistryContract):
    state: SaveState
    saving_authorizes_export: Literal[False] = False


class PublicCohortOption(RegistryContract):
    cohort_selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    authority_state: CohortAuthorityState
    member_count: int = Field(ge=1, le=10_000, strict=True)
    denominator_count: int = Field(ge=1, le=10_000, strict=True)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicMeasurementOption(RegistryContract):
    """One measurement the exact manifest admits through a registered projection."""

    measurement: LongitudinalMeasurementSelection
    projection_policy_selector_id: str = Field(
        pattern=r"^projection_policy_[0-9a-f]{40}$"
    )
    projection_policy_version: int = Field(ge=1, strict=True)
    latest_version: bool
    projection_family: ProjectionFamily
    selection_rule: ProjectionSelectionRule
    component_count: int = Field(ge=0, strict=True)
    projection_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    standalone_values: Literal["available", "unavailable"]
    missing_prerequisite: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")


class PublicAnchorPolicyOption(RegistryContract):
    anchor_policy_selector_id: str = Field(pattern=r"^anchor_policy_[0-9a-f]{40}$")
    anchor_policy_version: int = Field(ge=1, strict=True)
    authority_state: AnchorPolicyAuthorityState
    candidate_count: int | None
    eligible_count: int | None
    candidate_page_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class PublicAnchorCandidate(RegistryContract):
    anchor_selector_id: str = Field(pattern=r"^anchor_candidate_[0-9a-f]{40}$")
    alias: str = Field(pattern=r"^candidate_[0-9a-f]{12}$")
    biological_timepoint_ordinal: int = Field(ge=1, strict=True)
    time_offset_seconds: int = Field(ge=0, strict=True)
    method_version: str = Field(max_length=32)
    eligibility_state: AnchorEligibilityState


class PublicAnchorCandidatePage(RegistryContract):
    anchor_policy_selector_id: str = Field(pattern=r"^anchor_policy_[0-9a-f]{40}$")
    anchor_policy_version: int = Field(ge=1, strict=True)
    candidate_page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[PublicAnchorCandidate, ...] = Field(max_length=1_000)
    explicit_selection_required: Literal[True] = True


class PublicD09Option(RegistryContract):
    d09_policy_selector_id: str = Field(pattern=r"^d09_policy_[0-9a-f]{40}$")
    d09_policy_version: int = Field(ge=1, strict=True)
    authority_state: PolicyAuthorityState
    summary_state: CohortSummaryState | None


class LongitudinalSelectorCatalog(RegistryContract):
    """Bounded selector page for one journey step; opaque selectors only."""

    schema_version: Literal["traceback.e12-longitudinal-selectors.v1"] = (
        "traceback.e12-longitudinal-selectors.v1"
    )
    step: Literal["cohorts", "cohort_version", "anchor_candidates"]
    measurement_scopes: tuple[MeasurementScope, ...] = Field(
        max_length=MAX_MEASUREMENT_SCOPES
    )
    cohorts: tuple[PublicCohortOption, ...] = Field(max_length=SELECTOR_PAGE_LIMIT)
    next_after_selector_id: str | None = None
    next_after_version: int | None = None
    selected_cohort: PublicCohortOption | None = None
    measurement_options: tuple[PublicMeasurementOption, ...] = Field(
        default=(), max_length=MAX_LISTED_OPTIONS
    )
    anchor_policies: tuple[PublicAnchorPolicyOption, ...] = Field(
        default=(), max_length=MAX_LISTED_OPTIONS
    )
    d09_policies: tuple[PublicD09Option, ...] = Field(
        default=(), max_length=MAX_LISTED_OPTIONS
    )
    anchor_candidates: PublicAnchorCandidatePage | None = None
    options_truncated: bool = False
    save: PublicSaveAvailability
    free_form_identity_accepted: Literal[False] = False
    release_export_authorized: Literal[False] = False


class LongitudinalVersionDiffResponse(RegistryContract):
    schema_version: Literal["traceback.e12-longitudinal-version-diff.v1"] = (
        "traceback.e12-longitudinal-version-diff.v1"
    )
    cohort_selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    diff: LongitudinalVersionDiff
    shown_before_results: Literal[True] = True


class LongitudinalWorkspaceResponse(RegistryContract):
    schema_version: Literal["traceback.e12-longitudinal-workspace-response.v1"] = (
        "traceback.e12-longitudinal-workspace-response.v1"
    )
    workspace: LongitudinalWorkspaceProjection
    save: PublicSaveAvailability
    release_control: Literal["disabled"] = "disabled"
    export_control: Literal["disabled"] = "disabled"


class LongitudinalSourceDetail(RegistryContract):
    """One source row plus the authority and segments that touch it."""

    schema_version: Literal["traceback.e12-longitudinal-source-detail.v1"] = (
        "traceback.e12-longitudinal-source-detail.v1"
    )
    row: LongitudinalSourceRow
    segments: tuple[LongitudinalSegment, ...] = Field(max_length=2)
    time_axis: PublicTimeAxis
    cohort_selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_version: int = Field(ge=1, strict=True)
    cohort_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_status_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d03_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d07_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    denominator_ledger_label: Literal["operator-entered, unverified"] = (
        "operator-entered, unverified"
    )


class PublicSaveReceipt(RegistryContract):
    """Shown only after transactional publication and the final fence."""

    schema_version: Literal["traceback.e12-longitudinal-save-receipt.v1"] = (
        "traceback.e12-longitudinal-save-receipt.v1"
    )
    saved_selector_id: str = Field(pattern=r"^saved_comparison_[0-9a-f]{40}$")
    comparison_version: int = Field(ge=1, strict=True)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_state_version: int = Field(ge=1, strict=True)
    workspace_replay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied: bool
    dependency_fence_kind: Literal[DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE]
    final_fence_passed: Literal[True] = True
    saving_authorizes_export: Literal[False] = False


class PublicSavedComparisonRow(RegistryContract):
    saved_selector_id: str = Field(pattern=r"^saved_comparison_[0-9a-f]{40}$")
    comparison_version: int = Field(ge=1, strict=True)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_state: SavedComparisonAuthorityState
    stale_dependencies: tuple[DependencySlot, ...]


class LongitudinalSavedPage(RegistryContract):
    schema_version: Literal["traceback.e12-longitudinal-saved-page.v1"] = (
        "traceback.e12-longitudinal-saved-page.v1"
    )
    save: PublicSaveAvailability
    # ``requires_every_configured_scope``: the grant covers some configured
    # scopes but not all, so no saved selector is listed (Reopen is refused).
    listing_state: Literal["listed", "requires_every_configured_scope"] = "listed"
    records: tuple[PublicSavedComparisonRow, ...] = Field(max_length=SAVED_PAGE_LIMIT)
    next_after_selector_id: str | None = None
    next_after_version: int | None = None


class CommitmentChange(StrEnum):
    """Which saved authority commitment differs from the rebuilt workspace."""

    COHORT_MANIFEST = "cohort_manifest_changed"
    D03_ANCHOR_POLICY = "d03_anchor_policy_changed"
    D07_ENVELOPE = "d07_envelope_changed"
    APPROVED_ANCHOR = "approved_anchor_changed"
    D03_DECISION = "d03_decision_changed"
    D07_COMPARISONS = "d07_comparisons_changed"
    D09_SUMMARY = "d09_summary_changed"
    D10_CONTEXT = "d10_context_changed"
    D04_RECORD_HISTORY = "d04_record_history_changed"
    SOURCE_COMMITMENTS = "source_commitments_changed"
    READER_GRANT = "reader_grant_changed"
    PROJECTION_POLICY = "projection_policy_changed"
    DEPENDENCY_HEADS = "dependency_heads_changed"
    REPLAY = "workspace_replay_changed"
    PUBLICATION_NOT_COMPOSITE = "publication_not_composite_fenced"


_COMMITMENT_FIELDS: tuple[tuple[str, CommitmentChange], ...] = (
    ("cohort_manifest_sha256", CommitmentChange.COHORT_MANIFEST),
    ("d03_anchor_policy_sha256", CommitmentChange.D03_ANCHOR_POLICY),
    ("d07_envelope_sha256", CommitmentChange.D07_ENVELOPE),
    ("approved_anchor_sha256", CommitmentChange.APPROVED_ANCHOR),
    ("d03_decision_sha256", CommitmentChange.D03_DECISION),
    ("d07_comparisons_sha256", CommitmentChange.D07_COMPARISONS),
    ("d09_summary_sha256", CommitmentChange.D09_SUMMARY),
    ("d10_context_sha256", CommitmentChange.D10_CONTEXT),
    ("d04_record_history_sha256", CommitmentChange.D04_RECORD_HISTORY),
    ("source_commitments_sha256", CommitmentChange.SOURCE_COMMITMENTS),
    ("reader_grant_sha256", CommitmentChange.READER_GRANT),
)


class PublicSavedSelection(RegistryContract):
    """The saved selection's opaque selectors and controlled measurement only."""

    cohort_selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_version: int = Field(ge=1, strict=True)
    anchor_policy_selector_id: str = Field(pattern=r"^anchor_policy_[0-9a-f]{40}$")
    anchor_policy_version: int = Field(ge=1, strict=True)
    approved_anchor_selector_id: str = Field(pattern=r"^anchor_candidate_[0-9a-f]{40}$")
    projection_policy_selector_id: str = Field(
        pattern=r"^projection_policy_[0-9a-f]{40}$"
    )
    projection_policy_version: int = Field(ge=1, strict=True)
    d09_policy_selector_id: str = Field(pattern=r"^d09_policy_[0-9a-f]{40}$")
    d09_policy_version: int = Field(ge=1, strict=True)
    measurement: LongitudinalMeasurementSelection


class LongitudinalReopenDiff(RegistryContract):
    """Version, authority and policy diff, presented before any result."""

    schema_version: Literal["traceback.e12-longitudinal-reopen-diff.v1"] = (
        "traceback.e12-longitudinal-reopen-diff.v1"
    )
    saved_selector_id: str = Field(pattern=r"^saved_comparison_[0-9a-f]{40}$")
    comparison_version: int = Field(ge=1, strict=True)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    saved_selection: PublicSavedSelection
    saved_workspace_replay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rebuilt_workspace_replay_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    registry_authority_state: SavedComparisonAuthorityState
    stale_dependencies: tuple[DependencySlot, ...]
    cohort_version_diff: LongitudinalVersionDiff | None
    latest_cohort_version: int | None
    newer_cohort_version_available: bool
    changes: tuple[CommitmentChange, ...]
    rebuild_error: LongitudinalErrorCode | None
    comparison_state: SavedComparisonAuthorityState
    silent_upgrade: Literal[False] = False
    saved_bytes_rewritten: Literal[False] = False
    shown_before_results: Literal[True] = True


class PublicSavedCommitments(RegistryContract):
    """The saved object's immutable authority digests (no reader grant)."""

    cohort_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d03_anchor_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d07_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_anchor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d03_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d07_comparisons_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d09_summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d10_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d04_record_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commitments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LongitudinalReopenResponse(RegistryContract):
    schema_version: Literal["traceback.e12-longitudinal-reopen.v1"] = (
        "traceback.e12-longitudinal-reopen.v1"
    )
    stage: Literal["diff", "results"]
    diff: LongitudinalReopenDiff
    save: PublicSaveAvailability
    current_workspace: LongitudinalWorkspaceProjection | None = None
    # A stale reopen shows only the immutable saved commitments: the saved
    # bytes hold no rows, and current rows would relabel current authority as
    # the saved comparison.  No values, no comparison numbers, no segments.
    historical_commitments: PublicSavedCommitments | None = None
    stale_segments: tuple[LongitudinalSegment, ...] = Field(default=(), max_length=0)
    refresh_action: Literal["start_new_comparison_at_current_authority"] | None = None
    release_control: Literal["disabled"] = "disabled"
    export_control: Literal["disabled"] = "disabled"


# --- the optional longitudinal source adapter ------------------------------------------


class _SealedSourceType(type):
    def __setattr__(cls, name: str, value: object) -> None:
        raise TypeError("longitudinal source class is sealed")

    def __delattr__(cls, name: str) -> None:
        raise TypeError("longitudinal source class is sealed")


_STORE_TYPES: tuple[tuple[str, type], ...] = (
    ("reader_authorization_registry", ReaderAuthorizationRegistry),
    ("linkage_store", ProviderLinkageStore),
    ("cohort_registry", CohortRegistry),
    ("cohort_record_catalog", CohortRecordCatalog),
    ("result_catalog", ResultCatalog),
    ("result_trust_registry", ResultTrustRegistry),
    ("supersession_store", RecordSupersessionStore),
    ("anchor_policy_registry", AnchorPolicyRegistry),
    ("projection_policy_registry", ProjectionPolicyRegistry),
    ("result_view_source_registry", ResultViewSourceRegistry),
    ("measurement_source_artifact_registry", MeasurementSourceArtifactRegistry),
    ("d03_decision_registry", LongitudinalDecisionRegistry),
    ("d07_comparison_registry", RepeatabilityComparisonRegistry),
    ("d09_summary_registry", DenominatorPolicyRegistry),
    ("d10_context_registry", CovariateContextRegistry),
)


class LongitudinalExplorerSource(metaclass=_SealedSourceType):
    """Exact store set for the E12 routes plus the optional saved registry.

    ``measurement_scopes`` is operator configuration: the D02 measurements the
    selector step offers (each still needs a grant scope).  ``comparison_registry``
    is the installed ``LongitudinalComparisonRegistry``; without it Save is
    disabled and Reopen is unavailable.
    """

    __slots__ = ("_coordinator", "_now", "_registry", "_scopes", "_stores")

    def __init__(
        self,
        *,
        stores: Mapping[str, object],
        measurement_scopes: Sequence[MeasurementScope],
        comparison_registry: LongitudinalComparisonRegistry | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if set(stores) != {name for name, _ in _STORE_TYPES}:
            raise TypeError("longitudinal source requires the exact store set")
        for name, cls in _STORE_TYPES:
            if type(stores[name]) is not cls:
                raise TypeError("longitudinal source requires the exact store types")
        if comparison_registry is not None and (
            type(comparison_registry) is not LongitudinalComparisonRegistry
        ):
            raise TypeError("longitudinal source requires the exact saved registry")
        scopes = tuple(measurement_scopes)
        if not 1 <= len(scopes) <= MAX_MEASUREMENT_SCOPES or any(
            type(item) is not MeasurementScope for item in scopes
        ):
            raise TypeError("longitudinal source requires 1..8 measurement scopes")
        if len({canonical_contract_bytes(item) for item in scopes}) != len(scopes):
            raise ValueError("measurement scopes must be unique")
        captured = {name: stores[name] for name, _ in _STORE_TYPES}
        # The composite coordinator validates that the stores are wired to one
        # another; it also backs every Save/Reopen fence.
        coordinator = CompositeAuthorityCoordinator(
            linkage_store=captured["linkage_store"],
            record_history_store=captured["supersession_store"],
            cohort_registry=captured["cohort_registry"],
            reader_registry=captured["reader_authorization_registry"],
            record_catalog=captured["cohort_record_catalog"],
            result_catalog=captured["result_catalog"],
            result_trust_registry=captured["result_trust_registry"],
            source_registry=captured["result_view_source_registry"],
            decision_registry=captured["d03_decision_registry"],
            comparison_registry=captured["d07_comparison_registry"],
            d09_registry=captured["d09_summary_registry"],
            d10_registry=captured["d10_context_registry"],
            family_source_registry=captured["measurement_source_artifact_registry"],
            anchor_registry=captured["anchor_policy_registry"],
            projection_registry=captured["projection_policy_registry"],
        )
        object.__setattr__(self, "_stores", captured)
        object.__setattr__(self, "_scopes", scopes)
        object.__setattr__(self, "_registry", comparison_registry)
        object.__setattr__(self, "_coordinator", coordinator)
        object.__setattr__(self, "_now", now or (lambda: datetime.now(UTC)))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("longitudinal source is sealed")

    def __delattr__(self, name: str) -> None:
        raise TypeError("longitudinal source is sealed")

    @property
    def reader_registry(self) -> ReaderAuthorizationRegistry:
        return self._stores["reader_authorization_registry"]  # type: ignore[return-value]

    @property
    def result_catalog(self) -> ResultCatalog:
        return self._stores["result_catalog"]  # type: ignore[return-value]

    @property
    def measurement_scopes(self) -> tuple[MeasurementScope, ...]:
        return self._scopes

    def store(self, name: str) -> Any:
        return self._stores[name]

    def builder_stores(self) -> dict[str, object]:
        return dict(self._stores)

    @property
    def comparison_registry(self) -> LongitudinalComparisonRegistry | None:
        return self._registry

    @property
    def coordinator(self) -> CompositeAuthorityCoordinator:
        return self._coordinator

    def now(self) -> datetime:
        value = self._now()
        if type(value) is not datetime or value.utcoffset() is None:
            raise TypeError("longitudinal clock must return an aware datetime")
        return value.astimezone(UTC).replace(microsecond=0)


# --- errors ---------------------------------------------------------------------------


class _RouteFailure(Exception):
    """Internal: one closed (status, code, remediation) outcome."""

    def __init__(self, status: int, code: str, remediation: str | None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.remediation = remediation


class _Denied(Exception):
    """Internal: the bounded permission-denied shell."""


_STATUS = {
    LongitudinalErrorCode.INVALID_REQUEST: 400,
    LongitudinalErrorCode.PERMISSION_DENIED: 403,
    LongitudinalErrorCode.AUTHORITY_STALE: 409,
    LongitudinalErrorCode.TRUST_REVOKED: 409,
    LongitudinalErrorCode.READ_CONFLICT: 409,
    LongitudinalErrorCode.INTEGRITY_FAILURE: 500,
    LongitudinalErrorCode.STORAGE_FAILURE: 503,
}


def _fail(
    code: LongitudinalErrorCode, remediation: LongitudinalRemediation
) -> NoReturn:
    if code is LongitudinalErrorCode.PERMISSION_DENIED:
        raise _Denied
    raise _RouteFailure(_STATUS[code], code.value, remediation.value)


def _invalid() -> NoReturn:
    _fail(
        LongitudinalErrorCode.INVALID_REQUEST, LongitudinalRemediation.CORRECT_REQUEST
    )


def _registry_failure(exc: BaseException) -> NoReturn:
    """Map a saved-registry failure to one closed code; never its text."""

    if isinstance(exc, LongitudinalComparisonRegistryUnsafe):
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    if isinstance(exc, LongitudinalComparisonRegistryReadConflict):
        _fail(LongitudinalErrorCode.READ_CONFLICT, LongitudinalRemediation.RETRY_READ)
    if isinstance(exc, LongitudinalComparisonRegistryStale):
        _fail(LongitudinalErrorCode.AUTHORITY_STALE, LongitudinalRemediation.RETRY_READ)
    if isinstance(exc, LongitudinalComparisonRegistryConflict):
        _fail(LongitudinalErrorCode.READ_CONFLICT, LongitudinalRemediation.RETRY_READ)
    if isinstance(exc, OSError):
        _fail(
            LongitudinalErrorCode.STORAGE_FAILURE,
            LongitudinalRemediation.CHECK_LOCAL_STORAGE,
        )
    _fail(
        LongitudinalErrorCode.INTEGRITY_FAILURE,
        LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
    )


def _store_read(operation: Callable[[], Any]) -> Any:
    """Run one public store read; a failure is stale authority, never detail."""

    failure: BaseException | None = None
    try:
        return operation()
    except (_Denied, _RouteFailure, ReaderAuthorizationDenied, BoundaryDenied):
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to one closed code below
        failure = exc
    unsafe = type(failure).__name__.endswith("Unsafe")
    if unsafe:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    if isinstance(failure, OSError):
        _fail(
            LongitudinalErrorCode.STORAGE_FAILURE,
            LongitudinalRemediation.CHECK_LOCAL_STORAGE,
        )
    _fail(LongitudinalErrorCode.AUTHORITY_STALE, LongitudinalRemediation.RETRY_READ)


# --- pinned store reads ----------------------------------------------------------------

_PINS: dict[tuple[type, str], Any] = {
    (cls, name): cls.__dict__[name]
    for cls, names in (
        (CohortRegistry, ("list_selectors", "resolve_history")),
        (AnchorPolicyRegistry, ("list_selectors", "derive_candidate_page")),
        (ProjectionPolicyRegistry, ("list_selectors", "resolve")),
        (DenominatorPolicyRegistry, ("list_selectors",)),
        (ResultViewSourceRegistry, ("registry_identity",)),
        (
            LongitudinalComparisonRegistry,
            ("identity", "register", "resolve", "list_selectors"),
        ),
    )
    for name in names
}
_PINNED_BUILD = build_longitudinal_workspace
_PINNED_PROJECT = project_longitudinal_workspace
_PINNED_SELECTOR_ID = saved_module._selector_id


def _call(store: object, cls: type, name: str, *args: Any, **kwargs: Any) -> Any:
    function = _PINS[(cls, name)]
    if type(store) is not cls or cls.__dict__.get(name) is not function:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    return function(store, *args, **kwargs)


# --- public encoding ---------------------------------------------------------------------


def validate_longitudinal_public(value: object, *, key: str = "") -> None:
    """Every key is a controlled name, no protected field name appears, and every
    string is either a typed 64-hex digest or passes the public-text boundary."""

    if isinstance(value, Mapping):
        for name, nested in value.items():
            validate_longitudinal_public(
                nested, key=validate_public_key(name, protected=_PROTECTED_KEYS)
            )
    elif isinstance(value, (list, tuple)):
        for nested in value:
            validate_longitudinal_public(nested, key=key)
    elif isinstance(value, str):
        if not _HEX64.fullmatch(value):
            validate_public_text(value)
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError("public value has an unsupported type")


def _public(model: RegistryContract) -> dict[str, object]:
    payload = model.model_dump(mode="json")
    validate_longitudinal_public(payload)
    return payload


# --- reader authorization ------------------------------------------------------------


def _cohort_registry_id(source: LongitudinalExplorerSource) -> str:
    """The lock-free immutable cohort binding of the E06 registry (as D08 uses)."""

    try:
        identity = _call(
            source.store("result_view_source_registry"),
            ResultViewSourceRegistry,
            "registry_identity",
        )
        return str(identity.cohort_registry_id)
    except Exception:  # noqa: BLE001 - an unreadable binding denies
        raise _Denied from None


class _Gate:
    """``with`` one reader authorization for one exact scope under its fence.

    Private to :class:`_AuthorizedView`; routes never construct it.
    """

    def __init__(
        self,
        binder: ReaderSessionBinder,
        request: BrowserRequest,
        source: LongitudinalExplorerSource,
        scope: MeasurementScope,
    ) -> None:
        self._context = binder.reader_authorization(
            request,
            cohort_registry_id=_cohort_registry_id(source),
            measurement_scope=scope,
        )

    def __enter__(self) -> ReaderAuthorization:
        try:
            return self._context.__enter__()
        except ReaderAuthorizationDenied:
            raise _Denied from None

    def __exit__(self, *exc: object) -> bool:
        try:
            return bool(self._context.__exit__(*exc))
        except ReaderAuthorizationDenied:
            raise _Denied from None


class _AuthorizedView:
    """One request's reader authorization, built before any route runs.

    ``configured`` is the operator's measurement set; ``granted`` is the part
    the session's own grant currently covers (``configured`` intersected with
    the grant).  No grant over any configured scope denies at construction.
    Routes read authority only through :meth:`gate` (deny unless the scope is
    in ``granted``, then the live reader gate) and :meth:`saved_gate` (deny
    unless ``granted == configured``), so no route derives scope itself.
    """

    __slots__ = ("_binder", "_request", "_source", "configured", "granted")

    def __init__(
        self,
        binder: ReaderSessionBinder | None,
        request: BrowserRequest,
        source: LongitudinalExplorerSource,
    ) -> None:
        if type(binder) is not ReaderSessionBinder or not binder.enabled:
            raise _Denied
        self._binder = binder
        self._request = request
        self._source = source
        self.configured: tuple[MeasurementScope, ...] = tuple(source.measurement_scopes)
        granted: list[MeasurementScope] = []
        for scope in self.configured:
            try:
                with _Gate(binder, request, source, scope):
                    pass
            except _Denied:
                continue
            granted.append(scope)
        if not granted:
            raise _Denied
        self.granted: tuple[MeasurementScope, ...] = tuple(granted)

    def gate(self, scope: MeasurementScope) -> _Gate:
        """The live reader gate for one configured, granted scope."""

        if type(scope) is not MeasurementScope or scope not in self.granted:
            raise _Denied
        return _Gate(self._binder, self._request, self._source, scope)

    def saved_gate(self) -> None:
        """Saved selectors and objects need a grant over every configured scope."""

        if self.granted != self.configured:
            raise _Denied
        for scope in self.configured:
            with self.gate(scope):
                pass

    def session_credential(self) -> ReaderGrantBinding:
        return self._binder.session_credential(self._request)

    def held_authorization(self, scope: MeasurementScope) -> ReaderAuthorization:
        """Re-authorize inside a composite hold (which holds the reader fence)."""

        if type(scope) is not MeasurementScope or scope not in self.granted:
            raise _Denied
        return self._binder.reader_authorization_in_held_fence(
            self._request,
            cohort_registry_id=_cohort_registry_id(self._source),
            measurement_scope=scope,
        )


def _scope_of(measurement: LongitudinalMeasurementSelection) -> MeasurementScope:
    return MeasurementScope(
        family=measurement.family,
        quantity_id=measurement.quantity_id,
        unit=measurement.unit,
    )


def _same_grant(first: ReaderAuthorization, final: ReaderAuthorization) -> None:
    if (
        final.grant_sha256 != first.grant_sha256
        or final.cohort_registry_id != first.cohort_registry_id
        or final.measurement_scope != first.measurement_scope
    ):
        raise _Denied


# --- save availability -------------------------------------------------------------


def save_availability(source: LongitudinalExplorerSource) -> PublicSaveAvailability:
    registry = source.comparison_registry
    if registry is None:
        return PublicSaveAvailability(state=SaveState.REGISTRY_ABSENT)
    try:
        _, _, version, _ = _call(registry, LongitudinalComparisonRegistry, "identity")
    except Exception:  # noqa: BLE001 - corrupt, stale or rolled back
        return PublicSaveAvailability(state=SaveState.REGISTRY_UNHEALTHY)
    if type(version) is not int or version >= MAX_SAVED_COMPARISONS:
        return PublicSaveAvailability(state=SaveState.REGISTRY_FULL)
    return PublicSaveAvailability(state=SaveState.AVAILABLE)


# --- request parsing ----------------------------------------------------------------


def _parse_workspace_request(value: object) -> LongitudinalWorkspaceRequest:
    if not isinstance(value, dict):
        _invalid()
    try:
        return LongitudinalWorkspaceRequest.model_validate_json(json.dumps(value))
    except (ValidationError, TypeError, ValueError):
        _invalid()


def _single(params: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = params.get(name)
    if values is None:
        return None
    if len(values) != 1:
        _invalid()
    return values[0]


def _int_param(params: Mapping[str, Sequence[str]], name: str) -> int | None:
    value = _single(params, name)
    if value is None:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 6:
        _invalid()
    number = int(value)
    if number < 1:
        _invalid()
    return number


def _scope_param(params: Mapping[str, Sequence[str]]) -> MeasurementScope | None:
    family = _single(params, "family")
    quantity = _single(params, "quantity_id")
    unit = _single(params, "unit")
    if family is None and quantity is None and unit is None:
        return None
    if family is None or quantity is None or unit is None:
        _invalid()
    try:
        return MeasurementScope(
            family=MethodFamily(family), quantity_id=quantity, unit=unit
        )
    except (ValidationError, ValueError):
        _invalid()


# --- selector catalogue ---------------------------------------------------------------


def _cohort_option(record: Any) -> PublicCohortOption:
    return PublicCohortOption(
        cohort_selector_id=record.selector_id,
        cohort_version=record.cohort_version,
        authority_state=record.authority_state,
        member_count=record.member_count,
        denominator_count=record.denominator_count,
        manifest_sha256=record.manifest_sha256,
    )


def _cohort_page(
    cohort: CohortRegistry,
    authorization: ReaderAuthorization,
    *,
    after_selector_id: str | None,
    after_version: int | None,
    limit: int,
) -> Any:
    cursor: dict[str, object] = {}
    if after_selector_id is not None:
        cursor = {
            "after_selector_id": after_selector_id,
            "after_version": after_version,
        }
    page = _store_read(
        lambda: _call(cohort, CohortRegistry, "list_selectors", limit=limit, **cursor)
    )
    if page.registry_id != authorization.cohort_registry_id:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    return page


def _selector_versions(
    cohort: CohortRegistry, authorization: ReaderAuthorization, selector_id: str
) -> list[Any]:
    """Every version row of one cohort selector, via the bounded D05 page."""

    number = int(selector_id[len("cohort_selector_") :], 16)
    after: tuple[str | None, int | None] = (
        (None, None) if number == 0 else (f"cohort_selector_{number - 1:040x}", 100_000)
    )
    rows: list[Any] = []
    for _ in range(MAX_SCAN_PAGES):
        page = _cohort_page(
            cohort,
            authorization,
            after_selector_id=after[0],
            after_version=after[1],
            limit=100,
        )
        matching = [item for item in page.records if item.selector_id == selector_id]
        rows.extend(matching)
        if page.next_after_selector_id is None or len(matching) != len(page.records):
            return rows
        after = (page.next_after_selector_id, page.next_after_version)
    return rows


def _scan(
    fetch: Callable[[dict[str, object]], Any], cursor_name: str
) -> tuple[list[Any], bool]:
    records: list[Any] = []
    cursor: dict[str, object] = {}
    for _ in range(MAX_SCAN_PAGES):
        page = fetch(cursor)
        records.extend(page.records)
        if page.next_after_selector_id is None:
            return records, False
        cursor = {
            "after_selector_id": page.next_after_selector_id,
            cursor_name: getattr(page, f"next_{cursor_name}"),
        }
    return records, True


def _measurement_options(
    source: LongitudinalExplorerSource,
    cohort_row: Any,
    scope: MeasurementScope,
) -> tuple[list[PublicMeasurementOption], bool]:
    registry = source.store("projection_policy_registry")
    records, truncated = _scan(
        lambda cursor: _store_read(
            lambda: _call(
                registry,
                ProjectionPolicyRegistry,
                "list_selectors",
                limit=100,
                **cursor,
            )
        ),
        "after_policy_version",
    )
    options: list[PublicMeasurementOption] = []
    for record in records:
        if record.anchor_definition_sha256 != cohort_row.anchor_definition_sha256:
            continue
        resolved = _store_read(
            lambda record=record: _call(
                registry,
                ProjectionPolicyRegistry,
                "resolve",
                record.selector_id,
                record.policy_version,
            )
        )
        policy = resolved.policy
        anchor = policy.measurement_anchor
        if (
            anchor.measurement_definition_sha256
            != cohort_row.measurement_definition_sha256
            or anchor.anchor_definition_sha256 != cohort_row.anchor_definition_sha256
            or anchor.authority_sha256 != cohort_row.anchor_authority_sha256
            or policy.measurement.method_definition.family != scope.family
            or policy.measurement.quantity_id != scope.quantity_id
            or policy.measurement.unit != scope.unit
        ):
            continue
        family = record.family
        prerequisite = {
            ProjectionFamily.CELL_ORIGIN: "e08_cell_origin_artifact_binding",
            ProjectionFamily.CNA_CHROMOSOME: "e09_cna_artifact_binding",
            ProjectionFamily.CNA_SEGMENT: "e09_cna_artifact_binding",
        }.get(family)
        options.append(
            PublicMeasurementOption(
                measurement=LongitudinalMeasurementSelection(
                    family=scope.family,
                    quantity_id=scope.quantity_id,
                    unit=scope.unit,
                    measurement_definition_sha256=(
                        policy.measurement.measurement_definition_sha256
                    ),
                ),
                projection_policy_selector_id=record.selector_id,
                projection_policy_version=record.policy_version,
                latest_version=record.latest_version,
                projection_family=family,
                selection_rule=record.selection_rule,
                component_count=record.component_count,
                projection_policy_sha256=record.policy_sha256,
                standalone_values="unavailable" if prerequisite else "available",
                missing_prerequisite=prerequisite,
            )
        )
        if len(options) >= MAX_LISTED_OPTIONS:
            return options, True
    return options, truncated


def _anchor_options(
    source: LongitudinalExplorerSource, cohort_row: Any
) -> tuple[list[PublicAnchorPolicyOption], bool]:
    registry = source.store("anchor_policy_registry")
    records, truncated = _scan(
        lambda cursor: _store_read(
            lambda: _call(
                registry, AnchorPolicyRegistry, "list_selectors", limit=100, **cursor
            )
        ),
        "after_approval_version",
    )
    options = [
        PublicAnchorPolicyOption(
            anchor_policy_selector_id=record.selector_id,
            anchor_policy_version=record.approval_version,
            authority_state=record.authority_state,
            candidate_count=record.candidate_count,
            eligible_count=record.eligible_count,
            candidate_page_sha256=record.candidate_page_sha256,
        )
        for record in records
        if record.cohort_manifest_sha256 == cohort_row.manifest_sha256
    ]
    return options[:MAX_LISTED_OPTIONS], truncated or len(options) > MAX_LISTED_OPTIONS


def _d09_options(
    source: LongitudinalExplorerSource, cohort_row: Any
) -> tuple[list[PublicD09Option], bool]:
    registry = source.store("d09_summary_registry")
    records, truncated = _scan(
        lambda cursor: _store_read(
            lambda: _call(
                registry,
                DenominatorPolicyRegistry,
                "list_selectors",
                limit=100,
                **cursor,
            )
        ),
        "after_policy_version",
    )
    options = [
        PublicD09Option(
            d09_policy_selector_id=record.selector_id,
            d09_policy_version=record.policy_version,
            authority_state=record.authority_state,
            summary_state=record.summary_state,
        )
        for record in records
        if record.cohort_manifest_sha256 == cohort_row.manifest_sha256
    ]
    return options[:MAX_LISTED_OPTIONS], truncated or len(options) > MAX_LISTED_OPTIONS


def _candidate_page(
    source: LongitudinalExplorerSource,
    cohort_row: Any,
    selector_id: str,
    version: int,
) -> PublicAnchorCandidatePage:
    page = _store_read(
        lambda: _call(
            source.store("anchor_policy_registry"),
            AnchorPolicyRegistry,
            "derive_candidate_page",
            selector_id,
            version,
        )
    )
    if page.cohort_manifest_sha256 != cohort_row.manifest_sha256:
        _invalid()
    return PublicAnchorCandidatePage(
        anchor_policy_selector_id=page.policy_selector_id,
        anchor_policy_version=page.approval_version,
        candidate_page_sha256=page.candidate_page_sha256,
        candidates=tuple(
            PublicAnchorCandidate(
                anchor_selector_id=item.anchor_selector_id,
                alias=item.alias,
                biological_timepoint_ordinal=item.biological_timepoint_ordinal,
                time_offset_seconds=item.time_offset_seconds,
                method_version=item.method_version,
                eligibility_state=item.eligibility_state,
            )
            for item in page.candidates
        ),
    )


def selector_catalog(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    params: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    allowed = set(params) - {
        "after_selector_id",
        "after_version",
        "cohort_selector_id",
        "cohort_version",
        "family",
        "quantity_id",
        "unit",
        "anchor_policy_selector_id",
        "anchor_policy_version",
    }
    scopes = view.granted
    if allowed:
        _invalid()
    cohort_id = _single(params, "cohort_selector_id")
    cohort_version = _int_param(params, "cohort_version")
    after_id = _single(params, "after_selector_id")
    after_version = _int_param(params, "after_version")
    anchor_id = _single(params, "anchor_policy_selector_id")
    anchor_version = _int_param(params, "anchor_policy_version")
    scope = _scope_param(params)
    if (after_id is None) != (after_version is None) or (
        after_id is not None and not _COHORT_SELECTOR.fullmatch(after_id)
    ):
        _invalid()
    if (cohort_id is None) != (cohort_version is None) or (
        cohort_id is not None and not _COHORT_SELECTOR.fullmatch(cohort_id)
    ):
        _invalid()
    if (anchor_id is None) != (anchor_version is None) or (
        anchor_id is not None and not _ANCHOR_POLICY_SELECTOR.fullmatch(anchor_id)
    ):
        _invalid()
    if cohort_id is not None and scope is None:
        _invalid()
    if anchor_id is not None and cohort_id is None:
        _invalid()
    gate_scope = scope if scope is not None else scopes[0]
    cohort = source.store("cohort_registry")
    with view.gate(gate_scope) as authorization:
        save = save_availability(source)
        if cohort_id is None:
            page = _cohort_page(
                cohort,
                authorization,
                after_selector_id=after_id,
                after_version=after_version,
                limit=SELECTOR_PAGE_LIMIT,
            )
            catalog = LongitudinalSelectorCatalog(
                step="cohorts",
                measurement_scopes=scopes,
                cohorts=tuple(_cohort_option(item) for item in page.records),
                next_after_selector_id=page.next_after_selector_id,
                next_after_version=page.next_after_version,
                save=save,
            )
        else:
            assert scope is not None and cohort_version is not None
            rows = [
                item
                for item in _selector_versions(cohort, authorization, cohort_id)
                if item.cohort_version == cohort_version
            ]
            if len(rows) != 1:
                _fail(
                    LongitudinalErrorCode.INVALID_REQUEST,
                    LongitudinalRemediation.RESELECT_COHORT_VERSION,
                )
            row = rows[0]
            measurements, m_truncated = _measurement_options(source, row, scope)
            anchors, a_truncated = _anchor_options(source, row)
            d09s, d_truncated = _d09_options(source, row)
            candidates = (
                _candidate_page(source, row, anchor_id, anchor_version)  # type: ignore[arg-type]
                if anchor_id is not None
                else None
            )
            catalog = LongitudinalSelectorCatalog(
                step="anchor_candidates"
                if candidates is not None
                else "cohort_version",
                measurement_scopes=scopes,
                cohorts=(),
                selected_cohort=_cohort_option(row),
                measurement_options=tuple(measurements),
                anchor_policies=tuple(anchors),
                d09_policies=tuple(d09s),
                anchor_candidates=candidates,
                options_truncated=m_truncated or a_truncated or d_truncated,
                save=save,
            )
        payload = _public(catalog)
    return payload


# --- version diff -----------------------------------------------------------------


def _version_diff(
    source: LongitudinalExplorerSource,
    authorization: ReaderAuthorization,
    selector_id: str,
    version: int,
) -> LongitudinalVersionDiff:
    history = _store_read(
        lambda: _call(
            source.store("cohort_registry"),
            CohortRegistry,
            "resolve_history",
            selector_id,
            version,
        )
    )
    if (
        history.registry_id != authorization.cohort_registry_id
        or history.manifests[-1].version != version
    ):
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    return derive_version_diff(tuple(history.manifests))


def version_diff(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    params: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    if set(params) - {
        "cohort_selector_id",
        "cohort_version",
        "family",
        "quantity_id",
        "unit",
    }:
        _invalid()
    scope = _scope_param(params)
    selector_id = _single(params, "cohort_selector_id")
    version = _int_param(params, "cohort_version")
    if (
        scope is None
        or selector_id is None
        or version is None
        or not (_COHORT_SELECTOR.fullmatch(selector_id))
    ):
        _invalid()
    with view.gate(scope) as authorization:
        diff = _version_diff(source, authorization, selector_id, version)
        payload = _public(
            LongitudinalVersionDiffResponse(cohort_selector_id=selector_id, diff=diff)
        )
    return payload


# --- workspace and source detail ----------------------------------------------------


def _build(
    view: _AuthorizedView,
    source: LongitudinalExplorerSource,
    workspace_request: LongitudinalWorkspaceRequest,
) -> tuple[LongitudinalWorkspace, ReaderAuthorization]:
    """Authorize first, then build from live authority with the session binding."""

    scope = _scope_of(workspace_request.measurement)
    with view.gate(scope) as first:
        pass
    credential = view.session_credential()
    failure: LongitudinalWorkspaceBoundaryError | None = None
    workspace: LongitudinalWorkspace | None = None
    try:
        workspace = _PINNED_BUILD(
            workspace_request,
            reader_session_credential=credential,
            **source.builder_stores(),
        )
    except LongitudinalWorkspaceBoundaryError as exc:
        failure = exc
    if failure is not None:
        _fail(failure.code, failure.remediation)
    assert workspace is not None
    if workspace.reader_authorization.grant_sha256 != first.grant_sha256:
        raise _Denied
    return workspace, first


def _workspace_body(body: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != keys:
        _invalid()
    return body


def workspace(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    body: object,
) -> dict[str, object]:
    values = _workspace_body(body, {"request"})
    workspace_request = _parse_workspace_request(values["request"])
    built, first = _build(view, source, workspace_request)
    projection = _PINNED_PROJECT(built)
    payload = _public(
        LongitudinalWorkspaceResponse(
            workspace=projection, save=save_availability(source)
        )
    )
    with view.gate(_scope_of(workspace_request.measurement)) as final:
        _same_grant(first, final)
    return payload


def source_detail(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    body: object,
) -> dict[str, object]:
    values = _workspace_body(body, {"request", "row_ordinal"})
    workspace_request = _parse_workspace_request(values["request"])
    ordinal = values["row_ordinal"]
    if type(ordinal) is not int or not 1 <= ordinal <= 1_000:
        _invalid()
    built, first = _build(view, source, workspace_request)
    projection = _PINNED_PROJECT(built)
    # Only a row the request's filters make visible, and only visible segments.
    rows = [row for row in projection.rows if row.row_ordinal == ordinal]
    if len(rows) != 1:
        _invalid()
    authority = projection.authority
    detail = LongitudinalSourceDetail(
        row=rows[0],
        segments=tuple(
            item
            for item in projection.segments
            if ordinal in (item.from_row_ordinal, item.to_row_ordinal)
        ),
        time_axis=projection.time_axis,
        cohort_selector_id=authority.cohort_selector_id,
        cohort_version=authority.cohort_version,
        cohort_manifest_sha256=authority.cohort_manifest_sha256,
        record_status_sha256=authority.record_status_sha256,
        d03_policy_sha256=authority.d03_policy_sha256,
        d07_envelope_sha256=authority.d07_envelope_sha256,
        projection_policy_sha256=authority.projection_policy_sha256,
        replay_sha256=projection.replay_sha256,
        filters_sha256=projection.filters_sha256,
    )
    payload = _public(detail)
    with view.gate(_scope_of(workspace_request.measurement)) as final:
        _same_grant(first, final)
    return payload


# --- save ----------------------------------------------------------------------------


def _digest(domain: bytes, *parts: bytes) -> str:
    digest = hashlib.sha256(domain)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def saved_commitments(workspace: LongitudinalWorkspace) -> SavedComparisonCommitmentsV1:
    """Exact digests of every authority-derived input the workspace replayed."""

    authority = workspace.authority
    rows = workspace.protected_rows
    return SavedComparisonCommitmentsV1(
        cohort_manifest_sha256=authority.cohort_manifest_sha256,
        d03_anchor_policy_sha256=authority.d03_policy_sha256,
        d07_envelope_sha256=authority.d07_envelope_sha256,
        approved_anchor_sha256=workspace.anchor_record_sha256,
        d03_decision_sha256=(
            authority.d03_series_decision_sha256
            or _digest(
                b"traceback-e12-saved-d03-absent-v1",
                authority.d03_series_state.value.encode("ascii"),
            )
        ),
        d07_comparisons_sha256=_digest(
            b"traceback-e12-saved-d07-comparisons-v1",
            *(
                b"%d:%s:%s"
                % (
                    row.row_ordinal,
                    row.comparison_state.value.encode("ascii"),
                    (
                        row.comparison.comparison_sha256.encode("ascii")
                        if row.comparison is not None
                        else b"-"
                    ),
                )
                for row in rows
            ),
        ),
        d09_summary_sha256=authority.d09_summary_sha256,
        d10_context_sha256=(
            workspace.covariate_context.context_sha256
            or _digest(
                b"traceback-e12-saved-d10-absent-v1",
                workspace.covariate_context.state.value.encode("ascii"),
            )
        ),
        d04_record_history_sha256=_digest(
            b"traceback-e12-saved-d04-history-v1",
            *(canonical_contract_bytes(row.history) for row in rows),
        ),
        source_commitments_sha256=_digest(
            b"traceback-e12-saved-sources-v1",
            *(
                canonical_contract_bytes(row.source) if row.source is not None else b"-"
                for row in rows
            ),
        ),
        reader_grant_sha256=workspace.reader_authorization.grant_sha256,
    )


def _saved_filters(filters: LongitudinalWorkspaceFilters) -> SavedComparisonFiltersV1:
    outcomes = []
    for state in filters.compatibility_states:
        try:
            outcomes.append(LongitudinalOutcome(state.value))
        except ValueError:
            # Anchor and not-evaluated filters have no saved representation.
            _invalid()
    return SavedComparisonFiltersV1(
        timepoint_ordinals=filters.timepoint_ordinals,
        lineage_roles=filters.lineage_roles,
        record_availability=filters.record_availability,
        compatibility_outcomes=tuple(
            item for item in LongitudinalOutcome if item in outcomes
        ),
    )


def _workspace_filters(
    filters: SavedComparisonFiltersV1,
) -> LongitudinalWorkspaceFilters:
    if filters.result_view_filters is not None:
        _invalid()
    return LongitudinalWorkspaceFilters(
        timepoint_ordinals=filters.timepoint_ordinals,
        lineage_roles=filters.lineage_roles,
        record_availability=filters.record_availability,
        compatibility_states=tuple(
            RowCompatibilityState(item.value) for item in filters.compatibility_outcomes
        ),
    )


def _family_request(
    source: LongitudinalExplorerSource, workspace: LongitudinalWorkspace
) -> SavedFamilyProjectionRequestV1:
    authority = workspace.authority
    resolved = _store_read(
        lambda: _call(
            source.store("projection_policy_registry"),
            ProjectionPolicyRegistry,
            "resolve",
            authority.projection_selector_id,
            authority.projection_policy_version,
        )
    )
    head = workspace.dependency_heads.projection_policy
    if resolved.policy_sha256 != authority.projection_policy_sha256 or (
        resolved.registry_id,
        resolved.registry_epoch_sha256,
        resolved.state_head_sha256,
    ) != (head.id, head.epoch, head.head):
        _fail(LongitudinalErrorCode.READ_CONFLICT, LongitudinalRemediation.RETRY_READ)
    policy = resolved.policy
    rule = policy.selection_rule
    if rule is ProjectionSelectionRule.FINITE_COMPONENTS:
        present = {item.statistic for item in policy.components}
        count = len(policy.components)
    else:
        present = set(policy.all_component_statistics)
        count = 0
    statistics = tuple(item for item in type(next(iter(present))) if item in present)
    return SavedFamilyProjectionRequestV1(
        family=policy.family,
        selection_rule=rule,
        statistics=statistics,
        statistic_units=tuple(_PROJECTION_STATISTIC_UNIT[item] for item in statistics),
        projection_policy_selector_id=authority.projection_selector_id,
        projection_policy_version=authority.projection_policy_version,
        projection_policy_sha256=authority.projection_policy_sha256,
        component_count=count,
    )


def _selection(workspace: LongitudinalWorkspace) -> SavedComparisonSelectionV1:
    request = workspace.request
    return SavedComparisonSelectionV1(
        cohort_selector_id=request.cohort_selector_id,
        cohort_version=request.cohort_version,
        anchor_policy_selector_id=request.anchor_policy_selector_id,
        anchor_policy_version=request.anchor_policy_version,
        approved_anchor_selector_id=request.anchor_selector_id,
        # The approved-anchor version is its approval version; the live page
        # digest is bound through the workspace replay digest.
        approved_anchor_version=request.anchor_policy_version,
        projection_policy_selector_id=request.projection_policy_selector_id,
        projection_policy_version=request.projection_policy_version,
        d09_policy_selector_id=request.d09_policy_selector_id,
        d09_policy_version=request.d09_policy_version,
        measurement=SavedComparisonMeasurementV1(
            measurement_definition_sha256=request.measurement.measurement_definition_sha256,
            scope=_scope_of(request.measurement),
        ),
        filters=_saved_filters(request.filters),
    )


def build_saved_comparison(
    source: LongitudinalExplorerSource,
    workspace: LongitudinalWorkspace,
    *,
    comparison_version: int,
) -> SavedLongitudinalComparisonV1:
    """``SavedLongitudinalComparisonV1`` from one built workspace."""

    heads = workspace.dependency_heads
    e06 = heads.e06_source
    sources = sorted(
        {
            (row.source.selector_id, row.source.source_version)
            for row in workspace.protected_rows
            if row.source is not None
        }
    )
    return build_saved_longitudinal_comparison(
        selection=_selection(workspace),
        comparison_version=comparison_version,
        family_projection_request=_family_request(source, workspace),
        commitments=saved_commitments(workspace),
        e06_registry_id=e06.id,
        e06_registry_epoch_sha256=e06.epoch,
        e06_state_head_sha256=e06.head,
        e06_sources=tuple(
            SavedE06SourceRefV1(selector_id=selector, source_version=version)
            for selector, version in sources
        ),
        dependency_heads=heads,
        workspace_replay_sha256=workspace.replay_sha256,
        created_at=source.now(),
    )


def _composite_fence(source: LongitudinalExplorerSource) -> CompositeAuthorityFence:
    return CompositeAuthorityFence(source.coordinator)


def _saved_versions(
    source: LongitudinalExplorerSource,
    registry: LongitudinalComparisonRegistry,
    selector_id: str,
) -> list[int]:
    """Every committed version of one saved selector, via bounded pages."""

    number = int(selector_id[len("saved_comparison_") :], 16)
    cursor: dict[str, object] = (
        {}
        if number == 0
        else {
            "after_selector_id": f"saved_comparison_{number - 1:040x}",
            "after_version": MAX_SAVED_COMPARISONS,
        }
    )
    versions: list[int] = []
    for _ in range(MAX_SCAN_PAGES + 1):
        try:
            page = _call(
                registry,
                LongitudinalComparisonRegistry,
                "list_selectors",
                dependency_fence=_composite_fence(source),
                limit=100,
                **cursor,
            )
        except LongitudinalComparisonRegistryError as exc:
            _registry_failure(exc)
        matching = [
            item.comparison_version
            for item in page.records
            if item.selector_id == selector_id
        ]
        versions.extend(matching)
        if page.next_after_selector_id is None or len(matching) != len(page.records):
            return versions
        cursor = {
            "after_selector_id": page.next_after_selector_id,
            "after_version": page.next_after_version,
        }
    return versions


def _resolve_saved(
    source: LongitudinalExplorerSource,
    registry: LongitudinalComparisonRegistry,
    selector_id: str,
    version: int,
) -> RegisteredSavedComparisonV1:
    try:
        return _call(
            registry,
            LongitudinalComparisonRegistry,
            "resolve",
            selector_id,
            version,
            dependency_fence=_composite_fence(source),
        )
    except LongitudinalComparisonRegistryError as exc:
        _registry_failure(exc)


def save(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    body: object,
) -> dict[str, object]:
    values = _workspace_body(body, {"request"})
    workspace_request = _parse_workspace_request(values["request"])
    scope = _scope_of(workspace_request.measurement)
    with view.gate(scope):
        availability = save_availability(source)
    if availability.state is not SaveState.AVAILABLE:
        raise _RouteFailure(409, "save_unavailable", availability.state.value)
    registry = source.comparison_registry
    assert registry is not None
    built, first = _build(view, source, workspace_request)
    try:
        _, epoch, _, _ = _call(registry, LongitudinalComparisonRegistry, "identity")
    except LongitudinalComparisonRegistryError as exc:
        _registry_failure(exc)
    selection = _selection(built)
    selector_id = _PINNED_SELECTOR_ID(epoch, selection)
    versions = _saved_versions(source, registry, selector_id)
    saved: SavedLongitudinalComparisonV1 | None = None
    if versions:
        latest = _resolve_saved(source, registry, selector_id, max(versions))
        previous = latest.saved
        if (
            previous.workspace_replay_sha256 == built.replay_sha256
            and previous.dependency_heads == built.dependency_heads
            and previous.selection == selection
        ):
            # Exact retry of an unchanged comparison: republish the exact
            # committed bytes so the registry returns its idempotent receipt.
            saved = previous
    if saved is None:
        version = max(versions, default=0) + 1
        if version > MAX_SAVED_COMPARISONS:
            raise _RouteFailure(409, "save_unavailable", SaveState.REGISTRY_FULL.value)
        try:
            saved = build_saved_comparison(source, built, comparison_version=version)
        except (ValidationError, ValueError, TypeError):
            _invalid()
    try:
        receipt = _call(
            registry,
            LongitudinalComparisonRegistry,
            "register",
            saved,
            dependency_fence=_composite_fence(source),
        )
    except LongitudinalComparisonRegistryError as exc:
        _registry_failure(exc)
    if (
        receipt.dependency_heads != built.dependency_heads
        or receipt.dependency_fence_kind
        is not DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
        or receipt.selector_id != selector_id
        or receipt.comparison_version != saved.comparison_version
    ):
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    # Final fence: under one composite hold, the published head vector must
    # still be current and the reader must still be authorized.  Only then is
    # a receipt returned.
    dependency_scope = SavedComparisonDependencyScopeV1(
        cohort_selector_id=selection.cohort_selector_id,
        cohort_version=selection.cohort_version,
    )
    response: PublicSaveReceipt | None = None
    failure: BaseException | None = None
    try:
        with _composite_fence(source).hold() as held:
            if held.read_heads(dependency_scope) != receipt.dependency_heads:
                raise LongitudinalComparisonRegistryStale("published authority moved")
            final = view.held_authorization(scope)
            _same_grant(first, final)
            response = PublicSaveReceipt(
                saved_selector_id=receipt.selector_id,
                comparison_version=receipt.comparison_version,
                object_sha256=receipt.object_sha256,
                registry_state_version=receipt.state_version,
                workspace_replay_sha256=saved.workspace_replay_sha256,
                applied=receipt.applied,
                dependency_fence_kind=receipt.dependency_fence_kind,
            )
    except (_Denied, ReaderAuthorizationDenied):
        raise _Denied from None
    except LongitudinalComparisonRegistryError as exc:
        failure = exc
    if failure is not None:
        _registry_failure(failure)
    assert response is not None
    return _public(response)


# --- saved list and reopen ---------------------------------------------------------------


def saved_page(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    params: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    # No authorized scope at all: the denial shell (at view construction).
    # Some but not every configured scope: an empty page, no saved read.
    if view.granted != view.configured:
        # Re-check every granted scope under the live gate before answering,
        # so a grant revoked during the request still gets the denial shell.
        for scope in view.granted:
            with view.gate(scope):
                pass
        return _public(
            LongitudinalSavedPage(
                save=save_availability(source),
                listing_state="requires_every_configured_scope",
                records=(),
            )
        )
    view.saved_gate()
    if set(params) - {"after_selector_id", "after_version"}:
        _invalid()
    after_id = _single(params, "after_selector_id")
    after_version = _int_param(params, "after_version")
    if (after_id is None) != (after_version is None) or (
        after_id is not None and not _SAVED_SELECTOR.fullmatch(after_id)
    ):
        _invalid()
    availability = save_availability(source)
    registry = source.comparison_registry
    if registry is None or availability.state is SaveState.REGISTRY_UNHEALTHY:
        return _public(LongitudinalSavedPage(save=availability, records=()))
    cursor: dict[str, object] = {}
    if after_id is not None:
        cursor = {"after_selector_id": after_id, "after_version": after_version}
    try:
        page = _call(
            registry,
            LongitudinalComparisonRegistry,
            "list_selectors",
            dependency_fence=_composite_fence(source),
            limit=SAVED_PAGE_LIMIT,
            **cursor,
        )
    except LongitudinalComparisonRegistryError as exc:
        _registry_failure(exc)
    payload = _public(
        LongitudinalSavedPage(
            save=availability,
            records=tuple(
                PublicSavedComparisonRow(
                    saved_selector_id=item.selector_id,
                    comparison_version=item.comparison_version,
                    object_sha256=item.object_sha256,
                    authority_state=item.authority_state,
                    stale_dependencies=item.stale_dependencies,
                )
                for item in page.records
            ),
            next_after_selector_id=page.next_after_selector_id,
            next_after_version=page.next_after_version,
        )
    )
    view.saved_gate()
    return payload


def _reopen_request(
    saved: SavedLongitudinalComparisonV1, candidate_page_sha256: str
) -> LongitudinalWorkspaceRequest:
    selection = saved.selection
    scope = selection.measurement.scope
    return LongitudinalWorkspaceRequest(
        cohort_selector_id=selection.cohort_selector_id,
        cohort_version=selection.cohort_version,
        anchor_policy_selector_id=selection.anchor_policy_selector_id,
        anchor_policy_version=selection.anchor_policy_version,
        anchor_selector_id=selection.approved_anchor_selector_id,
        anchor_candidate_page_sha256=candidate_page_sha256,
        projection_policy_selector_id=selection.projection_policy_selector_id,
        projection_policy_version=selection.projection_policy_version,
        d09_policy_selector_id=selection.d09_policy_selector_id,
        d09_policy_version=selection.d09_policy_version,
        measurement=LongitudinalMeasurementSelection(
            family=scope.family,
            quantity_id=scope.quantity_id,
            unit=scope.unit,
            measurement_definition_sha256=selection.measurement.measurement_definition_sha256,
        ),
        filters=_workspace_filters(selection.filters),
    )


def _public_selection(saved: SavedLongitudinalComparisonV1) -> PublicSavedSelection:
    selection = saved.selection
    scope = selection.measurement.scope
    return PublicSavedSelection(
        cohort_selector_id=selection.cohort_selector_id,
        cohort_version=selection.cohort_version,
        anchor_policy_selector_id=selection.anchor_policy_selector_id,
        anchor_policy_version=selection.anchor_policy_version,
        approved_anchor_selector_id=selection.approved_anchor_selector_id,
        projection_policy_selector_id=selection.projection_policy_selector_id,
        projection_policy_version=selection.projection_policy_version,
        d09_policy_selector_id=selection.d09_policy_selector_id,
        d09_policy_version=selection.d09_policy_version,
        measurement=LongitudinalMeasurementSelection(
            family=scope.family,
            quantity_id=scope.quantity_id,
            unit=scope.unit,
            measurement_definition_sha256=selection.measurement.measurement_definition_sha256,
        ),
    )


def _historical_commitments(
    saved: SavedLongitudinalComparisonV1,
) -> PublicSavedCommitments:
    values = saved.commitments.model_dump(
        mode="python", exclude={"reader_grant_sha256"}
    )
    return PublicSavedCommitments(
        **values,
        projection_policy_sha256=saved.family_projection_request.projection_policy_sha256,
    )


def reopen(
    view: _AuthorizedView,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    body: object,
) -> dict[str, object]:
    view.saved_gate()
    if (
        not isinstance(body, dict)
        or set(body) != {"saved_selector_id", "comparison_version", "stage"}
        or type(body["saved_selector_id"]) is not str
        or not _SAVED_SELECTOR.fullmatch(body["saved_selector_id"])
        or type(body["comparison_version"]) is not int
        or not 1 <= body["comparison_version"] <= MAX_SAVED_COMPARISONS
        or body["stage"] not in ("diff", "results")
    ):
        _invalid()
    stage: Literal["diff", "results"] = body["stage"]
    registry = source.comparison_registry
    if registry is None:
        raise _RouteFailure(409, "save_unavailable", SaveState.REGISTRY_ABSENT.value)
    failure: BaseException | None = None
    try:
        registered = _call(
            registry,
            LongitudinalComparisonRegistry,
            "resolve",
            body["saved_selector_id"],
            body["comparison_version"],
            dependency_fence=_composite_fence(source),
        )
    except LongitudinalComparisonRegistryConflict as exc:
        # An unknown selector answers like a denial: no existence oracle.
        if not isinstance(exc, LongitudinalComparisonRegistryStale):
            raise _Denied from None
        failure = exc
    except LongitudinalComparisonRegistryError as exc:
        failure = exc
    if failure is not None:
        _registry_failure(failure)
    saved = registered.saved
    scope = saved.selection.measurement.scope
    # The saved object's own scope must be configured and granted before
    # anything of it is presented or replayed.
    with view.gate(scope) as first:
        selection = saved.selection
        page_sha256: str | None = None
        try:
            page = _call(
                source.store("anchor_policy_registry"),
                AnchorPolicyRegistry,
                "derive_candidate_page",
                selection.anchor_policy_selector_id,
                selection.anchor_policy_version,
            )
            if page.cohort_manifest_sha256 == saved.commitments.cohort_manifest_sha256:
                page_sha256 = page.candidate_page_sha256
        except Exception:  # noqa: BLE001 - a stale page is a stale reopen
            page_sha256 = None
        cohort_diff: LongitudinalVersionDiff | None
        try:
            cohort_diff = _version_diff(
                source, first, selection.cohort_selector_id, selection.cohort_version
            )
        except _RouteFailure:
            cohort_diff = None
        versions = [
            item.cohort_version
            for item in _selector_versions(
                source.store("cohort_registry"), first, selection.cohort_selector_id
            )
        ]
    rebuilt: LongitudinalWorkspace | None = None
    rebuild_error: LongitudinalErrorCode | None = None
    if page_sha256 is None:
        rebuild_error = LongitudinalErrorCode.AUTHORITY_STALE
    else:
        try:
            rebuilt, _ = _build(view, source, _reopen_request(saved, page_sha256))
        except _RouteFailure as exc:
            rebuild_error = LongitudinalErrorCode(exc.code)
    changes: list[CommitmentChange] = []
    if (
        registered.publication_fence_kind
        is not DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
    ):
        changes.append(CommitmentChange.PUBLICATION_NOT_COMPOSITE)
    if registered.stale_dependencies:
        changes.append(CommitmentChange.DEPENDENCY_HEADS)
    if rebuilt is not None:
        live = saved_commitments(rebuilt)
        changes.extend(
            change
            for name, change in _COMMITMENT_FIELDS
            if getattr(live, name) != getattr(saved.commitments, name)
        )
        if (
            rebuilt.authority.projection_policy_sha256
            != saved.family_projection_request.projection_policy_sha256
        ):
            changes.append(CommitmentChange.PROJECTION_POLICY)
        if rebuilt.dependency_heads != saved.dependency_heads and (
            CommitmentChange.DEPENDENCY_HEADS not in changes
        ):
            changes.append(CommitmentChange.DEPENDENCY_HEADS)
        if rebuilt.replay_sha256 != saved.workspace_replay_sha256:
            changes.append(CommitmentChange.REPLAY)
    current = (
        rebuilt is not None
        and not changes
        and registered.authority_state is SavedComparisonAuthorityState.CURRENT
        and rebuilt.replay_sha256 == saved.workspace_replay_sha256
        and rebuilt.dependency_heads == saved.dependency_heads
    )
    state = (
        SavedComparisonAuthorityState.CURRENT
        if current
        else SavedComparisonAuthorityState.STALE
    )
    latest = max(versions) if versions else None
    diff = LongitudinalReopenDiff(
        saved_selector_id=registered.selector_id,
        comparison_version=registered.comparison_version,
        object_sha256=registered.object_sha256,
        saved_selection=_public_selection(saved),
        saved_workspace_replay_sha256=saved.workspace_replay_sha256,
        rebuilt_workspace_replay_sha256=(
            rebuilt.replay_sha256 if rebuilt is not None else None
        ),
        registry_authority_state=registered.authority_state,
        stale_dependencies=registered.stale_dependencies,
        cohort_version_diff=cohort_diff,
        latest_cohort_version=latest,
        newer_cohort_version_available=(
            latest is not None and latest > selection.cohort_version
        ),
        changes=tuple(dict.fromkeys(changes)),
        rebuild_error=rebuild_error,
        comparison_state=state,
    )
    if stage == "diff":
        response = LongitudinalReopenResponse(
            stage="diff", diff=diff, save=save_availability(source)
        )
    elif current:
        assert rebuilt is not None
        response = LongitudinalReopenResponse(
            stage="results",
            diff=diff,
            save=save_availability(source),
            current_workspace=_PINNED_PROJECT(rebuilt),
        )
    else:
        response = LongitudinalReopenResponse(
            stage="results",
            diff=diff,
            save=save_availability(source),
            historical_commitments=_historical_commitments(saved),
            refresh_action="start_new_comparison_at_current_authority",
        )
    payload = _public(response)
    if not current:
        with view.gate(scope) as final:
            _same_grant(first, final)
        return payload
    # A current reopen is returned only if, under one final composite hold,
    # every dependency head still equals the saved (and rebuilt) vector and
    # the reader is still authorized; otherwise nothing is presented.
    assert rebuilt is not None
    dependency_scope = SavedComparisonDependencyScopeV1(
        cohort_selector_id=selection.cohort_selector_id,
        cohort_version=selection.cohort_version,
    )
    failure = None
    try:
        with _composite_fence(source).hold() as held:
            if held.read_heads(dependency_scope) != saved.dependency_heads:
                raise LongitudinalComparisonRegistryStale("authority moved")
            final = view.held_authorization(scope)
            _same_grant(first, final)
    except (_Denied, ReaderAuthorizationDenied):
        raise _Denied from None
    except LongitudinalComparisonRegistryError as exc:
        failure = exc
    if failure is not None:
        _registry_failure(failure)
    return payload


# --- dispatch -----------------------------------------------------------------------

_GET_ROUTES: dict[str, Callable[..., dict[str, object]]] = {
    "selectors": selector_catalog,
    "diff": version_diff,
    "saved": saved_page,
}
_POST_ROUTES: dict[str, Callable[..., dict[str, object]]] = {
    "workspace": workspace,
    "source": source_detail,
    "save": save,
    "reopen": reopen,
}
GET_ROUTE_PATHS = frozenset(ROUTE_PREFIX + name for name in _GET_ROUTES)
POST_ROUTE_PATHS = frozenset(ROUTE_PREFIX + name for name in _POST_ROUTES)


def handle_longitudinal_route(
    method: str,
    path: str,
    *,
    binder: ReaderSessionBinder | None,
    request: BrowserRequest,
    source: LongitudinalExplorerSource,
    params: Mapping[str, Sequence[str]] | None = None,
    body: object = None,
) -> tuple[int, dict[str, object]]:
    """Return (status, public payload).  B01 ``BoundaryDenied`` propagates.

    Every other failure is one closed code: the bounded permission-denied
    shell, or ``{"error": {"code", "remediation"}}`` with no detail.
    """

    routes = _GET_ROUTES if method == "GET" else _POST_ROUTES
    handler = (
        routes.get(path.removeprefix(ROUTE_PREFIX))
        if path.startswith(ROUTE_PREFIX)
        else None
    )
    if handler is None:
        return 404, {"error": {"code": "TBX-WEB-404"}}
    try:
        # Reader authorization first, for every route and before any input
        # parsing: a session without a grant over a configured scope gets the
        # same bounded shell whatever it sent.
        view = _AuthorizedView(binder, request, source)
        if method == "GET":
            return 200, handler(view, request, source, params or {})
        return 200, handler(view, request, source, body)
    except BoundaryDenied:
        raise
    except (_Denied, ReaderAuthorizationDenied):
        return 403, dict(_PERMISSION_DENIED)
    except _RouteFailure as exc:
        error: dict[str, object] = {"code": exc.code}
        if exc.remediation is not None:
            error["remediation"] = exc.remediation
        return exc.status, {"error": error}
    except Exception:  # noqa: BLE001 - never a partial payload or detail
        return 500, {
            "error": {
                "code": LongitudinalErrorCode.INTEGRITY_FAILURE.value,
                "remediation": LongitudinalRemediation.VERIFY_STORE_INTEGRITY.value,
            }
        }


__all__ = [
    "GET_ROUTE_PATHS",
    "POST_ROUTE_PATHS",
    "ROUTE_PREFIX",
    "LongitudinalExplorerSource",
    "SaveState",
    "build_saved_comparison",
    "handle_longitudinal_route",
    "save_availability",
    "saved_commitments",
    "validate_longitudinal_public",
]
