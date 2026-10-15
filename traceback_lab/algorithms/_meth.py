"""Shared plumbing for the streaming ``meth-calls`` consumers (D28)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..meth_calls import MethCalls
from ..registry import InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary


def load_calls(ctx: InputContext) -> tuple[MethCalls, np.ndarray | None]:
    """The parent's call store and this input's read-summary row mask (D33)."""

    directory = ctx.extras.get("upstream", {}).get("meth-calls")
    if not directory:
        raise RuntimeError("meth-calls output is missing (upstream not run)")
    calls = MethCalls.open(Path(directory) / "calls")
    _, mask = load_summary(ctx)
    return calls, mask


@dataclass
class MethBase(Base):
    """A streaming consumer of ``meth-calls`` (row-masked for derived inputs)."""

    family: str = "methylation"
    consumes: str = "meth_calls"
    upstream: tuple[str, ...] = ("read-summary", "meth-calls")
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True))

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        calls, _ = load_calls(ctx)
        if calls.meta.get("records_with_mm", 0) == 0:
            return Skip("missing_tags", "no eligible read carries an MM tag")
        return Ready()


__all__ = ["MethBase", "load_calls"]
