"""D08 protected longitudinal read model and its bounded public projection.

``build_longitudinal_workspace`` is the E12 builder named in
``docs/E12-INTEGRATION-PLAN.md`` ("Required implementation cut").  It takes an
exact :class:`LongitudinalWorkspaceRequest` (opaque selectors, one requested D02
measurement and normalized filters), the protected reader-session credential
and the merged stores.  Every authority comes from those stores; the caller
never supplies a manifest, status, source, decision, comparison, policy,
anchor, coordinate or value.

Authority protocol (bracketed composite snapshot)
-------------------------------------------------

The composite coordinator (``composite_authority_fence``) holds every store
fence in the global lock order but runs no caller code inside the hold, and
the stores' public reads cannot run inside it.  The builder therefore:

1. captures the request and credential as exact bounded canonical bytes;
2. authorizes the reader under the reader registry's own fence (``A1``) and,
   before any other protected read, checks the bounded D05 selector page
   (1,001 members is rejected here);
3. takes composite snapshot ``H1`` (every store head plus the scoped D06
   status head, captured and revalidated under the full fence set);
4. performs only the stores' public live-replaying reads, and requires every
   head each read reports to equal the corresponding ``H1`` head;
5. takes composite snapshot ``H2`` and re-authorizes the reader (``A2``);
6. requires ``H2 == H1`` and ``A2 == A1`` (up to ``evaluated_at``), then derives
   the workspace from the captured reads only.

Chained heads (append-only journals with rollback detection) are covered by
``H2 == H1``: no committed mutation of those stores landed between the holds.
Two heads are content digests, not chains: the D06 scoped head (the status
digest of the selected cohort version, derived from E04 rows that can be
removed and re-added) and the E04 head, which since #81 also covers a digest
of every committed catalog row.  A remove-and-re-add between the holds can
restore an equal digest, so every D06-derived read (D06 status, E06 sources,
family artifacts, D09 summary, D10 context) is additionally bound to the
``H1`` status digest; a read that observed the intermediate state fails that
binding (``read_conflict``) or fails its own replay (``authority_stale``,
which a retry resolves).  The returned workspace is a point-in-time record of
``H1``, never a lease.  D08 consumes E04 rows only through D06 status
bindings.  Time-dependent replays (reader grant expiry, D07 envelope validity)
are evaluated live at each read; D07 ``replayed_at`` is the as-of time of a
comparison.

No family adapter, parser or derivation runs while any store fence is held:
the adapters run between the bracketing snapshots.

Threat model: the process/OS-user boundary is the trust boundary.  In-process
code mutation and same-user filesystem races are out of scope.  Pins and type
checks detect accidental or naive class and instance replacement only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, NoReturn, TypeVar

from pydantic import Field, StringConstraints, ValidationError, model_validator

import evidence_inspector.anchor_policy_registry as anchor_module
import evidence_inspector.cohort_import as d06_module
import evidence_inspector.cohort_registry as d05_module
import evidence_inspector.covariate_context_registry as d10_module
import evidence_inspector.denominator_policy_registry as d09_module
import evidence_inspector.longitudinal_decision_registry as d03_module
import evidence_inspector.measurement_source_artifact_registry as family_module
import evidence_inspector.projection_policy_registry as projection_module
import evidence_inspector.provider_linkage_store as d01_module
import evidence_inspector.reader_authorization_registry as reader_module
import evidence_inspector.record_supersession_store as d04_module
import evidence_inspector.repeatability_comparison_registry as d07_module
import evidence_inspector.result_catalog as e04_module
import evidence_inspector.result_trust_registry as trust_module
import evidence_inspector.result_view_source_registry as e06_module
from evidence_inspector.anchor_policy_registry import (
    AnchorPolicyRegistry,
    ResolvedApprovedAnchor,
)
from evidence_inspector.cohort_import import (
    CohortManifestRecordStatus,
    CohortMemberRecordStatus,
    CohortRecordAvailability,
    CohortRecordCatalog,
    CohortRecordWithheldReason,
)
from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MemberLineageRole,
    TimeAxisKind,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import (
    CohortRegistry,
    CohortSelectorRecord,
    RegisteredCohortHistory,
)
from evidence_inspector.cohort_summary import CohortPopulationProjection
from evidence_inspector.compatibility import ExecutionState, InformationState
from evidence_inspector.composite_authority_fence import (
    CompositeAuthorityCoordinator,
    CompositeAuthorityRetry,
    CompositeAuthoritySnapshotV1,
    CompositeAuthorityStale,
    CompositeAuthorityUnsafe,
)
from evidence_inspector.covariate_context import (
    AggregateCovariateGroup,
    CovariateClassification,
    CovariateReason,
    project_aggregate_covariate_summary,
)
from evidence_inspector.covariate_context_registry import (
    ContextAuthorityState,
    CovariateContextRegistry,
    RegisteredLiveCovariateContext,
)
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistry,
    RegisteredDenominatorPolicySummary,
)
from evidence_inspector.fragment_explorer import FragmentQuantity, PanelId
from evidence_inspector.longitudinal_comparison_registry import (
    DependencyHeadV1,
    DependencySlot,
    SavedComparisonDependencyHeadsV1,
    SavedComparisonDependencyScopeV1,
)
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalMemberDecision,
    LongitudinalNextAction,
    LongitudinalOutcome,
    LongitudinalReason,
    longitudinal_member_decision_sha256,
    measurement_definition_sha256,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    RegisteredLongitudinalSeriesDecision,
    SeriesAuthorityState,
)
from evidence_inspector.measurement_source_artifact_registry import (
    ArtifactAuthorityState,
    MeasurementSourceArtifactRegistry,
    RegisteredFragmentSourceArtifact,
)
from evidence_inspector.method_registry import (
    MethodFamily,
    MethodReference,
    QuantityId,
    RegistryContract,
    Sha256,
    UnitId,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.projection_policy_registry import (
    FragmentProjectionPolicyV1,
    FragmentStatistic,
    ProjectionFamily,
    ProjectionPolicyRegistry,
    ResolvedProjectionPolicy,
    StatisticUnit,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
    ReaderAuthorization,
    ReaderAuthorizationDenied,
    ReaderAuthorizationRegistry,
    ReaderGrantBinding,
)
from evidence_inspector.record_supersession_store import (
    MAX_HISTORY_PAGE_RECORDS,
    RecordHistoryEntry,
    RecordHistorySnapshot,
    RecordSupersessionStore,
    record_sha256,
)
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    RepeatabilityClassification,
    RepeatabilityReason,
)
from evidence_inspector.repeatability_comparison_registry import (
    ComparisonAuthorityState,
    RegisteredRepeatabilityComparison,
    RepeatabilityComparisonRegistry,
)
from evidence_inspector.result_catalog import ResultCatalog
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from evidence_inspector.result_view_source_registry import (
    RegisteredResultViewSource,
    ResultViewSourceRegistry,
    SourceAuthorityState,
)
from evidence_inspector.safe_ingress import contract_type_graph, exact_model_bytes
from evidence_inspector.source_value_projection import (
    FragmentLongitudinalValueProjectionV1,
    SourceMeasurementIdentity,
    SourceValueCoordinateUnresolved,
    SourceValueFamilyMismatch,
    SourceValueMeasurementMismatch,
    SourceValuePolicyRejected,
    SourceValueProjectionError,
    SourceValueProjectionSetV1,
    SourceValueReplayRejected,
    SourceValueRepresentationDrift,
    SourceValueVectorTooLarge,
    SourceValueWithheld,
    project_source_values,
)
from traceback_runner.signing import SigningError

MAX_WORKSPACE_MEMBERS = 1_000
MAX_REQUEST_BYTES = 16 * 1024
MAX_SELECTOR_SCAN_PAGES = 200
MAX_HISTORY_PAGES = 100
LIMITATION_STATEMENT = (
    "Comparisons are descriptive technical differences with no causal or "
    "clinical interpretation."
)
OPERATOR_ENTERED_LABEL = "operator-entered, unverified"

_COMPARABLE_OUTCOMES = frozenset(
    {LongitudinalOutcome.EQUIVALENT, LongitudinalOutcome.QUALIFIED_COMPATIBLE}
)

CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
AnchorPolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_policy_[0-9a-f]{40}$")
]
AnchorCandidateSelectorId = Annotated[
    str, StringConstraints(pattern=r"^anchor_candidate_[0-9a-f]{40}$")
]
ProjectionSelectorId = Annotated[
    str, StringConstraints(pattern=r"^projection_policy_[0-9a-f]{40}$")
]
D09SelectorId = Annotated[str, StringConstraints(pattern=r"^d09_policy_[0-9a-f]{40}$")]
SourceAlias = Annotated[str, StringConstraints(pattern=r"^source_[0-9a-f]{16}$")]


# --- boundary errors ----------------------------------------------------------------


class LongitudinalErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    PERMISSION_DENIED = "permission_denied"
    AUTHORITY_STALE = "authority_stale"
    TRUST_REVOKED = "trust_revoked"
    INTEGRITY_FAILURE = "integrity_failure"
    STORAGE_FAILURE = "storage_failure"
    READ_CONFLICT = "read_conflict"


class LongitudinalRemediation(StrEnum):
    CORRECT_REQUEST = "correct_request"
    REDUCE_COHORT_TO_BOUND = "reduce_cohort_to_bound"
    OBTAIN_CURRENT_READER_GRANT = "obtain_current_reader_grant"
    RESELECT_COHORT_VERSION = "reselect_cohort_version"
    RESELECT_ANCHOR = "reselect_anchor"
    RESELECT_POLICY = "reselect_policy"
    REFRESH_SOURCE_REGISTRATION = "refresh_source_registration"
    REFRESH_DECISION_REGISTRATION = "refresh_decision_registration"
    RETRY_READ = "retry_read"
    VERIFY_STORE_INTEGRITY = "verify_store_integrity"
    CHECK_LOCAL_STORAGE = "check_local_storage"
    REVIEW_TRUST_AUTHORITY = "review_trust_authority"


class LongitudinalWorkspaceBoundaryError(Exception):
    """Closed safe failure: one code and one remediation, nothing else.

    It is always raised outside any ``except`` block, so it carries no
    ``__cause__`` or ``__context__`` and no nested exception text.
    """

    def __init__(
        self, code: LongitudinalErrorCode, remediation: LongitudinalRemediation
    ) -> None:
        if (
            type(code) is not LongitudinalErrorCode
            or type(remediation) is not LongitudinalRemediation
        ):
            code = LongitudinalErrorCode.INTEGRITY_FAILURE
            remediation = LongitudinalRemediation.VERIFY_STORE_INTEGRITY
        super().__init__(f"{code.value}:{remediation.value}")
        self.code = code
        self.remediation = remediation


def _fail(
    code: LongitudinalErrorCode, remediation: LongitudinalRemediation
) -> NoReturn:
    raise LongitudinalWorkspaceBoundaryError(code, remediation)


_UNSAFE_ERRORS: tuple[type[BaseException], ...] = (
    CompositeAuthorityUnsafe,
    d01_module.ProviderLinkageStoreUnsafe,
    d01_module.ProviderLinkageStoreSchemaError,
    d04_module.RecordSupersessionUnsafe,
    d05_module.CohortRegistryUnsafe,
    reader_module.ReaderAuthorizationRegistryUnsafe,
    e04_module.CatalogUnsupportedSchema,
    trust_module.ResultTrustRegistryUnsafe,
    e06_module.ResultViewSourceRegistryUnsafe,
    d03_module.LongitudinalDecisionRegistryUnsafe,
    d07_module.RepeatabilityComparisonRegistryUnsafe,
    d09_module.DenominatorPolicyRegistryUnsafe,
    d10_module.CovariateContextRegistryUnsafe,
    family_module.MeasurementSourceArtifactRegistryUnsafe,
    anchor_module.AnchorPolicyRegistryUnsafe,
    projection_module.ProjectionPolicyRegistryUnsafe,
)
_STORAGE_ERRORS: tuple[type[BaseException], ...] = (
    d06_module.CohortImportFilesystemError,
    e04_module.CatalogFilesystemError,
)
_STALE_ERRORS: tuple[type[BaseException], ...] = (
    CompositeAuthorityStale,
    e06_module.ResultViewSourceRegistryStale,
    d03_module.LongitudinalDecisionRegistryStale,
    d07_module.RepeatabilityComparisonRegistryStale,
    d09_module.DenominatorPolicyRegistryStale,
    d10_module.CovariateContextRegistryStale,
    family_module.MeasurementSourceArtifactRegistryStale,
    anchor_module.AnchorPolicyRegistryStale,
)
_CONFLICT_ERRORS: tuple[type[BaseException], ...] = (
    d01_module.ProviderLinkageStoreError,
    d04_module.RecordSupersessionError,
    d05_module.CohortRegistryError,
    d06_module.CohortImportError,
    e04_module.CatalogError,
    e06_module.ResultViewSourceRegistryError,
    d03_module.LongitudinalDecisionRegistryError,
    d07_module.RepeatabilityComparisonRegistryError,
    d09_module.DenominatorPolicyRegistryError,
    d10_module.CovariateContextRegistryError,
    family_module.MeasurementSourceArtifactRegistryError,
    anchor_module.AnchorPolicyRegistryError,
    projection_module.ProjectionPolicyRegistryError,
    reader_module.ReaderAuthorizationRegistryError,
)
_TRUST_ERRORS: tuple[type[BaseException], ...] = (
    trust_module.ResultTrustRegistryError,
    SigningError,
)

T = TypeVar("T")


def _guarded(
    operation: Callable[[], T],
    *,
    conflict: LongitudinalErrorCode = LongitudinalErrorCode.AUTHORITY_STALE,
    remediation: LongitudinalRemediation = LongitudinalRemediation.RETRY_READ,
) -> T:
    """Run one store read and map any failure to one closed boundary error.

    The mapping is decided inside the ``except`` clauses but the error is
    raised after the ``try`` statement, so it has no exception context.
    """

    failure: tuple[LongitudinalErrorCode, LongitudinalRemediation]
    try:
        return operation()
    except LongitudinalWorkspaceBoundaryError as exc:
        failure = (exc.code, exc.remediation)
    except ReaderAuthorizationDenied:
        failure = (
            LongitudinalErrorCode.PERMISSION_DENIED,
            LongitudinalRemediation.OBTAIN_CURRENT_READER_GRANT,
        )
    except CompositeAuthorityRetry:
        failure = (
            LongitudinalErrorCode.READ_CONFLICT,
            LongitudinalRemediation.RETRY_READ,
        )
    except _UNSAFE_ERRORS:
        failure = (
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    except _STORAGE_ERRORS:
        failure = (
            LongitudinalErrorCode.STORAGE_FAILURE,
            LongitudinalRemediation.CHECK_LOCAL_STORAGE,
        )
    except _TRUST_ERRORS:
        failure = (
            LongitudinalErrorCode.TRUST_REVOKED,
            LongitudinalRemediation.REVIEW_TRUST_AUTHORITY,
        )
    except _STALE_ERRORS:
        failure = (LongitudinalErrorCode.AUTHORITY_STALE, remediation)
    except _CONFLICT_ERRORS:
        failure = (conflict, remediation)
    except OSError:
        failure = (
            LongitudinalErrorCode.STORAGE_FAILURE,
            LongitudinalRemediation.CHECK_LOCAL_STORAGE,
        )
    except Exception:
        failure = (
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    _fail(*failure)


# --- request ------------------------------------------------------------------------


class RowCompatibilityState(StrEnum):
    """The six D03 outcomes plus the anchor row and members D03 did not decide."""

    ANCHOR = "anchor"
    EQUIVALENT = "equivalent"
    QUALIFIED_COMPATIBLE = "qualified_compatible"
    REQUIRES_REANALYSIS = "requires_reanalysis"
    REGISTERED_BRIDGE = "registered_bridge"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"
    NOT_EVALUATED = "not_evaluated"


class LongitudinalMeasurementSelection(RegistryContract):
    """The requested D02 measurement definition, quantity and unit."""

    schema_version: Literal["traceback.e12-measurement-selection.v1"] = (
        "traceback.e12-measurement-selection.v1"
    )
    family: MethodFamily
    quantity_id: QuantityId
    unit: UnitId
    measurement_definition_sha256: Sha256


class LongitudinalWorkspaceFilters(RegistryContract):
    """Controlled row filters; an empty tuple means no restriction on that axis.

    Filters select visible rows only after authority construction; they never
    change counts, eligibility, roles or values.
    """

    schema_version: Literal["traceback.e12-workspace-filters.v1"] = (
        "traceback.e12-workspace-filters.v1"
    )
    timepoint_ordinals: tuple[
        Annotated[int, Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)], ...
    ] = Field(default=(), max_length=MAX_WORKSPACE_MEMBERS)
    lineage_roles: tuple[MemberLineageRole, ...] = Field(
        default=(), max_length=len(MemberLineageRole)
    )
    record_availability: tuple[CohortRecordAvailability, ...] = Field(
        default=(), max_length=len(CohortRecordAvailability)
    )
    compatibility_states: tuple[RowCompatibilityState, ...] = Field(
        default=(), max_length=len(RowCompatibilityState)
    )

    @model_validator(mode="after")
    def unique_values(self) -> LongitudinalWorkspaceFilters:
        for values in (
            self.timepoint_ordinals,
            self.lineage_roles,
            self.record_availability,
            self.compatibility_states,
        ):
            if len(values) != len(set(values)):
                raise ValueError("filter values must be unique")
        return self


class LongitudinalWorkspaceRequest(RegistryContract):
    """Opaque selectors only: never a manifest, anchor, policy or value.

    ``anchor_candidate_page_sha256`` is the version of the approved-anchor
    selector: the digest of the live candidate page the operator chose from.
    The builder re-derives that page and fails closed if it changed.
    """

    schema_version: Literal["traceback.e12-longitudinal-workspace-request.v1"] = (
        "traceback.e12-longitudinal-workspace-request.v1"
    )
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    anchor_policy_selector_id: AnchorPolicySelectorId
    anchor_policy_version: int = Field(ge=1, le=100_000, strict=True)
    anchor_selector_id: AnchorCandidateSelectorId
    anchor_candidate_page_sha256: Sha256
    projection_policy_selector_id: ProjectionSelectorId
    projection_policy_version: int = Field(ge=1, le=100_000, strict=True)
    d09_policy_selector_id: D09SelectorId
    d09_policy_version: int = Field(ge=1, le=100_000, strict=True)
    measurement: LongitudinalMeasurementSelection
    filters: LongitudinalWorkspaceFilters = LongitudinalWorkspaceFilters()


_REQUEST_TYPES, _REQUEST_ENUMS = contract_type_graph(LongitudinalWorkspaceRequest)
_CREDENTIAL_TYPES, _CREDENTIAL_ENUMS = contract_type_graph(ReaderGrantBinding)


def normalize_filters(
    filters: LongitudinalWorkspaceFilters,
) -> LongitudinalWorkspaceFilters:
    """Canonical order for every filter axis, so caller order never matters."""

    return LongitudinalWorkspaceFilters(
        timepoint_ordinals=tuple(sorted(filters.timepoint_ordinals)),
        lineage_roles=tuple(
            item for item in MemberLineageRole if item in filters.lineage_roles
        ),
        record_availability=tuple(
            item
            for item in CohortRecordAvailability
            if item in filters.record_availability
        ),
        compatibility_states=tuple(
            item
            for item in RowCompatibilityState
            if item in filters.compatibility_states
        ),
    )


def _capture_request(request: object) -> LongitudinalWorkspaceRequest:
    """Exact bounded canonical bytes of the caller's request, re-parsed."""

    captured: LongitudinalWorkspaceRequest | None = None
    try:
        content = exact_model_bytes(
            request,
            LongitudinalWorkspaceRequest,
            model_types=_REQUEST_TYPES,
            enum_types=_REQUEST_ENUMS,
            max_bytes=MAX_REQUEST_BYTES,
            max_nodes=4_096,
            max_depth=8,
            max_collection_items=MAX_WORKSPACE_MEMBERS,
            max_string_bytes=256,
        )
        parsed = contract_from_canonical_bytes(LongitudinalWorkspaceRequest, content)
        captured = parsed.model_copy(
            update={"filters": normalize_filters(parsed.filters)}
        )
        captured = contract_from_canonical_bytes(
            LongitudinalWorkspaceRequest, canonical_contract_bytes(captured)
        )
    except Exception:
        captured = None
    if captured is None:
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.CORRECT_REQUEST,
        )
    return captured


