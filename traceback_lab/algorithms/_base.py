"""Shared plumbing for Lab algorithms (not an algorithm itself)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..read_summary import ReadSummary, derived_mask
from ..registry import InputContext, Ready, Requirements, Skip, ToolPin


@dataclass
class Base:
    """Dataclass defaults for the plug-in Protocol fields."""

    id: str
    version: str = "1"
    family: str = "qc"
    requires: Requirements = field(default_factory=Requirements)
    consumes: str = "read_summary"
    cost: str = "fast"
    parameters: dict[str, Any] = field(default_factory=dict)
    tools: tuple[ToolPin, ...] = ()
    upstream: tuple[str, ...] = ("read-summary",)

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        minimum = self.requires.min_eligible_reads
        if minimum and ctx.eligible_reads is not None and ctx.eligible_reads < minimum:
            return Skip("depth", f"needs {minimum:,} eligible reads; this input has {ctx.eligible_reads:,}")
        return Ready()


def load_summary(ctx: InputContext) -> tuple[ReadSummary, np.ndarray | None]:
    """The parent's read summary and this input's row mask (D33)."""

    upstream = ctx.extras.get("upstream", {})
    directory = upstream.get("read-summary")
    if not directory:
        raise RuntimeError("read-summary output is missing (upstream not run)")
    summary = ReadSummary.open(Path(directory) / "columns")
    return summary, derived_mask(summary, ctx.derived)


def metric(metric_id: str, value: Any, *, unit: str | None = None, n: int | None = None, **extra: Any) -> dict[str, Any]:
    if isinstance(value, float) and not np.isfinite(value):
        value = None
    if isinstance(value, np.generic):
        value = value.item()
    out: dict[str, Any] = {"metric_id": metric_id, "value": value, "unit": unit, "n": n}
    out.update(extra)
    return out


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None
