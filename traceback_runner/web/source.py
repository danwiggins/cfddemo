"""Adapter from the durable runner store to the frozen browser projection."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from traceback_runner.operator import build_job_view
from traceback_runner.store import JobStore, StoredJobProjectionSnapshot

from .contracts import JobProjection, OpaqueId, ProblemOwner

_PUBLIC_JOB_ID = re.compile(r"^job_([0-9a-f]{32})$")


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
        return JobProjection(
            job_id=self._public_id(snapshot),
            state=record.state,
            stage_label=stage_label,
            updated_at=observed_at,
            revision=snapshot.revision,
            stale=operator_view.stale,
            headline=operator_view.headline,
            owner=ProblemOwner.OPERATOR,
            next_action=next_action,
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
