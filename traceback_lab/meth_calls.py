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
- ``channels`` (u1): bit 0 = a C+m call exists, bit 1 = a C+h call exists,
  bit 2 / bit 3 = that C+m / C+h value is an explicit ML value (not an
  implicit ``.`` canonical call);
- ``invalid_rows.bin`` (u4): read-summary rows of MM-carrying records that
  produced no calls (``invalid_mm``), so derived inputs can mask the count.

Rules (D28):

- only D21-eligible records are decoded;
- query positions map to the reference with
  ``get_aligned_pairs(matches_only=True)``; both bases of the CpG must align to
  adjacent reference positions, else the call (in an insertion, a soft clip,
  or split by a deletion) is dropped and counted;
- skip mode ``.`` (or none): every CpG base of the code's fundamental base
  (``C+`` or ``G-``) that the code does not list is an implicit canonical call
  for that code; ``?``: unlisted is missing;
- ``codes_sampled`` holds the codes seen in the first 10,000 eligible reads
  (tagged or not), the population of the ``global-5hmc`` channel gate;
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

SCHEMA = "traceback.lab.meth-calls.v2"
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
EXPLICIT_M = 4  # the C+m value came from ML (not an implicit "." canonical call)
EXPLICIT_H = 8


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
    # CpG calls in stored coordinates: on the C of a stored CG (forward C+
    # calls) or on its G (reverse records, opposite-strand G- calls).
    sites: dict[int, dict[str, int]] = {}
    explicit: set[tuple[int, str]] = set()
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
                explicit.add((position, code))
    modes = mm_skip_modes(mm)
    complement = {"C": "G", "G": "C"}
    for code, mode in modes.items():
        base, letter = code[0], code[-1]
        if mode != "." or base not in complement or letter not in ("m", "h"):
            continue
        # Implicit canonical calls: every CpG base of the code's fundamental
        # base in the original read orientation (its complement on the stored
        # sequence of a reverse record) that the code does not list.
        target = complement[base] if reverse else base
        for index, stored in enumerate(sequence):
            if stored != target:
                continue
            on_c = stored == "C" and index + 1 < len(sequence) and sequence[index + 1] == "G"
            on_g = stored == "G" and index > 0 and sequence[index - 1] == "C"
            if on_c or on_g:
                sites.setdefault(index, {}).setdefault(letter, 0)
    if not sites:
        return [], 0
    pairs = dict(record.get_aligned_pairs(matches_only=True))
    out: list[tuple[int, int, int, int, int]] = []
    dropped = 0
    for position in sorted(sites):
        on_g = sequence[position] == "G"
        c_pos = position - 1 if on_g else position
        ref_c, ref_g = pairs.get(c_pos), pairs.get(c_pos + 1)
        # Both bases of the CpG must align, adjacently, to the reference;
        # otherwise (insertion, soft clip, deletion between them) the call
        # has no genomic CpG coordinate and is dropped.
        if ref_c is None or ref_g is None or ref_g != ref_c + 1:
            dropped += 1
            continue
        calls = sites[position]
        channels = (
            (HAS_M if "m" in calls else 0)
            | (HAS_H if "h" in calls else 0)
            | (EXPLICIT_M if (position, "m") in explicit else 0)
            | (EXPLICIT_H if (position, "h") in explicit else 0)
        )
        out.append((ref_c, 1 if on_g else 0, calls.get("m", 0), calls.get("h", 0), channels))
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
    invalid_rows: list[int] = []

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
            if sampled < CODE_SAMPLE:  # the first 10,000 eligible reads, tagged or not
                sampled += 1
                for code in mm_codes(mm or ""):
                    if code not in codes:
                        codes.append(code)
            if mm is None:
                continue
            counts["records_with_mm"] += 1
            try:
                sites, dropped = _record_sites(record)
            except _InvalidRecord:
                counts["invalid_mm"] += 1
                invalid_rows.append(row)
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
    np.asarray(invalid_rows, dtype="<u4").tofile(out / "invalid_rows.bin")
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


    def invalid_records(self, row_mask: np.ndarray | None = None) -> int:
        """Records with MM that contributed no calls (D28 ``invalid_mm``), masked."""

        path = self.directory / "invalid_rows.bin"
        if not path.exists() or int(self.meta.get("invalid_mm", 0)) == 0:
            return 0
        rows = np.fromfile(path, dtype="<u4")
        return int(rows.size if row_mask is None else row_mask[rows].sum())


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


__all__ = ["CHUNK_CALLS", "DTYPE", "EXPLICIT_H", "EXPLICIT_M", "HAS_H", "HAS_M", "MethCalls", "SCHEMA", "argmax_state", "write_meth_calls"]