def _capture_credential(credential: object) -> ReaderGrantBinding:
    """The server-side sealed session binding; anything else is a denial."""

    captured: ReaderGrantBinding | None = None
    try:
        content = exact_model_bytes(
            credential,
            ReaderGrantBinding,
            model_types=_CREDENTIAL_TYPES,
            enum_types=_CREDENTIAL_ENUMS,
            max_bytes=1_024,
            max_nodes=16,
            max_depth=3,
            max_collection_items=4,
            max_string_bytes=128,
        )
        captured = contract_from_canonical_bytes(ReaderGrantBinding, content)
    except Exception:
        captured = None
    if captured is None:
        _fail(
            LongitudinalErrorCode.PERMISSION_DENIED,
            LongitudinalRemediation.OBTAIN_CURRENT_READER_GRANT,
        )
    return captured


# --- protected rows -------------------------------------------------------------------


class HistoryState(StrEnum):
    """D04 role of this row's exact result record."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    AUTHORITY_INVALID = "authority_invalid"
    NOT_RECORDED = "not_recorded"
    BINDING_MISMATCH = "binding_mismatch"


class SourceState(StrEnum):
    VERIFIED = "verified"
    NOT_REGISTERED = "not_registered"
    RECORD_NOT_AVAILABLE = "record_not_available"
    MEASUREMENT_MISMATCH = "measurement_mismatch"


class ValueState(StrEnum):
    PROJECTED = "projected"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ARTIFACT_NOT_REGISTERED = "artifact_not_registered"
    FAMILY_PREREQUISITE_MISSING = "family_prerequisite_missing"
    PROJECTION_REJECTED = "projection_rejected"


class ValueRejection(StrEnum):
    POLICY_REJECTED = "policy_rejected"
    FAMILY_MISMATCH = "family_mismatch"
    MEASUREMENT_MISMATCH = "measurement_mismatch"
    COORDINATE_UNRESOLVED = "coordinate_unresolved"
    REPRESENTATION_DRIFT = "representation_drift"
    VALUE_WITHHELD = "value_withheld"
    REPLAY_REJECTED = "replay_rejected"
    VECTOR_TOO_LARGE = "vector_too_large"
    INVALID = "invalid"


class FamilyPrerequisite(StrEnum):
    """Named missing prerequisite for a family that has no applicable artifact."""

    E08_CELL_ORIGIN_ARTIFACT_BINDING = "e08_cell_origin_artifact_binding"
    E09_CNA_ARTIFACT_BINDING = "e09_cna_artifact_binding"


class DecisionState(StrEnum):
    DECIDED = "decided"
    ANCHOR = "anchor"
    NOT_IN_SERIES = "not_in_series"
    SERIES_NOT_REGISTERED = "series_not_registered"
    SERIES_AMBIGUOUS = "series_ambiguous"
    SERIES_STALE = "series_stale"


class ComparisonRegistryState(StrEnum):
    """What D07 returned for this row's exact D03 decision."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_REGISTERED = "not_registered"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"


class ComparisonState(StrEnum):
    """Public comparison state after every gate; only ``available`` has numbers."""

    AVAILABLE = "available"
    SUPPRESSED = "suppressed"
    ANCHOR_REFERENCE = "anchor_reference"


class SuppressionReason(StrEnum):
    RECORD_NOT_AVAILABLE = "record_not_available"
    ANCHOR_RECORD_NOT_AVAILABLE = "anchor_record_not_available"
    HISTORY_NOT_ACTIVE = "history_not_active"
    ANCHOR_HISTORY_NOT_ACTIVE = "anchor_history_not_active"
    D03_NOT_DECIDED = "d03_not_decided"
    D03_NOT_COMPARABLE = "d03_not_comparable"
    D03_DELTA_NOT_ALLOWED = "d03_delta_not_allowed"
    LINKAGE_BINDING_MISMATCH = "linkage_binding_mismatch"
    RESULT_BINDING_MISMATCH = "result_binding_mismatch"
    D07_NOT_REGISTERED = "d07_not_registered"
    D07_AMBIGUOUS = "d07_ambiguous"
    D07_STALE = "d07_stale"
    D07_UNAVAILABLE = "d07_unavailable"
    D07_BINDING_MISMATCH = "d07_binding_mismatch"


class ProtectedHistory(RegistryContract):
    state: HistoryState
    record_sha256: Sha256 | None = None
    successor_present: bool = False
    affected_comparison_count: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def coherent(self) -> ProtectedHistory:
        recorded = self.state in {
            HistoryState.ACTIVE,
            HistoryState.SUPERSEDED,
            HistoryState.AUTHORITY_INVALID,
        }
        if recorded != (self.record_sha256 is not None):
            raise ValueError("history record digest does not match its state")
        if not recorded and (self.successor_present or self.affected_comparison_count):
            raise ValueError("unrecorded history carries no warnings")
        return self


class ProtectedSource(RegistryContract):
    """E06 commitments of one verified source; E06 ledgers stay operator-entered."""

    selector_id: Annotated[str, StringConstraints(pattern=r"^e06_source_[0-9a-f]{40}$")]
    source_version: int = Field(ge=1, le=16, strict=True)
    object_sha256: Sha256
    source_sha256: Sha256
    source_replay_sha256: Sha256
    denominator_ledger_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    execution_state: ExecutionState
    information_state: InformationState


class ProtectedValues(RegistryContract):
    state: ValueState
    rejection: ValueRejection | None = None
    prerequisite: FamilyPrerequisite | None = None
    artifact_selector_id: str | None = Field(default=None, max_length=64)
    artifact_object_sha256: Sha256 | None = None
    artifact_replay_sha256: Sha256 | None = None
    projection: SourceValueProjectionSetV1 | None = None

    @model_validator(mode="after")
    def coherent(self) -> ProtectedValues:
        if (self.state is ValueState.PROJECTED) != (self.projection is not None):
            raise ValueError("values are present only when projected")
        if (self.state is ValueState.PROJECTION_REJECTED) != (
            self.rejection is not None
        ):
            raise ValueError("a rejection reason is present only when rejected")
        if (self.state is ValueState.FAMILY_PREREQUISITE_MISSING) != (
            self.prerequisite is not None
        ):
            raise ValueError("a prerequisite is named only when missing")
        return self


class ProtectedDecision(RegistryContract):
    """The exact D03 member decision fields D08 consumes."""

    decision_sha256: Sha256
    member_result_id: str = Field(min_length=1, max_length=128)
    anchor_record_sha256: Sha256
    member_record_sha256: Sha256
    anchor_linkage_receipt_sha256: Sha256 | None
    member_linkage_receipt_sha256: Sha256 | None
    policy_sha256: Sha256
    outcome: LongitudinalOutcome
    reason_codes: tuple[LongitudinalReason, ...] = Field(min_length=1, max_length=32)
    next_action: LongitudinalNextAction
    mismatch_dimensions: tuple[ComparisonDimension, ...] = Field(max_length=16)
    unknown_dimensions: tuple[ComparisonDimension, ...] = Field(max_length=16)
    bridge_reference_count: int = Field(ge=0, le=32, strict=True)
    delta_allowed: bool


class ProtectedComparison(RegistryContract):
    """The exact replayed D07 comparison fields D08 may copy."""

    selector_id: str = Field(min_length=1, max_length=64)
    comparison_sha256: Sha256
    d03_decision_sha256: Sha256
    anchor_policy_sha256: Sha256
    anchor_record_sha256: Sha256
    member_record_sha256: Sha256
    repeatability_envelope_sha256: Sha256 | None
    availability: ComparisonAvailability
    classification: RepeatabilityClassification
    reason_codes: tuple[RepeatabilityReason, ...] = Field(min_length=1, max_length=4)
    anchor_value: float | None
    member_value: float | None
    delta: float | None
    anchor_uncertainty_lower: float | None
    anchor_uncertainty_upper: float | None
    member_uncertainty_lower: float | None
    member_uncertainty_upper: float | None
    anchor_denominator_count: int | None
    member_denominator_count: int | None
    maximum_absolute_delta: float | None
    replayed_at: datetime

    @model_validator(mode="after")
    def numbers_only_when_available(self) -> ProtectedComparison:
        numeric = (
            self.anchor_value,
            self.member_value,
            self.delta,
            self.anchor_uncertainty_lower,
            self.anchor_uncertainty_upper,
            self.member_uncertainty_lower,
            self.member_uncertainty_upper,
            self.anchor_denominator_count,
            self.member_denominator_count,
            self.maximum_absolute_delta,
        )
        available = self.availability is ComparisonAvailability.AVAILABLE
        if available != all(item is not None for item in numeric) or (
            not available and any(item is not None for item in numeric)
        ):
            raise ValueError("D07 numbers are present only when available")
        return self


class ProtectedLongitudinalRow(RegistryContract):
    """Every protected identity behind one public row; never crosses HTTP."""

    schema_version: Literal["traceback.e12-protected-longitudinal-row.v1"] = (
        "traceback.e12-protected-longitudinal-row.v1"
    )
    protected_only: Literal[True] = True
    row_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    member: CohortMember
    member_sha256: Sha256
    is_anchor: bool
    status: CohortMemberRecordStatus
    history: ProtectedHistory
    source_state: SourceState
    source: ProtectedSource | None
    values: ProtectedValues
    decision_state: DecisionState
    decision: ProtectedDecision | None
    comparison_state: ComparisonRegistryState
    comparison: ProtectedComparison | None

    @model_validator(mode="after")
    def coherent(self) -> ProtectedLongitudinalRow:
        if hashlib.sha256(canonical_contract_bytes(self.member)).hexdigest() != (
            self.member_sha256
        ):
            raise ValueError("protected row member digest is invalid")
        if self.status.member_sha256 != self.member_sha256:
            raise ValueError("protected row status does not bind its member")
        if (self.source_state is SourceState.VERIFIED) != (self.source is not None):
            raise ValueError("a source is present only when verified")
        available = self.status.availability is CohortRecordAvailability.AVAILABLE
        if not available and (
            self.source is not None
            or self.values.state is not ValueState.SOURCE_UNAVAILABLE
        ):
            raise ValueError("an unavailable record carries no source or values")
        if (self.decision_state is DecisionState.DECIDED) != (
            self.decision is not None
        ):
            raise ValueError("a decision is present only when decided")
        if self.is_anchor != (self.decision_state is DecisionState.ANCHOR):
            raise ValueError("only the anchor row has the anchor decision state")
        if (
            self.comparison_state
            in {
                ComparisonRegistryState.AVAILABLE,
                ComparisonRegistryState.UNAVAILABLE,
            }
        ) != (self.comparison is not None):
            raise ValueError("a comparison is present only when D07 returned one")
        return self


