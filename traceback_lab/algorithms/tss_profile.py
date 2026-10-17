"""Autosomal protein-coding TSS coverage from pinned GENCODE v46 basic CHR.

Gene-feature boundaries supply one TSS per gene. D21-eligible primary molecule
spans contribute their overlap bases to strand-oriented 50 bp bins in ±1 kb.
The reported ratio is mean coverage at [-150,+50) divided by mean coverage
at [-1000,-500) and [+500,+1000). This is descriptive, not expression calling.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from ..assets import AssetError, TSS_KIND, TSS_SHA256, inspect_asset, open_verified_asset
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ..store import LabStore
from ._base import Base, load_summary, metric

HG38_CHR1_LENGTH = 248_956_422
WINDOW = 1000
BIN = 50
MIN_READS = 5_000_000
OFFSETS = np.arange(-WINDOW, WINDOW, BIN, dtype=np.int64)
AUTOSOMES = frozenset(f"chr{number}" for number in range(1, 23))
_GENE_ID = re.compile(r'gene_id "([^"]+)";')
_GENE_TYPE = re.compile(r'gene_type "([^"]+)";')


@dataclass(frozen=True)
class Tss:
    gene_id: str
    contig: str
    pos0: int
    strand: str


def read_tss(stream: BinaryIO) -> tuple[Tss, ...]:
    """Read only protein-coding gene rows; GTF starts/ends are 1-based closed."""

    genes: dict[str, Tss] = {}
    with gzip.open(stream, "rt", encoding="utf-8") as gtf:
        for line in gtf:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 9 or fields[2] != "gene" or fields[0] not in AUTOSOMES:
                continue
            if not (match := _GENE_TYPE.search(fields[8])) or match.group(1) != "protein_coding":
                continue
            gene = _GENE_ID.search(fields[8])
            if gene is None or fields[6] not in ("+", "-"):
                raise ValueError("protein-coding GTF gene has no valid identity or strand")
            start, end = int(fields[3]), int(fields[4])
            if start < 1 or end < start:
                raise ValueError("protein-coding GTF gene has invalid coordinates")
            record = Tss(gene.group(1), fields[0], start - 1 if fields[6] == "+" else end - 1, fields[6])
            if record.gene_id in genes:
                raise ValueError("GENCODE gene identifier occurs more than once")
            genes[record.gene_id] = record
    if not genes:
        raise ValueError("annotation has no autosomal protein-coding genes")
    return tuple(sorted(genes.values(), key=lambda row: (row.contig, row.pos0, row.gene_id)))


def _integral(starts: np.ndarray, ends: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Integrated per-base coverage through each position, without a genome array."""

    start_index = np.searchsorted(starts, positions, side="left")
    end_index = np.searchsorted(ends, positions, side="left")
    start_sum = np.concatenate(([0], np.cumsum(starts, dtype=np.int64)))
    end_sum = np.concatenate(([0], np.cumsum(ends, dtype=np.int64)))
    return positions * (start_index - end_index) - start_sum[start_index] + end_sum[end_index]


