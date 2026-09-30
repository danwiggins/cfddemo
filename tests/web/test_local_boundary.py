"""B01 localhost threat-boundary and frozen-contract regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import count

import pytest
from pydantic import ValidationError

from traceback_runner.contracts import JobState
from traceback_runner.web.api import ApiProblem, LocalApiKernel
from traceback_runner.web.auth import (
    BootstrapBroker,
    BoundaryDenied,
    BrowserRequest,
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
        allow_test_token_factory=True,
    )
    return LocalWebBoundary(build_loopback_config(port=8765), broker), broker


def _grant(boundary: LocalWebBoundary, broker: BootstrapBroker):
    code = boundary.issue_bootstrap()
    request = BrowserRequest(
        method="POST",
        path="/api/v1/session/bootstrap",
        host="127.0.0.1:8765",
        origin="http://127.0.0.1:8765",
    )
    return boundary.exchange_bootstrap(request, code), code


def _exchange_request(*, ipv6: bool = False) -> BrowserRequest:
    authority = "[::1]:8765" if ipv6 else "127.0.0.1:8765"
    return BrowserRequest(
        method="POST",
        path="/api/v1/session/bootstrap",
        host=authority,
        origin=f"http://{authority}",
    )


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


def test_bootstrap_exchange_is_atomic_under_concurrency() -> None:
    boundary, _ = _boundary()
    code = boundary.issue_bootstrap()

    def exchange() -> str:
        try:
            boundary.exchange_bootstrap(_exchange_request(), code)
        except BoundaryDenied:
            return "denied"
        return "granted"

    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = tuple(executor.map(lambda _: exchange(), range(64)))
    assert outcomes.count("granted") == 1
    assert outcomes.count("denied") == 63


def test_credential_strength_ttl_attempts_and_session_count_are_bounded() -> None:
    with pytest.raises(ValueError, match="test-only"):
        BootstrapBroker(token_factory=lambda: "credential-" + "0" * 64)
    weak = BootstrapBroker(
        token_factory=lambda: "short",
        allow_test_token_factory=True,
        token_attempt_limit=2,
    )
    with pytest.raises(RuntimeError, match="strong unique credential"):
        weak.issue_bootstrap(authority="127.0.0.1:8765")

    for kwargs in (
        {"bootstrap_ttl_seconds": 301},
        {"session_ttl_seconds": 86_401},
        {"max_active_sessions": 257},
        {"token_attempt_limit": 17},
    ):
        with pytest.raises(ValueError):
            BootstrapBroker(**kwargs)

    tokens = count()
    broker = BootstrapBroker(
        now=lambda: 100.0,
        token_factory=lambda: f"credential-{next(tokens):064d}",
        allow_test_token_factory=True,
        max_active_sessions=1,
    )
    boundary = LocalWebBoundary(build_loopback_config(port=8765), broker)
    first = boundary.issue_bootstrap()
    boundary.exchange_bootstrap(_exchange_request(), first)
    second = boundary.issue_bootstrap()
    with pytest.raises(BoundaryDenied) as capacity:
        boundary.exchange_bootstrap(_exchange_request(), second)
    assert capacity.value.code == "TBX-AUTH-004"


def test_missing_session_cross_origin_host_and_csrf_fail_closed() -> None:
    boundary, broker = _boundary()
    grant, _ = _grant(boundary, broker)

    with pytest.raises(BoundaryDenied) as missing:
        boundary.authorize(
            BrowserRequest(method="GET", path="/api/v1/jobs", host="127.0.0.1:8765")
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


def test_exact_authority_forwarded_headers_and_address_family_fail_closed() -> None:
    for payload in (
        {
            "bind_host": "127.0.0.1",
            "port": 8765,
            "allowed_host_headers": ("evil.example:8765",),
            "allowed_origins": ("http://127.0.0.1:8765",),
        },
        {
            "bind_host": "127.0.0.1",
            "port": 8765,
            "allowed_host_headers": ("127.0.0.1:8765",),
            "allowed_origins": ("http://user@127.0.0.1:8765",),
        },
    ):
        with pytest.raises(ValidationError, match="exactly match"):
            LoopbackServerConfig.model_validate(payload)

    boundary, broker = _boundary()
    grant, _ = _grant(boundary, broker)
    with pytest.raises(BoundaryDenied) as forwarded:
        boundary.authorize(
            BrowserRequest(
                method="GET",
                path="/api/v1/jobs",
                host="127.0.0.1:8765",
                session_token=grant.session_token,
                forwarded_headers=("Forwarded",),
            )
        )
    assert forwarded.value.code == "TBX-AUTH-003"

    ipv6_boundary = LocalWebBoundary(
        build_loopback_config(port=8765, ipv6=True), broker
    )
    with pytest.raises(BoundaryDenied) as family:
        ipv6_boundary.authorize(
            BrowserRequest(
                method="GET",
                path="/api/v1/jobs",
                host="[::1]:8765",
                session_token=grant.session_token,
            )
        )
    assert family.value.code == "TBX-AUTH-001"


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


@pytest.mark.parametrize(
    "field,value",
    (
        ("problem", "Review https://evil.example/private"),
        ("cause", "/Volumes/provider/raw-input.bam"),
        ("fix", "ACGTACGTACGTACGTACGTACGTACGTACGT"),
        ("preserved_work", "550e8400-e29b-41d4-a716-446655440000"),
        ("repeated_work", "token=super-secret-value"),
    ),
)
def test_problem_response_rejects_every_reviewed_privacy_class(
    field: str, value: str
) -> None:
    payload = _problem().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        ProblemDetail.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    (
        "docs/../../etc/passwd.md",
        "docs/operator/../private.md",
        "docs/operator/help.md?next=https://evil.example",
        "docs\\operator\\help.md",
    ),
)
def test_problem_docs_path_is_bounded_to_bundled_markdown(path: str) -> None:
    payload = _problem().model_dump(mode="json")
    payload["docs_path"] = path
    with pytest.raises(ValidationError, match="documentation path|String should match"):
        ProblemDetail.model_validate(payload)
