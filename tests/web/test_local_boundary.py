"""B01 localhost threat-boundary and frozen-contract regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest
from pydantic import ValidationError

from traceback_runner.contracts import JobState
from traceback_runner.web.api import ApiProblem, LocalApiKernel
from traceback_runner.web.auth import (
    BoundaryDenied,
    BrowserRequest,
    BootstrapBroker,
    LocalWebBoundary,
    LoopbackServerConfig,
    build_loopback_config,
)
from traceback_runner.web.contracts import (
    ActionKind,
    JobAction,
    JobProjection,
    ProblemDetail,
    ProblemOwner,
)


def _problem() -> ProblemDetail:
    return ProblemDetail(
        code="TBX-WEB-404",
        problem="Requested local object is unavailable",
        cause="The object does not exist or is not visible in this session",
        fix="Refresh the local queue",
        docs_path="docs/operator/jobs.md",
        owner=ProblemOwner.OPERATOR,
        retryable=False,
        correlation_id="cor_0123456789abcdef",
        preserved_work="Existing verified work is unchanged",
        repeated_work="No work was repeated",
    )


def _job(*, stale: bool = False) -> JobProjection:
    action = JobAction(
        action=ActionKind.RETRY,
        label="Retry local operation",
        expected_revision=4,
        enabled=not stale,
        disabled_reason=("Refresh stale status before retrying" if stale else None),
    )
    return JobProjection(
        job_id="job_0123456789abcdef",
        state=JobState.RETRYABLE_FAILURE,
        stage_label="Validate aggregate output",
        updated_at=datetime(2026, 9, 29, tzinfo=UTC),
        revision=4,
        stale=stale,
        headline="Job needs recovery",
        owner=ProblemOwner.OPERATOR,
        next_action="Review the safe problem and retry",
        problem=_problem(),
        actions=(action,),
    )


def _boundary() -> tuple[LocalWebBoundary, BootstrapBroker]:
    tokens = count()
    broker = BootstrapBroker(
        now=lambda: 100.0,
        token_factory=lambda: f"secret-token-{next(tokens):032d}",
    )
    return LocalWebBoundary(build_loopback_config(port=8765), broker), broker


def _grant(boundary: LocalWebBoundary, broker: BootstrapBroker):
    code = broker.issue_bootstrap()
    request = BrowserRequest(
        method="POST",
        path="/api/v1/session/bootstrap",
        host="127.0.0.1:8765",
        origin="http://127.0.0.1:8765",
    )
    return boundary.exchange_bootstrap(request, code), code


@pytest.mark.parametrize("host", ("0.0.0.0", "192.0.2.10", "localhost"))
def test_server_config_refuses_nonliteral_or_nonloopback_bind(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        LoopbackServerConfig(
            bind_host=host,
            port=8765,
            allowed_host_headers=("127.0.0.1:8765",),
            allowed_origins=("http://127.0.0.1:8765",),
        )


def test_bootstrap_is_fragment_only_one_use_and_cookie_is_hardened() -> None:
    boundary, broker = _boundary()
    grant, code = _grant(boundary, broker)

    assert broker.launch_fragment(code).startswith("#bootstrap=")
    assert "?" not in broker.launch_fragment(code)
    assert grant.cookie_http_only
    assert grant.cookie_same_site == "Strict"

    with pytest.raises(BoundaryDenied) as replay:
        boundary.exchange_bootstrap(
            BrowserRequest(
                method="POST",
                path="/api/v1/session/bootstrap",
                host="127.0.0.1:8765",
                origin="http://127.0.0.1:8765",
            ),
            code,
        )
    assert replay.value.status_code == 401


def test_missing_session_cross_origin_host_and_csrf_fail_closed() -> None:
    boundary, broker = _boundary()
    grant, _ = _grant(boundary, broker)

    with pytest.raises(BoundaryDenied) as missing:
        boundary.authorize(
            BrowserRequest(
                method="GET", path="/api/v1/jobs", host="127.0.0.1:8765"
            )
        )
    assert (missing.value.status_code, missing.value.code) == (401, "TBX-AUTH-001")

    for request in (
        BrowserRequest(
            method="POST",
            path="/api/v1/jobs/job_0123456789abcdef/retry",
            host="evil.example:8765",
            origin="http://127.0.0.1:8765",
            session_token=grant.session_token,
            csrf_token=grant.csrf_token,
        ),
        BrowserRequest(
            method="POST",
            path="/api/v1/jobs/job_0123456789abcdef/retry",
            host="127.0.0.1:8765",
            origin="https://evil.example",
            session_token=grant.session_token,
            csrf_token=grant.csrf_token,
        ),
        BrowserRequest(
            method="POST",
            path="/api/v1/jobs/job_0123456789abcdef/retry",
            host="127.0.0.1:8765",
            origin="http://127.0.0.1:8765",
            session_token=grant.session_token,
            csrf_token="wrong-token",
        ),
    ):
        with pytest.raises(BoundaryDenied) as denied:
            boundary.authorize(request)
        assert denied.value.status_code == 403


def test_authorization_precedes_guessed_object_lookup() -> None:
    boundary, broker = _boundary()
    kernel = LocalApiKernel(
        boundary=boundary,
        jobs=(_job(),),
        not_found_problem=_problem(),
    )
    guessed_id = "job_ffffffffffffffff"

    with pytest.raises(BoundaryDenied):
        kernel.get_job(
            BrowserRequest(
                method="GET",
                path=f"/api/v1/jobs/{guessed_id}",
                host="127.0.0.1:8765",
            ),
            guessed_id,
        )

    grant, _ = _grant(boundary, broker)
    with pytest.raises(ApiProblem) as unknown:
        kernel.get_job(
            BrowserRequest(
                method="GET",
                path=f"/api/v1/jobs/{guessed_id}",
                host="127.0.0.1:8765",
                session_token=grant.session_token,
            ),
            guessed_id,
        )
    assert unknown.value.status_code == 404
    assert guessed_id not in unknown.value.problem.model_dump_json()


def test_projection_contract_is_closed_and_stale_actions_are_disabled() -> None:
    assert not _job(stale=True).actions[0].enabled

    payload = _job().model_dump(mode="json")
    payload["source_path"] = "/private/input"
    with pytest.raises(ValidationError, match="Extra inputs"):
        JobProjection.model_validate(payload)

    payload = _job().model_dump(mode="json")
    payload["stale"] = True
    with pytest.raises(ValidationError, match="stale projections"):
        JobProjection.model_validate(payload)
