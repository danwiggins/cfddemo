"""D33 single-pass read summary: one row per BAM record, memmapped columns.

The ``read-summary`` intermediate reads an aligned input **once** and writes
fixed-width numpy columns (raw ``.bin`` files plus ``meta.json``).  Every
pysam-only algorithm (bam-qc, depth, fragment-size, …) consumes these columns
instead of re-reading the BAM, and derived inputs (downsample fractions, split
halves) are **row masks** over the same columns:

- ``h_ds`` = h64("ds-v1" ‖ qname): downsample fraction f keeps
  ``h_ds < f · 2⁶⁴``, so smaller fractions nest inside larger ones;
- ``h_split`` = h64("split-v1" ‖ qname): half A keeps ``h_split < 2⁶³``.

``h64`` is the first 8 bytes of blake2b, read as a big-endian unsigned int.
Hashes of read names are stored; read names themselves never are.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .identity import validate_fraction

SCHEMA = "traceback.lab.read-summary.v1"
DS_SALT = b"ds-v1"
SPLIT_SALT = b"split-v1"
CHUNK_ROWS = 1 << 20
MIN_MAPQ = 20
FLAG_EXCLUDE = 0x4 | 0x100 | 0x200 | 0x400 | 0x800
# D21: chr1-22, chrX, chrY (hg38 naming).  A reference with none of these
# (a toy test reference) falls back to every contig, like the signed policy.
_CANONICAL = re.compile(r"^chr([1-9]|1[0-9]|2[0-2]|X|Y)$")
_AUTOSOME = re.compile(r"^chr([1-9]|1[0-9]|2[0-2])$")
_CHRM = re.compile(r"^chrM$")

COLUMNS: dict[str, str] = {
    "contig": "<i2",  # reference id, -1 when unmapped
    "start": "<i4",  # 0-based reference start, -1 when unmapped
    "end": "<i4",  # reference end (exclusive), -1 when unmapped
    "strand": "<i1",  # 0 forward, 1 reverse
    "flag": "<u2",
    "mapq": "<u1",
    "read_len": "<i4",  # original read length (sequence + hard clips)
    "clip5": "<i4",  # soft+hard clipped bases at the read's 5' end
    "clip3": "<i4",  # soft+hard clipped bases at the read's 3' end
    "mn_ok": "<i1",  # 1 when MN is absent or equals the stored sequence length
    "has_mm": "<i1",  # 1 when the record carries an MM tag
    "mean_q": "<f4",  # qs tag, else the mean base quality; NaN when neither
    "h_ds": "<u8",
    "h_split": "<u8",
}


def h64(salt: bytes, name: str) -> int:
    return int.from_bytes(hashlib.blake2b(salt + name.encode("utf-8")).digest()[:8], "big")


def _clips(cigar: list[tuple[int, int]] | None) -> tuple[int, int]:
    if not cigar:
        return 0, 0
    left = right = 0
    for operation, length in cigar:
        if operation in (4, 5):
            left += length
        else:
            break
    for operation, length in reversed(cigar):
        if operation in (4, 5):
            right += length
        else:
            break
    return left, right


def write_summary(bam_path: Path, out: Path) -> dict[str, Any]:
    """Scan ``bam_path`` once and write the columns into ``out``.

    Only ``OSError``/``ValueError`` from pysam are expected failures; they
    propagate so the worker exits non-zero and the result is ``failed``
    (D21: an empty summary is never returned as success).
    """

    import pysam

    out.mkdir(parents=True, exist_ok=True)
    streams = {name: (out / f"{name}.bin").open("wb") for name in COLUMNS}
    buffers: dict[str, list[Any]] = {name: [] for name in COLUMNS}
    rows = 0

    def flush() -> None:
        for name, values in buffers.items():
            if values:
                np.asarray(values, dtype=COLUMNS[name]).tofile(streams[name])
                values.clear()

    try:
        with pysam.AlignmentFile(str(bam_path), "rb") as bam:
            contigs = list(bam.references)
            lengths = [int(length) for length in bam.lengths]
            try:
                index_mapped = int(sum(stat.mapped for stat in bam.get_index_statistics()))
            except (OSError, ValueError):
                index_mapped = None
            for record in bam.fetch(until_eof=True):
                name = record.query_name or ""
                unmapped = record.is_unmapped
                left, right = _clips(record.cigartuples)
                reverse = record.is_reverse
                sequence_length = record.query_length or 0
                hard = sum(length for op, length in (record.cigartuples or ()) if op == 5)
                mn_ok = 1
                if record.has_tag("MN"):
                    mn_ok = int(int(record.get_tag("MN")) == sequence_length)
                if record.has_tag("qs"):
                    mean_q = float(record.get_tag("qs"))
                else:
                    qualities = record.query_qualities
                    mean_q = float(np.frombuffer(qualities, dtype=np.uint8).mean()) if qualities else float("nan")
                buffers["contig"].append(-1 if unmapped else record.reference_id)
                buffers["start"].append(-1 if unmapped else record.reference_start)
                buffers["end"].append(-1 if unmapped else record.reference_end)
                buffers["strand"].append(1 if reverse else 0)
                buffers["flag"].append(record.flag)
                buffers["mapq"].append(record.mapping_quality)
                buffers["read_len"].append(sequence_length + hard)
                buffers["clip5"].append(right if reverse else left)
                buffers["clip3"].append(left if reverse else right)
                buffers["mn_ok"].append(mn_ok)
                buffers["has_mm"].append(int(record.has_tag("MM") or record.has_tag("Mm")))
                buffers["mean_q"].append(mean_q)
                buffers["h_ds"].append(h64(DS_SALT, name))
                buffers["h_split"].append(h64(SPLIT_SALT, name))
                rows += 1
                if len(buffers["flag"]) >= CHUNK_ROWS:
                    flush()
            flush()
    finally:
        for stream in streams.values():
            stream.close()
    meta = {
        "schema": SCHEMA,
        "rows": rows,
        "columns": COLUMNS,
        "contigs": contigs,
        "lengths": lengths,
        "index_mapped_upper_bound": index_mapped,
    }
    (out / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    return meta


@dataclass
class ReadSummary:
    """Memmapped view of one read summary."""

    directory: Path
    meta: dict[str, Any]

    @classmethod
    def open(cls, directory: Path) -> ReadSummary:
        meta = json.loads((directory / "meta.json").read_text())
        if meta.get("schema") != SCHEMA:
            raise ValueError("not a traceback read summary")
        return cls(directory=directory, meta=meta)

    @property
    def rows(self) -> int:
        return int(self.meta["rows"])

    @property
    def contigs(self) -> list[str]:
        return list(self.meta["contigs"])

    @property
    def lengths(self) -> list[int]:
        return [int(value) for value in self.meta["lengths"]]

    def column(self, name: str) -> np.ndarray:
        dtype = np.dtype(COLUMNS[name])
        if self.rows == 0:
            return np.zeros(0, dtype=dtype)
        return np.memmap(self.directory / f"{name}.bin", dtype=dtype, mode="r", shape=(self.rows,))

    def contig_ids(self, pattern: re.Pattern[str]) -> np.ndarray:
        return np.array([index for index, name in enumerate(self.contigs) if pattern.fullmatch(name)], dtype=np.int16)

    def canonical_ids(self) -> np.ndarray:
        """chr1–22, chrX, chrY (or every contig when none match, e.g. a toy reference)."""

        ids = self.contig_ids(_CANONICAL)
        return ids if ids.size else np.arange(len(self.contigs), dtype=np.int16)

    def autosome_ids(self) -> np.ndarray:
        ids = self.contig_ids(_AUTOSOME)
        return ids if ids.size else self.canonical_ids()

    def chrm_ids(self) -> np.ndarray:
        return self.contig_ids(_CHRM)

    def canonical_length(self) -> int:
        lengths = self.lengths
        return int(sum(lengths[index] for index in self.canonical_ids()))

    # -- predicates -----------------------------------------------------------
    def primary(self) -> np.ndarray:
        return (self.column("flag") & 0x900) == 0

    def eligible(self, *, contigs: np.ndarray | None = None, min_mapq: int = MIN_MAPQ) -> np.ndarray:
        """D21: primary, mapped, not QC-fail/duplicate, MAPQ ≥ 20, on ``contigs``."""

        flag = self.column("flag")
        ids = self.canonical_ids() if contigs is None else contigs
        return (
            ((flag & FLAG_EXCLUDE) == 0)
            & (self.column("mapq") >= min_mapq)
            & np.isin(self.column("contig"), ids)
        )

    def spans(self) -> np.ndarray:
        return (self.column("end").astype(np.int64) - self.column("start").astype(np.int64))


def downsample_mask(h_ds: np.ndarray, fraction: float) -> np.ndarray:
    """Rows kept by downsample fraction ``f``: ``h_ds < f · 2⁶⁴`` (nests by construction)."""

    from fractions import Fraction
    from math import ceil

    fraction = validate_fraction(fraction)
    # For an integer h: h < f·2⁶⁴  ⇔  h < ceil(f·2⁶⁴) (exact rational arithmetic).
    threshold = ceil(Fraction(fraction) * 2**64)
    return h_ds < np.uint64(threshold)


def split_mask(h_split: np.ndarray, half: str) -> np.ndarray:
    if half not in {"A", "B"}:
        raise ValueError("a split half is A or B")
    below = h_split < np.uint64(2**63)
    return below if half == "A" else ~below


def derived_mask(summary: ReadSummary, derived: Mapping[str, Any] | None) -> np.ndarray | None:
    """The row mask of a derived input (``None`` for the full input)."""

    if not derived:
        return None
    transform = derived.get("transform")
    if transform == "downsample":
        return downsample_mask(summary.column("h_ds"), float(derived["fraction"]))
    if transform == "split":
        return split_mask(summary.column("h_split"), str(derived["half"]))
    raise ValueError(f"no row mask for transform {transform!r}")


def depth_stamp(summary: ReadSummary, mask: np.ndarray | None = None) -> dict[str, Any]:
    """D33 depth: eligible counts, never index counts (shown as an upper bound)."""

    eligible = summary.eligible()
    if mask is not None:
        eligible &= mask
    spans = summary.spans()[eligible]
    genome = summary.canonical_length() or 1
    total = int(spans.sum()) if spans.size else 0
    return {
        "eligible_reads": int(eligible.sum()),
        "mean_span_bp": round(float(spans.mean()), 2) if spans.size else None,
        "approx_depth_x": round(total / genome, 6),
        "index_mapped_upper_bound": summary.meta.get("index_mapped_upper_bound"),
    }


def span_counts(summary: ReadSummary, mask: np.ndarray | None = None) -> dict[int, int]:
    """The Lab's 1-bp span counter over D21-eligible records (parity-tested
    against ``measurement.scan_records`` binned into the 7 local bins)."""

    eligible = summary.eligible()
    if mask is not None:
        eligible &= mask
    spans = summary.spans()[eligible]
    spans = spans[spans > 0]
    values, counts = np.unique(spans, return_counts=True)
    return {int(value): int(count) for value, count in zip(values, counts)}


def bin_counts(counts: Mapping[int, int], edges: Iterable[int]) -> list[int]:
    bounds = list(edges)
    out = [0] * len(bounds)
    for span, count in counts.items():
        for index, lower in enumerate(bounds):
            upper = bounds[index + 1] if index + 1 < len(bounds) else None
            if span >= lower and (upper is None or span < upper):
                out[index] += count
                break
    return out


__all__ = [
    "COLUMNS",
    "ReadSummary",
    "bin_counts",
    "depth_stamp",
    "derived_mask",
    "downsample_mask",
    "h64",
    "span_counts",
    "split_mask",
    "write_summary",
]
