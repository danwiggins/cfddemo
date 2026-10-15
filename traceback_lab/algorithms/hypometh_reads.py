"""``hypometh-reads``: share of hypomethylated reads, plus U/X/M counts (Appendix A).

Per read, over its CpG site calls with a C+m channel: methylated fraction =
5mC argmax calls / calls.  Among reads with >= 4 such calls:

- ``hypometh_read_fraction`` = reads with methylated fraction < 0.25 / reads;
- ``u_x_m_counts`` = UXM classes (Loyfer 2023): U <= 0.25, M >= 0.75, X between.

Calls of one read are contiguous in ``meth-calls`` (written in read order), so
reads are aggregated chunk by chunk with the last read carried into the next
chunk; memory stays bounded by the chunk size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..meth_calls import HAS_M, argmax_state
from ..registry import AlgorithmOutput, InputContext, Requirements
from ._base import metric, ratio
from ._meth import MethBase, load_calls

MIN_CPGS = 4
HYPO_BELOW = 0.25
U_MAX = 0.25
M_MIN = 0.75


def classify_reads(totals: np.ndarray, methylated: np.ndarray) -> dict[str, int]:
    keep = totals >= MIN_CPGS
    fraction = methylated[keep] / totals[keep]
    return {
        "reads": int(keep.sum()),
        "hypo": int((fraction < HYPO_BELOW).sum()),
        "U": int((fraction <= U_MAX).sum()),
        "M": int((fraction >= M_MIN).sum()),
        "X": int(((fraction > U_MAX) & (fraction < M_MIN)).sum()),
    }


@dataclass
class HypomethReads(MethBase):
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=500_000))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"min_cpgs": MIN_CPGS, "hypo_below": HYPO_BELOW, "u_max": U_MAX, "m_min": M_MIN}
    )

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        calls, mask = load_calls(ctx)
        tally = {"reads": 0, "hypo": 0, "U": 0, "M": 0, "X": 0}
        carry_row: int | None = None
        carry = np.zeros(2, dtype=np.int64)  # [calls, methylated] of the carried read

        def add(totals: np.ndarray, methylated: np.ndarray) -> None:
            for key, value in classify_reads(totals, methylated).items():
                tally[key] += value

        for chunk in calls.chunks(mask):
            chunk = chunk[(chunk["channels"] & HAS_M) != 0]
            if chunk.size == 0:
                continue
            rows = chunk["read_row"]
            is_m = (argmax_state(chunk) == 1).astype(np.int64)
            starts = np.flatnonzero(np.concatenate(([True], rows[1:] != rows[:-1])))
            totals = np.diff(np.append(starts, rows.size)).astype(np.int64)
            methylated = np.add.reduceat(is_m, starts)
            read_ids = rows[starts]
            if carry_row is not None:
                if int(read_ids[0]) == carry_row:
                    totals[0] += carry[0]
                    methylated[0] += carry[1]
                else:
                    add(carry[:1], carry[1:])
            carry_row = int(read_ids[-1])
            carry = np.array([totals[-1], methylated[-1]], dtype=np.int64)
            add(totals[:-1], methylated[:-1])
        if carry_row is not None:
            add(carry[:1], carry[1:])
        n = tally["reads"]
        return AlgorithmOutput(
            metrics=[
                metric("hypometh_read_fraction", ratio(tally["hypo"], n), unit="fraction", n=n),
                metric("u_x_m_counts", {"U": tally["U"], "X": tally["X"], "M": tally["M"]}, unit="reads", n=n),
            ]
        )


HYPOMETH_READS = HypomethReads("hypometh-reads")
