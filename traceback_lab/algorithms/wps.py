"""Genome-wide long-fragment windowed protection score (WPS).

For each base-centred 120 bp window, count 120–180 bp molecules that span the
entire window and subtract molecules with either aligned-span endpoint inside it.
The exact per-base scores are streamed as compressed 1 Mb chunks, with a
separate 1 kb mean preview. These are descriptive tracks, not peak calls.
"""

from __future__ import annotations

import io
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Ready, Skip
from ._base import Base, load_summary, metric

WINDOW = 120
HALF = WINDOW // 2
FRAGMENT_RANGE = (120, 180)
BIN = 1_000
CHUNK = 1_000_000
MIN_DEPTH_X = 10.0
DISK_MARGIN_BYTES = 2 * 1024**3
AUTOSOMES = frozenset(f"chr{number}" for number in range(1, 23))


def _add_intervals(diff: np.ndarray, left: np.ndarray, right: np.ndarray,
                   lo: int, hi: int, sign: int) -> None:
    """Add signed half-open centre intervals clipped to the current chunk."""

    left = np.maximum(left, lo)
    right = np.minimum(right, hi)
    valid = left < right
    if not np.any(valid):
        return
    size = hi - lo + 1
    diff += sign * np.bincount(left[valid] - lo, minlength=size)
    diff -= sign * np.bincount(right[valid] - lo, minlength=size)


