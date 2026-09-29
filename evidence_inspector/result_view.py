"""Closed, framework-independent result filtering and view-state contracts.

The E06 boundary is presentation data, not a UI implementation. It preserves
independent execution, information, trust, qualification, and display-role
axes; binds every visible row to exact E05 and E01 identities; and carries the
same denominator and attrition ledger through filtering without inventing
zeroes for missing or withheld values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from evidence_inspector.compatibility import (
    BoundMeasurementIdentity,
    BundleId,
    CompatibilityContract,
    CompatibilityDecision,
    CompatibilityOutcome,
    ExecutionState,
    InformationState,
    ResultId,
    TrustState,
    VerifiedMeasurementRecord,
    canonical_compatibility_bytes,
    compatibility_key_sha256,
)
from evidence_inspector.method_registry import (
    CurrentMethodCapability,
    DisplayRole,
    MethodReference,
    QualificationState,
    Sha256,
    canonical_contract_bytes,
)

MAX_VIEW_SOURCES = 64
MAX_FILTER_IDENTITIES = 64
MAX_ATTRITION_REASONS = 32
MAX_LABEL_LENGTH = 160

_RESERVED_LABEL_PREFIXES = (
    "donor",
    "filename",
    "path",
    "patient",
    "read",
    "sample",
    "sequence",
)
_SAFE_RESERVED_PREFIX_LEXEMES = {
    "pathology",
    "readiness",
    "readout",
    "ready",
    "runner",
    "runtime",
    "sampled",
    "sequencer",
}


def _reject_reserved_privacy_terms(value: str, *, field: str) -> str:
    for segment in re.split(r"[^a-z0-9]+", value.lower()):
        if segment in _SAFE_RESERVED_PREFIX_LEXEMES:
            continue
        if any(segment.startswith(prefix) for prefix in _RESERVED_LABEL_PREFIXES):
            raise ValueError(f"{field} contains a reserved privacy term")
    return value


def _safe_label(value: str) -> str:
    if "/" in value or "\\" in value or "://" in value:
        raise ValueError("accessible label cannot contain a path or URI")
    return _reject_reserved_privacy_terms(value, field="accessible label")


def _safe_controlled_token(value: str) -> str:
    return _reject_reserved_privacy_terms(value, field="controlled identifier")


AccessibleLabel = Annotated[
    str,
    StringConstraints(min_length=2, max_length=MAX_LABEL_LENGTH),
    AfterValidator(_safe_label),
]
ViewId = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=96,
        pattern=r"^(filter|fixture)_[a-z0-9]+(?:_[a-z0-9]+)*$",
    ),
    AfterValidator(_safe_controlled_token),
]
ReasonCode = Annotated[
    str,
    StringConstraints(
        min_length=4,
        max_length=64,
        pattern=r"^reason_[a-z0-9]+(?:_[a-z0-9]+)*$",
    ),
    AfterValidator(_safe_controlled_token),
]


class CountState(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    WITHHELD = "withheld"


class CountValue(CompatibilityContract):
    """One count whose unavailable states cannot masquerade as numeric zero."""

    state: CountState
    value: int | None = Field(default=None, ge=0, le=10**15)
    accessible_label: AccessibleLabel

    @model_validator(mode="after")
    def coherent_value(self) -> CountValue:
        if self.state == CountState.OBSERVED and self.value is None:
            raise ValueError("observed count requires a numeric value")
        if self.state != CountState.OBSERVED and self.value is not None:
            raise ValueError("missing or withheld count cannot contain a value")
        return self


class AttritionStage(StrEnum):
    ACCEPTANCE = "acceptance"
    ELIGIBILITY = "eligibility"
    DISPLAY = "display"


class AttritionReason(CompatibilityContract):
    stage: AttritionStage
    reason_code: ReasonCode
    accessible_label: AccessibleLabel
    count: CountValue

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.stage.value, self.reason_code


class DenominatorLedger(CompatibilityContract):
    """Visible denominator stages plus exact, reconciled attrition reasons."""

    schema_version: Literal["traceback.denominator-ledger.v1"] = (
        "traceback.denominator-ledger.v1"
    )
    input_records: CountValue
    accepted_records: CountValue
    eligible_records: CountValue
    displayed_records: CountValue
    attrition: tuple[AttritionReason, ...] = Field(
        min_length=3, max_length=MAX_ATTRITION_REASONS
    )

    @model_validator(mode="after")
    def reconcile_stages(self) -> DenominatorLedger:
        keys = [item.sort_key for item in self.attrition]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("attrition reasons must be uniquely sorted")
        stage_pairs = (
            (
                AttritionStage.ACCEPTANCE,
                self.input_records,
                self.accepted_records,
            ),
            (
                AttritionStage.ELIGIBILITY,
                self.accepted_records,
                self.eligible_records,
            ),
            (
                AttritionStage.DISPLAY,
                self.eligible_records,
                self.displayed_records,
            ),
        )
        for stage, before, after in stage_pairs:
            reasons = [item for item in self.attrition if item.stage == stage]
            if not reasons:
                raise ValueError("every denominator stage requires an attrition reason")
            if before.state == after.state == CountState.OBSERVED:
                if any(item.count.state != CountState.OBSERVED for item in reasons):
                    raise ValueError(
                        "observed denominator stages require observed attrition"
                    )
                assert before.value is not None and after.value is not None
                if after.value > before.value:
                    raise ValueError("denominator stages cannot increase")
                if (
                    sum(item.count.value or 0 for item in reasons)
                    != before.value - after.value
                ):
                    raise ValueError("attrition does not reconcile denominator stage")
            elif any(item.count.state == CountState.OBSERVED for item in reasons):
                raise ValueError(
                    "unavailable denominator stages cannot expose observed attrition"
                )
        return self


class QualificationAxis(StrEnum):
    UNKNOWN = "unknown"
    DEVELOPMENT_UNQUALIFIED = "development_unqualified"
    QUALIFIED = "qualified"
    NOT_ASSIGNED = "not_assigned"


class DisplayRoleAxis(StrEnum):
    PROVIDER_PRIMARY = "provider_primary"
    RESEARCH_BASELINE = "research_baseline"
    RESEARCH_CHALLENGER = "research_challenger"
    DISABLED = "disabled"
    NOT_ASSIGNED = "not_assigned"


class ViewSurfaceState(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    LOADING = "loading"
    ERROR = "error"


class MethodFilterIdentity(CompatibilityContract):
    method_ref: MethodReference
    method_definition_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (
            self.method_ref.method_id,
            self.method_ref.version,
            self.method_definition_sha256,
        )


class AuthorityFilterIdentity(CompatibilityContract):
    registry_sha256: Sha256
    registry_version: int = Field(ge=1, le=10_000_000)
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0, le=10_000_000)
    capability_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, int, str, int, str]:
        return (
            self.registry_sha256,
            self.registry_version,
            self.authority_head_sha256,
            self.authority_revision,
            self.capability_sha256,
        )


class ResultFilterIdentity(CompatibilityContract):
    result_id: ResultId
    result_sha256: Sha256
    bundle_id: BundleId
    bundle_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.result_id,
            self.result_sha256,
            self.bundle_id,
            self.bundle_sha256,
        )


class CompatibilityFilterIdentity(CompatibilityContract):
    decision_sha256: Sha256
    outcome: CompatibilityOutcome
    policy_sha256: Sha256
    authority_head_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.decision_sha256,
            self.outcome.value,
            self.policy_sha256,
            self.authority_head_sha256,
        )


def _capability_sha256(capability: CurrentMethodCapability) -> str:
    return hashlib.sha256(canonical_contract_bytes(capability)).hexdigest()


def _qualification_axis(
    value: QualificationState | None,
) -> QualificationAxis:
    if value is None:
        return QualificationAxis.NOT_ASSIGNED
    return QualificationAxis(value.value)


def _display_role_axis(value: DisplayRole | None) -> DisplayRoleAxis:
    if value is None:
        return DisplayRoleAxis.NOT_ASSIGNED
    return DisplayRoleAxis(value.value)


def _decision_binding_for(
    record: VerifiedMeasurementRecord, decision: CompatibilityDecision
) -> BoundMeasurementIdentity:
    matches = [
        item
        for item in (decision.binding.left, decision.binding.right)
        if item.result_id == record.result_id
    ]
    if len(matches) != 1:
        raise ValueError("compatibility decision does not bind exact result")
    return matches[0]


def _method_identity(record: VerifiedMeasurementRecord) -> MethodFilterIdentity:
    return MethodFilterIdentity(
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
    )


def _authority_identity(
    record: VerifiedMeasurementRecord,
) -> AuthorityFilterIdentity:
    capability = record.current_capability
    return AuthorityFilterIdentity(
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        capability_sha256=_capability_sha256(capability),
    )


def _result_identity(record: VerifiedMeasurementRecord) -> ResultFilterIdentity:
    return ResultFilterIdentity(
        result_id=record.result_id,
        result_sha256=record.result_sha256,
        bundle_id=record.bundle_id,
        bundle_sha256=record.bundle_sha256,
    )


def _compatibility_identity(
    decision: CompatibilityDecision,
) -> CompatibilityFilterIdentity:
    return CompatibilityFilterIdentity(
        decision_sha256=decision.decision_sha256,
        outcome=decision.outcome,
        policy_sha256=decision.binding.policy_sha256,
        authority_head_sha256=decision.binding.authority_head_sha256,
    )


class ResultViewSource(CompatibilityContract):
    """One displayable aggregate bound to exact compatibility and authority."""

    schema_version: Literal["traceback.result-view-source.v1"] = (
        "traceback.result-view-source.v1"
    )
    record: VerifiedMeasurementRecord
    compatibility_decision: CompatibilityDecision
    compatibility_identity: CompatibilityFilterIdentity
    method_identity: MethodFilterIdentity
    authority_identity: AuthorityFilterIdentity
    result_identity: ResultFilterIdentity
    denominator: DenominatorLedger
    accessible_label: AccessibleLabel
    qc_label: AccessibleLabel

    @model_validator(mode="after")
    def bind_exact_identities(self) -> ResultViewSource:
        bound = _decision_binding_for(self.record, self.compatibility_decision)
        expected_bound = BoundMeasurementIdentity(
            result_id=self.record.result_id,
            result_sha256=self.record.result_sha256,
            bundle_id=self.record.bundle_id,
            bundle_sha256=self.record.bundle_sha256,
            method_ref=self.record.method.method_ref,
            method_definition_sha256=self.record.method_definition_sha256,
            capability_sha256=_capability_sha256(self.record.current_capability),
            registry_sha256=self.record.current_capability.registry_sha256,
            registry_version=self.record.current_capability.registry_version,
            authority_head_sha256=(
                self.record.current_capability.authority_head_sha256
            ),
            authority_revision=self.record.current_capability.authority_revision,
            compatibility_key_sha256=compatibility_key_sha256(
                self.record.compatibility_key
            ),
            execution_state=self.record.execution_state,
            information_state=self.record.information_state,
            trust_state=self.record.trust_state,
        )
        if bound != expected_bound:
            raise ValueError("compatibility binding does not match exact record")
        if self.compatibility_identity != _compatibility_identity(
            self.compatibility_decision
        ):
            raise ValueError("compatibility filter identity does not match decision")
        if self.method_identity != _method_identity(self.record):
            raise ValueError("method filter identity does not match record")
        if self.authority_identity != _authority_identity(self.record):
            raise ValueError("authority filter identity does not match record")
        if self.result_identity != _result_identity(self.record):
            raise ValueError("result filter identity does not match record")
        if self.record.execution_state == ExecutionState.NOT_RUN:
            denominator_counts = (
                self.denominator.input_records,
                self.denominator.accepted_records,
                self.denominator.eligible_records,
                self.denominator.displayed_records,
                *(reason.count for reason in self.denominator.attrition),
            )
            if any(count.state != CountState.MISSING for count in denominator_counts):
                raise ValueError(
                    "not-run result requires missing denominator and attrition counts"
                )
        return self


def bind_result_view_source(
    *,
    record: VerifiedMeasurementRecord,
    compatibility_decision: CompatibilityDecision,
    denominator: DenominatorLedger,
    accessible_label: str,
    qc_label: str,
) -> ResultViewSource:
    return ResultViewSource(
        record=record,
        compatibility_decision=compatibility_decision,
        compatibility_identity=_compatibility_identity(compatibility_decision),
        method_identity=_method_identity(record),
        authority_identity=_authority_identity(record),
        result_identity=_result_identity(record),
        denominator=denominator,
        accessible_label=accessible_label,
        qc_label=qc_label,
    )


class NormalizedResultFilters(CompatibilityContract):
    schema_version: Literal["traceback.normalized-result-filters.v1"] = (
        "traceback.normalized-result-filters.v1"
    )
    execution_states: tuple[ExecutionState, ...] = Field(min_length=1)
    information_states: tuple[InformationState, ...] = Field(min_length=1)
    trust_states: tuple[TrustState, ...] = Field(min_length=1)
    qualification_states: tuple[QualificationAxis, ...] = Field(min_length=1)
    display_roles: tuple[DisplayRoleAxis, ...] = Field(min_length=1)
    compatibility_outcomes: tuple[CompatibilityOutcome, ...] = Field(min_length=1)
    compatibility_identities: tuple[CompatibilityFilterIdentity, ...] = Field(
        default=(), max_length=MAX_FILTER_IDENTITIES
    )
    method_identities: tuple[MethodFilterIdentity, ...] = Field(
        default=(), max_length=MAX_FILTER_IDENTITIES
    )
    authority_identities: tuple[AuthorityFilterIdentity, ...] = Field(
        default=(), max_length=MAX_FILTER_IDENTITIES
    )
    result_identities: tuple[ResultFilterIdentity, ...] = Field(
        default=(), max_length=MAX_FILTER_IDENTITIES
    )

    @model_validator(mode="after")
    def canonical_order(self) -> NormalizedResultFilters:
        enum_fields = (
            self.execution_states,
            self.information_states,
            self.trust_states,
            self.qualification_states,
            self.display_roles,
            self.compatibility_outcomes,
        )
        if any(values != tuple(sorted(set(values), key=str)) for values in enum_fields):
            raise ValueError("filter state axes must be uniquely sorted")
        identity_fields = (
            self.compatibility_identities,
            self.method_identities,
            self.authority_identities,
            self.result_identities,
        )
        for values in identity_fields:
            keys = [item.sort_key for item in values]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError("filter identities must be uniquely sorted")
        return self


EnumT = TypeVar("EnumT", bound=StrEnum)
IdentityT = TypeVar("IdentityT")


def _normalized_enum_values(
    values: Iterable[EnumT] | None, enum_type: type[EnumT]
) -> tuple[EnumT, ...]:
    selected = tuple(enum_type) if values is None else tuple(values)
    return tuple(sorted(set(selected), key=str))


def _normalized_identities(
    values: Iterable[IdentityT] | None,
) -> tuple[IdentityT, ...]:
    selected = () if values is None else tuple(values)
    return tuple(sorted(set(selected), key=lambda item: item.sort_key))  # type: ignore[attr-defined]


def normalize_result_filters(
    *,
    execution_states: Iterable[ExecutionState] | None = None,
    information_states: Iterable[InformationState] | None = None,
    trust_states: Iterable[TrustState] | None = None,
    qualification_states: Iterable[QualificationAxis] | None = None,
    display_roles: Iterable[DisplayRoleAxis] | None = None,
    compatibility_outcomes: Iterable[CompatibilityOutcome] | None = None,
    compatibility_identities: Iterable[CompatibilityFilterIdentity] | None = None,
    method_identities: Iterable[MethodFilterIdentity] | None = None,
    authority_identities: Iterable[AuthorityFilterIdentity] | None = None,
    result_identities: Iterable[ResultFilterIdentity] | None = None,
) -> NormalizedResultFilters:
    """Deduplicate and sort filters; empty state axes remain invalid."""

    return NormalizedResultFilters(
        execution_states=_normalized_enum_values(execution_states, ExecutionState),
        information_states=_normalized_enum_values(
            information_states, InformationState
        ),
        trust_states=_normalized_enum_values(trust_states, TrustState),
        qualification_states=_normalized_enum_values(
            qualification_states, QualificationAxis
        ),
        display_roles=_normalized_enum_values(display_roles, DisplayRoleAxis),
        compatibility_outcomes=_normalized_enum_values(
            compatibility_outcomes, CompatibilityOutcome
        ),
        compatibility_identities=_normalized_identities(compatibility_identities),
        method_identities=_normalized_identities(method_identities),
        authority_identities=_normalized_identities(authority_identities),
        result_identities=_normalized_identities(result_identities),
    )


def result_filters_sha256(filters: NormalizedResultFilters) -> str:
    return hashlib.sha256(canonical_compatibility_bytes(filters)).hexdigest()


class ResultViewRequest(CompatibilityContract):
    schema_version: Literal["traceback.result-view-request.v1"] = (
        "traceback.result-view-request.v1"
    )
    filter_id: ViewId
    sources: tuple[ResultViewSource, ...] = Field(
        min_length=1, max_length=MAX_VIEW_SOURCES
    )
    filters: NormalizedResultFilters

    @model_validator(mode="after")
    def deterministic_sources_and_filters(self) -> ResultViewRequest:
        source_ids = [item.result_identity.result_id for item in self.sources]
        if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
            raise ValueError("view sources must use uniquely sorted result IDs")
        bindings = (
            (self.filters.compatibility_identities, "compatibility_identity"),
            (self.filters.method_identities, "method_identity"),
            (self.filters.authority_identities, "authority_identity"),
            (self.filters.result_identities, "result_identity"),
        )
        for requested, attribute in bindings:
            available = {getattr(source, attribute) for source in self.sources}
            if any(item not in available for item in requested):
                raise ValueError("exact filter identity is not bound by a source")
        return self


def result_view_request_sha256(request: ResultViewRequest) -> str:
    return hashlib.sha256(canonical_compatibility_bytes(request)).hexdigest()


class ResultViewRow(CompatibilityContract):
    compatibility_identity: CompatibilityFilterIdentity
    method_identity: MethodFilterIdentity
    authority_identity: AuthorityFilterIdentity
    result_identity: ResultFilterIdentity
    execution_state: ExecutionState
    information_state: InformationState
    trust_state: TrustState
    qualification_state: QualificationAxis
    display_role: DisplayRoleAxis
    accessible_label: AccessibleLabel
    qc_label: AccessibleLabel
    denominator: DenominatorLedger


class ResultView(CompatibilityContract):
    schema_version: Literal["traceback.result-view.v1"] = "traceback.result-view.v1"
    filter_id: ViewId
    normalized_filters: NormalizedResultFilters
    filters_sha256: Sha256
    request_sha256: Sha256
    surface_state: Literal[ViewSurfaceState.READY, ViewSurfaceState.EMPTY]
    source_count: int = Field(ge=1, le=MAX_VIEW_SOURCES)
    visible_count: int = Field(ge=0, le=MAX_VIEW_SOURCES)
    excluded_count: int = Field(ge=0, le=MAX_VIEW_SOURCES)
    rows: tuple[ResultViewRow, ...] = Field(max_length=MAX_VIEW_SOURCES)
    replay_sha256: Sha256

    @model_validator(mode="after")
    def coherent_view(self) -> ResultView:
        if self.filters_sha256 != result_filters_sha256(self.normalized_filters):
            raise ValueError("filters digest does not match normalized filters")
        if self.visible_count != len(self.rows):
            raise ValueError("visible count does not match view rows")
        if self.source_count != self.visible_count + self.excluded_count:
            raise ValueError("view counts do not reconcile")
        expected_state = ViewSurfaceState.READY if self.rows else ViewSurfaceState.EMPTY
        if self.surface_state != expected_state:
            raise ValueError("surface state does not match visible rows")
        row_ids = [item.result_identity.result_id for item in self.rows]
        if row_ids != sorted(row_ids) or len(row_ids) != len(set(row_ids)):
            raise ValueError("view rows must use uniquely sorted result IDs")
        if self.replay_sha256 != _model_digest(self, exclude={"replay_sha256"}):
            raise ValueError("view replay digest does not match canonical view")
        return self


ExactT = TypeVar("ExactT")


def _matches_exact(value: ExactT, allowed: tuple[ExactT, ...]) -> bool:
    return not allowed or value in allowed


def _source_matches(source: ResultViewSource, filters: NormalizedResultFilters) -> bool:
    record = source.record
    return all(
        (
            record.execution_state in filters.execution_states,
            record.information_state in filters.information_states,
            record.trust_state in filters.trust_states,
            _qualification_axis(record.current_capability.qualification_state)
            in filters.qualification_states,
            _display_role_axis(record.current_capability.display_role)
            in filters.display_roles,
            source.compatibility_decision.outcome in filters.compatibility_outcomes,
            _matches_exact(
                source.compatibility_identity,
                filters.compatibility_identities,
            ),
            _matches_exact(source.method_identity, filters.method_identities),
            _matches_exact(source.authority_identity, filters.authority_identities),
            _matches_exact(source.result_identity, filters.result_identities),
        )
    )


def _row(source: ResultViewSource) -> ResultViewRow:
    record = source.record
    return ResultViewRow(
        compatibility_identity=source.compatibility_identity,
        method_identity=source.method_identity,
        authority_identity=source.authority_identity,
        result_identity=source.result_identity,
        execution_state=record.execution_state,
        information_state=record.information_state,
        trust_state=record.trust_state,
        qualification_state=_qualification_axis(
            record.current_capability.qualification_state
        ),
        display_role=_display_role_axis(record.current_capability.display_role),
        accessible_label=source.accessible_label,
        qc_label=source.qc_label,
        denominator=source.denominator,
    )


def _model_digest(
    contract: CompatibilityContract, *, exclude: set[str] | None = None
) -> str:
    payload = contract.model_dump(mode="json", exclude=exclude or set())
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_result_view(request: ResultViewRequest) -> ResultView:
    rows = tuple(
        _row(source)
        for source in request.sources
        if _source_matches(source, request.filters)
    )
    payload: dict[str, Any] = {
        "schema_version": "traceback.result-view.v1",
        "filter_id": request.filter_id,
        "normalized_filters": request.filters,
        "filters_sha256": result_filters_sha256(request.filters),
        "request_sha256": result_view_request_sha256(request),
        "surface_state": ViewSurfaceState.READY if rows else ViewSurfaceState.EMPTY,
        "source_count": len(request.sources),
        "visible_count": len(rows),
        "excluded_count": len(request.sources) - len(rows),
        "rows": rows,
    }
    placeholder = ResultView.model_construct(**payload, replay_sha256="0" * 64)
    return ResultView(
        **payload,
        replay_sha256=_model_digest(placeholder, exclude={"replay_sha256"}),
    )


class ResultViewReplayError(ValueError):
    """A canonical result view could not be replayed exactly."""


def replay_result_view(request: ResultViewRequest, expected: ResultView) -> ResultView:
    actual = build_result_view(request)
    if actual != expected:
        raise ResultViewReplayError("result view does not replay exactly")
    return actual


class SurfaceErrorCode(StrEnum):
    LOAD_FAILED = "load_failed"
    INVALID_FIXTURE = "invalid_fixture"


class ResultViewFixture(CompatibilityContract):
    """Synthetic UI fixture state with accessible, non-color-only labeling."""

    schema_version: Literal["traceback.result-view-fixture.v1"] = (
        "traceback.result-view-fixture.v1"
    )
    fixture_id: ViewId
    surface_state: ViewSurfaceState
    accessible_label: AccessibleLabel
    view: ResultView | None = None
    error_code: SurfaceErrorCode | None = None
    error_message: AccessibleLabel | None = None

    @model_validator(mode="after")
    def coherent_fixture(self) -> ResultViewFixture:
        if self.surface_state in (ViewSurfaceState.READY, ViewSurfaceState.EMPTY):
            if self.view is None or self.view.surface_state != self.surface_state:
                raise ValueError("ready or empty fixture requires matching view")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("ready or empty fixture cannot contain an error")
        elif self.surface_state == ViewSurfaceState.LOADING:
            if (
                self.view is not None
                or self.error_code is not None
                or self.error_message is not None
            ):
                raise ValueError("loading fixture cannot contain result or error")
        elif self.surface_state == ViewSurfaceState.ERROR:
            if (
                self.view is not None
                or self.error_code is None
                or self.error_message is None
            ):
                raise ValueError("error fixture requires safe error code and message")
        return self


def build_synthetic_surface_fixtures(
    *, ready_view: ResultView, empty_view: ResultView
) -> tuple[ResultViewFixture, ...]:
    """Return deterministic loading, empty, ready, and error UI fixtures."""

    if ready_view.surface_state != ViewSurfaceState.READY:
        raise ValueError("ready_view must contain visible rows")
    if empty_view.surface_state != ViewSurfaceState.EMPTY:
        raise ValueError("empty_view must contain no visible rows")
    return (
        ResultViewFixture(
            fixture_id="fixture_error",
            surface_state=ViewSurfaceState.ERROR,
            accessible_label="Result view error",
            error_code=SurfaceErrorCode.LOAD_FAILED,
            error_message="Unable to load the result view. Retry locally.",
        ),
        ResultViewFixture(
            fixture_id="fixture_loading",
            surface_state=ViewSurfaceState.LOADING,
            accessible_label="Loading result view",
        ),
        ResultViewFixture(
            fixture_id="fixture_empty",
            surface_state=ViewSurfaceState.EMPTY,
            accessible_label="No results match the active filters",
            view=empty_view,
        ),
        ResultViewFixture(
            fixture_id="fixture_ready",
            surface_state=ViewSurfaceState.READY,
            accessible_label="Filtered results ready",
            view=ready_view,
        ),
    )


ResultViewT = TypeVar("ResultViewT", bound=CompatibilityContract)


def canonical_result_view_bytes(contract: CompatibilityContract) -> bytes:
    return canonical_compatibility_bytes(contract)


def result_view_contract_from_canonical_bytes(
    model: type[ResultViewT], content: bytes
) -> ResultViewT:
    try:
        contract = model.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ResultViewReplayError("result view contract JSON is invalid") from exc
    if canonical_result_view_bytes(contract) != content:
        raise ResultViewReplayError("result view contract JSON is not canonical")
    return contract


__all__ = [
    "AccessibleLabel",
    "AttritionReason",
    "AttritionStage",
    "AuthorityFilterIdentity",
    "CompatibilityFilterIdentity",
    "CountState",
    "CountValue",
    "DenominatorLedger",
    "DisplayRoleAxis",
    "MAX_FILTER_IDENTITIES",
    "MAX_VIEW_SOURCES",
    "MethodFilterIdentity",
    "NormalizedResultFilters",
    "QualificationAxis",
    "ResultFilterIdentity",
    "ResultView",
    "ResultViewFixture",
    "ResultViewReplayError",
    "ResultViewRequest",
    "ResultViewRow",
    "ResultViewSource",
    "SurfaceErrorCode",
    "ViewSurfaceState",
    "bind_result_view_source",
    "build_result_view",
    "build_synthetic_surface_fixtures",
    "canonical_result_view_bytes",
    "normalize_result_filters",
    "replay_result_view",
    "result_filters_sha256",
    "result_view_contract_from_canonical_bytes",
    "result_view_request_sha256",
]
