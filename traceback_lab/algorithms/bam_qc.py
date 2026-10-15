"""``bam-qc``: mapping, MAPQ, chimera proxy, read quality, coverage and GC bias.

Formulas follow Appendix A of the spec, computed from the read summary
(primaries only; index statistics count secondary and supplementary records).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..read_summary import ReadSummary
from ..registry import AlgorithmOutput, InputContext
from ._base import Base, load_summary, metric, ratio

WINDOW = 1_000_000
SAMPLE_EVERY = 100


def covered_fraction(summary: ReadSummary, rows: np.ndarray) -> float | None:
    """Bases with depth >= 1 in every 100th 1 Mb window of chr1-22."""

    contig = summary.column("contig")
    start = summary.column("start")
    end = summary.column("end")
    lengths = summary.lengths
    covered = total = 0
    serial = 0
    for cid in summary.autosome_ids():
        length = lengths[int(cid)]
        on_contig = rows & (contig == cid)
        starts = start[on_contig].astype(np.int64)
        ends = end[on_contig].astype(np.int64)
        order = np.argsort(starts, kind="stable")
        starts, ends = starts[order], ends[order]
        for window_start in range(0, length, WINDOW):
            take = serial % SAMPLE_EVERY == 0
            serial += 1
            if not take:
                continue
            window_end = min(length, window_start + WINDOW)
            total += window_end - window_start
            inside = (ends > window_start) & (starts < window_end)
            covered += _union(np.clip(starts[inside], window_start, window_end), np.clip(ends[inside], window_start, window_end))
    return ratio(covered, total)


def _union(starts: np.ndarray, ends: np.ndarray) -> int:
    if starts.size == 0:
        return 0
    order = np.argsort(starts, kind="stable")
    starts, ends = starts[order], ends[order]
    running = np.maximum.accumulate(ends)
    gaps = starts[1:] > running[:-1]
    block_starts = np.concatenate(([starts[0]], starts[1:][gaps]))
    block_ends = np.concatenate((running[:-1][gaps], [running[-1]]))
    return int((block_ends - block_starts).sum())


def gc_bias_slope(summary: ReadSummary, rows: np.ndarray, fasta: str | None) -> tuple[float | None, int]:
    """OLS slope of log2(bin count / median) on bin GC, 1 Mb bins, GC 0.3-0.7."""

    if not fasta:
        return None, 0
    import pysam

    contig = summary.column("contig")
    start = summary.column("start")
    gcs: list[float] = []
    counts: list[int] = []
    with pysam.FastaFile(fasta) as reference:
        for cid in summary.autosome_ids():
            name = summary.contigs[int(cid)]
            length = summary.lengths[int(cid)]
            positions = start[rows & (contig == cid)]
            binned = np.bincount((positions // WINDOW).astype(np.int64), minlength=(length + WINDOW - 1) // WINDOW)
            for index in range((length + WINDOW - 1) // WINDOW):
                sequence = reference.fetch(name, index * WINDOW, min(length, (index + 1) * WINDOW)).upper()
                acgt = sum(sequence.count(base) for base in "ACGT")
                if acgt < 0.5 * len(sequence) or acgt == 0:
                    continue  # mostly N
                gc = (sequence.count("G") + sequence.count("C")) / acgt
                gcs.append(gc)
                counts.append(int(binned[index]))
    gc_array = np.asarray(gcs)
    count_array = np.asarray(counts, dtype=float)
    keep = (count_array > 0) & (gc_array >= 0.3) & (gc_array <= 0.7)
    if keep.sum() < 3:
        return None, int(keep.sum())
    y = np.log2(count_array[keep] / np.median(count_array[keep]))
    x = gc_array[keep]
    if np.ptp(x) == 0:
        return None, int(keep.sum())
    slope = float(np.polyfit(x, y, 1)[0])
    return slope, int(keep.sum())


@dataclass
class BamQc(Base):
    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        flag = summary.column("flag")
        rows = np.ones(summary.rows, dtype=bool) if mask is None else mask
        primary = rows & ((flag & 0x900) == 0)
        mapped = primary & ((flag & 0x4) == 0)
        supplementary = rows & ((flag & 0x800) != 0) & ((flag & 0x4) == 0)
        mapq = summary.column("mapq")
        quality = summary.column("mean_q")[primary]
        quality = quality[np.isfinite(quality)]
        n_primary = int(primary.sum())
        n_mapped = int(mapped.sum())
        duplicates = int((rows & ((flag & 0x400) != 0)).sum())
        slope, gc_bins = gc_bias_slope(summary, mapped & (mapq >= 20), ctx.extras.get("reference_fasta"))
        return AlgorithmOutput(
            metrics=[
                metric("total_reads", n_primary, unit="reads", n=n_primary),
                metric("mapped_fraction", ratio(n_mapped, n_primary), unit="fraction", n=n_primary),
                metric("mapq_ge20_fraction", ratio(int((mapped & (mapq >= 20)).sum()), n_mapped), unit="fraction", n=n_mapped),
                metric("supplementary_fraction", ratio(int(supplementary.sum()), n_mapped), unit="fraction", n=n_mapped),
                metric("mean_read_q", float(quality.mean()) if quality.size else None, unit="phred", n=int(quality.size)),
                metric("genome_ge1x_fraction", covered_fraction(summary, mapped), unit="fraction"),
                metric("gc_bias_slope", slope, unit="log2/GC", n=gc_bins),
                metric(
                    "duplicate_fraction",
                    ratio(duplicates, n_primary) if duplicates else None,
                    unit="fraction",
                    n=n_primary if duplicates else None,
                ),
            ]
        )


BAM_QC = BamQc("bam-qc")
