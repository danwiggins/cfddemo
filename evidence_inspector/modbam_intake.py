"""Private, fail-closed intake contracts for ordered unaligned modBAM chunks.

This module never publishes paths, filenames, read names, sequences, or BAM
header records.  The detailed manifest is a private local execution artifact;
``ModbamIntakeSummary`` is the only model intended for public status surfaces.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Literal, Sequence

from pydantic import Field, StringConstraints, model_validator

from .models import Identifier, Sha256, StrictModel, canonical_json_bytes

INTAKE_SCHEMA_VERSION = "traceback.modbam-intake.v1"
INTAKE_METHOD_ID = "traceback.modbam-intake.full-scan.v1"
ALIGNMENT_PLAN_SCHEMA_VERSION = "traceback.modbam-alignment-plan.v1"
ALIGNMENT_METHOD_ID = "traceback.modbam-align-grch38.preserve-mm-ml-mn.v1"

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


class ModbamIntakeError(ValueError):
    """Sanitized failure safe to show outside the private execution boundary."""


class ExactToolIdentity(StrictModel):
    """Exact executable identity supplied by the local operator."""

    name: Identifier
    version: ShortText
    executable_sha256: Sha256


class BasecallerIdentity(StrictModel):
    """Exact basecaller and model identity without copied BAM header content."""

    tool: ExactToolIdentity
    model_id: Identifier
    model_version: ShortText


class ModbamIntakeBounds(StrictModel):
    """Required operational limits; no implicit expansion is permitted."""

    max_chunks: int = Field(ge=1, le=1024)
    max_chunk_bytes: int = Field(ge=1)
    max_total_bytes: int = Field(ge=1)
    max_records: int = Field(ge=1)


class SafeHeaderIdentity(StrictModel):
    """Non-sensitive header attributes used only for compatibility checks."""

    sam_format_version: ShortText
    sort_order: Literal["unknown", "unsorted"]
    has_sequence_dictionary: Literal[False] = False


class ModbamReadLedger(StrictModel):
    """Reconciled record and modification-tag accounting."""

    total_records: int = Field(ge=0)
    primary_unmapped_records: int = Field(ge=0)
    mapped_records: int = Field(ge=0)
    secondary_records: int = Field(ge=0)
    supplementary_records: int = Field(ge=0)
    complete_tag_records: int = Field(ge=0)
    incomplete_tag_records: int = Field(ge=0)
    valid_tag_records: int = Field(ge=0)
    invalid_tag_records: int = Field(ge=0)
    missing_mm_records: int = Field(ge=0)
    missing_ml_records: int = Field(ge=0)
    missing_mn_records: int = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> ModbamReadLedger:
        if self.total_records != (
            self.primary_unmapped_records
            + self.mapped_records
            + self.secondary_records
            + self.supplementary_records
        ):
            raise ValueError("record disposition ledger must reconcile")
        if self.total_records != self.complete_tag_records + self.incomplete_tag_records:
            raise ValueError("tag presence ledger must reconcile")
        if self.complete_tag_records != self.valid_tag_records + self.invalid_tag_records:
            raise ValueError("tag validity ledger must reconcile")
        return self


class PrivateModbamChunk(StrictModel):
    """Path-free identity and full-scan result for one private BAM chunk."""

    order: int = Field(ge=0)
    size_bytes: int = Field(ge=1)
    content_sha256: Sha256
    private_header_sha256: Sha256
    safe_header: SafeHeaderIdentity
    ledger: ModbamReadLedger


class PrivateModbamManifest(StrictModel):
    """Deterministic local-only manifest; never place in prompts or public output."""

    schema_version: Literal["traceback.modbam-intake.v1"] = INTAKE_SCHEMA_VERSION
    privacy_classification: Literal["private_local_only"] = "private_local_only"
    method_id: Literal["traceback.modbam-intake.full-scan.v1"] = INTAKE_METHOD_ID
    scanner: ExactToolIdentity
    basecaller: BasecallerIdentity
    bounds: ModbamIntakeBounds
    ordered_chunks: tuple[PrivateModbamChunk, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> PrivateModbamManifest:
        orders = [chunk.order for chunk in self.ordered_chunks]
        if orders != list(range(len(orders))):
            raise ValueError("chunk order must be contiguous")
        if len(self.ordered_chunks) > self.bounds.max_chunks:
            raise ValueError("chunk count exceeds declared bound")
        if len({chunk.content_sha256 for chunk in self.ordered_chunks}) != len(
            self.ordered_chunks
        ):
            raise ValueError("chunk content identities must be unique")
        headers = {chunk.safe_header for chunk in self.ordered_chunks}
        if len(headers) != 1:
            raise ValueError("safe BAM header attributes are incompatible")
        private_header_hashes = {
            chunk.private_header_sha256 for chunk in self.ordered_chunks
        }
        if len(private_header_hashes) != 1:
            raise ValueError("BAM headers are not byte-equivalent after parsing")
        if sum(chunk.size_bytes for chunk in self.ordered_chunks) > self.bounds.max_total_bytes:
            raise ValueError("aggregate input exceeds declared bound")
        record_count = sum(
            chunk.ledger.total_records for chunk in self.ordered_chunks
        )
        if record_count > self.bounds.max_records:
            raise ValueError("record count exceeds declared bound")
        return self

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


class ModbamIntakeSummary(StrictModel):
    """Aggregate-only status suitable for public logs and user interfaces."""

    schema_version: Literal["traceback.modbam-intake-summary.v1"] = (
        "traceback.modbam-intake-summary.v1"
    )
    status: Literal["ready_for_alignment"] = "ready_for_alignment"
    chunk_count: int = Field(ge=1)
    total_bytes: int = Field(ge=1)
    total_records: int = Field(ge=1)
    valid_tag_records: int = Field(ge=1)
    all_records_primary_unmapped: Literal[True] = True
    all_records_have_valid_mm_ml_mn: Literal[True] = True


class IntakeResult(StrictModel):
    """Return envelope that keeps private and public contracts visibly distinct."""

    private_manifest: PrivateModbamManifest
    public_summary: ModbamIntakeSummary


class RegisteredGrch38Asset(StrictModel):
    """Exact registered GRCh38 reference assets required by the plan."""

    asset_id: Identifier
    assembly: Literal["GRCh38"] = "GRCh38"
    fasta_sha256: Sha256
    fai_sha256: Sha256
    minimap2_index_sha256: Sha256
    sequence_dictionary_sha256: Sha256


class AlignmentDiskBudget(StrictModel):
    """Explicit byte ceilings checked before execution begins."""

    combined_bam_bytes: int = Field(ge=1)
    sort_temporary_bytes: int = Field(ge=1)
    aligned_bam_bytes: int = Field(ge=1)
    index_bytes: int = Field(ge=1)
    available_workspace_bytes: int = Field(ge=1)
    available_output_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_capacity(self) -> AlignmentDiskBudget:
        workspace_required = self.combined_bam_bytes + self.sort_temporary_bytes
        output_required = self.aligned_bam_bytes + self.index_bytes
        if workspace_required > self.available_workspace_bytes:
            raise ValueError("workspace disk budget is insufficient")
        if output_required > self.available_output_bytes:
            raise ValueError("output disk budget is insufficient")
        return self


class ExecutionStage(StrictModel):
    id: Identifier
    tool: ExactToolIdentity
    argv: tuple[str, ...] = Field(min_length=2)
    stdin_from_stage: Identifier | None = None


class PostRunCheck(StrictModel):
    id: Identifier
    requirement: ShortText


class ModbamAlignmentPlan(StrictModel):
    """Auditable, non-executing plan using only private mount aliases."""

    schema_version: Literal["traceback.modbam-alignment-plan.v1"] = (
        ALIGNMENT_PLAN_SCHEMA_VERSION
    )
    privacy_classification: Literal["private_local_only"] = "private_local_only"
    method_id: Literal[
        "traceback.modbam-align-grch38.preserve-mm-ml-mn.v1"
    ] = ALIGNMENT_METHOD_ID
    input_manifest_sha256: Sha256
    expected_chunk_count: int = Field(ge=1)
    expected_record_count: int = Field(ge=1)
    reference: RegisteredGrch38Asset
    disk_budget: AlignmentDiskBudget
    estimated_peak_workspace_bytes: int = Field(ge=1)
    estimated_output_bytes: int = Field(ge=1)
    pre_run_checks: tuple[PostRunCheck, ...] = Field(min_length=4)
    stages: tuple[ExecutionStage, ...] = Field(min_length=4)
    post_run_checks: tuple[PostRunCheck, ...] = Field(min_length=6)

    @model_validator(mode="after")
    def reconcile_disk_estimates(self) -> ModbamAlignmentPlan:
        if self.estimated_peak_workspace_bytes != (
            self.disk_budget.combined_bam_bytes
            + self.disk_budget.sort_temporary_bytes
        ):
            raise ValueError("workspace estimate must reconcile with byte ceilings")
        if self.estimated_output_bytes != (
            self.disk_budget.aligned_bam_bytes + self.disk_budget.index_bytes
        ):
            raise ValueError("output estimate must reconcile with byte ceilings")
        return self


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_MM_PREFIX = re.compile(
    r"^(?P<base>[ACGTUN])(?P<strand>[+-])(?P<codes>[A-Za-z]+|[0-9]+)(?P<mode>[.?])?$"
)


def _mm_event_count(mm: object, sequence: object) -> int:
    if not isinstance(mm, str) or not mm or not mm.endswith(";"):
        raise ValueError
    if not isinstance(sequence, str) or not sequence:
        raise ValueError
    event_count = 0
    for raw_group in mm[:-1].split(";"):
        fields = raw_group.split(",")
        match = _MM_PREFIX.fullmatch(fields[0])
        if match is None:
            raise ValueError
        deltas = fields[1:]
        if any(not item.isdecimal() for item in deltas):
            raise ValueError
        base = match.group("base")
        canonical_count = (
            len(sequence) if base == "N" else sequence.upper().count(base)
        )
        consumed = -1
        for delta in deltas:
            consumed += int(delta) + 1
            if consumed >= canonical_count:
                raise ValueError
        codes = match.group("codes")
        code_count = len(codes) if codes.isalpha() else 1
        event_count += len(deltas) * code_count
    return event_count


def _tags_valid(record: object) -> tuple[bool, tuple[bool, bool, bool]]:
    has_tag = getattr(record, "has_tag")
    get_tag = getattr(record, "get_tag")
    present = tuple(bool(has_tag(tag)) for tag in ("MM", "ML", "MN"))
    if not all(present):
        return False, present  # type: ignore[return-value]
    try:
        mm, mm_type = get_tag("MM", with_value_type=True)
        ml, ml_type = get_tag("ML", with_value_type=True)
        mn, mn_type = get_tag("MN", with_value_type=True)
        sequence = getattr(record, "query_sequence", None)
        if mm_type != "Z" or ml_type != "BC" or mn_type not in "cCsSiI":
            raise ValueError
        if getattr(ml, "typecode", None) != "B":
            raise ValueError
        if isinstance(mn, bool) or not isinstance(mn, int):
            raise ValueError
        if not isinstance(sequence, str) or mn != len(sequence):
            raise ValueError
        if isinstance(ml, (str, bytes)):
            raise ValueError
        probabilities = tuple(ml)
        if any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= 255
            for item in probabilities
        ):
            raise ValueError
        if _mm_event_count(mm, sequence) != len(probabilities):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        return False, present  # type: ignore[return-value]
    return True, present  # type: ignore[return-value]


def _header_identities(alignment: object) -> tuple[SafeHeaderIdentity, str]:
    header = getattr(alignment, "header").to_dict()
    hd = header.get("HD", {})
    version = hd.get("VN")
    sort_order = hd.get("SO", "unknown")
    references = tuple(getattr(alignment, "references"))
    if not isinstance(version, str) or sort_order not in {"unknown", "unsorted"}:
        raise ValueError
    if references or header.get("SQ"):
        raise ValueError
    safe = SafeHeaderIdentity(sam_format_version=version, sort_order=sort_order)
    private_digest = hashlib.sha256(canonical_json_bytes(header)).hexdigest()
    return safe, private_digest


def inspect_modbam_chunks(
    input_paths: Sequence[str | Path],
    *,
    bounds: ModbamIntakeBounds,
    scanner: ExactToolIdentity,
    basecaller: BasecallerIdentity,
) -> IntakeResult:
    """Fully scan an explicitly ordered set of unaligned modBAM chunks."""

    if not input_paths:
        raise ModbamIntakeError("intake rejected: no chunks supplied")
    if len(input_paths) > bounds.max_chunks:
        raise ModbamIntakeError("intake rejected: chunk count exceeds declared bound")

    paths = tuple(Path(item) for item in input_paths)
    file_ids: set[tuple[int, int]] = set()
    initial_stats: list[os.stat_result] = []
    for path in paths:
        try:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise OSError
            if not os.access(path, os.R_OK):
                raise OSError
            if details.st_size <= 0 or details.st_size > bounds.max_chunk_bytes:
                raise ModbamIntakeError("intake rejected: a chunk violates its byte bound")
            identity = (details.st_dev, details.st_ino)
            if identity in file_ids:
                raise ModbamIntakeError("intake rejected: duplicate file identity")
            file_ids.add(identity)
            initial_stats.append(details)
        except ModbamIntakeError:
            raise
        except OSError:
            raise ModbamIntakeError(
                "intake rejected: a chunk is not a readable regular file"
            ) from None

    if sum(details.st_size for details in initial_stats) > bounds.max_total_bytes:
        raise ModbamIntakeError("intake rejected: aggregate bytes exceed declared bound")

    chunks: list[PrivateModbamChunk] = []
    content_ids: set[str] = set()
    scanned_records = 0
    failure_counts: dict[str, int] = {}
    try:
        import pysam

        for order, path in enumerate(paths):
            content_sha256 = _sha256_path(path)
            if content_sha256 in content_ids:
                raise ModbamIntakeError("intake rejected: duplicate content identity")
            content_ids.add(content_sha256)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened_stat = os.fstat(descriptor)
            expected_stat = initial_stats[order]
            if (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
            ) != (
                expected_stat.st_dev,
                expected_stat.st_ino,
                expected_stat.st_size,
                expected_stat.st_mtime_ns,
            ):
                os.close(descriptor)
                raise ModbamIntakeError("intake rejected: a chunk changed during validation")
            with os.fdopen(descriptor, "rb") as raw_handle, pysam.AlignmentFile(
                raw_handle, "rb", check_sq=False
            ) as alignment:
                try:
                    safe_header, private_header_sha256 = _header_identities(alignment)
                except (AttributeError, TypeError, ValueError):
                    raise ModbamIntakeError(
                        "intake rejected: BAMs are not uniformly unaligned "
                        "with compatible safe headers"
                    ) from None
                counts = {
                    "total_records": 0,
                    "primary_unmapped_records": 0,
                    "mapped_records": 0,
                    "secondary_records": 0,
                    "supplementary_records": 0,
                    "complete_tag_records": 0,
                    "incomplete_tag_records": 0,
                    "valid_tag_records": 0,
                    "invalid_tag_records": 0,
                    "missing_mm_records": 0,
                    "missing_ml_records": 0,
                    "missing_mn_records": 0,
                }
                for record in alignment.fetch(until_eof=True):
                    counts["total_records"] += 1
                    scanned_records += 1
                    if scanned_records > bounds.max_records:
                        raise ModbamIntakeError(
                            "intake rejected: record count exceeds declared bound"
                        )
                    if bool(record.is_secondary):
                        counts["secondary_records"] += 1
                    elif bool(record.is_supplementary):
                        counts["supplementary_records"] += 1
                    elif not bool(record.is_unmapped) or record.reference_id != -1:
                        counts["mapped_records"] += 1
                    else:
                        counts["primary_unmapped_records"] += 1
                    valid, present = _tags_valid(record)
                    if all(present):
                        counts["complete_tag_records"] += 1
                        counts["valid_tag_records" if valid else "invalid_tag_records"] += 1
                    else:
                        counts["incomplete_tag_records"] += 1
                    for tag, is_present in zip(("mm", "ml", "mn"), present, strict=True):
                        if not is_present:
                            counts[f"missing_{tag}_records"] += 1
                final_stat = os.fstat(raw_handle.fileno())
                if (
                    final_stat.st_dev,
                    final_stat.st_ino,
                    final_stat.st_size,
                    final_stat.st_mtime_ns,
                ) != (
                    opened_stat.st_dev,
                    opened_stat.st_ino,
                    opened_stat.st_size,
                    opened_stat.st_mtime_ns,
                ):
                    raise ModbamIntakeError(
                        "intake rejected: a chunk changed during validation"
                    )
                ledger = ModbamReadLedger(**counts)
                if ledger.total_records == 0:
                    failure_counts["empty_chunks"] = failure_counts.get("empty_chunks", 0) + 1
                if ledger.primary_unmapped_records != ledger.total_records:
                    failure_counts["non_primary_unmapped_records"] = (
                        failure_counts.get("non_primary_unmapped_records", 0)
                        + ledger.total_records
                        - ledger.primary_unmapped_records
                    )
                if ledger.valid_tag_records != ledger.total_records:
                    failure_counts["invalid_or_missing_tag_records"] = (
                        failure_counts.get("invalid_or_missing_tag_records", 0)
                        + ledger.total_records
                        - ledger.valid_tag_records
                    )
                chunks.append(
                    PrivateModbamChunk(
                        order=order,
                        size_bytes=opened_stat.st_size,
                        content_sha256=content_sha256,
                        private_header_sha256=private_header_sha256,
                        safe_header=safe_header,
                        ledger=ledger,
                    )
                )
    except ModbamIntakeError:
        raise
    except Exception as exc:
        raise ModbamIntakeError(
            f"intake rejected: BAM decoding failed ({type(exc).__name__})"
        ) from None

    if (
        len({chunk.safe_header for chunk in chunks}) != 1
        or len({chunk.private_header_sha256 for chunk in chunks}) != 1
    ):
        failure_counts["incompatible_safe_headers"] = len(chunks)
    if failure_counts:
        categories = ",".join(
            f"{key}={failure_counts[key]}" for key in sorted(failure_counts)
        )
        raise ModbamIntakeError(
            f"intake rejected after aggregate validation: chunks={len(chunks)}; "
            f"records={scanned_records}; {categories}"
        )

    manifest = PrivateModbamManifest(
        scanner=scanner,
        basecaller=basecaller,
        bounds=bounds,
        ordered_chunks=tuple(chunks),
    )
    total_bytes = sum(chunk.size_bytes for chunk in chunks)
    return IntakeResult(
        private_manifest=manifest,
        public_summary=ModbamIntakeSummary(
            chunk_count=len(chunks),
            total_bytes=total_bytes,
            total_records=scanned_records,
            valid_tag_records=scanned_records,
        ),
    )


def build_grch38_alignment_plan(
    manifest: PrivateModbamManifest,
    *,
    reference: RegisteredGrch38Asset,
    samtools: ExactToolIdentity,
    minimap2: ExactToolIdentity,
    disk_budget: AlignmentDiskBudget,
) -> ModbamAlignmentPlan:
    """Build, but do not execute, the exact tag-preserving alignment plan."""

    total_bytes = sum(chunk.size_bytes for chunk in manifest.ordered_chunks)
    if disk_budget.combined_bam_bytes < total_bytes:
        raise ModbamIntakeError(
            "alignment plan rejected: combined BAM byte ceiling is below input bytes"
        )
    chunk_args = tuple(
        f"/private/input/chunk-{chunk.order:04d}.bam"
        for chunk in manifest.ordered_chunks
    )
    stages = (
        ExecutionStage(
            id="combine_unaligned_chunks",
            tool=samtools,
            argv=(
                "samtools",
                "cat",
                "-o",
                "/private/work/combined.unaligned.bam",
                *chunk_args,
            ),
        ),
        ExecutionStage(
            id="emit_tagged_fastq",
            tool=samtools,
            argv=(
                "samtools",
                "fastq",
                "-T",
                "MM,ML,MN",
                "/private/work/combined.unaligned.bam",
            ),
        ),
        ExecutionStage(
            id="align_grch38",
            tool=minimap2,
            stdin_from_stage="emit_tagged_fastq",
            argv=(
                "minimap2",
                "-a",
                "-x",
                "map-ont",
                "-y",
                "/private/reference/grch38.mmi",
                "-",
            ),
        ),
        ExecutionStage(
            id="coordinate_sort",
            tool=samtools,
            stdin_from_stage="align_grch38",
            argv=(
                "samtools",
                "sort",
                "-T",
                "/private/work/sort",
                "-o",
                "/private/output/aligned.sorted.bam",
                "-",
            ),
        ),
        ExecutionStage(
            id="build_index",
            tool=samtools,
            argv=(
                "samtools",
                "index",
                "-b",
                "/private/output/aligned.sorted.bam",
                "/private/output/aligned.sorted.bam.bai",
            ),
        ),
    )
    pre_run_checks = (
        PostRunCheck(
            id="input_identity",
            requirement="Every private mount digest equals the ordered intake manifest.",
        ),
        PostRunCheck(
            id="tool_identity",
            requirement=(
                "Samtools and minimap2 versions and executable digests equal this plan."
            ),
        ),
        PostRunCheck(
            id="reference_assets",
            requirement=(
                "FASTA, FAI, minimap2 index, and sequence-dictionary digests "
                "equal the registered GRCh38 asset."
            ),
        ),
        PostRunCheck(
            id="disk_capacity",
            requirement=(
                "Measured free bytes meet both declared disk ceilings before execution."
            ),
        ),
    )
    checks = (
        PostRunCheck(
            id="input_digest_recheck",
            requirement="Every chunk digest equals the private intake manifest.",
        ),
        PostRunCheck(
            id="record_count",
            requirement="Output primary-record count equals the manifest total.",
        ),
        PostRunCheck(
            id="tag_count",
            requirement="Every output primary record retains valid MM, ML, and MN tags.",
        ),
        PostRunCheck(
            id="reference_identity",
            requirement=(
                "Output SQ dictionary digest equals the registered GRCh38 dictionary."
            ),
        ),
        PostRunCheck(
            id="sort_order",
            requirement=(
                "Output header declares coordinate sort and an independent order scan passes."
            ),
        ),
        PostRunCheck(
            id="index_integrity",
            requirement="BAI opens and samtools quickcheck succeeds for the output BAM.",
        ),
        PostRunCheck(
            id="record_partition",
            requirement="Mapped plus unmapped output records equals the manifest total.",
        ),
    )
    return ModbamAlignmentPlan(
        input_manifest_sha256=manifest.manifest_sha256,
        expected_chunk_count=len(manifest.ordered_chunks),
        expected_record_count=sum(
            chunk.ledger.total_records for chunk in manifest.ordered_chunks
        ),
        reference=reference,
        disk_budget=disk_budget,
        estimated_peak_workspace_bytes=(
            disk_budget.combined_bam_bytes + disk_budget.sort_temporary_bytes
        ),
        estimated_output_bytes=(
            disk_budget.aligned_bam_bytes + disk_budget.index_bytes
        ),
        pre_run_checks=pre_run_checks,
        stages=stages,
        post_run_checks=checks,
    )


__all__ = [
    "ALIGNMENT_METHOD_ID",
    "INTAKE_METHOD_ID",
    "AlignmentDiskBudget",
    "BasecallerIdentity",
    "ExactToolIdentity",
    "IntakeResult",
    "ModbamAlignmentPlan",
    "ModbamIntakeBounds",
    "ModbamIntakeError",
    "ModbamIntakeSummary",
    "PrivateModbamManifest",
    "RegisteredGrch38Asset",
    "build_grch38_alignment_plan",
    "inspect_modbam_chunks",
]
