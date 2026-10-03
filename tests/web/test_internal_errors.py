"""H6-min: the dispatcher catch, its counter, and the watchdog's runtime removal.

Security spec: docs/PILOT-SECURITY-HARDENING.md § H6 as reduced by the gate
decisions (no diagnostics ring) and the Eng review's corrected criteria 23-25.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

import evidence_inspector.longitudinal_workspace as workspace_module
import evidence_inspector.reader_authorization_registry as reader_module
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.result_view_source_registry import ResultViewSourceRegistry
from tests.test_reader_authorization_registry import (
    NOW,
    SELECTOR,
    create_registry,
    grant_for,
)
from tests.web.longitudinal_env import Env, _http, fresh_env
from tests.web.test_loopback_server import _exchange, _request, _store
from traceback_runner.web import server as server_module
from traceback_runner.web.server import LocalWebServerStopped, RunningLocalWebService

INTERNAL = {"error": {"code": "TBX-INTERNAL"}}
SECRET = "result_" + "a" * 40


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as running:
        yield running


def _replace_handler(monkeypatch, key: tuple[str, str], handler) -> None:
    routes = dict(server_module._ROUTES)
    routes[key] = dataclasses.replace(routes[key], handler=handler)
    monkeypatch.setattr(server_module, "_ROUTES", routes)


def test_unexpected_handler_error_is_one_counted_bounded_500(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 23: a handler raising RuntimeError (test hook) gives the
    bounded 500 problem and one counter entry that holds no path or text."""

    def explode(*args) -> None:
        raise RuntimeError(f"/api/v1/jobs/{SECRET} detail")

    _replace_handler(monkeypatch, ("GET", "/api/v1/jobs"), explode)
    cookie, _ = _exchange(service)
    status, headers, content = _request(
        service, "GET", "/api/v1/jobs", headers={"Cookie": cookie}
    )
    assert (status, json.loads(content)) == (500, INTERNAL)
    assert headers["cache-control"] == "no-store"
    assert SECRET.encode() not in content
    assert service.internal_errors == {"TBX-INTERNAL": 1}
    assert SECRET not in repr(service.internal_errors)
    # The server keeps serving.
    assert service.is_running
    assert _request(service, "GET", "/")[0] == 200
    status, _, _ = _request(service, "GET", "/api/v1/jobs", headers={"Cookie": cookie})
    assert status == 500
    assert service.internal_errors == {"TBX-INTERNAL": 2}


