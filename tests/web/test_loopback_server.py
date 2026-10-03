"""Real-socket regressions for the packaged B01 loopback adapter."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from evidence_inspector.models import sha256_bytes
from traceback_runner.contracts import InputKind, JobRequest
from traceback_runner.store import JobStore
from traceback_runner.web import server as server_module
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
    config = service.config
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


def _exchange(
    service: RunningLocalWebService, bootstrap_code: str | None = None
) -> tuple[str, str]:
    status, headers, content = _request(
        service,
        "POST",
        "/api/v1/session/bootstrap",
        headers={"Origin": service.base_url},
        payload={"bootstrap": bootstrap_code or service.bootstrap_code},
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
        assert service.config.port != 0
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

        cookie, csrf = _exchange(service, service.issue_bootstrap())
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

        config = service.config
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

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "instance.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "instance.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(first.anchor_path.stat().st_mode) == 0o600
    assert state.stat().st_uid == os.geteuid()
    saved = json.loads((state / "instance.json").read_bytes())
    assert saved["capability_enabled"] is False
    assert "bootstrap" not in saved and "session" not in saved
    with pytest.raises(LocalWebServerError, match="already running"):
        RunningLocalWebService.start(store=store, state_directory=state)

    first.close()
    first.close()
    assert not (state / "instance.json").exists()
    (state / "instance.json").write_bytes(b"stale truncated state")
    os.chmod(state / "instance.json", 0o600)

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

    insecure_parent = tmp_path / "insecure-parent"
    insecure_parent.mkdir(mode=0o700)
    os.chmod(insecure_parent, 0o770)
    with pytest.raises(LocalWebServerError, match="state parent"):
        RunningLocalWebService.start(
            store=store, state_directory=insecure_parent / "state"
        )


def test_state_directory_replacement_during_publication_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    displaced = tmp_path / "displaced-state"
    original = server_module._write_instance_state

    def replace_after_write(directory_fd: int, payload: dict[str, object]) -> None:
        original(directory_fd, payload)
        state.rename(displaced)
        state.mkdir(mode=0o700)

    monkeypatch.setattr(server_module, "_write_instance_state", replace_after_write)
    with pytest.raises(LocalWebServerError, match="identity changed"):
        RunningLocalWebService.start(store=store, state_directory=state)
    assert not (displaced / "instance.json").exists()
    assert not (state / "instance.json").exists()


def test_lock_entry_substitution_cannot_create_two_live_services(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    published = (state / "instance.json").read_bytes()
    replacement = state / ".replacement-lock"
    replacement.write_bytes(b"")
    os.chmod(replacement, 0o600)
    os.replace(replacement, state / "instance.lock")

    try:
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)

        deadline = time.monotonic() + 2
        while first.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.server.security_failed.is_set()
        assert not first.is_running
        assert (state / "instance.json").read_bytes() == published
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        first.close()

    with RunningLocalWebService.start(store=store, state_directory=state) as restarted:
        assert _request(restarted, "GET", "/")[0] == 200


def test_live_state_directory_replacement_remains_serialized_by_parent_anchor(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    displaced = tmp_path / "displaced-state"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    published = (state / "instance.json").read_bytes()
    state.rename(displaced)
    state.mkdir(mode=0o700)

    try:
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)

        deadline = time.monotonic() + 2
        while first.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.server.security_failed.is_set()
        assert not first.is_running
        assert (displaced / "instance.json").read_bytes() == published
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        first.close()

    with RunningLocalWebService.start(store=store, state_directory=state) as restarted:
        assert _request(restarted, "GET", "/")[0] == 200


def test_startup_anchor_substitution_is_detected_and_cannot_bypass_parent_lease(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    published = (state / "instance.json").read_bytes()
    anchor = server_module._RUNTIMES[first._runtime_id].startup_anchor
    anchor_path = anchor.parent_path / anchor.name
    replacement = anchor.parent_path / f"{anchor.name}.replacement"
    replacement.write_bytes(b"")
    os.chmod(replacement, 0o600)
    os.replace(replacement, anchor.parent_path / anchor.name)

    try:
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
        deadline = time.monotonic() + 2
        while first.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.server.security_failed.is_set()
        assert not first.is_running
        assert (state / "instance.json").read_bytes() == published
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        first.close()
        anchor_path.unlink(missing_ok=True)


def test_stable_lock_root_identity_substitution_is_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    with RunningLocalWebService.start(store=store, state_directory=state) as service:
        substitute = tmp_path / "substitute-lock-root"
        substitute.mkdir(mode=0o700)
        runtime = server_module._RUNTIMES[service._runtime_id]
        spoofed = replace(runtime.startup_anchor, parent_path=substitute)
        with pytest.raises(LocalWebServerError, match="startup parent identity"):
            server_module._require_startup_anchor(spoofed)


def test_startup_parent_substitution_fails_existing_service_closed(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    parent = tmp_path / "web-root"
    state = parent / "state"
    displaced = tmp_path / "displaced-web-root"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    published = (state / "instance.json").read_bytes()
    parent.rename(displaced)
    parent.mkdir(mode=0o700)
    state.mkdir(mode=0o700)

    try:
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
        deadline = time.monotonic() + 2
        while first.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.server.security_failed.is_set()
        assert not first.is_running
        assert (displaced / "state" / "instance.json").read_bytes() == published
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        first.close()

    with RunningLocalWebService.start(store=store, state_directory=state) as restarted:
        assert _request(restarted, "GET", "/")[0] == 200


def test_authority_rejected_before_body_validation_or_read(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        config = service.config
        connection = http.client.HTTPConnection(
            config.bind_host, config.port, timeout=1
        )
        connection.putrequest("POST", "/api/v1/session/bootstrap", skip_host=True)
        connection.putheader("Host", "evil.example")
        connection.putheader("Origin", service.base_url)
        connection.putheader("Content-Type", "text/plain")
        connection.putheader("Content-Length", "4096")
        connection.endheaders()
        assert connection.getresponse().status == 403
        connection.close()


@pytest.mark.parametrize(
    "cookie_header",
    [
        "traceback_session=one; traceback_session=two",
        'traceback_session="quoted"',
        "traceback_session=valid; malformed",
    ],
)
def test_ambiguous_session_cookies_are_rejected(
    tmp_path: Path, cookie_header: str
) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        status, _, _ = _request(
            service,
            "GET",
            "/api/v1/jobs",
            headers={"Cookie": cookie_header},
        )
        assert status == 401


def test_duplicate_cookie_headers_are_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        cookie, _ = _exchange(service)
        config = service.config
        connection = http.client.HTTPConnection(
            config.bind_host, config.port, timeout=3
        )
        connection.putrequest("GET", "/api/v1/jobs", skip_host=True)
        connection.putheader("Host", config.authority)
        connection.putheader("Cookie", cookie)
        connection.putheader("Cookie", cookie)
        connection.endheaders()
        assert connection.getresponse().status == 401
        connection.close()


def test_partial_request_flood_has_bounded_workers_and_no_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = _store(tmp_path)
    clients: list[socket.socket] = []
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        config = service.config
        for _ in range(server_module.MAX_HTTP_WORKERS * 3):
            # Linux drops a SYN while the listen backlog is full and resends it
            # after a 1 s initial RTO, so a 1 s connect timeout races the server
            # draining its backlog; 5 s allows one retransmit.
            client = socket.create_connection(
                (config.bind_host, config.port), timeout=5
            )
            client.sendall(b"GET / HTTP/1.1\r\nHost: ")
            clients.append(client)
        time.sleep(0.2)
        assert service.server.active_workers <= server_module.MAX_HTTP_WORKERS
        for client in clients:
            client.close()
        time.sleep(0.2)
        assert _request(service, "GET", "/")[0] == 200
    assert "Traceback" not in capsys.readouterr().err


def test_header_and_body_limits_fail_without_reading_oversized_body(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        config = service.config
        client = socket.create_connection((config.bind_host, config.port), timeout=1)
        headers = "".join(f"X-Padding-{index}: x\r\n" for index in range(33))
        client.sendall(
            f"GET / HTTP/1.1\r\nHost: {config.authority}\r\n{headers}\r\n".encode()
        )
        assert b" 431 " in client.recv(1024)
        client.close()

        connection = http.client.HTTPConnection(
            config.bind_host, config.port, timeout=1
        )
        connection.putrequest("POST", "/api/v1/session/bootstrap", skip_host=True)
        connection.putheader("Host", config.authority)
        connection.putheader("Origin", service.base_url)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(server_module.MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        assert connection.getresponse().status == 400
        connection.close()


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
        assert service.config.bind_host == "::1"
        assert service.config.authority.startswith("[::1]:")
        assert _request(service, "GET", "/")[0] == 200


def test_service_store_survives_another_store_closing_last(tmp_path: Path) -> None:
    """A runner process opening and closing the same job store while the web
    service runs must not invalidate the service store's pinned WAL/SHM
    identities: the service holds a journal anchor for its lifetime."""

    import gc

    store, job_id = _store(tmp_path)
    store.get(job_id.removeprefix("job_"))  # the service store reads (and pins) first
    wal = Path(f"{store.path}-wal")
    with RunningLocalWebService.start(
        store=store, state_directory=tmp_path / "state"
    ) as service:
        before = wal.stat().st_ino
        other = JobStore(store.path)  # e.g. `traceback run` or `pause`
        other.get(job_id.removeprefix("job_"))
        del other
        gc.collect()  # connections close on collection; force the "last close"
        assert wal.exists() and wal.stat().st_ino == before
        cookie, _ = _exchange(service)
        status, _, content = _request(
            service, "GET", "/api/v1/jobs", headers={"Cookie": cookie}
        )
        assert status == 200
        assert [item["job_id"] for item in json.loads(content)["jobs"]] == [job_id]
    gc.collect()
    # The anchor is released with the service.
    assert not wal.exists()


def test_failed_start_releases_the_journal_anchor_even_if_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If start() fails after taking the journal anchor, the anchor is
    released even when an earlier cleanup step itself raises."""

    import gc

    store, job_id = _store(tmp_path)
    store.get(job_id.removeprefix("job_"))
    wal = Path(f"{store.path}-wal")

    def fail_start(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected start failure")

    real_close_startup_anchor = server_module._close_startup_anchor

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        # Release the real host-wide startup lock first, so later tests can
        # start a service, then fail as a broken cleanup step would.
        real_close_startup_anchor(*args, **kwargs)
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(server_module, "_RunningLocalWebRuntime", fail_start)
    monkeypatch.setattr(server_module, "_close_startup_anchor", fail_cleanup)
    with pytest.raises(OSError, match="injected cleanup failure") as excinfo:
        RunningLocalWebService.start(store=store, state_directory=tmp_path / "state")
    # Keep the store and the traceback (which holds start()'s frame) alive, so
    # only an explicit anchor close, not garbage collection, can drop the WAL.
    gc.collect()
    assert excinfo.value is not None and store.path
    assert not wal.exists()
