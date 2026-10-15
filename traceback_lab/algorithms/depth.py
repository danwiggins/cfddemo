"""``depth``: eligible-read depth (D33); the index count is an upper bound only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..read_summary import depth_stamp
from ..registry import AlgorithmOutput, InputContext
from ._base import Base, load_summary, metric


@dataclass
class Depth(Base):
    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        stamp = depth_stamp(summary, mask)
        eligible = stamp["eligible_reads"]
        return AlgorithmOutput(
            metrics=[
                metric("eligible_reads", eligible, unit="reads", n=eligible),
                metric("mean_span_bp", stamp["mean_span_bp"], unit="bp", n=eligible),
                metric("approx_depth_x", stamp["approx_depth_x"], unit="x", n=eligible),
                metric(
                    "index_mapped_upper_bound",
                    None if mask is not None else stamp["index_mapped_upper_bound"],
                    unit="records",
                ),
            ]
        )


DEPTH = Depth("depth")
