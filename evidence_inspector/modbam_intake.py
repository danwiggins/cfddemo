"""Private, fail-closed intake contracts for ordered unaligned modBAM chunks.

This module never publishes paths, filenames, read names, sequences, or BAM
header records.  The detailed manifest is a private local execution artifact;
``ModbamIntakeSummary`` is the only model intended for public status surfaces.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol, Sequence

from pydantic import Field, StringConstraints, model_validator

from .models import Identifier, Sha256, StrictModel, canonical_json_bytes

INTAKE_SCHEMA_VERSION = "traceback.modbam-intake.v1"
INTAKE_METHOD_ID = "traceback.modbam-intake.full-scan.v1"
ALIGNMENT_PLAN_SCHEMA_VERSION = "traceback.modbam-alignment-plan.v1"
ALIGNMENT_METHOD_ID = "traceback.modbam-align-grch38.preserve-mm-ml-mn.v1"
SAMTOOLS_SORT_MEMORY_BYTES = 268_435_456
_BAM_FIXED_OVERHEAD_BYTES = 1_048_576
_ALIGNED_RECORD_OVERHEAD_BYTES = 512
_INDEX_MINIMUM_BYTES = 1_048_576

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


class RegisteredToolIdentity(ExactToolIdentity):
    """Tool identity resolved from the same immutable execution registry."""

    registry_record_sha256: Sha256


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
    unsupported_layout_records: int = Field(ge=0)
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
            + self.unsupported_layout_records
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
    version: Identifier
    assembly: Literal["GRCh38"] = "GRCh38"
    registry_record_sha256: Sha256
    fasta_sha256: Sha256
    fai_sha256: Sha256
    minimap2_index_sha256: Sha256
    sequence_dictionary_sha256: Sha256


class AlignmentDiskBudget(StrictModel):
    """Explicit byte ceilings checked before execution begins."""

    measurement_method: Literal["shutil.disk_usage.v1"] = "shutil.disk_usage.v1"
    workspace_mount_sha256: Sha256
    output_mount_sha256: Sha256
    measurement_sha256: Sha256
    combined_bam_bytes: int = Field(ge=1)
    sort_temporary_bytes: int = Field(ge=1)
    aligned_bam_bytes: int = Field(ge=1)
    index_bytes: int = Field(ge=1)
    available_workspace_bytes: int = Field(ge=1)
    available_output_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_capacity(self) -> AlignmentDiskBudget:
        expected_measurement = hashlib.sha256(
            canonical_json_bytes(
                {
                    "measurement_method": self.measurement_method,
                    "workspace_mount_sha256": self.workspace_mount_sha256,
                    "output_mount_sha256": self.output_mount_sha256,
                    "available_workspace_bytes": self.available_workspace_bytes,
                    "available_output_bytes": self.available_output_bytes,
                }
            )
        ).hexdigest()
        if self.measurement_sha256 != expected_measurement:
            raise ValueError("disk capacity measurement digest is invalid")
        workspace_required = self.combined_bam_bytes + self.sort_temporary_bytes
        output_required = self.aligned_bam_bytes + self.index_bytes
        if workspace_required > self.available_workspace_bytes:
            raise ValueError("workspace disk budget is insufficient")
        if output_required > self.available_output_bytes:
            raise ValueError("output disk budget is insufficient")
        if self.workspace_mount_sha256 == self.output_mount_sha256:
            shared_required = workspace_required + output_required
            if shared_required > min(
                self.available_workspace_bytes,
                self.available_output_bytes,
            ):
                raise ValueError("shared-filesystem disk budget is insufficient")
        return self


def measure_alignment_disk_budget(
    workspace_path: str | Path,
    output_path: str | Path,
    *,
    combined_bam_bytes: int,
    sort_temporary_bytes: int,
    aligned_bam_bytes: int,
    index_bytes: int,
) -> AlignmentDiskBudget:
    """Measure local capacity while retaining only private mount fingerprints."""

    workspace = Path(workspace_path).resolve(strict=True)
    output = Path(output_path).resolve(strict=True)
    available_workspace = shutil.disk_usage(workspace).free
    available_output = shutil.disk_usage(output).free
    workspace_digest = hashlib.sha256(os.fsencode(workspace)).hexdigest()
    output_digest = hashlib.sha256(os.fsencode(output)).hexdigest()
    payload = {
        "measurement_method": "shutil.disk_usage.v1",
        "workspace_mount_sha256": workspace_digest,
        "output_mount_sha256": output_digest,
        "available_workspace_bytes": available_workspace,
        "available_output_bytes": available_output,
    }
    return AlignmentDiskBudget(
        workspace_mount_sha256=workspace_digest,
        output_mount_sha256=output_digest,
        measurement_sha256=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        combined_bam_bytes=combined_bam_bytes,
        sort_temporary_bytes=sort_temporary_bytes,
        aligned_bam_bytes=aligned_bam_bytes,
        index_bytes=index_bytes,
        available_workspace_bytes=available_workspace,
        available_output_bytes=available_output,
    )


class AlignmentDiskMinimums(StrictModel):
    """Conservative admission minima, not scientific or compression estimates."""

    policy_id: Literal["traceback.modbam-disk-minimums.v1"] = (
        "traceback.modbam-disk-minimums.v1"
    )
    combined_bam_bytes: int = Field(ge=1)
    sort_temporary_bytes: int = Field(ge=1)
    aligned_bam_bytes: int = Field(ge=1)
    index_bytes: int = Field(ge=1)
    samtools_sort_memory_bytes: Literal[268435456] = SAMTOOLS_SORT_MEMORY_BYTES


class ExecutionStage(StrictModel):
    id: Identifier
    tool: RegisteredToolIdentity
    argv: tuple[str, ...] = Field(min_length=2)
    stdin_from_stage: Identifier | None = None


class PostRunCheck(StrictModel):
    id: Identifier
    requirement: ShortText


class AlignmentRegistryIdentity(StrictModel):
    registry_id: Identifier
    version: Identifier
    immutable_snapshot_sha256: Sha256


class AlignmentResourceRegistry(Protocol):
    """Trusted resolver boundary for reference and executable attestations."""

    @property
    def identity(self) -> AlignmentRegistryIdentity: ...

    def resolve_grch38(
        self, asset_id: str, version: str
    ) -> RegisteredGrch38Asset: ...

    def resolve_tool(self, name: str, version: str) -> RegisteredToolIdentity: ...


class ModbamAlignmentPlan(StrictModel):
    """Auditable, non-executing plan using only private mount aliases."""

    schema_version: Literal["traceback.modbam-alignment-plan.v1"] = (
        ALIGNMENT_PLAN_SCHEMA_VERSION
    )
    privacy_classification: Literal["private_local_only"] = "private_local_only"
    method_id: Literal[
        "traceback.modbam-align-grch38.preserve-mm-ml-mn.v1"
    ] = ALIGNMENT_METHOD_ID
    plan_sha256: Sha256
    input_manifest_sha256: Sha256
    resource_registry: AlignmentRegistryIdentity
    expected_chunk_count: int = Field(ge=1)
    expected_input_bytes: int = Field(ge=1)
    expected_record_count: int = Field(ge=1)
    reference: RegisteredGrch38Asset
    disk_minimums: AlignmentDiskMinimums
    disk_budget: AlignmentDiskBudget
    estimated_peak_workspace_bytes: int = Field(ge=1)
    estimated_output_bytes: int = Field(ge=1)
    pre_run_checks: tuple[PostRunCheck, ...] = Field(min_length=4)
    stages: tuple[ExecutionStage, ...] = Field(min_length=4)
    post_run_checks: tuple[PostRunCheck, ...] = Field(min_length=6)

    @model_validator(mode="after")
    def reconcile_disk_estimates(self) -> ModbamAlignmentPlan:
        plan_payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        if self.plan_sha256 != hashlib.sha256(
            canonical_json_bytes(plan_payload)
        ).hexdigest():
            raise ValueError("alignment plan digest is invalid")
        if self.disk_minimums != _disk_minimums(
            self.expected_input_bytes, self.expected_record_count
        ):
            raise ValueError("disk minima do not match the sealed admission policy")
        for field_name in (
            "combined_bam_bytes",
            "sort_temporary_bytes",
            "aligned_bam_bytes",
            "index_bytes",
        ):
            if getattr(self.disk_budget, field_name) < getattr(
                self.disk_minimums, field_name
            ):
                raise ValueError("disk ceiling is below the derived minimum")
        if self.estimated_peak_workspace_bytes != (
            self.disk_budget.combined_bam_bytes
            + self.disk_budget.sort_temporary_bytes
        ):
            raise ValueError("workspace estimate must reconcile with byte ceilings")
        if self.estimated_output_bytes != (
            self.disk_budget.aligned_bam_bytes + self.disk_budget.index_bytes
        ):
            raise ValueError("output estimate must reconcile with byte ceilings")
        if [stage.id for stage in self.stages] != [
            "combine_unaligned_chunks",
            "emit_tagged_fastq",
            "align_grch38",
            "coordinate_sort",
            "build_index",
        ]:
            raise ValueError("alignment stages must match the sealed five-stage topology")
        combine, emit, align, sort_stage, index = self.stages
        if any(
            stage.tool.name != "samtools"
            for stage in (combine, emit, sort_stage, index)
        ) or align.tool.name != "minimap2":
            raise ValueError("stage executable identity does not match sealed tool")
        chunk_args = tuple(
            f"/private/input/chunk-{order:04d}.bam"
            for order in range(self.expected_chunk_count)
        )
        expected_argv = (
            (
                "/private/tools/samtools",
                "cat",
                "-o",
                "/private/work/combined.unaligned.bam",
                *chunk_args,
            ),
            (
                "/private/tools/samtools",
                "fastq",
                "-T",
                "MM,ML,MN",
                "/private/work/combined.unaligned.bam",
            ),
            (
                "/private/tools/minimap2",
                "-a",
                "-x",
                "map-ont",
                "--secondary=no",
                "-y",
                "/private/reference/grch38.mmi",
                "-",
            ),
            (
                "/private/tools/samtools",
                "sort",
                "-@",
                "1",
                "-m",
                "256M",
                "-T",
                "/private/work/sort",
                "-o",
                "/private/output/aligned.sorted.bam",
                "-",
            ),
            (
                "/private/tools/samtools",
                "index",
                "-b",
                "/private/output/aligned.sorted.bam",
                "/private/output/aligned.sorted.bam.bai",
            ),
        )
        if tuple(stage.argv for stage in self.stages) != expected_argv:
            raise ValueError("alignment argv does not match the sealed method")
        if tuple(stage.stdin_from_stage for stage in self.stages) != (
            None,
            None,
            "emit_tagged_fastq",
            "align_grch38",
            None,
        ):
            raise ValueError("alignment pipe topology does not match the sealed method")
        return self


def _disk_minimums(input_bytes: int, record_count: int) -> AlignmentDiskMinimums:
    """Derive v1 byte floors from input bytes and one-output-record policy."""

    combined = input_bytes + _BAM_FIXED_OVERHEAD_BYTES
    sort_temporary = input_bytes * 2 + _BAM_FIXED_OVERHEAD_BYTES
    aligned = (
        input_bytes * 2
        + record_count * _ALIGNED_RECORD_OVERHEAD_BYTES
        + _BAM_FIXED_OVERHEAD_BYTES
    )
    index = max(_INDEX_MINIMUM_BYTES, aligned // 32)
    return AlignmentDiskMinimums(
        combined_bam_bytes=combined,
        sort_temporary_bytes=sort_temporary,
        aligned_bam_bytes=aligned,
        index_bytes=index,
    )


def _sha256_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    handle.seek(0)
    return digest.hexdigest()


def _source_stat_identity(details: os.stat_result) -> tuple[int, ...]:
    """Fields that must remain stable while a private snapshot is created."""

    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _snapshot_source(
    source: BinaryIO,
    snapshot: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    """Copy and hash one already-open source into an unlinked private file."""

    digest = hashlib.sha256()
    copied = 0
    source.seek(0)
    for block in iter(lambda: source.read(1024 * 1024), b""):
        copied += len(block)
        if copied > max_bytes:
            raise ModbamIntakeError(
                "intake rejected: a chunk violates its byte bound"
            )
        snapshot.write(block)
        digest.update(block)
    snapshot.flush()
    os.fsync(snapshot.fileno())
    snapshot.seek(0)
    return digest.hexdigest(), copied


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
    chunks: list[PrivateModbamChunk] = []
    content_ids: set[str] = set()
    scanned_records = 0
    aggregate_bytes = 0
    failure_counts: dict[str, int] = {}
    try:
        import pysam

        for order, path in enumerate(paths):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except OSError:
                raise ModbamIntakeError(
                    "intake rejected: a chunk is not a readable regular file"
                ) from None
            with os.fdopen(descriptor, "rb") as source:
                opened_stat = os.fstat(source.fileno())
                try:
                    named_stat = os.stat(path, follow_symlinks=False)
                except OSError:
                    raise ModbamIntakeError(
                        "intake rejected: a chunk changed during validation"
                    ) from None
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or stat.S_ISLNK(named_stat.st_mode)
                    or (named_stat.st_dev, named_stat.st_ino)
                    != (opened_stat.st_dev, opened_stat.st_ino)
                ):
                    raise ModbamIntakeError(
                        "intake rejected: a chunk is not a readable regular file"
                    )
                if (
                    opened_stat.st_size <= 0
                    or opened_stat.st_size > bounds.max_chunk_bytes
                ):
                    raise ModbamIntakeError(
                        "intake rejected: a chunk violates its byte bound"
                    )
                identity = (opened_stat.st_dev, opened_stat.st_ino)
                if identity in file_ids:
                    raise ModbamIntakeError(
                        "intake rejected: duplicate file identity"
                    )
                file_ids.add(identity)
                aggregate_bytes += opened_stat.st_size
                if aggregate_bytes > bounds.max_total_bytes:
                    raise ModbamIntakeError(
                        "intake rejected: aggregate bytes exceed declared bound"
                    )

                with tempfile.TemporaryFile(mode="w+b") as snapshot:
                    content_sha256, copied_bytes = _snapshot_source(
                        source,
                        snapshot,
                        max_bytes=bounds.max_chunk_bytes,
                    )
                    if (
                        copied_bytes != opened_stat.st_size
                        or _source_stat_identity(os.fstat(source.fileno()))
                        != _source_stat_identity(opened_stat)
                    ):
                        raise ModbamIntakeError(
                            "intake rejected: a chunk changed during validation"
                        )
                    if content_sha256 in content_ids:
                        raise ModbamIntakeError(
                            "intake rejected: duplicate content identity"
                        )
                    content_ids.add(content_sha256)
                    snapshot_stat = os.fstat(snapshot.fileno())
                    with pysam.AlignmentFile(
                        snapshot,
                        "rb",
                        check_sq=False,
                    ) as alignment:
                        try:
                            safe_header, private_header_sha256 = _header_identities(
                                alignment
                            )
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
                            "unsupported_layout_records": 0,
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
                                    "intake rejected: record count exceeds "
                                    "declared bound"
                                )
                            if bool(record.is_secondary):
                                counts["secondary_records"] += 1
                            elif bool(record.is_supplementary):
                                counts["supplementary_records"] += 1
                            elif (
                                bool(record.is_reverse)
                                or bool(record.is_paired)
                                or record.next_reference_id != -1
                                or record.next_reference_start != -1
                                or record.template_length != 0
                            ):
                                counts["unsupported_layout_records"] += 1
                            elif (
                                not bool(record.is_unmapped)
                                or record.reference_id != -1
                            ):
                                counts["mapped_records"] += 1
                            else:
                                counts["primary_unmapped_records"] += 1
                            valid, present = _tags_valid(record)
                            if all(present):
                                counts["complete_tag_records"] += 1
                                validity_key = (
                                    "valid_tag_records"
                                    if valid
                                    else "invalid_tag_records"
                                )
                                counts[validity_key] += 1
                            else:
                                counts["incomplete_tag_records"] += 1
                            for tag, is_present in zip(
                                ("mm", "ml", "mn"),
                                present,
                                strict=True,
                            ):
                                if not is_present:
                                    counts[f"missing_{tag}_records"] += 1

                    final_snapshot_stat = os.fstat(snapshot.fileno())
                    if (
                        _source_stat_identity(final_snapshot_stat)
                        != _source_stat_identity(snapshot_stat)
                        or final_snapshot_stat.st_size != copied_bytes
                        or _sha256_handle(snapshot) != content_sha256
                    ):
                        raise ModbamIntakeError(
                            "intake rejected: private snapshot integrity failed"
                        )
                    if (
                        _source_stat_identity(os.fstat(source.fileno()))
                        != _source_stat_identity(opened_stat)
                    ):
                        raise ModbamIntakeError(
                            "intake rejected: a chunk changed during validation"
                        )
                    try:
                        final_named_stat = os.stat(path, follow_symlinks=False)
                    except OSError:
                        raise ModbamIntakeError(
                            "intake rejected: a chunk changed during validation"
                        ) from None
                    if (
                        stat.S_ISLNK(final_named_stat.st_mode)
                        or (final_named_stat.st_dev, final_named_stat.st_ino)
                        != (opened_stat.st_dev, opened_stat.st_ino)
                    ):
                        raise ModbamIntakeError(
                            "intake rejected: a chunk changed during validation"
                        )

                    ledger = ModbamReadLedger(**counts)
                    if ledger.total_records == 0:
                        failure_counts["empty_chunks"] = (
                            failure_counts.get("empty_chunks", 0) + 1
                        )
                    if ledger.primary_unmapped_records != ledger.total_records:
                        failure_counts["non_primary_unmapped_records"] = (
                            failure_counts.get("non_primary_unmapped_records", 0)
                            + ledger.total_records
                            - ledger.primary_unmapped_records
                        )
                    if ledger.valid_tag_records != ledger.total_records:
                        failure_counts["invalid_or_missing_tag_records"] = (
                            failure_counts.get(
                                "invalid_or_missing_tag_records", 0
                            )
                            + ledger.total_records
                            - ledger.valid_tag_records
                        )
                    chunks.append(
                        PrivateModbamChunk(
                            order=order,
                            size_bytes=copied_bytes,
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
    registry: AlignmentResourceRegistry,
    reference_id: str,
    reference_version: str,
    samtools_version: str,
    minimap2_version: str,
    disk_budget: AlignmentDiskBudget,
) -> ModbamAlignmentPlan:
    """Build, but do not execute, the exact tag-preserving alignment plan."""

    reference = registry.resolve_grch38(reference_id, reference_version)
    samtools = registry.resolve_tool("samtools", samtools_version)
    minimap2 = registry.resolve_tool("minimap2", minimap2_version)
    if (reference.asset_id, reference.version) != (
        reference_id,
        reference_version,
    ):
        raise ModbamIntakeError(
            "alignment plan rejected: registry returned the wrong reference binding"
        )
    if (samtools.name, samtools.version) != ("samtools", samtools_version):
        raise ModbamIntakeError(
            "alignment plan rejected: registry returned the wrong samtools binding"
        )
    if (minimap2.name, minimap2.version) != ("minimap2", minimap2_version):
        raise ModbamIntakeError(
            "alignment plan rejected: registry returned the wrong minimap2 binding"
        )
    total_bytes = sum(chunk.size_bytes for chunk in manifest.ordered_chunks)
    record_count = sum(
        chunk.ledger.total_records for chunk in manifest.ordered_chunks
    )
    disk_minimums = _disk_minimums(total_bytes, record_count)
    for field_name in (
        "combined_bam_bytes",
        "sort_temporary_bytes",
        "aligned_bam_bytes",
        "index_bytes",
    ):
        if getattr(disk_budget, field_name) < getattr(disk_minimums, field_name):
            raise ModbamIntakeError(
                "alignment plan rejected: a disk ceiling is below its derived minimum"
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
                "/private/tools/samtools",
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
                "/private/tools/samtools",
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
                "/private/tools/minimap2",
                "-a",
                "-x",
                "map-ont",
                "--secondary=no",
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
                "/private/tools/samtools",
                "sort",
                "-@",
                "1",
                "-m",
                "256M",
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
                "/private/tools/samtools",
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
            requirement=(
                "Primary mapped plus primary unmapped output count equals the manifest total."
            ),
        ),
        PostRunCheck(
            id="tag_count",
            requirement="Every output record retains valid MM, ML, and MN tags.",
        ),
        PostRunCheck(
            id="secondary_count",
            requirement="Output secondary-record count is zero under --secondary=no.",
        ),
        PostRunCheck(
            id="supplementary_accounting",
            requirement=(
                "Supplementary records are reported separately from the primary denominator."
            ),
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
            requirement=(
                "Total output equals primary mapped, primary unmapped, and supplementary."
            ),
        ),
    )
    plan_fields = dict(
        input_manifest_sha256=manifest.manifest_sha256,
        resource_registry=registry.identity,
        expected_chunk_count=len(manifest.ordered_chunks),
        expected_input_bytes=total_bytes,
        expected_record_count=record_count,
        reference=reference,
        disk_minimums=disk_minimums,
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
    draft = ModbamAlignmentPlan.model_construct(
        plan_sha256="0" * 64,
        **plan_fields,
    )
    plan_sha256 = hashlib.sha256(
        canonical_json_bytes(
            draft.model_dump(mode="json", exclude={"plan_sha256"})
        )
    ).hexdigest()
    return ModbamAlignmentPlan(plan_sha256=plan_sha256, **plan_fields)


def validate_registered_alignment_plan(
    plan: ModbamAlignmentPlan,
    registry: AlignmentResourceRegistry,
) -> ModbamAlignmentPlan:
    """Re-resolve registry records before executing a serialized plan."""

    if registry.identity != plan.resource_registry:
        raise ModbamIntakeError(
            "alignment plan rejected: registry snapshot identity changed"
        )
    reference = registry.resolve_grch38(
        plan.reference.asset_id,
        plan.reference.version,
    )
    samtools = registry.resolve_tool(
        "samtools",
        plan.stages[0].tool.version,
    )
    minimap2 = registry.resolve_tool(
        "minimap2",
        plan.stages[2].tool.version,
    )
    if reference != plan.reference:
        raise ModbamIntakeError(
            "alignment plan rejected: reference registry binding changed"
        )
    if any(
        stage.tool != samtools
        for stage in (
            plan.stages[0],
            plan.stages[1],
            plan.stages[3],
            plan.stages[4],
        )
    ) or plan.stages[2].tool != minimap2:
        raise ModbamIntakeError(
            "alignment plan rejected: tool registry binding changed"
        )
    return plan


__all__ = [
    "ALIGNMENT_METHOD_ID",
    "INTAKE_METHOD_ID",
    "AlignmentDiskBudget",
    "AlignmentDiskMinimums",
    "AlignmentRegistryIdentity",
    "AlignmentResourceRegistry",
    "BasecallerIdentity",
    "ExactToolIdentity",
    "IntakeResult",
    "ModbamAlignmentPlan",
    "ModbamIntakeBounds",
    "ModbamIntakeError",
    "ModbamIntakeSummary",
    "PrivateModbamManifest",
    "RegisteredToolIdentity",
    "RegisteredGrch38Asset",
    "build_grch38_alignment_plan",
    "inspect_modbam_chunks",
    "measure_alignment_disk_budget",
    "validate_registered_alignment_plan",
]
