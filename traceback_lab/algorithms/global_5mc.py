"""``global-5mc``: genome-wide CpG 5mC fraction (Appendix A).

Over every CpG site call with a C+m channel on D21-eligible reads, both
strands: the call is the argmax over (5mC, 5hmC, canonical) ML probabilities;
value = 5mC calls / all such calls.  Streams ``meth-calls`` chunk by chunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..meth_calls import HAS_M, argmax_state
from ..registry import AlgorithmOutput, InputContext
from ._base import metric, ratio
from ._meth import MethBase, load_calls


@dataclass
class Global5mc(MethBase):
    parameters: dict[str, Any] = field(default_factory=lambda: {"call": "argmax", "channel": "C+m"})

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        calls, mask = load_calls(ctx)
        sites = methylated = 0
        for chunk in calls.chunks(mask):
            chunk = chunk[(chunk["channels"] & HAS_M) != 0]
            sites += int(chunk.size)
            methylated += int((argmax_state(chunk) == 1).sum())
        return AlgorithmOutput(metrics=[metric("cpg_5mc_fraction", ratio(methylated, sites), unit="fraction", n=sites)])


GLOBAL_5MC = Global5mc("global-5mc")
