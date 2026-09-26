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
    FragmentMarkerObservation,
    GenomicMarker,
    KATSMAN_METHATLAS_METHOD,
    LOYFER_UXM_METHOD,
    MarkerCountRow,
    MethodDefinition,
    RangeClassification,
    ReferenceRangeRow,
    UxmState,
    UxmThresholds,
    classify_uxm,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cell_origin"


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
