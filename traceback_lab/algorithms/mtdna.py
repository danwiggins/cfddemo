"""``mtdna``: mitochondrial share of cfDNA and its fragment-size mode (Appendix A).

``chrm_fraction`` = chrM molecules / all molecules, both under the D21 filter
(primary, mapped, not QC-fail or duplicate, MAPQ >= 20) on chr1–22, chrX, chrY
and chrM.  Appendix A writes "chrM MAPQ >= 20 primaries / all primaries"; the
denominator here applies the same default D21 filter so the share does not
move with the mapping rate (stated in the spec's audit trail).

``chrm_mode_bp`` = argmax of the chrM span histogram over 1–1,000 bp.  The
panel is the chrM 1-bp span histogram in the fragment-size ``density`` shape.
A reference without chrM skips with reason ``reference``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Ready, Skip
from ._base import Base, load_summary, metric, ratio

HIST_MAX = 1000


@dataclass
class Mtdna(Base):
    family: str = "fragmentomics"
    parameters: dict[str, Any] = field(default_factory=lambda: {"hist_max_bp": HIST_MAX, "denominator": "d21_with_chrM"})

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        summary, _ = load_summary(ctx)
        if summary.chrm_ids().size == 0:
            return Skip("reference", "the reference has no chrM contig")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        chrm = summary.chrm_ids()
        contigs = np.concatenate([summary.canonical_ids(), chrm])
        eligible = summary.eligible(contigs=contigs)
        if mask is not None:
            eligible &= mask
        on_chrm = eligible & np.isin(summary.column("contig"), chrm)
        n_all = int(eligible.sum())
        n_chrm = int(on_chrm.sum())
        spans = summary.spans()[on_chrm]
        spans = spans[(spans >= 1) & (spans <= HIST_MAX)]
        hist = np.bincount(spans.astype(np.int64), minlength=HIST_MAX + 1)[: HIST_MAX + 1]
        mode = int(hist[1:].argmax() + 1) if hist[1:].sum() > 0 else None
        panel = {"span_bp": list(range(1, HIST_MAX + 1)), "count": [int(v) for v in hist[1:]]}
        return AlgorithmOutput(
            metrics=[
                metric("chrm_fraction", ratio(n_chrm, n_all), unit="fraction", n=n_all),
                metric("chrm_mode_bp", mode, unit="bp", n=int(spans.size)),
            ],
            panels=[{"panel_id": "chrm-size-density", "kind": "density", "data_ref": "artifacts/chrm_size_hist.json"}],
            artifacts={"chrm_size_hist.json": json.dumps(panel, separators=(",", ":")).encode()},
        )


MTDNA = Mtdna("mtdna")
