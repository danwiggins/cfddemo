"""Contract tests for cell-origin methylation evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    CellOriginInputBundle,
    CellOriginResult,
    DeconvolutionOutput,
    DeconvolutionOutputV2,
    FragmentMarkerObservation,
    GenomicMarker,
    KATSMAN_METHATLAS_METHOD,
    LOYFER_UXM_METHOD,
    MarkerCountRow,
    MethodDefinition,
    ModkitCpgCall,
    ModkitCpgCallV2,
    ModkitIngestionLedgerV2,
    ModkitInputProvenanceV2,
    ModkitInputResultV2,
    NnlsRowScale,
    RangeClassification,
    ReferenceRangeRow,
    UxmState,
    UxmThresholds,
    classify_uxm,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cell_origin"
V2_SOLVER_DIAGNOSTICS = {
    "row_scale": "sqrt_count",
    "solver_tolerance": 1e-12,
    "max_iterations": 10_000,
    "solver_implementation_id": "traceback.active-set-nnls.v1",
}
V2_ATLAS_SHA256 = "a" * 64


def _modkit_provenance(
    policy: str = "precall_combined_m_h",
) -> dict[str, object]:
    probability_input = policy == "precall_combined_m_h"
    return {
        "source_schema_id": (
            "traceback.generic-cmh-probabilities.v1"
            if probability_input
            else "traceback.generic-hard-call-cpg.v2"
        ),
        "source_schema_version": "1" if probability_input else "2",
        "source_tool_id": "unknown",
        "source_tool_version": "unknown",
        "source_model_id": "unknown",
        "source_model_version": "unknown",
        "policy": policy,
        "probability_threshold": 0.7 if probability_input else None,
        "probability_threshold_source": (
            "adapter_explicit" if probability_input else "source_unknown"
        ),
        "tie_policy": "exclude_exact_ties",
        "probability_tie_tolerance": 0.0,
        "probability_sum_tolerance": 1e-6,
        "reference_id": "reference.synthetic.v1",
        "reference_sha256": "a" * 64,
        "reference_context_provider_id": "fixture-reference-provider.v1",
        "reference_context_validation_scope": "centered_cpg_dyad",
        "coordinate_policy_id": "canonical-reference-forward-cpg-c.v1",
        "duplicate_policy": "exact_observation_only",
    }


def _modkit_call(
    *,
    original_position0: int = 100,
    canonical_cpg_position0: int = 100,
    modification_strand: str = "+",
    reference_mod_strand: str = "+",
    selected_state_probability: float = 0.8,
    state: str = "methylated",
    policy: str = "precall_combined_m_h",
) -> dict[str, object]:
    return {
        "schema_version": "cell-origin-cpg-call.v2",
        "fragment_digest": "b" * 64,
        "chromosome": "chr1",
        "original_position0": original_position0,
        "canonical_cpg_position0": canonical_cpg_position0,
        "modification_strand": modification_strand,
        "reference_mod_strand": reference_mod_strand,
        "selected_state_probability": selected_state_probability,
        "state": state,
        "policy": policy,
    }


def _modkit_ledger(
    policy: str = "precall_combined_m_h",
) -> dict[str, object]:
    probability_input = policy == "precall_combined_m_h"
    return {
        "policy": policy,
        "total_rows": 5,
        "source_failed_rows": 1,
        "source_passed_rows": 4,
        "excluded_non_c_rows": 1,
        "candidate_c_rows": 3,
        "hard_call_c_rows": 0 if probability_input else 1,
        "hard_call_m_rows": 0 if probability_input else 1,
        "hard_call_h_rows": 0 if probability_input else 1,
        "probability_input_rows": 3 if probability_input else 0,
        "excluded_probability_tie_rows": 1 if probability_input else 0,
        "excluded_low_confidence_rows": 0,
        "eligible_call_rows": 2 if probability_input else 3,
        "unmethylated_call_rows": 1,
        "methylated_call_rows": 1 if probability_input else 2,
        "reference_plus_call_rows": 1,
        "reference_minus_call_rows": 1 if probability_input else 2,
        "malformed_rows": 0,
        "duplicate_rows": 0,
    }


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_payload(name: str) -> Any:
    return json.loads(fixture_bytes(name))


def validate_json(model: type[Any], payload: Any) -> Any:
    return model.model_validate_json(json.dumps(payload))


def test_hand_checkable_fixtures_validate() -> None:
    markers = TypeAdapter(tuple[GenomicMarker, ...]).validate_json(
        fixture_bytes("markers.json")
    )
    observation = FragmentMarkerObservation.model_validate_json(
        fixture_bytes("fragment_observation.json")
    )
    matrix = AtlasUMatrix.model_validate_json(
        fixture_bytes("atlas_u_matrix.json")
    )
    result = CellOriginResult.model_validate_json(fixture_bytes("result.json"))

    assert len(markers) == 2
    assert observation.state == UxmState.U
    assert observation.methylation_fraction == 0.25
    assert matrix.rows[0].values[0].u_fraction == 0.8
    assert result.marker_counts[0].u_fraction == 2 / 5
    assert result.deconvolution.estimates[0].fraction == pytest.approx(1 / 3)
    assert result.validation.passed


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0.0, UxmState.U),
        (0.25, UxmState.U),
        (0.250999999, UxmState.U),
        (0.251, UxmState.X),
        (0.749999999, UxmState.X),
        (0.75, UxmState.M),
        (1.0, UxmState.M),
    ],
)
def test_exact_uxm_boundaries(fraction: float, expected: UxmState) -> None:
    assert classify_uxm(fraction, 4) == expected


@pytest.mark.parametrize("cpg_count", [0, 1, 2, 3])
def test_uxm_requires_four_cpgs(cpg_count: int) -> None:
    with pytest.raises(ValueError, match="at least 4"):
        classify_uxm(0.0, cpg_count)


@pytest.mark.parametrize("fraction", [-0.1, 1.1, float("nan"), float("inf")])
def test_uxm_rejects_invalid_fraction(fraction: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        classify_uxm(fraction, 4)


def test_uxm_threshold_contract_is_fixed() -> None:
    assert UxmThresholds() == UxmThresholds(
        minimum_cpgs=4,
        unmethylated_max_exclusive=0.251,
        methylated_min_inclusive=0.75,
    )
    with pytest.raises(ValidationError):
        UxmThresholds(unmethylated_max_exclusive=0.25)


def test_contracts_are_frozen_and_forbid_extra_fields() -> None:
    marker = TypeAdapter(tuple[GenomicMarker, ...]).validate_json(
        fixture_bytes("markers.json")
    )[0]
    with pytest.raises(ValidationError, match="frozen"):
        marker.start0 = 50  # type: ignore[misc]

    payload = fixture_payload("fragment_observation.json")
    payload["read_id"] = "raw-read-name"
    with pytest.raises(ValidationError, match="Extra inputs"):
        validate_json(FragmentMarkerObservation, payload)


def test_marker_uses_nonempty_zero_based_half_open_interval() -> None:
    payload = fixture_payload("markers.json")[0]
    payload["end0"] = payload["start0"]
    with pytest.raises(ValidationError, match="non-empty"):
        validate_json(GenomicMarker, payload)

    payload = fixture_payload("markers.json")[0]
    payload["chromosome"] = "1"
    with pytest.raises(ValidationError):
        validate_json(GenomicMarker, payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda p: p.update(callable_cpg_count=5), "callable_cpg_count"),
        (lambda p: p.update(methylated_cpg_count=2), "methylated_cpg_count"),
        (lambda p: p.update(methylation_fraction=0.5), "methylation_fraction"),
        (lambda p: p.update(state="X"), "fixed UXM"),
    ],
)
def test_fragment_observation_reconciles_calls(
    mutation: Any, message: str
) -> None:
    payload = fixture_payload("fragment_observation.json")
    mutation(payload)
    with pytest.raises(ValidationError, match=message):
        validate_json(FragmentMarkerObservation, payload)


def test_fragment_observation_rejects_wrong_fragment_and_outside_marker() -> None:
    payload = fixture_payload("fragment_observation.json")
    payload["cpg_calls"][0]["fragment_digest"] = "d" * 64
    with pytest.raises(ValidationError, match="same fragment"):
        validate_json(FragmentMarkerObservation, payload)

    payload = fixture_payload("fragment_observation.json")
    payload["cpg_calls"][0]["position0"] = 200
    with pytest.raises(ValidationError, match="outside"):
        validate_json(FragmentMarkerObservation, payload)


def test_fragment_observation_rejects_duplicate_cpg_locus() -> None:
    payload = fixture_payload("fragment_observation.json")
    payload["cpg_calls"][1]["position0"] = payload["cpg_calls"][0]["position0"]
    payload["cpg_calls"][1]["strand"] = payload["cpg_calls"][0]["strand"]
    with pytest.raises(ValidationError, match="unique"):
        validate_json(FragmentMarkerObservation, payload)


def test_v2_cpg_call_preserves_original_and_canonical_coordinates() -> None:
    plus = validate_json(ModkitCpgCallV2, _modkit_call())
    minus = validate_json(
        ModkitCpgCallV2,
        _modkit_call(
            original_position0=101,
            canonical_cpg_position0=100,
            modification_strand="-",
            reference_mod_strand="-",
        )
    )

    assert plus.original_position0 == plus.canonical_cpg_position0
    assert minus.original_position0 - 1 == minus.canonical_cpg_position0
    assert minus.modification_strand == minus.reference_mod_strand

    with pytest.raises(ValidationError, match="canonical CpG position"):
        validate_json(
            ModkitCpgCallV2,
            _modkit_call(
                original_position0=101,
                canonical_cpg_position0=101,
                reference_mod_strand="-",
            )
        )
    with pytest.raises(ValidationError, match="underflow"):
        validate_json(
            ModkitCpgCallV2,
            _modkit_call(
                original_position0=0,
                canonical_cpg_position0=0,
                reference_mod_strand="-",
            )
        )


def test_v1_and_v2_cpg_calls_cannot_be_relabelled() -> None:
    v1 = fixture_payload("fragment_observation.json")["cpg_calls"][0]
    assert validate_json(ModkitCpgCall, v1).position0 == 110
    with pytest.raises(ValidationError):
        validate_json(ModkitCpgCallV2, v1)
    with pytest.raises(ValidationError):
        validate_json(ModkitCpgCall, _modkit_call())

    payload = _modkit_call()
    payload["modified_probability"] = payload.pop("selected_state_probability")
    with pytest.raises(ValidationError, match="selected_state_probability"):
        validate_json(ModkitCpgCallV2, payload)


def test_modkit_v2_provenance_binds_generic_schema_and_probability_policy() -> None:
    combined = validate_json(ModkitInputProvenanceV2, _modkit_provenance())
    assert combined.probability_threshold == 0.7
    assert combined.probability_sum_tolerance == 1e-6

    zero_threshold = _modkit_provenance()
    zero_threshold["probability_threshold"] = 0.0
    assert (
        validate_json(
            ModkitInputProvenanceV2, zero_threshold
        ).probability_threshold
        == 0.0
    )

    hard = validate_json(
        ModkitInputProvenanceV2,
        _modkit_provenance("hard_call_collapsed_m_h")
    )
    assert hard.probability_threshold is None

    mismatched = _modkit_provenance()
    mismatched["source_schema_id"] = "traceback.generic-hard-call-cpg.v2"
    with pytest.raises(ValidationError, match="generic C/m/h schema"):
        validate_json(ModkitInputProvenanceV2, mismatched)

    threshold_claim = _modkit_provenance("hard_call_collapsed_m_h")
    threshold_claim["probability_threshold"] = 0.7
    with pytest.raises(ValidationError, match="cannot claim"):
        validate_json(ModkitInputProvenanceV2, threshold_claim)

    for field, value in (
        ("probability_tie_tolerance", 1e-9),
        ("probability_sum_tolerance", 1e-5),
    ):
        changed = _modkit_provenance()
        changed[field] = value
        with pytest.raises(ValidationError, match=field):
            validate_json(ModkitInputProvenanceV2, changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("total_rows", 6, "total rows"),
        ("source_passed_rows", 5, "total rows"),
        ("candidate_c_rows", 4, "source-passed"),
        ("eligible_call_rows", 3, "terminal buckets"),
        ("reference_plus_call_rows", 2, "reference-strand counts"),
        ("probability_input_rows", 2, "probability-input rows"),
        ("malformed_rows", 1, "malformed_rows"),
        ("duplicate_rows", 1, "duplicate_rows"),
    ],
)
def test_modkit_v2_ledger_reconciles_every_stage(
    field: str,
    value: int,
    message: str,
) -> None:
    payload = _modkit_ledger()
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        validate_json(ModkitIngestionLedgerV2, payload)


def test_hard_call_ledger_cannot_claim_probability_filtering() -> None:
    hard = _modkit_ledger("hard_call_collapsed_m_h")
    ledger = validate_json(ModkitIngestionLedgerV2, hard)
    hard_state_rows = (
        ledger.hard_call_c_rows
        + ledger.hard_call_m_rows
        + ledger.hard_call_h_rows
    )
    assert hard_state_rows == 3

    hard["excluded_low_confidence_rows"] = 1
    hard["eligible_call_rows"] = 2
    hard["methylated_call_rows"] = 1
    hard["reference_minus_call_rows"] = 1
    with pytest.raises(ValidationError, match="cannot claim"):
        validate_json(ModkitIngestionLedgerV2, hard)


def test_hard_call_ledger_binds_source_states_to_emitted_states() -> None:
    forged = _modkit_ledger("hard_call_collapsed_m_h")
    forged.update(
        total_rows=1,
        source_failed_rows=0,
        source_passed_rows=1,
        excluded_non_c_rows=0,
        candidate_c_rows=1,
        hard_call_c_rows=1,
        hard_call_m_rows=0,
        hard_call_h_rows=0,
        eligible_call_rows=1,
        unmethylated_call_rows=0,
        methylated_call_rows=1,
        reference_plus_call_rows=1,
        reference_minus_call_rows=0,
    )

    with pytest.raises(ValidationError, match="C rows must equal"):
        validate_json(ModkitIngestionLedgerV2, forged)


def test_modkit_v2_result_reconciles_calls_without_collapsing_strands() -> None:
    calls = [
        _modkit_call(),
        _modkit_call(
            original_position0=101,
            canonical_cpg_position0=100,
            modification_strand="+",
            reference_mod_strand="-",
            selected_state_probability=0.9,
            state="unmethylated",
        ),
    ]
    result = validate_json(
        ModkitInputResultV2,
        {
            "schema_version": "cell-origin-modkit-input.v2",
            "provenance": _modkit_provenance(),
            "ledger": _modkit_ledger(),
            "calls": calls,
        }
    )
    assert len(result.calls) == 2
    assert result.calls[0].canonical_cpg_position0 == (
        result.calls[1].canonical_cpg_position0
    )

    duplicate = result.model_dump(mode="python")
    duplicate["calls"] = list(duplicate["calls"])
    duplicate["calls"][1] = duplicate["calls"][0]
    duplicate["ledger"]["reference_plus_call_rows"] = 2
    duplicate["ledger"]["reference_minus_call_rows"] = 0
    duplicate["ledger"]["unmethylated_call_rows"] = 0
    duplicate["ledger"]["methylated_call_rows"] = 2
    duplicate["calls"] = tuple(duplicate["calls"])
    with pytest.raises(ValidationError, match="exact call observations"):
        ModkitInputResultV2.model_validate(duplicate)


@pytest.mark.parametrize(
    ("probability", "message"),
    [
        (0.5, "exact probability ties"),
        (0.4, "must exceed 0.5"),
        (0.6, "below the bound threshold"),
    ],
)
def test_probability_calls_must_follow_bound_threshold_and_tie_policy(
    probability: float,
    message: str,
) -> None:
    payload = {
        "schema_version": "cell-origin-modkit-input.v2",
        "provenance": _modkit_provenance(),
        "ledger": _modkit_ledger(),
        "calls": [
            _modkit_call(selected_state_probability=probability),
            _modkit_call(
                original_position0=101,
                canonical_cpg_position0=100,
                reference_mod_strand="-",
                selected_state_probability=0.9,
                state="unmethylated",
            ),
        ],
    }
    with pytest.raises(ValidationError, match=message):
        validate_json(ModkitInputResultV2, payload)


def test_marker_count_row_reconciles_uxm_and_denominator() -> None:
    row = MarkerCountRow(
        marker_id="marker.1",
        u_count=2,
        x_count=1,
        m_count=1,
        classified_fragment_count=4,
        u_fraction=0.5,
    )
    assert row.classified_fragment_count == 4

    with pytest.raises(ValidationError, match=r"U \+ X \+ M"):
        MarkerCountRow(
            marker_id="marker.1",
            u_count=2,
            x_count=1,
            m_count=1,
            classified_fragment_count=5,
            u_fraction=0.5,
        )
    with pytest.raises(ValidationError, match="all classified"):
        MarkerCountRow(
            marker_id="marker.1",
            u_count=2,
            x_count=1,
            m_count=1,
            classified_fragment_count=4,
            u_fraction=0.25,
        )


def test_loyfer_and_katsman_method_components_cannot_be_mixed() -> None:
    assert LOYFER_UXM_METHOD != KATSMAN_METHATLAS_METHOD
    payload = LOYFER_UXM_METHOD.model_dump(mode="json")
    payload["atlas_kind"] = "methatlas"
    with pytest.raises(ValidationError, match="scientifically compatible"):
        validate_json(MethodDefinition, payload)

    matrix = fixture_payload("atlas_u_matrix.json")
    matrix["method"] = KATSMAN_METHATLAS_METHOD.model_dump(mode="json")
    with pytest.raises(ValidationError, match="specifically a Loyfer"):
        validate_json(AtlasUMatrix, matrix)


def test_atlas_matrix_requires_declared_order_and_unique_markers() -> None:
    payload = fixture_payload("atlas_u_matrix.json")
    payload["rows"][0]["values"].reverse()
    with pytest.raises(ValidationError, match="declared cell types in order"):
        validate_json(AtlasUMatrix, payload)

    payload = fixture_payload("atlas_u_matrix.json")
    payload["rows"][1]["marker_id"] = payload["rows"][0]["marker_id"]
    with pytest.raises(ValidationError, match="marker_id values must be unique"):
        validate_json(AtlasUMatrix, payload)


def test_local_input_bundle_cross_validates_marker_registry() -> None:
    markers = fixture_payload("markers.json")
    observation = fixture_payload("fragment_observation.json")
    matrix = fixture_payload("atlas_u_matrix.json")
    payload = {
        "schema_version": "cell-origin-input.v1",
        "method": LOYFER_UXM_METHOD.model_dump(mode="json"),
        "markers": markers,
        "observations": [observation],
        "atlas_u_matrix": matrix,
    }
    bundle = validate_json(CellOriginInputBundle, payload)
    assert bundle.atlas_u_matrix.atlas_id == "atlas.synthetic-loyfer.v1"

    payload["markers"][1]["atlas_id"] = "atlas.other"
    with pytest.raises(ValidationError, match="every marker"):
        validate_json(CellOriginInputBundle, payload)


def test_deconvolution_requires_canonical_normalized_fractions() -> None:
    payload = fixture_payload("result.json")
    payload["deconvolution"]["estimates"][0]["fraction"] = 33.333
    with pytest.raises(ValidationError):
        validate_json(CellOriginResult, payload)

    payload = fixture_payload("result.json")
    payload["deconvolution"]["estimates"][0]["fraction"] = 0.2
    with pytest.raises(ValidationError, match="sum to 1"):
        validate_json(CellOriginResult, payload)


def test_v2_deconvolution_requires_explicit_row_scale_and_schema() -> None:
    v1 = fixture_payload("result.json")["deconvolution"]
    with pytest.raises(ValidationError, match="schema_version|row_scale"):
        validate_json(DeconvolutionOutputV2, v1)

    missing_scale = {
        **v1,
        "schema_version": "cell-origin-deconvolution.v2",
    }
    with pytest.raises(ValidationError, match="row_scale"):
        validate_json(DeconvolutionOutputV2, missing_scale)

    wrong_schema = {
        **v1,
        "schema_version": "cell-origin-deconvolution.v1",
        "diagnostics": {**v1["diagnostics"], **V2_SOLVER_DIAGNOSTICS},
    }
    with pytest.raises(ValidationError, match="cell-origin-deconvolution.v2"):
        validate_json(DeconvolutionOutputV2, wrong_schema)


@pytest.mark.parametrize("row_scale", list(NnlsRowScale))
def test_v2_deconvolution_round_trips_each_row_scale(
    row_scale: NnlsRowScale,
) -> None:
    payload = fixture_payload("result.json")["deconvolution"]
    payload["schema_version"] = "cell-origin-deconvolution.v2"
    payload["atlas_sha256"] = V2_ATLAS_SHA256
    payload["diagnostics"].update(V2_SOLVER_DIAGNOSTICS)
    payload["diagnostics"]["row_scale"] = row_scale.value

    output = validate_json(DeconvolutionOutputV2, payload)

    assert output.schema_version == "cell-origin-deconvolution.v2"
    assert output.diagnostics.row_scale == row_scale
    assert (
        DeconvolutionOutputV2.model_validate_json(output.model_dump_json())
        == output
    )


def test_v1_and_v2_deconvolution_boundaries_do_not_silently_coerce() -> None:
    v1 = fixture_payload("result.json")["deconvolution"]
    assert validate_json(DeconvolutionOutput, v1).diagnostics.converged

    v2 = {
        **v1,
        "schema_version": "cell-origin-deconvolution.v2",
        "atlas_sha256": V2_ATLAS_SHA256,
        "diagnostics": {
            **v1["diagnostics"],
            **V2_SOLVER_DIAGNOSTICS,
            "row_scale": "reference_count",
        },
    }
    with pytest.raises(ValidationError, match="schema_version|row_scale"):
        validate_json(DeconvolutionOutput, v2)

    invalid_scale = {
        **v1,
        "schema_version": "cell-origin-deconvolution.v2",
        "atlas_sha256": V2_ATLAS_SHA256,
        "diagnostics": {
            **v1["diagnostics"],
            **V2_SOLVER_DIAGNOSTICS,
            "row_scale": "variance_weighted",
        },
    }
    with pytest.raises(ValidationError, match="row_scale"):
        validate_json(DeconvolutionOutputV2, invalid_scale)


@pytest.mark.parametrize(
    "field",
    ["solver_tolerance", "max_iterations", "solver_implementation_id"],
)
def test_v2_deconvolution_requires_effective_solver_configuration(
    field: str,
) -> None:
    payload = fixture_payload("result.json")["deconvolution"]
    payload["schema_version"] = "cell-origin-deconvolution.v2"
    payload["atlas_sha256"] = V2_ATLAS_SHA256
    payload["diagnostics"].update(V2_SOLVER_DIAGNOSTICS)
    del payload["diagnostics"][field]

    with pytest.raises(ValidationError, match=field):
        validate_json(DeconvolutionOutputV2, payload)


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"solver_tolerance": 0.0}, "solver_tolerance"),
        ({"solver_tolerance": float("nan")}, "solver_tolerance"),
        ({"max_iterations": 0}, "max_iterations"),
        ({"solver_implementation_id": "contains spaces"}, "solver_implementation_id"),
    ],
)
def test_v2_deconvolution_rejects_invalid_solver_configuration(
    updates: dict[str, object],
    match: str,
) -> None:
    payload = fixture_payload("result.json")["deconvolution"]
    payload["schema_version"] = "cell-origin-deconvolution.v2"
    payload["atlas_sha256"] = V2_ATLAS_SHA256
    payload["diagnostics"].update(V2_SOLVER_DIAGNOSTICS)
    payload["diagnostics"].update(updates)

    with pytest.raises(ValidationError, match=match):
        validate_json(DeconvolutionOutputV2, payload)


def test_v2_deconvolution_requires_valid_atlas_digest() -> None:
    payload = fixture_payload("result.json")["deconvolution"]
    payload["schema_version"] = "cell-origin-deconvolution.v2"
    payload["diagnostics"].update(V2_SOLVER_DIAGNOSTICS)

    with pytest.raises(ValidationError, match="atlas_sha256"):
        validate_json(DeconvolutionOutputV2, payload)

    payload["atlas_sha256"] = "not-a-sha256"
    with pytest.raises(ValidationError, match="atlas_sha256"):
        validate_json(DeconvolutionOutputV2, payload)


def test_bootstrap_and_range_rows_are_bound_to_estimates() -> None:
    payload = fixture_payload("result.json")
    payload["bootstrap"]["intervals"][0]["lower_fraction"] = 0.4
    with pytest.raises(ValidationError, match="contain its estimate"):
        validate_json(CellOriginResult, payload)

    payload = fixture_payload("result.json")
    payload["range_comparison"]["rows"][0]["classification"] = "within"
    with pytest.raises(ValidationError, match="inclusive range"):
        validate_json(CellOriginResult, payload)


@pytest.mark.parametrize(
    ("fraction", "minimum", "maximum", "classification"),
    [
        (0.1, 0.1, 0.2, RangeClassification.WITHIN),
        (0.2, 0.1, 0.2, RangeClassification.WITHIN),
        (0.09, 0.1, 0.2, RangeClassification.BELOW),
        (0.21, 0.1, 0.2, RangeClassification.ABOVE),
    ],
)
def test_reference_ranges_are_inclusive_and_canonical(
    fraction: float,
    minimum: float,
    maximum: float,
    classification: RangeClassification,
) -> None:
    row = ReferenceRangeRow(
        cell_type_id="immune",
        fraction=fraction,
        min_fraction=minimum,
        max_fraction=maximum,
        classification=classification,
        source_ids=("source.slide",),
    )
    assert row.classification == classification


def test_provenance_counts_reconcile() -> None:
    payload = fixture_payload("result.json")
    payload["provenance"]["classified_fragment_marker_count"] = 11
    payload["provenance"]["excluded_fewer_than_four_cpgs"] = 2
    with pytest.raises(ValidationError, match="exceed marker overlaps"):
        validate_json(CellOriginResult, payload)


def test_failed_validation_cannot_be_published() -> None:
    payload = fixture_payload("result.json")
    payload["validation"]["records"][0]["passed"] = False
    with pytest.raises(ValidationError, match="cannot cross"):
        validate_json(CellOriginResult, payload)


def test_result_rejects_method_or_cell_type_mismatch() -> None:
    payload = fixture_payload("result.json")
    payload["deconvolution"]["method"] = KATSMAN_METHATLAS_METHOD.model_dump(
        mode="json"
    )
    with pytest.raises(ValidationError, match="does not match result"):
        validate_json(CellOriginResult, payload)

    payload = fixture_payload("result.json")
    payload["bootstrap"]["intervals"][0]["cell_type_id"] = "neuron"
    with pytest.raises(ValidationError, match="cell types must match"):
        validate_json(CellOriginResult, payload)


def walk_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [key for key in value] + [
            key for child in value.values() for key in walk_keys(child)
        ]
    if isinstance(value, list):
        return [key for child in value for key in walk_keys(child)]
    return []


def test_published_result_is_aggregate_and_model_safe() -> None:
    result = CellOriginResult.model_validate_json(fixture_bytes("result.json"))
    payload = result.model_dump(mode="json")
    keys = set(walk_keys(payload))

    assert "fragment_digest" not in keys
    assert "read_id" not in keys
    assert "path" not in keys
    serialized = result.model_dump_json()
    assert "/Users/" not in serialized
    assert "fragment_observation" not in serialized
    assert "cpg_calls" not in serialized


def test_strict_contract_rejects_percent_strings() -> None:
    payload = fixture_payload("result.json")
    payload["marker_counts"][0]["u_fraction"] = "40%"
    with pytest.raises(ValidationError):
        validate_json(CellOriginResult, payload)
