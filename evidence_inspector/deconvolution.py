"""Deterministic Loyfer fragment-UXM deconvolution.

The observed value for each marker is its fragment-level U fraction.  NNLS is
fit against the corresponding Loyfer atlas U column, weighting each row by the
number of classified fragments that contributed to that observed fraction.
The resulting nonnegative coefficients are normalized to canonical fractions.

Reference comparisons produced here are descriptive comparisons with an
observed cohort.  They are not clinical normal intervals.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    BootstrapInterval,
    BootstrapResult,
    CellFractionEstimate,
    DeconvolutionOutput,
    LOYFER_UXM_METHOD,
    MarkerCountRow,
    NnlsDiagnostics,
    RangeClassification,
    RangeComparison,
    ReferenceRangeRow,
)

DEFAULT_TOLERANCE = 1e-12


class DeconvolutionError(ValueError):
    """Raised when inputs cannot produce a defensible deconvolution."""


@dataclass(frozen=True, slots=True)
class ObservedCohortRange:
    """Inclusive study-reference bounds; never a clinical normal interval."""

    cell_type_id: str
    min_fraction: float
    max_fraction: float
    source_ids: tuple[str, ...]


def _validate_positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeconvolutionError(f"{name} must be a positive integer")


def _aligned_arrays(
    marker_counts: Sequence[MarkerCountRow],
    atlas: AtlasUMatrix,
) -> tuple[tuple[str, ...], np.ndarray, np.ndarray, np.ndarray]:
    if atlas.method != LOYFER_UXM_METHOD:
        raise DeconvolutionError("deconvolution requires the Loyfer UXM method")
    if not marker_counts:
        raise DeconvolutionError("at least one marker count is required")

    marker_ids = tuple(row.marker_id for row in marker_counts)
    if len(set(marker_ids)) != len(marker_ids):
        raise DeconvolutionError("marker count IDs must be unique")
    atlas_by_id = {row.marker_id: row for row in atlas.rows}
    if set(marker_ids) != set(atlas_by_id):
        missing = sorted(set(atlas_by_id) - set(marker_ids))
        unexpected = sorted(set(marker_ids) - set(atlas_by_id))
        detail = []
        if missing:
            detail.append(f"missing marker IDs: {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected marker IDs: {', '.join(unexpected)}")
        raise DeconvolutionError(
            "marker counts and atlas must have identical marker IDs"
            + (f" ({'; '.join(detail)})" if detail else "")
        )

    matrix = np.asarray(
        [
            [value.u_fraction for value in atlas_by_id[marker_id].values]
            for marker_id in marker_ids
        ],
        dtype=np.float64,
    )
    observed = np.asarray(
        [row.u_fraction for row in marker_counts],
        dtype=np.float64,
    )
    counts = np.asarray(
        [row.classified_fragment_count for row in marker_counts],
        dtype=np.float64,
    )
    if not (
        np.all(np.isfinite(matrix))
        and np.all(np.isfinite(observed))
        and np.all(np.isfinite(counts))
    ):
        raise DeconvolutionError("deconvolution inputs must be finite")
    return marker_ids, matrix, observed, counts


def _active_set_nnls(
    matrix: np.ndarray,
    observed: np.ndarray,
    *,
    tolerance: float,
    max_iterations: int,
) -> tuple[np.ndarray, int, bool]:
    """Solve NNLS with a deterministic Lawson-Hanson active set."""

    rows, columns = matrix.shape
    if rows == 0 or columns == 0 or observed.shape != (rows,):
        raise DeconvolutionError("NNLS matrix dimensions are invalid")

    solution = np.zeros(columns, dtype=np.float64)
    passive = np.zeros(columns, dtype=bool)
    gradient = matrix.T @ observed
    iterations = 0

    while np.any((~passive) & (gradient > tolerance)):
        if iterations >= max_iterations:
            return solution, iterations, False
        candidates = np.where(~passive, gradient, -np.inf)
        passive[int(np.argmax(candidates))] = True

        while True:
            if iterations >= max_iterations:
                return solution, iterations, False
            iterations += 1
            trial = np.zeros(columns, dtype=np.float64)
            active_matrix = matrix[:, passive]
            active_solution, *_ = np.linalg.lstsq(
                active_matrix,
                observed,
                rcond=None,
            )
            trial[passive] = active_solution
            if np.all(trial[passive] > tolerance):
                solution = trial
                break

            nonpositive = passive & (trial <= tolerance)
            denominators = solution[nonpositive] - trial[nonpositive]
            valid = denominators > 0.0
            if not np.any(valid):
                passive[nonpositive] = False
                solution[nonpositive] = 0.0
                break
            alpha = float(
                np.min(solution[nonpositive][valid] / denominators[valid])
            )
            solution = solution + alpha * (trial - solution)
            released = passive & (solution <= tolerance)
            passive[released] = False
            solution[released] = 0.0

        residual = observed - matrix @ solution
        gradient = matrix.T @ residual

    solution[solution < tolerance] = 0.0
    residual = observed - matrix @ solution
    gradient = matrix.T @ residual
    primal_ok = bool(np.all(solution >= -tolerance))
    dual_ok = bool(np.all(gradient[~passive] <= tolerance))
    return solution, iterations, primal_ok and dual_ok


def _solve_arrays(
    matrix: np.ndarray,
    observed: np.ndarray,
    counts: np.ndarray,
    *,
    tolerance: float,
    max_iterations: int,
) -> tuple[np.ndarray, int, bool, float]:
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance <= 0.0
    ):
        raise DeconvolutionError("tolerance must be finite and greater than zero")
    _validate_positive_integer(max_iterations, "max_iterations")
    if np.all(observed <= tolerance):
        raise DeconvolutionError(
            "zero U-fraction signal cannot be normalized into cell fractions"
        )

    row_scale = np.sqrt(counts)
    weighted_matrix = matrix * row_scale[:, np.newaxis]
    weighted_observed = observed * row_scale
    weights, iterations, converged = _active_set_nnls(
        weighted_matrix,
        weighted_observed,
        tolerance=float(tolerance),
        max_iterations=max_iterations,
    )
    residual = weighted_matrix @ weights - weighted_observed
    residual_l2 = float(np.linalg.norm(residual))
    return weights, iterations, converged, residual_l2


def _result_id(
    marker_counts: Sequence[MarkerCountRow],
    atlas: AtlasUMatrix,
) -> str:
    payload = {
        "atlas_id": atlas.atlas_id,
        "marker_counts": [
            row.model_dump(mode="json") for row in marker_counts
        ],
        "method": LOYFER_UXM_METHOD.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"nnls.{digest[:24]}"


def deconvolve_uxm(
    marker_counts: Sequence[MarkerCountRow],
    atlas: AtlasUMatrix,
    *,
    result_id: str | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    max_iterations: int = 10_000,
) -> DeconvolutionOutput:
    """Run count-weighted NNLS against a Loyfer atlas U matrix."""

    marker_ids, matrix, observed, counts = _aligned_arrays(marker_counts, atlas)
    weights, iterations, converged, residual_l2 = _solve_arrays(
        matrix,
        observed,
        counts,
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    if not converged:
        raise DeconvolutionError(
            f"NNLS did not converge within {max_iterations} iterations"
        )
    weight_sum = float(np.sum(weights))
    if not math.isfinite(weight_sum) or weight_sum <= tolerance:
        raise DeconvolutionError(
            "NNLS produced no positive weight to normalize"
        )

    fractions = weights / weight_sum
    estimates = tuple(
        CellFractionEstimate(
            cell_type_id=cell_type_id,
            raw_nnls_weight=float(weight),
            fraction=float(fraction),
        )
        for cell_type_id, weight, fraction in zip(
            atlas.cell_type_ids,
            weights,
            fractions,
            strict=True,
        )
    )
    return DeconvolutionOutput(
        result_id=result_id or _result_id(marker_counts, atlas),
        method=LOYFER_UXM_METHOD,
        atlas_id=atlas.atlas_id,
        marker_ids=marker_ids,
        estimates=estimates,
        diagnostics=NnlsDiagnostics(
            converged=True,
            iterations=iterations,
            residual_l2=residual_l2,
            objective_value=0.5 * residual_l2 * residual_l2,
        ),
    )


def bootstrap_uxm(
    marker_counts: Sequence[MarkerCountRow],
    atlas: AtlasUMatrix,
    source: DeconvolutionOutput,
    *,
    replicates: int,
    random_seed: int,
    confidence_level: float = 0.95,
    tolerance: float = DEFAULT_TOLERANCE,
    max_iterations: int = 10_000,
) -> BootstrapResult:
    """Bootstrap U/non-U fragment calls within each marker with a fixed seed."""

    if isinstance(replicates, bool) or not isinstance(replicates, int):
        raise DeconvolutionError("replicates must be an integer")
    if replicates < 2:
        raise DeconvolutionError("replicates must be at least 2")
    if (
        isinstance(random_seed, bool)
        or not isinstance(random_seed, int)
        or random_seed < 0
    ):
        raise DeconvolutionError("random_seed must be a nonnegative integer")
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not math.isfinite(confidence_level)
        or not 0.0 < confidence_level < 1.0
    ):
        raise DeconvolutionError(
            "confidence_level must be finite and strictly between 0 and 1"
        )

    marker_ids, matrix, observed, counts = _aligned_arrays(marker_counts, atlas)
    if source.method != LOYFER_UXM_METHOD:
        raise DeconvolutionError("bootstrap source must use the Loyfer UXM method")
    if source.atlas_id != atlas.atlas_id:
        raise DeconvolutionError("bootstrap source atlas does not match")
    if source.marker_ids != marker_ids:
        raise DeconvolutionError("bootstrap source marker order does not match")
    if tuple(item.cell_type_id for item in source.estimates) != atlas.cell_type_ids:
        raise DeconvolutionError("bootstrap source cell types do not match")

    integer_counts = counts.astype(np.int64)
    rng = np.random.default_rng(random_seed)
    samples = np.empty((replicates, len(atlas.cell_type_ids)), dtype=np.float64)
    for index in range(replicates):
        sampled_u = rng.binomial(integer_counts, observed)
        sampled_observed = sampled_u / counts
        try:
            weights, _, converged, _ = _solve_arrays(
                matrix,
                sampled_observed,
                counts,
                tolerance=tolerance,
                max_iterations=max_iterations,
            )
        except DeconvolutionError as error:
            raise DeconvolutionError(
                f"bootstrap replicate {index} is degenerate: {error}"
            ) from error
        weight_sum = float(np.sum(weights))
        if not converged or weight_sum <= tolerance:
            raise DeconvolutionError(
                f"bootstrap replicate {index} did not produce a valid solution"
            )
        samples[index] = weights / weight_sum

    alpha = (1.0 - confidence_level) / 2.0
    lower = np.quantile(samples, alpha, axis=0, method="linear")
    upper = np.quantile(samples, 1.0 - alpha, axis=0, method="linear")
    point_estimates = np.asarray(
        [item.fraction for item in source.estimates],
        dtype=np.float64,
    )
    lower = np.minimum(lower, point_estimates)
    upper = np.maximum(upper, point_estimates)
    intervals = tuple(
        BootstrapInterval(
            cell_type_id=cell_type_id,
            estimate=float(estimate),
            lower_fraction=float(low),
            upper_fraction=float(high),
        )
        for cell_type_id, estimate, low, high in zip(
            atlas.cell_type_ids,
            point_estimates,
            lower,
            upper,
            strict=True,
        )
    )
    return BootstrapResult(
        source_result_id=source.result_id,
        replicates=replicates,
        random_seed=random_seed,
        confidence_level=float(confidence_level),
        intervals=intervals,
    )


def compare_observed_cohort_ranges(
    source: DeconvolutionOutput,
    ranges: Sequence[ObservedCohortRange],
    *,
    partial_table: bool = False,
) -> RangeComparison:
    """Compare estimates inclusively with study-observed cohort ranges."""

    if not ranges:
        raise DeconvolutionError("at least one observed cohort range is required")
    estimate_by_id = {
        estimate.cell_type_id: estimate for estimate in source.estimates
    }
    range_ids = [item.cell_type_id for item in ranges]
    if len(set(range_ids)) != len(range_ids):
        raise DeconvolutionError("observed cohort range IDs must be unique")
    if set(range_ids) != set(estimate_by_id):
        raise DeconvolutionError(
            "observed cohort ranges must match all deconvolution cell types"
        )
    range_by_id = {item.cell_type_id: item for item in ranges}

    rows = []
    for estimate in source.estimates:
        reference = range_by_id[estimate.cell_type_id]
        fraction = estimate.fraction
        classification = (
            RangeClassification.BELOW
            if fraction < reference.min_fraction
            else RangeClassification.ABOVE
            if fraction > reference.max_fraction
            else RangeClassification.WITHIN
        )
        try:
            row = ReferenceRangeRow(
                cell_type_id=estimate.cell_type_id,
                fraction=fraction,
                min_fraction=reference.min_fraction,
                max_fraction=reference.max_fraction,
                classification=classification,
                source_ids=reference.source_ids,
            )
        except (TypeError, ValueError) as error:
            raise DeconvolutionError(
                f"invalid observed cohort range for {estimate.cell_type_id}"
            ) from error
        rows.append(row)
    return RangeComparison(rows=tuple(rows), partial_table=partial_table)


__all__ = [
    "DEFAULT_TOLERANCE",
    "DeconvolutionError",
    "ObservedCohortRange",
    "bootstrap_uxm",
    "compare_observed_cohort_ranges",
    "deconvolve_uxm",
]
