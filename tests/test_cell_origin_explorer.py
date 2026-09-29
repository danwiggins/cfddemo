"""Synthetic and adversarial tests for the E08 cell-origin explorer."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from traceback_runner.serialization import canonical_json_bytes

from evidence_inspector.cell_origin_explorer import (
    AXIS_LABEL,
    CellOriginExplorerArtifact,
    CellOriginExplorerError,
    CellOriginExplorerRequest,
    ExplorerStatus,
    LimitationId,
    build_cell_origin_explorer,
    build_cell_origin_explorer_artifact,
    canonical_cell_origin_explorer_bytes,
    cell_origin_explorer_from_canonical_bytes,
)
from evidence_inspector.cell_origin_models import CellOriginResult
from evidence_inspector.cell_origin_pipeline import (
    CellOriginResultBundle,
    ChartData,
    CompositionChartRow,
    ResourceSummary,
)
from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
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
    AttritionReason,
    AttritionStage,
    CountState,
    CountValue,
    DenominatorLedger,
    ResultViewRequest,
    bind_result_view_source,
    normalize_result_filters,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cell_origin" / "result.json"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
ATLAS_SHA = "a" * 64
HEAD_SHA = "b" * 64
REGISTRY_SHA = "c" * 64


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _bundle(*, unavailable_second_interval: bool = False) -> CellOriginResultBundle:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["result_id"] = "result_cell_origin_alpha"
    payload["deconvolution"]["atlas_id"] = "asset_atlas_alpha"
    payload["deconvolution"]["schema_version"] = "cell-origin-deconvolution.v2"
    payload["deconvolution"]["atlas_sha256"] = ATLAS_SHA
    payload["deconvolution"]["diagnostics"].update(
        {
            "row_scale": "reference_count",
            "solver_tolerance": 1e-12,
            "max_iterations": 10000,
            "solver_implementation_id": "traceback.active-set-nnls.v1",
        }
    )
    payload["bootstrap"]["schema_version"] = "cell-origin-bootstrap.v2"
    payload["bootstrap"]["information_status"] = (
        "partial_information" if unavailable_second_interval else "available"
    )
    for index, interval in enumerate(payload["bootstrap"]["intervals"]):
        interval["information_status"] = (
            "insufficient_information"
            if unavailable_second_interval and index == 1
            else "available"
        )
        if unavailable_second_interval and index == 1:
            interval["lower_fraction"] = None
            interval["upper_fraction"] = None
    payload["bootstrap"]["diagnostics"] = {
        "schema_version": "cell-origin-bootstrap-diagnostics.v2",
        "requested_resamples": 100,
        "successful_resamples": 100,
        "failed_resamples": 0,
        "degenerate_resamples": 0,
        "resampling_unit": "classified_fragment_call_within_marker",
        "method_id": "independent-marker-binomial-bootstrap.v1",
        "preserves_cross_marker_molecule_linkage": False,
        "limitation_id": "cross-marker-molecule-linkage-not-preserved",
        "minimum_tail_observations": 2,
        "tail_probability": 0.025000000000000022,
        "minimum_successful_resamples": 80,
        "maximum_unusable_resample_fraction": 0.0,
        "observed_unusable_resample_fraction": 0.0,
        "interval_eligibility_met": True,
        "nnls_row_scale": "reference_count",
        "solver_tolerance": 1e-12,
        "max_iterations": 10000,
        "solver_implementation_id": "traceback.active-set-nnls.v1",
    }
    result = CellOriginResult.model_validate_json(json.dumps(payload))
    interval_by_id = {
        item.cell_type_id: item for item in result.bootstrap.intervals  # type: ignore[union-attr]
    }
    estimates = sorted(
        result.deconvolution.estimates,
        key=lambda item: (-item.fraction, item.cell_type_id),
    )
    rows = []
    for rank, estimate in enumerate(estimates, 1):
        interval = interval_by_id[estimate.cell_type_id]
        rows.append(
            CompositionChartRow(
                cell_type_id=estimate.cell_type_id,
                label=f"Presentation label {rank}",
                rank=rank,
                fraction=estimate.fraction,
                percent=estimate.fraction * 100,
                lower_fraction=interval.lower_fraction,
                upper_fraction=interval.upper_fraction,
                lower_percent=(
                    interval.lower_fraction * 100
                    if interval.lower_fraction is not None
                    else None
                ),
                upper_percent=(
                    interval.upper_fraction * 100
                    if interval.upper_fraction is not None
                    else None
                ),
                uncertainty_available=interval.lower_fraction is not None,
                uncertainty_status=interval.information_status,
                color="#112233",
                show_by_default=True,
            )
        )
    return CellOriginResultBundle(
        result=result,
        charts=ChartData(
            composition_rows=tuple(rows),
            composition_title="Synthetic composition",
        ),
        resources=ResourceSummary(
            registered_markers=2,
            usable_markers=2,
            excluded_incomplete_atlas_markers=0,
            collapsed_duplicate_regions=0,
            cell_type_count=2,
        ),
        notices=("Synthetic-only fixture.",),
    )


def _method() -> MethodDefinition:
    return MethodDefinition(
        method_id="mth_cell_origin_alpha",
        version="1.0.0",
        family=MethodFamily.CELL_ORIGIN,
        quantity_id="qty_cell_origin_fraction",
        unit="unit_fraction",
        parameter_schema_sha256="d" * 64,
        tools=(
            ToolReference(
                tool_id="tool_cell_origin_alpha",
                version="1.0.0",
                artifact_sha256="e" * 64,
            ),
        ),
        assets=(
            AssetReference(
                asset_id="asset_atlas_alpha",
                version="1.0.0",
                content_sha256=ATLAS_SHA,
            ),
        ),
    )


def _record(
    bundle: CellOriginResultBundle,
    *,
    suffix: str = "alpha",
    execution: ExecutionState = ExecutionState.COMPLETE,
    information: InformationState = InformationState.SUFFICIENT,
    trust: TrustState = TrustState.VERIFIED,
) -> VerifiedMeasurementRecord:
    method = _method()
    atlas = method.assets[0]
    capability = CurrentMethodCapability(
        registry_sha256=REGISTRY_SHA,
        registry_version=2,
        authority_head_sha256=HEAD_SHA,
        authority_revision=4,
        method_definition_sha256=method_definition_sha256(method),
        method_ref=method.method_ref,
        authority_scope="scope_research_alpha",
        as_of=NOW,
        qualification_state=QualificationState.DEVELOPMENT_UNQUALIFIED,
        display_role=DisplayRole.RESEARCH_BASELINE,
        research_inspectable=True,
        current_provider_eligible=False,
        effective_approval_ref=None,
    )
    return VerifiedMeasurementRecord(
        result_id=(bundle.result.result_id if suffix == "alpha" else f"result_peer_{suffix}"),
        result_sha256=(_digest(bundle.result) if suffix == "alpha" else "1" * 64),
        bundle_id=f"bundle_cell_origin_{suffix}",
        bundle_sha256=(_digest(bundle) if suffix == "alpha" else "2" * 64),
        method=method,
        method_definition_sha256=method_definition_sha256(method),
        current_capability=capability,
        execution_state=execution,
        information_state=information,
        trust_state=trust,
        compatibility_key=MeasurementCompatibilityKey(
            measurement_family=method.family,
            quantity_id=method.quantity_id,
            unit=method.unit,
            result_schema=ResultSchemaReference(
                schema_id="schema_cell_origin_alpha", version="1.0.0"
            ),
            reference_asset=None,
            grid_asset=None,
            atlas_asset=atlas,
            panel_asset=None,
            normalization_semantics_id="sem_cell_origin_normalization",
            coordinate_semantics_id=None,
            denominator_semantics_id="sem_registered_atlas_contributors",
            registered_policy=CompatibilityPolicyReference(
                policy_id="policy_cell_origin_alpha", version="1.0.0"
            ),
        ),
    )


def _decision(record: VerifiedMeasurementRecord, peer: VerifiedMeasurementRecord):
    rule = MeasurementCompatibilityPolicy(
        measurement_family=record.method.family,
        quantity_id=record.method.quantity_id,
        unit=record.method.unit,
        allowed_method_definitions=(
            AllowedMethodDefinition(
                method_ref=record.method.method_ref,
                method_definition_sha256=record.method_definition_sha256,
            ),
        ),
        allowed_result_schemas=(record.compatibility_key.result_schema,),
        delta_allowed_when_comparable=False,
        shared_axis_allowed_when_comparable=False,
    )
    policy = CompatibilityPolicy(
        policy_id="policy_cell_origin_alpha",
        version="1.0.0",
        registry_sha256=REGISTRY_SHA,
        registry_version=2,
        authority_head_sha256=HEAD_SHA,
        authority_revision=4,
        measurement_policies=(rule,),
    )
    return decide_compatibility(
        CompatibilityRequest(
            left=record,
            right=peer,
            policy=policy,
            trusted_policy_sha256=compatibility_policy_sha256(policy),
            trusted_authority_head_sha256=HEAD_SHA,
        )
    )


def _denominator(*, missing: bool = False) -> DenominatorLedger:
    def count(value: int, label: str) -> CountValue:
        return CountValue(
            state=CountState.MISSING if missing else CountState.OBSERVED,
            value=None if missing else value,
            accessible_label=label,
        )

    return DenominatorLedger(
        input_records=count(10, "Input aggregate records"),
        accepted_records=count(10, "Accepted aggregate records"),
        eligible_records=count(10, "Eligible aggregate records"),
        displayed_records=count(10, "Displayed aggregate records"),
        attrition=tuple(
            AttritionReason(
                stage=stage,
                reason_code=f"reason_zero_{stage.value}",
                accessible_label=f"Zero excluded at {stage.value}",
                count=count(0, f"Zero at {stage.value}"),
            )
            for stage in sorted(AttritionStage, key=str)
        ),
    )


def _request(
    *,
    bundle: CellOriginResultBundle | None = None,
    execution: ExecutionState = ExecutionState.COMPLETE,
    information: InformationState = InformationState.SUFFICIENT,
    trust: TrustState = TrustState.VERIFIED,
) -> CellOriginExplorerRequest:
    exact_bundle = bundle or _bundle()
    record = _record(
        exact_bundle,
        execution=execution,
        information=information,
        trust=trust,
    )
    peer = _record(exact_bundle, suffix="peer")
    source = bind_result_view_source(
        record=record,
        compatibility_decision=_decision(record, peer),
        denominator=_denominator(missing=execution == ExecutionState.NOT_RUN),
        accessible_label="Cell-origin aggregate",
        qc_label="Synthetic technical QC",
    )
    return CellOriginExplorerRequest(
        result_view_request=ResultViewRequest(
            filter_id="filter_cell_origin",
            sources=(source,),
            filters=normalize_result_filters(),
        ),
        bundle=(
            exact_bundle
            if execution == ExecutionState.COMPLETE
            and information == InformationState.SUFFICIENT
            and trust == TrustState.VERIFIED
            else None
        ),
    )


def test_builds_exact_rows_support_denominators_and_diagnostics() -> None:
    view = build_cell_origin_explorer(_request())

    assert view.status == ExplorerStatus.READY
    assert view.axis_label == AXIS_LABEL
    assert view.binding.cell_origin_method_sha256 == _digest(_bundle().result.method)
    assert [row.contributor_id for row in view.dot_interval_rows] == ["liver", "immune"]
    assert view.dot_interval_rows[0].estimate_fraction == 2 / 3
    assert view.exact_table_rows[0].estimate_fraction == 2 / 3
    assert view.marker_support.observed_markers == 2  # type: ignore[union-attr]
    assert view.fragment_denominators.marker_overlaps == 12  # type: ignore[union-attr]
    assert view.atlas_coverage.marker_coverage_fraction == 1.0  # type: ignore[union-attr]
    assert view.diagnostics.residual_l2 == 0.0  # type: ignore[union-attr]
    assert view.diagnostics.condition_number is None  # type: ignore[union-attr]
    assert LimitationId.CONDITIONING_NOT_REPORTED in view.limitations
    assert all(row.label == row.contributor_id for row in view.dot_interval_rows)


def test_zero_is_preserved_and_unavailable_interval_has_no_whisker() -> None:
    bundle = _bundle(unavailable_second_interval=True)
    payload = bundle.model_dump(mode="json")
    payload["result"]["deconvolution"]["estimates"][0]["raw_nnls_weight"] = 0.0
    payload["result"]["deconvolution"]["estimates"][0]["fraction"] = 0.0
    payload["result"]["deconvolution"]["estimates"][1]["fraction"] = 1.0
    payload["result"]["bootstrap"]["intervals"][0]["estimate"] = 0.0
    payload["result"]["bootstrap"]["intervals"][0]["lower_fraction"] = 0.0
    payload["result"]["bootstrap"]["intervals"][1]["estimate"] = 1.0
    for row in payload["charts"]["composition_rows"]:
        fraction = 0.0 if row["cell_type_id"] == "immune" else 1.0
        row["fraction"] = fraction
        row["percent"] = fraction * 100
        if row["cell_type_id"] == "immune":
            row["lower_fraction"] = 0.0
            row["lower_percent"] = 0.0
    exact = CellOriginResultBundle.model_validate_json(json.dumps(payload))
    view = build_cell_origin_explorer(_request(bundle=exact))

    immune = next(row for row in view.dot_interval_rows if row.contributor_id == "immune")
    liver = next(row for row in view.dot_interval_rows if row.contributor_id == "liver")
    assert immune.estimate_fraction == 0.0
    assert liver.lower_fraction is None and liver.upper_fraction is None
    assert liver.uncertainty_status == "insufficient_information"


def test_bootstrap_not_run_is_distinct_and_has_no_whiskers() -> None:
    payload = _bundle().model_dump(mode="json")
    payload["result"]["bootstrap"] = None
    for row in payload["charts"]["composition_rows"]:
        row["lower_fraction"] = None
        row["upper_fraction"] = None
        row["lower_percent"] = None
        row["upper_percent"] = None
        row["uncertainty_available"] = False
        row["uncertainty_status"] = "insufficient_information"
    bundle = CellOriginResultBundle.model_validate_json(json.dumps(payload))

    view = build_cell_origin_explorer(_request(bundle=bundle))

    assert view.uncertainty_status == "not_run"
    assert LimitationId.UNCERTAINTY_NOT_RUN in view.limitations
    assert all(
        row.uncertainty_status == "not_run"
        and row.lower_fraction is None
        and row.upper_fraction is None
        for row in view.dot_interval_rows
    )


def test_exact_filter_exclusion_is_missing_without_numeric_rows() -> None:
    request = _request()
    filtered_request = CellOriginExplorerRequest(
        result_view_request=ResultViewRequest(
            filter_id=request.result_view_request.filter_id,
            sources=request.result_view_request.sources,
            filters=normalize_result_filters(
                execution_states=(ExecutionState.FAILED,)
            ),
        ),
        bundle=request.bundle,
    )

    view = build_cell_origin_explorer(filtered_request)

    assert view.status == ExplorerStatus.MISSING
    assert view.dot_interval_rows == ()


@pytest.mark.parametrize(
    ("execution", "information", "expected"),
    [
        (ExecutionState.FAILED, InformationState.UNKNOWN, ExplorerStatus.FAILED),
        (ExecutionState.NOT_RUN, InformationState.UNKNOWN, ExplorerStatus.NOT_RUN),
        (
            ExecutionState.COMPLETE,
            InformationState.INSUFFICIENT,
            ExplorerStatus.INSUFFICIENT_INFORMATION,
        ),
    ],
)
def test_unavailable_states_never_emit_numeric_rows(
    execution: ExecutionState,
    information: InformationState,
    expected: ExplorerStatus,
) -> None:
    view = build_cell_origin_explorer(
        _request(execution=execution, information=information)
    )
    assert view.status == expected
    assert view.dot_interval_rows == ()
    assert view.exact_table_rows == ()
    assert view.marker_support is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "contributors"),
        ("missing", "contributors"),
        ("denominator", "denominator mismatch"),
        ("atlas", "atlas identity"),
    ],
)
def test_rejects_contributor_denominator_and_atlas_mismatches(
    mutation: str, message: str
) -> None:
    request = _request()
    payload = request.model_dump(mode="json")
    if mutation == "duplicate":
        payload["bundle"]["charts"]["composition_rows"][1]["cell_type_id"] = "liver"
    elif mutation == "missing":
        payload["bundle"]["charts"]["composition_rows"].pop()
    elif mutation == "denominator":
        payload["bundle"]["resources"]["registered_markers"] = 3
    else:
        payload["bundle"]["result"]["deconvolution"]["atlas_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match=message):
        CellOriginExplorerRequest.model_validate_json(json.dumps(payload))


def test_canonical_artifact_replays_and_rejects_tampering() -> None:
    artifact = build_cell_origin_explorer_artifact(_request())
    encoded = canonical_cell_origin_explorer_bytes(artifact)
    assert cell_origin_explorer_from_canonical_bytes(encoded) == artifact

    payload = json.loads(encoded)
    payload["view"]["dot_interval_rows"][0]["estimate_fraction"] = 0.5
    payload["view"]["exact_table_rows"][0]["estimate_fraction"] = 0.5
    tampered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(CellOriginExplorerError, match="invalid"):
        cell_origin_explorer_from_canonical_bytes(tampered)


def test_output_contains_no_presentation_aliases_or_private_fields() -> None:
    view = build_cell_origin_explorer(_request())
    rendered = view.model_dump_json().lower()
    assert "presentation label" not in rendered
    assert not any(
        key in rendered
        for key in ("donor_id", "patient_id", "read_id", "sample_id", "local_path")
    )


def test_private_source_text_fails_closed_before_digest_validation() -> None:
    payload = _request().model_dump(mode="json")
    payload["bundle"]["notices"] = ["credential TOKEN=synthetic-placeholder"]
    with pytest.raises(ValidationError, match="private data"):
        CellOriginExplorerRequest.model_validate_json(json.dumps(payload))


def test_view_contract_is_closed_and_bounded() -> None:
    artifact = build_cell_origin_explorer_artifact(_request())
    payload = artifact.model_dump(mode="json")
    payload["view"]["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        CellOriginExplorerArtifact.model_validate_json(json.dumps(payload))
