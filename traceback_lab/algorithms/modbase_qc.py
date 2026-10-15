"""``modbase-qc``: can the methylation calls be trusted (Appendix A).

Version 2 reads the D28 ``meth-calls`` store instead of re-decoding MM/ML
from the BAM, so derived inputs cost one masked pass over the store, and only
CpGs that align to the reference count (CpGs in the soft-clipped adapter are
dropped by ``meth-calls``).

- ``mod_codes``: the mod codes declared in the first 10,000 D21-eligible reads
  of the **parent** input (``C+m`` only means no 5hmC channel), as recorded by
  ``meth-calls``; the codes are a property of the basecall model, so a derived
  input reports its parent's;
- ``mm_tagged_fraction``: eligible records carrying MM / eligible records
  (from the read summary);
- ``ml_mean``: mean ML/255 over every C+m and C+h value at aligned CpG calls;
- ``ml_below_filter_fraction``: share of aligned CpG calls whose most likely
  state (5mC, 5hmC or canonical, canonical = 1 − Σ mods) has probability < 0.7;
- ``invalid_mm_records``: eligible records with MM whose ``MN`` mismatches or
  whose MM/ML do not parse; they contribute no calls (D28).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..meth_calls import EXPLICIT_H, EXPLICIT_M
from ..registry import AlgorithmOutput, InputContext, Ready, Skip
from ._base import load_summary, metric, ratio
from ._meth import MethBase, load_calls

FILTER_THRESHOLD = 0.7


@dataclass
class ModbaseQc(MethBase):
    version: str = "2"
    family: str = "qc"
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"code_sample": "first 10000 eligible reads", "filter_threshold": FILTER_THRESHOLD}
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        if not np.any(eligible & (summary.column("has_mm") == 1)):
            return Skip("missing_tags", "no eligible read carries an MM tag (basecalled without modified bases?)")
        return super().preflight(ctx)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        n_eligible = int(eligible.sum())
        n_tagged = int((eligible & (summary.column("has_mm") == 1)).sum())
        calls, _ = load_calls(ctx)
        cut = float(self.parameters["filter_threshold"]) * 255.0
        ml_sum = ml_values = sites = low = 0
        for chunk in calls.chunks(mask):
            # ML statistics describe the caller's own probabilities: only
            # explicit ML values count, never implicit "." canonical calls.
            has_m = (chunk["channels"] & EXPLICIT_M) != 0
            has_h = (chunk["channels"] & EXPLICIT_H) != 0
            ml_sum += int(chunk["p_m"][has_m].sum(dtype=np.int64)) + int(chunk["p_h"][has_h].sum(dtype=np.int64))
            ml_values += int(has_m.sum()) + int(has_h.sum())
            called = chunk[has_m | has_h]
            sites += int(called.size)
            best = np.maximum(np.maximum(called["p_m"], called["p_h"]), called["p_canonical"]).astype(np.float64)
            low += int((best < cut).sum())
        return AlgorithmOutput(
            metrics=[
                metric("mod_codes", list(calls.meta.get("codes_sampled", [])), unit=None, n=int(calls.meta.get("code_sample_records", 0))),
                metric("mm_tagged_fraction", ratio(n_tagged, n_eligible), unit="fraction", n=n_eligible),
                metric("ml_mean", ml_sum / (255.0 * ml_values) if ml_values else None, unit="fraction", n=ml_values),
                metric("ml_below_filter_fraction", ratio(low, sites), unit="fraction", n=sites),
                metric("invalid_mm_records", calls.invalid_records(mask), unit="reads", n=n_tagged),
            ]
        )


MODBASE_QC = ModbaseQc("modbase-qc")