# --- public contracts -------------------------------------------------------------------


class TimeCoordinateSemantics(StrEnum):
    ABSOLUTE_COLLECTION_TIME = "absolute_collection_time"
    SUBJECT_RELATIVE = "subject_relative"
    STUDY_RELATIVE = "study_relative"


class PublicTimeAxis(RegistryContract):
    """Controlled axis description; never a timestamp or protected handle."""

    kind: TimeAxisKind
    unit: Literal["seconds"] = "seconds"
    definition_sha256: Sha256
    coordinate_semantics: TimeCoordinateSemantics
    offset_origin: Literal["first_biological_coordinate"] = (
        "first_biological_coordinate"
    )


class PublicFragmentValue(RegistryContract):
    """One E07 projected value with privacy-safe coordinate and digests only."""

    family: Literal[ProjectionFamily.FRAGMENT] = ProjectionFamily.FRAGMENT
    fragment_quantity: FragmentQuantity
    panel: PanelId
    bin_index: int = Field(ge=0, strict=True)
    lower_inclusive: int = Field(ge=0, strict=True)
    upper_exclusive: int | None = Field(gt=0, strict=True)
    statistic: FragmentStatistic
    statistic_unit: StatisticUnit
    count: int | None = Field(ge=0, strict=True)
    fraction_numerator: int | None = Field(ge=0, strict=True)
    fraction_denominator: int | None = Field(gt=0, strict=True)
    artifact_sha256: Sha256
    chart_sha256: Sha256


class PublicComparison(RegistryContract):
    """D07 numbers copied only from an available replayed comparison."""

    comparison_sha256: Sha256
    classification: Literal[
        RepeatabilityClassification.EXACT_SAME_VALUE,
        RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
    ]
    anchor_value: float
    member_value: float
    delta: float
    anchor_uncertainty_lower: float
    anchor_uncertainty_upper: float
    member_uncertainty_lower: float
    member_uncertainty_upper: float
    anchor_denominator_count: int
    member_denominator_count: int
    maximum_absolute_delta: float
    anchor_relative: Literal[True] = True
    interpretation: Literal[
        "descriptive_technical_difference_only_no_causal_or_clinical_meaning"
    ] = "descriptive_technical_difference_only_no_causal_or_clinical_meaning"


class LongitudinalSourceRow(RegistryContract):
    """One public source row: aliases, ordinals, digests, states and values.

    It carries no protected member, timepoint, provider, subject, collection,
    specimen, run, analysis or result identifier, and no numeric source value
    unless the applicable family projection verified it.
    """

    row_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    source_alias: SourceAlias
    timepoint_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    offset_seconds: int = Field(strict=True)
    lineage_role: MemberLineageRole
    denominator_contributor: bool
    history_state: HistoryState
    affected_comparison_warning: bool
    record_availability: CohortRecordAvailability
    withheld_reason: CohortRecordWithheldReason | None
    catalog_result_sha256: Sha256 | None
    method_ref: MethodReference | None
    source_state: SourceState
    source_sha256: Sha256 | None
    source_replay_sha256: Sha256 | None
    denominator_ledger_sha256: Sha256 | None
    denominator_ledger_label: Literal["operator-entered, unverified"] | None
    execution_state: ExecutionState | None
    information_state: InformationState | None
    compatibility_state: RowCompatibilityState
    compatibility_reasons: tuple[LongitudinalReason, ...] = Field(max_length=32)
    mismatch_dimensions: tuple[ComparisonDimension, ...] = Field(max_length=16)
    unknown_dimensions: tuple[ComparisonDimension, ...] = Field(max_length=16)
    next_action: LongitudinalNextAction | None
    bridge_reference_count: int = Field(ge=0, le=32, strict=True)
    bridge_execution_state: Literal["not_executed"] = "not_executed"
    decision_sha256: Sha256 | None
    value_state: ValueState
    value_rejection: ValueRejection | None
    missing_prerequisite: FamilyPrerequisite | None
    values: tuple[PublicFragmentValue, ...] = Field(max_length=8_192)
    comparison_state: ComparisonState
    suppression_reasons: tuple[SuppressionReason, ...] = Field(max_length=16)
    comparison: PublicComparison | None

    @model_validator(mode="after")
    def suppression_is_structural(self) -> LongitudinalSourceRow:
        available = self.record_availability is CohortRecordAvailability.AVAILABLE
        if not available and (
            self.catalog_result_sha256 is not None
            or self.method_ref is not None
            or self.source_sha256 is not None
            or self.values
            or self.comparison is not None
        ):
            raise ValueError("missing or withheld rows carry no result details")
        if (self.record_availability is CohortRecordAvailability.WITHHELD) != (
            self.withheld_reason is not None
        ):
            raise ValueError("a withheld reason is present only when withheld")
        if (self.value_state is ValueState.PROJECTED) != bool(self.values):
            raise ValueError("values are present only when projected")
        if (self.source_state is SourceState.VERIFIED) != (
            self.source_sha256 is not None
        ):
            raise ValueError("source commitments are present only when verified")
        if (self.denominator_ledger_sha256 is None) != (
            self.denominator_ledger_label is None
        ):
            raise ValueError("an E06 ledger digest is always labelled unverified")
        if (self.comparison_state is ComparisonState.AVAILABLE) != (
            self.comparison is not None
        ):
            raise ValueError("comparison numbers are present only when available")
        if self.comparison is not None and (
            self.compatibility_state
            not in {
                RowCompatibilityState.EQUIVALENT,
                RowCompatibilityState.QUALIFIED_COMPATIBLE,
            }
            or self.suppression_reasons
        ):
            raise ValueError("only an eligible D03 outcome can carry D07 numbers")
        if (self.comparison_state is ComparisonState.SUPPRESSED) != bool(
            self.suppression_reasons
        ):
            raise ValueError("a suppressed comparison names its reasons")
        if (self.compatibility_state is RowCompatibilityState.ANCHOR) != (
            self.comparison_state is ComparisonState.ANCHOR_REFERENCE
        ):
            raise ValueError("only the anchor row is the comparison reference")
        return self


class LongitudinalSegment(RegistryContract):
    """One explicitly authorized anchor-relative segment between adjacent points.

    Both endpoints passed every live gate; the segment names the D07
    comparison(s) that authorize it.  Renderers draw only these.
    """

    from_row_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    to_row_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    from_timepoint_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    to_timepoint_ordinal: int = Field(ge=2, le=MAX_WORKSPACE_MEMBERS, strict=True)
    from_comparison_sha256: Sha256 | None
    to_comparison_sha256: Sha256 | None
    from_delta: float | None
    to_delta: float | None
    anchor_relative: Literal[True] = True

    @model_validator(mode="after")
    def adjacent(self) -> LongitudinalSegment:
        if self.to_timepoint_ordinal != self.from_timepoint_ordinal + 1:
            raise ValueError("segments connect adjacent biological timepoints only")
        if (self.from_comparison_sha256 is None) != (self.from_delta is None) or (
            self.to_comparison_sha256 is None
        ) != (self.to_delta is None):
            raise ValueError("segment endpoint comparison is incomplete")
        if self.from_comparison_sha256 is None and self.to_comparison_sha256 is None:
            raise ValueError("a segment needs at least one D07 comparison")
        return self


class CovariateContextState(StrEnum):
    AVAILABLE = "available"
    NOT_REGISTERED = "not_registered"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"


class PublicCovariateContext(RegistryContract):
    """Aggregate D10 limitation context; tokens are operator-entered."""

    state: CovariateContextState
    token_provenance: Literal["operator-entered, unverified"] = OPERATOR_ENTERED_LABEL
    context_sha256: Sha256 | None = None
    classification: CovariateClassification | None = None
    reason_codes: tuple[CovariateReason, ...] = Field(default=(), max_length=3)
    included_member_count: int | None = Field(default=None, ge=0, strict=True)
    groups: tuple[AggregateCovariateGroup, ...] = Field(default=(), max_length=10_000)
    values_changed: Literal[False] = False
    eligibility_changed: Literal[False] = False
    biological_attribution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> PublicCovariateContext:
        available = self.state is CovariateContextState.AVAILABLE
        if available != (
            self.context_sha256 is not None
            and self.classification is not None
            and self.included_member_count is not None
        ):
            raise ValueError("covariate aggregates are present only when available")
        if not available and (self.reason_codes or self.groups):
            raise ValueError("an unavailable covariate context carries no aggregates")
        return self


class VersionDiffKind(StrEnum):
    INITIAL_VERSION = "initial_version"
    PREDECESSOR = "predecessor"


class VersionDiffReason(StrEnum):
    MEMBERS_ADDED = "members_added"
    MEMBERS_REMOVED = "members_removed"
    INCLUSION_POLICY_CHANGED = "inclusion_policy_changed"
    EXCLUSION_POLICY_CHANGED = "exclusion_policy_changed"
    MISSINGNESS_POLICY_CHANGED = "missingness_policy_changed"
    UNIT_OF_ANALYSIS_CHANGED = "unit_of_analysis_changed"
    REPLICATE_RULE_CHANGED = "replicate_rule_changed"
    REANALYSIS_RULE_CHANGED = "reanalysis_rule_changed"
    TIME_AXIS_CHANGED = "time_axis_changed"
    MEASUREMENT_ANCHOR_CHANGED = "measurement_anchor_changed"
    PROVIDER_AUTHORITY_CHANGED = "provider_authority_changed"


class LongitudinalVersionDiff(RegistryContract):
    """Deterministic diff of the selected cohort version against its predecessor.

    Counts, set digests and controlled reasons only; member identities stay
    protected.  D03 policy changes are not derivable from two manifests and
    are reported as ``not_comparable`` until a saved comparison supplies the
    prior policy (Reopen is a later PR).
    """

    schema_version: Literal["traceback.e12-cohort-version-diff.v1"] = (
        "traceback.e12-cohort-version-diff.v1"
    )
    kind: VersionDiffKind
    selected_version: int = Field(ge=1, le=100_000, strict=True)
    predecessor_version: int | None = Field(ge=1, le=100_000, strict=True)
    selected_manifest_sha256: Sha256
    predecessor_manifest_sha256: Sha256 | None
    added_member_count: int = Field(ge=0, strict=True)
    removed_member_count: int = Field(ge=0, strict=True)
    unchanged_member_count: int = Field(ge=0, strict=True)
    added_member_set_sha256: Sha256
    removed_member_set_sha256: Sha256
    reasons: tuple[VersionDiffReason, ...] = Field(max_length=len(VersionDiffReason))
    d03_policy_change: Literal["not_comparable"] = "not_comparable"
    silent_upgrade: Literal[False] = False


class LimitationCode(StrEnum):
    DESCRIPTIVE_ONLY = "descriptive_only_no_causal_or_clinical_meaning"
    D09_IS_POPULATION_CONTEXT_NOT_A_GATE = "d09_is_population_context_not_a_gate"
    E06_LEDGER_OPERATOR_ENTERED = "e06_ledger_operator_entered_unverified"
    COVARIATE_TOKENS_OPERATOR_ENTERED = "covariate_tokens_operator_entered_unverified"
    COVARIATE_CONTEXT_UNAVAILABLE = "covariate_context_unavailable"
    D03_SERIES_UNAVAILABLE = "d03_series_unavailable"
    STANDALONE_VALUES_UNAVAILABLE = "standalone_values_unavailable"
    E04_ROWS_ONLY_THROUGH_D06 = "e04_rows_only_through_d06_bindings"
    SINGLE_MEASUREMENT_SINGLE_ANCHOR = "single_measurement_single_anchor"
    METHOD_AUTHORITY_HEAD_NOT_CURRENT_VERIFIED = (
        "method_authority_head_not_current_verified"
    )


class PublicDependencyHead(RegistryContract):
    """One dependency store's ID, epoch and head as built from (``H1``)."""

    slot: DependencySlot
    head: DependencyHeadV1

    @model_validator(mode="after")
    def never_the_reader_registry(self) -> PublicDependencyHead:
        if self.slot is DependencySlot.READER_AUTHORIZATION or self.head.id.startswith(
            "reader_registry_"
        ):
            raise ValueError("the reader-registry head is never public")
        return self


def public_dependency_heads(
    heads: SavedComparisonDependencyHeadsV1,
) -> tuple[PublicDependencyHead, ...]:
    """Every head except the reader registry's.

    The reader-registry head is the session credential's bound head in the
    normal case, so it never enters public bytes or the replay digest.
    """

    return tuple(
        PublicDependencyHead(slot=slot, head=getattr(heads, slot.value))
        for slot in DependencySlot
        if slot is not DependencySlot.READER_AUTHORIZATION
    )


class LongitudinalAuthorityCommitments(RegistryContract):
    """Every exact authority identity the workspace was built from."""

    heads: tuple[PublicDependencyHead, ...] = Field(
        min_length=len(DependencySlot) - 1, max_length=len(DependencySlot) - 1
    )
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    record_status_sha256: Sha256
    d09_selector_id: D09SelectorId
    d09_policy_version: int = Field(ge=1, strict=True)
    d09_summary_sha256: Sha256
    d09_population_sha256: Sha256
    anchor_policy_selector_id: AnchorPolicySelectorId
    anchor_policy_version: int = Field(ge=1, strict=True)
    anchor_policy_object_sha256: Sha256
    d03_policy_sha256: Sha256
    d07_envelope_sha256: Sha256
    anchor_selector_id: AnchorCandidateSelectorId
    anchor_candidate_page_sha256: Sha256
    anchor_row_ordinal: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    projection_selector_id: ProjectionSelectorId
    projection_policy_version: int = Field(ge=1, strict=True)
    projection_policy_sha256: Sha256
    projection_family: ProjectionFamily
    d03_series_state: DecisionState
    d03_series_decision_sha256: Sha256 | None
    d10_context_state: CovariateContextState

    @model_validator(mode="after")
    def exact_public_slots(self) -> LongitudinalAuthorityCommitments:
        if tuple(item.slot for item in self.heads) != tuple(
            slot
            for slot in DependencySlot
            if slot is not DependencySlot.READER_AUTHORIZATION
        ):
            raise ValueError("public heads are exactly the non-reader slots in order")
        return self


class _WorkspaceCore(RegistryContract):
    authority: LongitudinalAuthorityCommitments
    time_axis: PublicTimeAxis
    population: CohortPopulationProjection
    covariate_context: PublicCovariateContext
    version_diff: LongitudinalVersionDiff
    limitations: tuple[LimitationCode, ...] = Field(max_length=len(LimitationCode))
    limitation_statement: Literal[
        "Comparisons are descriptive technical differences with no causal or "
        "clinical interpretation."
    ] = LIMITATION_STATEMENT
    filters_sha256: Sha256
    replay_sha256: Sha256
    product_release_authorized: Literal[False] = False
    release_export_authorized: Literal[False] = False
    diagnostic_interpretation_allowed: Literal[False] = False
    synthetic_only: Literal[True] = True


