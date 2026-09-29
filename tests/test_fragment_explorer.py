"""Offline property and adversarial tests for the E07 fragment explorer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityPolicyReference,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    MeasurementCompatibilityPolicy,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
)
from evidence_inspector.fragment_explorer import (
    ExplorerControls,
    ExplorerMethodSelection,
    ExplorerSourceState,
    FragmentExplorerError,
    FragmentExplorerRequest,
    FragmentExplorerState,
    FragmentExplorerView,
    FragmentQuantity,
    PanelId,
    VerifiedFragmentSource,
    build_fragment_explorer_state,
    build_fragment_explorer_view,
    canonical_fragment_explorer_bytes,
    fragment_explorer_from_canonical_bytes,
    fragment_source_from_verified_bundle,
    replay_fragment_explorer_view,
)
from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    DisplayRole,
    MethodDefinition,
    MethodFamily,
    MethodReference,
    QualificationState,
    ToolReference,
    method_definition_sha256,
)
from traceback_runner.bundles import build_result_bundle, verify_bundle
from traceback_runner.contracts import (
    BundleContent,
    CompletionState,
    ExclusionCounts,
    FragmentMeasurement,
    HistogramBin,
    HistogramCount,
    ResultBundleManifest,
    canonical_json_bytes,
)
from traceback_runner.export import chart_for_measurement
from traceback_runner.signing import (
    KeyPurpose,
    TrustStore,
    generate_development_keypair,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
HEAD_SHA256 = "a" * 64
REGISTRY_SHA256 = "b" * 64

QUANTITY_IDS = {
    FragmentQuantity.RAW_QUERY_LENGTH: "qty_fragment_raw_query_length",
    FragmentQuantity.ALIGNED_QUERY_LENGTH: "qty_fragment_aligned_query_length",
    FragmentQuantity.ALIGNED_REFERENCE_SPAN: (
        "qty_fragment_aligned_reference_span"
    ),
}
DEFINITION_IDS = {
    FragmentQuantity.RAW_QUERY_LENGTH: "raw-query-length.v1",
    FragmentQuantity.ALIGNED_QUERY_LENGTH: "aligned-query-length.v1",
    FragmentQuantity.ALIGNED_REFERENCE_SPAN: "aligned-reference-span.v1",
}
METHOD_SUFFIXES = {
    FragmentQuantity.RAW_QUERY_LENGTH: "raw",
    FragmentQuantity.ALIGNED_QUERY_LENGTH: "query",
    FragmentQuantity.ALIGNED_REFERENCE_SPAN: "span",
}


def _digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        content = canonical_json_bytes(value)  # type: ignore[arg-type]
    else:
        content = canonical_json_bytes(value)
    return hashlib.sha256(content).hexdigest()


def _canonical_tampered_view(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "view_sha256"}
    payload["view_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return canonical_json_bytes(payload)


def _asset(name: str, digit: str) -> AssetReference:
    return AssetReference(
        asset_id=f"asset_{name}",
        version="1.0.0",
        content_sha256=digit * 64,
    )


ASSETS = tuple(
    sorted(
        (
            _asset("atlas_alpha", "1"),
            _asset("grid_alpha", "2"),
            _asset("panel_alpha", "3"),
            _asset("reference_alpha", "4"),
        ),
        key=lambda item: (item.asset_id, item.version),
    )
)
ASSET_BY_ID = {item.asset_id: item for item in ASSETS}


def _method(suffix: str, quantity: FragmentQuantity) -> MethodDefinition:
    return MethodDefinition(
        method_id=f"mth_fragment_{suffix}",
        version="1.0.0",
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id=QUANTITY_IDS[quantity],
        unit="unit_bp",
        parameter_schema_sha256=("5" if suffix == "alpha" else "6") * 64,
        tools=(
            ToolReference(
                tool_id=f"tool_fragment_{suffix}",
                version="1.0.0",
                artifact_sha256="7" * 64,
            ),
        ),
        assets=ASSETS,
    )


def _capability(method: MethodDefinition) -> CurrentMethodCapability:
    return CurrentMethodCapability(
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=HEAD_SHA256,
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


def _key(method: MethodDefinition) -> MeasurementCompatibilityKey:
    return MeasurementCompatibilityKey(
        measurement_family=method.family,
        quantity_id=method.quantity_id,
        unit=method.unit,
        result_schema=ResultSchemaReference(
            schema_id="schema_fragment_histogram", version="1.0.0"
        ),
        reference_asset=ASSET_BY_ID["asset_reference_alpha"],
        grid_asset=ASSET_BY_ID["asset_grid_alpha"],
        atlas_asset=ASSET_BY_ID["asset_atlas_alpha"],
        panel_asset=ASSET_BY_ID["asset_panel_alpha"],
        normalization_semantics_id="sem_normalization_alpha",
        coordinate_semantics_id="sem_coordinate_alpha",
        denominator_semantics_id="sem_denominator_alpha",
        registered_policy=CompatibilityPolicyReference(
            policy_id="policy_fragment_explorer", version="1.0.0"
        ),
    )


def _measurement(
    quantity: FragmentQuantity,
    counts: tuple[int, int, int] = (2, 3, 5),
) -> FragmentMeasurement:
    eligible = sum(counts)
    return FragmentMeasurement(
        definition_id=DEFINITION_IDS[quantity],
        reference_id="synthetic-reference.v1",
        completion=CompletionState.COMPLETE,
        records_scanned=eligible + 2,
        eligible_alignments=eligible,
        exclusions=ExclusionCounts(
            unmapped=2,
            secondary=0,
            supplementary=0,
            qc_failure=0,
            duplicate=0,
            low_mapping_quality=0,
            unregistered_contig=0,
            no_reference_span=0,
        ),
        histogram=(
            HistogramCount(
                bin=HistogramBin(lower_inclusive=0, upper_exclusive=100),
                count=counts[0],
            ),
            HistogramCount(
                bin=HistogramBin(lower_inclusive=100, upper_exclusive=200),
                count=counts[1],
            ),
            HistogramCount(
                bin=HistogramBin(lower_inclusive=200, upper_exclusive=None),
                count=counts[2],
            ),
        ),
    )


def _manifest(
    suffix: str,
    measurement: FragmentMeasurement,
) -> tuple[ResultBundleManifest, Any]:
    measurement_sha = _digest(measurement)
    chart = chart_for_measurement(measurement, measurement_sha)
    chart_sha = _digest(chart)
    manifest = ResultBundleManifest(
        record_id=f"record-{suffix}",
        workflow_release_id="synthetic-workflow.v1",
        measurement_schema_versions=(measurement.schema_version,),
        contents=(
            BundleContent(
                relative_path="charts/fragment-length.v1.json",
                sha256=chart_sha,
                size_bytes=len(canonical_json_bytes(chart)),
            ),
            BundleContent(
                relative_path="measurements/fragment-length.v1.json",
                sha256=measurement_sha,
                size_bytes=len(canonical_json_bytes(measurement)),
            ),
        ),
        signing_key_id="synthetic-key.v1",
    )
    return manifest, chart


def _record(
    suffix: str,
    quantity: FragmentQuantity,
    *,
    measurement: FragmentMeasurement | None = None,
    manifest: ResultBundleManifest | None = None,
    execution_state: ExecutionState = ExecutionState.COMPLETE,
    information_state: InformationState = InformationState.SUFFICIENT,
    trust_state: TrustState = TrustState.VERIFIED,
) -> VerifiedMeasurementRecord:
    method = _method(METHOD_SUFFIXES[quantity], quantity)
    return VerifiedMeasurementRecord(
        result_id=f"result_{suffix}",
        result_sha256=_digest(measurement) if measurement is not None else "8" * 64,
        bundle_id=f"bundle_{suffix}",
        bundle_sha256=_digest(manifest) if manifest is not None else "9" * 64,
        method=method,
        method_definition_sha256=method_definition_sha256(method),
        current_capability=_capability(method),
        execution_state=execution_state,
        information_state=information_state,
        trust_state=trust_state,
        compatibility_key=_key(method),
    )


def _source(
    suffix: str,
    *,
    quantity: FragmentQuantity = FragmentQuantity.ALIGNED_REFERENCE_SPAN,
    counts: tuple[int, int, int] = (2, 3, 5),
    state: ExplorerSourceState = ExplorerSourceState.COMPLETE,
) -> VerifiedFragmentSource:
    data_states = {
        ExplorerSourceState.COMPLETE,
        ExplorerSourceState.INSUFFICIENT,
        ExplorerSourceState.REVOKED,
        ExplorerSourceState.UNVERIFIED,
    }
    measurement = _measurement(quantity, counts) if state in data_states else None
    manifest: ResultBundleManifest | None = None
    chart = None
    if measurement is not None:
        manifest, chart = _manifest(suffix, measurement)
    execution = (
        ExecutionState.FAILED
        if state == ExplorerSourceState.FAILED
        else ExecutionState.COMPLETE
    )
    information = (
        InformationState.INSUFFICIENT
        if state == ExplorerSourceState.INSUFFICIENT
        else InformationState.SUFFICIENT
    )
    trust = (
        TrustState.REVOKED
        if state == ExplorerSourceState.REVOKED
        else TrustState.UNVERIFIED
        if state == ExplorerSourceState.UNVERIFIED
        else TrustState.VERIFIED
    )
    record = _record(
        suffix,
        quantity,
        measurement=measurement,
        manifest=manifest,
        execution_state=execution,
        information_state=information,
        trust_state=trust,
    )
    return VerifiedFragmentSource(
        record=record,
        quantity=quantity,
        state=state,
        manifest=manifest,
        measurement=measurement,
        chart=chart,
    )


def _policy(
    *sources: VerifiedFragmentSource,
    delta_allowed: bool = True,
    shared_axis_allowed: bool = True,
) -> CompatibilityPolicy:
    grouped: dict[tuple[str, str, str], list[VerifiedMeasurementRecord]] = {}
    for source in sources:
        key = source.record.compatibility_key
        grouped.setdefault(
            (key.measurement_family.value, key.quantity_id, key.unit), []
        ).append(source.record)
    rules = []
    for group_key in sorted(grouped):
        records = grouped[group_key]
        first = records[0]
        methods = tuple(
            sorted(
                {
                    AllowedMethodDefinition(
                        method_ref=item.method.method_ref,
                        method_definition_sha256=item.method_definition_sha256,
                    )
                    for item in records
                },
                key=lambda item: item.sort_key,
            )
        )
        rules.append(
            MeasurementCompatibilityPolicy(
                measurement_family=first.compatibility_key.measurement_family,
                quantity_id=first.compatibility_key.quantity_id,
                unit=first.compatibility_key.unit,
                allowed_method_definitions=methods,
                allowed_result_schemas=(first.compatibility_key.result_schema,),
                delta_allowed_when_comparable=delta_allowed,
                shared_axis_allowed_when_comparable=shared_axis_allowed,
            )
        )
    return CompatibilityPolicy(
        policy_id="policy_fragment_explorer",
        version="1.0.0",
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=HEAD_SHA256,
        authority_revision=4,
        measurement_policies=tuple(rules),
    )


def _selection(source: VerifiedFragmentSource) -> ExplorerMethodSelection:
    return ExplorerMethodSelection(
        result_id=source.record.result_id,
        method_ref=source.record.method.method_ref,
    )


def _state(
    left: VerifiedFragmentSource,
    right: VerifiedFragmentSource,
    *,
    controls: ExplorerControls | None = None,
    left_controls: ExplorerControls | None = None,
    right_controls: ExplorerControls | None = None,
) -> FragmentExplorerState:
    if left_controls is not None or right_controls is not None:
        return build_fragment_explorer_state(
            left=_selection(left),
            right=_selection(right),
            filters_linked=False,
            left_controls=left_controls,
            right_controls=right_controls,
        )
    return build_fragment_explorer_state(
        left=_selection(left),
        right=_selection(right),
        filters_linked=True,
        linked_controls=controls
        or ExplorerControls(bin_start_inclusive=0, bin_end_exclusive=3),
    )


def _request(
    left: VerifiedFragmentSource,
    right: VerifiedFragmentSource,
    *,
    state: FragmentExplorerState | None = None,
    policy: CompatibilityPolicy | None = None,
) -> FragmentExplorerRequest:
    exact_policy = policy or _policy(left, right)
    sources = tuple(
        sorted((left, right), key=lambda item: item.record.result_id)
    )
    return FragmentExplorerRequest(
        sources=sources,
        policy=exact_policy,
        trusted_policy_sha256=compatibility_policy_sha256(exact_policy),
        trusted_authority_head_sha256=HEAD_SHA256,
        state=state or _state(left, right),
    )


def test_linked_view_has_exact_table_deltas_axes_and_replay() -> None:
    left = _source("alpha", counts=(2, 3, 5))
    right = _source("beta", counts=(1, 4, 5))
    request = _request(left, right)

    view = build_fragment_explorer_view(request)

    assert view.compatibility.outcome == CompatibilityOutcome.COMPARABLE
    assert view.synchronized_comparison
    assert view.shared_y_scale
    assert view.left.y_axis_max == view.right.y_axis_max == 5
    assert [item.count_delta_right_minus_left for item in view.delta_rows] == [
        -1,
        1,
        0,
    ]
    assert len(view.accessible_rows) == len(view.left.rows) + len(view.right.rows)
    assert replay_fragment_explorer_view(request, view) == view
    encoded = canonical_fragment_explorer_bytes(view)
    assert fragment_explorer_from_canonical_bytes(
        FragmentExplorerView, encoded
    ) == view
    state_bytes = canonical_fragment_explorer_bytes(request.state)
    assert fragment_explorer_from_canonical_bytes(
        FragmentExplorerState, state_bytes
    ) == request.state


@pytest.mark.parametrize("start,end", [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)])
def test_half_open_ranges_preserve_denominator_and_exact_totals(
    start: int, end: int
) -> None:
    left = _source("alpha", counts=(2, 3, 5))
    right = _source("beta", counts=(1, 4, 5))
    controls = ExplorerControls(
        bin_start_inclusive=start,
        bin_end_exclusive=end,
        minimum_count_inclusive=3,
    )
    view = build_fragment_explorer_view(
        _request(left, right, state=_state(left, right, controls=controls))
    )

    for panel, counts in ((view.left, (2, 3, 5)), (view.right, (1, 4, 5))):
        expected = tuple(value for value in counts[start:end] if value >= 3)
        assert tuple(item.count for item in panel.rows) == expected
        assert panel.denominator is not None
        assert panel.denominator.eligible_alignments == 10
        assert panel.denominator.displayed_alignments == sum(expected)
        assert panel.denominator.outside_display_alignments == 10 - sum(expected)
        assert all(item.fraction_denominator == 10 for item in panel.rows)


def test_unlinked_filters_withhold_cross_panel_deltas_and_shared_axis() -> None:
    left = _source("alpha", counts=(2, 3, 5))
    right = _source("beta", counts=(1, 4, 5))
    state = _state(
        left,
        right,
        left_controls=ExplorerControls(
            bin_start_inclusive=0, bin_end_exclusive=2
        ),
        right_controls=ExplorerControls(
            bin_start_inclusive=1, bin_end_exclusive=3
        ),
    )

    view = build_fragment_explorer_view(_request(left, right, state=state))

    assert not view.shared_y_scale
    assert not view.synchronized_comparison
    assert view.delta_rows == ()
    assert view.left.y_axis_max == 3
    assert view.right.y_axis_max == 5


@pytest.mark.parametrize(
    "left_quantity,right_quantity",
    [
        (
            FragmentQuantity.RAW_QUERY_LENGTH,
            FragmentQuantity.ALIGNED_QUERY_LENGTH,
        ),
        (
            FragmentQuantity.RAW_QUERY_LENGTH,
            FragmentQuantity.ALIGNED_REFERENCE_SPAN,
        ),
        (
            FragmentQuantity.ALIGNED_QUERY_LENGTH,
            FragmentQuantity.ALIGNED_REFERENCE_SPAN,
        ),
    ],
)
def test_raw_aligned_query_and_reference_span_are_never_conflated(
    left_quantity: FragmentQuantity, right_quantity: FragmentQuantity
) -> None:
    left = _source("alpha", quantity=left_quantity)
    right = _source("beta", quantity=right_quantity)

    view = build_fragment_explorer_view(_request(left, right))

    assert view.compatibility.outcome == CompatibilityOutcome.DIFFERENT_QUANTITY
    assert not view.shared_y_scale
    assert view.delta_rows == ()


def test_source_rejects_quantity_definition_chart_and_manifest_drift() -> None:
    source = _source("alpha")
    payload = source.model_dump(mode="json")
    payload["quantity"] = FragmentQuantity.RAW_QUERY_LENGTH.value
    with pytest.raises(ValidationError, match="quantity identity"):
        VerifiedFragmentSource.model_validate_json(json.dumps(payload))

    payload = source.model_dump(mode="json")
    payload["chart"]["rows"][0]["count"] += 1
    with pytest.raises(ValidationError, match="chart and measurement table"):
        VerifiedFragmentSource.model_validate_json(json.dumps(payload))

    payload = source.model_dump(mode="json")
    payload["manifest"]["contents"][0]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="manifest does not bind"):
        VerifiedFragmentSource.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "state",
    [
        ExplorerSourceState.LOADING,
        ExplorerSourceState.EMPTY,
        ExplorerSourceState.PARTIAL,
        ExplorerSourceState.FAILED,
        ExplorerSourceState.INSUFFICIENT,
        ExplorerSourceState.REVOKED,
        ExplorerSourceState.UNVERIFIED,
        ExplorerSourceState.UNSUPPORTED,
        ExplorerSourceState.UNAVAILABLE,
    ],
)
def test_non_complete_states_withhold_every_number(
    state: ExplorerSourceState,
) -> None:
    left = _source("alpha", state=state)
    right = _source("beta")

    view = build_fragment_explorer_view(_request(left, right))

    assert view.left.rows == ()
    assert view.left.denominator is None
    assert view.left.y_axis_max is None
    assert view.left.withholding_code is not None
    assert not view.shared_y_scale
    assert view.delta_rows == ()
    assert all(item.panel == PanelId.B for item in view.accessible_rows)


def test_policy_can_independently_withhold_delta_and_shared_axis() -> None:
    left = _source("alpha")
    right = _source("beta")
    policy = _policy(left, right, delta_allowed=False, shared_axis_allowed=False)

    view = build_fragment_explorer_view(_request(left, right, policy=policy))

    assert view.compatibility.outcome == CompatibilityOutcome.COMPARABLE
    assert view.synchronized_comparison
    assert not view.shared_y_scale
    assert view.delta_rows == ()


def test_equal_panel_local_controls_remain_unlinked_and_unsynchronized() -> None:
    left = _source("alpha")
    right = _source("beta")
    controls = ExplorerControls(bin_start_inclusive=0, bin_end_exclusive=3)
    state = _state(
        left,
        right,
        left_controls=controls,
        right_controls=controls,
    )

    view = build_fragment_explorer_view(_request(left, right, state=state))

    assert view.compatibility.outcome == CompatibilityOutcome.COMPARABLE
    assert not view.state.filters_linked
    assert not view.synchronized_comparison
    assert not view.shared_y_scale
    assert view.delta_rows == ()


def test_empty_display_uses_safe_axis_without_changing_denominator() -> None:
    left = _source("alpha")
    right = _source("beta")
    controls = ExplorerControls(
        bin_start_inclusive=0,
        bin_end_exclusive=3,
        minimum_count_inclusive=100,
    )

    view = build_fragment_explorer_view(
        _request(left, right, state=_state(left, right, controls=controls))
    )

    assert view.left.rows == view.right.rows == ()
    assert view.left.y_axis_max == view.right.y_axis_max == 1
    assert view.left.denominator is not None
    assert view.left.denominator.eligible_alignments == 10
    assert view.left.denominator.outside_display_alignments == 10


def test_invalid_ranges_and_out_of_bounds_windows_fail_closed() -> None:
    with pytest.raises(ValidationError, match="half-open"):
        ExplorerControls(bin_start_inclusive=1, bin_end_exclusive=1)
    left = _source("alpha")
    right = _source("beta")
    state = _state(
        left,
        right,
        controls=ExplorerControls(
            bin_start_inclusive=0, bin_end_exclusive=4
        ),
    )
    with pytest.raises(FragmentExplorerError, match="exceeds"):
        build_fragment_explorer_view(_request(left, right, state=state))


def test_accessible_table_parity_and_replay_reject_self_consistent_drift() -> None:
    left = _source("alpha")
    right = _source("beta")
    request = _request(left, right)
    view = build_fragment_explorer_view(request)
    payload = view.model_dump(mode="json")
    payload["accessible_rows"] = payload["accessible_rows"][:-1]
    with pytest.raises(ValidationError, match="accessible table"):
        FragmentExplorerView.model_validate_json(json.dumps(payload))

    payload = view.model_dump(mode="json")
    payload["shared_y_scale"] = False
    with pytest.raises(ValidationError, match="shared y scale"):
        FragmentExplorerView.model_validate_json(json.dumps(payload))

    payload = view.model_dump(mode="json")
    payload["left"]["y_axis_max"] += 1
    with pytest.raises(ValidationError, match="left y axis"):
        FragmentExplorerView.model_validate_json(json.dumps(payload))

    payload = view.model_dump(mode="json")
    payload["delta_rows"] = payload["delta_rows"][:-1]
    with pytest.raises(ValidationError, match="delta rows"):
        FragmentExplorerView.model_validate_json(json.dumps(payload))

    changed_controls = ExplorerControls(
        bin_start_inclusive=1, bin_end_exclusive=3
    )
    changed_request = _request(
        left,
        right,
        state=_state(left, right, controls=changed_controls),
    )
    changed_view = build_fragment_explorer_view(changed_request)
    with pytest.raises(FragmentExplorerError, match="canonical replay"):
        replay_fragment_explorer_view(request, changed_view)


def test_canonical_parse_rejects_state_panel_and_fraction_rebinding() -> None:
    left = _source("alpha")
    right = _source("beta")
    view = build_fragment_explorer_view(_request(left, right))

    payload = view.model_dump(mode="json")
    payload["left"]["selection"] = payload["right"]["selection"]
    with pytest.raises(FragmentExplorerError, match="JSON is invalid"):
        fragment_explorer_from_canonical_bytes(
            FragmentExplorerView, _canonical_tampered_view(payload)
        )

    payload = view.model_dump(mode="json")
    payload["left"]["panel"] = "b"
    payload["right"]["panel"] = "a"
    for row in payload["accessible_rows"]:
        row["panel"] = "b" if row["panel"] == "a" else "a"
    with pytest.raises(FragmentExplorerError, match="JSON is invalid"):
        fragment_explorer_from_canonical_bytes(
            FragmentExplorerView, _canonical_tampered_view(payload)
        )

    payload = view.model_dump(mode="json")
    payload["left"]["controls"]["bin_end_exclusive"] = 2
    with pytest.raises(FragmentExplorerError, match="JSON is invalid"):
        fragment_explorer_from_canonical_bytes(
            FragmentExplorerView, _canonical_tampered_view(payload)
        )

    payload = view.model_dump(mode="json")
    payload["left"]["rows"][0]["fraction_denominator"] += 1
    payload["accessible_rows"][0]["fraction_denominator"] += 1
    with pytest.raises(FragmentExplorerError, match="JSON is invalid"):
        fragment_explorer_from_canonical_bytes(
            FragmentExplorerView, _canonical_tampered_view(payload)
        )


def test_explicit_method_selection_is_required_and_never_auto_repaired() -> None:
    left = _source("alpha")
    right = _source("beta")
    mismatched = build_fragment_explorer_state(
        left=_selection(left),
        right=ExplorerMethodSelection(
            result_id=right.record.result_id,
            method_ref=MethodReference(
                method_id="mth_fragment_other", version="1.0.0"
            ),
        ),
        filters_linked=True,
        linked_controls=ExplorerControls(
            bin_start_inclusive=0, bin_end_exclusive=3
        ),
    )
    with pytest.raises(ValidationError, match="does not match result"):
        _request(left, right, state=mismatched)


@pytest.mark.parametrize(
    "private_value",
    ["donor123", "donorabc", "sample123", "sampleabc", "patient123", "patientabc"],
)
def test_privacy_terms_are_rejected_from_canonical_source_and_output(
    private_value: str,
) -> None:
    source = _source("alpha")
    payload = source.model_dump(mode="json")
    payload["manifest"]["record_id"] = private_value
    manifest = ResultBundleManifest.model_validate(payload["manifest"])
    payload["record"]["bundle_sha256"] = _digest(manifest)
    with pytest.raises(ValidationError, match="reserved privacy"):
        VerifiedFragmentSource.model_validate_json(json.dumps(payload))

    right = _source("beta")
    encoded = canonical_fragment_explorer_bytes(
        build_fragment_explorer_view(_request(source, right))
    ).decode()
    assert private_value not in encoded


@pytest.mark.parametrize(
    "safe_lexeme",
    [
        "runtime",
        "runner",
        "readout",
        "readiness",
        "pathology",
        "sampled",
        "sequencer",
    ],
)
def test_safe_domain_lexemes_remain_exportable(safe_lexeme: str) -> None:
    source = _source("alpha")
    payload = source.model_dump(mode="json")
    payload["manifest"]["record_id"] = safe_lexeme
    manifest = ResultBundleManifest.model_validate(payload["manifest"])
    payload["record"]["bundle_sha256"] = _digest(manifest)

    parsed = VerifiedFragmentSource.model_validate_json(json.dumps(payload))

    assert safe_lexeme.encode() in canonical_fragment_explorer_bytes(parsed)


def test_closed_bounded_models_and_noncanonical_json_fail_closed() -> None:
    left = _source("alpha")
    right = _source("beta")
    state = _state(left, right)
    payload = state.model_dump(mode="json")
    payload["automatic_method"] = True
    with pytest.raises(ValidationError, match="extra"):
        FragmentExplorerState.model_validate_json(json.dumps(payload))

    encoded = canonical_fragment_explorer_bytes(state)
    pretty = json.dumps(json.loads(encoded), indent=2, sort_keys=True).encode()
    with pytest.raises(FragmentExplorerError, match="not canonical"):
        fragment_explorer_from_canonical_bytes(FragmentExplorerState, pretty)


def test_verified_e02_adapter_drops_local_path_and_preserves_exact_content(
    tmp_path: Path,
) -> None:
    measurement = _measurement(FragmentQuantity.ALIGNED_REFERENCE_SPAN)
    key = generate_development_keypair(KeyPurpose.RESULT)
    trust = TrustStore()
    trust.add_signing_key(key)
    bundle_path = build_result_bundle(
        tmp_path / "synthetic-record",
        measurement=measurement,
        provenance={
            "schema_version": "traceback.run-provenance.v1",
            "run_token": "synthetic.token.v1",
            "input_kind": "modbam",
            "protocol_run_token": "synthetic.protocol.v1",
            "workflow_release_id": "synthetic-workflow.v1",
            "artifacts": [
                {
                    "role": "analysis_input",
                    "artifact_token": "synthetic-input.v1",
                    "size_bytes": 100,
                    "provider_hmac_sha256": "c" * 64,
                }
            ],
        },
        signing_key=key,
    )
    verified = verify_bundle(bundle_path, trust)
    record = _record(
        "alpha",
        FragmentQuantity.ALIGNED_REFERENCE_SPAN,
        measurement=verified.measurement,
        manifest=verified.manifest,
    )

    source = fragment_source_from_verified_bundle(
        verified,
        record=record,
        quantity=FragmentQuantity.ALIGNED_REFERENCE_SPAN,
    )
    encoded = canonical_fragment_explorer_bytes(source)

    assert source.measurement == verified.measurement
    assert source.chart == verified.chart
    assert str(tmp_path).encode() not in encoded
    assert b"private" not in encoded
