"""Pure, strict contracts for the local Traceback runner."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from evidence_inspector.models import Sha256, canonical_json_bytes, sha256_bytes

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RunnerContract(BaseModel):
    """Closed, immutable base for runner boundary objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class InputKind(StrEnum):
    MODBAM = "modbam"
    MINKNOW_POD5 = "minknow_pod5"


class JobState(StrEnum):
    DISCOVERED = "discovered"
    SNAPSHOTTING = "snapshotting"
    VALIDATING = "validating"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    VALIDATING_OUTPUT = "validating_output"
    SIGNING = "signing"
    COMPLETE = "complete"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class PreflightOutcome(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCKED = "blocked"


class ArtifactCommitment(RunnerContract):
    """Export-safe commitment; deliberately contains no locator or raw digest."""

    role: Identifier
    size_bytes: int = Field(ge=0)
    provider_hmac_sha256: Sha256


class WorkflowRelease(RunnerContract):
    schema_version: Annotated[
        str, StringConstraints(pattern=r"^traceback\.workflow-release\.v1$")
    ] = "traceback.workflow-release.v1"
    release_id: Identifier
    release_sha256: Sha256
    reference_id: Identifier
    measurement_definition_id: Identifier
    supported_input_kinds: tuple[InputKind, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_input_kinds(self) -> WorkflowRelease:
        if len(set(self.supported_input_kinds)) != len(self.supported_input_kinds):
            raise ValueError("supported_input_kinds must be unique")
        return self


class JobRequest(RunnerContract):
    schema_version: Annotated[
        str, StringConstraints(pattern=r"^traceback\.job-request\.v1$")
    ] = "traceback.job-request.v1"
    sample_token: Identifier
    input_kind: InputKind
    input_tree_sha256_local: Sha256
    workflow_release_sha256: Sha256
    execution_options: dict[Identifier, bool | int | str] = Field(default_factory=dict)


class PreflightCheck(RunnerContract):
    code: Annotated[str, StringConstraints(pattern=r"^TBX-[A-Z]+-[0-9]{3}$")]
    outcome: PreflightOutcome
    problem: NonEmptyText
    remediation: NonEmptyText
    owner: NonEmptyText
    retryable: bool


class PreflightReport(RunnerContract):
    schema_version: Annotated[
        str, StringConstraints(pattern=r"^traceback\.preflight\.v1$")
    ] = "traceback.preflight.v1"
    outcome: PreflightOutcome
    checks: tuple[PreflightCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def outcome_matches_checks(self) -> PreflightReport:
        outcomes = {check.outcome for check in self.checks}
        expected = (
            PreflightOutcome.BLOCKED
            if PreflightOutcome.BLOCKED in outcomes
            else PreflightOutcome.WARN
            if PreflightOutcome.WARN in outcomes
            else PreflightOutcome.PASS
        )
        if self.outcome != expected:
            raise ValueError("report outcome must equal the most severe check outcome")
        return self


def job_key(request: JobRequest) -> str:
    """Return the deterministic identity of an exact runner request."""

    return sha256_bytes(canonical_json_bytes(request))


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED: frozenset({JobState.SNAPSHOTTING, JobState.CANCELLED}),
    JobState.SNAPSHOTTING: frozenset(
        {JobState.VALIDATING, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}
    ),
    JobState.VALIDATING: frozenset(
        {JobState.READY, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}
    ),
    JobState.READY: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED}),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.PAUSE_REQUESTED,
            JobState.VALIDATING_OUTPUT,
            JobState.RETRYABLE_FAILURE,
            JobState.TERMINAL_FAILURE,
        }
    ),
    JobState.PAUSE_REQUESTED: frozenset(
        {JobState.PAUSED, JobState.RETRYABLE_FAILURE}
    ),
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.VALIDATING_OUTPUT: frozenset(
        {JobState.SIGNING, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}
    ),
    JobState.SIGNING: frozenset(
        {JobState.COMPLETE, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}
    ),
    JobState.RETRYABLE_FAILURE: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.COMPLETE: frozenset({JobState.SUPERSEDED}),
    JobState.TERMINAL_FAILURE: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.SUPERSEDED: frozenset(),
}


def validate_transition(previous: JobState, next_state: JobState) -> None:
    """Raise when a requested state transition is not part of release one."""

    if next_state not in _ALLOWED_TRANSITIONS[previous]:
        raise ValueError(f"invalid job transition: {previous} -> {next_state}")
