"""Frozen B01 projection, action, and problem contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from traceback_runner.contracts import JobState, RunnerContract

MAX_ACTIONS = 8


def _safe_operator_text(value: str) -> str:
    if "/Users/" in value or "/home/" in value or "\\Users\\" in value:
        raise ValueError("operator text cannot contain an absolute path")
    if re.search(
        r"(?:donor|patient|sample|read)[_-]?(?:id|identifier)?\s*[:=._-]\s*\S+",
        value,
        flags=re.IGNORECASE,
    ):
        raise ValueError("operator text cannot contain a private identifier")
    return value


SafeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
    AfterValidator(_safe_operator_text),
]
OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=12,
        max_length=96,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$",
    ),
]
CorrelationId = Annotated[
    str,
    StringConstraints(pattern=r"^cor_[a-z0-9]{16,48}$"),
]


class ProblemOwner(StrEnum):
    OPERATOR = "operator"
    SUPPORT = "support"
    WORKFLOW_ADMIN = "workflow_admin"


class ProblemDetail(RunnerContract):
    """One safe problem shape shared by local API, UI, CLI, and support."""

    schema_version: Literal["traceback.problem-detail.v1"] = (
        "traceback.problem-detail.v1"
    )
    code: str = Field(pattern=r"^TBX-[A-Z]+-[0-9]{3}$")
    problem: SafeText
    cause: SafeText
    fix: SafeText
    docs_path: str = Field(pattern=r"^docs/[A-Za-z0-9._/-]+$")
    owner: ProblemOwner
    retryable: bool
    correlation_id: CorrelationId
    preserved_work: SafeText
    repeated_work: SafeText


class ActionKind(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    RETRY = "retry"
    VERIFY = "verify"
    SAVE_LOCAL_COPY = "save_local_copy"


class JobAction(RunnerContract):
    action: ActionKind
    label: SafeText
    expected_revision: int = Field(ge=0)
    enabled: bool
    disabled_reason: SafeText | None = None

    @model_validator(mode="after")
    def explain_disabled_action(self) -> JobAction:
        if self.enabled == (self.disabled_reason is not None):
            raise ValueError("disabled actions require exactly one safe reason")
        return self


class JobProjection(RunnerContract):
    """Browser-safe projection; never contains a source path or raw identifier."""

    schema_version: Literal["traceback.job-projection.v1"] = (
        "traceback.job-projection.v1"
    )
    job_id: OpaqueId
    state: JobState
    stage_label: SafeText
    updated_at: datetime
    revision: int = Field(ge=0)
    stale: bool
    headline: SafeText
    owner: ProblemOwner
    next_action: SafeText
    problem: ProblemDetail | None = None
    actions: tuple[JobAction, ...] = Field(default=(), max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def stale_projection_disables_mutations(self) -> JobProjection:
        kinds = [action.action for action in self.actions]
        if kinds != sorted(kinds, key=str) or len(kinds) != len(set(kinds)):
            raise ValueError("job actions must be uniquely sorted")
        if self.stale and any(action.enabled for action in self.actions):
            raise ValueError("stale projections cannot expose enabled actions")
        if any(action.expected_revision != self.revision for action in self.actions):
            raise ValueError("job actions must bind the current projection revision")
        return self
