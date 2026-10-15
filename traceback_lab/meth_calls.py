"""D28 ``meth-calls``: site-level CpG methylation calls, decoded once, streamed in chunks.

The producer reads the aligned BAM in the same record order as the read
summary (so ``read_row`` indexes the summary and derived inputs reuse it as a
row mask, D33) and writes fixed-size chunk files of a numpy structured array
(``calls-<n>.bin``) plus ``meta.json``.  Consumers iterate the chunks with
``np.memmap``, so peak RSS is bounded by the chunk size, not by depth.

One row per CpG **site call** on a read (deviation from the spec's
one-row-per-mod-code layout, recorded in the spec audit trail: 5mC and 5hmC at
the same site share a row, which keeps the per-site argmax a row-local
operation for streaming consumers):

- ``read_row`` (u4): row index in the read summary;
- ``contig`` (i2), ``ref_pos0`` (i4): the CpG's C position on the forward
  reference strand (a reverse-strand call on the G maps to the C one base to
  its left);
- ``strand`` (i1): 0 when the call is on the forward-strand C, 1 when on the
  reverse-strand C;
- ``p_m``, ``p_h`` (u1): ML values for C+m and C+h (0 when absent);
- ``p_canonical`` (u1): 255 − p_m − p_h, floored at 0;
- ``channels`` (u1): bit 0 = a C+m call exists, bit 1 = a C+h call exists.

Rules (D28):

- only D21-eligible records are decoded;
- query positions map to the reference with
  ``get_aligned_pairs(matches_only=True)``; a call on a base without an aligned
  reference position (insertion, soft clip) is dropped and counted;
- skip mode ``.`` (or none): every CpG C of the read that the code does not
  list is an implicit canonical call for that code; ``?``: unlisted is
  missing;
- a record whose ``MN`` differs from its sequence length, or whose MM/ML do
  not parse, contributes no calls and is counted under ``invalid_mm``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Container, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .modbase import mm_codes, mm_skip_modes, mm_tag, mn_ok

SCHEMA = "traceback.lab.meth-calls.v1"
CHUNK_CALLS = 1 << 20  # <= 1M calls per chunk (D28)
CODE_SAMPLE = 10_000
DTYPE = np.dtype(
    [
        ("read_row", "<u4"),
        ("contig", "<i2"),
        ("ref_pos0", "<i4"),
        ("strand", "<i1"),
        ("p_m", "u1"),
        ("p_h", "u1"),
        ("p_canonical", "u1"),
        ("channels", "u1"),
    ]
)
HAS_M = 1
HAS_H = 2


class _InvalidRecord(ValueError):
    pass


def _record_sites(record: Any) -> tuple[list[tuple[int, int, int, int, int]], int]:
    """``(ref_pos0, strand, p_m, p_h, channels)`` per CpG site of one record,
    and how many calls were dropped (no aligned reference base)."""

    mm = mm_tag(record)
    if mm is None:
        return [], 0
    if not mn_ok(record):
        raise _InvalidRecord("MN mismatch")
    sequence = record.query_sequence or ""
    try:
        parsed = record.modified_bases
    except (ValueError, KeyError, IndexError) as exc:
        raise _InvalidRecord(str(exc)) from exc
    if parsed is None:
        raise _InvalidRecord("MM/ML do not parse")
    reverse = record.is_reverse
    # CpG sites of the read in stored coordinates: the C of a stored CG for a
    # forward record, the G for a reverse record (the read's own C).
    sites: dict[int, dict[str, int]] = {}
    for (base, _strand, code), calls in parsed.items():
        if base not in ("C", "G") or code not in ("m", "h"):
            continue
        for position, quality in calls:
            if quality < 0:
                raise _InvalidRecord("MM call without ML")
            on_c = position + 1 < len(sequence) and sequence[position] == "C" and sequence[position + 1] == "G"
            on_g = 0 < position < len(sequence) and sequence[position - 1] == "C" and sequence[position] == "G"
            if on_c or on_g:
                sites.setdefault(position, {})[code] = int(quality)
    modes = mm_skip_modes(mm)
    implicit = [code[-1] for code, mode in modes.items() if code[:2] == "C+" and code[-1] in ("m", "h") and mode == "."]
    if implicit:
        offset = 1 if reverse else 0
        for index in range(len(sequence) - 1):
            if sequence[index] == "C" and sequence[index + 1] == "G":
                position = index + offset
                entry = sites.setdefault(position, {})
                for code in implicit:
                    entry.setdefault(code, 0)
    if not sites:
        return [], 0
    pairs = dict(record.get_aligned_pairs(matches_only=True))
    out: list[tuple[int, int, int, int, int]] = []
    dropped = 0
    for position in sorted(sites):
        ref = pairs.get(position)
        if ref is None:
            dropped += 1
            continue
        on_g = sequence[position] == "G"
        ref_pos0 = ref - 1 if on_g else ref
        strand = 1 if on_g else 0
        calls = sites[position]
        channels = (HAS_M if "m" in calls else 0) | (HAS_H if "h" in calls else 0)
        out.append((ref_pos0, strand, calls.get("m", 0), calls.get("h", 0), channels))
    return out, dropped


def write_meth_calls(
    bam_path: Path,
    out: Path,
    *,
    eligible: Callable[[Any], bool],
    contig_index: dict[str, int],
    chunk_calls: int = CHUNK_CALLS,
) -> dict[str, Any]:
    """Decode every eligible record of ``bam_path`` into chunk files under ``out``."""

    import pysam

    out.mkdir(parents=True, exist_ok=True)
    buffer = np.zeros(chunk_calls, dtype=DTYPE)
    filled = 0
    chunks: list[int] = []
    counts = {"records_decoded": 0, "records_with_mm": 0, "invalid_mm": 0, "dropped_unaligned": 0, "calls": 0}
    codes: list[str] = []
    sampled = 0

    def flush() -> None:
        nonlocal filled
        if filled:
            buffer[:filled].tofile(out / f"calls-{len(chunks):05d}.bin")
            chunks.append(filled)
            filled = 0

    with pysam.AlignmentFile(str(bam_path), "rb") as bam:
        for row, record in enumerate(bam.fetch(until_eof=True)):
            if not eligible(record):
                continue
            counts["records_decoded"] += 1
            mm = mm_tag(record)
            if mm is None:
                continue
            counts["records_with_mm"] += 1
            if sampled < CODE_SAMPLE:
                sampled += 1
                for code in mm_codes(mm):
                    if code not in codes:
                        codes.append(code)
            try:
                sites, dropped = _record_sites(record)
            except _InvalidRecord:
                counts["invalid_mm"] += 1
                continue
            counts["dropped_unaligned"] += dropped
            cid = contig_index[record.reference_name]
            for ref_pos0, strand, p_m, p_h, channels in sites:
                buffer[filled] = (row, cid, ref_pos0, strand, p_m, p_h, max(0, 255 - p_m - p_h), channels)
                filled += 1
                counts["calls"] += 1
                if filled == chunk_calls:
                    flush()
        flush()
    meta = {
        "schema": SCHEMA,
        "dtype": [list(field) for field in DTYPE.descr],
        "chunks": chunks,
        "codes_sampled": sorted(codes),
        "code_sample_records": sampled,
        **counts,
    }
    (out / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    return meta


@dataclass
class MethCalls:
    """Read side: chunk-by-chunk memmapped iteration."""

    directory: Path
    meta: dict[str, Any]

    @classmethod
    def open(cls, directory: Path) -> MethCalls:
        meta = json.loads((directory / "meta.json").read_text())
        if meta.get("schema") != SCHEMA:
            raise ValueError("not a traceback meth-calls store")
        return cls(directory, meta)

    @property
    def calls(self) -> int:
        return int(sum(self.meta["chunks"]))

    def chunks(self, row_mask: np.ndarray | None = None) -> Iterator[np.ndarray]:
        """Each chunk as a memmapped structured array, filtered by the read
        summary's row mask (a derived input) when given."""

        for index, rows in enumerate(self.meta["chunks"]):
            chunk = np.memmap(self.directory / f"calls-{index:05d}.bin", dtype=DTYPE, mode="r", shape=(rows,))
            if row_mask is not None:
                chunk = chunk[row_mask[chunk["read_row"]]]
            yield chunk


def argmax_state(chunk: np.ndarray) -> np.ndarray:
    """Per row: 0 canonical, 1 5mC, 2 5hmC (argmax over ML; ties go canonical, then 5mC)."""

    p_c = chunk["p_canonical"].astype(np.int16)
    p_m = chunk["p_m"].astype(np.int16)
    p_h = chunk["p_h"].astype(np.int16)
    state = np.zeros(chunk.size, dtype=np.int8)
    state[(p_m > p_c) & (p_m >= p_h)] = 1
    state[(p_h > p_c) & (p_h > p_m)] = 2
    return state


def contig_index(contigs: Container[str] | list[str]) -> dict[str, int]:
    return {name: index for index, name in enumerate(contigs)}  # type: ignore[arg-type]


__all__ = ["CHUNK_CALLS", "DTYPE", "HAS_H", "HAS_M", "MethCalls", "SCHEMA", "argmax_state", "write_meth_calls"]
