"""Real-socket regressions for the packaged B01 loopback adapter."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import stat
from pathlib import Path

import pytest

from evidence_inspector.models import sha256_bytes
from traceback_runner.contracts import InputKind, JobRequest
from traceback_runner.store import JobStore
from traceback_runner.web.server import LocalWebServerError, RunningLocalWebService


def _store(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(tmp_path / "runner" / "jobs.sqlite3")
    record = store.submit(
        JobRequest(
            sample_token="sample.synthetic",
            input_kind=InputKind.MODBAM,
            input_tree_sha256_local=sha256_bytes(b"synthetic-input"),
            workflow_release_sha256=sha256_bytes(b"synthetic-release"),
            execution_options={"offline": True, "threads": 1},
        )
    )
    return store, f"job_{record.job_id}"


def _request(
    service: RunningLocalWebService,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    config = service.boundary.config
    connection = http.client.HTTPConnection(config.bind_host, config.port, timeout=3)
    body = json.dumps(payload, separators=(",", ":")).encode() if payload else None
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", config.authority)
    if body is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    content = response.read()
    response_headers = {name.casefold(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, content


def _exchange(service: RunningLocalWebService) -> tuple[str, str]:
    status, headers, content = _request(
        service,
        "POST",
        "/api/v1/session/bootstrap",
        headers={"Origin": service.base_url},
        payload={"bootstrap": service.bootstrap_code},
    )
    assert status == 200
    cookie = headers["set-cookie"].split(";", 1)[0]
    csrf = json.loads(content)["csrf_token"]
    return cookie, csrf


def test_real_http_fragment_bootstrap_store_projection_and_offline_assets(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    state = tmp_path / "web-state"
    with RunningLocalWebService.start(store=store, state_directory=state) as service:
        assert service.boundary.config.port != 0
        assert service.launch_url.startswith(f"{service.base_url}/#bootstrap=")
        assert "?" not in service.launch_url

        status, headers, index = _request(service, "GET", "/")
        assert status == 200
        assert headers["content-security-policy"].startswith("default-src 'self'")
        status, _, script = _request(service, "GET", "/assets/app.js")
        assert status == 200
        assert b"history.replaceState" in script
        assert b"localStorage" not in script
        for content in (index, script):
            assert b"http://" not in content.lower()
            assert b"https://" not in content.lower()
            assert b"//cdn" not in content.lower()

        cookie, _ = _exchange(service)
        status, _, content = _request(
            service,
            "GET",
            "/api/v1/jobs",
            headers={"Cookie": cookie},
        )
        assert status == 200
        jobs = json.loads(content)["jobs"]
        assert [item["job_id"] for item in jobs] == [job_id]
        assert str(store.path) not in content.decode()


def test_real_http_exact_authority_session_csrf_forwarding_and_guessed_ids(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        status, _, _ = _request(
            service,
            "POST",
            "/api/v1/session/bootstrap",
            payload={"bootstrap": service.bootstrap_code},
        )
        assert status == 403

        service.bootstrap_code = service.boundary.issue_bootstrap()
        cookie, csrf = _exchange(service)
        common = {"Cookie": cookie, "Origin": service.base_url}

        status, _, _ = _request(
            service,
            "POST",
            "/api/v1/session/validate",
            headers={**common, "X-Traceback-CSRF": "wrong"},
        )
        assert status == 403
        status, _, content = _request(
            service,
            "POST",
            "/api/v1/session/validate",
            headers={**common, "X-Traceback-CSRF": csrf},
        )
        assert (status, json.loads(content)) == (200, {"authorized": True})

        status, _, _ = _request(
            service,
            "GET",
            "/api/v1/jobs",
            headers={"Cookie": cookie, "X-Forwarded-Port": "443"},
        )
        assert status == 403

        guessed = "/api/v1/jobs/job_ffffffffffffffffffffffffffffffff"
        unauthenticated, _, unauthenticated_body = _request(service, "GET", guessed)
        authenticated, _, authenticated_body = _request(
            service, "GET", guessed, headers={"Cookie": cookie}
        )
        assert unauthenticated == 401
        assert authenticated == 404
        assert b"ffffffff" not in unauthenticated_body
        assert b"ffffffff" not in authenticated_body

        config = service.boundary.config
        connection = http.client.HTTPConnection(
            config.bind_host, config.port, timeout=3
        )
        connection.putrequest("GET", "/api/v1/jobs", skip_host=True)
        connection.putheader("Host", "localhost")
        connection.putheader("Cookie", cookie)
        connection.endheaders()
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection(
            config.bind_host, config.port, timeout=3
        )
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        assert connection.getresponse().status == 403
        connection.close()


def test_state_permissions_restart_rotation_and_explicit_cross_user_limit(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    first_cookie, _ = _exchange(first)
    first_instance = first.instance_id
    first.close()

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "instance.json").stat().st_mode) == 0o600
    assert state.stat().st_uid == os.geteuid()
    saved = json.loads((state / "instance.json").read_bytes())
    assert saved["capability_enabled"] is False
    assert "bootstrap" not in saved and "session" not in saved

    with RunningLocalWebService.start(store=store, state_directory=state) as second:
        assert second.instance_id != first_instance
        status, _, _ = _request(
            second,
            "GET",
            "/api/v1/jobs",
            headers={"Cookie": first_cookie},
        )
        assert status == 401

    insecure = tmp_path / "insecure-state"
    insecure.mkdir(mode=0o755)
    with pytest.raises(LocalWebServerError, match="0700"):
        RunningLocalWebService.start(store=store, state_directory=insecure)


def test_localhost_operation_survives_external_egress_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = _store(tmp_path)
    original_connect = socket.socket.connect
    observed: list[tuple[str, int]] = []

    def loopback_only(instance: socket.socket, address: object) -> object:
        host, port = address[0], address[1]  # type: ignore[index]
        if not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("external egress denied by synthetic host policy")
        observed.append((host, port))
        return original_connect(instance, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        assert _request(service, "GET", "/")[0] == 200
        cookie, _ = _exchange(service)
        assert (
            _request(service, "GET", "/api/v1/jobs", headers={"Cookie": cookie})[0]
            == 200
        )
        probe = socket.socket()
        with pytest.raises(RuntimeError, match="external egress denied"):
            probe.connect(("192.0.2.1", 443))
        probe.close()
    assert observed
    assert all(ipaddress.ip_address(host).is_loopback for host, _ in observed)


def test_ipv6_listener_is_family_bound_when_available(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    try:
        service = RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state-v6",
            ipv6=True,
        )
    except LocalWebServerError:
        pytest.skip("IPv6 loopback is unavailable on this host")
    with service:
        assert service.boundary.config.bind_host == "::1"
        assert service.boundary.config.authority.startswith("[::1]:")
        assert _request(service, "GET", "/")[0] == 200
