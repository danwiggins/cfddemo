"""Experimental whole-chromosome dosage screen from a low-pass BAM.

This module intentionally implements a narrower claim than ichorCNA. It counts
high-quality primary read starts in fixed autosomal bins, removes obvious
centromeric/low-mappability bins, and compares each chromosome's median with the
sample-wide median. It can screen for broad dosage shifts; it cannot estimate
tumor fraction or resolve focal/subclonal copy-number events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "copy-number-screen.v1"
AUTOSOMES = tuple(f"chr{index}" for index in range(1, 23))
WINDOW_SIZE_BP = 5_000_000
MIN_MAPQ = 20
LOW_BIN_FRACTION = 0.55
EVENT_THRESHOLD_LOG2 = 0.20

Chromosome = Annotated[
    str,
    StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2])$"),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
Classification = Literal["within_threshold", "gain_screen", "loss_screen"]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ChromosomeDosage(StrictModel):
    chromosome: Chromosome
    ordinal: int = Field(ge=1, le=22)
    read_count: int = Field(ge=0)
    full_bin_count: int = Field(ge=1)
    retained_bin_count: int = Field(ge=1)
    median_retained_bin_count: FiniteFloat = Field(gt=0)
    log2_ratio: FiniteFloat
    estimated_copy_number: FiniteFloat = Field(ge=0)
    classification: Classification

    @model_validator(mode="after")
    def validate_derived_values(self) -> ChromosomeDosage:
        if self.chromosome != f"chr{self.ordinal}":
            raise ValueError("chromosome and ordinal disagree")
        if self.retained_bin_count > self.full_bin_count:
            raise ValueError("retained bins cannot exceed full bins")
        expected_copy_number = 2.0 * (2.0**self.log2_ratio)
        if not math.isclose(
            self.estimated_copy_number,
            expected_copy_number,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("estimated copy number does not match log2 ratio")
        expected_classification: Classification
        if self.log2_ratio >= EVENT_THRESHOLD_LOG2:
            expected_classification = "gain_screen"
        elif self.log2_ratio <= -EVENT_THRESHOLD_LOG2:
            expected_classification = "loss_screen"
        else:
            expected_classification = "within_threshold"
        if self.classification != expected_classification:
            raise ValueError("classification does not match threshold")
        return self


class CopyNumberProvenance(StrictModel):
    schema_version: Literal["copy-number-screen.v1"] = SCHEMA_VERSION
    input_artifact_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    accepted_read_count: int = Field(ge=1)
    window_size_bp: Literal[5_000_000] = WINDOW_SIZE_BP
    minimum_mapping_quality: Literal[20] = MIN_MAPQ
    low_bin_retention_fraction: Literal[0.55] = LOW_BIN_FRACTION
    event_threshold_log2: Literal[0.2] = EVENT_THRESHOLD_LOG2
    filters: tuple[str, ...]
    method: Literal["sample_internal_chromosome_median"] = (
        "sample_internal_chromosome_median"
    )
    verification_level: Literal["recomputed"] = "recomputed"


class CopyNumberResultBundle(StrictModel):
    schema_version: Literal["copy-number-screen.v1"] = SCHEMA_VERSION
    chromosomes: tuple[ChromosomeDosage, ...] = Field(min_length=22, max_length=22)
    genome_median_bin_count: FiniteFloat = Field(gt=0)
    genome_log2_mad: FiniteFloat = Field(ge=0)
    flagged_chromosome_count: int = Field(ge=0, le=22)
    provenance: CopyNumberProvenance
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def validate_bundle(self) -> CopyNumberResultBundle:
        if tuple(row.chromosome for row in self.chromosomes) != AUTOSOMES:
            raise ValueError("chromosomes must be ordered chr1 through chr22")
        flagged = sum(
            row.classification != "within_threshold" for row in self.chromosomes
        )
        if flagged != self.flagged_chromosome_count:
            raise ValueError("flagged chromosome count is inconsistent")
        return self


def compute_dosage(
    window_counts: Mapping[str, Sequence[int]],
    *,
    input_artifact_sha256: str,
    accepted_read_count: int,
) -> CopyNumberResultBundle:
    """Compute a sample-internal whole-chromosome dosage screen."""

    retained_by_chromosome: dict[str, np.ndarray] = {}
    all_retained: list[float] = []
    for chromosome in AUTOSOMES:
        values = np.asarray(window_counts.get(chromosome, ()), dtype=float)
        if values.size == 0 or np.any(values < 0):
            raise ValueError(f"{chromosome} requires non-negative full-bin counts")
        chromosome_median = float(np.median(values))
        retained = values[values >= chromosome_median * LOW_BIN_FRACTION]
        if retained.size == 0:
            raise ValueError(f"{chromosome} has no retained bins")
        retained_by_chromosome[chromosome] = retained
        all_retained.extend(float(value) for value in retained)

    genome_median = float(np.median(all_retained))
    if genome_median <= 0:
        raise ValueError("genome-wide retained-bin median must be positive")
    log2_values = np.log2(np.asarray(all_retained) / genome_median)
    log2_mad = float(np.median(np.abs(log2_values - np.median(log2_values))))

    rows: list[ChromosomeDosage] = []
    for ordinal, chromosome in enumerate(AUTOSOMES, start=1):
        original = tuple(int(value) for value in window_counts[chromosome])
        retained = retained_by_chromosome[chromosome]
        chromosome_median = float(np.median(retained))
        log2_ratio = float(math.log2(chromosome_median / genome_median))
        if log2_ratio >= EVENT_THRESHOLD_LOG2:
            classification: Classification = "gain_screen"
        elif log2_ratio <= -EVENT_THRESHOLD_LOG2:
            classification = "loss_screen"
        else:
            classification = "within_threshold"
        rows.append(
            ChromosomeDosage(
                chromosome=chromosome,
                ordinal=ordinal,
                read_count=sum(original),
                full_bin_count=len(original),
                retained_bin_count=int(retained.size),
                median_retained_bin_count=chromosome_median,
                log2_ratio=log2_ratio,
                estimated_copy_number=2.0 * (2.0**log2_ratio),
                classification=classification,
            )
        )

    return CopyNumberResultBundle(
        chromosomes=tuple(rows),
        genome_median_bin_count=genome_median,
        genome_log2_mad=log2_mad,
        flagged_chromosome_count=sum(
            row.classification != "within_threshold" for row in rows
        ),
        provenance=CopyNumberProvenance(
            input_artifact_sha256=input_artifact_sha256,
            accepted_read_count=accepted_read_count,
            filters=(
                "autosomes chr1 through chr22",
                "primary alignments only",
                "mapped, QC-pass, non-duplicate reads",
                "mapping quality at least 20",
                "read start assigned to one 5 Mb full-length bin",
                "bins below 55% of their chromosome median excluded",
            ),
        ),
        limitations=(
            "Experimental broad whole-chromosome screen; not ichorCNA.",
            "No panel of normals, segmentation model, or tumor-fraction estimate.",
            "Sample-internal normalization cannot establish focal or subclonal events.",
            "Sex chromosomes and partial terminal bins are excluded.",
            (
                "The fixed threshold is a conservative visualization boundary, not a "
                "validated clinical cutoff."
            ),
        ),
    )


def scan_bam(path: Path) -> tuple[dict[str, tuple[int, ...]], int]:
    """Count accepted read starts in full 5 Mb autosomal bins."""

    import pysam

    counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    accepted = 0
    with pysam.AlignmentFile(path, "rb") as bam:
        lengths = dict(zip(bam.references, bam.lengths, strict=True))
        missing = set(AUTOSOMES) - set(lengths)
        if missing:
            raise ValueError(f"BAM is missing autosomes: {sorted(missing)}")
        for read in bam.fetch(until_eof=True):
            if (
                read.is_unmapped
                or read.is_secondary
                or read.is_supplementary
                or read.is_qcfail
                or read.is_duplicate
                or read.mapping_quality < MIN_MAPQ
            ):
                continue
            chromosome = bam.get_reference_name(read.reference_id)
            if chromosome not in AUTOSOMES:
                continue
            bin_index = read.reference_start // WINDOW_SIZE_BP
            if bin_index >= lengths[chromosome] // WINDOW_SIZE_BP:
                continue
            counts[chromosome][bin_index] += 1
            accepted += 1

    window_counts = {
        chromosome: tuple(
            counts[chromosome].get(index, 0)
            for index in range(lengths[chromosome] // WINDOW_SIZE_BP)
        )
        for chromosome in AUTOSOMES
    }
    return window_counts, accepted


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle(bundle: CopyNumberResultBundle, output_path: Path) -> None:
    """Atomically write one strictly validated aggregate result."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        bundle.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, output_path)


def cli_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the experimental whole-chromosome dosage screen."
    )
    parser.add_argument("--bam", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/local/copy-number/result.json"),
    )
    args = parser.parse_args(argv)
    window_counts, accepted = scan_bam(args.bam)
    bundle = compute_dosage(
        window_counts,
        input_artifact_sha256=sha256_file(args.bam),
        accepted_read_count=accepted,
    )
    write_bundle(bundle, args.output)
    print(
        f"Wrote {args.output}: {accepted:,} accepted reads, "
        f"{bundle.flagged_chromosome_count} broad events."
    )
    return 0


__all__ = [
    "AUTOSOMES",
    "EVENT_THRESHOLD_LOG2",
    "CopyNumberResultBundle",
    "compute_dosage",
    "scan_bam",
]
