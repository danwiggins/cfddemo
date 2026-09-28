"""Strict exploratory whole-chromosome dosage quality control.

This additive v2 contract does not reinterpret historical
``copy-number-screen.v1`` artifacts.  It makes the existing sample-internal
screen's exploratory identity explicit, binds the analysis to an exact
reference/bin definition, and accounts for every inspected alignment once.
It is not a tumor-fraction estimator or a diagnostic test.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "copy-number-dosage-qc.v2"
AUTOSOMES = tuple(f"chr{index}" for index in range(1, 23))
DEFAULT_WINDOW_SIZE_BP = 5_000_000
DEFAULT_MIN_MAPQ = 20
DEFAULT_LOW_BIN_FRACTION = 0.55
DEFAULT_VISUALIZATION_BOUNDARY_LOG2 = 0.20

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Md5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ContigName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
DosageDirection = Literal[
    "within_visualization_boundary",
    "higher_relative_dosage",
    "lower_relative_dosage",
]
SequenceIdentityVerification = Literal["md5_verified", "length_only_unverified"]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class DosageQcIdentity(StrictModel):
    method_id: Literal["sample-internal-whole-chromosome-dosage-qc"] = (
        "sample-internal-whole-chromosome-dosage-qc"
    )
    intended_use: Literal["exploratory_quality_control"] = (
        "exploratory_quality_control"
    )
    calibration_status: Literal["uncalibrated"] = "uncalibrated"
    diagnostic_interpretation_allowed: Literal[False] = False
    tumor_fraction_estimate_present: Literal[False] = False
    qualification_status: Literal["development_unqualified"] = (
        "development_unqualified"
    )


class ReferenceContigBinding(StrictModel):
    name: ContigName
    length: int = Field(gt=0)
    md5: Md5 | None = None


class ReferenceBinding(StrictModel):
    reference_id: Identifier
    assembly: Identifier
    fasta_sha256: Sha256
    contigs: tuple[ReferenceContigBinding, ...] = Field(min_length=22)
    bin_definition_sha256: Sha256
    coordinate_system: Literal["zero_based_half_open"] = "zero_based_half_open"

    @model_validator(mode="after")
    def validate_contigs(self) -> ReferenceBinding:
        names = [contig.name for contig in self.contigs]
        if len(names) != len(set(names)):
            raise ValueError("reference contig names must be unique")
        missing = set(AUTOSOMES) - set(names)
        if missing:
            raise ValueError(f"reference binding is missing autosomes: {sorted(missing)}")
        return self


class BinDefinition(StrictModel):
    contig: ContigName
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    is_terminal_partial_bin: bool

    @model_validator(mode="after")
    def increasing(self) -> BinDefinition:
        if self.end <= self.start:
            raise ValueError("bin end must be greater than start")
        return self


class BinCount(StrictModel):
    contig: ContigName
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    is_terminal_partial_bin: bool
    accepted_read_start_count: int = Field(ge=0)
    included_in_screen: bool
    exclusion_reason: Literal["terminal_partial_bin"] | None = None

    @model_validator(mode="after")
    def validate_inclusion(self) -> BinCount:
        expected_included = not self.is_terminal_partial_bin
        if self.included_in_screen != expected_included:
            raise ValueError("only complete bins may be included in the v2 screen")
        expected_reason = None if expected_included else "terminal_partial_bin"
        if self.exclusion_reason != expected_reason:
            raise ValueError("bin exclusion reason is inconsistent")
        return self


class ReadAccounting(StrictModel):
    inspected_alignment_count: int = Field(ge=0)
    accepted_autosomal_read_count: int = Field(ge=0)
    excluded_unmapped: int = Field(ge=0)
    excluded_secondary: int = Field(ge=0)
    excluded_supplementary: int = Field(ge=0)
    excluded_qc_failure: int = Field(ge=0)
    excluded_duplicate: int = Field(ge=0)
    excluded_below_mapq: int = Field(ge=0)
    excluded_non_autosomal: int = Field(ge=0)
    excluded_outside_analyzed_bins: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> ReadAccounting:
        classified = (
            self.accepted_autosomal_read_count
            + self.excluded_unmapped
            + self.excluded_secondary
            + self.excluded_supplementary
            + self.excluded_qc_failure
            + self.excluded_duplicate
            + self.excluded_below_mapq
            + self.excluded_non_autosomal
            + self.excluded_outside_analyzed_bins
        )
        if classified != self.inspected_alignment_count:
            raise ValueError("read-accounting buckets do not equal inspected alignments")
        return self


class ReferenceVerification(StrictModel):
    reference_id: Identifier
    sequence_identity: SequenceIdentityVerification
    verified_contig_count: int = Field(ge=22)


class ChromosomeDosageQc(StrictModel):
    chromosome: Annotated[
        str, StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2])$")
    ]
    ordinal: int = Field(ge=1, le=22)
    accepted_read_count: int = Field(ge=0)
    complete_bin_count: int = Field(ge=1)
    retained_complete_bin_count: int = Field(ge=1)
    median_retained_bin_count: FiniteFloat = Field(gt=0)
    log2_ratio: FiniteFloat
    relative_diploid_dosage: FiniteFloat = Field(ge=0)
    dosage_direction: DosageDirection

    @model_validator(mode="after")
    def validate_derived_values(self) -> ChromosomeDosageQc:
        if self.chromosome != f"chr{self.ordinal}":
            raise ValueError("chromosome and ordinal disagree")
        if self.retained_complete_bin_count > self.complete_bin_count:
            raise ValueError("retained bins cannot exceed complete bins")
        expected_dosage = 2.0 * (2.0**self.log2_ratio)
        if not math.isclose(
            self.relative_diploid_dosage,
            expected_dosage,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("relative diploid dosage does not match log2 ratio")
        if self.log2_ratio >= DEFAULT_VISUALIZATION_BOUNDARY_LOG2:
            expected_direction: DosageDirection = "higher_relative_dosage"
        elif self.log2_ratio <= -DEFAULT_VISUALIZATION_BOUNDARY_LOG2:
            expected_direction = "lower_relative_dosage"
        else:
            expected_direction = "within_visualization_boundary"
        if self.dosage_direction != expected_direction:
            raise ValueError("dosage direction does not match visualization boundary")
        return self


class DosageQcProvenance(StrictModel):
    input_artifact_sha256: Sha256
    reference: ReferenceBinding
    reference_verification: ReferenceVerification
    read_accounting: ReadAccounting
    window_size_bp: int = Field(gt=0)
    minimum_mapping_quality: int = Field(ge=0, le=255)
    low_bin_retention_fraction: FiniteFloat = Field(gt=0, le=1)
    visualization_boundary_log2: FiniteFloat = Field(gt=0)
    filters: tuple[str, ...]
    method: Literal["sample_internal_chromosome_median"] = (
        "sample_internal_chromosome_median"
    )
    verification_level: Literal["recomputed"] = "recomputed"


class DosageQcResultBundle(StrictModel):
    schema_version: Literal["copy-number-dosage-qc.v2"] = SCHEMA_VERSION
    identity: DosageQcIdentity = DosageQcIdentity()
    bins: tuple[BinCount, ...] = Field(min_length=22)
    chromosomes: tuple[ChromosomeDosageQc, ...] = Field(min_length=22, max_length=22)
    genome_median_bin_count: FiniteFloat = Field(gt=0)
    genome_log2_mad: FiniteFloat = Field(ge=0)
    outside_visualization_boundary_count: int = Field(ge=0, le=22)
    provenance: DosageQcProvenance
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reconcile_bundle(self) -> DosageQcResultBundle:
        if tuple(row.chromosome for row in self.chromosomes) != AUTOSOMES:
            raise ValueError("chromosomes must be ordered chr1 through chr22")
        if sum(row.accepted_read_start_count for row in self.bins) != (
            self.provenance.read_accounting.accepted_autosomal_read_count
        ):
            raise ValueError("accepted read count does not equal emitted bin counts")
        by_contig = {
            chromosome: sum(
                row.accepted_read_start_count
                for row in self.bins
                if row.contig == chromosome
            )
            for chromosome in AUTOSOMES
        }
        if any(
            row.accepted_read_count != by_contig[row.chromosome]
            for row in self.chromosomes
        ):
            raise ValueError("chromosome read count does not equal its bin counts")
        outside = sum(
            row.dosage_direction != "within_visualization_boundary"
            for row in self.chromosomes
        )
        if outside != self.outside_visualization_boundary_count:
            raise ValueError("visualization-boundary count is inconsistent")
        return self


class BamDosageQcScan(StrictModel):
    bins: tuple[BinCount, ...]
    accounting: ReadAccounting
    reference_verification: ReferenceVerification


def fixed_width_bins(
    reference: ReferenceBinding | Sequence[ReferenceContigBinding],
    *,
    window_size_bp: int = DEFAULT_WINDOW_SIZE_BP,
) -> tuple[BinDefinition, ...]:
    """Create complete-coverage autosomal bins in reference-contig order."""

    if window_size_bp <= 0:
        raise ValueError("window size must be positive")
    contigs = reference.contigs if isinstance(reference, ReferenceBinding) else reference
    lengths = {contig.name: contig.length for contig in contigs}
    missing = set(AUTOSOMES) - set(lengths)
    if missing:
        raise ValueError(f"bin source is missing autosomes: {sorted(missing)}")
    rows: list[BinDefinition] = []
    for chromosome in AUTOSOMES:
        length = lengths[chromosome]
        for start in range(0, length, window_size_bp):
            end = min(start + window_size_bp, length)
            rows.append(
                BinDefinition(
                    contig=chromosome,
                    start=start,
                    end=end,
                    is_terminal_partial_bin=end - start < window_size_bp,
                )
            )
    return tuple(rows)


def bin_definition_sha256(bins: Sequence[BinDefinition]) -> str:
    """Hash the canonical JSON representation of an ordered bin definition."""

    payload = [row.model_dump(mode="json") for row in bins]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_bins(
    reference: ReferenceBinding,
    bins: Sequence[BinDefinition],
    *,
    window_size_bp: int,
) -> None:
    """Fail closed unless bins exactly and contiguously cover every autosome."""

    expected = fixed_width_bins(reference, window_size_bp=window_size_bp)
    if tuple(bins) != expected:
        raise ValueError("bin definition is not the exact ordered reference tiling")
    if bin_definition_sha256(bins) != reference.bin_definition_sha256:
        raise ValueError("bin-definition digest does not match reference binding")


def _verify_bam_reference(bam: object, reference: ReferenceBinding) -> ReferenceVerification:
    lengths = dict(zip(bam.references, bam.lengths, strict=True))
    expected = {contig.name: contig for contig in reference.contigs}
    for chromosome in AUTOSOMES:
        if chromosome not in lengths:
            raise ValueError(f"BAM is missing bound autosome: {chromosome}")
        if lengths[chromosome] != expected[chromosome].length:
            raise ValueError(f"BAM/reference contig length mismatch: {chromosome}")

    header_rows = {
        row["SN"]: row for row in bam.header.to_dict().get("SQ", ()) if "SN" in row
    }
    all_md5_verified = True
    for chromosome in AUTOSOMES:
        expected_md5 = expected[chromosome].md5
        observed_md5 = header_rows.get(chromosome, {}).get("M5")
        if expected_md5 is not None and observed_md5 is not None:
            if observed_md5.lower() != expected_md5:
                raise ValueError(f"BAM/reference contig MD5 mismatch: {chromosome}")
        else:
            all_md5_verified = False
    return ReferenceVerification(
        reference_id=reference.reference_id,
        sequence_identity=(
            "md5_verified" if all_md5_verified else "length_only_unverified"
        ),
        verified_contig_count=len(AUTOSOMES),
    )


def scan_bam(
    path: Path,
    *,
    reference: ReferenceBinding,
    bins: Sequence[BinDefinition],
    window_size_bp: int = DEFAULT_WINDOW_SIZE_BP,
    minimum_mapping_quality: int = DEFAULT_MIN_MAPQ,
) -> BamDosageQcScan:
    """Count every BAM alignment into exactly one accounting bucket."""

    import pysam

    if not 0 <= minimum_mapping_quality <= 255:
        raise ValueError("minimum mapping quality must be between 0 and 255")
    validate_bins(reference, bins, window_size_bp=window_size_bp)
    counts = [0] * len(bins)
    bin_index = {
        (row.contig, row.start // window_size_bp): index
        for index, row in enumerate(bins)
    }
    bucket_names = (
        "accepted_autosomal_read_count",
        "excluded_unmapped",
        "excluded_secondary",
        "excluded_supplementary",
        "excluded_qc_failure",
        "excluded_duplicate",
        "excluded_below_mapq",
        "excluded_non_autosomal",
        "excluded_outside_analyzed_bins",
    )
    accounting = {name: 0 for name in bucket_names}
    inspected = 0

    with pysam.AlignmentFile(path, "rb") as bam:
        verification = _verify_bam_reference(bam, reference)
        for read in bam.fetch(until_eof=True):
            inspected += 1
            if read.is_unmapped:
                accounting["excluded_unmapped"] += 1
                continue
            if read.is_secondary:
                accounting["excluded_secondary"] += 1
                continue
            if read.is_supplementary:
                accounting["excluded_supplementary"] += 1
                continue
            if read.is_qcfail:
                accounting["excluded_qc_failure"] += 1
                continue
            if read.is_duplicate:
                accounting["excluded_duplicate"] += 1
                continue
            if read.mapping_quality < minimum_mapping_quality:
                accounting["excluded_below_mapq"] += 1
                continue
            chromosome = bam.get_reference_name(read.reference_id)
            if chromosome not in AUTOSOMES:
                accounting["excluded_non_autosomal"] += 1
                continue
            target = bin_index.get((chromosome, read.reference_start // window_size_bp))
            if target is None:
                accounting["excluded_outside_analyzed_bins"] += 1
                continue
            definition = bins[target]
            if not definition.start <= read.reference_start < definition.end:
                accounting["excluded_outside_analyzed_bins"] += 1
                continue
            counts[target] += 1
            accounting["accepted_autosomal_read_count"] += 1

    emitted = tuple(
        BinCount(
            **definition.model_dump(mode="python"),
            accepted_read_start_count=count,
            included_in_screen=not definition.is_terminal_partial_bin,
            exclusion_reason=(
                "terminal_partial_bin" if definition.is_terminal_partial_bin else None
            ),
        )
        for definition, count in zip(bins, counts, strict=True)
    )
    return BamDosageQcScan(
        bins=emitted,
        accounting=ReadAccounting(
            inspected_alignment_count=inspected,
            **accounting,
        ),
        reference_verification=verification,
    )


def compute_dosage_qc(
    scan: BamDosageQcScan,
    *,
    input_artifact_sha256: str,
    reference: ReferenceBinding,
    window_size_bp: int = DEFAULT_WINDOW_SIZE_BP,
    minimum_mapping_quality: int = DEFAULT_MIN_MAPQ,
) -> DosageQcResultBundle:
    """Apply the legacy chromosome-median calculation to accountable full bins."""

    if scan.reference_verification.reference_id != reference.reference_id:
        raise ValueError("scan/reference identity mismatch")
    definitions = tuple(
        BinDefinition(
            contig=row.contig,
            start=row.start,
            end=row.end,
            is_terminal_partial_bin=row.is_terminal_partial_bin,
        )
        for row in scan.bins
    )
    validate_bins(reference, definitions, window_size_bp=window_size_bp)

    retained_by_chromosome: dict[str, np.ndarray] = {}
    all_retained: list[float] = []
    for chromosome in AUTOSOMES:
        values = np.asarray(
            [
                row.accepted_read_start_count
                for row in scan.bins
                if row.contig == chromosome and row.included_in_screen
            ],
            dtype=float,
        )
        if values.size == 0:
            raise ValueError(f"{chromosome} requires at least one complete bin")
        chromosome_median = float(np.median(values))
        retained = values[values >= chromosome_median * DEFAULT_LOW_BIN_FRACTION]
        if retained.size == 0:
            raise ValueError(f"{chromosome} has no retained complete bins")
        retained_by_chromosome[chromosome] = retained
        all_retained.extend(float(value) for value in retained)

    genome_median = float(np.median(all_retained))
    if genome_median <= 0:
        raise ValueError("genome-wide retained-bin median must be positive")
    log2_values = np.log2(np.asarray(all_retained) / genome_median)
    log2_mad = float(np.median(np.abs(log2_values - np.median(log2_values))))

    rows: list[ChromosomeDosageQc] = []
    for ordinal, chromosome in enumerate(AUTOSOMES, start=1):
        chromosome_bins = [row for row in scan.bins if row.contig == chromosome]
        complete_bins = [row for row in chromosome_bins if row.included_in_screen]
        retained = retained_by_chromosome[chromosome]
        chromosome_median = float(np.median(retained))
        log2_ratio = float(math.log2(chromosome_median / genome_median))
        if log2_ratio >= DEFAULT_VISUALIZATION_BOUNDARY_LOG2:
            direction: DosageDirection = "higher_relative_dosage"
        elif log2_ratio <= -DEFAULT_VISUALIZATION_BOUNDARY_LOG2:
            direction = "lower_relative_dosage"
        else:
            direction = "within_visualization_boundary"
        rows.append(
            ChromosomeDosageQc(
                chromosome=chromosome,
                ordinal=ordinal,
                accepted_read_count=sum(
                    row.accepted_read_start_count for row in chromosome_bins
                ),
                complete_bin_count=len(complete_bins),
                retained_complete_bin_count=len(retained),
                median_retained_bin_count=chromosome_median,
                log2_ratio=log2_ratio,
                relative_diploid_dosage=2.0 * (2.0**log2_ratio),
                dosage_direction=direction,
            )
        )

    return DosageQcResultBundle(
        bins=scan.bins,
        chromosomes=tuple(rows),
        genome_median_bin_count=genome_median,
        genome_log2_mad=log2_mad,
        outside_visualization_boundary_count=sum(
            row.dosage_direction != "within_visualization_boundary" for row in rows
        ),
        provenance=DosageQcProvenance(
            input_artifact_sha256=input_artifact_sha256,
            reference=reference,
            reference_verification=scan.reference_verification,
            read_accounting=scan.accounting,
            window_size_bp=window_size_bp,
            minimum_mapping_quality=minimum_mapping_quality,
            low_bin_retention_fraction=DEFAULT_LOW_BIN_FRACTION,
            visualization_boundary_log2=DEFAULT_VISUALIZATION_BOUNDARY_LOG2,
            filters=(
                "autosomes chr1 through chr22",
                "primary alignments only",
                "mapped, QC-pass, non-duplicate reads",
                f"mapping quality at least {minimum_mapping_quality}",
                "read start assigned to one exact reference-bound bin",
                "terminal partial bins counted but excluded from dosage calculation",
                "complete bins below 55% of their chromosome median excluded",
            ),
        ),
        limitations=(
            "Exploratory, uncalibrated whole-chromosome dosage quality control.",
            "Not a cancer test and not a tumor-fraction estimate.",
            "No GC or mappability correction, panel of normals, or segmentation model.",
            "Descriptive dosage directions use an unvalidated visualization boundary.",
        ),
    )


__all__ = [
    "AUTOSOMES",
    "BamDosageQcScan",
    "BinCount",
    "BinDefinition",
    "DosageQcResultBundle",
    "ReadAccounting",
    "ReferenceBinding",
    "ReferenceContigBinding",
    "bin_definition_sha256",
    "compute_dosage_qc",
    "fixed_width_bins",
    "scan_bam",
    "validate_bins",
]