def _validate_segments(
    rows: tuple[LongitudinalSourceRow, ...], segments: tuple[LongitudinalSegment, ...]
) -> None:
    by_ordinal = {row.row_ordinal: row for row in rows}
    for segment in segments:
        endpoints = []
        for ordinal, timepoint, digest, delta in (
            (
                segment.from_row_ordinal,
                segment.from_timepoint_ordinal,
                segment.from_comparison_sha256,
                segment.from_delta,
            ),
            (
                segment.to_row_ordinal,
                segment.to_timepoint_ordinal,
                segment.to_comparison_sha256,
                segment.to_delta,
            ),
        ):
            row = by_ordinal.get(ordinal)
            if row is None or row.timepoint_ordinal != timepoint:
                raise ValueError("segment endpoint is not a row at its timepoint")
            if row.lineage_role is not MemberLineageRole.BIOLOGICAL_DRAW:
                raise ValueError("segment endpoints are biological draws")
            if row.comparison_state is ComparisonState.ANCHOR_REFERENCE:
                if digest is not None:
                    raise ValueError("the anchor endpoint has no comparison")
            elif row.comparison is None or (
                row.comparison.comparison_sha256,
                row.comparison.delta,
            ) != (digest, delta):
                raise ValueError("segment endpoint is not an available comparison")
            endpoints.append(row)
    pairs = [(item.from_row_ordinal, item.to_row_ordinal) for item in segments]
    if pairs != sorted(set(pairs)):
        raise ValueError("segments must be uniquely ordered")


class LongitudinalWorkspace(_WorkspaceCore):
    """Protected D08 read model: exact authority, protected and public rows."""

    schema_version: Literal["traceback.e12-longitudinal-workspace.v1"] = (
        "traceback.e12-longitudinal-workspace.v1"
    )
    protected_only: Literal[True] = True
    request: LongitudinalWorkspaceRequest
    reader_authorization: ReaderAuthorization
    dependency_heads: SavedComparisonDependencyHeadsV1
    anchor_record_sha256: Sha256
    protected_rows: tuple[ProtectedLongitudinalRow, ...] = Field(
        min_length=1, max_length=MAX_WORKSPACE_MEMBERS
    )
    rows: tuple[LongitudinalSourceRow, ...] = Field(
        min_length=1, max_length=MAX_WORKSPACE_MEMBERS
    )
    visible_row_ordinals: tuple[int, ...] = Field(max_length=MAX_WORKSPACE_MEMBERS)
    segments: tuple[LongitudinalSegment, ...] = Field(max_length=MAX_WORKSPACE_MEMBERS)

    @model_validator(mode="after")
    def coherent(self) -> LongitudinalWorkspace:
        ordinals = [row.row_ordinal for row in self.rows]
        if (
            ordinals != list(range(1, len(self.rows) + 1))
            or [row.row_ordinal for row in self.protected_rows] != ordinals
        ):
            raise ValueError("rows must use canonical ordinals")
        if self.visible_row_ordinals != tuple(
            sorted(set(self.visible_row_ordinals))
        ) or not set(self.visible_row_ordinals) <= set(ordinals):
            raise ValueError("visible rows must be canonical row ordinals")
        _validate_segments(self.rows, self.segments)
        if self.authority.heads != public_dependency_heads(self.dependency_heads):
            raise ValueError("public heads must be the protected head vector")
        return self


class LongitudinalWorkspaceProjection(_WorkspaceCore):
    """Public contract for the loopback explorer; protected rows are absent."""

    schema_version: Literal["traceback.e12-longitudinal-workspace-projection.v1"] = (
        "traceback.e12-longitudinal-workspace-projection.v1"
    )
    request: LongitudinalWorkspaceRequest
    total_row_count: int = Field(ge=1, le=MAX_WORKSPACE_MEMBERS, strict=True)
    rows: tuple[LongitudinalSourceRow, ...] = Field(max_length=MAX_WORKSPACE_MEMBERS)
    segments: tuple[LongitudinalSegment, ...] = Field(max_length=MAX_WORKSPACE_MEMBERS)

    @model_validator(mode="after")
    def coherent(self) -> LongitudinalWorkspaceProjection:
        ordinals = [row.row_ordinal for row in self.rows]
        if ordinals != sorted(set(ordinals)) or any(
            item > self.total_row_count for item in ordinals
        ):
            raise ValueError("projected rows must be canonical")
        _validate_segments(self.rows, self.segments)
        return self


# --- pure derivation ----------------------------------------------------------------------