def score_centres(starts: np.ndarray, ends: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Exact integer WPS for centre coordinates [lo, hi), using half-open reads."""

    diff = np.zeros(hi - lo + 1, dtype=np.int64)
    # [start, end) spans [c-60, c+60) iff c in [start+60, end-60].
    _add_intervals(diff, starts + HALF, ends - HALF + 1, lo, hi, 1)
    # Physical aligned-span endpoints are start and end-1. An endpoint p is
    # in [c-60, c+60) iff c is in [p-59, p+60].
    _add_intervals(diff, starts - HALF + 1, starts + HALF + 1, lo, hi, -1)
    _add_intervals(diff, ends - HALF, ends + HALF, lo, hi, -1)
    # A 120 bp fragment can put both ends in the same 120 bp window. WPS
    # subtracts that molecule once, rather than subtracting two endpoints.
    _add_intervals(diff, np.maximum(starts - HALF + 1, ends - HALF),
                   np.minimum(starts + HALF + 1, ends + HALF), lo, hi, 1)
    return np.cumsum(diff[:-1], dtype=np.int64)


def chromosome_track(starts: np.ndarray, ends: np.ndarray, length: int,
                     *, bin_bp: int = BIN, chunk_bp: int = CHUNK,
                     emit: Callable[[int, np.ndarray], None] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return bin means; optionally stream every exact per-base chunk."""

    if bin_bp < 1 or chunk_bp < 1:
        raise ValueError("bin and chunk sizes must be positive")
    bins = (length + bin_bp - 1) // bin_bp
    sums = np.zeros(bins, dtype=np.int64)
    counts = np.zeros(bins, dtype=np.int64)
    spans = ends - starts
    selected = (starts >= 0) & (ends <= length) & (spans >= FRAGMENT_RANGE[0]) & (spans <= FRAGMENT_RANGE[1])
    starts = starts[selected].astype(np.int64)
    ends = ends[selected].astype(np.int64)
    order = np.argsort(starts, kind="stable")
    starts, ends = starts[order], ends[order]
    last = max(HALF, length - HALF + 1)
    for lo in range(HALF, last, chunk_bp):
        hi = min(lo + chunk_bp, last)
        # A fragment more than 180 bp upstream or 60 bp downstream cannot
        # contribute either a spanning count or an endpoint to this chunk.
        low = np.searchsorted(starts, lo - FRAGMENT_RANGE[1] - HALF, side="left")
        high = np.searchsorted(starts, hi + HALF, side="right")
        score = score_centres(starts[low:high], ends[low:high], lo, hi)
        if emit is not None:
            emit(lo, score)
        first_bin = lo // bin_bp
        centres = np.arange(lo, hi, dtype=np.int64) // bin_bp - first_bin
        local_bins = int(centres[-1]) + 1
        sums[first_bin:first_bin + local_bins] += np.bincount(
            centres, weights=score, minlength=local_bins).astype(np.int64)
        counts[first_bin:first_bin + local_bins] += np.bincount(
            centres, minlength=local_bins).astype(np.int64)
    mean = np.divide(sums, counts, out=np.zeros(bins, dtype=np.float64), where=counts > 0)
    return mean.astype(np.float32), counts.astype(np.int32)


@dataclass
class Wps(Base):
    family: str = "fragmentomics"
    cost: str = "slow"
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "window_bp": WINDOW, "fragment_bp": list(FRAGMENT_RANGE), "bin_bp": BIN,
        "endpoint": "aligned span start and end-1", "min_depth_x": MIN_DEPTH_X,
        "track": "exact per-base scores in 1 Mb chunks plus 1 kb mean preview",
    })

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.platform != "ont":
            return Skip("platform", "long-fragment WPS needs single-molecule ONT aligned spans")
        if ctx.reference_id != "hg38":
            return Skip("reference", "long-fragment WPS needs hg38 coordinates")
        if ctx.approx_depth_x is None or ctx.approx_depth_x < MIN_DEPTH_X:
            return Skip("depth", "long-fragment WPS needs at least 10x approximate autosomal depth")
        upstream = ctx.extras.get("upstream", {}).get("read-summary")
        if not upstream:
            return Skip("upstream", "long-fragment WPS needs the read-summary output")
        try:
            metadata = json.loads((Path(upstream) / "columns" / "meta.json").read_bytes())
            contigs = metadata["contigs"]
            lengths = metadata["lengths"]
            if len(contigs) != len(lengths):
                raise ValueError("contig lengths do not match")
            if any(int(length) <= 0 for length in lengths):
                raise ValueError("invalid contig length")
        except (OSError, ValueError, KeyError, TypeError):
            return Skip("upstream", "long-fragment WPS needs valid read-summary contig lengths")
        autosomal_bp = sum(int(length) for name, length in zip(contigs, lengths)
                           if name in AUTOSOMES)
        if autosomal_bp == 0:
            return Skip("reference", "long-fragment WPS needs chr1–chr22 autosome names in the hg38 summary")
        if ctx.derived is not None:
            return Ready()  # Derived controls retain scalar/preview results without exact per-base chunks.
        # int32 exact scores occupy four bytes per valid centre before compression.
        # Allow 10% for chunk/preview overhead and 2 GiB for other scratch use.
        required = (4 * autosomal_bp * 11 + 9) // 10 + DISK_MARGIN_BYTES
        volume = ctx.root
        while not volume.exists() and volume != volume.parent:
            volume = volume.parent
        free = shutil.disk_usage(volume).free
        if free < required:
            return Skip("disk", f"long-fragment WPS needs {required / 1024**3:.1f} GiB free "
                        f"on the lab volume; {free / 1024**3:.1f} GiB available")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        decision = self.preflight(ctx)
        if isinstance(decision, Skip):
            raise ValueError(decision.message)
        summary, mask = load_summary(ctx)
        ids = np.asarray([index for index, name in enumerate(summary.contigs)
                          if name in AUTOSOMES], dtype=np.int16)
        if not ids.size:
            raise ValueError("hg38 read summary has no autosomal contigs")
        eligible = summary.eligible(contigs=ids)
        if mask is not None:
            eligible &= mask
        contig = summary.column("contig")
        starts = summary.column("start")
        ends = summary.column("end")
        tracks: dict[str, np.ndarray] = {}
        metadata: list[dict[str, Any]] = []
        total_centres = 0
        qualifying_fragments = 0
        qualifying_span_bp = 0
        exact_chunks: list[dict[str, Any]] = []
        publish_exact = ctx.derived is None
        for cid in ids:
            name = summary.contigs[int(cid)]
            on = eligible & (contig == cid)
            on_starts = starts[on]
            on_ends = ends[on]
            span = on_ends.astype(np.int64) - on_starts.astype(np.int64)
            qualifying = ((on_starts >= 0) & (on_ends <= summary.lengths[int(cid)])
                          & (span >= FRAGMENT_RANGE[0]) & (span <= FRAGMENT_RANGE[1]))
            qualifying_fragments += int(qualifying.sum())
            qualifying_span_bp += int(span[qualifying].sum(dtype=np.int64))
            def emit(start: int, score: np.ndarray) -> None:
                if np.any((score < np.iinfo(np.int32).min) | (score > np.iinfo(np.int32).max)):
                    raise OverflowError("WPS per-base score exceeds int32")
                if not publish_exact:
                    return
                relative = Path("wps_exact") / name / f"{start:010d}.npz"
                target = out / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(target, score=score.astype(np.int32))
                exact_chunks.append({"path": relative.as_posix(), "contig": name,
                                     "start": start, "stop": start + len(score),
                                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})

            mean, counts = chromosome_track(on_starts, on_ends, summary.lengths[int(cid)], emit=emit)
            tracks[name] = mean
            total_centres += int(counts.sum())
            metadata.append({"contig": name, "length_bp": summary.lengths[int(cid)], "bins": len(mean)})
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **tracks)
        autosomal_bp = sum(int(summary.lengths[int(cid)]) for cid in ids)
        qualifying_depth_x = qualifying_span_bp / autosomal_bp
        panel = {"window_bp": WINDOW, "fragment_bp": list(FRAGMENT_RANGE), "bin_bp": BIN,
                 "contigs": metadata, "valid_centres": total_centres,
                 "qualifying_fragments": qualifying_fragments,
                 "qualifying_fragment_depth_x": qualifying_depth_x,
                 "track_format": "npz float32 mean WPS per 1 kb genomic bin",
                 "exact_index": "wps_exact_index.json" if publish_exact else None}
        artifacts = {"wps_track.npz": buffer.getvalue(),
                     "wps_summary.json": json.dumps(panel, separators=(",", ":")).encode()}
        if publish_exact:
            exact_index = {"schema": "traceback.wps-exact.v1", "window_bp": WINDOW,
                           "fragment_bp": list(FRAGMENT_RANGE), "centres": "zero-based; valid [60,length-60]",
                           "chunk_format": "npz score:int32; one integer per centre in [start,stop)",
                           "chunks": exact_chunks}
            artifacts["wps_exact_index.json"] = json.dumps(exact_index, separators=(",", ":")).encode()
        return AlgorithmOutput(
            metrics=[metric("qualifying_fragment_depth_x", qualifying_depth_x,
                            unit="x", n=qualifying_fragments)],
            artifacts=artifacts,
        )


WPS = Wps("wps")
