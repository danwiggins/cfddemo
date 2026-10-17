"""Exploratory Weidner three-CpG clock on ONT CpG calls.

The published model was fitted to whole-blood array methylation.  Applying
it to cfDNA ONT calls is a research transfer, with no clinical calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pysam

from ..epi_clock_model import (
    COEFFICIENTS_SHA256, WEIDNER_INTERCEPT, WEIDNER_WEIGHTS,
    ClockModelError, inspect_map,
)
from ..meth_calls import HAS_M, argmax_state
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import load_summary, metric
from ._meth import MethBase, load_calls

HG38_CHR1_LENGTH = 248_956_422
MIN_SITE_CALLS = 5  # engineering completeness gate, not a published clinical threshold


@dataclass
class EpiClock(MethBase):
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=5_000_000))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "model": "Weidner-3", "coefficient_asset_sha256": COEFFICIENTS_SHA256,
        "call": "argmax", "min_calls_per_site": MIN_SITE_CALLS,
    })

    def asset_sha256s(self, root: Path) -> list[str]:
        from ..store import LabStore

        try:
            return [inspect_map(LabStore(root)).sha256]
        except ClockModelError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        summary, _ = load_summary(ctx)
        lengths = dict(zip(summary.contigs, summary.lengths))
        if lengths.get("chr1") != HG38_CHR1_LENGTH:
            return Skip("reference", "Weidner probe coordinates require hg38")
        from ..store import LabStore

        try:
            model = inspect_map(LabStore(ctx.root))
            if model.sha256 not in ctx.extras.get("planned_asset_sha256s", ()):
                return Skip("missing_asset", "Weidner map changed after execution identity was planned")
            reference = ctx.extras.get("reference_fasta")
            if not reference:
                return Skip("reference", "reference FASTA is unavailable")
            with pysam.FastaFile(str(reference)) as fasta:
                for _, chrom, pos0 in model.sites:
                    if pos0 + 2 > fasta.get_reference_length(chrom) or fasta.fetch(chrom, pos0, pos0 + 2).upper() != "CG":
                        return Skip("missing_asset", "registered Weidner position is not an hg38 CpG")
        except (ClockModelError, OSError, ValueError, KeyError) as exc:
            return Skip("missing_asset", f"verified hg38 Weidner coordinate map unavailable: {exc}")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        model = inspect_map(LabStore(ctx.root))
        if model.sha256 not in ctx.extras.get("planned_asset_sha256s", ()):
            raise RuntimeError("Weidner map changed after execution identity was planned")
        calls, mask = load_calls(ctx)
        summary, _ = load_summary(ctx)
        counts = np.zeros(3, dtype=np.int64)
        methylated = np.zeros(3, dtype=np.int64)
        positions = [(summary.contigs.index(chrom), pos0) for _, chrom, pos0 in model.sites]
        for chunk in calls.chunks(mask):
            for index, (contig, pos0) in enumerate(positions):
                rows = chunk[(chunk["contig"] == contig) & (chunk["ref_pos0"] == pos0) & ((chunk["channels"] & HAS_M) != 0)]
                counts[index] += rows.size
                methylated[index] += (argmax_state(rows) == 1).sum()
        fractions = [float(methylated[i] / counts[i]) if counts[i] >= MIN_SITE_CALLS else None for i in range(3)]
        age = (WEIDNER_INTERCEPT + sum(weight * fraction for weight, fraction in zip(WEIDNER_WEIGHTS, fractions))) if all(fraction is not None for fraction in fractions) else None
        # A registration changed after planning must never produce a result under an older identity.
        if inspect_map(LabStore(ctx.root)).sha256 != model.sha256:
            raise RuntimeError("Weidner coordinate map changed during execution")
        return AlgorithmOutput(metrics=[
            metric("epigenetic_age_years", age, unit="years", n=int(counts.min())),
            *[metric(f"{site}_5mc_fraction", fraction, unit="fraction", n=int(counts[i]))
              for i, ((site, _, _), fraction) in enumerate(zip(model.sites, fractions))],
        ])


EPI_CLOCK = EpiClock("epi-clock")
