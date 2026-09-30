"""Pure B01 local API kernel over frozen operator projections."""

from __future__ import annotations

from collections.abc import Iterable

from .auth import BrowserRequest, LocalWebBoundary
from .contracts import JobProjection, OpaqueId, ProblemDetail


class ApiProblem(LookupError):
    def __init__(self, status_code: int, problem: ProblemDetail) -> None:
        super().__init__(problem.problem)
        self.status_code = status_code
        self.problem = problem


class LocalApiKernel:
    """Authorize before projection lookup so guessed IDs disclose nothing."""

    def __init__(
        self,
        *,
        boundary: LocalWebBoundary,
        jobs: Iterable[JobProjection],
        not_found_problem: ProblemDetail,
    ) -> None:
        self._boundary = boundary
        self._jobs = {job.job_id: job for job in jobs}
        self._not_found_problem = not_found_problem

    def list_jobs(self, request: BrowserRequest) -> tuple[JobProjection, ...]:
        self._boundary.authorize(request)
        return tuple(self._jobs[key] for key in sorted(self._jobs))

    def get_job(self, request: BrowserRequest, job_id: OpaqueId) -> JobProjection:
        self._boundary.authorize(request)
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise ApiProblem(404, self._not_found_problem) from exc
