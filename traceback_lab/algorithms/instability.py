"""Bin-weighted segment log2 median absolute deviation (Appendix A).

This is an exploratory, cohort-relative copy-number instability measure.
The selected copy-number result is an exact D19 upstream dependency; no
Illumina threshold is applied to ONT data.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..registry import AlgorithmOutput, InputContext, Requirements
from ._base import Base, metric


def weighted_median(values: list[tuple[float, int]]) -> float | None:
    """Median of integer-weighted values without expanding one row per bin."""

    if not values:
        return None
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return None

    def at_rank(rank: int) -> float:
        seen = 0
        for value, weight in ordered:
            seen += weight
            if rank < seen:
                return value
        raise AssertionError("rank outside weighted values")

    return (at_rank((total - 1) // 2) + at_rank(total // 2)) / 2


def segment_tmad(segments: list[dict[str, Any]]) -> tuple[float | None, int]:
    values: list[tuple[float, int]] = []
    for segment in segments:
        value, weight = segment.get("median_log2"), segment.get("n_bins")
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and isinstance(weight, int)
            and not isinstance(weight, bool) and weight > 0
        ):
            values.append((float(value), weight))
    n = sum(weight for _, weight in values)
    centre = weighted_median(values)
    if centre is None:
        return None, 0
    return weighted_median([(abs(value - centre), weight) for value, weight in values]), n


@dataclass
class Instability(Base):
    family: str = "copy-number"
    consumes: str = "counting_bam"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=2_000_000))
    upstream: tuple[str, ...] = ("copy-number",)
    parameters: dict[str, Any] = field(default_factory=lambda: {"measure": "segment_log2_weighted_mad", "weight": "n_bins"})

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        directory = ctx.extras.get("upstream", {}).get("copy-number")
        if not directory:
            raise RuntimeError("selected copy-number artifact is missing")
        payload = json.loads((Path(directory) / "cn_segments.json").read_text(encoding="utf-8"))
        value, n = segment_tmad(payload["segments"])
        return AlgorithmOutput(metrics=[metric("tmad", value, unit="log2", n=n)])


INSTABILITY = Instability("instability")
