"""Reader launch exchange over real loopback HTTP and the own-grant session check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as registry_module
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.reader_authorization_registry import (
    ReaderAuthorizationDenied,
    ReaderDenialReason,
    ReaderKeyStatus,
    ReaderRevocationReason,
    reader_trust_sha256,
)
from evidence_inspector.reader_authorization_synthetic import synthetic_reader_trust
from tests.test_reader_authorization_registry import (
    COHORT,
    NOW,
    OTHER_COHORT,
    OTHER_SELECTOR,
    SCOPE,
    SELECTOR,
    create_registry,
    grant_for,
)
from tests.web.test_loopback_server import _exchange, _request, _store
from traceback_runner.web import server as server_module
from traceback_runner.web.auth import BrowserRequest
from traceback_runner.web.server import LocalWebServerError, RunningLocalWebService

ROUTE = "/api/v1/session/reader-launch"
THIRD_SELECTOR = "reader_grant_" + "e" * 32


@pytest.fixture(autouse=True)
def fresh_profile_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_PROCESS_PROFILE", {})


@pytest.fixture
def registry(tmp_path: Path):
    created = create_registry(
        tmp_path / "reader", clock=AuthorityTimeSource.fixed(NOW)
    )
    created.add_grant(grant_for(created))
    try:
        yield created
    finally:
        created.close()


@pytest.fixture
def service(tmp_path: Path, registry):
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state", reader_registry=registry
    ) as running:
        yield running


def _fragment(link: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in link.split("#", 1)[1].split("&"))


def _launch(service, cookie: str, csrf: str | None, credential: str, **extra):
    headers = {"Origin": service.base_url, "Cookie": cookie}
    if csrf is not None:
        headers["X-Traceback-CSRF"] = csrf
    headers.update(extra)
    return _request(service, "POST", ROUTE, headers=headers, payload={"launch": credential})


def _follow(service, link: str) -> tuple[str, str]:
    values = _fragment(link)
    cookie, csrf = _exchange(service, values["bootstrap"])
    status, _, content = _launch(service, cookie, csrf, values["reader_launch"])
    assert status == 200, content
    assert json.loads(content) == {"reader_bound": True}
    return cookie, values["reader_launch"]


def _authorize(service, cookie: str, *, cohort: str = COHORT):
    binder = server_module._RUNTIMES[service._runtime_id].reader
    token = cookie.split("=", 1)[1]
    request = BrowserRequest(
        method="GET",
        path="/api/v1/longitudinal/workspace",
        host=service.config.authority,
        session_token=token,
    )
    with binder.reader_authorization(
        request, cohort_registry_id=cohort, measurement_scope=SCOPE
    ) as authorization:
        return authorization


def _denied(service, cookie: str, reason: ReaderDenialReason) -> None:
    with pytest.raises(ReaderAuthorizationDenied) as info:
        _authorize(service, cookie)
    assert info.value.reason is reason


def test_launch_link_is_fragment_only_and_binds_through_post(service) -> None:
    link = service.issue_reader_launch_url(SELECTOR)
    assert link.startswith(f"{service.base_url}/#bootstrap=")
    assert "?" not in link
    assert link.index("reader_launch=") > link.index("#")
    cookie, credential = _follow(service, link)
    assert _authorize(service, cookie).cohort_registry_id == COHORT
    # One use: a replay on another link's reader session is denied.
    other = _fragment(service.issue_reader_launch_url(SELECTOR))
    other_cookie, other_csrf = _exchange(service, other["bootstrap"])
    status, headers, content = _launch(service, other_cookie, other_csrf, credential)
    assert status == 403
    assert json.loads(content) == {"error": {"code": "permission_denied"}}
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    # H1: an operator session never reaches the reader launch route.
    operator_cookie, operator_csrf = _exchange(service, service.issue_bootstrap())
    status, _, content = _launch(service, operator_cookie, operator_csrf, credential)
    assert (status, json.loads(content)) == (
        403,
        {"error": {"code": "TBX-AUTH-007"}},
    )


def test_exchange_route_keeps_full_b01_checks(service) -> None:
    link = service.issue_reader_launch_url(SELECTOR)
    values = _fragment(link)
    cookie, csrf = _exchange(service, values["bootstrap"])
    credential = values["reader_launch"]
    status, _, content = _launch(service, cookie, None, credential)
    assert (status, json.loads(content)["error"]["code"]) == (403, "TBX-AUTH-002")
    status, _, content = _request(
        service,
        "POST",
        ROUTE,
        headers={"Cookie": cookie, "X-Traceback-CSRF": csrf},
        payload={"launch": credential},
    )
    assert (status, json.loads(content)["error"]["code"]) == (403, "TBX-AUTH-003")
    status, _, content = _launch(
        service, cookie, csrf, credential, Origin="http://localhost:1"
    )
    assert status == 403
    status, _, _ = _request(
        service,
        "POST",
        ROUTE,
        headers={"Origin": service.base_url},
        payload={"launch": credential},
    )
    assert status == 401
    status, _, _ = _request(
        service, "GET", ROUTE, headers={"Cookie": cookie}
    )
    assert status == 404
    status, _, _ = _request(
        service,
        "POST",
        f"{ROUTE}?launch={credential}",
        headers={"Origin": service.base_url, "Cookie": cookie, "X-Traceback-CSRF": csrf},
        payload={"launch": credential},
    )
    assert status == 404
    status, _, _ = _launch(service, cookie, csrf, credential, Forwarded="for=1.2.3.4")
    assert status == 403
    # None of the rejected transports consumed the credential.
    status, _, _ = _launch(service, cookie, csrf, credential)
    assert status == 200


def test_route_is_absent_without_a_reader_registry(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as running:
        with pytest.raises(LocalWebServerError, match="not configured"):
            running.issue_reader_launch_url(SELECTOR)
        # No reader session can exist without a registry, and an operator
        # session never reaches the reader launch route (H1).
        cookie, csrf = _exchange(running)
        status, _, content = _launch(running, cookie, csrf, "A" * 43)
        assert (status, json.loads(content)["error"]["code"]) == (403, "TBX-AUTH-007")


def test_credential_never_reaches_server_output(
    service, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    capfd.readouterr()
    cookie, credential = _follow(service, service.issue_reader_launch_url(SELECTOR))
    captured = capfd.readouterr()
    assert credential not in captured.out + captured.err
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert credential.encode() not in path.read_bytes()


def test_unrelated_registry_changes_keep_the_session_and_its_own_end_it(
    service, registry
) -> None:
    cookie, _ = _follow(service, service.issue_reader_launch_url(SELECTOR))
    _authorize(service, cookie)
    registry.add_grant(
        grant_for(registry, selector=OTHER_SELECTOR, cohorts=(OTHER_COHORT,))
    )
    _authorize(service, cookie)
    registry.revoke_grant(
        OTHER_SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST
    )
    _authorize(service, cookie)
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    _authorize(service, cookie)
    with pytest.raises(ReaderAuthorizationDenied) as info:
        _authorize(service, cookie, cohort=OTHER_COHORT)
    assert info.value.reason is ReaderDenialReason.SCOPE_MISMATCH
    registry.revoke_grant(SELECTOR, reason=ReaderRevocationReason.OPERATOR_REQUEST)
    _denied(service, cookie, ReaderDenialReason.GRANT_REVOKED)


def test_rotating_out_the_signing_key_ends_the_session(service, registry) -> None:
    cookie, _ = _follow(service, service.issue_reader_launch_url(SELECTOR))
    first = synthetic_reader_trust()
    second = synthetic_reader_trust(revision=2, previous=first)
    registry.rotate_trust(second, expected_trust_sha256=reader_trust_sha256(second))
    registry.add_grant(grant_for(registry, selector=THIRD_SELECTOR, key_version=2))
    other, _ = _follow(service, service.issue_reader_launch_url(THIRD_SELECTOR))
    third = synthetic_reader_trust(
        revision=3, previous=second, statuses={1: ReaderKeyStatus.REVOKED}
    )
    registry.rotate_trust(third, expected_trust_sha256=reader_trust_sha256(third))
    _denied(service, cookie, ReaderDenialReason.UNTRUSTED_KEY)
    # The grant signed by the new key keeps its session.
    _authorize(service, other)
