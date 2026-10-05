"""Adapter from the durable runner store to the frozen browser projection.

Each projected job names its analysis (read from the sample token:
``local-<ref>``, ``local-<ref>:cell-origin`` or ``local-<ref>:copy-number``)
and, when it failed on a coded problem, that code with a fixed label from
``state_copy`` (signal SH5).  The stored failure summary itself never leaves
the job store: it may name a file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from traceback_runner.contracts import JobState
from traceback_runner.operator import build_job_view
from traceback_runner.problems import CODED_FAILURE
from traceback_runner.store import JobStore, StoredJobProjectionSnapshot

from .contracts import JobProjection, OpaqueId, ProblemDetail, ProblemOwner
from .state_copy import copy_for, job_problem_copy

_PUBLIC_JOB_ID = re.compile(r"^job_([0-9a-f]{32})$")
_PROBLEM_CODE = re.compile(r"^TBX-[A-Z]+(?:-[A-Z]+)?-[0-9]{3}$")
_FAILED_STATES = frozenset({JobState.RETRYABLE_FAILURE, JobState.TERMINAL_FAILURE})
_LOCAL_SAMPLE_PREFIX = "local-"
# Sample-token suffixes reserved for the signal analyses; any other suffix is
# a fragment policy (D2), and a token without one is the built-in fragment run.
_ANALYSIS_SUFFIXES: dict[str, Literal["cell_origin", "copy_number"]] = {
    "cell-origin": "cell_origin",
    "copy-number": "copy_number",
}

Analysis = Literal["fragment", "cell_origin", "copy_number"]


def analysis_of_sample_token(sample_token: str) -> Analysis:
    """The analysis a job's sample token names; ``fragment`` unless reserved."""

    if not sample_token.startswith(_LOCAL_SAMPLE_PREFIX):
        return "fragment"
    _, _, suffix = sample_token.partition(":")
    return _ANALYSIS_SUFFIXES.get(suffix, "fragment")


def job_problem(job_id: str, state: JobState, last_error: str | None) -> ProblemDetail | None:
    """The coded problem a failed job stopped on, with its fixed label; else ``None``."""

    if state not in _FAILED_STATES or last_error is None:
        return None
    match = CODED_FAILURE.match(last_error)
    if match is None or not _PROBLEM_CODE.fullmatch(match.group(1)):
        return None
    code = match.group(1)
    label, meaning = job_problem_copy(code)
    return ProblemDetail(
        code=code,
        problem=label,
        cause=meaning,
        fix="Run traceback logs for this job for the cause and the fix",
        docs_path="docs/OPERATOR-GUIDE.md",
        owner=ProblemOwner.OPERATOR,
        retryable=state == JobState.RETRYABLE_FAILURE,
        correlation_id=f"cor_{job_id.removeprefix('job_')[:16]}",
        preserved_work="Existing verified work is unchanged",
        repeated_work="No work was repeated",
    )


class JobStoreProjectionSource:
    """Read bounded projections from SQLite; never maintain a second job store."""

    def __init__(
        self,
        store: JobStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        limit: int = 100,
    ) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("browser queue limit must be between 1 and 100")
        self._store = store
        self._now = now
        self._limit = limit

    @staticmethod
    def _public_id(snapshot: StoredJobProjectionSnapshot) -> str:
        return f"job_{snapshot.record.job_id}"

    @staticmethod
    def _private_id(job_id: OpaqueId) -> str:
        match = _PUBLIC_JOB_ID.fullmatch(job_id)
        if match is None:
            raise KeyError(job_id)
        return match.group(1)

    def _project(self, snapshot: StoredJobProjectionSnapshot) -> JobProjection:
        record = snapshot.record
        observed_at = datetime.fromtimestamp(record.updated_at, UTC)
        operator_view = build_job_view(
            job_id=self._public_id(snapshot),
            state=record.state,
            observed_at=observed_at,
            now=self._now(),
        )
        stage_label = (record.current_stage or record.state.value).replace("_", " ")
        next_action = (
            "Refresh local runner status"
            if operator_view.stale
            else "Inspect this local job"
        )
        try:
            analysis = analysis_of_sample_token(self._store.request(record.job_id).sample_token)
        except (KeyError, ValueError):
            analysis = "fragment"
        return JobProjection(
            job_id=self._public_id(snapshot),
            state=record.state,
            analysis=analysis,
            analysis_label=copy_for("analysis", analysis)[0],
            stage_label=stage_label,
            updated_at=observed_at,
            revision=snapshot.revision,
            stale=operator_view.stale,
            headline=operator_view.headline,
            owner=ProblemOwner.OPERATOR,
            next_action=next_action,
            problem=job_problem(self._public_id(snapshot), record.state, record.last_error),
            actions=(),
        )

    def list_jobs(self) -> tuple[JobProjection, ...]:
        return tuple(
            self._project(snapshot)
            for snapshot in self._store.list_projection_snapshots(limit=self._limit)
        )

    def get_job(self, job_id: OpaqueId) -> JobProjection:
        return self._project(
            self._store.get_projection_snapshot(self._private_id(job_id))
        )