def coverage_profile(
    genes: tuple[Tss, ...], contigs: list[str], lengths: list[int],
    read_contig: np.ndarray, starts: np.ndarray, ends: np.ndarray,
) -> dict[str, Any]:
    """Aggregate equal-width gene windows using exact molecule-overlap bases."""

    totals = np.zeros(len(OFFSETS), dtype=np.int64)
    n_genes = 0
    by_name = {name: index for index, name in enumerate(contigs)}
    for name in sorted(AUTOSOMES & by_name.keys()):
        cid = by_name[name]
        selected = [gene for gene in genes if gene.contig == name and
                    gene.pos0 - WINDOW >= 0 and gene.pos0 + WINDOW < lengths[cid]]
        if not selected:
            continue
        on = read_contig == cid
        s = np.sort(starts[on].astype(np.int64))
        e = np.sort(ends[on].astype(np.int64))
        pos = np.asarray([gene.pos0 for gene in selected], dtype=np.int64)[:, None]
        offsets = OFFSETS[None, :]
        reverse = np.asarray([gene.strand == "-" for gene in selected])[:, None]
        left = np.where(reverse, pos - offsets - BIN + 1, pos + offsets)
        right = left + BIN
        bases = _integral(s, e, right.ravel()) - _integral(s, e, left.ravel())
        totals += bases.reshape(len(selected), len(OFFSETS)).sum(axis=0)
        n_genes += len(selected)
    coverage = totals.astype(float) / (n_genes * BIN) if n_genes else np.zeros(len(OFFSETS))
    flank = float(np.mean(np.concatenate((coverage[:10], coverage[30:]))))
    central = float(np.mean(coverage[17:21]))
    return {
        "offset_bp": OFFSETS.tolist(), "coverage_x": coverage.tolist(),
        "gene_n": n_genes, "flank_depth_x": flank,
        "central_depth_x": central,
        "central_flank_ratio": central / flank if flank > 0 else None,
    }


@dataclass
class TssProfile(Base):
    family: str = "fragmentomics"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=MIN_READS))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "annotation": "GENCODE v46 basic CHR protein-coding gene features",
        "annotation_sha256": TSS_SHA256, "window_bp": WINDOW, "bin_bp": BIN,
        "central_bp": [-150, 50], "flank_bp": [[-1000, -500], [500, 1000]],
        "min_autosomal_eligible_reads": MIN_READS,
    })

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return [inspect_asset(LabStore(root), TSS_KIND, expected_sha256=TSS_SHA256).sha256]
        except AssetError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        summary, mask = load_summary(ctx)
        if dict(zip(summary.contigs, summary.lengths)).get("chr1") != HG38_CHR1_LENGTH:
            return Skip("reference", "TSS annotation requires hg38 chr1 coordinates")
        eligible = summary.eligible(contigs=summary.autosome_ids())
        if mask is not None:
            eligible &= mask
        count = int(eligible.sum())
        if count < MIN_READS:
            return Skip("depth", f"TSS profile needs {MIN_READS:,} autosomal eligible reads; this input has {count:,}")
        try:
            inspect_asset(LabStore(ctx.root), TSS_KIND, expected_sha256=TSS_SHA256)
        except AssetError:
            return Skip("missing_asset", "register the pinned GENCODE v46 basic CHR GTF")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        if dict(zip(summary.contigs, summary.lengths)).get("chr1") != HG38_CHR1_LENGTH:
            raise ValueError("TSS annotation requires hg38 chr1 coordinates")
        eligible = summary.eligible(contigs=summary.autosome_ids())
        if mask is not None:
            eligible &= mask
        if int(eligible.sum()) < MIN_READS:
            raise ValueError("TSS profile needs 5 million autosomal eligible reads")
        store = LabStore(ctx.root)
        annotation = inspect_asset(store, TSS_KIND, expected_sha256=TSS_SHA256)
        with open_verified_asset(store, annotation) as stream:
            genes = read_tss(stream)
        profile = coverage_profile(
            genes, summary.contigs, summary.lengths,
            summary.column("contig")[eligible],
            summary.column("start")[eligible], summary.column("end")[eligible],
        )
        profile["annotation_sha256"] = annotation.sha256
        profile["eligible_autosomal_reads"] = int(eligible.sum())
        return AlgorithmOutput(
            metrics=[metric("central_flank_ratio", profile["central_flank_ratio"], unit="ratio", n=profile["gene_n"]),
                     metric("tss_genes", profile["gene_n"], unit="genes")],
            panels=[{"panel_id": "tss-coverage", "kind": "density", "data_ref": "artifacts/tss_profile.json"}],
            artifacts={"tss_profile.json": json.dumps(profile, separators=(",", ":")).encode()},
        )


TSS_PROFILE = TssProfile("tss-profile")