def test_error_after_the_response_started_only_closes_and_counts(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    def half(handler, *args) -> None:
        handler._send(200, "application/json; charset=utf-8", b'{"jobs":[]}\n')
        raise AttributeError("late")

    _replace_handler(monkeypatch, ("GET", "/api/v1/jobs"), half)
    cookie, _ = _exchange(service)
    status, _, content = _request(service, "GET", "/api/v1/jobs", headers={"Cookie": cookie})
    assert (status, content) == (200, b'{"jobs":[]}\n')
    assert service.internal_errors == {"TBX-INTERNAL": 1}


def test_no_internal_error_is_counted_for_ordinary_denials(service) -> None:
    for path in ("/api/v1/jobs", "/nope", "/api/v1/jobs/job_" + "0" * 32):
        _request(service, "GET", path)
    cookie, _ = _exchange(service)
    _request(service, "GET", "/api/v1/explorer/catalog?x=1", headers={"Cookie": cookie})
    assert service.internal_errors == {}


# --- watchdog: a stopped service is no longer a running runtime -----------------------


@pytest.fixture
def reader_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    registry = create_registry(tmp_path / "reader", clock=AuthorityTimeSource.fixed(NOW))
    registry.add_grant(grant_for(registry))
    store, _ = _store(tmp_path)
    try:
        running = RunningLocalWebService.start(
            store=store, state_directory=tmp_path / "state", reader_registry=registry
        )
        try:
            yield running, store, tmp_path / "state", registry
        finally:
            running.close()
    finally:
        registry.close()


def _trip(service: RunningLocalWebService) -> None:
    server_module._RUNTIMES[service._runtime_id].server.security_failed.set()
    deadline = time.monotonic() + 5
    while service._runtime_id in server_module._RUNTIMES and time.monotonic() < deadline:
        time.sleep(0.01)


def test_watchdog_shutdown_removes_the_runtime_and_stops_link_issuance(
    reader_service,
) -> None:
    service, store, state, registry = reader_service
    service.issue_reader_launch_url(SELECTOR)
    _trip(service)
    assert service._runtime_id not in server_module._RUNTIMES
    assert service._runtime_id in server_module._STOPPED_RUNTIMES
    assert not service.is_running
    assert service.server.security_failed.is_set()
    with pytest.raises(LocalWebServerStopped):
        service.issue_reader_launch_url(SELECTOR)
    with pytest.raises(LocalWebServerStopped):
        service.issue_bootstrap()
    # A stopped service keeps its lease until it is closed (fail closed) ...
    from traceback_runner.web.server import LocalWebServerError

    with pytest.raises(LocalWebServerError, match="already running"):
        RunningLocalWebService.start(
            store=store, state_directory=state, reader_registry=registry
        )
    # ... and close() still releases it.
    service.close()
    assert service._runtime_id not in server_module._STOPPED_RUNTIMES
    with RunningLocalWebService.start(
        store=store, state_directory=state, reader_registry=registry
    ) as restarted:
        assert restarted.issue_reader_launch_url(SELECTOR).startswith(restarted.base_url)


# --- the longitudinal route and a pre-existing dropped connection ----------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yield from fresh_env(tmp_path, monkeypatch)


def test_d08_programming_error_reaches_the_page_as_tbx_internal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 25 through the route: a store call raising AttributeError
    inside the D08 build is ``internal_error`` in D08 and a counted 500
    ``TBX-INTERNAL`` at the route, not ``integrity_failure``."""

    original = workspace_module._call

    def broken(store, cls, name, *args, **kwargs):
        if (cls, name) == (ResultViewSourceRegistry, "resolve"):
            raise AttributeError(SECRET)
        return original(store, cls, name, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_call", broken)
    status, content = env.post("workspace", {"request": env.request_json})
    assert (status, json.loads(content)) == (500, INTERNAL)
    assert env.service.internal_errors == {"TBX-INTERNAL": 1}


def test_tripped_watchdog_stops_issuance_before_the_runtime_is_retired(
    reader_service,
) -> None:
    """No window in which a tripped service still issues a (dead) link."""

    service = reader_service[0]
    server_module._RUNTIMES[service._runtime_id].server.security_failed.set()
    with pytest.raises(LocalWebServerStopped):
        service.issue_reader_launch_url(SELECTOR)
    with pytest.raises(LocalWebServerStopped):
        service.issue_bootstrap()


def test_a_kept_alive_connection_gets_a_bounded_500_on_each_request(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response-started flag resets per request on one connection."""

    import http.client

    cookie, _ = _exchange(service)
    config = service.config
    connection = http.client.HTTPConnection(config.bind_host, config.port, timeout=3)

    def get():
        connection.putrequest("GET", "/api/v1/jobs", skip_host=True)
        connection.putheader("Host", config.authority)
        connection.putheader("Cookie", cookie)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()

    def explode(*args) -> None:
        raise RuntimeError("boom")

    try:
        assert get()[0] == 200
        _replace_handler(monkeypatch, ("GET", "/api/v1/jobs"), explode)
        status, content = get()
        assert (status, json.loads(content)) == (500, INTERNAL)
    finally:
        connection.close()
    assert service.internal_errors == {"TBX-INTERNAL": 1}


@pytest.mark.parametrize("where", ["rebuild", "candidate_page"])
def test_reopen_never_shows_a_programming_error_as_a_stale_reopen(
    env: Env, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    import traceback_runner.web.longitudinal as web_module
    from evidence_inspector.anchor_policy_registry import AnchorPolicyRegistry

    status, content = env.post("save", {"request": env.request_json})
    assert status == 200, content
    receipt = json.loads(content)
    if where == "rebuild":
        original = workspace_module._call

        def broken(store, cls, name, *args, **kwargs):
            if (cls, name) == (ResultViewSourceRegistry, "resolve"):
                raise AttributeError(SECRET)
            return original(store, cls, name, *args, **kwargs)

        monkeypatch.setattr(workspace_module, "_call", broken)
    else:
        original_web = web_module._call

        def broken_web(store, cls, name, *args, **kwargs):
            if (cls, name) == (AnchorPolicyRegistry, "derive_candidate_page"):
                raise AttributeError(SECRET)
            return original_web(store, cls, name, *args, **kwargs)

        monkeypatch.setattr(web_module, "_call", broken_web)
    status, content = env.post(
        "reopen",
        {
            "saved_selector_id": receipt["saved_selector_id"],
            "comparison_version": receipt["comparison_version"],
            "stage": "results",
        },
    )
    assert (status, json.loads(content)) == (500, INTERNAL)
    assert env.service.internal_errors == {"TBX-INTERNAL": 1}


def test_unknown_comparison_ids_no_longer_drop_the_connection(env: Env) -> None:
    """Before H6 an unmapped KeyError in the compare route dropped the
    connection with no response; now it is the bounded, counted 500."""

    cookie, _ = env.operator()
    path = (
        "/api/v1/explorer/compare?left=result_" + "0" * 40 + "&right=result_" + "1" * 40
    )
    status, content = _http(env.service, "GET", path, {"Cookie": cookie})
    assert (status, json.loads(content)) == (500, INTERNAL)
    assert env.service.internal_errors == {"TBX-INTERNAL": 1}
