"""Adversarial offline tests for E06 result filters and view states."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityPolicyReference,
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    MeasurementCompatibilityPolicy,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
    decide_compatibility,
)
from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    DisplayRole,
    MethodDefinition,
    MethodFamily,
    QualificationState,
    ToolReference,
    method_definition_sha256,
)
from evidence_inspector.result_view import (
    MAX_FILTER_IDENTITIES,
    MAX_VIEW_SOURCES,
    AttritionReason,
    AttritionStage,
    CountState,
    CountValue,
    DenominatorLedger,
    DisplayRoleAxis,
    QualificationAxis,
    ResultFilterIdentity,
    ResultView,
    ResultViewReplayError,
    ResultViewRequest,
    ResultViewSource,
    ViewSurfaceState,
    bind_result_view_source,
    build_result_view,
    build_synthetic_surface_fixtures,
    canonical_result_view_bytes,
    normalize_result_filters,
    replay_result_view,
    result_view_contract_from_canonical_bytes,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
HEAD_SHA256 = "a" * 64
REGISTRY_SHA256 = "b" * 64


def _asset() -> AssetReference:
    return AssetReference(
        asset_id="asset_reference_alpha",
        version="1.0.0",
        content_sha256="1" * 64,
    )


def _method() -> MethodDefinition:
    return MethodDefinition(
        method_id="mth_fragment_alpha",
        version="1.0.0",
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id="qty_fragment_fraction",
        unit="unit_fraction",
        parameter_schema_sha256="2" * 64,
        tools=(
            ToolReference(
                tool_id="tool_measurement_alpha",
                version="1.0.0",
                artifact_sha256="3" * 64,
            ),
        ),
        assets=(_asset(),),
    )


def _capability(
    method: MethodDefinition,
    *,
    qualification: QualificationState | None,
    display_role: DisplayRole | None,
    authority_revision: int = 4,
) -> CurrentMethodCapability:
    provider_eligible = (
        qualification == QualificationState.QUALIFIED
        and display_role == DisplayRole.PROVIDER_PRIMARY
    )
    return CurrentMethodCapability(
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=HEAD_SHA256,
        authority_revision=authority_revision,
        method_definition_sha256=method_definition_sha256(method),
        method_ref=method.method_ref,
        authority_scope="scope_research_alpha",
        as_of=NOW,
        qualification_state=qualification,
        display_role=display_role,
        research_inspectable=True,
        current_provider_eligible=provider_eligible,
        effective_approval_ref=(
            "approval_provider_alpha" if provider_eligible else None
        ),
    )


def _record(
    suffix: str,
    digest_digit: str,
    *,
    execution: ExecutionState = ExecutionState.COMPLETE,
    information: InformationState = InformationState.SUFFICIENT,
    trust: TrustState = TrustState.VERIFIED,
    qualification: QualificationState | None = (
        QualificationState.DEVELOPMENT_UNQUALIFIED
    ),
    display_role: DisplayRole | None = DisplayRole.RESEARCH_BASELINE,
) -> VerifiedMeasurementRecord:
    method = _method()
    policy_ref = CompatibilityPolicyReference(
        policy_id="policy_longitudinal_alpha", version="1.0.0"
    )
    key = MeasurementCompatibilityKey(
        measurement_family=method.family,
        quantity_id=method.quantity_id,
        unit=method.unit,
        result_schema=ResultSchemaReference(
            schema_id="schema_measurement_alpha", version="1.0.0"
        ),
        reference_asset=_asset(),
        grid_asset=_asset(),
        atlas_asset=_asset(),
        panel_asset=_asset(),
        normalization_semantics_id="sem_normalization_alpha",
        coordinate_semantics_id="sem_coordinate_alpha",
        denominator_semantics_id="sem_denominator_alpha",
        registered_policy=policy_ref,
    )
    return VerifiedMeasurementRecord(
        result_id=f"result_{suffix}",
        result_sha256=digest_digit * 64,
        bundle_id=f"bundle_{suffix}",
        bundle_sha256=hex((int(digest_digit, 16) + 1) % 16)[2:] * 64,
        method=method,
        method_definition_sha256=method_definition_sha256(method),
        current_capability=_capability(
            method,
            qualification=qualification,
            display_role=display_role,
        ),
        execution_state=execution,
        information_state=information,
        trust_state=trust,
        compatibility_key=key,
    )


def _policy(*records: VerifiedMeasurementRecord) -> CompatibilityPolicy:
    method = records[0].method
    rule = MeasurementCompatibilityPolicy(
        measurement_family=method.family,
        quantity_id=method.quantity_id,
        unit=method.unit,
        allowed_method_definitions=(
            AllowedMethodDefinition(
                method_ref=method.method_ref,
                method_definition_sha256=method_definition_sha256(method),
            ),
        ),
        allowed_result_schemas=(records[0].compatibility_key.result_schema,),
        delta_allowed_when_comparable=True,
        shared_axis_allowed_when_comparable=True,
    )
    return CompatibilityPolicy(
        policy_id="policy_longitudinal_alpha",
        version="1.0.0",
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=HEAD_SHA256,
        authority_revision=4,
        measurement_policies=(rule,),
    )


def _decision(anchor: VerifiedMeasurementRecord, peer: VerifiedMeasurementRecord):
    policy = _policy(anchor, peer)
    return decide_compatibility(
        CompatibilityRequest(
            left=anchor,
            right=peer,
            policy=policy,
            trusted_policy_sha256=compatibility_policy_sha256(policy),
            trusted_authority_head_sha256=HEAD_SHA256,
        )
    )


def _observed(value: int, label: str) -> CountValue:
    return CountValue(
        state=CountState.OBSERVED,
        value=value,
        accessible_label=label,
    )


def _missing(label: str) -> CountValue:
    return CountValue(
        state=CountState.MISSING,
        accessible_label=label,
    )


def _ledger(*, available: bool = True, partial: bool = False) -> DenominatorLedger:
    values = (
        (
            _observed(100, "Input records"),
            _observed(90, "Accepted records"),
            _missing("Eligible count missing")
            if partial
            else _observed(80, "Eligible records"),
            _missing("Displayed count missing")
            if partial
            else _observed(75, "Displayed records"),
        )
        if available
        else (
            _missing("Input count missing"),
            _missing("Accepted count missing"),
            _missing("Eligible count missing"),
            _missing("Displayed count missing"),
        )
    )
    attrition_counts = (
        (
            _observed(10, "Acceptance exclusions"),
            _missing("Eligibility exclusions missing")
            if partial
            else _observed(10, "Eligibility exclusions"),
            _missing("Display exclusions missing")
            if partial
            else _observed(5, "Display exclusions"),
        )
        if available
        else (
            _missing("Acceptance exclusions missing"),
            _missing("Eligibility exclusions missing"),
            _missing("Display exclusions missing"),
        )
    )
    return DenominatorLedger(
        input_records=values[0],
        accepted_records=values[1],
        eligible_records=values[2],
        displayed_records=values[3],
        attrition=(
            AttritionReason(
                stage=AttritionStage.ACCEPTANCE,
                reason_code="reason_policy_exclusion",
                accessible_label="Policy exclusion",
                count=attrition_counts[0],
            ),
            AttritionReason(
                stage=AttritionStage.DISPLAY,
                reason_code="reason_display_exclusion",
                accessible_label="Outside display range",
                count=attrition_counts[2],
            ),
            AttritionReason(
                stage=AttritionStage.ELIGIBILITY,
                reason_code="reason_measurement_ineligible",
                accessible_label="Measurement ineligible",
                count=attrition_counts[1],
            ),
        ),
    )


def _source(
    anchor: VerifiedMeasurementRecord,
    record: VerifiedMeasurementRecord,
) -> ResultViewSource:
    return bind_result_view_source(
        record=record,
        compatibility_decision=_decision(anchor, record),
        denominator=(
            _ledger()
            if record.execution_state == ExecutionState.COMPLETE
            else _ledger(
                available=record.execution_state == ExecutionState.FAILED,
                partial=record.execution_state == ExecutionState.FAILED,
            )
        ),
        accessible_label=f"Synthetic aggregate {record.result_id.removeprefix('result_')}",
        qc_label=f"Execution {record.execution_state.value.replace('_', ' ')}",
    )


@pytest.fixture
def sources() -> tuple[ResultViewSource, ...]:
    anchor = _record("anchor", "1")
    records = (
        _record("alpha", "2"),
        _record(
            "beta",
            "3",
            execution=ExecutionState.FAILED,
            display_role=DisplayRole.RESEARCH_CHALLENGER,
        ),
        _record(
            "delta",
            "4",
            information=InformationState.INSUFFICIENT,
            trust=TrustState.REVOKED,
            qualification=QualificationState.QUALIFIED,
            display_role=DisplayRole.PROVIDER_PRIMARY,
        ),
        _record(
            "gamma",
            "5",
            execution=ExecutionState.NOT_RUN,
            information=InformationState.INSUFFICIENT,
            trust=TrustState.UNVERIFIED,
            qualification=None,
            display_role=None,
        ),
    )
    return tuple(_source(anchor, record) for record in records)


def _request(
    sources: tuple[ResultViewSource, ...],
    **filter_overrides: object,
) -> ResultViewRequest:
    return ResultViewRequest(
        filter_id="filter_synthetic_matrix",
        sources=sources,
        filters=normalize_result_filters(**filter_overrides),
    )


def test_unfiltered_view_preserves_all_independent_axes_and_ledgers(
    sources: tuple[ResultViewSource, ...],
) -> None:
    view = build_result_view(_request(sources))

    assert view.surface_state == ViewSurfaceState.READY
    assert view.visible_count == 4
    assert {row.execution_state for row in view.rows} == {
        ExecutionState.COMPLETE,
        ExecutionState.FAILED,
        ExecutionState.NOT_RUN,
    }
    assert {row.information_state for row in view.rows} == {
        InformationState.SUFFICIENT,
        InformationState.INSUFFICIENT,
    }
    assert {row.trust_state for row in view.rows} == {
        TrustState.VERIFIED,
        TrustState.REVOKED,
        TrustState.UNVERIFIED,
    }
    assert view.rows[0].denominator == sources[0].denominator


@pytest.mark.parametrize(
    ("filters", "expected"),
    (
        ({"execution_states": (ExecutionState.FAILED,)}, ("result_beta",)),
        ({"execution_states": (ExecutionState.NOT_RUN,)}, ("result_gamma",)),
        (
            {"information_states": (InformationState.INSUFFICIENT,)},
            ("result_delta", "result_gamma"),
        ),
        ({"trust_states": (TrustState.REVOKED,)}, ("result_delta",)),
        ({"qualification_states": (QualificationAxis.QUALIFIED,)}, ("result_delta",)),
        ({"display_roles": (DisplayRoleAxis.PROVIDER_PRIMARY,)}, ("result_delta",)),
    ),
)
def test_each_status_axis_filters_without_inference(
    sources: tuple[ResultViewSource, ...],
    filters: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    view = build_result_view(_request(sources, **filters))

    assert tuple(row.result_identity.result_id for row in view.rows) == expected


def test_cross_axis_filter_can_be_empty_without_converting_to_zero(
    sources: tuple[ResultViewSource, ...],
) -> None:
    view = build_result_view(
        _request(
            sources,
            execution_states=(ExecutionState.FAILED,),
            trust_states=(TrustState.REVOKED,),
        )
    )

    assert view.surface_state == ViewSurfaceState.EMPTY
    assert view.visible_count == 0
    assert view.rows == ()


def test_failed_filter_preserves_partial_visible_denominator(
    sources: tuple[ResultViewSource, ...],
) -> None:
    view = build_result_view(
        _request(sources, execution_states=(ExecutionState.FAILED,))
    )

    ledger = view.rows[0].denominator
    assert ledger.input_records.value == 100
    assert ledger.accepted_records.value == 90
    assert ledger.eligible_records.state == CountState.MISSING
    assert ledger.eligible_records.value is None


def test_exact_identity_filters_select_only_bound_source(
    sources: tuple[ResultViewSource, ...],
) -> None:
    target = sources[2]
    view = build_result_view(
        _request(
            sources,
            compatibility_identities=(target.compatibility_identity,),
            method_identities=(target.method_identity,),
            authority_identities=(target.authority_identity,),
            result_identities=(target.result_identity,),
        )
    )

    assert view.visible_count == 1
    assert view.rows[0].result_identity == target.result_identity
    assert view.rows[0].compatibility_identity == target.compatibility_identity


def test_exact_filter_identity_must_be_bound_by_source(
    sources: tuple[ResultViewSource, ...],
) -> None:
    foreign = sources[0].result_identity.model_copy(
        update={"result_id": "result_foreign"}
    )

    with pytest.raises(ValidationError, match="not bound by a source"):
        _request(sources, result_identities=(foreign,))


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("result_identity", "result_sha256"),
        ("method_identity", "method_definition_sha256"),
        ("authority_identity", "capability_sha256"),
        ("compatibility_identity", "decision_sha256"),
    ),
)
def test_source_rejects_tampered_exact_bindings(
    sources: tuple[ResultViewSource, ...],
    field: str,
    replacement: str,
) -> None:
    payload = sources[0].model_dump(mode="json")
    payload[field][replacement] = "f" * 64

    with pytest.raises(ValidationError, match="does not match"):
        ResultViewSource.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "state_update",
    (
        {"execution_state": ExecutionState.NOT_RUN},
        {"information_state": InformationState.INSUFFICIENT},
        {"trust_state": TrustState.REVOKED},
    ),
)
def test_source_rejects_stale_comparable_decision_after_state_change(
    state_update: dict[str, object],
) -> None:
    anchor = _record("stale_anchor", "6")
    original = _record("stale_peer", "7")
    comparable = _decision(anchor, original)
    assert comparable.outcome == CompatibilityOutcome.COMPARABLE
    changed = original.model_copy(update=state_update)
    denominator = (
        _ledger(available=False)
        if changed.execution_state == ExecutionState.NOT_RUN
        else _ledger()
    )

    with pytest.raises(ValidationError, match="compatibility decision is stale"):
        bind_result_view_source(
            record=changed,
            compatibility_decision=comparable,
            denominator=denominator,
            accessible_label="Synthetic aggregate stale peer",
            qc_label="State changed after decision",
        )


def test_missing_and_withheld_counts_cannot_carry_zero() -> None:
    for state in (CountState.MISSING, CountState.WITHHELD):
        with pytest.raises(ValidationError, match="cannot contain a value"):
            CountValue(
                state=state,
                value=0,
                accessible_label="Count unavailable",
            )


def test_not_run_rejects_fully_observed_denominator() -> None:
    anchor = _record("notrun_anchor", "8")
    record = _record(
        "notrun_peer",
        "9",
        execution=ExecutionState.NOT_RUN,
    )

    with pytest.raises(ValidationError, match="not-run result requires missing"):
        bind_result_view_source(
            record=record,
            compatibility_decision=_decision(anchor, record),
            denominator=_ledger(),
            accessible_label="Synthetic aggregate not run",
            qc_label="Execution not run",
        )


def test_not_run_with_missing_denominator_remains_visible_state() -> None:
    anchor = _record("visible_anchor", "a")
    record = _record(
        "visible_peer",
        "b",
        execution=ExecutionState.NOT_RUN,
    )
    source = _source(anchor, record)
    view = build_result_view(
        _request((source,), execution_states=(ExecutionState.NOT_RUN,))
    )

    assert view.surface_state == ViewSurfaceState.READY
    assert view.visible_count == 1
    assert view.rows[0].denominator.input_records.state == CountState.MISSING
    assert view.rows[0].denominator.input_records.value is None


def test_observed_denominator_and_attrition_must_reconcile() -> None:
    payload = _ledger().model_dump(mode="json")
    payload["displayed_records"]["value"] = 74

    with pytest.raises(ValidationError, match="does not reconcile"):
        DenominatorLedger.model_validate_json(json.dumps(payload))


def test_normalization_deduplicates_and_sorts_filters() -> None:
    filters = normalize_result_filters(
        execution_states=(
            ExecutionState.NOT_RUN,
            ExecutionState.COMPLETE,
            ExecutionState.NOT_RUN,
        )
    )

    assert filters.execution_states == (
        ExecutionState.COMPLETE,
        ExecutionState.NOT_RUN,
    )


def test_empty_state_axis_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        normalize_result_filters(execution_states=())


def test_replay_digest_binds_filters_rows_and_denominators(
    sources: tuple[ResultViewSource, ...],
) -> None:
    request = _request(sources, trust_states=(TrustState.VERIFIED,))
    view = build_result_view(request)

    assert replay_result_view(request, view) == view
    tampered = view.model_copy(update={"excluded_count": 0})
    with pytest.raises(ResultViewReplayError, match="does not replay"):
        replay_result_view(request, tampered)


def test_canonical_round_trip_and_normalization_drift_rejection(
    sources: tuple[ResultViewSource, ...],
) -> None:
    view = build_result_view(_request(sources))
    content = canonical_result_view_bytes(view)

    assert result_view_contract_from_canonical_bytes(ResultView, content) == view
    with pytest.raises(ResultViewReplayError, match="not canonical"):
        result_view_contract_from_canonical_bytes(ResultView, content + b"\n")


def test_surface_fixtures_cover_ready_empty_loading_and_error(
    sources: tuple[ResultViewSource, ...],
) -> None:
    ready = build_result_view(_request(sources))
    empty = build_result_view(
        _request(
            sources,
            execution_states=(ExecutionState.FAILED,),
            trust_states=(TrustState.REVOKED,),
        )
    )

    fixtures = build_synthetic_surface_fixtures(ready_view=ready, empty_view=empty)

    assert tuple(item.surface_state for item in fixtures) == (
        ViewSurfaceState.ERROR,
        ViewSurfaceState.LOADING,
        ViewSurfaceState.EMPTY,
        ViewSurfaceState.READY,
    )
    assert all(item.accessible_label for item in fixtures)


@pytest.mark.parametrize(
    "label",
    (
        "Donor alpha",
        "Donor_12345",
        "Patient result",
        "patient007",
        "Sample identifier",
        "sample_abc",
        "Read ID 42",
        "read_id_42",
        "sequence42",
        "filename_backup",
        "donorABC123",
        "patientMRN42",
        "sampleABC123",
        "readABC123",
        "sequenceABC123",
        "filenameABC123",
        "file:///private/result",
        "local\\private\\result",
    ),
)
def test_accessible_labels_reject_private_identifiers_and_paths(
    label: str,
) -> None:
    with pytest.raises(ValidationError, match="privacy term|path or URI"):
        CountValue(
            state=CountState.MISSING,
            accessible_label=label,
        )


@pytest.mark.parametrize(
    "label",
    (
        "Ready results",
        "Reader status",
        "Reading complete",
        "Readable summary",
        "Readiness status",
        "Readout summary",
        "Pathway aggregate",
        "Pathology marker aggregate",
        "Sampled aggregate",
        "Sequencer status",
    ),
)
def test_accessible_labels_preserve_documented_safe_vocabulary(label: str) -> None:
    value = CountValue(state=CountState.MISSING, accessible_label=label)

    assert value.accessible_label == label


def test_exact_result_id_rejects_reserved_private_stem() -> None:
    with pytest.raises(ValidationError, match="privacy term"):
        ResultFilterIdentity(
            result_id="result_donor_alpha",
            result_sha256="1" * 64,
            bundle_id="bundle_alpha",
            bundle_sha256="2" * 64,
        )


def test_filter_id_rejects_reserved_private_stem(
    sources: tuple[ResultViewSource, ...],
) -> None:
    with pytest.raises(ValidationError, match="privacy term"):
        ResultViewRequest(
            filter_id="filter_donor_alpha",
            sources=sources,
            filters=normalize_result_filters(),
        )


def test_contracts_reject_unknown_fields(sources: tuple[ResultViewSource, ...]) -> None:
    payload = build_result_view(_request(sources)).model_dump(mode="json")
    payload["automatic_primary"] = True

    with pytest.raises(ValidationError, match="Extra inputs"):
        ResultView.model_validate_json(json.dumps(payload))


def test_source_and_filter_bounds_fail_closed(
    sources: tuple[ResultViewSource, ...],
) -> None:
    with pytest.raises(ValidationError, match=f"at most {MAX_VIEW_SOURCES}"):
        ResultViewRequest(
            filter_id="filter_too_many",
            sources=tuple(sources[0] for _ in range(MAX_VIEW_SOURCES + 1)),
            filters=normalize_result_filters(),
        )

    identities = tuple(
        ResultFilterIdentity(
            result_id=f"result_bound_{index:02d}",
            result_sha256=f"{index % 16:x}" * 64,
            bundle_id=f"bundle_bound_{index:02d}",
            bundle_sha256=f"{(index + 1) % 16:x}" * 64,
        )
        for index in range(MAX_FILTER_IDENTITIES + 1)
    )
    with pytest.raises(ValidationError, match=f"at most {MAX_FILTER_IDENTITIES}"):
        normalize_result_filters(result_identities=identities)


def test_qualification_and_display_role_do_not_override_revoked_trust(
    sources: tuple[ResultViewSource, ...],
) -> None:
    row = build_result_view(_request(sources, trust_states=(TrustState.REVOKED,))).rows[
        0
    ]

    assert row.trust_state == TrustState.REVOKED
    assert row.qualification_state == QualificationAxis.QUALIFIED
    assert row.display_role == DisplayRoleAxis.PROVIDER_PRIMARY
    assert row.compatibility_identity.outcome == CompatibilityOutcome.UNKNOWN


def test_provider_primary_is_not_automatically_sorted_first(
    sources: tuple[ResultViewSource, ...],
) -> None:
    view = build_result_view(_request(sources))

    assert tuple(row.result_identity.result_id for row in view.rows) == (
        "result_alpha",
        "result_beta",
        "result_delta",
        "result_gamma",
    )
    assert view.rows[2].display_role == DisplayRoleAxis.PROVIDER_PRIMARY
