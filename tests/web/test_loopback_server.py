"""Real-socket regressions for the packaged B01 loopback adapter."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import fcntl
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
        # Release the real per-state-directory startup anchor first, then fail
        # as a broken cleanup step would.
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


# --- A3: per-state-directory single-instance anchor -------------------------

_HOLDER_SCRIPT = """
import sys
from pathlib import Path
from traceback_runner.store import JobStore
from traceback_runner.web.server import LocalWebServerError, RunningLocalWebService

root = Path(sys.argv[1])
store = JobStore(root / "runner" / "jobs.sqlite3")
print("imported", flush=True)
if sys.stdin.readline().strip() != "go":
    sys.exit(0)
try:
    service = RunningLocalWebService.start(store=store, state_directory=root / "state")
except LocalWebServerError as exc:
    print(f"refused: {exc}", flush=True)
    sys.exit(3)
print("ready", flush=True)
sys.stdin.readline()
service.close()
"""


def _spawn_holder(root: Path) -> subprocess.Popen[str]:
    """Spawn a process that imports the server, then starts on ``go``."""

    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )


def _go(process: subprocess.Popen[str]) -> str:
    assert _await_line(process) == "imported"
    assert process.stdin is not None
    process.stdin.write("go\n")
    process.stdin.flush()
    return _await_line(process)


def _await_line(process: subprocess.Popen[str]) -> str:
    stdout = process.stdout
    assert stdout is not None
    result: list[str] = []
    reader = threading.Thread(target=lambda: result.append(stdout.readline()))
    reader.daemon = True
    reader.start()
    reader.join(timeout=60)
    if not result:
        process.kill()
        pytest.fail("holder process did not report within 60 s")
    return result[0].strip()


def _stop_holder(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def _concurrent_starts(
    roots: list[Path], stores_root: Path
) -> Iterator[tuple[list[RunningLocalWebService], list[BaseException]]]:
    """Start one service per root at once; each thread later closes its own.

    The store's journal anchor is a SQLite connection bound to the starting
    thread, so a service must be closed on the thread that started it.  Each
    start gets its own job store: concurrent first opens of one SQLite file
    race on its WAL sidecars, which is a job-store concern, not the web lock's.
    """

    stores = [
        _store(stores_root / f"store-{index:02d}")[0] for index in range(len(roots))
    ]

    barrier = threading.Barrier(len(roots))
    release = threading.Event()
    settled = threading.Semaphore(0)
    started: list[RunningLocalWebService] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def start(root: Path, store: JobStore) -> None:
        try:
            barrier.wait(timeout=10)
            service = RunningLocalWebService.start(store=store, state_directory=root)
        except BaseException as exc:  # collected for assertion
            with guard:
                failures.append(exc)
            settled.release()
            return
        with guard:
            started.append(service)
        settled.release()
        release.wait(timeout=120)
        service.close()

    threads = [
        threading.Thread(target=start, args=(root, store))
        for root, store in zip(roots, stores, strict=True)
    ]
    for thread in threads:
        thread.start()
    try:
        for _ in threads:
            assert settled.acquire(timeout=60)
        yield started, failures
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert not any(service.is_running for service in started)


def test_distinct_state_directories_run_concurrently_in_one_process(
    tmp_path: Path,
) -> None:
    with _concurrent_starts(
        [tmp_path / "root-a" / "state", tmp_path / "root-b" / "state"], tmp_path
    ) as (started, failures):
        assert failures == []
        assert len(started) == 2
        assert started[0].anchor_path != started[1].anchor_path
        for service in started:
            assert service.is_running
            assert _request(service, "GET", "/")[0] == 200


def test_sixteen_concurrent_starts_on_distinct_roots_all_succeed(
    tmp_path: Path,
) -> None:
    roots = [tmp_path / f"root-{index:02d}" / "state" for index in range(16)]
    with _concurrent_starts(roots, tmp_path) as (started, failures):
        assert failures == []
        assert len(started) == 16
        assert len({service.anchor_path for service in started}) == 16


def test_concurrent_starts_on_one_root_admit_exactly_one(tmp_path: Path) -> None:
    with _concurrent_starts([tmp_path / "state"] * 8, tmp_path) as (started, failures):
        assert len(started) == 1
        assert len(failures) == 7
        assert all(
            isinstance(exc, LocalWebServerError) and "already running" in str(exc)
            for exc in failures
        )


def test_distinct_roots_run_in_two_processes_and_one_root_is_refused(
    tmp_path: Path,
) -> None:
    store, _ = _store(tmp_path)
    holder_root = tmp_path / "holder"
    # Both processes import in parallel; each starts only on "go".
    holder = _spawn_holder(holder_root)
    rival = _spawn_holder(tmp_path / "local")
    try:
        assert _go(holder) == "ready"
        # A different root starts while the other process holds its own root.
        with RunningLocalWebService.start(
            store=store, state_directory=tmp_path / "local" / "state"
        ) as service:
            assert _request(service, "GET", "/")[0] == 200
            # The other process's root is refused here...
            with pytest.raises(LocalWebServerError, match="already running"):
                RunningLocalWebService.start(
                    store=store, state_directory=holder_root / "state"
                )
            # ...and this process's root is refused in a second process.
            assert _go(rival) == "refused: local web service is already running"
            _stop_holder(rival)
            assert rival.returncode == 3
    finally:
        _stop_holder(rival)
        _stop_holder(holder)
    assert holder.returncode == 0
    with RunningLocalWebService.start(
        store=store, state_directory=holder_root / "state"
    ) as restarted:
        assert _request(restarted, "GET", "/")[0] == 200


def test_stale_anchor_file_with_racing_starts_admits_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    # A close that cannot take the stable lock root skips the unlink and
    # leaves a stale, unlocked anchor; that must not weaken single-instance.
    first = RunningLocalWebService.start(store=store, state_directory=state)
    anchor_path = first.anchor_path
    monkeypatch.setattr(server_module, "_STARTUP_PARENT_LOCK_TIMEOUT_SECONDS", 0.2)
    root_fd = os.open(server_module._STABLE_LOCK_ROOT, os.O_RDONLY)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            first.close()
            assert anchor_path.exists()
            # While the root's flock stays busy a start reports busy, never
            # "already running".
            with pytest.raises(LocalWebServerError, match="startup lock is busy"):
                RunningLocalWebService.start(
                    store=store, state_directory=tmp_path / "other" / "state"
                )
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        os.close(root_fd)
    monkeypatch.undo()
    stale_inode = anchor_path.stat().st_ino
    with _concurrent_starts([state] * 8, tmp_path / "racers") as (started, failures):
        assert len(started) == 1
        assert len(failures) == 7
        assert all("already running" in str(exc) for exc in failures)
        assert started[0].anchor_path.stat().st_ino == stale_inode
    assert not anchor_path.exists()


def test_anchor_replaced_during_open_or_unlinked_at_runtime_never_admits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    lock_root = server_module._STABLE_LOCK_ROOT
    real_flock = fcntl.flock
    swapped: list[str] = []

    # Open: the named anchor is swapped right after its flock is taken and
    # before the stable root is released; the start fails closed.
    def swap_after_anchor_lock(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        if swapped or operation != fcntl.LOCK_EX | fcntl.LOCK_NB:
            return
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return
        name = next(
            entry.name
            for entry in os.scandir(lock_root)
            if entry.name.startswith(".traceback-web-")
            and entry.inode() == metadata.st_ino
        )
        replacement = lock_root / f"{name}.swap"
        replacement.write_bytes(b"")
        os.chmod(replacement, 0o600)
        os.replace(replacement, lock_root / name)
        swapped.append(name)

    monkeypatch.setattr(server_module.fcntl, "flock", swap_after_anchor_lock)
    try:
        with pytest.raises(LocalWebServerError, match="anchor identity changed"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        monkeypatch.undo()
    assert swapped
    assert not (state / "instance.json").exists()
    (lock_root / swapped[0]).unlink(missing_ok=True)

    # Runtime: unlinking the live anchor fails the service closed, and a
    # racing start that creates a fresh anchor is still refused by the lease.
    first = RunningLocalWebService.start(store=store, state_directory=state)
    anchor_path = first.anchor_path
    try:
        anchor_path.unlink()
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
        deadline = time.monotonic() + 2
        while first.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.server.security_failed.is_set()
        assert not first.is_running
        # The refused start unlinked the fresh anchor it created.
        assert not anchor_path.exists()
        # A same-name file created by someone else is not first's anchor.
        anchor_path.write_bytes(b"")
        os.chmod(anchor_path, 0o600)
        foreign_inode = anchor_path.stat().st_ino
    finally:
        # Close: first's anchor is gone, so close must not unlink the
        # replacement that another start may own.
        first.close()
    assert anchor_path.stat().st_ino == foreign_inode
    with RunningLocalWebService.start(store=store, state_directory=state) as again:
        assert _request(again, "GET", "/")[0] == 200
    assert not anchor_path.exists()


def test_start_racing_a_close_on_one_root_never_overlaps_or_loses_its_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close of A interleaved with start of B on the same state directory.

    B is launched while A holds the stable root's flock around its unlink: B
    must wait.  B then completes while A still holds its old, unlinked anchor
    inode, and A's final release must not disturb B's anchor.
    """

    store, _ = _store(tmp_path)
    state = tmp_path / "state"
    first = RunningLocalWebService.start(store=store, state_directory=state)
    anchor = server_module._RUNTIMES[first._runtime_id].startup_anchor
    real_flock = fcntl.flock
    started = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def start_second() -> None:
        try:
            outcome["service"] = RunningLocalWebService.start(
                store=store, state_directory=state
            )
        except BaseException as exc:  # collected for assertion
            outcome["error"] = exc
        started.set()
        release.wait(timeout=60)
        service = outcome.get("service")
        if isinstance(service, RunningLocalWebService):
            service.close()

    second = threading.Thread(target=start_second)
    observed: list[str] = []

    root_metadata = os.fstat(anchor.parent_fd)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    contended = threading.Event()

    def interleave(descriptor: int, operation: int) -> None:
        if threading.current_thread() is second:
            try:
                real_flock(descriptor, operation)
            except BlockingIOError:
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == root_identity:
                    contended.set()
                raise
            return
        if descriptor == anchor.parent_fd and operation == fcntl.LOCK_UN:
            # A has unlinked its anchor and still holds the stable root.
            assert not (anchor.parent_path / anchor.name).exists()
            second.start()
            # B has actually been refused the stable root at least once.
            assert contended.wait(timeout=30)
            assert not started.is_set()
            observed.append("root-held")
        elif descriptor == anchor.descriptor and operation == fcntl.LOCK_UN:
            # A has released the root but still holds its old anchor inode.
            assert started.wait(timeout=30)
            observed.append("old-anchor-held")
        real_flock(descriptor, operation)

    monkeypatch.setattr(server_module.fcntl, "flock", interleave)
    try:
        first.close()
    finally:
        monkeypatch.undo()
    try:
        assert observed == ["root-held", "old-anchor-held"]
        assert "error" not in outcome, outcome.get("error")
        service = outcome["service"]
        assert isinstance(service, RunningLocalWebService)
        assert service.is_running
        runtime = server_module._RUNTIMES[service._runtime_id]
        named = os.stat(service.anchor_path, follow_symlinks=False)
        pinned = os.fstat(runtime.startup_anchor.descriptor)
        assert (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino)
        assert _request(service, "GET", "/")[0] == 200
        with pytest.raises(LocalWebServerError, match="already running"):
            RunningLocalWebService.start(store=store, state_directory=state)
    finally:
        release.set()
        second.join(timeout=60)
    assert not second.is_alive()
