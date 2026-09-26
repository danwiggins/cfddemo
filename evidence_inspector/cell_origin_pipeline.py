"""Bounded orchestration for Loyfer fragment-UXM cell-origin regeneration.

The public pipeline accepts either a validated, normalized modkit extract TSV
or an aligned modBAM that can be extracted by a verified modkit executable.
It joins the supplied Loyfer marker registry, region BED, and U atlas; performs
fragment UXM classification; fits count-weighted NNLS; and publishes an atomic,
aggregate-only JSON result bundle.

Loyfer Supplementary Table S8 provides the method-matched observed plasma
cohort used for chart context.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from evidence_inspector.cell_origin_inputs import (
    AtlasUColumns,
    CellOriginInputError,
    FractionUnit,
    MarkerBedColumns,
    ModkitExtractColumns,
    load_loyfer_atlas_u_matrix,
    load_marker_bed,
    load_modkit_extract_calls,
)
from evidence_inspector.cell_origin_models import (
    AtlasUMatrix,
    CellOriginProvenance,
    CellOriginResult,
    DigestArtifact,
    GenomicMarker,
    LOYFER_UXM_METHOD,
    SoftwareVersion,
    StrictModel,
    UxmThresholds,
    ValidationCheck,
    ValidationRecord,
    ValidationReport,
    VerificationLevel,
)
from evidence_inspector.deconvolution import bootstrap_uxm, deconvolve_uxm
from evidence_inspector.uxm import (
    UxmClassificationResult,
    UxmDiagnostics,
    UxmStopReason,
    aggregate_marker_counts,
    classify_uxm_calls,
)

PIPELINE_VERSION = "1.0.0"
SCHEMA_VERSION = "cell-origin-pipeline.v1"
DEFAULT_MAXIMUM_CALLS = 1_000_000
DEFAULT_MAXIMUM_GROUPS = 100_000
DEFAULT_MAXIMUM_CPGS_PER_GROUP = 10_000
DEFAULT_BOOTSTRAP_REPLICATES = 200
DEFAULT_RANDOM_SEED = 7
DEFAULT_TOP_COMPOSITION_ROWS = 12
PALETTE = (
    "#0F766E",
    "#2563EB",
    "#7C3AED",
    "#DB2777",
    "#EA580C",
    "#CA8A04",
    "#16A34A",
    "#0891B2",
    "#4F46E5",
    "#9333EA",
    "#E11D48",
    "#475569",
)

NORMALIZED_MODKIT_COLUMNS = ModkitExtractColumns(
    fragment_id="read_id",
    chromosome="chrom",
    position0="ref_position",
    strand="mod_strand",
    modified_primary_base="modified_primary_base",
    call_code="call_code",
    modified_probability="modified_probability",
    fail="fail",
)

MARKER_METADATA_HEADER = (
    "#chr",
    "start",
    "end",
    "startCpG",
    "endCpG",
    "target",
    "region",
    "lenCpG",
    "bp",
    "tg_mean",
    "bg_mean",
    "delta_means",
    "delta_quants",
    "delta_maxmin",
    "ttest",
    "direction",
)
ATLAS_METADATA_HEADER = (
    "chr",
    "start",
    "end",
    "startCpG",
    "endCpG",
    "target",
    "name",
    "direction",
)
NATIVE_MODKIT_REQUIRED_COLUMNS = frozenset(
    {
        "read_id",
        "ref_position",
        "chrom",
        "mod_strand",
        "modified_primary_base",
        "fail",
        "call_code",
    }
)


class CellOriginPipelineError(RuntimeError):
    """Sanitized, actionable pipeline failure."""


class CommandStep(StrictModel):
    """One directly executable argv vector; never a shell command string."""

    name: str = Field(min_length=1, max_length=64)
    argv: tuple[str, ...] = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=512)


class AlignmentCommandPlan(StrictModel):
    """Auditable minimap2 plan that carries MM/ML/MN via FASTQ comments."""

    steps: tuple[CommandStep, ...] = Field(min_length=1)
    preserved_tags: tuple[Literal["MM", "ML", "MN"], ...] = ("MM", "ML", "MN")
    postconditions: tuple[str, ...] = Field(min_length=1)


class PreflightItem(StrictModel):
    item_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    ready: bool
    blocking: bool
    version: str | None = Field(default=None, max_length=128)
    detail: str = Field(min_length=1, max_length=512)


class PreflightReport(StrictModel):
    items: tuple[PreflightItem, ...] = Field(min_length=1)

    @property
    def ready(self) -> bool:
        return not any(item.blocking and not item.ready for item in self.items)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            item.detail
            for item in self.items
            if item.blocking and not item.ready
        )


class CompositionChartRow(StrictModel):
    cell_type_id: str
    label: str
    rank: int = Field(ge=1)
    fraction: float = Field(ge=0.0, le=1.0)
    percent: float = Field(ge=0.0, le=100.0)
    lower_fraction: float = Field(ge=0.0, le=1.0)
    upper_fraction: float = Field(ge=0.0, le=1.0)
    lower_percent: float = Field(ge=0.0, le=100.0)
    upper_percent: float = Field(ge=0.0, le=100.0)
    color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    show_by_default: bool

    @model_validator(mode="after")
    def validate_interval(self) -> CompositionChartRow:
        if not self.lower_fraction <= self.fraction <= self.upper_fraction:
            raise ValueError("composition uncertainty must contain the estimate")
        return self


class HealthyRangeChartRow(StrictModel):
    """Method-matched observed plasma range from Loyfer Supplementary Table S8."""

    cell_type_id: str
    label: str
    sample_fraction: float = Field(ge=0.0, le=1.0)
    sample_percent: float = Field(ge=0.0, le=100.0)
    sample_lower_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    sample_upper_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    healthy_min_percent: float = Field(ge=0.0, le=100.0)
    healthy_q1_percent: float = Field(ge=0.0, le=100.0)
    healthy_median_percent: float = Field(ge=0.0, le=100.0)
    healthy_q3_percent: float = Field(ge=0.0, le=100.0)
    healthy_max_percent: float = Field(ge=0.0, le=100.0)
    healthy_mean_percent: float = Field(ge=0.0, le=100.0)
    comparison_valid: Literal[True] = True
    classification: Literal["below", "within", "above"]
    uncertainty_available: bool
    reference_label: Literal["Loyfer Table S8 plasma donors (n=23)"] = (
        "Loyfer Table S8 plasma donors (n=23)"
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> HealthyRangeChartRow:
        if not (
            self.healthy_min_percent
            <= self.healthy_q1_percent
            <= self.healthy_median_percent
            <= self.healthy_q3_percent
            <= self.healthy_max_percent
        ):
            raise ValueError("healthy chart statistics must be ordered")
        if self.uncertainty_available != (
            self.sample_lower_percent is not None
            and self.sample_upper_percent is not None
        ):
            raise ValueError("uncertainty availability does not match interval")
        return self


class ChartData(StrictModel):
    composition_rows: tuple[CompositionChartRow, ...] = Field(min_length=1)
    healthy_context_rows: tuple[HealthyRangeChartRow, ...] = ()
    composition_title: str
    range_title: str | None = None
    x_axis_title: Literal["Estimated contribution (%)"] = (
        "Estimated contribution (%)"
    )
    method_warning: str | None = None


class ResourceSummary(StrictModel):
    registered_markers: int = Field(ge=1)
    usable_markers: int = Field(ge=1)
    excluded_incomplete_atlas_markers: int = Field(ge=0)
    collapsed_duplicate_regions: int = Field(ge=0)
    cell_type_count: int = Field(ge=1)


class CellOriginResultBundle(StrictModel):
    schema_version: Literal["cell-origin-pipeline.v1"] = SCHEMA_VERSION
    result: CellOriginResult
    charts: ChartData
    resources: ResourceSummary
    notices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    marker_bed: Path
    marker_metadata: Path
    atlas_u_matrix: Path
    output_path: Path
    extract_tsv: Path | None = None
    aligned_modbam: Path | None = None
    healthy_table: Path | None = None
    healthy_sheet: str | None = None
    healthy_fraction_unit: FractionUnit = FractionUnit.FRACTION
    atlas_id: str = "atlas.loyfer-u250-l4.hg38"
    atlas_source_id: str = "source.loyfer-u250"
    healthy_source_id: str = "source.loyfer-table-s8"
    maximum_calls: int = DEFAULT_MAXIMUM_CALLS
    maximum_groups: int = DEFAULT_MAXIMUM_GROUPS
    maximum_cpgs_per_group: int = DEFAULT_MAXIMUM_CPGS_PER_GROUP
    bootstrap_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES
    random_seed: int = DEFAULT_RANDOM_SEED


@dataclass(frozen=True, slots=True)
class _LoyferResources:
    markers: tuple[GenomicMarker, ...]
    atlas: AtlasUMatrix
    labels: Mapping[str, str]
    registered_marker_count: int
    excluded_incomplete_count: int
    collapsed_duplicate_count: int


@dataclass(frozen=True, slots=True)
class _LoyferHealthyRow:
    cell_type_id: str
    label: str
    donor_fractions: tuple[float, ...]
    reported_mean: float


@dataclass(frozen=True, slots=True)
class _LoyferHealthyTable:
    rows: tuple[_LoyferHealthyRow, ...]
    donor_ids: tuple[str, ...]
    source_id: str


def _identifier(value: str, *, kind: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized or len(normalized) > 128:
        raise CellOriginPipelineError(f"{kind} cannot be normalized safely")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise CellOriginPipelineError("unable to checksum an input artifact") from exc
    return digest.hexdigest()


def _artifact(path: Path, artifact_id: str) -> DigestArtifact:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CellOriginPipelineError("unable to inspect an input artifact") from exc
    return DigestArtifact(
        artifact_id=artifact_id,
        sha256=_sha256(path),
        size_bytes=size,
    )


def _version_token(raw: str) -> str:
    match = re.search(r"[0-9]+(?:\.[0-9A-Za-z]+)+(?:[-+][0-9A-Za-z.-]+)?", raw)
    if match:
        return match.group(0)[:64]
    cleaned = re.sub(r"[^A-Za-z0-9_.+:-]+", "-", raw.strip()).strip("-")
    return (cleaned or "unknown")[:64]


def _tool_version(executable: str) -> tuple[bool, str | None]:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return completed.returncode == 0 and bool(output), (
        _version_token(output[0]) if output else None
    )


def preflight(
    config: PipelineConfig,
    *,
    require_healthy: bool = False,
    require_modkit: bool = True,
    executable_finder: Callable[[str], str | None] = shutil.which,
    version_probe: Callable[[str], tuple[bool, str | None]] = _tool_version,
) -> PreflightReport:
    """Check tools and required local artifacts without exposing their paths."""

    items: list[PreflightItem] = []
    for tool in ("minimap2", "samtools", "modkit"):
        executable = executable_finder(tool)
        blocking = tool != "modkit" or require_modkit
        if executable is None:
            detail = (
                "modkit is not installed; install a pinned release before "
                "extracting methylation calls"
                if tool == "modkit"
                else f"{tool} is not installed or is not on PATH"
            )
            items.append(
                PreflightItem(
                    item_id=f"software.{tool}",
                    ready=False,
                    blocking=blocking,
                    detail=detail,
                )
            )
            continue
        ready, version = version_probe(executable)
        items.append(
            PreflightItem(
                item_id=f"software.{tool}",
                ready=ready,
                blocking=blocking,
                version=version,
                detail=(
                    f"{tool} version detected"
                    if ready
                    else f"{tool} exists but its version could not be verified"
                ),
            )
        )

    artifact_checks = (
        ("reference.hg38-fasta", Path("data/local/reference/hg38.primary.fa")),
        ("reference.hg38-index", Path("data/local/reference/hg38.primary.fa.mmi")),
        ("atlas.marker-bed", config.marker_bed),
        ("atlas.marker-metadata", config.marker_metadata),
        ("atlas.u-matrix", config.atlas_u_matrix),
    )
    for item_id, path in artifact_checks:
        ready = path.is_file() and path.stat().st_size > 0
        items.append(
            PreflightItem(
                item_id=item_id,
                ready=ready,
                blocking=True,
                detail=(
                    f"{item_id} is available"
                    if ready
                    else f"{item_id} is missing or empty"
                ),
            )
        )

    input_count = sum(
        item is not None for item in (config.extract_tsv, config.aligned_modbam)
    )
    input_ready = input_count == 1 and (
        (config.extract_tsv is not None and config.extract_tsv.is_file())
        or (
            config.aligned_modbam is not None
            and config.aligned_modbam.is_file()
        )
    )
    items.append(
        PreflightItem(
            item_id="input.methylation-calls",
            ready=input_ready,
            blocking=True,
            detail=(
                "exactly one aligned modBAM or validated modkit extract is available"
                if input_ready
                else "provide exactly one existing aligned modBAM or validated modkit extract TSV"
            ),
        )
    )

    healthy_ready = (
        config.healthy_table is not None and config.healthy_table.is_file()
    )
    items.append(
        PreflightItem(
            item_id="reference.healthy-table-s8",
            ready=healthy_ready,
            blocking=require_healthy,
            detail=(
                "healthy Table S8 is available as contextual reference data"
                if healthy_ready
                else "provide a normalized healthy Table S8 file for the range chart"
            ),
        )
    )
    return PreflightReport(items=tuple(items))


def build_alignment_command_plan(
    source_modbam: Path,
    reference_index: Path,
    work_directory: Path,
    *,
    threads: int = 4,
) -> AlignmentCommandPlan:
    """Return, but never execute, a minimap2 alignment plan.

    ``samtools fastq -T`` serializes MM/ML/MN as FASTQ comments and minimap2
    ``-y`` copies those comments back into SAM auxiliary fields. The plan still
    requires post-run tag and sequence-orientation validation before modkit use.
    """

    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise CellOriginPipelineError("threads must be a positive integer")
    fastq = work_directory / "reads.with-mod-tags.fastq"
    sam = work_directory / "aligned.with-mod-tags.sam"
    unsorted_bam = work_directory / "aligned.unsorted.bam"
    aligned_bam = work_directory / "aligned.sorted.bam"
    return AlignmentCommandPlan(
        steps=(
            CommandStep(
                name="export-fastq-with-tags",
                argv=(
                    "samtools",
                    "fastq",
                    "-T",
                    "MM,ML,MN",
                    "-n",
                    "-0",
                    str(fastq),
                    "-1",
                    os.devnull,
                    "-2",
                    os.devnull,
                    "-s",
                    os.devnull,
                    str(source_modbam),
                ),
                purpose="Copy primary Nanopore reads and modification tags to FASTQ comments.",
            ),
            CommandStep(
                name="align-hg38",
                argv=(
                    "minimap2",
                    "-a",
                    "-x",
                    "map-ont",
                    "-y",
                    "-t",
                    str(threads),
                    "-o",
                    str(sam),
                    str(reference_index),
                    str(fastq),
                ),
                purpose="Align to hg38 while restoring FASTQ comment tags to SAM.",
            ),
            CommandStep(
                name="sam-to-bam",
                argv=(
                    "samtools",
                    "view",
                    "-b",
                    "-o",
                    str(unsorted_bam),
                    str(sam),
                ),
                purpose="Convert SAM to BAM without a shell pipeline.",
            ),
            CommandStep(
                name="coordinate-sort",
                argv=(
                    "samtools",
                    "sort",
                    "-@",
                    str(threads),
                    "-o",
                    str(aligned_bam),
                    str(unsorted_bam),
                ),
                purpose="Coordinate-sort the aligned modBAM.",
            ),
            CommandStep(
                name="index",
                argv=("samtools", "index", str(aligned_bam)),
                purpose="Index the coordinate-sorted aligned modBAM.",
            ),
        ),
        postconditions=(
            "Compare input and output primary-read counts.",
            "Sample both orientations and verify MM, ML, and MN remain valid.",
            "Run modkit validate on the aligned modBAM before extraction.",
            "Do not delete the source modBAM or intermediates until checks pass.",
        ),
    )


def _strict_dict_reader(
    path: Path,
    *,
    expected_header: Sequence[str],
    max_rows: int,
) -> tuple[dict[str, str], ...]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if tuple(reader.fieldnames or ()) != tuple(expected_header):
                raise CellOriginPipelineError(
                    "a Loyfer resource header does not match its registered schema"
                )
            rows: list[dict[str, str]] = []
            for row_number, row in enumerate(reader, start=1):
                if row_number > max_rows:
                    raise CellOriginPipelineError(
                        f"a Loyfer resource exceeds the row cap of {max_rows}"
                    )
                if None in row or any(value is None for value in row.values()):
                    raise CellOriginPipelineError(
                        "a Loyfer resource contains a malformed row"
                    )
                rows.append(dict(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CellOriginPipelineError("unable to parse a Loyfer resource") from exc
    if not rows:
        raise CellOriginPipelineError("a Loyfer resource contains no records")
    return tuple(rows)


def _read_regions(path: Path, *, max_rows: int) -> tuple[str, ...]:
    regions: list[str] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            for row_number, row in enumerate(reader, start=1):
                if row_number > max_rows:
                    raise CellOriginPipelineError(
                        f"marker BED exceeds the row cap of {max_rows}"
                    )
                if len(row) != 3:
                    raise CellOriginPipelineError(
                        "marker BED must contain exactly three columns"
                    )
                chromosome, start, end = row
                try:
                    start0, end0 = int(start), int(end)
                except ValueError as exc:
                    raise CellOriginPipelineError(
                        "marker BED contains a non-integer coordinate"
                    ) from exc
                if start != str(start0) or end != str(end0) or end0 <= start0:
                    raise CellOriginPipelineError(
                        "marker BED contains an invalid half-open interval"
                    )
                regions.append(f"{chromosome}:{start0}-{end0}")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CellOriginPipelineError("unable to parse marker BED") from exc
    if len(regions) != len(set(regions)):
        raise CellOriginPipelineError("marker BED contains duplicate regions")
    return tuple(regions)


def _load_loyfer_resources(config: PipelineConfig) -> _LoyferResources:
    metadata_rows = _strict_dict_reader(
        config.marker_metadata,
        expected_header=MARKER_METADATA_HEADER,
        max_rows=20_000,
    )
    metadata_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in metadata_rows:
        metadata_groups[row["region"]].append(row)
    for rows in metadata_groups.values():
        first = rows[0]
        if any(
            (
                row["#chr"],
                row["start"],
                row["end"],
                row["direction"],
            )
            != (
                first["#chr"],
                first["start"],
                first["end"],
                first["direction"],
            )
            for row in rows[1:]
        ):
            raise CellOriginPipelineError(
                "duplicate Loyfer region IDs disagree on coordinates or direction"
            )
    metadata_by_region = {
        region: rows[0] for region, rows in metadata_groups.items()
    }
    collapsed_duplicates = len(metadata_rows) - len(metadata_by_region)
    regions = _read_regions(config.marker_bed, max_rows=20_000)
    if set(regions) != set(metadata_by_region):
        raise CellOriginPipelineError(
            "marker BED and Markers.U250 region IDs do not match exactly"
        )

    try:
        with config.atlas_u_matrix.open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            header = tuple(reader.fieldnames or ())
            if header[:8] != ATLAS_METADATA_HEADER or len(header) <= 8:
                raise CellOriginPipelineError(
                    "Atlas.U250 header does not match the registered schema"
                )
            raw_cell_labels = header[8:]
            cell_ids = tuple(
                _identifier(value, kind="cell type") for value in raw_cell_labels
            )
            if len(cell_ids) != len(set(cell_ids)):
                raise CellOriginPipelineError(
                    "cell type labels collide after identifier normalization"
                )
            labels = dict(zip(cell_ids, raw_cell_labels, strict=True))
            atlas_by_region: dict[str, dict[str, str]] = {}
            atlas_order: list[str] = []
            incomplete = 0
            for row_number, row in enumerate(reader, start=1):
                if row_number > 20_000:
                    raise CellOriginPipelineError(
                        "Atlas.U250 exceeds the row cap of 20000"
                    )
                if None in row or any(value is None for value in row.values()):
                    raise CellOriginPipelineError(
                        "Atlas.U250 contains a malformed row"
                    )
                region = row["name"]
                metadata_rows_for_region = metadata_groups.get(region)
                if metadata_rows_for_region is None:
                    raise CellOriginPipelineError(
                        "Atlas.U250 contains an unregistered marker"
                    )
                metadata = metadata_rows_for_region[0]
                registered_targets = {
                    item["target"] for item in metadata_rows_for_region
                }
                if (
                    row["chr"] != metadata["#chr"]
                    or row["start"] != metadata["start"]
                    or row["end"] != metadata["end"]
                    or row["target"] not in registered_targets
                    or row["direction"] != metadata["direction"]
                    or row["direction"] != "U"
                ):
                    raise CellOriginPipelineError(
                        "Loyfer marker resources disagree on coordinates, target, or direction"
                    )
                previous = atlas_by_region.get(region)
                if previous is not None:
                    comparable_fields = (
                        "chr",
                        "start",
                        "end",
                        "startCpG",
                        "endCpG",
                        "name",
                        "direction",
                        *raw_cell_labels,
                    )
                    if any(previous[key] != row[key] for key in comparable_fields):
                        raise CellOriginPipelineError(
                            "duplicate Atlas.U250 regions disagree on U values"
                        )
                    continue
                atlas_by_region[region] = dict(row)
                atlas_order.append(region)

            atlas_rows: list[dict[str, str]] = []
            for region in atlas_order:
                row = atlas_by_region[region]
                values = tuple(row[label] for label in raw_cell_labels)
                if any(value in {"", "NA"} for value in values):
                    incomplete += 1
                    continue
                try:
                    parsed = tuple(float(value) for value in values)
                except ValueError as exc:
                    raise CellOriginPipelineError(
                        "Atlas.U250 contains a nonnumeric U fraction"
                    ) from exc
                if any(
                    not np.isfinite(value) or not 0.0 <= value <= 1.0
                    for value in parsed
                ):
                    raise CellOriginPipelineError(
                        "Atlas.U250 contains an invalid U fraction"
                    )
                atlas_rows.append(
                    {
                        "marker_id": region,
                        **{
                            cell_id: value
                            for cell_id, value in zip(
                                cell_ids, values, strict=True
                            )
                        },
                    }
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CellOriginPipelineError("unable to parse Atlas.U250") from exc

    if set(atlas_by_region) != set(regions):
        raise CellOriginPipelineError(
            "Atlas.U250 and marker BED region IDs do not match exactly"
        )
    if not atlas_rows:
        raise CellOriginPipelineError(
            "Atlas.U250 has no complete marker rows across all cell types"
        )

    atlas_buffer = io.StringIO(newline="")
    atlas_writer = csv.DictWriter(
        atlas_buffer,
        fieldnames=("marker_id", *cell_ids),
        delimiter="\t",
        lineterminator="\n",
    )
    atlas_writer.writeheader()
    atlas_writer.writerows(atlas_rows)
    atlas_buffer.seek(0)

    usable_regions = tuple(row["marker_id"] for row in atlas_rows)
    marker_buffer = io.StringIO(newline="")
    marker_writer = csv.writer(
        marker_buffer, delimiter="\t", lineterminator="\n"
    )
    for region in usable_regions:
        row = metadata_by_region[region]
        targets = {
            item["target"] for item in metadata_groups[region]
        }
        marker_writer.writerow(
            (
                row["#chr"],
                row["start"],
                row["end"],
                region,
                (
                    _identifier(next(iter(targets)), kind="marker target")
                    if len(targets) == 1
                    else "multi-target"
                ),
            )
        )
    marker_buffer.seek(0)

    try:
        atlas = load_loyfer_atlas_u_matrix(
            atlas_buffer,
            columns=AtlasUColumns(
                marker_id="marker_id",
                cell_type_columns=tuple((item, item) for item in cell_ids),
            ),
            atlas_id=config.atlas_id,
            source_ids=(config.atlas_source_id,),
            expected_marker_ids=usable_regions,
            expected_cell_type_ids=cell_ids,
            max_rows=20_000,
        )
        markers = load_marker_bed(
            marker_buffer,
            columns=MarkerBedColumns(
                chromosome=0,
                start0=1,
                end0=2,
                marker_id=3,
                target_cell_type_id=4,
            ),
            atlas_id=config.atlas_id,
            source_ids=(config.atlas_source_id,),
            expected_marker_ids=usable_regions,
            expected_cell_type_ids=(*cell_ids, "multi-target"),
            max_rows=20_000,
        )
    except CellOriginInputError as exc:
        raise CellOriginPipelineError(str(exc)) from exc
    return _LoyferResources(
        markers=markers,
        atlas=atlas,
        labels=labels,
        registered_marker_count=len(metadata_rows),
        excluded_incomplete_count=incomplete,
        collapsed_duplicate_count=collapsed_duplicates,
    )


def _interval_index(
    markers: Sequence[GenomicMarker],
) -> dict[str, tuple[tuple[int, ...], tuple[int, ...], tuple[GenomicMarker, ...]]]:
    by_chromosome: dict[str, list[GenomicMarker]] = defaultdict(list)
    for marker in markers:
        by_chromosome[marker.chromosome].append(marker)
    index = {}
    for chromosome, values in by_chromosome.items():
        ordered = tuple(sorted(values, key=lambda item: (item.start0, item.end0)))
        starts = tuple(item.start0 for item in ordered)
        prefix_max: list[int] = []
        maximum = -1
        for item in ordered:
            maximum = max(maximum, item.end0)
            prefix_max.append(maximum)
        index[chromosome] = (starts, tuple(prefix_max), ordered)
    return index


def _matching_marker(
    call: Any,
    index: Mapping[
        str, tuple[tuple[int, ...], tuple[int, ...], tuple[GenomicMarker, ...]]
    ],
) -> tuple[GenomicMarker | None, bool]:
    chromosome_index = index.get(call.chromosome)
    if chromosome_index is None:
        return None, False
    starts, prefix_max, markers = chromosome_index
    cursor = bisect_right(starts, call.position0) - 1
    matches: list[GenomicMarker] = []
    while cursor >= 0 and prefix_max[cursor] > call.position0:
        marker = markers[cursor]
        if marker.start0 <= call.position0 < marker.end0:
            matches.append(marker)
            if len(matches) > 1:
                return None, True
        cursor -= 1
    return (matches[0], False) if matches else (None, False)


def _partitioned_classification(
    calls: Sequence[Any],
    markers: Sequence[GenomicMarker],
    *,
    maximum_groups: int,
    maximum_cpgs_per_group: int,
) -> UxmClassificationResult:
    """Use an interval index, then delegate exact UXM semantics marker by marker."""

    index = _interval_index(markers)
    by_marker: dict[str, list[Any]] = defaultdict(list)
    marker_by_id = {marker.marker_id: marker for marker in markers}
    outside = 0
    ambiguous = 0
    invalid_fragments: set[str] = set()
    assigned: list[tuple[str, Any]] = []
    for call in calls:
        marker, is_ambiguous = _matching_marker(call, index)
        if is_ambiguous:
            ambiguous += 1
            invalid_fragments.add(call.fragment_digest)
        elif marker is None:
            outside += 1
        else:
            assigned.append((marker.marker_id, call))
    for marker_id, call in assigned:
        if call.fragment_digest not in invalid_fragments:
            by_marker[marker_id].append(call)

    observations = []
    diagnostic_values: dict[str, int] = defaultdict(int)
    remaining_groups = maximum_groups
    stop_reason = UxmStopReason.COMPLETE_INPUT
    marker_order = {marker.marker_id: index for index, marker in enumerate(markers)}
    for marker_id in sorted(by_marker, key=marker_order.__getitem__):
        if remaining_groups < 1:
            stop_reason = UxmStopReason.GROUP_CAP
            break
        marker_calls = by_marker[marker_id]
        partial = classify_uxm_calls(
            marker_calls,
            (marker_by_id[marker_id],),
            maximum_calls=len(marker_calls) + 1,
            maximum_groups=remaining_groups,
            maximum_cpgs_per_group=maximum_cpgs_per_group,
        )
        observations.extend(partial.observations)
        values = asdict(partial.diagnostics)
        for key, value in values.items():
            if key not in {"stop_reason", "partial_input"}:
                diagnostic_values[key] += int(value)
        remaining_groups -= partial.diagnostics.fragment_marker_group_count
        if partial.diagnostics.stop_reason == UxmStopReason.GROUP_CAP:
            stop_reason = UxmStopReason.GROUP_CAP
            break

    marker_counts, marker_weights = aggregate_marker_counts(
        observations, marker_order=markers
    )
    diagnostics = UxmDiagnostics(
        inspected_calls=len(calls),
        retained_unique_cpg_calls=diagnostic_values[
            "retained_unique_cpg_calls"
        ],
        duplicate_cpg_calls=diagnostic_values["duplicate_cpg_calls"],
        excluded_failed_calls=diagnostic_values["excluded_failed_calls"],
        excluded_non_c_calls=diagnostic_values["excluded_non_c_calls"],
        excluded_unsupported_modification_calls=diagnostic_values[
            "excluded_unsupported_modification_calls"
        ],
        excluded_invalid_calls=diagnostic_values["excluded_invalid_calls"],
        excluded_outside_marker_calls=outside,
        excluded_ambiguous_marker_calls=ambiguous,
        excluded_unknown_marker_calls=diagnostic_values[
            "excluded_unknown_marker_calls"
        ],
        excluded_conflicting_duplicate_groups=diagnostic_values[
            "excluded_conflicting_duplicate_groups"
        ],
        excluded_oversized_groups=diagnostic_values[
            "excluded_oversized_groups"
        ],
        fragment_marker_group_count=diagnostic_values[
            "fragment_marker_group_count"
        ],
        classified_fragment_marker_count=len(observations),
        excluded_fewer_than_four_cpgs=diagnostic_values[
            "excluded_fewer_than_four_cpgs"
        ],
        stop_reason=stop_reason,
        partial_input=stop_reason != UxmStopReason.COMPLETE_INPUT,
    )
    return UxmClassificationResult(
        observations=tuple(observations),
        marker_counts=marker_counts,
        marker_weights=marker_weights,
        diagnostics=diagnostics,
    )


def _filtered_atlas(
    atlas: AtlasUMatrix, marker_ids: Sequence[str]
) -> AtlasUMatrix:
    selected = set(marker_ids)
    rows = tuple(row for row in atlas.rows if row.marker_id in selected)
    if len(rows) != len(selected):
        raise CellOriginPipelineError(
            "classified marker IDs do not match the registered atlas"
        )
    return AtlasUMatrix(
        atlas_id=atlas.atlas_id,
        method=atlas.method,
        cell_type_ids=atlas.cell_type_ids,
        rows=rows,
        source_ids=atlas.source_ids,
    )


def _load_healthy(
    config: PipelineConfig,
    cell_type_ids: Sequence[str],
    labels: Mapping[str, str],
) -> _LoyferHealthyTable | None:
    if config.healthy_table is None:
        return None
    raw_to_id = {label: cell_id for cell_id, label in labels.items()}
    rows: list[_LoyferHealthyRow] = []
    donor_ids: tuple[str, ...]
    suffix = config.healthy_table.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise CellOriginPipelineError(
                "openpyxl is required to read Loyfer Supplementary Table S8"
            ) from exc
        try:
            workbook = load_workbook(
                config.healthy_table, read_only=True, data_only=True
            )
            sheet_name = config.healthy_sheet or "Table S8"
            worksheet = workbook[sheet_name]
            title = worksheet.cell(1, 1).value
            if (
                not isinstance(title, str)
                or "Supplementary Table S8" not in title
                or "Fragment-level deconvolution" not in title
            ):
                raise CellOriginPipelineError(
                    "workbook sheet is not registered Loyfer fragment-level Table S8"
                )
            if (
                worksheet.cell(3, 1).value != "tissue"
                or worksheet.cell(3, 2).value != "Plasma (n=23)"
            ):
                raise CellOriginPipelineError(
                    "Loyfer Table S8 plasma header does not match the registered schema"
                )
            raw_donor_ids = tuple(
                worksheet.cell(3, column).value for column in range(3, 26)
            )
            if len(raw_donor_ids) != 23 or any(
                not isinstance(item, str) or not item
                for item in raw_donor_ids
            ):
                raise CellOriginPipelineError(
                    "Loyfer Table S8 must contain exactly 23 plasma donor columns C:Y"
                )
            donor_ids = tuple(
                _identifier(item, kind="plasma donor")
                for item in raw_donor_ids
                if isinstance(item, str)
            )
            for row_number in range(4, worksheet.max_row + 1):
                raw_label = worksheet.cell(row_number, 1).value
                if raw_label is None:
                    if rows:
                        break
                    continue
                if not isinstance(raw_label, str) or not raw_label:
                    raise CellOriginPipelineError(
                        "Loyfer Table S8 contains an invalid tissue label"
                    )
                raw_mean = worksheet.cell(row_number, 2).value
                raw_values = tuple(
                    worksheet.cell(row_number, column).value
                    for column in range(3, 26)
                )
                if (
                    isinstance(raw_mean, bool)
                    or not isinstance(raw_mean, (int, float))
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        for value in raw_values
                    )
                ):
                    raise CellOriginPipelineError(
                        "Loyfer Table S8 plasma values must be numeric"
                    )
                mean = float(raw_mean)
                values = tuple(float(value) for value in raw_values)
                if any(
                    not np.isfinite(value) or not 0.0 <= value <= 1.0
                    for value in (mean, *values)
                ):
                    raise CellOriginPipelineError(
                        "Loyfer Table S8 plasma fractions must be within [0, 1]"
                    )
                if not np.isclose(
                    mean, float(np.mean(values)), rtol=0.0, atol=5e-6
                ):
                    raise CellOriginPipelineError(
                        "Loyfer Table S8 reported mean does not match its 23 donors"
                    )
                cell_id = (
                    "Other"
                    if raw_label == "Other"
                    else raw_to_id.get(
                        raw_label, _identifier(raw_label, kind="cell type")
                    )
                )
                rows.append(
                    _LoyferHealthyRow(
                        cell_type_id=cell_id,
                        label=raw_label,
                        donor_fractions=values,
                        reported_mean=mean,
                    )
                )
            workbook.close()
        except (OSError, KeyError, ValueError) as exc:
            if isinstance(exc, CellOriginPipelineError):
                raise
            raise CellOriginPipelineError(
                "unable to read Loyfer Supplementary Table S8"
            ) from exc
    else:
        delimiter = "\t" if suffix in {".tsv", ".txt"} else ","
        try:
            with config.healthy_table.open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                header = tuple(reader.fieldnames or ())
                if (
                    len(header) < 4
                    or header[0] != "cell_type_id"
                    or header[1] != "mean"
                ):
                    raise CellOriginPipelineError(
                        "normalized Loyfer Table S8 must start with cell_type_id, mean, and donor columns"
                    )
                donor_columns = header[2:]
                donor_ids = tuple(
                    _identifier(item, kind="plasma donor")
                    for item in donor_columns
                )
                divisor = (
                    100.0
                    if config.healthy_fraction_unit == FractionUnit.PERCENT
                    else 1.0
                )
                for record in reader:
                    raw_label = record["cell_type_id"]
                    values = tuple(
                        float(record[column]) / divisor
                        for column in donor_columns
                    )
                    mean = float(record["mean"]) / divisor
                    cell_id = (
                        "Other"
                        if raw_label == "Other"
                        else raw_to_id.get(
                            raw_label, _identifier(raw_label, kind="cell type")
                        )
                    )
                    rows.append(
                        _LoyferHealthyRow(
                            cell_type_id=cell_id,
                            label=raw_label,
                            donor_fractions=values,
                            reported_mean=mean,
                        )
                    )
        except (OSError, UnicodeError, csv.Error, KeyError, ValueError) as exc:
            if isinstance(exc, CellOriginPipelineError):
                raise
            raise CellOriginPipelineError(
                "unable to read normalized Loyfer Table S8"
            ) from exc
    if not rows:
        raise CellOriginPipelineError("Loyfer Table S8 contains no plasma rows")
    row_ids = tuple(row.cell_type_id for row in rows)
    if len(row_ids) != len(set(row_ids)):
        raise CellOriginPipelineError(
            "Loyfer Table S8 contains duplicate tissue categories"
        )
    unknown = set(row_ids) - set(cell_type_ids) - {"Other"}
    if unknown:
        raise CellOriginPipelineError(
            "Loyfer Table S8 contains tissue IDs absent from the atlas"
        )
    if "Other" not in row_ids:
        raise CellOriginPipelineError(
            "Loyfer Table S8 must include its published Other category"
        )
    return _LoyferHealthyTable(
        rows=tuple(rows),
        donor_ids=donor_ids,
        source_id=config.healthy_source_id,
    )


def _healthy_chart_rows(
    healthy: _LoyferHealthyTable,
    estimates: Mapping[str, float],
    intervals: Mapping[str, tuple[float, float]],
    labels: Mapping[str, str],
) -> tuple[HealthyRangeChartRow, ...]:
    direct_ids = {
        row.cell_type_id for row in healthy.rows if row.cell_type_id != "Other"
    }
    rows = []
    for row in healthy.rows:
        values = np.asarray(row.donor_fractions, dtype=np.float64)
        q1, median, q3 = np.quantile(
            values, (0.25, 0.5, 0.75), method="linear"
        )
        if row.cell_type_id == "Other":
            estimate = sum(
                fraction
                for cell_id, fraction in estimates.items()
                if cell_id not in direct_ids
            )
            lower = upper = None
        else:
            estimate = estimates[row.cell_type_id]
            lower, upper = intervals[row.cell_type_id]
        minimum = float(np.min(values))
        maximum = float(np.max(values))
        classification: Literal["below", "within", "above"] = (
            "below"
            if estimate < minimum
            else "above"
            if estimate > maximum
            else "within"
        )
        rows.append(
            HealthyRangeChartRow(
                cell_type_id=row.cell_type_id,
                label=(
                    row.label
                    if row.cell_type_id == "Other"
                    else labels.get(row.cell_type_id, row.label)
                ),
                sample_fraction=estimate,
                sample_percent=estimate * 100.0,
                sample_lower_percent=(
                    lower * 100.0 if lower is not None else None
                ),
                sample_upper_percent=(
                    upper * 100.0 if upper is not None else None
                ),
                healthy_min_percent=minimum * 100.0,
                healthy_q1_percent=float(q1) * 100.0,
                healthy_median_percent=float(median) * 100.0,
                healthy_q3_percent=float(q3) * 100.0,
                healthy_max_percent=maximum * 100.0,
                healthy_mean_percent=row.reported_mean * 100.0,
                classification=classification,
                uncertainty_available=lower is not None,
            )
        )
    return tuple(
        sorted(rows, key=lambda item: (-item.sample_fraction, item.label))
    )


def _model_safe(result: CellOriginResult) -> bool:
    payload = result.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True)
    forbidden_keys = {"fragment_digest", "path", "read_id", "sequence"}

    def keys(value: Any) -> Iterable[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    return not forbidden_keys.intersection(keys(payload)) and ".bam" not in serialized


def _atomic_write(path: Path, bundle: CellOriginResultBundle) -> str:
    payload = bundle.model_dump_json(indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        CellOriginResultBundle.model_validate_json(temporary.read_bytes())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise CellOriginPipelineError(
            "result bundle could not be validated and atomically published"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _native_probability(raw: str, call_code: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and call_code in parsed:
        return str(parsed[call_code])
    pairs = dict(
        re.findall(
            r"([A-Za-z])\s*[:=]\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)",
            raw,
        )
    )
    if call_code in pairs:
        return pairs[call_code]
    raise CellOriginPipelineError(
        "installed modkit emitted an unsupported base_probs encoding; normalize the extract explicitly"
    )


def _normalize_native_modkit(source: Path, destination: Path) -> None:
    try:
        with source.open("r", encoding="utf-8", newline="") as input_handle:
            reader = csv.DictReader(input_handle, delimiter="\t")
            header = frozenset(reader.fieldnames or ())
            if not NATIVE_MODKIT_REQUIRED_COLUMNS.issubset(header):
                raise CellOriginPipelineError(
                    "installed modkit emitted an unsupported extract schema"
                )
            probability_column = next(
                (
                    name
                    for name in ("modified_probability", "call_prob")
                    if name in header
                ),
                None,
            )
            if probability_column is None and "base_probs" not in header:
                raise CellOriginPipelineError(
                    "modkit extract lacks call_prob, modified_probability, or base_probs"
                )
            with destination.open(
                "w", encoding="utf-8", newline=""
            ) as output_handle:
                writer = csv.DictWriter(
                    output_handle,
                    fieldnames=NORMALIZED_MODKIT_COLUMNS.declared(),
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                for row_number, row in enumerate(reader, start=1):
                    if row_number > DEFAULT_MAXIMUM_CALLS:
                        raise CellOriginPipelineError(
                            "modkit extract exceeds the configured call cap"
                        )
                    native_call_code = row["call_code"]
                    call_code = "C" if native_call_code == "-" else native_call_code
                    writer.writerow(
                        {
                            "read_id": row["read_id"],
                            "chrom": row["chrom"],
                            "ref_position": row["ref_position"],
                            "mod_strand": row["mod_strand"],
                            "modified_primary_base": row[
                                "modified_primary_base"
                            ],
                            "call_code": call_code,
                            "modified_probability": (
                                row[probability_column]
                                if probability_column is not None
                                else _native_probability(
                                    row["base_probs"], native_call_code
                                )
                            ),
                            "fail": row["fail"].lower(),
                        }
                    )
    except (OSError, UnicodeError, csv.Error, KeyError) as exc:
        raise CellOriginPipelineError(
            "unable to normalize modkit extract output"
        ) from exc


def _extract_modbam(
    config: PipelineConfig,
    *,
    executable: str,
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    if config.aligned_modbam is None:
        raise CellOriginPipelineError("aligned modBAM input is not configured")
    temporary = tempfile.TemporaryDirectory(prefix="traceback-modkit-")
    native = Path(temporary.name) / "modkit.native.tsv"
    normalized = Path(temporary.name) / "modkit.normalized.tsv"
    argv = [
        executable,
        "extract",
        "calls",
        "--reference",
        str(Path("data/local/reference/hg38.primary.fa")),
        "--cpg",
        "--include-bed",
        str(config.marker_bed),
        "--mapped-only",
        "--suppress-progress",
        "--force",
        str(config.aligned_modbam),
        str(native),
    ]
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        temporary.cleanup()
        raise CellOriginPipelineError("modkit extraction could not run") from exc
    if completed.returncode != 0:
        temporary.cleanup()
        raise CellOriginPipelineError(
            "modkit extraction failed; run modkit validate on the aligned modBAM"
        )
    _normalize_native_modkit(native, normalized)
    return normalized, temporary


def run_pipeline(
    config: PipelineConfig,
    *,
    fragment_hash_salt: bytes,
    software_versions: Mapping[str, str] | None = None,
) -> CellOriginResultBundle:
    """Execute the bounded cell-origin pipeline and atomically publish JSON."""

    if (
        sum(item is not None for item in (config.extract_tsv, config.aligned_modbam))
        != 1
    ):
        raise CellOriginPipelineError(
            "provide exactly one aligned modBAM or validated modkit extract TSV"
        )
    resources = _load_loyfer_resources(config)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    call_source = config.extract_tsv
    versions = dict(software_versions or {})
    if config.aligned_modbam is not None:
        modkit = shutil.which("modkit")
        if modkit is None:
            raise CellOriginPipelineError(
                "modkit is required to extract calls from an aligned modBAM"
            )
        ready, version = _tool_version(modkit)
        if not ready or version is None:
            raise CellOriginPipelineError("modkit version could not be verified")
        versions["modkit"] = version
        call_source, temporary = _extract_modbam(config, executable=modkit)
    assert call_source is not None

    try:
        calls = load_modkit_extract_calls(
            call_source,
            columns=NORMALIZED_MODKIT_COLUMNS,
            fragment_hash_salt=fragment_hash_salt,
            max_rows=config.maximum_calls,
        )
        if not calls:
            raise CellOriginPipelineError(
                "validated modkit extract contains no eligible cytosine calls"
            )
        classified = _partitioned_classification(
            calls,
            resources.markers,
            maximum_groups=config.maximum_groups,
            maximum_cpgs_per_group=config.maximum_cpgs_per_group,
        )
        if not classified.marker_counts:
            raise CellOriginPipelineError(
                "no fragments overlap a usable marker with at least four callable CpGs"
            )
        marker_ids = tuple(row.marker_id for row in classified.marker_counts)
        atlas = _filtered_atlas(resources.atlas, marker_ids)
        deconvolution = deconvolve_uxm(classified.marker_counts, atlas)
        bootstrap = bootstrap_uxm(
            classified.marker_counts,
            atlas,
            deconvolution,
            replicates=config.bootstrap_replicates,
            random_seed=config.random_seed,
        )
        healthy = _load_healthy(
            config, resources.atlas.cell_type_ids, resources.labels
        )

        input_path = (
            config.extract_tsv
            if config.extract_tsv is not None
            else config.aligned_modbam
        )
        assert input_path is not None
        artifacts = [
            _artifact(input_path, "artifact.methylation-input"),
            _artifact(config.marker_bed, "artifact.loyfer-region-bed"),
            _artifact(config.marker_metadata, "artifact.loyfer-marker-metadata"),
            _artifact(config.atlas_u_matrix, "artifact.loyfer-atlas-u"),
        ]
        if config.healthy_table is not None:
            artifacts.append(
                _artifact(config.healthy_table, "artifact.healthy-table-s8")
            )

        versions.setdefault("cell-origin-pipeline", PIPELINE_VERSION)
        versions.setdefault("python", platform.python_version())
        versions.setdefault("numpy", np.__version__)
        source_ids = [config.atlas_source_id]
        if healthy is not None:
            source_ids.append(config.healthy_source_id)
        partial = classified.diagnostics.partial_input
        provenance = CellOriginProvenance(
            schema_version=SCHEMA_VERSION,
            input_artifacts=tuple(artifacts),
            source_ids=tuple(source_ids),
            software_versions=tuple(
                SoftwareVersion(
                    software_id=_identifier(name, kind="software"),
                    version=_version_token(version),
                )
                for name, version in sorted(versions.items())
            ),
            method=LOYFER_UXM_METHOD,
            uxm_thresholds=UxmThresholds(),
            input_fragment_count=len(
                {call.fragment_digest for call in calls}
            ),
            marker_overlap_count=classified.diagnostics.fragment_marker_group_count,
            classified_fragment_marker_count=(
                classified.diagnostics.classified_fragment_marker_count
            ),
            excluded_fewer_than_four_cpgs=(
                classified.diagnostics.excluded_fewer_than_four_cpgs
            ),
            partial_input=partial,
            verification_level=(
                VerificationLevel.SAMPLED_RECOMPUTED
                if partial
                else VerificationLevel.RECOMPUTED
            ),
        )
        validation = ValidationReport(
            records=tuple(
                ValidationRecord(check=check, passed=True)
                for check in ValidationCheck
            )
        )
        result_digest = hashlib.sha256(
            json.dumps(
                {
                    "deconvolution": deconvolution.model_dump(mode="json"),
                    "artifacts": [
                        artifact.model_dump(mode="json") for artifact in artifacts
                    ],
                    "bootstrap": bootstrap.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result = CellOriginResult(
            result_id=f"cell-origin.{result_digest[:24]}",
            method=LOYFER_UXM_METHOD,
            marker_counts=classified.marker_counts,
            deconvolution=deconvolution,
            bootstrap=bootstrap,
            range_comparison=None,
            provenance=provenance,
            validation=validation,
        )
        if not _model_safe(result):
            raise CellOriginPipelineError(
                "aggregate result failed the model-safe publication check"
            )

        interval_by_id = {
            item.cell_type_id: (
                item.lower_fraction,
                item.upper_fraction,
            )
            for item in bootstrap.intervals
        }
        composition = []
        ordered_estimates = sorted(
            deconvolution.estimates,
            key=lambda item: (-item.fraction, item.cell_type_id),
        )
        for rank, estimate in enumerate(ordered_estimates, start=1):
            lower, upper = interval_by_id[estimate.cell_type_id]
            composition.append(
                CompositionChartRow(
                    cell_type_id=estimate.cell_type_id,
                    label=resources.labels.get(
                        estimate.cell_type_id, estimate.cell_type_id
                    ),
                    rank=rank,
                    fraction=estimate.fraction,
                    percent=estimate.fraction * 100.0,
                    lower_fraction=lower,
                    upper_fraction=upper,
                    lower_percent=lower * 100.0,
                    upper_percent=upper * 100.0,
                    color=PALETTE[(rank - 1) % len(PALETTE)],
                    show_by_default=rank <= DEFAULT_TOP_COMPOSITION_ROWS,
                )
            )
        estimates = {
            item.cell_type_id: item.fraction
            for item in deconvolution.estimates
        }
        healthy_rows = (
            _healthy_chart_rows(
                healthy,
                estimates,
                interval_by_id,
                resources.labels,
            )
            if healthy is not None
            else ()
        )
        notices = [
            (
                f"{resources.excluded_incomplete_count} atlas markers with one "
                "or more NA cell-type values were excluded; no imputation was used."
            ),
            (
                "Observed markers without at least four callable fragment CpGs "
                "do not enter the NNLS fit."
            ),
        ]
        if resources.collapsed_duplicate_count:
            notices.append(
                f"{resources.collapsed_duplicate_count} duplicate Loyfer marker "
                "row was collapsed by genomic region after identical atlas "
                "values were verified."
            )
        method_warning = None
        range_title = (
            "Regenerated sample vs Loyfer healthy plasma donors (n=23)"
            if healthy is not None
            else None
        )
        bundle = CellOriginResultBundle(
            result=result,
            charts=ChartData(
                composition_rows=tuple(composition),
                healthy_context_rows=healthy_rows,
                composition_title="Regenerated cell-origin composition",
                range_title=range_title,
                method_warning=method_warning,
            ),
            resources=ResourceSummary(
                registered_markers=resources.registered_marker_count,
                usable_markers=len(resources.markers),
                excluded_incomplete_atlas_markers=(
                    resources.excluded_incomplete_count
                ),
                collapsed_duplicate_regions=(
                    resources.collapsed_duplicate_count
                ),
                cell_type_count=len(resources.atlas.cell_type_ids),
            ),
            notices=tuple(notices),
        )
        _atomic_write(config.output_path, bundle)
        return bundle
    except (CellOriginInputError, ValueError) as exc:
        if isinstance(exc, CellOriginPipelineError):
            raise
        raise CellOriginPipelineError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.cleanup()


def _config_from_args(arguments: argparse.Namespace) -> PipelineConfig:
    unit = FractionUnit(arguments.healthy_unit)
    return PipelineConfig(
        marker_bed=arguments.marker_bed,
        marker_metadata=arguments.marker_metadata,
        atlas_u_matrix=arguments.atlas,
        output_path=arguments.output,
        extract_tsv=arguments.extract_tsv,
        aligned_modbam=arguments.aligned_modbam,
        healthy_table=arguments.healthy_table,
        healthy_sheet=arguments.healthy_sheet,
        healthy_fraction_unit=unit,
        maximum_calls=arguments.maximum_calls,
        maximum_groups=arguments.maximum_groups,
        bootstrap_replicates=arguments.bootstrap_replicates,
        random_seed=arguments.random_seed,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regenerate bounded Loyfer fragment-UXM cell-origin results."
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--extract-tsv", type=Path)
    input_group.add_argument("--aligned-modbam", type=Path)
    parser.add_argument(
        "--marker-bed",
        type=Path,
        default=Path("data/local/loyfer/Regions.U250.l4.hg38.bed"),
    )
    parser.add_argument(
        "--marker-metadata",
        type=Path,
        default=Path("data/local/loyfer/Markers.U250.hg38.tsv"),
    )
    parser.add_argument(
        "--atlas",
        type=Path,
        default=Path("data/local/loyfer/Atlas.U250.l4.hg38.full.tsv"),
    )
    parser.add_argument(
        "--healthy-table",
        type=Path,
        default=Path(
            "data/local/loyfer/loyfer-supplementary-tables.xlsx"
        ),
    )
    parser.add_argument("--healthy-sheet", default="Table S8")
    parser.add_argument(
        "--healthy-unit",
        choices=tuple(item.value for item in FractionUnit),
        default=FractionUnit.FRACTION.value,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/local/cell-origin/result.json"),
    )
    parser.add_argument("--maximum-calls", type=int, default=DEFAULT_MAXIMUM_CALLS)
    parser.add_argument(
        "--maximum-groups", type=int, default=DEFAULT_MAXIMUM_GROUPS
    )
    parser.add_argument(
        "--bootstrap-replicates",
        type=int,
        default=DEFAULT_BOOTSTRAP_REPLICATES,
    )
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument(
        "--print-alignment-plan",
        action="store_true",
        help="Print an argv-only plan for the configured modBAM and exit.",
    )
    return parser


def cli_main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    config = _config_from_args(arguments)
    if arguments.print_alignment_plan:
        if config.aligned_modbam is None:
            parser.error("--print-alignment-plan requires --aligned-modbam")
        plan = build_alignment_command_plan(
            config.aligned_modbam,
            Path("data/local/reference/hg38.primary.fa.mmi"),
            Path("data/local/alignment-work"),
        )
        print(plan.model_dump_json(indent=2))
        return 0

    report = preflight(
        config,
        require_healthy=True,
        require_modkit=config.aligned_modbam is not None,
    )
    if not report.ready:
        print("Cell-origin regeneration is blocked:", file=sys.stderr)
        for blocker in report.blockers:
            print(f"- {blocker}", file=sys.stderr)
        return 2
    salt = os.environ.get("TRACEBACK_FRAGMENT_HASH_SALT")
    if not salt:
        print(
            "Cell-origin regeneration is blocked:\n"
            "- set TRACEBACK_FRAGMENT_HASH_SALT to a private, nonempty value",
            file=sys.stderr,
        )
        return 2
    try:
        bundle = run_pipeline(
            config,
            fragment_hash_salt=salt.encode("utf-8"),
        )
    except CellOriginPipelineError as exc:
        print(f"Cell-origin regeneration failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "result_id": bundle.result.result_id,
                "output": str(config.output_path),
                "cell_types": len(bundle.charts.composition_rows),
                "classified_fragment_markers": (
                    bundle.result.provenance.classified_fragment_marker_count
                ),
            },
            indent=2,
        )
    )
    return 0


__all__ = [
    "AlignmentCommandPlan",
    "CellOriginPipelineError",
    "CellOriginResultBundle",
    "ChartData",
    "CommandStep",
    "CompositionChartRow",
    "HealthyRangeChartRow",
    "PipelineConfig",
    "PreflightItem",
    "PreflightReport",
    "ResourceSummary",
    "build_alignment_command_plan",
    "build_argument_parser",
    "cli_main",
    "preflight",
    "run_pipeline",
]
