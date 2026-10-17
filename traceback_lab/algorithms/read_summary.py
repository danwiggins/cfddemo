"""``read-summary``: the D33 single-pass intermediate every pysam algorithm reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..read_summary import ReadSummary, depth_stamp, write_summary
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, metric


@dataclass
class ReadSummaryIntermediate(Base):
    requires: Requirements = field(default_factory=lambda: Requirements(platforms=frozenset({"ont", "illumina"})))
    upstream: tuple[str, ...] = field(default=())
    consumes: str = "full_bam"

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        write_summary(ctx.input_dir / "aligned.bam", out / "columns")
        stamp = depth_stamp(ReadSummary.open(out / "columns"))
        return AlgorithmOutput(
            metrics=[
                metric("eligible_reads", stamp["eligible_reads"], unit="reads"),
                metric("mean_span_bp", stamp["mean_span_bp"], unit="bp", n=stamp["eligible_reads"]),
                metric("approx_depth_x", stamp["approx_depth_x"], unit="x", n=stamp["eligible_reads"]),
                metric("index_mapped_upper_bound", stamp["index_mapped_upper_bound"], unit="records"),
            ]
        )


READ_SUMMARY = ReadSummaryIntermediate("read-summary")
