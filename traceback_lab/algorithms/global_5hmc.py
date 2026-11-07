"""``global-5hmc``: genome-wide CpG 5hmC fraction (Appendix A).

Same as ``global-5mc`` for the C+h channel: over CpG site calls carrying a C+h
channel, the share whose argmax is 5hmC.  Skipped with ``missing_channel``
when no ``C+h`` code appears in the first 10,000 MM-tagged reads (a 5mC-only
basecall model has no 5hmC channel; it must not report zero).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..meth_calls import HAS_H, argmax_state
from ..registry import AlgorithmOutput, InputContext, Ready, Skip
from ._base import metric, ratio
from ._meth import MethBase, load_calls


@dataclass
class Global5hmc(MethBase):
    parameters: dict[str, Any] = field(default_factory=lambda: {"call": "argmax", "channel": "C+h"})

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        calls, _ = load_calls(ctx)
        if "C+h" not in calls.meta.get("codes_sampled", []):
            return Skip("missing_channel", "the MM tags carry no C+h (5hmC) channel")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        calls, mask = load_calls(ctx)
        sites = hydroxy = 0
        for chunk in calls.chunks(mask):
            chunk = chunk[(chunk["channels"] & HAS_H) != 0]
            sites += int(chunk.size)
            hydroxy += int((argmax_state(chunk) == 2).sum())
        return AlgorithmOutput(metrics=[metric("cpg_5hmc_fraction", ratio(hydroxy, sites), unit="fraction", n=sites)])


GLOBAL_5HMC = Global5hmc("global-5hmc")
