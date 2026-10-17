"""Bin-weighted segment log2 median absolute deviation (Appendix A).

This is an exploratory, cohort-relative copy-number instability measure.
The selected copy-number result is an exact D19 upstream dependency; no
Illumina threshold is applied to ONT data. A null segment estimate is a
legitimate missing measurement and is excluded from TMAD with explicit
coverage counts; malformed segment weights or nonfinite estimates fail.
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


@dataclass(frozen=True)
class TmadSummary:
    value: float | None
    bins_used: int
    segments_used: int
    bins_missing_estimate: int
    segments_missing_estimate: int


def segment_tmad(segments: list[dict[str, Any]]) -> TmadSummary:
    values: list[tuple[float, int]] = []
    missing_bins = 0
    missing_segments = 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"copy-number segment {index} is not an object")
        value, weight = segment.get("median_log2"), segment.get("n_bins")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError(f"copy-number segment {index} has invalid n_bins")
        if value is None:
            missing_bins += weight
            missing_segments += 1
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"copy-number segment {index} has invalid median_log2")
        values.append((float(value), weight))
    n = sum(weight for _, weight in values)
    centre = weighted_median(values)
    if centre is None:
        return TmadSummary(None, 0, 0, missing_bins, missing_segments)
    return TmadSummary(
        weighted_median([(abs(value - centre), weight) for value, weight in values]),
        n, len(values), missing_bins, missing_segments,
    )


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
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise ValueError("copy-number segment artifact has no segment list")
        summary = segment_tmad(payload["segments"])
        return AlgorithmOutput(metrics=[
            metric("tmad", summary.value, unit="log2", n=summary.bins_used),
            metric("segments_used", summary.segments_used, unit="segments"),
            metric("segments_missing_estimate", summary.segments_missing_estimate, unit="segments"),
            metric("bins_missing_estimate", summary.bins_missing_estimate, unit="bins"),
        ])


INSTABILITY = Instability("instability")