def _sha256(domain: bytes, *parts: bytes) -> str:
    digest = hashlib.sha256(domain)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _contract_digest(value: RegistryContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def member_set_sha256(member_sha256s: Iterable[str]) -> str:
    return _sha256(
        b"traceback-e12-member-set-v1",
        *(item.encode("ascii") for item in sorted(member_sha256s)),
    )


def _source_alias(
    cohort_selector_id: str, cohort_version: int, member_sha256: str
) -> str:
    digest = _sha256(
        b"traceback-e12-source-alias-v1",
        cohort_selector_id.encode("ascii"),
        str(cohort_version).encode("ascii"),
        member_sha256.encode("ascii"),
    )
    return f"source_{digest[:16]}"


def timepoint_positions(
    manifest: CohortManifest,
) -> dict[tuple[int, str], tuple[int, int]]:
    """Public (ordinal, offset) per protected (coordinate, timepoint handle).

    The same derivation as the anchor-policy candidate page: biological
    timepoints in (coordinate, handle) order, 1-based ordinals, and the signed
    offset in seconds from the first biological coordinate.  Technical
    replicates and reanalyses share their source collection's handle and
    coordinate, so they never create another timepoint.
    """

    keys = sorted(
        {
            (item.time_coordinate, item.biological_timepoint_id)
            for item in manifest.members
        }
    )
    origin = keys[0][0]
    return {key: (index, key[0] - origin) for index, key in enumerate(keys, start=1)}


def _time_axis(manifest: CohortManifest) -> PublicTimeAxis:
    semantics = {
        TimeAxisKind.COLLECTION_TIME: TimeCoordinateSemantics.ABSOLUTE_COLLECTION_TIME,
        TimeAxisKind.SUBJECT_RELATIVE: TimeCoordinateSemantics.SUBJECT_RELATIVE,
        TimeAxisKind.STUDY_RELATIVE: TimeCoordinateSemantics.STUDY_RELATIVE,
    }[manifest.time_axis.kind]
    return PublicTimeAxis(
        kind=manifest.time_axis.kind,
        definition_sha256=manifest.time_axis.definition_sha256,
        coordinate_semantics=semantics,
    )


def derive_version_diff(
    history: tuple[CohortManifest, ...],
) -> LongitudinalVersionDiff:
    """Diff the last manifest of a registered history against its predecessor."""

    selected = history[-1]
    selected_members = {
        hashlib.sha256(canonical_contract_bytes(item)).hexdigest()
        for item in selected.members
    }
    if len(history) < 2:
        return LongitudinalVersionDiff(
            kind=VersionDiffKind.INITIAL_VERSION,
            selected_version=selected.version,
            predecessor_version=None,
            selected_manifest_sha256=cohort_manifest_sha256(selected),
            predecessor_manifest_sha256=None,
            added_member_count=len(selected_members),
            removed_member_count=0,
            unchanged_member_count=0,
            added_member_set_sha256=member_set_sha256(selected_members),
            removed_member_set_sha256=member_set_sha256(()),
            reasons=(VersionDiffReason.MEMBERS_ADDED,),
        )
    previous = history[-2]
    previous_members = {
        hashlib.sha256(canonical_contract_bytes(item)).hexdigest()
        for item in previous.members
    }
    added = selected_members - previous_members
    removed = previous_members - selected_members
    checks = (
        (bool(added), VersionDiffReason.MEMBERS_ADDED),
        (bool(removed), VersionDiffReason.MEMBERS_REMOVED),
        (
            previous.policies.inclusion_sha256 != selected.policies.inclusion_sha256,
            VersionDiffReason.INCLUSION_POLICY_CHANGED,
        ),
        (
            previous.policies.exclusion_sha256 != selected.policies.exclusion_sha256,
            VersionDiffReason.EXCLUSION_POLICY_CHANGED,
        ),
        (
            previous.policies.missingness_sha256
            != selected.policies.missingness_sha256,
            VersionDiffReason.MISSINGNESS_POLICY_CHANGED,
        ),
        (
            previous.unit_of_analysis != selected.unit_of_analysis,
            VersionDiffReason.UNIT_OF_ANALYSIS_CHANGED,
        ),
        (
            previous.technical_replicate_rule != selected.technical_replicate_rule,
            VersionDiffReason.REPLICATE_RULE_CHANGED,
        ),
        (
            previous.reanalysis_rule != selected.reanalysis_rule,
            VersionDiffReason.REANALYSIS_RULE_CHANGED,
        ),
        (previous.time_axis != selected.time_axis, VersionDiffReason.TIME_AXIS_CHANGED),
        (
            previous.measurement_anchor != selected.measurement_anchor,
            VersionDiffReason.MEASUREMENT_ANCHOR_CHANGED,
        ),
        (
            previous.provider_authorities != selected.provider_authorities,
            VersionDiffReason.PROVIDER_AUTHORITY_CHANGED,
        ),
    )
    return LongitudinalVersionDiff(
        kind=VersionDiffKind.PREDECESSOR,
        selected_version=selected.version,
        predecessor_version=previous.version,
        selected_manifest_sha256=cohort_manifest_sha256(selected),
        predecessor_manifest_sha256=cohort_manifest_sha256(previous),
        added_member_count=len(added),
        removed_member_count=len(removed),
        unchanged_member_count=len(selected_members & previous_members),
        added_member_set_sha256=member_set_sha256(added),
        removed_member_set_sha256=member_set_sha256(removed),
        reasons=tuple(reason for changed, reason in checks if changed),
    )


def _row_compatibility(row: ProtectedLongitudinalRow) -> RowCompatibilityState:
    if row.is_anchor:
        return RowCompatibilityState.ANCHOR
    if row.decision is None:
        return RowCompatibilityState.NOT_EVALUATED
    return RowCompatibilityState(row.decision.outcome.value)


def comparison_suppression(
    row: ProtectedLongitudinalRow,
    anchor: ProtectedLongitudinalRow,
    *,
    policy_sha256: str,
    envelope_sha256: str,
    anchor_record_sha256: str,
) -> tuple[SuppressionReason, ...]:
    """Every failed gate for one anchor-relative D07 comparison; empty = eligible.

    Gates: D06 availability of both endpoints, D04 active history of both
    endpoints, the D03 decision (comparable outcome, delta allowed, exact D01
    receipts and D06 result), and an available D07 comparison bound to that
    exact decision, policy, anchor record and the resolved anchor envelope.
    D09 counts are never a gate.
    """

    reasons: list[SuppressionReason] = []
    if row.status.availability is not CohortRecordAvailability.AVAILABLE:
        reasons.append(SuppressionReason.RECORD_NOT_AVAILABLE)
    if anchor.status.availability is not CohortRecordAvailability.AVAILABLE:
        reasons.append(SuppressionReason.ANCHOR_RECORD_NOT_AVAILABLE)
    if row.history.state is not HistoryState.ACTIVE:
        reasons.append(SuppressionReason.HISTORY_NOT_ACTIVE)
    if anchor.history.state is not HistoryState.ACTIVE:
        reasons.append(SuppressionReason.ANCHOR_HISTORY_NOT_ACTIVE)
    decision = row.decision
    if decision is None:
        reasons.append(SuppressionReason.D03_NOT_DECIDED)
    else:
        if decision.outcome not in _COMPARABLE_OUTCOMES:
            reasons.append(SuppressionReason.D03_NOT_COMPARABLE)
        elif not decision.delta_allowed:
            reasons.append(SuppressionReason.D03_DELTA_NOT_ALLOWED)
        if (
            decision.member_linkage_receipt_sha256
            != row.member.committed_receipt_sha256
            or decision.anchor_linkage_receipt_sha256
            != anchor.member.committed_receipt_sha256
            or decision.policy_sha256 != policy_sha256
            or decision.anchor_record_sha256 != anchor_record_sha256
        ):
            reasons.append(SuppressionReason.LINKAGE_BINDING_MISMATCH)
        binding = row.status.binding
        if binding is None or binding.result.result_id != decision.member_result_id:
            reasons.append(SuppressionReason.RESULT_BINDING_MISMATCH)
    state = row.comparison_state
    comparison = row.comparison
    if state is ComparisonRegistryState.NOT_REGISTERED:
        reasons.append(SuppressionReason.D07_NOT_REGISTERED)
    elif state is ComparisonRegistryState.AMBIGUOUS:
        reasons.append(SuppressionReason.D07_AMBIGUOUS)
    elif state is ComparisonRegistryState.STALE:
        reasons.append(SuppressionReason.D07_STALE)
    elif state is ComparisonRegistryState.NOT_APPLICABLE or comparison is None:
        if decision is not None:
            reasons.append(SuppressionReason.D07_NOT_REGISTERED)
    else:
        if comparison.availability is not ComparisonAvailability.AVAILABLE:
            reasons.append(SuppressionReason.D07_UNAVAILABLE)
        if (
            decision is None
            or comparison.d03_decision_sha256 != decision.decision_sha256
            or comparison.member_record_sha256 != decision.member_record_sha256
            or comparison.anchor_policy_sha256 != policy_sha256
            or comparison.anchor_record_sha256 != anchor_record_sha256
            or comparison.repeatability_envelope_sha256 != envelope_sha256
        ):
            reasons.append(SuppressionReason.D07_BINDING_MISMATCH)
    return tuple(
        sorted(set(reasons), key=lambda item: list(SuppressionReason).index(item))
    )


def _public_values(values: ProtectedValues) -> tuple[PublicFragmentValue, ...]:
    projection = values.projection
    if projection is None:
        return ()
    result = []
    for item in projection.projections:
        if type(item) is not FragmentLongitudinalValueProjectionV1:
            raise ValueError("only fragment values are projected in this cut")
        result.append(
            PublicFragmentValue(
                fragment_quantity=item.fragment_quantity,
                panel=item.panel,
                bin_index=item.bin.bin_index,
                lower_inclusive=item.bin.lower_inclusive,
                upper_exclusive=item.bin.upper_exclusive,
                statistic=item.statistic,
                statistic_unit=item.statistic_unit,
                count=item.count,
                fraction_numerator=item.fraction_numerator,
                fraction_denominator=item.fraction_denominator,
                artifact_sha256=item.artifact_sha256,
                chart_sha256=item.chart_sha256,
            )
        )
    return tuple(result)


def _public_row(
    row: ProtectedLongitudinalRow,
    anchor: ProtectedLongitudinalRow,
    *,
    positions: dict[tuple[int, str], tuple[int, int]],
    cohort_selector_id: str,
    cohort_version: int,
    policy_sha256: str,
    envelope_sha256: str,
    anchor_record_sha256: str,
) -> LongitudinalSourceRow:
    member = row.member
    timepoint, offset = positions[
        (member.time_coordinate, member.biological_timepoint_id)
    ]
    binding = row.status.binding
    source = row.source
    decision = row.decision
    compatibility = _row_compatibility(row)
    if row.is_anchor:
        comparison_state = ComparisonState.ANCHOR_REFERENCE
        reasons: tuple[SuppressionReason, ...] = ()
    else:
        reasons = comparison_suppression(
            row,
            anchor,
            policy_sha256=policy_sha256,
            envelope_sha256=envelope_sha256,
            anchor_record_sha256=anchor_record_sha256,
        )
        comparison_state = (
            ComparisonState.SUPPRESSED if reasons else ComparisonState.AVAILABLE
        )
    public_comparison = None
    if comparison_state is ComparisonState.AVAILABLE:
        value = row.comparison
        assert value is not None
        public_comparison = PublicComparison(
            comparison_sha256=value.comparison_sha256,
            classification=value.classification,
            anchor_value=value.anchor_value,
            member_value=value.member_value,
            delta=value.delta,
            anchor_uncertainty_lower=value.anchor_uncertainty_lower,
            anchor_uncertainty_upper=value.anchor_uncertainty_upper,
            member_uncertainty_lower=value.member_uncertainty_lower,
            member_uncertainty_upper=value.member_uncertainty_upper,
            anchor_denominator_count=value.anchor_denominator_count,
            member_denominator_count=value.member_denominator_count,
            maximum_absolute_delta=value.maximum_absolute_delta,
        )
    return LongitudinalSourceRow(
        row_ordinal=row.row_ordinal,
        source_alias=_source_alias(
            cohort_selector_id, cohort_version, row.member_sha256
        ),
        timepoint_ordinal=timepoint,
        offset_seconds=offset,
        lineage_role=member.lineage_role,
        denominator_contributor=member.denominator_contribution,
        history_state=row.history.state,
        affected_comparison_warning=(
            row.history.state is HistoryState.SUPERSEDED
            or row.history.affected_comparison_count > 0
        ),
        record_availability=row.status.availability,
        withheld_reason=row.status.withheld_reason,
        catalog_result_sha256=(
            _contract_digest(binding.result) if binding is not None else None
        ),
        method_ref=binding.result.method_ref if binding is not None else None,
        source_state=row.source_state,
        source_sha256=source.source_sha256 if source else None,
        source_replay_sha256=source.source_replay_sha256 if source else None,
        denominator_ledger_sha256=source.denominator_ledger_sha256 if source else None,
        denominator_ledger_label=OPERATOR_ENTERED_LABEL if source else None,
        execution_state=source.execution_state if source else None,
        information_state=source.information_state if source else None,
        compatibility_state=compatibility,
        compatibility_reasons=decision.reason_codes if decision else (),
        mismatch_dimensions=decision.mismatch_dimensions if decision else (),
        unknown_dimensions=decision.unknown_dimensions if decision else (),
        next_action=decision.next_action if decision else None,
        bridge_reference_count=decision.bridge_reference_count if decision else 0,
        decision_sha256=decision.decision_sha256 if decision else None,
        value_state=row.values.state,
        value_rejection=row.values.rejection,
        missing_prerequisite=row.values.prerequisite,
        values=_public_values(row.values),
        comparison_state=comparison_state,
        suppression_reasons=reasons,
        comparison=public_comparison,
    )


def derive_segments(
    rows: tuple[LongitudinalSourceRow, ...],
) -> tuple[LongitudinalSegment, ...]:
    """Segments between adjacent biological timepoints whose single draw is eligible.

    A timepoint qualifies only when it has exactly one biological-draw row and
    that row is the anchor reference or carries an available comparison.  A
    timepoint with any ineligible or a second draw breaks the series, so an
    incompatible middle member can never connect compatible endpoints.
    Technical replicates and reanalyses never become endpoints.
    """

    draws: dict[int, list[LongitudinalSourceRow]] = {}
    for row in rows:
        if row.lineage_role is MemberLineageRole.BIOLOGICAL_DRAW:
            draws.setdefault(row.timepoint_ordinal, []).append(row)
    eligible: dict[int, LongitudinalSourceRow] = {}
    for timepoint, items in draws.items():
        if len(items) == 1 and items[0].comparison_state in {
            ComparisonState.AVAILABLE,
            ComparisonState.ANCHOR_REFERENCE,
        }:
            eligible[timepoint] = items[0]
    segments = []
    for timepoint in sorted(eligible):
        following = eligible.get(timepoint + 1)
        if following is None:
            continue
        start = eligible[timepoint]
        segments.append(
            LongitudinalSegment(
                from_row_ordinal=start.row_ordinal,
                to_row_ordinal=following.row_ordinal,
                from_timepoint_ordinal=timepoint,
                to_timepoint_ordinal=timepoint + 1,
                from_comparison_sha256=(
                    start.comparison.comparison_sha256 if start.comparison else None
                ),
                to_comparison_sha256=(
                    following.comparison.comparison_sha256
                    if following.comparison
                    else None
                ),
                from_delta=start.comparison.delta if start.comparison else None,
                to_delta=following.comparison.delta if following.comparison else None,
            )
        )
    return tuple(
        sorted(segments, key=lambda item: (item.from_row_ordinal, item.to_row_ordinal))
    )


def filter_rows(
    rows: tuple[LongitudinalSourceRow, ...], filters: LongitudinalWorkspaceFilters
) -> tuple[int, ...]:
    """Visible row ordinals; filters never change roles, counts or values."""

    def keep(row: LongitudinalSourceRow) -> bool:
        return (
            (
                not filters.timepoint_ordinals
                or row.timepoint_ordinal in filters.timepoint_ordinals
            )
            and (not filters.lineage_roles or row.lineage_role in filters.lineage_roles)
            and (
                not filters.record_availability
                or row.record_availability in filters.record_availability
            )
            and (
                not filters.compatibility_states
                or row.compatibility_state in filters.compatibility_states
            )
        )

    return tuple(row.row_ordinal for row in rows if keep(row))


class WorkspaceAuthorityInputs(RegistryContract):
    """Everything the pure derivation consumes besides the protected rows.

    Built only by the builder from live reads; exposed for property tests of
    the derivation, never accepted from a caller by any public entry point.
    """

    request: LongitudinalWorkspaceRequest
    reader_authorization: ReaderAuthorization
    dependency_heads: SavedComparisonDependencyHeadsV1
    authority: LongitudinalAuthorityCommitments
    time_axis: PublicTimeAxis
    population: CohortPopulationProjection
    covariate_context: PublicCovariateContext
    version_diff: LongitudinalVersionDiff
    anchor_record_sha256: Sha256
    limitations: tuple[LimitationCode, ...] = Field(max_length=len(LimitationCode))


def filters_sha256(filters: LongitudinalWorkspaceFilters) -> str:
    return _sha256(
        b"traceback-e12-workspace-filters-v1", canonical_contract_bytes(filters)
    )


_REPLAY_TIME_PLACEHOLDER = datetime(1970, 1, 1, tzinfo=UTC)


def _row_replay_bytes(row: ProtectedLongitudinalRow) -> bytes:
    """Row commitment without D07's live ``replayed_at`` instant.

    ``replayed_at`` is the authority clock at the read, so it differs across
    rebuilds of unchanged authority; it stays in the protected row only.
    """

    if row.comparison is None:
        return canonical_contract_bytes(row)
    return canonical_contract_bytes(
        row.model_copy(
            update={
                "comparison": row.comparison.model_copy(
                    update={"replayed_at": _REPLAY_TIME_PLACEHOLDER}
                )
            }
        )
    )


def _replay_sha256(
    inputs: WorkspaceAuthorityInputs,
    protected_rows: tuple[ProtectedLongitudinalRow, ...],
    rows: tuple[LongitudinalSourceRow, ...],
    segments: tuple[LongitudinalSegment, ...],
) -> str:
    """Digest every authority, row, filter and value commitment.

    The reader authorization and session credential are deliberately absent.
    """

    payload = (
        canonical_contract_bytes(inputs.request),
        canonical_contract_bytes(inputs.authority),
        canonical_contract_bytes(inputs.time_axis),
        canonical_contract_bytes(inputs.population),
        canonical_contract_bytes(inputs.covariate_context),
        canonical_contract_bytes(inputs.version_diff),
        inputs.anchor_record_sha256.encode("ascii"),
        *(_row_replay_bytes(item) for item in protected_rows),
        *(canonical_contract_bytes(item) for item in rows),
        *(canonical_contract_bytes(item) for item in segments),
    )
    return _sha256(b"traceback-e12-workspace-replay-v1", *payload)


def derive_workspace(
    inputs: WorkspaceAuthorityInputs,
    protected_rows: tuple[ProtectedLongitudinalRow, ...],
    *,
    positions: dict[tuple[int, str], tuple[int, int]],
) -> LongitudinalWorkspace:
    """Pure derivation from captured authority; no store, callback or I/O."""

    anchors = [row for row in protected_rows if row.is_anchor]
    if (
        len(anchors) != 1
        or anchors[0].row_ordinal != inputs.authority.anchor_row_ordinal
    ):
        raise ValueError("workspace requires exactly one pinned anchor row")
    anchor = anchors[0]
    authority = inputs.authority
    rows = tuple(
        _public_row(
            row,
            anchor,
            positions=positions,
            cohort_selector_id=authority.cohort_selector_id,
            cohort_version=authority.cohort_version,
            policy_sha256=authority.d03_policy_sha256,
            envelope_sha256=authority.d07_envelope_sha256,
            anchor_record_sha256=inputs.anchor_record_sha256,
        )
        for row in protected_rows
    )
    segments = derive_segments(rows)
    filters = inputs.request.filters
    return LongitudinalWorkspace(
        authority=authority,
        time_axis=inputs.time_axis,
        population=inputs.population,
        covariate_context=inputs.covariate_context,
        version_diff=inputs.version_diff,
        limitations=inputs.limitations,
        filters_sha256=filters_sha256(filters),
        replay_sha256=_replay_sha256(inputs, protected_rows, rows, segments),
        request=inputs.request,
        reader_authorization=inputs.reader_authorization,
        dependency_heads=inputs.dependency_heads,
        anchor_record_sha256=inputs.anchor_record_sha256,
        protected_rows=protected_rows,
        rows=rows,
        visible_row_ordinals=filter_rows(rows, filters),
        segments=segments,
    )


def project_longitudinal_workspace(
    workspace: LongitudinalWorkspace,
) -> LongitudinalWorkspaceProjection:
    """The public projection: visible rows and segments, nothing protected."""

    if type(workspace) is not LongitudinalWorkspace:
        raise TypeError("an exact longitudinal workspace is required")
    captured = contract_from_canonical_bytes(
        LongitudinalWorkspace, canonical_contract_bytes(workspace)
    )
    visible = set(captured.visible_row_ordinals)
    return LongitudinalWorkspaceProjection(
        authority=captured.authority,
        time_axis=captured.time_axis,
        population=captured.population,
        covariate_context=captured.covariate_context,
        version_diff=captured.version_diff,
        limitations=captured.limitations,
        filters_sha256=captured.filters_sha256,
        replay_sha256=captured.replay_sha256,
        request=captured.request,
        total_row_count=len(captured.rows),
        rows=tuple(row for row in captured.rows if row.row_ordinal in visible),
        segments=tuple(
            item
            for item in captured.segments
            if item.from_row_ordinal in visible and item.to_row_ordinal in visible
        ),
    )


def longitudinal_projection_bytes(projection: LongitudinalWorkspaceProjection) -> bytes:
    if type(projection) is not LongitudinalWorkspaceProjection:
        raise TypeError("an exact longitudinal projection is required")
    return canonical_contract_bytes(projection)


# --- live gather -------------------------------------------------------------------------

_PINS: dict[type, tuple[str, ...]] = {
    ReaderAuthorizationRegistry: ("authority_read_fence", "authorize_reader_in_fence"),
    ProviderLinkageStore: (),
    CohortRegistry: ("list_selectors", "resolve_history"),
    CohortRecordCatalog: ("record_status_for_manifest",),
    ResultCatalog: (),
    ResultTrustRegistry: (),
    RecordSupersessionStore: ("record_history_snapshot",),
    AnchorPolicyRegistry: ("resolve_anchor",),
    ProjectionPolicyRegistry: ("resolve",),
    ResultViewSourceRegistry: (
        "registry_identity",
        "list_selectors",
        "selector_for_member",
        "resolve",
    ),
    MeasurementSourceArtifactRegistry: (
        "list_selectors",
        "selector_for_e06_source",
        "resolve",
    ),
    LongitudinalDecisionRegistry: ("list_selectors", "resolve"),
    RepeatabilityComparisonRegistry: ("list_selectors", "resolve"),
    DenominatorPolicyRegistry: ("resolve",),
    CovariateContextRegistry: ("list_selectors", "resolve"),
}
_PINNED: dict[tuple[type, str], Any] = {
    (cls, name): cls.__dict__[name] for cls, names in _PINS.items() for name in names
}
_PINNED_SNAPSHOT = CompositeAuthorityCoordinator.__dict__["snapshot"]
_PINNED_FRAGMENT_PROJECT = project_source_values


def _call(store: object, cls: type, name: str, *args: Any, **kwargs: Any) -> Any:
    """Invoke one captured unbound store method after re-checking the pin."""

    function = _PINNED[(cls, name)]
    if type(store) is not cls or cls.__dict__.get(name) is not function:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    try:
        state = object.__getattribute__(store, "__dict__")
    except AttributeError:
        state = {}
    if name in state:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    return function(store, *args, **kwargs)


def _require_store(store: object, cls: type) -> None:
    if type(store) is not cls:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    state = object.__getattribute__(store, "__dict__")
    for name in _PINS[cls]:
        if cls.__dict__.get(name) is not _PINNED[(cls, name)] or name in state:
            _fail(
                LongitudinalErrorCode.INTEGRITY_FAILURE,
                LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
            )


def _same_head(head: DependencyHeadV1, identity: tuple[str, str, str]) -> bool:
    return (head.id, head.epoch, head.head) == identity


def _require(condition: bool) -> None:
    """A read did not bind the captured snapshot: a concurrent change or race."""

    if not condition:
        _fail(LongitudinalErrorCode.READ_CONFLICT, LongitudinalRemediation.RETRY_READ)


def _require_integrity(condition: bool) -> None:
    if not condition:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )


def _authorize(
    registry: ReaderAuthorizationRegistry,
    credential: ReaderGrantBinding,
    *,
    sources: ResultViewSourceRegistry,
    scope: MeasurementScope,
) -> ReaderAuthorization:
    """Authorize first; every failure here is the same ``permission_denied``.

    The cohort registry the grant must cover is the E06 registry's immutable
    lock-free identity binding; reading it, or finding any store unhealthy or
    mis-typed, denies without revealing which.
    """

    def operation() -> ReaderAuthorization:
        cohort_registry_id = _call(
            sources, ResultViewSourceRegistry, "registry_identity"
        ).cohort_registry_id
        fence = _call(registry, ReaderAuthorizationRegistry, "authority_read_fence")
        with fence:
            return _call(
                registry,
                ReaderAuthorizationRegistry,
                "authorize_reader_in_fence",
                credential.grant_sha256,
                expected_state_head_sha256=credential.state_head_sha256,
                cohort_registry_id=cohort_registry_id,
                measurement_scope=scope,
            )

    failure = None
    try:
        return operation()
    except ReaderAuthorizationDenied:
        failure = LongitudinalErrorCode.PERMISSION_DENIED
    except Exception:
        # A missing, unhealthy or replaced registry denies like any other grant
        # failure: no partial response, no detail.
        failure = LongitudinalErrorCode.PERMISSION_DENIED
    _fail(failure, LongitudinalRemediation.OBTAIN_CURRENT_READER_GRANT)


