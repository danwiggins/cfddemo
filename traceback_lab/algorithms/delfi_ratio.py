"""``delfi-ratio``: DELFI-style short/long fragment ratio track (Appendix A).

Per 5 Mb bin on chr1–22 (bins start at 0 on each contig; a molecule belongs to
the bin holding its start): short = D21-eligible spans 100–150 bp, long =
spans 151–220 bp.  ratio = short / long per bin; bins with no long molecule
are null and left out.  The track is median-centred: centred = ratio −
median(ratio) over the kept bins.  ``ratio_sd`` = the sample standard
deviation (ddof = 1) of the centred ratios; null with fewer than 2 bins.

This is a cohort-relative profile only: there is no GC correction and no
DELFI classifier (the original DELFI code has no license; this is an in-house
implementation of the published definition).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Requirements
from ._base import Base, load_summary, metric

BIN = 5_000_000
SHORT = (100, 150)
LONG = (151, 220)


def ratio_track(contigs: list[str], lengths: list[int], contig: np.ndarray, start: np.ndarray, span: np.ndarray, ids: np.ndarray) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    is_short = (span >= SHORT[0]) & (span <= SHORT[1])
    is_long = (span >= LONG[0]) & (span <= LONG[1])
    for cid in ids:
        on = contig == cid
        n_bins = (lengths[int(cid)] + BIN - 1) // BIN
        index = (start[on] // BIN).astype(np.int64)
        short = np.bincount(index[is_short[on]], minlength=n_bins)[:n_bins]
        long_ = np.bincount(index[is_long[on]], minlength=n_bins)[:n_bins]
        for b in range(n_bins):
            rows.append(
                {
                    "contig": contigs[int(cid)],
                    "start": b * BIN,
                    "short": int(short[b]),
                    "long": int(long_[b]),
                    "ratio": float(short[b] / long_[b]) if long_[b] else None,
                }
            )
    kept = [row["ratio"] for row in rows if row["ratio"] is not None]
    median = float(np.median(kept)) if kept else 0.0
    for row in rows:
        row["centred"] = row["ratio"] - median if row["ratio"] is not None else None
    return rows


@dataclass
class DelfiRatio(Base):
    family: str = "fragmentomics"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=1_000_000))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"bin_bp": BIN, "short": list(SHORT), "long": list(LONG), "centre": "median", "gc_correction": False}
    )

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        ids = summary.autosome_ids()
        eligible = summary.eligible(contigs=ids)
        if mask is not None:
            eligible &= mask
        rows = ratio_track(
            summary.contigs,
            summary.lengths,
            summary.column("contig")[eligible],
            summary.column("start")[eligible].astype(np.int64),
            summary.spans()[eligible],
            ids,
        )
        centred = np.asarray([row["centred"] for row in rows if row["centred"] is not None], dtype=float)
        sd = float(np.std(centred, ddof=1)) if centred.size >= 2 else None
        panel = {"bin_bp": BIN, "median_ratio_removed": True, "bins": rows}
        return AlgorithmOutput(
            metrics=[metric("ratio_sd", sd, unit="ratio", n=int(centred.size))],
            panels=[{"panel_id": "ratio-track", "kind": "density", "data_ref": "artifacts/ratio_track.json"}],
            artifacts={"ratio_track.json": json.dumps(panel, separators=(",", ":")).encode()},
        )


DELFI_RATIO = DelfiRatio("delfi-ratio")
