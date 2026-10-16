"""``pmd-score``: CpG 5mC inside vs outside common PMDs (Appendix A).

The Zhou 2018 hg38 PMD annotation (``PMD_coordinates_hg38.bed.gz``: 100 kb
bins labelled ``commonPMD`` / ``commonHMD`` / ``Neither``) is a Lab asset at
``ROOT/lab/assets/pmd/PMD_coordinates_hg38.bed.gz``.  Its sha256 is pinned in
the parameters (so it enters the execution identity, D19); a missing or
different file skips with ``missing_asset``.

Over CpG site calls with a C+m channel (argmax call, both strands):

- ``pmd_meth``: 5mC fraction of calls inside ``commonPMD`` intervals;
- ``non_pmd_meth``: 5mC fraction of every other call;
- ``pmd_delta`` = non_pmd_meth − pmd_meth (positive = PMD hypomethylation).

Zhou 2018 measured solo-WCGW CpGs; Appendix A uses every CpG call, so values
are not comparable with solo-WCGW numbers.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..assets import AssetError, PMD_FILENAME, PMD_SHA256, inspect_asset
from ..meth_calls import HAS_M, argmax_state
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import load_summary, metric, ratio
from ._meth import MethBase, load_calls

ASSET_NAME = PMD_FILENAME
ASSET_SHA256 = PMD_SHA256
LABEL = "commonPMD"
# The annotation is in hg38 coordinates: refuse any other reference.
HG38_CHR1_LENGTH = 248_956_422


def asset_path(root: Path) -> Path:
    return root / "lab" / "assets" / "pmd" / ASSET_NAME


def read_intervals(path: Path, label: str = LABEL) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Sorted, merged ``label`` intervals per contig (0-based half-open)."""

    raw: dict[str, list[tuple[int, int]]] = {}
    with gzip.open(path, "rt", encoding="ascii") as stream:
        for line in stream:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 6 or fields[5] != label:
                continue
            raw.setdefault(fields[0], []).append((int(fields[1]), int(fields[2])))
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for contig, intervals in raw.items():
        intervals.sort()
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        array = np.asarray(merged, dtype=np.int64)
        out[contig] = (array[:, 0], array[:, 1])
    return out


def inside(positions: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    index = np.searchsorted(starts, positions, side="right") - 1
    ok = index >= 0
    result = np.zeros(positions.size, dtype=bool)
    result[ok] = positions[ok] < ends[index[ok]]
    return result


@dataclass
class PmdScore(MethBase):
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=500_000))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"asset": ASSET_NAME, "asset_sha256": ASSET_SHA256, "label": LABEL, "call": "argmax"}
    )

    def asset_sha256s(self, root: Path) -> list[str]:
        from ..store import LabStore

        try:
            return [inspect_asset(LabStore(root), expected_sha256=str(self.parameters["asset_sha256"])).sha256]
        except AssetError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        summary, _ = load_summary(ctx)
        lengths = dict(zip(summary.contigs, summary.lengths))
        if lengths.get("chr1") != HG38_CHR1_LENGTH:
            return Skip("reference", "the PMD annotation is in hg38 coordinates; this reference is not hg38")
        from ..store import LabStore

        try:
            inspect_asset(LabStore(ctx.root), expected_sha256=str(self.parameters["asset_sha256"]))
        except AssetError as exc:
            return Skip("missing_asset", str(exc))
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        try:
            asset = inspect_asset(LabStore(ctx.root), expected_sha256=str(self.parameters["asset_sha256"]))
        except AssetError as exc:
            raise RuntimeError("PMD asset changed after preflight") from exc
        intervals = read_intervals(asset.file)
        summary, _ = load_summary(ctx)
        calls, mask = load_calls(ctx)
        tally = np.zeros(4, dtype=np.int64)  # in_sites, in_meth, out_sites, out_meth
        for chunk in calls.chunks(mask):
            chunk = chunk[(chunk["channels"] & HAS_M) != 0]
            if chunk.size == 0:
                continue
            methylated = argmax_state(chunk) == 1
            in_pmd = np.zeros(chunk.size, dtype=bool)
            for cid in np.unique(chunk["contig"]):
                name = summary.contigs[int(cid)]
                if name not in intervals:
                    continue
                rows = chunk["contig"] == cid
                in_pmd[rows] = inside(chunk["ref_pos0"][rows].astype(np.int64), *intervals[name])
            tally += [in_pmd.sum(), (methylated & in_pmd).sum(), (~in_pmd).sum(), (methylated & ~in_pmd).sum()]
        pmd = ratio(int(tally[1]), int(tally[0]))
        non = ratio(int(tally[3]), int(tally[2]))
        delta = non - pmd if pmd is not None and non is not None else None
        return AlgorithmOutput(
            metrics=[
                metric("pmd_meth", pmd, unit="fraction", n=int(tally[0])),
                metric("non_pmd_meth", non, unit="fraction", n=int(tally[2])),
                metric("pmd_delta", delta, unit="fraction", n=int(tally[0] + tally[2])),
            ]
        )


PMD_SCORE = PmdScore("pmd-score")
