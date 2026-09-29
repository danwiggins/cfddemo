"""Tests for versioned Loyfer UXM deconvolution objectives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evidence_inspector.deconvolution as deconvolution_module
from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    AtlasUMatrixRow,
    AtlasUValue,
    BootstrapInformationStatus,
    BootstrapResult,
    BootstrapResultV2,
    LOYFER_UXM_METHOD,
    MarkerCountRow,
    NnlsRowScale,
    RangeClassification,
)
from evidence_inspector.deconvolution import (
    DeconvolutionError,
    NNLS_SOLVER_IMPLEMENTATION_ID,
    ObservedCohortRange,
    bootstrap_uxm,
    bootstrap_uxm_v2,
    compare_observed_cohort_ranges,
    deconvolve_uxm,
    deconvolve_uxm_v2,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cell_origin"


def fixture_atlas() -> AtlasUMatrix:
    return AtlasUMatrix.model_validate_json(
        (FIXTURES / "atlas_u_matrix.json").read_bytes()
    )


def marker_count(
    marker_id: str,
    u_count: int,
    total: int,
) -> MarkerCountRow:
    return MarkerCountRow(
        marker_id=marker_id,
        u_count=u_count,
        x_count=total - u_count,
        m_count=0,
        classified_fragment_count=total,
        u_fraction=u_count / total,
    )


def test_hand_solvable_mixture_recovers_nonnegative_fractions() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )

    result = deconvolve_uxm(counts, atlas, result_id="nnls.hand.v1")

    assert [item.fraction for item in result.estimates] == pytest.approx(
        [1 / 3, 2 / 3]
    )
    assert [item.raw_nnls_weight for item in result.estimates] == pytest.approx(
        [1 / 3, 2 / 3]
    )
    assert result.marker_ids == tuple(row.marker_id for row in counts)
    assert result.method == LOYFER_UXM_METHOD
    assert result.diagnostics.converged
    assert result.diagnostics.residual_l2 == pytest.approx(0.0, abs=1e-12)
    assert result.diagnostics.objective_value == pytest.approx(0.0, abs=1e-12)


def test_versioned_row_scales_match_independent_analytic_solutions() -> None:
    atlas = AtlasUMatrix(
        atlas_id="atlas.weighted.v1",
        method=LOYFER_UXM_METHOD,
        cell_type_ids=("a",),
        rows=(
            AtlasUMatrixRow(
                marker_id="m1",
                values=(AtlasUValue(cell_type_id="a", u_fraction=1.0),),
            ),
            AtlasUMatrixRow(
                marker_id="m2",
                values=(AtlasUValue(cell_type_id="a", u_fraction=1.0),),
            ),
        ),
        source_ids=("source.atlas",),
    )
    counts = (
        marker_count("m1", 9, 10),
        marker_count("m2", 10, 100),
    )

    historical_v1 = deconvolve_uxm(counts, atlas)
    historical_v2 = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.SQRT_COUNT,
    )
    reference = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.REFERENCE_COUNT,
    )
    unweighted = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.UNWEIGHTED,
    )

    expected_historical = (10 * 0.9 + 100 * 0.1) / 110
    assert historical_v1.estimates[0].raw_nnls_weight == pytest.approx(
        expected_historical
    )
    assert historical_v2.estimates[0].raw_nnls_weight == pytest.approx(
        expected_historical
    )
    assert historical_v2.schema_version == "cell-origin-deconvolution.v2"
    assert historical_v2.diagnostics.row_scale == NnlsRowScale.SQRT_COUNT
    assert historical_v2.diagnostics.solver_tolerance == 1e-12
    assert historical_v2.diagnostics.max_iterations == 10_000
    assert (
        historical_v2.diagnostics.solver_implementation_id
        == NNLS_SOLVER_IMPLEMENTATION_ID
    )
    assert reference.estimates[0].raw_nnls_weight == pytest.approx(
        (10**2 * 0.9 + 100**2 * 0.1) / (10**2 + 100**2)
    )
    assert reference.diagnostics.row_scale == NnlsRowScale.REFERENCE_COUNT
    assert unweighted.estimates[0].raw_nnls_weight == pytest.approx(0.5)
    assert unweighted.diagnostics.row_scale == NnlsRowScale.UNWEIGHTED
    assert historical_v1.result_id != historical_v2.result_id
    assert historical_v2.result_id != reference.result_id
    assert reference.result_id != unweighted.result_id
    assert all(
        result.estimates[0].fraction == 1.0
        for result in (historical_v1, historical_v2, reference, unweighted)
    )


def test_boundary_solution_keeps_zero_component() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 8, 10),
        marker_count("marker.liver.1", 2, 10),
    )

    result = deconvolve_uxm(counts, atlas)

    assert [item.raw_nnls_weight for item in result.estimates] == pytest.approx(
        [1.0, 0.0],
        abs=1e-12,
    )
    assert [item.fraction for item in result.estimates] == pytest.approx(
        [1.0, 0.0],
        abs=1e-12,
    )


def test_marker_rows_can_be_reordered_but_sets_must_match() -> None:
    atlas = fixture_atlas()
    reversed_counts = (
        marker_count("marker.liver.1", 6, 10),
        marker_count("marker.immune.1", 4, 10),
    )

    result = deconvolve_uxm(reversed_counts, atlas)

    assert result.marker_ids == ("marker.liver.1", "marker.immune.1")
    assert [item.fraction for item in result.estimates] == pytest.approx(
        [1 / 3, 2 / 3]
    )


@pytest.mark.parametrize(
    "counts",
    [
        (marker_count("marker.immune.1", 4, 10),),
        (
            marker_count("marker.immune.1", 4, 10),
            marker_count("unknown", 6, 10),
        ),
    ],
)
def test_marker_mismatch_fails_closed(
    counts: tuple[MarkerCountRow, ...],
) -> None:
    with pytest.raises(DeconvolutionError, match="identical marker IDs"):
        deconvolve_uxm(counts, fixture_atlas())


def test_singular_atlas_is_deterministic_and_nonnegative() -> None:
    atlas = AtlasUMatrix(
        atlas_id="atlas.singular.v1",
        method=LOYFER_UXM_METHOD,
        cell_type_ids=("first", "second"),
        rows=(
            AtlasUMatrixRow(
                marker_id="m1",
                values=(
                    AtlasUValue(cell_type_id="first", u_fraction=0.8),
                    AtlasUValue(cell_type_id="second", u_fraction=0.8),
                ),
            ),
            AtlasUMatrixRow(
                marker_id="m2",
                values=(
                    AtlasUValue(cell_type_id="first", u_fraction=0.2),
                    AtlasUValue(cell_type_id="second", u_fraction=0.2),
                ),
            ),
        ),
        source_ids=("source.atlas",),
    )
    counts = (marker_count("m1", 8, 10), marker_count("m2", 2, 10))

    first = deconvolve_uxm(counts, atlas)
    second = deconvolve_uxm(counts, atlas)

    assert first == second
    assert first.estimates[0].fraction == pytest.approx(1.0)
    assert first.estimates[1].fraction == pytest.approx(0.0)
    assert all(item.raw_nnls_weight >= 0 for item in first.estimates)


def test_zero_signal_cannot_be_normalized() -> None:
    counts = (
        marker_count("marker.immune.1", 0, 10),
        marker_count("marker.liver.1", 0, 10),
    )

    with pytest.raises(DeconvolutionError, match="zero U-fraction signal"):
        deconvolve_uxm(counts, fixture_atlas())


def test_unknown_row_scale_fails_closed() -> None:
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )

    with pytest.raises(DeconvolutionError, match="unsupported NNLS row scale"):
        deconvolve_uxm_v2(
            counts,
            fixture_atlas(),
            row_scale="invented",  # type: ignore[arg-type]
        )


def test_v2_row_scale_is_required() -> None:
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )

    with pytest.raises(TypeError, match="row_scale"):
        deconvolve_uxm_v2(counts, fixture_atlas())  # type: ignore[call-arg]


def test_v2_identity_binds_solver_settings_and_full_atlas() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )
    changed_payload = atlas.model_dump(mode="python")
    changed_payload["rows"][0]["values"][0]["u_fraction"] = 0.79
    changed_atlas = AtlasUMatrix.model_validate(changed_payload)

    baseline = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.REFERENCE_COUNT
    )
    repeated = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.REFERENCE_COUNT
    )
    changed_tolerance = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.REFERENCE_COUNT,
        tolerance=1e-10,
    )
    changed_iterations = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.REFERENCE_COUNT,
        max_iterations=9_999,
    )
    changed_matrix = deconvolve_uxm_v2(
        counts,
        changed_atlas,
        row_scale=NnlsRowScale.REFERENCE_COUNT,
    )

    assert repeated == baseline
    assert changed_atlas.atlas_id == atlas.atlas_id
    assert changed_matrix.atlas_sha256 != baseline.atlas_sha256
    assert len(baseline.atlas_sha256) == 64
    assert len(
        {
            baseline.result_id,
            changed_tolerance.result_id,
            changed_iterations.result_id,
            changed_matrix.result_id,
        }
    ) == 4


def test_result_id_is_deterministic_and_input_bound() -> None:
    atlas = fixture_atlas()
    first_counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )
    changed_counts = (
        marker_count("marker.immune.1", 5, 10),
        marker_count("marker.liver.1", 6, 10),
    )

    first = deconvolve_uxm(first_counts, atlas)
    repeated = deconvolve_uxm(first_counts, atlas)
    changed = deconvolve_uxm(changed_counts, atlas)

    assert first.result_id == repeated.result_id
    assert first.result_id != changed.result_id


def test_seeded_bootstrap_is_reproducible_and_bound_to_source() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm(counts, atlas)

    first = bootstrap_uxm(
        counts,
        atlas,
        source,
        replicates=40,
        random_seed=7,
    )
    repeated = bootstrap_uxm(
        counts,
        atlas,
        source,
        replicates=40,
        random_seed=7,
    )
    changed_seed = bootstrap_uxm(
        counts,
        atlas,
        source,
        replicates=40,
        random_seed=8,
    )

    assert first == repeated
    assert first != changed_seed
    assert first.source_result_id == source.result_id
    assert all(
        item.lower_fraction <= item.estimate <= item.upper_fraction
        for item in first.intervals
    )


def test_v2_bootstrap_seed_and_replay_are_deterministic() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )

    first = bootstrap_uxm_v2(
        counts, atlas, source, replicates=100, random_seed=7
    )
    replay = bootstrap_uxm_v2(
        counts, atlas, source, replicates=100, random_seed=7
    )
    changed = bootstrap_uxm_v2(
        counts, atlas, source, replicates=100, random_seed=8
    )

    assert first == replay
    assert BootstrapResultV2.model_validate_json(first.model_dump_json()) == first
    assert first != changed
    assert first.diagnostics.requested_resamples == 100
    assert (
        first.diagnostics.successful_resamples
        + first.diagnostics.failed_resamples
        + first.diagnostics.degenerate_resamples
        == 100
    )
    assert not first.diagnostics.preserves_cross_marker_molecule_linkage


def test_v2_bootstrap_uses_and_records_source_solver_configuration() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts,
        atlas,
        row_scale=NnlsRowScale.REFERENCE_COUNT,
        tolerance=1e-10,
        max_iterations=321,
    )

    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=100, random_seed=7
    )

    assert result.diagnostics.nnls_row_scale == NnlsRowScale.REFERENCE_COUNT
    assert result.diagnostics.solver_tolerance == 1e-10
    assert result.diagnostics.max_iterations == 321
    assert (
        result.diagnostics.solver_implementation_id
        == NNLS_SOLVER_IMPLEMENTATION_ID
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"tolerance": 0.0}, "tolerance"),
        ({"max_iterations": 0}, "max_iterations"),
        ({"tolerance": 1e-9}, "must match"),
        ({"max_iterations": 999}, "must match"),
    ),
)
def test_v2_bootstrap_rejects_invalid_or_mismatched_solver_config_before_sampling(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    message: str,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )

    def sampling_must_not_start(_seed: int) -> object:
        raise AssertionError("sampling started before configuration validation")

    monkeypatch.setattr(
        deconvolution_module.np.random,
        "default_rng",
        sampling_must_not_start,
    )
    with pytest.raises(DeconvolutionError, match=message):
        bootstrap_uxm_v2(
            counts,
            atlas,
            source,
            replicates=100,
            random_seed=7,
            **kwargs,  # type: ignore[arg-type]
        )


def test_v2_bootstrap_rejects_unversioned_source() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    legacy = deconvolve_uxm(counts, atlas)

    with pytest.raises(DeconvolutionError, match="versioned deconvolution"):
        bootstrap_uxm_v2(  # type: ignore[arg-type]
            counts, atlas, legacy, replicates=100, random_seed=7
        )


class _DrawSequence:
    def __init__(self, draws: list[list[int]]) -> None:
        self._draws = iter(draws)

    def binomial(self, *_args: object, **_kwargs: object) -> object:
        return deconvolution_module.np.asarray(next(self._draws), dtype=int)


def test_v2_bootstrap_zero_success_reports_insufficient_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )

    def fail_solve(*_args: object, **_kwargs: object) -> object:
        raise DeconvolutionError("synthetic solver failure")

    monkeypatch.setattr(deconvolution_module, "_solve_arrays", fail_solve)
    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=3, random_seed=7
    )

    assert result.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )
    assert result.diagnostics.successful_resamples == 0
    assert result.diagnostics.failed_resamples == 3
    assert result.diagnostics.degenerate_resamples == 0
    assert all(item.lower_fraction is None for item in result.intervals)


def test_v2_bootstrap_all_degenerate_resamples_are_accounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )
    monkeypatch.setattr(
        deconvolution_module.np.random,
        "default_rng",
        lambda _seed: _DrawSequence([[0, 0], [0, 0], [0, 0]]),
    )

    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=3, random_seed=7
    )

    assert result.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )
    assert result.diagnostics.successful_resamples == 0
    assert result.diagnostics.failed_resamples == 0
    assert result.diagnostics.degenerate_resamples == 3


def test_v2_bootstrap_partially_degenerate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )
    draws = [[0, 0]] + [[40, 60]] * 40 + [[60, 40]] * 40
    monkeypatch.setattr(
        deconvolution_module.np.random,
        "default_rng",
        lambda _seed: _DrawSequence(draws),
    )

    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=81, random_seed=7
    )

    assert result.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )
    assert result.diagnostics.successful_resamples == 80
    assert result.diagnostics.failed_resamples == 0
    assert result.diagnostics.degenerate_resamples == 1
    assert result.diagnostics.observed_unusable_resample_fraction == pytest.approx(
        1 / 81
    )
    assert not result.diagnostics.interval_eligibility_met
    assert all(interval.lower_fraction is None for interval in result.intervals)


def test_v2_bootstrap_eligible_interval_is_independently_calculable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )
    draws = [[40, 60]] * 40 + [[60, 40]] * 40
    monkeypatch.setattr(
        deconvolution_module.np.random,
        "default_rng",
        lambda _seed: _DrawSequence(draws),
    )

    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=80, random_seed=7
    )

    assert result.information_status == BootstrapInformationStatus.AVAILABLE
    assert result.diagnostics.successful_resamples == 80
    assert result.diagnostics.degenerate_resamples == 0
    assert result.diagnostics.interval_eligibility_met
    immune, liver = result.intervals
    assert immune.lower_fraction == pytest.approx(1 / 3)
    assert immune.upper_fraction == pytest.approx(2 / 3)
    assert liver.lower_fraction == pytest.approx(1 / 3)
    assert liver.upper_fraction == pytest.approx(2 / 3)


def test_v2_bootstrap_solver_failure_blocks_interval_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )
    draws = [[40, 60]] * 41 + [[60, 40]] * 40
    monkeypatch.setattr(
        deconvolution_module.np.random,
        "default_rng",
        lambda _seed: _DrawSequence(draws),
    )
    real_solve = deconvolution_module._solve_arrays
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DeconvolutionError("synthetic solver failure")
        return real_solve(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(deconvolution_module, "_solve_arrays", fail_once)
    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=81, random_seed=7
    )

    assert result.diagnostics.successful_resamples == 80
    assert result.diagnostics.failed_resamples == 1
    assert result.diagnostics.minimum_successful_resamples == 80
    assert result.diagnostics.maximum_unusable_resample_fraction == 0.0
    assert not result.diagnostics.interval_eligibility_met
    assert result.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )


def test_v2_sparse_single_fragment_does_not_claim_zero_width_precision() -> None:
    atlas = AtlasUMatrix(
        atlas_id="atlas.sparse.v1",
        method=LOYFER_UXM_METHOD,
        cell_type_ids=("only",),
        rows=(
            AtlasUMatrixRow(
                marker_id="marker.only",
                values=(AtlasUValue(cell_type_id="only", u_fraction=1.0),),
            ),
        ),
        source_ids=("source.synthetic-atlas",),
    )
    counts = (marker_count("marker.only", 1, 1),)
    source = deconvolve_uxm_v2(
        counts, atlas, row_scale=NnlsRowScale.SQRT_COUNT
    )

    result = bootstrap_uxm_v2(
        counts, atlas, source, replicates=10, random_seed=7
    )

    assert result.diagnostics.successful_resamples == 10
    assert result.information_status == (
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION
    )
    assert result.intervals[0].lower_fraction is None
    assert result.intervals[0].upper_fraction is None


def test_legacy_bootstrap_result_remains_unchanged() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm(counts, atlas)

    legacy = bootstrap_uxm(
        counts, atlas, source, replicates=10, random_seed=7
    )

    assert isinstance(legacy, BootstrapResult)
    assert "diagnostics" not in legacy.model_dump()
    assert "schema_version" not in legacy.model_dump()


def test_bootstrap_rejects_changed_source_alignment() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 40, 100),
        marker_count("marker.liver.1", 60, 100),
    )
    source = deconvolve_uxm(tuple(reversed(counts)), atlas)

    with pytest.raises(DeconvolutionError, match="marker order"):
        bootstrap_uxm(
            counts,
            atlas,
            source,
            replicates=10,
            random_seed=1,
        )


def test_observed_cohort_ranges_are_inclusive_and_not_clinical_normals() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 5, 10),
        marker_count("marker.liver.1", 5, 10),
    )
    source = deconvolve_uxm(counts, atlas)
    ranges = (
        ObservedCohortRange("immune", 0.5, 0.7, ("source.cohort",)),
        ObservedCohortRange("liver", 0.6, 0.8, ("source.cohort",)),
    )

    comparison = compare_observed_cohort_ranges(source, ranges)

    assert comparison.reference_kind.value == "observed_cohort_range"
    assert [row.classification for row in comparison.rows] == [
        RangeClassification.WITHIN,
        RangeClassification.BELOW,
    ]
    assert "normal" not in json.dumps(
        comparison.model_dump(mode="json")
    ).lower()


def test_range_comparison_preserves_estimate_order_and_flags_above() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )
    source = deconvolve_uxm(counts, atlas)
    ranges = (
        ObservedCohortRange("liver", 0.2, 0.5, ("source.cohort",)),
        ObservedCohortRange("immune", 0.1, 0.2, ("source.cohort",)),
    )

    comparison = compare_observed_cohort_ranges(
        source,
        ranges,
        partial_table=True,
    )

    assert [row.cell_type_id for row in comparison.rows] == [
        "immune",
        "liver",
    ]
    assert [row.classification for row in comparison.rows] == [
        RangeClassification.ABOVE,
        RangeClassification.ABOVE,
    ]
    assert comparison.partial_table


def test_range_comparison_rejects_mismatch_duplicate_and_invalid_bounds() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )
    source = deconvolve_uxm(counts, atlas)

    with pytest.raises(DeconvolutionError, match="match all"):
        compare_observed_cohort_ranges(
            source,
            (ObservedCohortRange("immune", 0.1, 0.9, ("source.cohort",)),),
        )
    with pytest.raises(DeconvolutionError, match="unique"):
        compare_observed_cohort_ranges(
            source,
            (
                ObservedCohortRange("immune", 0.1, 0.9, ("source.cohort",)),
                ObservedCohortRange("immune", 0.1, 0.9, ("source.cohort",)),
            ),
        )
    with pytest.raises(DeconvolutionError, match="invalid observed"):
        compare_observed_cohort_ranges(
            source,
            (
                ObservedCohortRange("immune", 0.8, 0.2, ("source.cohort",)),
                ObservedCohortRange("liver", 0.1, 0.9, ("source.cohort",)),
            ),
        )


def test_iteration_and_bootstrap_controls_fail_closed() -> None:
    atlas = fixture_atlas()
    counts = (
        marker_count("marker.immune.1", 4, 10),
        marker_count("marker.liver.1", 6, 10),
    )
    source = deconvolve_uxm(counts, atlas)

    with pytest.raises(DeconvolutionError, match="positive integer"):
        deconvolve_uxm(counts, atlas, max_iterations=0)
    with pytest.raises(DeconvolutionError, match="at least 2"):
        bootstrap_uxm(
            counts,
            atlas,
            source,
            replicates=1,
            random_seed=7,
        )
