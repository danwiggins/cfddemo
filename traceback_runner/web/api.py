"""Pure B01 local API kernel over frozen operator projections."""

from __future__ import annotations

from typing import Protocol

from .auth import BrowserRequest, LocalWebBoundary
from .contracts import JobProjection, OpaqueId, ProblemDetail


class ApiProblem(LookupError):
    def __init__(self, status_code: int, problem: ProblemDetail) -> None:
        super().__init__(problem.problem)
        self.status_code = status_code
        self.problem = problem


class JobProjectionSource(Protocol):
    """Bounded source backed by the authoritative runner state."""

    def list_jobs(self) -> tuple[JobProjection, ...]: ...

    def get_job(self, job_id: OpaqueId) -> JobProjection: ...


class LocalApiKernel:
    """Authorize before projection lookup so guessed IDs disclose nothing."""

    def __init__(
        self,
        *,
        boundary: LocalWebBoundary,
        source: JobProjectionSource,
        not_found_problem: ProblemDetail,
    ) -> None:
        self._boundary = boundary
        self._source = source
        self._not_found_problem = not_found_problem

    def list_jobs(self, request: BrowserRequest) -> tuple[JobProjection, ...]:
        self._boundary.authorize(request)
        return self._source.list_jobs()

    @property
    def not_found_problem(self) -> ProblemDetail:
        return self._not_found_problem

    def get_job(self, request: BrowserRequest, job_id: OpaqueId) -> JobProjection:
        self._boundary.authorize(request)
        try:
            return self._source.get_job(job_id)
        except KeyError as exc:
            raise ApiProblem(404, self._not_found_problem) from exc
