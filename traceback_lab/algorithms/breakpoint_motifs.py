"""Exploratory CpG proximity and 4-mer context at aligned reference-span ends.

Each D21-eligible molecule supplies two aligned-span end positions: start
and end-1 (0-based). These positions do not prove physical cleavage sites.
The closest reference CpG is identified by its C coordinate; a tie selects
the lower coordinate. Each valid end is one proximity observation, including
when both ends of a molecule select the same CpG.

The 4-mer context follows end_motifs: ref[start:start+4] on the left and
reverse-complement(ref[end-4:end]) on the right. Invalid or off-contig motifs
are omitted from the panel but remain in the proximity denominator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..read_summary import (
    CHUNK_ROWS,
    FLAG_EXCLUDE,
    MIN_MAPQ,
    ReadSummary,
    downsample_mask,
    split_mask,
)
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, metric, ratio
from .end_motifs import K, MOTIFS, _encode, kmer_codes, revcomp_codes

CPG_RADIUS_BP = 10
REFERENCE_WINDOW_BP = 1 << 20


def _selected_rows(
    summary: ReadSummary, ctx: InputContext, lo: int, hi: int
) -> np.ndarray:
    """D21 eligibility and the D33 derived-input predicate for one row block."""

    flags = summary.column("flag")[lo:hi]
    contigs = summary.column("contig")[lo:hi]
    mapq = summary.column("mapq")[lo:hi]
    keep = (
        ((flags & FLAG_EXCLUDE) == 0)
        & (mapq >= MIN_MAPQ)
        & np.isin(contigs, summary.canonical_ids())
    )
    if not ctx.derived:
        return keep
    transform = ctx.derived.get("transform")
    if transform == "downsample":
        keep &= downsample_mask(
            summary.column("h_ds")[lo:hi], float(ctx.derived["fraction"])
        )
    elif transform == "split":
        keep &= split_mask(summary.column("h_split")[lo:hi], str(ctx.derived["half"]))
    else:
        raise ValueError(f"no row mask for transform {transform!r}")
    return keep


def _nearest_cpg(
    encoded: np.ndarray, positions: np.ndarray, fetch_start: int
) -> np.ndarray:
    """Nearest exact CG C coordinate within ten bases, else -1."""

    cpg = np.flatnonzero((encoded[:-1] == 1) & (encoded[1:] == 2)) + fetch_start
    if cpg.size == 0:
        return np.full(positions.size, -1, dtype=np.int64)
    insertion = np.searchsorted(cpg, positions, side="left")
    left = np.maximum(insertion - 1, 0)
    right = np.minimum(insertion, cpg.size - 1)
    left_distance = np.where(
        insertion > 0, positions - cpg[left], np.iinfo(np.int64).max
    )
    right_distance = np.where(
        insertion < cpg.size, cpg[right] - positions, np.iinfo(np.int64).max
    )
    choose_left = left_distance <= right_distance  # lower C on a tie
    nearest = np.where(choose_left, cpg[left], cpg[right])
    distance = np.where(choose_left, left_distance, right_distance)
    return np.where(distance <= CPG_RADIUS_BP, nearest, -1)


@dataclass
class BreakpointMotifs(Base):
    family: str = "fragmentomics"
    # Approved L2 catalog availability gate, not a validated performance cutoff.
    requires: Requirements = field(
        default_factory=lambda: Requirements(min_eligible_reads=1_000_000)
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "end_positions": "aligned_start_and_end_minus_one",
            "cpg_reference_coordinate": "C_of_exact_CG_0_based",
            "cpg_radius_bp_inclusive": CPG_RADIUS_BP,
            "nearest_tie": "lower_C_coordinate",
            "end_counting": "two_independent_ends_per_molecule",
            "motif_k": K,
            "motif_orientation": "left_forward_right_reverse_complement",
            "methylation_link": "unavailable_without_optional_upstream",
        }
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if not ctx.extras.get("reference_fasta"):
            return Skip("reference", "the registered reference FASTA is not available")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        import pysam

        directory = ctx.extras.get("upstream", {}).get("read-summary")
        if not directory:
            raise RuntimeError("read-summary output is missing (upstream not run)")
        summary = ReadSummary.open(Path(directory) / "columns")
        contigs = summary.column("contig")
        starts = summary.column("start")
        ends = summary.column("end")
        near_counts = np.zeros(len(MOTIFS), dtype=np.int64)
        far_counts = np.zeros(len(MOTIFS), dtype=np.int64)
        invalid_context = np.zeros(2, dtype=np.int64)  # near, far
        n_valid = n_near = 0

        with pysam.FastaFile(str(ctx.extras["reference_fasta"])) as reference:
            for lo in range(0, summary.rows, CHUNK_ROWS):
                hi = min(summary.rows, lo + CHUNK_ROWS)
                selected = _selected_rows(summary, ctx, lo, hi)
                if not np.any(selected):
                    continue
                chunk_contigs = contigs[lo:hi][selected]
                chunk_starts = starts[lo:hi][selected].astype(np.int64)
                chunk_ends = ends[lo:hi][selected].astype(np.int64)
                for cid in np.unique(chunk_contigs):
                    on_contig = chunk_contigs == cid
                    length = summary.lengths[int(cid)]
                    first = chunk_starts[on_contig]
                    last = chunk_ends[on_contig]
                    positive = last > first
                    left = first[positive & (first >= 0) & (first < length)]
                    right = last[positive & (last >= 1) & (last <= length)] - 1
                    positions = np.concatenate((left, right))
                    if positions.size == 0:
                        continue
                    is_right = np.concatenate(
                        (
                            np.zeros(left.size, dtype=bool),
                            np.ones(right.size, dtype=bool),
                        )
                    )
                    name = summary.contigs[int(cid)]
                    if reference.get_reference_length(name) != length:
                        raise ValueError(
                            "read-summary contig length differs from pinned reference"
                        )
                    windows = positions // REFERENCE_WINDOW_BP
                    for window in np.unique(windows):
                        on_window = windows == window
                        pos = positions[on_window]
                        right_end = is_right[on_window]
                        window_start = int(window) * REFERENCE_WINDOW_BP
                        window_end = min(length, window_start + REFERENCE_WINDOW_BP)
                        margin = CPG_RADIUS_BP + K
                        fetch_start = max(0, window_start - margin)
                        fetch_end = min(length, window_end + margin + 1)
                        encoded = _encode(reference.fetch(name, fetch_start, fetch_end))
                        near = _nearest_cpg(encoded, pos, fetch_start) >= 0
                        n_valid += int(pos.size)
                        n_near += int(near.sum())
                        motif_start = (
                            np.where(right_end, pos - K + 1, pos) - fetch_start
                        )
                        codes = kmer_codes(encoded, motif_start)
                        codes[right_end] = revcomp_codes(codes[right_end])
                        valid_motif = codes >= 0
                        near_counts += np.bincount(
                            codes[near & valid_motif], minlength=len(MOTIFS)
                        )
                        far_counts += np.bincount(
                            codes[~near & valid_motif], minlength=len(MOTIFS)
                        )
                        invalid_context[0] += int((near & ~valid_motif).sum())
                        invalid_context[1] += int((~near & ~valid_motif).sum())

        panel = {
            "motif": list(MOTIFS),
            "near_count": near_counts.tolist(),
            "far_count": far_counts.tolist(),
            "near_ends": n_near,
            "far_ends": n_valid - n_near,
            "invalid_context_near": int(invalid_context[0]),
            "invalid_context_far": int(invalid_context[1]),
            "methylation_link": {
                "status": "unavailable",
                "reason": "per-end C+m calls are not supplied as an optional upstream",
            },
        }
        return AlgorithmOutput(
            metrics=[
                metric(
                    "cpg_near_end_fraction",
                    ratio(n_near, n_valid),
                    unit="fraction",
                    n=n_valid,
                )
            ],
            panels=[
                {
                    "panel_id": "cpg-end-motifs",
                    "kind": "density",
                    "data_ref": "artifacts/cpg_end_motifs.json",
                }
            ],
            artifacts={
                "cpg_end_motifs.json": json.dumps(panel, separators=(",", ":")).encode()
            },
        )


BREAKPOINT_MOTIFS = BreakpointMotifs("breakpoint-motifs")
