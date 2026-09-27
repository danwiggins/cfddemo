"""Strict, immutable, versioned contracts for the local Traceback runner.

All release objects in this first wave are synthetic-only and explicitly
unapproved for real genomic data, hardware, scientific, or protocol use.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from evidence_inspector.models import Sha256

from .serialization import canonical_json_bytes, canonical_model_from_bytes, sha256_bytes

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^TBX-[A-Z]+-[0-9]{3}$")]
Md5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class RunnerContract(BaseModel):
    """Closed boundary object; child containers also use immutable types."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True,
        validate_default=True, allow_inf_nan=False,
    )


class InputKind(StrEnum):
    MODBAM = "modbam"
    MINKNOW_POD5 = "minknow_pod5"


class ApprovalState(StrEnum):
    UNAPPROVED_SYNTHETIC = "unapproved_synthetic"


class PreflightOutcome(StrEnum):
    PASS = "pass"
    WARN = "warn"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class JobState(StrEnum):
    DISCOVERED = "discovered"
    WAITING_FOR_FINALIZATION = "waiting_for_finalization"
    SNAPSHOTTING = "snapshotting"
    VALIDATING = "validating"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    BASECALLING = "basecalling"
    ALIGNING = "aligning"
    SORTING_INDEXING = "sorting_indexing"
    TECHNICAL_QC = "technical_qc"
    MEASURING = "measuring"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    VALIDATING_OUTPUT = "validating_output"
    SIGNING = "signing"
    COMPLETE = "complete"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class StageName(StrEnum):
    SNAPSHOT = "snapshot"
    VALIDATE = "validate"
    BASECALL = "basecall"
    ALIGN = "align"
    SORT_INDEX = "sort_index"
    TECHNICAL_QC = "technical_qc"
    MEASURE = "measure"
    VALIDATE_OUTPUT = "validate_output"
    SIGN = "sign"


class ReceiptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CompletionState(StrEnum):
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    CAPPED = "capped"


class LocalArtifact(RunnerContract):
    role: Identifier
    relative_path: str
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    sha256_local: Sha256
    stable: bool

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or value in {"", "."}:
            raise ValueError("relative_path must be a safe root-relative POSIX path")
        return value


class ArtifactCommitment(RunnerContract):
    """Export-safe commitment with no locator or reusable raw digest."""

    role: Identifier
    artifact_token: Identifier
    size_bytes: int = Field(ge=0)
    provider_hmac_sha256: Sha256


