"""Shared plumbing for Lab algorithms (not an algorithm itself)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

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

    # ``lab run --below-depth`` may run this method below its depth floor
    # (an underpowered, exploratory result).  False where the floor is part
    # of an assay-validity contract rather than a statistical-power floor.
    below_depth_allowed: ClassVar[bool] = True

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        minimum = self.requires.min_eligible_reads
        if minimum and ctx.eligible_reads is not None:
            floor = depth_floor(
                ctx, floor=minimum, observed=ctx.eligible_reads, kind="eligible_reads",
                message=f"needs {minimum:,} eligible reads; this input has {ctx.eligible_reads:,}",
                allowed=self.below_depth_allowed,
            )
            if floor is not None:
                return floor
        return Ready()


def below_depth(ctx: InputContext) -> bool:
    """True when ``lab run --below-depth`` asked to run past depth floors."""

    return ctx.extras.get("below_depth") is True


def depth_floor(
    ctx: InputContext, *, floor: int | float, observed: int | float | None, kind: str, message: str,
    allowed: bool = True,
) -> Skip | None:
    """A method depth floor: ``Skip("depth")`` below it, unless ``--below-depth``.

    The skip carries the floor, so the runner can mark a result it runs past
    the floor as underpowered.  A missing ``observed`` value is below the floor.
    """

    if observed is not None and np.isfinite(observed) and observed >= floor:
        return None
    if allowed and observed is not None and np.isfinite(observed) and below_depth(ctx):
        marks = ctx.extras.get("underpowered_marks")
        if isinstance(marks, list):
            # Inside the worker: a floor passed only because of the flag marks
            # the result even when the runner's preflight saw a different count.
            marks.append({"floor": floor, "observed": observed, "kind": kind})
        return None
    return Skip("depth", message, floor=floor, observed=observed, floor_kind=kind)


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
