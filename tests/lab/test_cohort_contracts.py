"""Synthetic contract checks; no donor or real-run values."""

import pytest
from pydantic import ValidationError

from traceback_lab.asset_contracts import AssetDescriptor
from traceback_lab.cohort_contracts import (
    CohortGeneration,
    CohortMember,
    CohortResult,
    project_metric,
)


def _sha(index: int) -> str:
    return f"{index:064x}"


def _member(index: int, *, compat: int = 7, value: float | None = None) -> CohortMember:
    return CohortMember(
        source_id=_sha(index + 1),
        input_id=_sha(index + 1000),
        aligner_identity="pre-aligned" if index % 2 else "minimap2:2.28",
        results=(
            CohortResult(
                algorithm_id="fragment-size",
                parameters_sha256=_sha(index + 2000),
                cohort_compat_key=_sha(compat),
                metrics={"mode_bp": float(index) if value is None else value},
            ),
        ),
    )


def _generation(n: int, *, kind: str = "internal_cohort") -> CohortGeneration:
    return CohortGeneration(cohort_id="kit-a", version=1, kind=kind, members=tuple(_member(i) for i in range(n)))


def test_asset_descriptor_is_exact_bytes_with_separate_terms() -> None:
    asset = AssetDescriptor(
        kind="pmd", sha256=_sha(12), size_bytes=123,
        origin="user", source_label="synthetic fixture", terms="unknown",
    )
    assert asset.asset_id == f"pmd:{_sha(12)}"
    assert "path" not in asset.model_dump()
    with pytest.raises(ValidationError, match="license_id"):
        AssetDescriptor(kind="pmd", sha256=_sha(12), size_bytes=123,
                        origin="user", source_label="fixture", terms="known")


@pytest.mark.parametrize("n,expected", [(0, 0), (9, 9), (10, 10), (29, 29), (30, 30)])
def test_projection_preserves_independent_n(n: int, expected: int) -> None:
    projection = project_metric(_generation(n), algorithm_id="fragment-size", metric_id="mode_bp",
                                cohort_compat_key=_sha(7))
    assert projection.percentile_n == expected
    assert projection.interpretation_cohort().n == expected
    assert sum(projection.aligner_counts.values()) == expected


def test_leave_one_out_applies_before_threshold() -> None:
    generation = _generation(10)
    projection = project_metric(generation, algorithm_id="fragment-size", metric_id="mode_bp",
                                cohort_compat_key=_sha(7), target_source_id=_sha(1))
    assert projection.percentile_n == 9
    assert projection.exclusions == {"self": 1}
    assert _sha(1) not in projection.source_ids


def test_exact_execution_identity_does_not_define_comparability() -> None:
    generation = _generation(2)
    projection = project_metric(generation, algorithm_id="fragment-size", metric_id="mode_bp",
                                cohort_compat_key=_sha(7))
    assert projection.percentile_n == 2
    assert generation.members[0].results[0].parameters_sha256 != generation.members[1].results[0].parameters_sha256


def test_incompatibility_and_missing_values_are_explained() -> None:
    missing = CohortMember(source_id=_sha(3), input_id=_sha(1003), aligner_identity="pre-aligned", results=())
    missing_metric = CohortMember(
        source_id=_sha(4), input_id=_sha(1004), aligner_identity="pre-aligned",
        results=(CohortResult(algorithm_id="fragment-size", parameters_sha256=_sha(2004),
                              cohort_compat_key=_sha(7), metrics={"mode_bp": None}),),
    )
    members = (_member(0), _member(1, compat=8), missing, missing_metric)
    generation = CohortGeneration(cohort_id="kit-a", version=1, members=members)
    projection = project_metric(generation, algorithm_id="fragment-size", metric_id="mode_bp",
                                cohort_compat_key=_sha(7))
    assert projection.percentile_n == 1
    assert projection.exclusions == {"incompatible": 1, "missing_metric": 1, "missing_result": 1}


def test_duplicate_samples_cannot_inflate_generation() -> None:
    with pytest.raises(ValidationError, match="duplicate source_id"):
        CohortGeneration(cohort_id="kit-a", version=1, members=(_member(0), _member(0)))


def test_provisional_external_remains_visible() -> None:
    projection = project_metric(_generation(10, kind="provisional_external"),
                                algorithm_id="fragment-size", metric_id="mode_bp", cohort_compat_key=_sha(7))
    assert projection.kind == "provisional_external"
    assert projection.interpretation_cohort().kind == "provisional_external"

@pytest.mark.parametrize("bad", [True, "0.5", float("nan"), float("inf")])
def test_metric_values_require_finite_numbers(bad: object) -> None:
    with pytest.raises(ValidationError):
        CohortResult(algorithm_id="fragment-size", parameters_sha256=_sha(3),
                     cohort_compat_key=_sha(7), metrics={"mode_bp": bad})


def test_nested_cohort_values_cannot_mutate_after_validation() -> None:
    result = CohortResult(algorithm_id="fragment-size", parameters_sha256=_sha(3),
                          cohort_compat_key=_sha(7), metrics={"mode_bp": 100.0})
    with pytest.raises(TypeError):
        result.metrics["mode_bp"] = float("nan")
    generation = CohortGeneration(cohort_id="kit-a", version=1,
                                  members=(CohortMember(source_id=_sha(1), input_id=_sha(2),
                                                        aligner_identity="pre-aligned", results=(result,)),))
    restored = CohortGeneration.model_validate_json(generation.model_dump_json(by_alias=True))
    assert restored == generation
    projection = project_metric(restored, algorithm_id="fragment-size", metric_id="mode_bp",
                                cohort_compat_key=_sha(7))
    with pytest.raises(TypeError):
        projection.exclusions["self"] = 1
    assert projection.model_dump(mode="json")["values"] == [100.0]


def test_asset_descriptor_dump_roundtrips_and_rejects_bad_digest() -> None:
    asset = AssetDescriptor(kind="pmd", sha256=_sha(15), size_bytes=42,
                            origin="user", source_label="fixture", terms="unknown")
    assert AssetDescriptor.model_validate(asset.model_dump()) == asset
    assert AssetDescriptor.model_validate_json(asset.model_dump_json(by_alias=True)) == asset
    with pytest.raises(ValidationError):
        AssetDescriptor(kind="pmd", sha256="not-a-digest", size_bytes=42,
                        origin="user", source_label="fixture", terms="unknown")


def test_member_rejects_duplicate_algorithm_and_pon_overcount() -> None:
    result = _member(0).results[0]
    with pytest.raises(ValidationError, match="one selected result per algorithm"):
        CohortMember(source_id=_sha(1), input_id=_sha(100), aligner_identity="pre-aligned",
                     results=(result, result))
    with pytest.raises(ValidationError, match="pon_n"):
        CohortGeneration(cohort_id="kit-a", version=1, members=(_member(0),), pon_n=2)
