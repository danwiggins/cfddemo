"""``mtdna``: mitochondrial share of cfDNA and its fragment-size mode (Appendix A).

``chrm_fraction`` = chrM primaries passing D21 (mapped, not QC-fail or
duplicate, MAPQ >= 20) / all primary records (mapped or not), as Appendix A
writes it ("chrM MAPQ >= 20 primaries / all primaries").

``chrm_mode_bp`` = the most frequent chrM span (all spans, smallest on ties).
The panel is the chrM 1-bp span histogram up to 1,000 bp in the
fragment-size ``density`` shape.
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
    parameters: dict[str, Any] = field(default_factory=lambda: {"hist_max_bp": HIST_MAX, "denominator": "all_primaries"})

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
        primary = summary.primary()
        on_chrm = summary.eligible(contigs=chrm)
        if mask is not None:
            primary &= mask
            on_chrm &= mask
        n_all = int(primary.sum())
        n_chrm = int(on_chrm.sum())
        all_spans = summary.spans()[on_chrm]
        all_spans = all_spans[all_spans >= 1]
        mode = None
        if all_spans.size:
            values, counts = np.unique(all_spans, return_counts=True)
            mode = int(values[counts.argmax()])
        spans = all_spans[all_spans <= HIST_MAX]
        hist = np.bincount(spans.astype(np.int64), minlength=HIST_MAX + 1)[: HIST_MAX + 1]
        panel = {"span_bp": list(range(1, HIST_MAX + 1)), "count": [int(v) for v in hist[1:]]}
        return AlgorithmOutput(
            metrics=[
                metric("chrm_fraction", ratio(n_chrm, n_all), unit="fraction", n=n_all),
                metric("chrm_mode_bp", mode, unit="bp", n=int(all_spans.size)),
            ],
            panels=[{"panel_id": "chrm-size-density", "kind": "density", "data_ref": "artifacts/chrm_size_hist.json"}],
            artifacts={"chrm_size_hist.json": json.dumps(panel, separators=(",", ":")).encode()},
        )


MTDNA = Mtdna("mtdna")
