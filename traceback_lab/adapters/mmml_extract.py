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

from ..meth_calls import _InvalidRecord, _record_sites
from ..modbase import mm_tag

EXTRACTOR_VERSION = "lab-mmml-cpg-v1"


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


def extract_cpg_calls(
    bam_path: Path,
    reference_fasta: Path,
    *,
    maximum_calls: int,
    filter_threshold: float,
    include_regions: RegionIndex | None = None,
) -> Extraction:
    """Stream a prefiltered BAM, refusing the locked call cap while reading.

    ``maximum_calls`` bounds the materialized calls handed to the existing
    Loyfer UXM classifier. The caller owns any prefilter BAM and removes it
    with its worker scratch directory.
    """

    if maximum_calls < 1 or not 0 < filter_threshold < 1:
        raise ValueError("cell-origin extraction needs positive cap and threshold in (0, 1)")
    salt = os.urandom(32)
    calls: list[ModkitCpgCall] = []
    scanned = tagged = invalid = unaligned = non_reference = outside = low_confidence = candidates = 0
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
                sites, dropped = _record_sites(record)
            except _InvalidRecord:
                invalid += 1
                continue
            unaligned += dropped
            fragment_digest = hashlib.sha256(
                salt + b"\0" + record.query_name.encode("utf-8")
            ).hexdigest()
            chromosome = record.reference_name
            for position0, reverse, p_m, p_h, _channels in sites:
                if include_regions is not None and not include_regions.overlaps(
                    chromosome, position0, position0 + 1
                ):
                    outside += 1
                    continue
                candidates += 1
                if candidates > maximum_calls:
                    raise ValueError("cell-origin MM/ML calls exceed the locked cap")
                if fasta.fetch(chromosome, position0, position0 + 2).upper() != "CG":
                    non_reference += 1
                    continue
                p_c = max(0, 255 - p_m - p_h)
                confidence = max(p_c, p_m, p_h) / 255.0
                if confidence < filter_threshold:
                    low_confidence += 1
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
        tuple(calls), scanned, tagged, invalid, unaligned, non_reference, outside, low_confidence,
    )


__all__ = ["EXTRACTOR_VERSION", "Extraction", "extract_cpg_calls"]