class LocalRunPackage(RunnerContract):
    schema_version: Literal["traceback.run-package-local.v1"] = "traceback.run-package-local.v1"
    run_id: Identifier
    input_kind: InputKind
    protocol_run_id: Identifier
    sample_id: Identifier
    completed: bool
    artifacts: tuple[LocalArtifact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def deterministic_unique_artifacts(self) -> LocalRunPackage:
        keys = [(item.role, item.relative_path) for item in self.artifacts]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("artifacts must be uniquely sorted by role and relative_path")
        return self


class ExportRunProvenance(RunnerContract):
    schema_version: Literal["traceback.run-provenance.v1"] = "traceback.run-provenance.v1"
    run_token: Identifier
    input_kind: InputKind
    protocol_run_token: Identifier
    workflow_release_id: Identifier
    artifacts: tuple[ArtifactCommitment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def deterministic_unique_artifacts(self) -> ExportRunProvenance:
        keys = [(item.role, item.artifact_token) for item in self.artifacts]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("artifact commitments must be uniquely sorted by role and token")
        return self


class ReferenceContig(RunnerContract):
    name: Identifier
    length: int = Field(gt=0)
    md5: Md5


class RegisteredReference(RunnerContract):
    schema_version: Literal["traceback.registered-reference.v1"] = "traceback.registered-reference.v1"
    reference_id: Identifier
    assembly: Identifier
    asset_sha256: Sha256
    contigs: tuple[ReferenceContig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_contigs(self) -> RegisteredReference:
        names = [contig.name for contig in self.contigs]
        if len(names) != len(set(names)):
            raise ValueError("registered reference contig names must be unique")
        return self


class HistogramBin(RunnerContract):
    lower_inclusive: int = Field(ge=0)
    upper_exclusive: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def increasing(self) -> HistogramBin:
        if self.upper_exclusive is not None and self.upper_exclusive <= self.lower_inclusive:
            raise ValueError("histogram bin upper bound must exceed lower bound")
        return self


class FragmentMeasurementPolicy(RunnerContract):
    schema_version: Literal["traceback.fragment-policy.v1"] = "traceback.fragment-policy.v1"
    definition_id: Identifier
    approval_state: Literal[ApprovalState.UNAPPROVED_SYNTHETIC] = ApprovalState.UNAPPROVED_SYNTHETIC
    reference_id: Identifier
    contigs: tuple[Identifier, ...] = Field(min_length=1)
    min_mapping_quality: int = Field(ge=0, le=255)
    consumed_cigar_operations: tuple[Literal["M", "D", "N", "=", "X"], ...] = ("M", "D", "N", "=", "X")
    exclude_unmapped: Literal[True] = True
    exclude_secondary: Literal[True] = True
    exclude_supplementary: Literal[True] = True
    exclude_qc_failure: Literal[True] = True
    exclude_duplicate: Literal[True] = True
    pairing_rule: Literal["count_each_eligible_primary_alignment"] = "count_each_eligible_primary_alignment"
    bins: tuple[HistogramBin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_policy(self) -> FragmentMeasurementPolicy:
        if len(self.contigs) != len(set(self.contigs)):
            raise ValueError("contigs must be unique")
        if self.consumed_cigar_operations != ("M", "D", "N", "=", "X"):
            raise ValueError("v1 consumed CIGAR operations are locked to M,D,N,=,X")
        for index, item in enumerate(self.bins):
            if item.upper_exclusive is None and index != len(self.bins) - 1:
                raise ValueError("only the final histogram bin may be unbounded")
        if self.bins[-1].upper_exclusive is not None:
            raise ValueError("final histogram bin must be explicitly unbounded")
        for previous, current in zip(self.bins, self.bins[1:]):
            if previous.upper_exclusive != current.lower_inclusive:
                raise ValueError("histogram bins must be contiguous and ordered")
        return self


class WorkflowStage(RunnerContract):
    name: StageName
    depends_on: tuple[StageName, ...] = ()
    implementation_digest: Sha256
    timeout_seconds: int = Field(gt=0)


class WorkflowRelease(RunnerContract):
    schema_version: Literal["traceback.workflow-release.v1"] = "traceback.workflow-release.v1"
    release_id: Identifier
    approval_state: Literal[ApprovalState.UNAPPROVED_SYNTHETIC] = ApprovalState.UNAPPROVED_SYNTHETIC
    synthetic_only: Literal[True] = True
    expires_at: datetime
    reference_id: Identifier
    reference_sha256: Sha256
    canonical_basecall_model_id: Identifier | None
    modified_base_model_ids: tuple[Identifier, ...]
    supported_input_kinds: tuple[InputKind, ...] = Field(min_length=1)
    stages: tuple[WorkflowStage, ...] = Field(min_length=1)
    fragment_policy: FragmentMeasurementPolicy
    claims_policy_version: Identifier
    result_signing_key_id: Identifier

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @model_validator(mode="after")
    def internally_consistent(self) -> WorkflowRelease:
        if len(set(self.supported_input_kinds)) != len(self.supported_input_kinds):
            raise ValueError("supported_input_kinds must be unique")
        if len(set(self.modified_base_model_ids)) != len(self.modified_base_model_ids):
            raise ValueError("modified_base_model_ids must be unique")
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("stage names must be unique")
        seen: set[StageName] = set()
        for stage in self.stages:
            if not set(stage.depends_on).issubset(seen):
                raise ValueError("stage dependencies must precede the dependent stage")
            seen.add(stage.name)
        if self.fragment_policy.reference_id != self.reference_id:
            raise ValueError("fragment policy and workflow reference_id must match")
        return self


class ExecutionOptions(RunnerContract):
    offline: Literal[True] = True
    threads: int = Field(default=1, ge=1, le=256)


class JobRequest(RunnerContract):
    schema_version: Literal["traceback.job-request.v1"] = "traceback.job-request.v1"
    sample_token: Identifier
    input_kind: InputKind
    input_tree_sha256_local: Sha256
    workflow_release_sha256: Sha256
    execution_options: ExecutionOptions = Field(default_factory=ExecutionOptions)


class JobRecord(RunnerContract):
    schema_version: Literal["traceback.job.v1"] = "traceback.job.v1"
    job_id: Identifier
    job_key: Sha256
    state: JobState
    workflow_release_sha256: Sha256
    input_snapshot_sha256: Sha256 | None = None
    active_stage: StageName | None = None
    lease_fencing_token: int | None = Field(default=None, ge=1)


class ArtifactDigest(RunnerContract):
    role: Identifier
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class StageReceipt(RunnerContract):
    schema_version: Literal["traceback.stage-receipt.v1"] = "traceback.stage-receipt.v1"
    job_id: Identifier
    stage: StageName
    attempt: int = Field(ge=1)
    fencing_token: int = Field(ge=1)
    workflow_release_sha256: Sha256
    ordered_inputs: tuple[ArtifactDigest, ...]
    outputs: tuple[ArtifactDigest, ...]
    status: ReceiptStatus
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def status_fields(self) -> StageReceipt:
        if self.status == ReceiptStatus.SUCCEEDED and self.error_code is not None:
            raise ValueError("successful receipts cannot contain an error_code")
        if self.status == ReceiptStatus.FAILED and self.error_code is None:
            raise ValueError("failed receipts require an error_code")
        for name, values in (("ordered_inputs", self.ordered_inputs), ("outputs", self.outputs)):
            roles = [item.role for item in values]
            if len(roles) != len(set(roles)):
                raise ValueError(f"{name} roles must be unique")
        return self


class PreflightCheck(RunnerContract):
    code: ErrorCode
    outcome: PreflightOutcome
    problem: NonEmptyText
    remediation: NonEmptyText
    owner: NonEmptyText
    stage: StageName | None = None
    retryable: bool
    likely_cause: NonEmptyText = "Synthetic fixture condition."
    documentation_path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_./-]+$")] = "/docs/OPERATOR-GUIDE"
    supporting_artifact_role: Identifier | None = None


_OUTCOME_SEVERITY = {PreflightOutcome.PASS: 0, PreflightOutcome.WARN: 1, PreflightOutcome.PARTIAL: 2, PreflightOutcome.BLOCKED: 3}


class PreflightReport(RunnerContract):
    schema_version: Literal["traceback.preflight.v1"] = "traceback.preflight.v1"
    outcome: PreflightOutcome
    fragment_measurement_eligible: bool = False
    future_methylation_eligible: bool = False
    checks: tuple[PreflightCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def outcome_matches_checks(self) -> PreflightReport:
        expected = max((check.outcome for check in self.checks), key=_OUTCOME_SEVERITY.__getitem__)
        if self.outcome != expected:
            raise ValueError("report outcome must equal the most severe check outcome")
        if self.outcome == PreflightOutcome.BLOCKED and self.fragment_measurement_eligible:
            raise ValueError("a blocked report cannot be fragment-measurement eligible")
        return self


class ExclusionCounts(RunnerContract):
    unmapped: int = Field(ge=0)
    secondary: int = Field(ge=0)
    supplementary: int = Field(ge=0)
    qc_failure: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    low_mapping_quality: int = Field(ge=0)
    unregistered_contig: int = Field(ge=0)
    no_reference_span: int = Field(ge=0)

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class HistogramCount(RunnerContract):
    bin: HistogramBin
    count: int = Field(ge=0)


class FragmentMeasurement(RunnerContract):
    schema_version: Literal["traceback.fragment-measurement.v1"] = "traceback.fragment-measurement.v1"
    definition_id: Identifier
    approval_state: Literal[ApprovalState.UNAPPROVED_SYNTHETIC] = ApprovalState.UNAPPROVED_SYNTHETIC
    reference_id: Identifier
    completion: CompletionState
    records_scanned: int = Field(ge=0)
    eligible_alignments: int = Field(ge=0)
    exclusions: ExclusionCounts
    histogram: tuple[HistogramCount, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reconciles(self) -> FragmentMeasurement:
        if self.records_scanned != self.eligible_alignments + self.exclusions.total:
            raise ValueError("records_scanned must reconcile eligible and excluded records")
        if sum(item.count for item in self.histogram) != self.eligible_alignments:
            raise ValueError("histogram counts must equal eligible_alignments")
        if self.completion != CompletionState.COMPLETE:
            raise ValueError("interrupted or capped scans cannot construct a measurement")
        if self.eligible_alignments == 0:
            raise ValueError("zero eligible alignments is measurement unavailable")
        return self


class BundleContent(RunnerContract):
    relative_path: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")]
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class ResultBundleManifest(RunnerContract):
    schema_version: Literal["traceback.result-bundle.v1"] = "traceback.result-bundle.v1"
    record_id: Identifier
    workflow_release_id: Identifier
    measurement_schema_versions: tuple[Identifier, ...] = Field(min_length=1)
    contents: tuple[BundleContent, ...] = Field(min_length=1)
    signing_key_id: Identifier
    development_trust_only: Literal[True] = True

    @model_validator(mode="after")
    def ordered_contents(self) -> ResultBundleManifest:
        paths = [item.relative_path for item in self.contents]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("bundle contents must have unique sorted paths")
        return self


class CompatibilityItem(RunnerContract):
    category: Identifier
    item_id: Identifier
    display_name: NonEmptyText
    description: NonEmptyText
    status: Literal["required", "recommended", "optional"]
    instruction_kind: Literal["compatibility_fact", "wet_lab_instruction"] = "compatibility_fact"
    rendering: Literal["display", "withhold"] = "display"
    approval_state: Literal[ApprovalState.UNAPPROVED_SYNTHETIC] = ApprovalState.UNAPPROVED_SYNTHETIC
    protocol_version: Identifier | None = None
    owner: NonEmptyText | None = None
    source: NonEmptyText | None = None
    source_version: Identifier | None = None
    last_reviewed: date | None = None

    @model_validator(mode="after")
    def fail_closed_instructions(self) -> CompatibilityItem:
        provenance = (self.protocol_version, self.owner, self.source, self.source_version, self.last_reviewed)
        if self.instruction_kind == "wet_lab_instruction":
            if self.rendering != "withhold":
                raise ValueError("unapproved wet-lab instructions must be withheld")
            if any(item is None for item in provenance):
                raise ValueError("wet-lab instructions require complete versioned provenance")
        return self


class CompatibilityManifest(RunnerContract):
    schema_version: Literal["traceback.compatibility-manifest.v1"] = "traceback.compatibility-manifest.v1"
    workflow_release_id: Identifier
    synthetic_only: Literal[True] = True
    items: tuple[CompatibilityItem, ...]


def job_key(request: JobRequest) -> str:
    """Return the deterministic identity of an exact runner request."""

    return sha256_bytes(canonical_json_bytes(request))


_ACTIVE_STATES = frozenset({JobState.RUNNING, JobState.BASECALLING, JobState.ALIGNING, JobState.SORTING_INDEXING, JobState.TECHNICAL_QC, JobState.MEASURING})
_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED: frozenset({JobState.WAITING_FOR_FINALIZATION, JobState.SNAPSHOTTING, JobState.CANCELLED}),
    JobState.WAITING_FOR_FINALIZATION: frozenset({JobState.SNAPSHOTTING, JobState.CANCELLED}),
    JobState.SNAPSHOTTING: frozenset({JobState.VALIDATING, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}),
    JobState.VALIDATING: frozenset({JobState.READY, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}),
    JobState.READY: frozenset({JobState.QUEUED, *_ACTIVE_STATES, JobState.CANCELLED}),
    JobState.QUEUED: frozenset({*_ACTIVE_STATES, JobState.CANCELLED}),
    **{state: frozenset({JobState.PAUSE_REQUESTED, JobState.VALIDATING_OUTPUT, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}) for state in _ACTIVE_STATES},
    JobState.PAUSE_REQUESTED: frozenset({JobState.PAUSED, JobState.RETRYABLE_FAILURE}),
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.VALIDATING_OUTPUT: frozenset({JobState.SIGNING, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}),
    JobState.SIGNING: frozenset({JobState.COMPLETE, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}),
    JobState.RETRYABLE_FAILURE: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.COMPLETE: frozenset({JobState.SUPERSEDED}),
    JobState.TERMINAL_FAILURE: frozenset(), JobState.CANCELLED: frozenset(), JobState.SUPERSEDED: frozenset(),
}


def validate_transition(previous: JobState, next_state: JobState) -> None:
    """Raise when a requested state transition is not part of release one."""

    if next_state not in _ALLOWED_TRANSITIONS[previous]:
        raise ValueError(f"invalid job transition: {previous} -> {next_state}")


def receipt_digest(receipt: StageReceipt) -> str:
    """Digest a stage receipt's canonical, path-free representation."""

    return sha256_bytes(canonical_json_bytes(receipt))