def _selector_record(
    registry: CohortRegistry, selector_id: str, version: int
) -> CohortSelectorRecord:
    """The bounded D05 selector-page row for one exact selector/version."""

    if version > 1:
        cursor: dict[str, Any] = {
            "after_selector_id": selector_id,
            "after_version": version - 1,
        }
    else:
        number = int(selector_id[len("cohort_selector_") :], 16)
        cursor = (
            {}
            if number == 0
            else {
                "after_selector_id": f"cohort_selector_{number - 1:040x}",
                "after_version": 100_000,
            }
        )
    page = _guarded(
        lambda: _call(registry, CohortRegistry, "list_selectors", limit=1, **cursor),
        conflict=LongitudinalErrorCode.INVALID_REQUEST,
        remediation=LongitudinalRemediation.RESELECT_COHORT_VERSION,
    )
    records = [
        item
        for item in page.records
        if item.selector_id == selector_id and item.cohort_version == version
    ]
    if len(records) != 1:
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.RESELECT_COHORT_VERSION,
        )
    return records[0]


def _scan_pages(
    fetch: Callable[[str | None], Any], *, cursor_field: str = "next_after_selector_id"
) -> list[Any]:
    """Every page of one bounded selector listing, with a hard page cap."""

    pages = []
    cursor: str | None = None
    for _ in range(MAX_SELECTOR_SCAN_PAGES):
        page = fetch(cursor)
        pages.append(page)
        cursor = getattr(page, cursor_field)
        if cursor is None:
            return pages
    _fail(
        LongitudinalErrorCode.INTEGRITY_FAILURE,
        LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
    )


def _snapshot(
    coordinator: CompositeAuthorityCoordinator, scope: SavedComparisonDependencyScopeV1
) -> CompositeAuthoritySnapshotV1:
    return _guarded(
        lambda: _PINNED_SNAPSHOT(coordinator, scope),
        remediation=LongitudinalRemediation.RESELECT_COHORT_VERSION,
    )


def _history_map(
    store: RecordSupersessionStore, heads: SavedComparisonDependencyHeadsV1
) -> dict[tuple[str, str, str], list[RecordHistoryEntry]]:
    """Every D04 record, bound to the captured D04 and D01 heads."""

    entries: dict[tuple[str, str, str], list[RecordHistoryEntry]] = {}
    cursor = None
    for _ in range(MAX_HISTORY_PAGES):
        page: RecordHistorySnapshot = _guarded(
            lambda cursor=cursor: _call(
                store,
                RecordSupersessionStore,
                "record_history_snapshot",
                cursor=cursor,
                limit=MAX_HISTORY_PAGE_RECORDS,
            )
        )
        _require(
            _same_head(
                heads.d04_history,
                (page.ledger_id, page.ledger_epoch_sha256, page.state_head_sha256),
            )
            and _same_head(
                heads.d01_linkage,
                (
                    page.linkage_store_id,
                    page.linkage_store_epoch_sha256,
                    page.linkage_state_head_sha256,
                ),
            )
        )
        for entry in page.records:
            record = entry.record
            entries.setdefault(
                (
                    record.provider_namespace,
                    record.analysis_record_id,
                    record.result_id,
                ),
                [],
            ).append(entry)
        cursor = page.next_cursor
        if cursor is None:
            return entries
    _fail(
        LongitudinalErrorCode.INTEGRITY_FAILURE,
        LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
    )


def _history_for(
    member: CohortMember,
    status: CohortMemberRecordStatus,
    entries: dict[tuple[str, str, str], list[RecordHistoryEntry]],
) -> ProtectedHistory:
    binding = status.binding
    if binding is None:
        return ProtectedHistory(state=HistoryState.NOT_RECORDED)
    found = entries.get(
        (member.provider_namespace, member.analysis_record_id, binding.result.result_id)
    )
    if not found:
        return ProtectedHistory(state=HistoryState.NOT_RECORDED)
    if len(found) != 1:
        return ProtectedHistory(state=HistoryState.BINDING_MISMATCH)
    entry = found[0]
    record = entry.record
    if (
        record.linkage_id != member.linkage_id
        or record.linkage_revision != member.linkage_revision
        or record.linkage_revision_sha256 != member.linkage_revision_sha256
        or record.bundle_sha256 != binding.result.bundle_sha256
    ):
        return ProtectedHistory(state=HistoryState.BINDING_MISMATCH)
    return ProtectedHistory(
        state=HistoryState(entry.state.value),
        record_sha256=record_sha256(record),
        successor_present=entry.successor_record_id is not None,
        affected_comparison_count=entry.affected_comparison_count,
    )


def _decision_fields(
    decision: LongitudinalMemberDecision,
) -> ProtectedDecision:
    return ProtectedDecision(
        decision_sha256=longitudinal_member_decision_sha256(decision),
        member_result_id=decision.member_result_id,
        anchor_record_sha256=decision.anchor_record_sha256,
        member_record_sha256=decision.member_record_sha256,
        anchor_linkage_receipt_sha256=decision.anchor_linkage_receipt_sha256,
        member_linkage_receipt_sha256=decision.member_linkage_receipt_sha256,
        policy_sha256=decision.policy_sha256,
        outcome=decision.outcome,
        reason_codes=decision.reason_codes,
        next_action=decision.next_action,
        mismatch_dimensions=decision.mismatch_dimensions,
        unknown_dimensions=decision.unknown_dimensions,
        bridge_reference_count=len(decision.bridge_refs),
        delta_allowed=decision.delta_allowed,
    )


def _comparison_fields(value: RegisteredRepeatabilityComparison) -> ProtectedComparison:
    comparison = value.comparison
    return ProtectedComparison(
        selector_id=value.selector_id,
        comparison_sha256=value.comparison_sha256,
        d03_decision_sha256=comparison.d03_decision_sha256,
        anchor_policy_sha256=comparison.anchor_policy_sha256,
        anchor_record_sha256=comparison.anchor_record_sha256,
        member_record_sha256=comparison.member_record_sha256,
        repeatability_envelope_sha256=comparison.repeatability_envelope_sha256,
        availability=comparison.availability,
        classification=comparison.classification,
        reason_codes=comparison.reason_codes,
        anchor_value=comparison.anchor_value,
        member_value=comparison.member_value,
        delta=comparison.delta,
        anchor_uncertainty_lower=comparison.anchor_uncertainty_lower,
        anchor_uncertainty_upper=comparison.anchor_uncertainty_upper,
        member_uncertainty_lower=comparison.member_uncertainty_lower,
        member_uncertainty_upper=comparison.member_uncertainty_upper,
        anchor_denominator_count=comparison.anchor_denominator_count,
        member_denominator_count=comparison.member_denominator_count,
        maximum_absolute_delta=comparison.maximum_absolute_delta,
        replayed_at=value.replayed_at,
    )


_REJECTIONS: tuple[tuple[type[SourceValueProjectionError], ValueRejection], ...] = (
    (SourceValuePolicyRejected, ValueRejection.POLICY_REJECTED),
    (SourceValueFamilyMismatch, ValueRejection.FAMILY_MISMATCH),
    (SourceValueMeasurementMismatch, ValueRejection.MEASUREMENT_MISMATCH),
    (SourceValueCoordinateUnresolved, ValueRejection.COORDINATE_UNRESOLVED),
    (SourceValueRepresentationDrift, ValueRejection.REPRESENTATION_DRIFT),
    (SourceValueWithheld, ValueRejection.VALUE_WITHHELD),
    (SourceValueReplayRejected, ValueRejection.REPLAY_REJECTED),
    (SourceValueVectorTooLarge, ValueRejection.VECTOR_TOO_LARGE),
)


def _project_fragment(
    policy: ResolvedProjectionPolicy,
    artifact: RegisteredFragmentSourceArtifact,
    manifest: CohortManifest,
    source: ProtectedSource,
) -> ProtectedValues:
    """Run the closed E07 adapter outside every store fence."""

    commitments = {
        "artifact_selector_id": artifact.selector_id,
        "artifact_object_sha256": artifact.object_sha256,
        "artifact_replay_sha256": artifact.artifact_replay_sha256,
    }
    rejection: ValueRejection | None = None
    projection: SourceValueProjectionSetV1 | None = None
    try:
        projection = _PINNED_FRAGMENT_PROJECT(
            policy,
            artifact.artifact,
            measurement_anchor=manifest.measurement_anchor,
            source_measurement=SourceMeasurementIdentity(
                method_ref=source.method_ref,
                method_definition_sha256=source.method_definition_sha256,
                quantity_id=source.quantity_id,
                unit=source.unit,
            ),
        )
    except SourceValueProjectionError as exc:
        rejection = next(
            (code for kind, code in _REJECTIONS if isinstance(exc, kind)),
            ValueRejection.INVALID,
        )
    except (ValidationError, ValueError, TypeError):
        rejection = ValueRejection.INVALID
    if projection is None:
        return ProtectedValues(
            state=ValueState.PROJECTION_REJECTED,
            rejection=rejection or ValueRejection.INVALID,
            **commitments,
        )
    return ProtectedValues(
        state=ValueState.PROJECTED, projection=projection, **commitments
    )


def _source_matches_measurement(
    source: RegisteredResultViewSource, measurement: LongitudinalMeasurementSelection
) -> bool:
    record = source.source.record
    method = record.method
    return (
        method.family == measurement.family
        and method.quantity_id == measurement.quantity_id
        and method.unit == measurement.unit
        and measurement_definition_sha256(
            method.method_ref,
            record.method_definition_sha256,
            method.quantity_id,
            method.unit,
        )
        == measurement.measurement_definition_sha256
    )


def _latest_versions(records: Iterable[Any]) -> dict[str, Any]:
    """Highest registered version per E06 selector (versions are append-only)."""

    chosen: dict[str, Any] = {}
    for item in records:
        known = chosen.get(item.selector_id)
        if known is None or item.source_version > known.source_version:
            chosen[item.selector_id] = item
    return chosen


def build_longitudinal_workspace(
    request: LongitudinalWorkspaceRequest,
    *,
    reader_authorization_registry: ReaderAuthorizationRegistry,
    reader_session_credential: ReaderGrantBinding,
    linkage_store: ProviderLinkageStore,
    cohort_registry: CohortRegistry,
    cohort_record_catalog: CohortRecordCatalog,
    result_catalog: ResultCatalog,
    result_trust_registry: ResultTrustRegistry,
    supersession_store: RecordSupersessionStore,
    anchor_policy_registry: AnchorPolicyRegistry,
    projection_policy_registry: ProjectionPolicyRegistry,
    result_view_source_registry: ResultViewSourceRegistry,
    measurement_source_artifact_registry: MeasurementSourceArtifactRegistry,
    d03_decision_registry: LongitudinalDecisionRegistry,
    d07_comparison_registry: RepeatabilityComparisonRegistry,
    d09_summary_registry: DenominatorPolicyRegistry,
    d10_context_registry: CovariateContextRegistry,
) -> LongitudinalWorkspace:
    """Build one D08 workspace from live authority only (see the module docstring).

    ``linkage_store``, ``result_catalog`` and ``result_trust_registry`` are not
    read directly: they complete the composite coordinator's exact store set
    (the plan's signature omits them).  E04 rows are consumed only through D06
    status bindings.
    """

    captured = _capture_request(request)
    credential = _capture_credential(reader_session_credential)
    measurement = captured.measurement
    scope = MeasurementScope(
        family=measurement.family,
        quantity_id=measurement.quantity_id,
        unit=measurement.unit,
    )
    # 1. The reader grant, before any protected selector or workspace read.
    first_authorization = _authorize(
        reader_authorization_registry,
        credential,
        sources=result_view_source_registry,
        scope=scope,
    )
    stores = (
        (reader_authorization_registry, ReaderAuthorizationRegistry),
        (linkage_store, ProviderLinkageStore),
        (cohort_registry, CohortRegistry),
        (cohort_record_catalog, CohortRecordCatalog),
        (result_catalog, ResultCatalog),
        (result_trust_registry, ResultTrustRegistry),
        (supersession_store, RecordSupersessionStore),
        (anchor_policy_registry, AnchorPolicyRegistry),
        (projection_policy_registry, ProjectionPolicyRegistry),
        (result_view_source_registry, ResultViewSourceRegistry),
        (measurement_source_artifact_registry, MeasurementSourceArtifactRegistry),
        (d03_decision_registry, LongitudinalDecisionRegistry),
        (d07_comparison_registry, RepeatabilityComparisonRegistry),
        (d09_summary_registry, DenominatorPolicyRegistry),
        (d10_context_registry, CovariateContextRegistry),
    )
    for store, cls in stores:
        _require_store(store, cls)
    coordinator: CompositeAuthorityCoordinator = _guarded(
        lambda: CompositeAuthorityCoordinator(
            linkage_store=linkage_store,
            record_history_store=supersession_store,
            cohort_registry=cohort_registry,
            reader_registry=reader_authorization_registry,
            record_catalog=cohort_record_catalog,
            result_catalog=result_catalog,
            result_trust_registry=result_trust_registry,
            source_registry=result_view_source_registry,
            decision_registry=d03_decision_registry,
            comparison_registry=d07_comparison_registry,
            d09_registry=d09_summary_registry,
            d10_registry=d10_context_registry,
            family_source_registry=measurement_source_artifact_registry,
            anchor_registry=anchor_policy_registry,
            projection_registry=projection_policy_registry,
        )
    )
    # 2. The bounded registry selection check, before any per-member traversal.
    selector_row = _selector_record(
        cohort_registry, captured.cohort_selector_id, captured.cohort_version
    )
    if selector_row.member_count > MAX_WORKSPACE_MEMBERS:
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.REDUCE_COHORT_TO_BOUND,
        )
    dependency_scope = SavedComparisonDependencyScopeV1(
        cohort_selector_id=captured.cohort_selector_id,
        cohort_version=captured.cohort_version,
    )
    first = _snapshot(coordinator, dependency_scope)
    heads = first.heads
    _require(
        _same_head(
            heads.reader_authorization,
            (
                first_authorization.registry_id,
                first_authorization.registry_epoch_sha256,
                first_authorization.state_head_sha256,
            ),
        )
    )
    failure: LongitudinalWorkspaceBoundaryError | None = None
    gathered = None
    try:
        gathered = _gather(
            captured,
            heads,
            first_authorization,
            selector_row,
            cohort_registry=cohort_registry,
            cohort_record_catalog=cohort_record_catalog,
            supersession_store=supersession_store,
            anchor_policy_registry=anchor_policy_registry,
            projection_policy_registry=projection_policy_registry,
            result_view_source_registry=result_view_source_registry,
            measurement_source_artifact_registry=measurement_source_artifact_registry,
            d03_decision_registry=d03_decision_registry,
            d07_comparison_registry=d07_comparison_registry,
            d09_summary_registry=d09_summary_registry,
            d10_context_registry=d10_context_registry,
        )
    except LongitudinalWorkspaceBoundaryError as exc:
        failure = exc
    if failure is not None:
        # A read failed: if any authority moved since the first snapshot, the
        # failure is a read race, never a stable scientific or stale state.
        moved = False
        if failure.code is not LongitudinalErrorCode.PERMISSION_DENIED:
            try:
                moved = _PINNED_SNAPSHOT(coordinator, dependency_scope) != first
            except Exception:
                moved = True
        code, remediation = (
            (LongitudinalErrorCode.READ_CONFLICT, LongitudinalRemediation.RETRY_READ)
            if moved
            else (failure.code, failure.remediation)
        )
        failure = None
        _fail(code, remediation)
    assert gathered is not None
    inputs, protected_rows, positions = gathered
    second: CompositeAuthoritySnapshotV1 | None = None
    second_code: LongitudinalErrorCode | None = None
    second_remediation = LongitudinalRemediation.RETRY_READ
    try:
        second = _snapshot(coordinator, dependency_scope)
    except LongitudinalWorkspaceBoundaryError as exc:
        # The first snapshot succeeded for this exact scope: a stale scope now
        # means authority moved during the build.
        second_code = (
            LongitudinalErrorCode.READ_CONFLICT
            if exc.code is LongitudinalErrorCode.AUTHORITY_STALE
            else exc.code
        )
        if second_code is not LongitudinalErrorCode.READ_CONFLICT:
            second_remediation = exc.remediation
    if second_code is not None:
        _fail(second_code, second_remediation)
    second_authorization = _authorize(
        reader_authorization_registry,
        credential,
        sources=result_view_source_registry,
        scope=scope,
    )
    _require(
        second == first
        and second_authorization.model_copy(
            update={"evaluated_at": first_authorization.evaluated_at}
        )
        == first_authorization
    )
    workspace: LongitudinalWorkspace | None = None
    try:
        workspace = derive_workspace(inputs, protected_rows, positions=positions)
    except (ValidationError, ValueError, TypeError, KeyError, AssertionError):
        workspace = None
    _require_integrity(workspace is not None)
    assert workspace is not None
    return workspace


