"""``long-tail``: pre-analytic long-fragment shares (Appendix A).

Over D21-eligible molecules: spans > 300 bp, 1,000–4,000 bp and > 7,500 bp,
each divided by all molecules.  A molecule whose read is **supplementary-split**
(its primary has at least one supplementary record) uses the read length
(sequence plus hard clips) instead of the primary's aligned span, because the
span of one split piece understates the molecule.  A primary is split when
any supplementary record shares its read-name hash (``h_ds``) in the read
summary, so no read name is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..read_summary import ReadSummary
from ..registry import AlgorithmOutput, InputContext
from ._base import Base, load_summary, metric, ratio

GT300 = 300
KB1_4 = (1000, 4000)
GT7500 = 7500


def molecule_lengths(summary: ReadSummary, mask: np.ndarray | None) -> tuple[np.ndarray, int]:
    """One length per eligible molecule, and how many used the read length."""

    eligible = summary.eligible()
    if mask is not None:
        eligible &= mask
    flag = summary.column("flag")
    supplementary = ((flag & 0x800) != 0) & ((flag & 0x4) == 0)
    split_hashes = np.unique(summary.column("h_ds")[supplementary])
    lengths = summary.spans()[eligible]
    if split_hashes.size == 0 or lengths.size == 0:
        return lengths, 0
    split = np.isin(summary.column("h_ds")[eligible], split_hashes)
    lengths = lengths.copy()
    lengths[split] = summary.column("read_len")[eligible][split]
    return lengths, int(split.sum())


@dataclass
class LongTail(Base):
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"gt": GT300, "kb_band": list(KB1_4), "lysis_gt": GT7500, "split_uses": "read_len"}
    )

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        lengths, _split = molecule_lengths(summary, mask)
        n = int(lengths.size)
        lo, hi = KB1_4
        return AlgorithmOutput(
            metrics=[
                metric("gt300_fraction", ratio(int((lengths > GT300).sum()), n), unit="fraction", n=n),
                metric(
                    "kb1_4_fraction", ratio(int(((lengths >= lo) & (lengths <= hi)).sum()), n), unit="fraction", n=n
                ),
                metric("gt7500_fraction", ratio(int((lengths > GT7500).sum()), n), unit="fraction", n=n),
            ],
            artifacts={},
        )


LONG_TAIL = LongTail("long-tail")
