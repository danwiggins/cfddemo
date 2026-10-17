"""Lab-owned, bounded MM/ML CpG extraction for unsigned cell-origin runs.

Only explicit MM/ML probabilities and canonical calls licensed by MM skip
mode ``.`` enter this stream. A ``?`` omission, absent ML, or invalid MN is
unknown, never an unmethylated observation. Read names are salted in memory.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pysam

from evidence_inspector.cell_origin_models import CpgCallState, ModkitCpgCall, Strand
from evidence_inspector.cell_origin_prefilter import RegionIndex

from ..modbase import InvalidMM, mm_skip_modes, mm_tag, mn_ok

EXTRACTOR_VERSION = "lab-mmml-cpg-v2"


@dataclass(frozen=True)
class Extraction:
    calls: tuple[ModkitCpgCall, ...]
    records_scanned: int
    records_with_mm: int
    invalid_mm: int
    dropped_unaligned: int
    non_reference_cpg: int
    outside_regions: int
    low_confidence: int
    unsupported_modification: int


def _reference_sites(record: pysam.AlignedSegment) -> tuple[list[tuple[int, int, dict[str, int]]], int]:
    """Map cytosine MM/ML channels to reference-forward CG candidates.

    The neighboring *query* base need only align adjacently; it may mismatch
    the reference G. Modkit's ``--cpg`` selects the reference motif, not a
    motif in the read sequence.
    """

    if not mn_ok(record):
        raise InvalidMM("MN differs from stored sequence length")
    try:
        parsed = record.modified_bases
    except (ValueError, KeyError, IndexError) as exc:
        raise InvalidMM("MM/ML do not parse") from exc
    if parsed is None:
        raise InvalidMM("MM/ML do not parse")
    sequence = record.query_sequence or ""
    sites: dict[tuple[int, int], dict[str, int]] = {}
    for (base, strand, code), entries in parsed.items():
        if base != "C":
            continue
        if any(quality < 0 for _, quality in entries):
            raise InvalidMM("MM call without ML")
        for position, quality in entries:
            sites.setdefault((position, strand), {})[str(code)] = int(quality)
    target_base = "G" if record.is_reverse else "C"
    positions = [index for index, base in enumerate(sequence) if base == target_base]
    for code, mode in mm_skip_modes(mm_tag(record) or "").items():
        if code[0] != "C" or mode != ".":
            continue
        strand = int(record.is_reverse) ^ int(code[1] == "-")
        for position in positions:
            sites.setdefault((position, strand), {}).setdefault(code[2:], 0)
    cigar = record.cigartuples or []
    if len(cigar) == 1 and cigar[0][0] in {0, 7, 8} and not record.is_unmapped:
        start, span = record.reference_start, cigar[0][1]

        def reference_of(position: int) -> int | None:
            return start + position if 0 <= position < span else None

    else:
        reference_of = dict(record.get_aligned_pairs(matches_only=True)).get
    mapped: list[tuple[int, int, dict[str, int]]] = []
    dropped = 0
    for (query_position, strand), channels in sorted(sites.items()):
        mod_position = reference_of(query_position)
        adjacent = reference_of(query_position - 1 if strand else query_position + 1)
        if mod_position is None or adjacent != mod_position + (-1 if strand else 1):
            dropped += 1
            continue
        mapped.append((mod_position - strand, strand, channels))
    return mapped, dropped


def extract_cpg_calls(
    bam_path: Path,
    reference_fasta: Path,
    *,
    maximum_calls: int,
    filter_threshold: float,
    include_regions: RegionIndex | None = None,
) -> Extraction:
    """Stream a prefiltered BAM, refusing the locked call cap while reading.

    ``maximum_calls`` counts reference-CpG candidate rows before confidence
    and unsupported-code filtering, like the signed modkit output-row cap.
    Materialized UXM calls cannot exceed it. The caller owns any prefilter
    BAM and removes it with its worker scratch directory.
    """

    if maximum_calls < 1 or not 0 < filter_threshold < 1:
        raise ValueError("cell-origin extraction needs positive cap and threshold in (0, 1)")
    salt = os.urandom(32)
    calls: list[ModkitCpgCall] = []
    scanned = tagged = invalid = unaligned = non_reference = outside = low_confidence = unsupported = candidates = 0
    with pysam.AlignmentFile(str(bam_path), "rb") as bam, pysam.FastaFile(str(reference_fasta)) as fasta:
        for record in bam.fetch(until_eof=True):
            scanned += 1
            if mm_tag(record) is None:
                continue
            tagged += 1
            if not record.query_name:
                invalid += 1
                continue
            try:
                sites, dropped = _reference_sites(record)
            except InvalidMM:
                invalid += 1
                continue
            unaligned += dropped
            fragment_digest = hashlib.sha256(
                salt + b"\0" + record.query_name.encode("utf-8")
            ).hexdigest()
            chromosome = record.reference_name
            for position0, reverse, channels in sites:
                if include_regions is not None and not include_regions.overlaps(
                    chromosome, position0, position0 + 1
                ):
                    outside += 1
                    continue
                if fasta.fetch(chromosome, position0, position0 + 2).upper() != "CG":
                    non_reference += 1
                    continue
                candidates += 1
                if candidates > maximum_calls:
                    raise ValueError("cell-origin MM/ML calls exceed the locked cap")
                p_c = max(0, 255 - sum(channels.values()))
                confidence = max((p_c, *channels.values())) / 255.0
                if confidence < filter_threshold:
                    low_confidence += 1
                    continue
                p_m, p_h = channels.get("m", 0), channels.get("h", 0)
                if any(value >= max(p_c, p_m, p_h) for code, value in channels.items()
                       if code not in {"m", "h"}):
                    unsupported += 1
                    continue
                methylated = (p_m > p_c and p_m >= p_h) or (p_h > p_c and p_h > p_m)
                calls.append(ModkitCpgCall(
                    fragment_digest=fragment_digest,
                    chromosome=chromosome,
                    position0=position0,
                    strand=Strand.MINUS if reverse else Strand.PLUS,
                    modification_code="m",
                    modified_probability=confidence,
                    state=CpgCallState.METHYLATED if methylated else CpgCallState.UNMETHYLATED,
                ))
    return Extraction(
        tuple(calls), scanned, tagged, invalid, unaligned, non_reference, outside, low_confidence, unsupported,
    )


__all__ = ["EXTRACTOR_VERSION", "Extraction", "extract_cpg_calls"]