def _gather(
    request: LongitudinalWorkspaceRequest,
    heads: SavedComparisonDependencyHeadsV1,
    authorization: ReaderAuthorization,
    selector_row: CohortSelectorRecord,
    *,
    cohort_registry: CohortRegistry,
    cohort_record_catalog: CohortRecordCatalog,
    supersession_store: RecordSupersessionStore,
    anchor_policy_registry: AnchorPolicyRegistry,
    projection_policy_registry: ProjectionPolicyRegistry,
    result_view_source_registry: ResultViewSourceRegistry,
    measurement_source_artifact_registry: MeasurementSourceArtifactRegistry,
    d03_decision_registry: LongitudinalDecisionRegistry,
    d07_comparison_registry: RepeatabilityComparisonRegistry,
    d09_summary_registry: DenominatorPolicyRegistry,
    d10_context_registry: CovariateContextRegistry,
) -> tuple[
    WorkspaceAuthorityInputs,
    tuple[ProtectedLongitudinalRow, ...],
    dict[tuple[int, str], tuple[int, int]],
]:
    """Every live read between the two composite snapshots, each head-bound."""

    selector_id = request.cohort_selector_id
    version = request.cohort_version
    measurement = request.measurement
    # D05: current registered history.
    history: RegisteredCohortHistory = _guarded(
        lambda: _call(
            cohort_registry, CohortRegistry, "resolve_history", selector_id, version
        ),
        remediation=LongitudinalRemediation.RESELECT_COHORT_VERSION,
    )
    _require(
        _same_head(
            heads.d05_cohort,
            (
                history.registry_id,
                history.registry_epoch_sha256,
                history.state_head_sha256,
            ),
        )
        and history.selected_manifest_sha256 == selector_row.manifest_sha256
        and history.registry_id == authorization.cohort_registry_id
    )
    manifest = history.manifests[-1]
    if len(manifest.members) > MAX_WORKSPACE_MEMBERS:
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.REDUCE_COHORT_TO_BOUND,
        )
    # Projection policy, bound to the D05 anchor and the requested D02 tuple.
    projection: ResolvedProjectionPolicy = _guarded(
        lambda: _call(
            projection_policy_registry,
            ProjectionPolicyRegistry,
            "resolve",
            request.projection_policy_selector_id,
            request.projection_policy_version,
        ),
        conflict=LongitudinalErrorCode.INVALID_REQUEST,
        remediation=LongitudinalRemediation.RESELECT_POLICY,
    )
    _require(
        _same_head(
            heads.projection_policy,
            (
                projection.registry_id,
                projection.registry_epoch_sha256,
                projection.state_head_sha256,
            ),
        )
    )
    policy = projection.policy
    if (
        policy.measurement_anchor != manifest.measurement_anchor
        or policy.measurement.method_definition.family != measurement.family
        or policy.measurement.quantity_id != measurement.quantity_id
        or policy.measurement.unit != measurement.unit
        or policy.measurement.measurement_definition_sha256
        != measurement.measurement_definition_sha256
    ):
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.RESELECT_POLICY,
        )
    # Anchor policy, D07 envelope and the explicitly selected anchor.
    anchor: ResolvedApprovedAnchor = _guarded(
        lambda: _call(
            anchor_policy_registry,
            AnchorPolicyRegistry,
            "resolve_anchor",
            request.anchor_policy_selector_id,
            request.anchor_policy_version,
            request.anchor_selector_id,
            expected_candidate_page_sha256=request.anchor_candidate_page_sha256,
        ),
        conflict=LongitudinalErrorCode.INVALID_REQUEST,
        remediation=LongitudinalRemediation.RESELECT_ANCHOR,
    )
    _require(
        _same_head(
            heads.anchor_policy,
            (
                anchor.registry_id,
                anchor.registry_epoch_sha256,
                anchor.state_head_sha256,
            ),
        )
    )
    anchor_key = anchor.anchor_record.comparison_key
    if (
        anchor.cohort_registry_id != history.registry_id
        or anchor.cohort_selector_id != selector_id
        or anchor.cohort_version != version
        or anchor.cohort_manifest_sha256 != history.selected_manifest_sha256
        or anchor.envelope.quantity_id != measurement.quantity_id
        or anchor.envelope.unit != measurement.unit
        or measurement_definition_sha256(
            anchor_key.method_ref,
            anchor_key.method_definition_sha256,
            anchor_key.quantity_id,
            anchor_key.unit,
        )
        != measurement.measurement_definition_sha256
    ):
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.RESELECT_ANCHOR,
        )
    # D06 status for the exact selected version.
    status: CohortManifestRecordStatus = _guarded(
        lambda: _call(
            cohort_record_catalog,
            CohortRecordCatalog,
            "record_status_for_manifest",
            selector_id,
            version,
        ),
        remediation=LongitudinalRemediation.RESELECT_COHORT_VERSION,
    )
    _require(
        _same_head(
            heads.d06_record_catalog,
            (status.registry_id, status.registry_epoch_sha256, status.status_sha256),
        )
        and status.registry_state_head_sha256 == history.state_head_sha256
        and status.cohort_manifest_sha256 == history.selected_manifest_sha256
        and status.linkage_state_head_sha256 == heads.d01_linkage.head
    )
    members = manifest.members
    member_digests = tuple(
        hashlib.sha256(canonical_contract_bytes(item)).hexdigest() for item in members
    )
    _require_integrity(
        len(status.members) == len(members)
        and all(
            item.member_sha256 == digest
            and item.provider_namespace == member.provider_namespace
            and item.analysis_record_id == member.analysis_record_id
            for item, member, digest in zip(
                status.members, members, member_digests, strict=True
            )
        )
    )
    # D09 population context (not a gate).
    d09: RegisteredDenominatorPolicySummary = _guarded(
        lambda: _call(
            d09_summary_registry,
            DenominatorPolicyRegistry,
            "resolve",
            request.d09_policy_selector_id,
            request.d09_policy_version,
        ),
        conflict=LongitudinalErrorCode.INVALID_REQUEST,
        remediation=LongitudinalRemediation.RESELECT_POLICY,
    )
    _require(
        _same_head(
            heads.d09_summary,
            (d09.registry_id, d09.registry_epoch_sha256, d09.state_head_sha256),
        )
    )
    summary = d09.summary
    if (
        summary.registry_id != history.registry_id
        or summary.selector_id != selector_id
        or summary.cohort_version != version
        or summary.cohort_manifest_sha256 != history.selected_manifest_sha256
    ):
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.RESELECT_POLICY,
        )
    population = summary.population
    _require(
        summary.record_status_sha256 == status.status_sha256
        and summary.registry_state_head_sha256 == history.state_head_sha256
    )
    _require_integrity(
        population.declared_members == len(members)
        and population.declared_denominator_units
        == sum(item.denominator_contribution for item in members)
    )
    # D04 bounded record history.
    history_entries = _history_map(supersession_store, heads)
    # E06 sources for available rows (privacy-safe page, then exact resolve).
    e06_pages = _e06_pages(result_view_source_registry, selector_id, version)
    for page in e06_pages:
        _require(
            _same_head(
                heads.e06_source,
                (page.registry_id, page.registry_epoch_sha256, page.state_head_sha256),
            )
        )
    e06_current = _latest_versions(item for page in e06_pages for item in page.records)
    family_pages = _scan_pages(
        lambda cursor: _guarded(
            lambda: _call(
                measurement_source_artifact_registry,
                MeasurementSourceArtifactRegistry,
                "list_selectors",
                selector_id,
                version,
                limit=100,
                **({} if cursor is None else {"after_selector_id": cursor}),
            )
        )
    )
    for page in family_pages:
        _require(
            _same_head(
                heads.family_source,
                (page.registry_id, page.registry_epoch_sha256, page.state_head_sha256),
            )
        )
    family_rows = {
        item.selector_id: item for page in family_pages for item in page.records
    }
    # D03 series for this exact anchor and policy.
    series_state, series = _resolve_series(d03_decision_registry, heads, anchor)
    decisions: dict[str, LongitudinalMemberDecision] = {}
    if series is not None:
        for item in series.decision.decisions:
            decisions[item.member_result_id] = item
    # D07 comparisons, matched by exact D03 decision, policy and envelope.
    comparisons = _comparison_index(d07_comparison_registry, heads, anchor)
    # D10 covariate context for this D09 population and D03 series.
    covariate = _covariate_context(
        d10_context_registry, heads, request, anchor, series, status, population
    )
    # Rows.
    anchor_record = anchor.anchor_record
    anchor_matches = [
        index
        for index, member in enumerate(members)
        if member.provider_namespace
        == anchor_record.linkage_revision.provider_namespace
        and member.linkage_id == anchor_record.linkage_revision.linkage_id
        and member.linkage_revision == anchor_record.linkage_revision.revision
    ]
    if len(anchor_matches) != 1:
        _fail(
            LongitudinalErrorCode.INVALID_REQUEST,
            LongitudinalRemediation.RESELECT_ANCHOR,
        )
    anchor_index = anchor_matches[0]
    family = policy.family
    prerequisite = {
        ProjectionFamily.CELL_ORIGIN: FamilyPrerequisite.E08_CELL_ORIGIN_ARTIFACT_BINDING,
        ProjectionFamily.CNA_CHROMOSOME: FamilyPrerequisite.E09_CNA_ARTIFACT_BINDING,
        ProjectionFamily.CNA_SEGMENT: FamilyPrerequisite.E09_CNA_ARTIFACT_BINDING,
    }.get(family)
    protected_rows: list[ProtectedLongitudinalRow] = []
    for index, (member, member_status, digest) in enumerate(
        zip(members, status.members, member_digests, strict=True)
    ):
        source_state = SourceState.RECORD_NOT_AVAILABLE
        source: ProtectedSource | None = None
        values = ProtectedValues(state=ValueState.SOURCE_UNAVAILABLE)
        binding = member_status.binding
        if binding is not None:
            source_state, source, values = _row_source(
                request,
                heads,
                manifest,
                projection,
                prerequisite,
                member,
                digest,
                binding,
                status,
                e06_current,
                family_rows,
                result_view_source_registry=result_view_source_registry,
                measurement_source_artifact_registry=measurement_source_artifact_registry,
            )
        is_anchor = index == anchor_index
        decision: ProtectedDecision | None = None
        if is_anchor:
            decision_state = DecisionState.ANCHOR
        elif series_state is not DecisionState.DECIDED:
            decision_state = series_state
        else:
            raw = (
                decisions.get(binding.result.result_id) if binding is not None else None
            )
            if raw is None:
                decision_state = DecisionState.NOT_IN_SERIES
            else:
                decision_state = DecisionState.DECIDED
                decision = _decision_fields(raw)
        comparison_state = ComparisonRegistryState.NOT_APPLICABLE
        comparison: ProtectedComparison | None = None
        if decision is not None:
            comparison_state, comparison = _row_comparison(
                d07_comparison_registry, heads, comparisons, decision.decision_sha256
            )
        protected_rows.append(
            ProtectedLongitudinalRow(
                row_ordinal=index + 1,
                member=member,
                member_sha256=digest,
                is_anchor=is_anchor,
                status=member_status,
                history=_history_for(member, member_status, history_entries),
                source_state=source_state,
                source=source,
                values=values,
                decision_state=decision_state,
                decision=decision,
                comparison_state=comparison_state,
                comparison=comparison,
            )
        )
    limitations = [
        LimitationCode.DESCRIPTIVE_ONLY,
        LimitationCode.D09_IS_POPULATION_CONTEXT_NOT_A_GATE,
        LimitationCode.E06_LEDGER_OPERATOR_ENTERED,
        LimitationCode.COVARIATE_TOKENS_OPERATOR_ENTERED,
        LimitationCode.E04_ROWS_ONLY_THROUGH_D06,
        LimitationCode.SINGLE_MEASUREMENT_SINGLE_ANCHOR,
        LimitationCode.METHOD_AUTHORITY_HEAD_NOT_CURRENT_VERIFIED,
    ]
    if covariate.state is not CovariateContextState.AVAILABLE:
        limitations.append(LimitationCode.COVARIATE_CONTEXT_UNAVAILABLE)
    if series_state is not DecisionState.DECIDED:
        limitations.append(LimitationCode.D03_SERIES_UNAVAILABLE)
    if prerequisite is not None:
        limitations.append(LimitationCode.STANDALONE_VALUES_UNAVAILABLE)
    authority = LongitudinalAuthorityCommitments(
        heads=public_dependency_heads(heads),
        cohort_selector_id=selector_id,
        cohort_version=version,
        cohort_manifest_sha256=history.selected_manifest_sha256,
        record_status_sha256=status.status_sha256,
        d09_selector_id=d09.selector_id,
        d09_policy_version=d09.policy_version,
        d09_summary_sha256=summary.summary_sha256,
        d09_population_sha256=population.population_sha256,
        anchor_policy_selector_id=anchor.policy_selector_id,
        anchor_policy_version=anchor.approval_version,
        anchor_policy_object_sha256=anchor.object_sha256,
        d03_policy_sha256=anchor.policy_sha256,
        d07_envelope_sha256=anchor.envelope_sha256,
        anchor_selector_id=anchor.candidate.anchor_selector_id,
        anchor_candidate_page_sha256=anchor.candidate_page_sha256,
        anchor_row_ordinal=anchor_index + 1,
        projection_selector_id=projection.selector_id,
        projection_policy_version=projection.policy_version,
        projection_policy_sha256=projection.policy_sha256,
        projection_family=family,
        d03_series_state=series_state,
        d03_series_decision_sha256=series.decision_sha256 if series else None,
        d10_context_state=covariate.state,
    )
    inputs = WorkspaceAuthorityInputs(
        request=request,
        reader_authorization=authorization,
        dependency_heads=heads,
        authority=authority,
        time_axis=_time_axis(manifest),
        population=population,
        covariate_context=covariate,
        version_diff=derive_version_diff(history.manifests),
        anchor_record_sha256=anchor.anchor_record_sha256,
        limitations=tuple(item for item in LimitationCode if item in set(limitations)),
    )
    return inputs, tuple(protected_rows), timepoint_positions(manifest)


