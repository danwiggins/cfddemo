"""Operator-safe state presentation and diagnostic redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from pydantic import Field

from .contracts import JobState, NonEmptyText, RunnerContract

STALE_AFTER_SECONDS = 45


class OperatorCategory(StrEnum):
    """Stable queue categories used by future local operator surfaces."""

    NEEDS_ATTENTION = "needs_attention"
    PROCESSING = "processing"
    QUEUED = "queued"
    COMPLETED_RECORDS = "completed_records"
    INACTIVE = "inactive"


class RecordAvailability(StrEnum):
    """Separate job progress from signed-record availability."""

    UNAVAILABLE = "unavailable"
    VERIFYING = "verifying"
    READY = "ready"


class OperatorBlocker(RunnerContract):
    code: str = Field(pattern=r"^TBX-[A-Z]+-[0-9]{3}$")
    problem: NonEmptyText
    likely_cause: NonEmptyText
    exact_fix: NonEmptyText
    owner: NonEmptyText
    retryable: bool
    docs_path: str = Field(pattern=r"^docs/[A-Za-z0-9._/-]+$")


class OperatorJobView(RunnerContract):
    schema_version: str = Field(pattern=r"^traceback\.operator-state\.v1$")
    job_id: str
    state: JobState
    category: OperatorCategory
    headline: NonEmptyText
    next_action: NonEmptyText
    stale: bool
    record_availability: RecordAvailability
    blocker: OperatorBlocker | None = None


_PROCESSING = frozenset(
    {
        JobState.SNAPSHOTTING,
        JobState.VALIDATING,
        JobState.RUNNING,
        JobState.BASECALLING,
        JobState.ALIGNING,
        JobState.SORTING_INDEXING,
        JobState.TECHNICAL_QC,
        JobState.MEASURING,
        JobState.VALIDATING_OUTPUT,
        JobState.SIGNING,
        JobState.PAUSE_REQUESTED,
    }
)
_NEEDS_ATTENTION = frozenset(
    {JobState.PAUSED, JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE}
)


def build_job_view(
    *,
    job_id: str,
    state: JobState,
    observed_at: datetime,
    now: datetime | None = None,
    signature_verified: bool = False,
    blocker: OperatorBlocker | None = None,
    local_unqualified: bool = False,
) -> OperatorJobView:
    """Map a runner state to an operator view without overstating completion."""

    current = now or datetime.now(UTC)
    if observed_at.tzinfo is None or current.tzinfo is None:
        raise ValueError("operator timestamps must be timezone-aware")
    age_seconds = max(0.0, (current - observed_at).total_seconds())
    stale = age_seconds > STALE_AFTER_SECONDS

    if blocker is not None or state in _NEEDS_ATTENTION:
        category = OperatorCategory.NEEDS_ATTENTION
    elif state in _PROCESSING:
        category = OperatorCategory.PROCESSING
    elif state in {
        JobState.DISCOVERED,
        JobState.WAITING_FOR_FINALIZATION,
        JobState.READY,
        JobState.QUEUED,
    }:
        category = OperatorCategory.QUEUED
    elif state == JobState.COMPLETE and signature_verified:
        category = OperatorCategory.COMPLETED_RECORDS
    else:
        category = OperatorCategory.INACTIVE

    if state == JobState.COMPLETE:
        availability = (
            RecordAvailability.READY
            if signature_verified
            else RecordAvailability.VERIFYING
        )
    else:
        availability = RecordAvailability.UNAVAILABLE

    if stale:
        headline = "Runner status is stale"
        next_action = "Refresh runner status before starting another action"
    elif blocker is not None:
        headline = blocker.problem
        next_action = blocker.exact_fix
    elif availability == RecordAvailability.READY:
        headline = "Signed local record ready"
        next_action = "Inspect or save the verified local record"
    elif availability == RecordAvailability.VERIFYING:
        headline = "Record created; verification required"
        next_action = "Verify the signature with a configured development trust root"
    elif state == JobState.RETRYABLE_FAILURE:
        headline = "Job needs recovery"
        next_action = "Review the redacted log, fix the blocker, then retry"
    elif state == JobState.PAUSED:
        headline = "Job paused"
        next_action = "Resume when the local runner is ready"
    elif state in _PROCESSING:
        headline = (
            "Local unqualified job processing" if local_unqualified else "Synthetic job processing"
        )
        next_action = "Wait for the next verified checkpoint"
    elif state in {
        JobState.DISCOVERED,
        JobState.WAITING_FOR_FINALIZATION,
        JobState.READY,
        JobState.QUEUED,
    }:
        headline = "Local unqualified job queued" if local_unqualified else "Synthetic job queued"
        next_action = f"Run traceback resume {job_id} --root <same-root> to start local execution"
    else:
        headline = f"Job {state.value.replace('_', ' ')}"
        next_action = "Inspect the local job state"

    return OperatorJobView(
        schema_version="traceback.operator-state.v1",
        job_id=job_id,
        state=state,
        category=category,
        headline=headline,
        next_action=next_action,
        stale=stale,
        record_availability=availability,
        blocker=blocker,
    )


_SENSITIVE_KEY = re.compile(
    r"(?:path|filename|sample|read[_-]?id|sequence|secret|token|password|sha256)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_. -]+){2,}")


def redact_diagnostic(value: Any) -> Any:
    """Return a JSON-safe diagnostic with identifiers and local paths removed."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(str(key))
                else redact_diagnostic(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic(item) for item in value]
    if isinstance(value, PurePath):
        return "[REDACTED]"
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("[REDACTED]", value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return "[REDACTED]"


def support_payload(
    *,
    job_state: JobState,
    error_codes: Sequence[str],
    events: Sequence[Mapping[str, Any]],
    runner_version: str,
) -> dict[str, Any]:
    """Build a deliberately small support payload with no local identifiers."""

    safe_event_keys = {
        "code",
        "state",
        "stage",
        "retryable",
        "timestamp",
        "attempt",
        "resource_percent",
    }
    safe_events = [
        {
            key: redact_diagnostic(value)
            for key, value in event.items()
            if key in safe_event_keys
        }
        for event in events
    ]
    return {
        "schema_version": "traceback.support-bundle.v1",
        "job_state": job_state.value,
        "error_codes": sorted(set(error_codes)),
        "runner_version": runner_version,
        "events": safe_events,
        "scope": "redacted local diagnostics; no raw genomic data",
    }
