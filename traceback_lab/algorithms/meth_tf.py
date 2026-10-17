"""Exploratory tumour/healthy methylation mixture over registered CpG markers.

The two reference columns are supplied and licensed by an operator. This
worker uses one 5mC argmax call per CpG observation, fits nonnegative column
weights with sqrt(call-count) row scaling, then reports the tumour share of
their sum. It has no clinical calibration, detection threshold, or interval.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..meth_calls import HAS_M, argmax_state
from ..meth_tf_reference import CALL_DEFINITION, RegisteredReference, inspect_reference
from ..read_summary import ReadSummary
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ..store import LabStore
from ._base import Base, metric
from ._meth import MethBase, load_calls

MIN_ELIGIBLE_READS = 2_500_000


def weighted_reference_condition_number(
    healthy: np.ndarray, tumour: np.ndarray, counts: np.ndarray,
) -> float | None:
    """2-norm condition of the observed, count-weighted reference columns."""
    selected = counts > 0
    if np.count_nonzero(selected) < 2:
        return None
    weight = np.sqrt(counts[selected].astype(float))
    matrix = np.column_stack((healthy[selected] * weight, tumour[selected] * weight))
    if np.linalg.matrix_rank(matrix) < 2:
        return None
    condition = float(np.linalg.cond(matrix))
    return condition if math.isfinite(condition) else None


def solve_two_component(
    healthy: np.ndarray, tumour: np.ndarray, methylated: np.ndarray, counts: np.ndarray,
) -> tuple[float | None, float | None]:
    """Analytic 2-column NNLS; (normalised tumour weight, weighted RMS)."""
    if not (healthy.ndim == tumour.ndim == methylated.ndim == counts.ndim == 1):
        raise ValueError("NNLS inputs must be vectors")
    if not (len(healthy) == len(tumour) == len(methylated) == len(counts)):
        raise ValueError("NNLS inputs have different lengths")
    selected = counts > 0
    if selected.sum() < 2:
        return None, None
    h = healthy[selected].astype(float)
    t = tumour[selected].astype(float)
    n = counts[selected].astype(float)
    k = methylated[selected].astype(float)
    if (not all(np.isfinite(vector).all() for vector in (h, t, n, k))
            or np.any((h < 0) | (h > 1) | (t < 0) | (t > 1) | (k < 0) | (k > n))):
        raise ValueError("NNLS inputs contain invalid marker fractions or counts")
    weight = np.sqrt(n)
    matrix = np.column_stack((h * weight, t * weight))
    observed = (k / n) * weight
    if np.linalg.matrix_rank(matrix) < 2:
        return None, None
    unconstrained = np.linalg.lstsq(matrix, observed, rcond=None)[0]
    candidates = [np.maximum(unconstrained, 0.0)] if np.all(unconstrained >= 0) else []
    for column in range(2):
        basis = matrix[:, column]
        denominator = float(np.dot(basis, basis))
        single = np.zeros(2)
        single[column] = max(0.0, float(np.dot(basis, observed)) / denominator) if denominator else 0.0
        candidates.append(single)
    candidates.append(np.zeros(2))
    weights = min(candidates, key=lambda row: float(np.sum((matrix @ row - observed) ** 2)))
    total = float(weights.sum())
    if total <= 0 or not math.isfinite(total):
        return None, None
    residual = float(np.linalg.norm(matrix @ weights - observed) / math.sqrt(float(n.sum())))
    return float(weights[1] / total), residual


def tally_markers(
    calls: Any, mask: np.ndarray | None, summary: ReadSummary, reference: RegisteredReference,
) -> tuple[np.ndarray, np.ndarray]:
    """Stream the D28 store and count 5mC calls at registered CpG coordinates."""
    markers = reference.reference.markers
    counts = np.zeros(len(markers), dtype=np.int64)
    methylated = np.zeros(len(markers), dtype=np.int64)
    contig_ids = {name: index for index, name in enumerate(summary.contigs)}
    by_contig: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for cid in {contig_ids[marker.chrom] for marker in markers}:
        indexes = np.asarray([index for index, marker in enumerate(markers)
                              if contig_ids[marker.chrom] == cid], dtype=np.int64)
        by_contig[cid] = (np.asarray([markers[index].pos0 for index in indexes], dtype=np.int64), indexes)
    for chunk in calls.chunks(mask):
        if np.any(chunk["read_row"] >= summary.rows):
            raise ValueError("meth-calls read_row lies outside read-summary")
        chunk = chunk[(chunk["channels"] & HAS_M) != 0]
        if chunk.size == 0:
            continue
        for cid, (positions, indexes) in by_contig.items():
            rows = chunk[chunk["contig"] == cid]
            if rows.size == 0:
                continue
            offsets = np.searchsorted(positions, rows["ref_pos0"].astype(np.int64))
            in_bounds = offsets < positions.size
            matching = np.zeros(rows.size, dtype=bool)
            matching[in_bounds] = positions[offsets[in_bounds]] == rows["ref_pos0"][in_bounds]
            if not np.any(matching):
                continue
            selected = indexes[offsets[matching]]
            np.add.at(counts, selected, 1)
            np.add.at(methylated, selected, (argmax_state(rows[matching]) == 1).astype(np.int64))
    return methylated, counts


@dataclass
class MethTF(MethBase):
    version: str = "2"
    cost: str = "slow"
    requires: Requirements = field(
        default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=MIN_ELIGIBLE_READS)
    )
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "channel": "C+m", "call": CALL_DEFINITION,
        "solver": "two-component-active-set-nnls-v1", "row_scale": "sqrt_call_count",
        "condition_diagnostic": "weighted_reference_2norm",
        "reported_fraction": "tumour_weight_over_sum", "minimum_covered_markers": 2,
    })

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return [inspect_reference(LabStore(root)).sha256]
        except (OSError, ValueError):
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "meth-TF needs a registered, non-example marker reference")
        decision = Base.preflight(self, ctx)
        if isinstance(decision, Skip):
            return decision
        try:
            reference = inspect_reference(LabStore(ctx.root))
        except (OSError, ValueError):
            return Skip("missing_asset", "register a compatible meth-TF marker reference with 'traceback lab asset add meth-tf-reference'")
        if reference.reference.reference_sha256 != ctx.reference_sha256:
            return Skip("reference", "the meth-TF marker reference does not match this genome reference")
        from .cell_origin import header_model

        model = header_model(ctx.input_dir / "aligned.bam")
        if model is None or model != reference.reference.modbase_model:
            return Skip("missing_tags", "the BAM's declared modified-base model does not match the meth-TF reference")
        summary = ReadSummary.open(Path(ctx.extras["upstream"]["read-summary"]) / "columns")
        lengths = dict(zip(summary.contigs, summary.lengths))
        if any(marker.chrom not in lengths or marker.pos0 >= lengths[marker.chrom]
               for marker in reference.reference.markers):
            return Skip("reference", "meth-TF markers lie outside this reference's autosomal contigs")
        return super().preflight(ctx)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        reference = inspect_reference(LabStore(ctx.root))
        if ctx.extras.get("planned_asset_sha256s") != [reference.sha256]:
            raise ValueError("the meth-TF reference changed after execution planning")
        if reference.reference.reference_sha256 != ctx.reference_sha256:
            raise ValueError("the meth-TF marker reference does not match this genome reference")
        calls, mask = load_calls(ctx)
        summary = ReadSummary.open(Path(ctx.extras["upstream"]["read-summary"]) / "columns")
        methylated, counts = tally_markers(calls, mask, summary, reference)
        markers = reference.reference.markers
        fraction, residual = solve_two_component(
            np.asarray([marker.healthy for marker in markers]),
            np.asarray([marker.tumour for marker in markers]),
            methylated, counts,
        )
        condition = weighted_reference_condition_number(
            np.asarray([marker.healthy for marker in markers]),
            np.asarray([marker.tumour for marker in markers]), counts,
        )
        used = int(counts.sum())
        covered = int(np.count_nonzero(counts))
        receipt = {
            "schema": "traceback.lab.meth-tf-fit.v1", "asset_sha256": reference.sha256,
            "reference_sha256": reference.reference.reference_sha256,
            "modbase_model": reference.reference.modbase_model,
            "call_definition": reference.reference.call_definition,
            "solver": self.parameters["solver"], "row_scale": self.parameters["row_scale"],
            "markers_registered": len(markers), "markers_covered": covered,
            "cpg_calls_used": used, "identifiable": fraction is not None,
            "tumor_fraction": fraction, "fit_rms": residual,
            "reference_condition_number": condition,
        }
        return AlgorithmOutput(
            metrics=[
                metric("tumor_fraction", fraction, unit="fraction", n=used),
                metric("identifiable", fraction is not None),
                metric("markers_covered", covered, unit="markers", n=len(markers)),
                metric("cpg_calls_used", used, unit="calls", n=used),
                metric("fit_rms", residual, unit="fraction", n=covered),
                metric("reference_condition_number", condition, n=covered),
            ],
            artifacts={"meth_tf_fit.json": json.dumps(receipt, separators=(",", ":")).encode()},
        )


METH_TF = MethTF("meth-tf")

__all__ = ["METH_TF", "solve_two_component", "tally_markers", "weighted_reference_condition_number"]