def _e06_pages(
    registry: ResultViewSourceRegistry, selector_id: str, version: int
) -> list[Any]:
    pages = []
    cursor: tuple[str, int] | None = None
    for _ in range(MAX_SELECTOR_SCAN_PAGES):
        extra: dict[str, Any] = (
            {}
            if cursor is None
            else {"after_selector_id": cursor[0], "after_source_version": cursor[1]}
        )
        page = _guarded(
            lambda extra=extra: _call(
                registry,
                ResultViewSourceRegistry,
                "list_selectors",
                selector_id,
                version,
                limit=100,
                **extra,
            )
        )
        pages.append(page)
        if page.next_after_selector_id is None:
            return pages
        cursor = (page.next_after_selector_id, page.next_after_source_version)
    _fail(
        LongitudinalErrorCode.INTEGRITY_FAILURE,
        LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
    )


def _row_source(
    request: LongitudinalWorkspaceRequest,
    heads: SavedComparisonDependencyHeadsV1,
    manifest: CohortManifest,
    projection: ResolvedProjectionPolicy,
    prerequisite: FamilyPrerequisite | None,
    member: CohortMember,
    member_sha256: str,
    binding: Any,
    status: CohortManifestRecordStatus,
    e06_current: dict[str, Any],
    family_rows: dict[str, Any],
    *,
    result_view_source_registry: ResultViewSourceRegistry,
    measurement_source_artifact_registry: MeasurementSourceArtifactRegistry,
) -> tuple[SourceState, ProtectedSource | None, ProtectedValues]:
    selector = _guarded(
        lambda: _call(
            result_view_source_registry,
            ResultViewSourceRegistry,
            "selector_for_member",
            request.cohort_selector_id,
            request.cohort_version,
            member_sha256,
        )
    )
    listed = e06_current.get(selector)
    if (
        listed is not None
        and listed.authority_state is not SourceAuthorityState.CURRENT
    ):
        # This member's latest registered source no longer replays under the
        # stable snapshot: an authority failure, never a missing source.
        _fail(
            LongitudinalErrorCode.AUTHORITY_STALE,
            LongitudinalRemediation.REFRESH_SOURCE_REGISTRATION,
        )
    if listed is None:
        return (
            SourceState.NOT_REGISTERED,
            None,
            ProtectedValues(state=ValueState.SOURCE_UNAVAILABLE),
        )
    resolved: RegisteredResultViewSource = _guarded(
        lambda: _call(
            result_view_source_registry,
            ResultViewSourceRegistry,
            "resolve",
            selector,
            listed.source_version,
            expected_member_sha256=member_sha256,
            expected_result_id=binding.result.result_id,
        ),
        remediation=LongitudinalRemediation.REFRESH_SOURCE_REGISTRATION,
    )
    _require(
        _same_head(
            heads.e06_source,
            (
                resolved.registry_id,
                resolved.registry_epoch_sha256,
                resolved.state_head_sha256,
            ),
        )
        and resolved.record_status_sha256 == status.status_sha256
        and resolved.object_sha256 == listed.object_sha256
    )
    _require_integrity(
        resolved.member_sha256 == member_sha256
        and resolved.binding_sha256 == _contract_digest(binding)
        and resolved.catalog_result_sha256 == _contract_digest(binding.result)
        and resolved.cohort_selector_id == request.cohort_selector_id
        and resolved.cohort_version == request.cohort_version
    )
    if not _source_matches_measurement(resolved, request.measurement):
        return (
            SourceState.MEASUREMENT_MISMATCH,
            None,
            ProtectedValues(state=ValueState.SOURCE_UNAVAILABLE),
        )
    record = resolved.source.record
    source = ProtectedSource(
        selector_id=resolved.selector_id,
        source_version=resolved.source_version,
        object_sha256=resolved.object_sha256,
        source_sha256=resolved.source_sha256,
        source_replay_sha256=resolved.source_replay_sha256,
        denominator_ledger_sha256=resolved.denominator_ledger_sha256,
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
        quantity_id=record.method.quantity_id,
        unit=record.method.unit,
        execution_state=record.execution_state,
        information_state=record.information_state,
    )
    if prerequisite is not None:
        return (
            SourceState.VERIFIED,
            source,
            ProtectedValues(
                state=ValueState.FAMILY_PREREQUISITE_MISSING, prerequisite=prerequisite
            ),
        )
    artifact_selector = _guarded(
        lambda: _call(
            measurement_source_artifact_registry,
            MeasurementSourceArtifactRegistry,
            "selector_for_e06_source",
            resolved.selector_id,
            resolved.source_version,
        )
    )
    listed_artifact = family_rows.get(artifact_selector)
    if listed_artifact is None or listed_artifact.authority_state is not (
        ArtifactAuthorityState.CURRENT
    ):
        if listed_artifact is not None:
            # Registered for this exact source but no longer replaying under a
            # stable snapshot: authority failure, never a scientific state.
            _fail(
                LongitudinalErrorCode.AUTHORITY_STALE,
                LongitudinalRemediation.REFRESH_SOURCE_REGISTRATION,
            )
        return (
            SourceState.VERIFIED,
            source,
            ProtectedValues(state=ValueState.ARTIFACT_NOT_REGISTERED),
        )
    artifact: RegisteredFragmentSourceArtifact = _guarded(
        lambda: _call(
            measurement_source_artifact_registry,
            MeasurementSourceArtifactRegistry,
            "resolve",
            artifact_selector,
            expected_e06_selector_id=resolved.selector_id,
            expected_e06_source_version=resolved.source_version,
            expected_member_sha256=member_sha256,
            expected_result_id=binding.result.result_id,
        ),
        remediation=LongitudinalRemediation.REFRESH_SOURCE_REGISTRATION,
    )
    _require(
        _same_head(
            heads.family_source,
            (
                artifact.registry_id,
                artifact.registry_epoch_sha256,
                artifact.state_head_sha256,
            ),
        )
        and artifact.e06_state_head_sha256 == heads.e06_source.head
        and artifact.record_status_sha256 == status.status_sha256
        and artifact.object_sha256 == listed_artifact.object_sha256
        and artifact.e06_source_replay_sha256 == resolved.source_replay_sha256
    )
    if type(projection.policy) is not FragmentProjectionPolicyV1:
        _fail(
            LongitudinalErrorCode.INTEGRITY_FAILURE,
            LongitudinalRemediation.VERIFY_STORE_INTEGRITY,
        )
    return (
        SourceState.VERIFIED,
        source,
        _project_fragment(projection, artifact, manifest, source),
    )


def _resolve_series(
    registry: LongitudinalDecisionRegistry,
    heads: SavedComparisonDependencyHeadsV1,
    anchor: ResolvedApprovedAnchor,
) -> tuple[DecisionState, RegisteredLongitudinalSeriesDecision | None]:
    pages = _scan_pages(
        lambda cursor: _guarded(
            lambda: _call(
                registry,
                LongitudinalDecisionRegistry,
                "list_selectors",
                limit=100,
                **({} if cursor is None else {"after_selector_id": cursor}),
            )
        )
    )
    candidates = []
    for page in pages:
        _require(
            _same_head(
                heads.d03_decision,
                (page.registry_id, page.registry_epoch_sha256, page.state_head_sha256),
            )
        )
        candidates.extend(
            item
            for item in page.records
            if item.policy_sha256 == anchor.policy_sha256
            and item.anchor_key_sha256 == anchor.policy.anchor_key_sha256
        )
    matched: list[RegisteredLongitudinalSeriesDecision] = []
    stale = False
    for item in candidates:
        if item.authority_state is not SeriesAuthorityState.CURRENT:
            stale = True
            continue
        resolved: RegisteredLongitudinalSeriesDecision = _guarded(
            lambda item=item: _call(
                registry, LongitudinalDecisionRegistry, "resolve", item.selector_id
            )
        )
        _require(
            _same_head(
                heads.d03_decision,
                (
                    resolved.registry_id,
                    resolved.registry_epoch_sha256,
                    resolved.state_head_sha256,
                ),
            )
        )
        if (
            resolved.decision.anchor_record_sha256 == anchor.anchor_record_sha256
            and resolved.decision.policy_sha256 == anchor.policy_sha256
        ):
            matched.append(resolved)
    if len(matched) == 1:
        return DecisionState.DECIDED, matched[0]
    if len(matched) > 1:
        return DecisionState.SERIES_AMBIGUOUS, None
    if stale:
        # A not-current series for this policy and anchor key cannot be proven
        # unrelated to this anchor: fail closed rather than show "not decided".
        _fail(
            LongitudinalErrorCode.AUTHORITY_STALE,
            LongitudinalRemediation.REFRESH_DECISION_REGISTRATION,
        )
    return DecisionState.SERIES_NOT_REGISTERED, None


def _comparison_index(
    registry: RepeatabilityComparisonRegistry,
    heads: SavedComparisonDependencyHeadsV1,
    anchor: ResolvedApprovedAnchor,
) -> dict[str, list[Any]]:
    """D07 selector rows for this policy and the resolved anchor envelope only."""

    pages = _scan_pages(
        lambda cursor: _guarded(
            lambda: _call(
                registry,
                RepeatabilityComparisonRegistry,
                "list_selectors",
                limit=100,
                **({} if cursor is None else {"after_selector_id": cursor}),
            )
        )
    )
    index: dict[str, list[Any]] = {}
    for page in pages:
        _require(
            _same_head(
                heads.d07_comparison,
                (page.registry_id, page.registry_epoch_sha256, page.state_head_sha256),
            )
            and page.result_trust_state_head_sha256 == heads.result_trust.head
        )
        for item in page.records:
            if (
                item.anchor_policy_sha256 == anchor.policy_sha256
                and item.repeatability_envelope_sha256 == anchor.envelope_sha256
            ):
                index.setdefault(item.d03_decision_sha256, []).append(item)
    return index


def _row_comparison(
    registry: RepeatabilityComparisonRegistry,
    heads: SavedComparisonDependencyHeadsV1,
    index: dict[str, list[Any]],
    decision_sha256: str,
) -> tuple[ComparisonRegistryState, ProtectedComparison | None]:
    rows = index.get(decision_sha256, [])
    if not rows:
        return ComparisonRegistryState.NOT_REGISTERED, None
    if len(rows) > 1:
        return ComparisonRegistryState.AMBIGUOUS, None
    row = rows[0]
    if row.authority_state is not ComparisonAuthorityState.CURRENT:
        return ComparisonRegistryState.STALE, None
    resolved: RegisteredRepeatabilityComparison = _guarded(
        lambda: _call(
            registry, RepeatabilityComparisonRegistry, "resolve", row.selector_id
        )
    )
    _require(
        _same_head(
            heads.d07_comparison,
            (
                resolved.registry_id,
                resolved.registry_epoch_sha256,
                resolved.state_head_sha256,
            ),
        )
        and resolved.result_trust_state_head_sha256 == heads.result_trust.head
        and resolved.object_sha256 == row.object_sha256
    )
    fields = _comparison_fields(resolved)
    if fields.availability is ComparisonAvailability.AVAILABLE:
        return ComparisonRegistryState.AVAILABLE, fields
    return ComparisonRegistryState.UNAVAILABLE, fields


def _covariate_context(
    registry: CovariateContextRegistry,
    heads: SavedComparisonDependencyHeadsV1,
    request: LongitudinalWorkspaceRequest,
    anchor: ResolvedApprovedAnchor,
    series: RegisteredLongitudinalSeriesDecision | None,
    status: CohortManifestRecordStatus,
    population: CohortPopulationProjection,
) -> PublicCovariateContext:
    if series is None:
        return PublicCovariateContext(state=CovariateContextState.NOT_APPLICABLE)
    pages = _scan_pages(
        lambda cursor: _guarded(
            lambda: _call(
                registry,
                CovariateContextRegistry,
                "list_selectors",
                limit=100,
                **({} if cursor is None else {"after_selector_id": cursor}),
            )
        )
    )
    matched: list[RegisteredLiveCovariateContext] = []
    stale = False
    for page in pages:
        _require(
            _same_head(
                heads.d10_context,
                (page.registry_id, page.registry_epoch_sha256, page.state_head_sha256),
            )
        )
        for item in page.records:
            if item.authority_state is not ContextAuthorityState.CURRENT:
                stale = True
                continue
            resolved: RegisteredLiveCovariateContext = _guarded(
                lambda item=item: _call(
                    registry, CovariateContextRegistry, "resolve", item.selector_id
                )
            )
            _require(
                _same_head(
                    heads.d10_context,
                    (
                        resolved.registry_id,
                        resolved.registry_epoch_sha256,
                        resolved.state_head_sha256,
                    ),
                )
            )
            live = resolved.live
            if (
                live.d09_population.selector_id == request.d09_policy_selector_id
                and live.d09_population.policy_version == request.d09_policy_version
                and live.d03_series.selector_id == series.selector_id
                and live.context.d02_anchor_policy_sha256 == anchor.policy_sha256
            ):
                # The context must derive from this build's exact D09
                # population, D06 status and D03 series object.
                _require(
                    live.d09_population.record_status_sha256 == status.status_sha256
                    and live.d09_population.population_sha256
                    == population.population_sha256
                    and live.d03_series.object_sha256 == series.object_sha256
                )
                matched.append(resolved)
    if len(matched) > 1:
        return PublicCovariateContext(state=CovariateContextState.AMBIGUOUS)
    if not matched:
        return PublicCovariateContext(
            state=(
                CovariateContextState.STALE
                if stale
                else CovariateContextState.NOT_REGISTERED
            )
        )
    aggregate = project_aggregate_covariate_summary(matched[0].live.context)
    return PublicCovariateContext(
        state=CovariateContextState.AVAILABLE,
        context_sha256=aggregate.protected_context_sha256,
        classification=aggregate.classification,
        reason_codes=aggregate.reason_codes,
        included_member_count=aggregate.included_member_count,
        groups=aggregate.groups,
    )


__all__ = [
    "LIMITATION_STATEMENT",
    "MAX_WORKSPACE_MEMBERS",
    "OPERATOR_ENTERED_LABEL",
    "ComparisonRegistryState",
    "ComparisonState",
    "CovariateContextState",
    "DecisionState",
    "FamilyPrerequisite",
    "HistoryState",
    "LimitationCode",
    "LongitudinalAuthorityCommitments",
    "LongitudinalErrorCode",
    "LongitudinalMeasurementSelection",
    "LongitudinalRemediation",
    "LongitudinalSegment",
    "LongitudinalSourceRow",
    "LongitudinalVersionDiff",
    "LongitudinalWorkspace",
    "LongitudinalWorkspaceBoundaryError",
    "LongitudinalWorkspaceFilters",
    "LongitudinalWorkspaceProjection",
    "LongitudinalWorkspaceRequest",
    "ProtectedComparison",
    "ProtectedDecision",
    "ProtectedHistory",
    "ProtectedLongitudinalRow",
    "ProtectedSource",
    "ProtectedValues",
    "PublicComparison",
    "PublicCovariateContext",
    "PublicFragmentValue",
    "PublicTimeAxis",
    "RowCompatibilityState",
    "SourceState",
    "SuppressionReason",
    "TimeCoordinateSemantics",
    "ValueRejection",
    "ValueState",
    "VersionDiffKind",
    "VersionDiffReason",
    "WorkspaceAuthorityInputs",
    "build_longitudinal_workspace",
    "comparison_suppression",
    "derive_segments",
    "derive_version_diff",
    "derive_workspace",
    "filter_rows",
    "filters_sha256",
    "longitudinal_projection_bytes",
    "member_set_sha256",
    "normalize_filters",
    "project_longitudinal_workspace",
    "timepoint_positions",
]
