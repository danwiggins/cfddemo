"""S6a: the Lab loopback server — session reuse, Host checks, containment, CSP."""

from __future__ import annotations

from traceback_lab.interpretation.engine import load_library

import http.client
import io
import json
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
import websocket

from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_lab.web import api as api_module
from traceback_lab.web.server import CSP, RunningLabServer
from traceback_runner import cli as signed_cli


@pytest.fixture(scope="module")
def ran_root(tmp_path_factory, synthetic_world) -> Path:
    """One default lab run on the synthetic chunks (curve + split controls)."""

    from traceback_runner.references import register_reference

    root = tmp_path_factory.mktemp("serve") / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    code = signed_cli.main(["lab", "run", str(synthetic_world["chunks"]), "--root", str(root), "--json"])
    assert code == 0
    return root


@pytest.fixture
def server(ran_root):
    with RunningLabServer(LabStore(ran_root)) as running:
        yield running


def _request(server, method, path, *, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    base = {"Host": f"127.0.0.1:{server.port}"}
    base.update(headers or {})
    connection.request(method, path, body=body, headers=base)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def _session(server) -> str:
    code = server.issue_link().split("#bootstrap=", 1)[1]
    body = json.dumps({"bootstrap": code})
    response, data = _request(
        server, "POST", "/api/v1/session/bootstrap",
        headers={"Origin": f"http://127.0.0.1:{server.port}", "Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    assert response.status == 200, data
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return cookie.split(";", 1)[0]


def _get(server, path, cookie, **headers):
    response, data = _request(server, "GET", path, headers={"Cookie": cookie, **headers})
    return response, data


def test_binds_loopback_and_sends_the_narrow_csp(server) -> None:
    assert server.httpd.server_address[0] == "127.0.0.1"
    response, body = _request(server, "GET", "/")
    assert response.status == 200
    assert response.getheader("Content-Security-Policy") == CSP
    assert "'unsafe-inline'" not in CSP
    assert b"<script>" not in body and b"style=" not in body


def test_foreign_host_and_forwarded_headers_are_403(server) -> None:
    response, _ = _request(server, "GET", "/", headers={"Host": "evil.example:80"})
    assert response.status == 403
    cookie = _session(server)
    response, _ = _get(server, "/api/status", cookie, **{"X-Forwarded-For": "10.0.0.1"})
    assert response.status == 403
    response, _ = _request(server, "GET", "/api/status", headers={"Host": "localhost", "Cookie": cookie})
    assert response.status == 403


def test_api_needs_a_session_and_the_bootstrap_works_once(server) -> None:
    response, _ = _request(server, "GET", "/api/status")
    assert response.status == 401
    code = server.issue_link().split("#bootstrap=", 1)[1]
    body = json.dumps({"bootstrap": code})
    headers = {"Origin": f"http://127.0.0.1:{server.port}", "Content-Type": "application/json", "Content-Length": str(len(body))}
    first, _ = _request(server, "POST", "/api/v1/session/bootstrap", headers=headers, body=body)
    second, _ = _request(server, "POST", "/api/v1/session/bootstrap", headers=headers, body=body)
    assert first.status == 200 and second.status in (401, 429)


def test_status_samples_and_sample(server) -> None:
    cookie = _session(server)
    response, data = _get(server, "/api/status", cookie)
    status = json.loads(data)
    assert response.status == 200 and status["generation"] > 0 and status["active"] is None
    samples = json.loads(_get(server, "/api/samples", cookie)[1])["samples"]
    assert len(samples) == 1 and samples[0]["controls"]["split"] is True
    id12 = samples[0]["id12"]
    payload = json.loads(_get(server, f"/api/samples/{id12}", cookie)[1])
    assert payload["found"] and payload["interpretation_version"] == load_library().version
    ids = [a["algorithm_id"] for a in payload["algorithms"]]
    assert ids[:4] == ["read-summary", "bam-qc", "depth", "fragment-size"]
    assert payload["curve"]["fractions"] == [0.1, 0.25, 0.5, 1.0]
    assert payload["library_health"]["no_library_entry"] == 0
    assert payload["preanalytic_sex"]["state"] == "no_verdict"
    assert payload["preanalytic_sex"]["reason"] == "no_assertion"
    text = json.dumps(payload)
    assert "/Users/" not in text and "chunks" not in text.lower().replace("excluded_chunks", "")


def test_sample_api_exposes_read_time_preanalytic_mismatch(server, monkeypatch) -> None:
    comparison = {"state": "mismatch", "reason": None, "declared": "M", "observed": "female"}
    monkeypatch.setattr(api_module.views, "sex_check_view", lambda _sample: comparison)
    cookie = _session(server)
    id12 = json.loads(_get(server, "/api/samples", cookie)[1])["samples"][0]["id12"]
    response, data = _get(server, f"/api/samples/{id12}", cookie)
    assert response.status == 200
    assert json.loads(data)["preanalytic_sex"] == comparison


def test_unknown_and_malformed_ids(server) -> None:
    cookie = _session(server)
    response, data = _get(server, "/api/samples/aaaaaaaaaaaa", cookie)
    assert response.status == 404 and json.loads(data)["found"] is False
    for path in ("/api/samples/../../etc", "/api/samples/ABCDEF012345", "/api/samples/x?y=1", "/api/status?root=/tmp"):
        response, _ = _get(server, path, cookie)
        assert response.status in (400, 404)


def test_artifact_is_served_and_contained(server, ran_root) -> None:
    cookie = _session(server)
    id12 = json.loads(_get(server, "/api/samples", cookie)[1])["samples"][0]["id12"]
    response, data = _get(server, f"/api/samples/{id12}/artifact/full/fragment-size/size-density", cookie)
    assert response.status == 200 and len(json.loads(data)["count"]) == 1000
    response, _ = _get(server, f"/api/samples/{id12}/artifact/0.5/fragment-size/size-density", cookie)
    assert response.status == 200
    # A symlinked data_ref target is refused.
    hist = next(ran_root.glob("lab/results/*/fragment-size/*/attempts/*/artifacts/size_hist.json"))
    secret = ran_root / "outside.json"
    secret.write_text('{"secret": true}')
    backup = hist.with_suffix(".bak")
    hist.rename(backup)
    hist.symlink_to(secret)
    try:
        for path in (f"/api/samples/{id12}/artifact/full/fragment-size/size-density",):
            response, data = _get(server, path, cookie)
            if response.status == 200:
                assert b"secret" not in data
    finally:
        hist.unlink()
        backup.rename(hist)


def test_contained_rejects_escape_routes(tmp_path) -> None:
    attempt = tmp_path / "attempt"
    (attempt / "artifacts").mkdir(parents=True)
    (attempt / "artifacts" / "ok.json").write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (attempt / "artifacts" / "link.json").symlink_to(outside)
    assert api_module._contained(attempt, "artifacts/ok.json") is not None
    for ref in ("../outside.json", "/etc/passwd", "artifacts/link.json", "artifacts/missing.json"):
        assert api_module._contained(attempt, ref) is None


def test_post_elsewhere_is_405_and_no_query_reaches_a_path(server) -> None:
    cookie = _session(server)
    response, _ = _request(server, "POST", "/api/status", headers={"Cookie": cookie, "Content-Length": "0"})
    assert response.status in (403, 405)  # a mutation without CSRF is refused before routing


def test_polling_never_blocks_a_starting_writer(server, ran_root) -> None:
    cookie = _session(server)
    store = LabStore(ran_root)
    stop = threading.Event()
    polls = []

    def poll() -> None:
        while not stop.is_set():
            polls.append(_get(server, "/api/status", cookie)[0].status)

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        for _ in range(20):
            with store.writer("run", "r-20261015-0000-abcd"):
                status = json.loads(_get(server, "/api/status", cookie)[1])
                assert status["active"]["command"] == "run"
    finally:
        stop.set()
        thread.join()
    assert polls and set(polls) == {200}


def test_invalid_library_on_disk_keeps_the_last_good_one(monkeypatch, tmp_path) -> None:
    from traceback_lab.interpretation import engine

    holder = api_module.LibraryHolder()
    good = holder.get()
    broken = tmp_path / "library.yaml"
    broken.write_text("version: 0.1.0\nentries: [\n")
    monkeypatch.setattr(api_module, "LIBRARY_FILE", broken)
    again = holder.get()
    assert again is good
    assert holder.health()["error"] and "line" in holder.health()["error"]
    del engine


def _serve_lines_after_ready(root: Path, *, timeout: float = 60) -> list[str]:
    from traceback_lab.serve_command import serve_command

    class Args:
        open_browser = False

    out = io.StringIO()
    stop = threading.Event()
    ready = threading.Event()
    seen = []
    errors = []

    def run() -> None:
        try:
            serve_command(
                Args(), LabStore(root), out=out, stdin=io.StringIO(""), stop=stop,
                ready=lambda server: (seen.append(server), ready.set()),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(
        target=run, daemon=True,
    )
    thread.start()
    try:
        ready.wait(timeout=timeout)
    finally:
        stop.set()
        thread.join(timeout=5)
    lines = out.getvalue().splitlines()
    assert not thread.is_alive(), f"serve thread did not stop; output={lines!r}, errors={errors!r}"
    assert not errors, f"serve startup failed; output={lines!r}, error={errors[0]!r}"
    assert ready.is_set() and seen, f"serve did not become ready within {timeout}s; output={lines!r}"
    return lines


def test_serve_command_prints_root_first_and_warns_on_an_empty_root(tmp_path) -> None:
    lines = _serve_lines_after_ready(tmp_path / "empty")
    assert lines[0] == f"ROOT  {tmp_path / 'empty'}"
    assert "has no Lab runs" in lines[1]
    assert lines[2].startswith("http://127.0.0.1:") and "#bootstrap=" in lines[2]


def test_serve_output_waits_for_delayed_readiness(tmp_path, monkeypatch) -> None:
    from traceback_lab import serve_command as serve_module

    entered = threading.Event()
    release = threading.Event()

    class DelayedServer:
        def __init__(self, store) -> None:
            pass

        def __enter__(self):
            entered.set()
            assert release.wait(10), "test did not release server startup"
            return self

        def __exit__(self, *args) -> None:
            pass

        def issue_link(self) -> str:
            return "http://127.0.0.1:1234/#bootstrap=synthetic"

    monkeypatch.setattr(serve_module, "RunningLabServer", DelayedServer)
    result: list[list[str]] = []
    errors: list[BaseException] = []

    def capture() -> None:
        try:
            result.append(_serve_lines_after_ready(tmp_path / "empty", timeout=10))
        except BaseException as exc:
            errors.append(exc)

    waiter = threading.Thread(target=capture, daemon=True)
    waiter.start()
    try:
        assert entered.wait(5)
        assert waiter.is_alive(), "serve capture returned before startup completed"
    finally:
        release.set()
        waiter.join(10)
    assert not waiter.is_alive() and not errors, errors
    assert result[0][2] == "http://127.0.0.1:1234/#bootstrap=synthetic"


def _chrome() -> str | None:
    for candidate in (
        os.environ.get("CHROME_BIN"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _body_attr(dom: str, name: str) -> str | None:
    import re

    match = re.search(rf'<body[^>]*data-{re.escape(name)}="([^"]*)"', dom)
    return match.group(1) if match else None


def _wait_for_dom(read_state, *, timeout: float, clock=None, pause=None) -> dict:
    """Wait for a caller's actual DOM predicate within one deadline."""

    clock = clock or time.monotonic
    pause = pause or time.sleep
    deadline = clock() + timeout
    while True:
        state = read_state()
        if state.get("matched"):
            return state
        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(f"Chrome DOM predicate not met before timeout: {state!r}")
        pause(min(0.2, remaining))


def _cdp_command(connection, method: str, params: dict, request_id: int, deadline: float,
                 events: deque[str], *, clock=None) -> dict:
    """Bound one response even if unrelated console events keep arriving."""

    clock = clock or time.monotonic
    command_deadline = min(deadline, clock() + 15)
    remaining = command_deadline - clock()
    if remaining <= 0:
        raise TimeoutError(f"DevTools {method} exceeded its deadline")
    connection.settimeout(remaining)
    connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        remaining = command_deadline - clock()
        if remaining <= 0:
            raise TimeoutError(f"DevTools {method} exceeded its deadline")
        connection.settimeout(remaining)
        message = json.loads(connection.recv())
        if message.get("id") == request_id:
            if "error" in message:
                raise AssertionError(f"DevTools {method} failed: {message['error']!r}")
            return message.get("result", {})
        if message.get("method") in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"):
            events.append(json.dumps(message, ensure_ascii=False)[-500:])


def _read_debug_port(path: Path) -> str | None:
    """An empty or partly written Chrome port file means startup is ongoing."""

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].isdecimal() or not lines[1].startswith("/devtools/browser/"):
        return None
    return lines[0]


def _dom_cdp(argv: list[str], profile: Path, *, ok, timeout: float = 90, action: str | None = None) -> str:
    """Wait for one Chrome page's caller-defined DOM state through DevTools."""

    deadline = time.monotonic() + timeout
    events: deque[str] = deque(maxlen=8)
    dom = ""
    failure: Exception | None = None
    cleanup_error = None
    connection = None
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [*argv[:-1], "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
             argv[-1]],
            stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
        )
        try:
            active_port = profile / "DevToolsActivePort"
            target_url = None
            direct_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline and process.poll() is None:
                port = _read_debug_port(active_port)
                if port:
                    try:
                        with direct_http.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                            targets = json.load(response)
                        target_url = next(
                            (target["webSocketDebuggerUrl"] for target in targets
                             if target.get("type") == "page" and target.get("url", "").startswith("http://127.0.0.1:")),
                            None,
                        )
                    except (OSError, ValueError, KeyError):
                        pass
                if target_url:
                    break
                time.sleep(0.1)
            if not target_url:
                raise AssertionError("Chrome did not expose its Lab page through DevTools")
            target = urllib.parse.urlsplit(target_url)
            if target.scheme != "ws" or target.hostname != "127.0.0.1" or target.port != int(port):
                raise AssertionError("Chrome DevTools target is not on the expected loopback port")
            connect_timeout = min(5, max(0.1, deadline - time.monotonic()))
            # websocket-client 1.9.2 ignores http_no_proxy unless a proxy host
            # option is also supplied. An already-connected local socket avoids
            # proxy environment variables without broadening Chrome's origins.
            direct_socket = socket.create_connection(("127.0.0.1", target.port), timeout=connect_timeout)
            try:
                connection = websocket.create_connection(
                    target_url, timeout=connect_timeout, suppress_origin=True,
                    http_no_proxy=["127.0.0.1", "localhost"], socket=direct_socket,
                )
            except Exception:
                direct_socket.close()
                raise
            command_id = 0

            def command(method: str, params: dict | None = None, *, until: float | None = None):
                nonlocal command_id
                command_id += 1
                return _cdp_command(connection, method, params or {}, command_id,
                                    deadline if until is None else until, events)

            def evaluate(expression: str, *, until: float | None = None):
                result = command("Runtime.evaluate", {"expression": expression, "returnByValue": True}, until=until)
                if "exceptionDetails" in result:
                    raise AssertionError(f"DevTools evaluation failed: {result['exceptionDetails']!r}")
                return result["result"].get("value")

            command("Runtime.enable")
            command("Log.enable")
            action_done = False
            def read_state() -> dict:
                nonlocal dom, action_done
                dom = evaluate("document.documentElement?.outerHTML ?? ''")
                if action and not action_done and 'data-ready="1"' in dom:
                    evaluate(action)
                    action_done = True
                    dom = evaluate("document.documentElement?.outerHTML ?? ''")
                return {
                    "matched": bool(ok(dom)), "ready": _body_attr(dom, "ready"),
                    "rendered": 'data-rendered="1"' in dom,
                    "csp": _body_attr(dom, "csp-violations"),
                    "fallback": "Chart could not be drawn" in dom,
                    "loading": "Loading…" in dom,
                }

            _wait_for_dom(read_state, timeout=max(0, deadline - time.monotonic()))
        except Exception as exc:
            failure = exc
            if connection is not None:
                try:
                        dom = evaluate("document.documentElement?.outerHTML ?? ''", until=time.monotonic() + 2)
                except Exception:
                    pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    cleanup_error = f"DevTools close failed: {exc!r}"
            if process.poll() is not None:
                pass  # Reaped leader PID must never be used as a process-group target.
            elif os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
                    )
                except subprocess.TimeoutExpired:
                    cleanup_error = "taskkill timed out"
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    cleanup_error = "direct browser process did not exit after kill"
            stderr.seek(0, os.SEEK_END)
            stderr.seek(max(0, stderr.tell() - 4096))
            stderr_tail = stderr.read().decode("utf-8", errors="replace")[-1000:]
    if failure or cleanup_error:
        raise AssertionError(
            f"Chrome DOM wait failed: {failure!r}; cleanup_error={cleanup_error!r}; "
            f"data_ready={_body_attr(dom, 'ready')}, csp_violations={_body_attr(dom, 'csp-violations')}, "
            f"fallback={'Chart could not be drawn' in dom}, console_tail={list(events)!r}, "
            f"stderr_tail={stderr_tail!r}"
        ) from failure
    return dom


def test_dom_wait_does_not_accept_ready_page_without_rendered_chart() -> None:
    now = [0.0]
    state = {"ready": "1", "rendered": False, "matched": False, "csp": "0", "fallback": "Chart could not be drawn"}

    def pause(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(AssertionError, match="Chrome DOM predicate not met before timeout") as caught:
        _wait_for_dom(lambda: state, timeout=0.5, clock=lambda: now[0], pause=pause)
    assert "'ready': '1'" in str(caught.value) and "Chart could not be drawn" in str(caught.value)


def test_dom_wait_accepts_a_chart_that_renders_after_page_readiness() -> None:
    now = [0.0]
    observations = iter((False, False, True))

    def read_state() -> dict:
        rendered = next(observations)
        return {"ready": "1", "rendered": rendered, "matched": rendered, "csp": "0"}

    def pause(seconds: float) -> None:
        now[0] += seconds

    assert _wait_for_dom(read_state, timeout=1, clock=lambda: now[0], pause=pause)["rendered"]


def test_cdp_command_has_a_deadline_under_continuous_console_events() -> None:
    now = [0.0]
    events: deque[str] = deque(maxlen=8)

    class FloodingConnection:
        def settimeout(self, seconds: float) -> None:
            assert 0 < seconds <= 15

        def send(self, payload: str) -> None:
            assert json.loads(payload)["id"] == 1

        def recv(self) -> str:
            now[0] += 1
            return json.dumps({"method": "Runtime.consoleAPICalled", "params": {"tick": now[0]}})

    with pytest.raises(TimeoutError, match="exceeded its deadline"):
        _cdp_command(FloodingConnection(), "Runtime.evaluate", {}, 1, 90, events, clock=lambda: now[0])
    assert now[0] == 15 and len(events) == 8


def test_debug_port_waits_for_complete_chrome_startup_file(tmp_path) -> None:
    active_port = tmp_path / "DevToolsActivePort"
    assert _read_debug_port(active_port) is None
    active_port.write_text("")
    assert _read_debug_port(active_port) is None
    active_port.write_text("1234\n")
    assert _read_debug_port(active_port) is None
    active_port.write_text("1234\n/devtools/browser/synthetic\n")
    assert _read_debug_port(active_port) == "1234"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_cdp_does_not_signal_an_already_reaped_browser(tmp_path, monkeypatch) -> None:
    class ExitedBrowser:
        pid = 1234

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float) -> int:
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: ExitedBrowser())

    def unexpected_kill(*args: object) -> None:
        raise AssertionError("reaped browser PID was signalled")

    monkeypatch.setattr(os, "killpg", unexpected_kill)
    with pytest.raises(AssertionError, match="did not expose its Lab page"):
        _dom_cdp(["chrome", "http://127.0.0.1/"], tmp_path / "profile", ok=lambda dom: True, timeout=1)


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless CSP check")
def test_headless_page_renders_a_chart_with_zero_csp_violations(server, tmp_path) -> None:
    id12 = api_module.LabApi(server.app.store).samples()["samples"][0]["id12"]
    dom = _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
         server.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
        ok=lambda page: 'data-ready="1"' in page and 'class="echart"' in page and 'data-rendered="1"' in page
            and "step_end read-summary status=ok" in page,
        action="[...document.querySelectorAll('#main a')].find(a => a.textContent === 'Run log line →')?.click()",
    )
    assert 'data-ready="1"' in dom, dom[:500]
    assert 'data-csp-violations="0"' in dom
    assert 'data-rendered="1"' in dom  # the ECharts density panel drew under the CSP
    assert "Data table" in dom
    assert "Run log line →" in dom
    assert "step_end read-summary status=ok" in dom
    assert "Research use only. Not CLIA-validated. Not a diagnosis." in dom
    assert "FLAG: PII" in dom
    assert "No operator sex assertion was supplied; no comparison was made." in dom
    assert "FLAG: PRIVACY — can reveal pregnancy or transplant status." in dom


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless Trust/QC check")
@pytest.mark.parametrize(
    ("comparison", "expected"),
    [
        ({"state": "mismatch", "reason": None, "declared": "M", "observed": "female"},
         "Pre-analytic mismatch: operator-declared M differs from the measured chrX/chrY call (female)"),
        ({"state": "no_verdict", "reason": "no_measurement", "declared": "M", "observed": None},
         "No measured chrX/chrY call is available; no comparison verdict."),
        ({"state": "no_verdict", "reason": "indeterminate_call", "declared": "M", "observed": "indeterminate"},
         "Measured chrX/chrY call is indeterminate; no comparison verdict."),
        ({"state": "concordant", "reason": None, "declared": "M", "observed": "male"},
         "Operator assertion (M) and measured chrX/chrY call (male) agree."),
        ({"state": "no_verdict", "reason": "malformed_metadata", "declared": None, "observed": "female",
          "problem": {"code": "TBX-LAB-023", "fix": "Restore the local sample record from a known-good backup before setting --sex again; reruns without --sex may continue"}},
         "Stored operator sex assertion metadata is malformed; no comparison verdict."),
        ({"state": "unexpected", "reason": None, "declared": "M", "observed": "male"},
         "Comparison state is unavailable; no comparison verdict."),
    ],
)
def test_trust_qc_shows_preanalytic_verdict_and_privacy_flag(server, tmp_path, monkeypatch, comparison, expected) -> None:
    monkeypatch.setattr(api_module.views, "sex_check_view", lambda _sample: comparison)
    id12 = api_module.LabApi(server.app.store).samples()["samples"][0]["id12"]
    dom = _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
         server.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
        ok=lambda page: 'data-ready="1"' in page and "Pre-analytic sex assertion check" in page,
    )
    assert expected in dom
    assert "FLAG: PRIVACY — can reveal pregnancy or transplant status." in dom
    assert "This is not a diagnostic sex or gender determination." in dom
    if comparison["reason"] == "malformed_metadata":
        assert "TBX-LAB-023: Restore the local sample record" in dom


def test_read_contained_refuses_symlinked_ancestors_and_hard_links(tmp_path) -> None:
    base = tmp_path / "results"
    real = tmp_path / "elsewhere" / "attempt"
    (real / "artifacts").mkdir(parents=True)
    (real / "artifacts" / "x.json").write_text("{}")
    (base / "input").mkdir(parents=True)
    (base / "input" / "attempt").symlink_to(real)  # a symlinked attempt directory
    assert api_module._read_contained(base, ["input", "attempt"], "artifacts/x.json") is None
    (base / "input" / "attempt").unlink()
    shutil.copytree(real, base / "input" / "attempt")
    assert api_module._read_contained(base, ["input", "attempt"], "artifacts/x.json") == b"{}"
    os.link(base / "input" / "attempt" / "artifacts" / "x.json", tmp_path / "hard.json")
    assert api_module._read_contained(base, ["input", "attempt"], "artifacts/x.json") is None


def test_unreadable_library_error_names_no_path(monkeypatch, tmp_path) -> None:
    holder = api_module.LibraryHolder()
    holder.get()
    monkeypatch.setattr(api_module, "LIBRARY_FILE", tmp_path / "missing" / "library.yaml")
    holder.get()
    assert holder.health()["error"] == "the library file is unreadable"


def test_status_finds_an_interrupted_run_through_the_pointer(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id):
        store.run_log(run_id).append("run_start", command="run")
        store.run_log(run_id).append("step_start", step="align")
    status = api_module.LabApi(store).status()
    assert status["interrupted"] == {"run_id": run_id, "step": "align"}


def test_status_keeps_sample_identity_before_first_step(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    source_id, input_id = "a" * 64, "b" * 64
    with store.writer("run", run_id):
        log = store.run_log(run_id)
        log.append("run_start", command="run")
        log.append("sample", source_id=source_id, input_id=input_id)
    assert api_module.LabApi(store).status()["interrupted"] == {
        "run_id": run_id, "step": None, "source_id": source_id, "input_id": input_id,
    }


@pytest.mark.parametrize("partial", (b'{"event":', b'{"event":"step_start","step":"caf\xc3'))
def test_status_keeps_complete_events_before_torn_final_line(tmp_path, partial) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id, step="align"):
        log = store.run_log(run_id)
        log.append("run_start", command="run")
        log.append("step_start", step="align")
        with log.path.open("ab") as stream:
            stream.write(partial)
        events, reason = api_module._run_events(store, run_id)
        assert reason is None
        assert [number for number, _ in events] == [1, 2]
        active = api_module.LabApi(store).status()
        assert active["active"]["step"] == "align"
        assert active["run_log_unavailable"] is None
    interrupted = api_module.LabApi(store).status()
    assert interrupted["interrupted"] == {"run_id": run_id, "step": "align"}
    assert interrupted["run_log_unavailable"] is None


@pytest.mark.parametrize("command", ("run", "simulate cna", "simulate split"))
def test_status_identifies_sample_only_after_run_sample_event(tmp_path, command) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    source_id, input_id = "a" * 64, "b" * 64
    with store.writer(command, run_id, step="bam-qc"):
        log = store.run_log(run_id)
        log.append("run_start", command=command)
        before = api_module.LabApi(store).status()["active"]
        assert "source_id" not in before and "input_id" not in before
        log.append("sample", source_id=source_id, input_id=input_id)
        log.append("step_start", step="bam-qc")
        active = api_module.LabApi(store).status()["active"]
        assert active["source_id"] == source_id and active["input_id"] == input_id
    interrupted = api_module.LabApi(store).status()["interrupted"]
    assert interrupted == {"run_id": run_id, "step": "bam-qc",
                           "source_id": source_id, "input_id": input_id}


@pytest.mark.parametrize("command", ("run", "simulate cna", "simulate split"))
def test_active_writer_waiting_for_first_log_event_is_not_unavailable(tmp_path, command) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer(command, run_id):
        status = api_module.LabApi(store).status()
        assert status["active"]["command"] == command
        assert status["run_log_unavailable"] is None
        store.runs_dir.mkdir()
        (store.runs_dir / f"{run_id}.jsonl").touch()
        assert api_module.LabApi(store).status()["run_log_unavailable"] is None
        (store.runs_dir / f"{run_id}.jsonl").write_bytes(b'{"event":"run_start","run_id":')
        assert api_module.LabApi(store).status()["run_log_unavailable"] is None
        (store.runs_dir / f"{run_id}.jsonl").write_bytes(b'{broken\n')
        assert api_module.LabApi(store).status()["run_log_unavailable"] == {
            "run_id": run_id, "reason": "malformed_or_empty",
        }


def test_torn_first_event_without_active_writer_is_unavailable(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id):
        store.runs_dir.mkdir(exist_ok=True)
        (store.runs_dir / f"{run_id}.jsonl").write_bytes(b'{"event":"run_start","run_id":')
    status = api_module.LabApi(store).status()
    assert status["active"] is None and status["interrupted"] is None
    assert status["run_log_unavailable"] == {"run_id": run_id, "reason": "malformed_or_empty"}


@pytest.mark.parametrize(("damage", "reason"), (
    ("oversized", "unreadable_or_over_1mib"),
    ("too_many_lines", "over_4096_lines"),
    ("malformed", "malformed_or_empty"),
    ("wrong_run", "malformed_or_empty"),
    ("unsafe", "unreadable_or_over_1mib"),
))
@pytest.mark.parametrize("command", ("run", "simulate cna", "simulate split"))
def test_status_reports_unavailable_latest_log_without_claiming_interruption(tmp_path, damage, reason, command) -> None:
    store = LabStore(tmp_path / "root")
    run_id = "r-20261015-0101-abcd"
    with store.writer(command, run_id, step="bam-qc"):
        log = store.run_log(run_id)
        log.append("run_start", command=command)
        log.append("step_start", step="bam-qc")
    path = store.runs_dir / f"{run_id}.jsonl"
    if damage == "oversized":
        path.write_bytes(b"x" * (1024 * 1024 + 1))
    elif damage == "too_many_lines":
        path.write_bytes(b"{}\n" * 4097)
    elif damage == "malformed":
        path.write_bytes(b'{"event":"run_start"}\n{broken\n')
    elif damage == "wrong_run":
        path.write_bytes(b'{"event":"run_start","run_id":"r-20261015-0101-eeee"}\n')
    else:
        target = tmp_path / "other.jsonl"
        target.write_text('{}\n')
        path.unlink()
        path.symlink_to(target)
    status = api_module.LabApi(store).status()
    assert status["active"] is None and status["interrupted"] is None
    assert status["run_log_unavailable"] == {"run_id": run_id, "reason": reason}


def test_status_does_not_require_run_logs_from_other_writer_commands(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("cohort add", run_id):
        active = api_module.LabApi(store).status()
        assert active["active"]["command"] == "cohort add"
        assert active["run_log_unavailable"] is None
    ended = api_module.LabApi(store).status()
    assert ended["active"] is None and ended["interrupted"] is None
    assert ended["run_log_unavailable"] is None


@pytest.mark.parametrize("pointer", (
    {"command": [], "run_id": "r-20261015-0101-abcd"},
    {"command": {}, "run_id": "r-20261015-0101-abcd"},
    {"command": "run", "run_id": []},
    {"command": "run", "run_id": "invalid"},
))
def test_status_reports_malformed_latest_run_pointer_without_echoing_it(tmp_path, pointer) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.last_run_file.write_text(json.dumps(pointer))
    status = api_module.LabApi(store).status()
    assert status["active"] is None and status["interrupted"] is None
    assert status["run_log_unavailable"] == {"run_id": None, "reason": "latest_run_pointer_unavailable"}


def test_active_nonanalysis_writer_does_not_echo_malformed_pointer(tmp_path) -> None:
    store = LabStore(tmp_path)
    with store.writer("cohort add", "r-20261015-0101-abcd"):
        store.last_run_file.write_text('{"run_id":"untrusted"}')
        status = api_module.LabApi(store).status()
    assert status["active"]["command"] == "cohort add"
    assert status["run_log_unavailable"] == {"run_id": None, "reason": "latest_run_pointer_unavailable"}


def test_recent_run_candidates_are_bounded_and_do_not_use_unbounded_run_ids(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.runs_dir.mkdir(parents=True)
    for index in range(80):
        path = store.runs_dir / f"r-20261015-{index:04d}-abcd.jsonl"
        path.write_text('{}\n')
        os.utime(path, ns=((index + 1) * 1_000_000_000, (index + 1) * 1_000_000_000))
    monkeypatch.setattr(store, "run_ids", lambda: (_ for _ in ()).throw(AssertionError("unbounded run_ids")))
    recent = api_module._recent_run_ids(store)
    assert len(recent) == 64
    assert recent[0] == "r-20261015-0079-abcd"
    assert recent[-1] == "r-20261015-0016-abcd"


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless Trust/QC check")
@pytest.mark.parametrize("kind", ("active", "active-pending", "active-before-sample", "interrupted",
                                  "active-unlogged-prior-badlog", "active-unlogged-prior-interrupted"))
@pytest.mark.parametrize("selected", ("a", "c"))
def test_trust_qc_run_note_is_scoped_without_replacing_result_status(tmp_path, kind, selected) -> None:
    source_a, input_a = "a" * 64, "b" * 64
    source_b, input_b = "c" * 64, "d" * 64
    samples = {}
    for source_id, input_id in ((source_a, input_a), (source_b, input_b)):
        id12 = source_id[:12]
        samples[id12] = {
            "found": True, "id12": id12, "source_id": source_id, "full_input_id": input_id, "example": False,
            "run_utc": "2026-10-16T00:00:00Z", "depth": {"approx_depth_x": 1.0},
            "excluded_chunks": [], "preanalytic_sex": {"state": "no_verdict", "reason": "no_assertion"},
            "cohort_n": 0,
            "algorithms": [{"algorithm_id": "bam-qc", "hidden": False, "status": "ok",
                            "skip_reason": None, "skip_message": None, "failure": None,
                            "duration_s": 1.0, "min_depth_x": None, "metrics": [],
                            "provenance": {"input_id": input_id}}],
            "library_health": {"no_library_entry": 0, "unverified_citations": 0,
                               "version": "fixture", "error": None},
        }
    marker = {"run_id": "r-20261015-0101-abcd", "step": "bam-qc",
              "source_id": source_a, "input_id": input_a}
    if kind == "active-pending":
        marker["step"] = "read-summary"
    shown_marker = {key: value for key, value in marker.items()
                    if kind not in ("active-before-sample", "active-unlogged-prior-badlog",
                                    "active-unlogged-prior-interrupted") or key not in ("source_id", "input_id")}
    if kind == "active-before-sample":
        shown_marker["step"] = None
    prior_run_id = "r-20261015-0101-eeee"
    if kind.startswith("active-unlogged"):
        shown_marker = {"run_id": "r-20261015-0101-dddd", "step": "cohort add"}
    with RunningLabServer(LabStore(tmp_path / "root")) as running:
        running.app.api.status = lambda: {
            "generation": 1, "active": {**shown_marker, "started_utc": "2026-10-16T00:00:00Z"} if kind.startswith("active") else None,
            "interrupted": shown_marker if kind == "interrupted" else (
                {"run_id": prior_run_id, "step": "read-summary"}
                if kind == "active-unlogged-prior-interrupted" else None),
            "run_log_unavailable": {"run_id": prior_run_id, "reason": "malformed_or_empty"}
            if kind == "active-unlogged-prior-badlog" else None,
            "library": samples[source_a[:12]]["library_health"],
        }
        running.app.api.samples = lambda: {"samples": [
            {"id12": row["id12"], "run_utc": row["run_utc"], "depth_x": 1.0, "example": False}
            for row in samples.values()], "root_has_runs": True}
        running.app.api.sample = lambda id12: samples.get(id12, {"found": False, "candidates": []})
        id12 = (source_a if selected == "a" else source_b)[:12]
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
             running.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
            ok=lambda page: 'data-ready="1"' in page and "<h2>Run</h2>" in page and (
                not kind.startswith("active-unlogged") or prior_run_id in page),
        )
    run_table = dom.split("<h2>Run</h2>", 1)[1].split("</table>", 1)[0]
    assert '<th scope="row">bam-qc</th><td>ok' in run_table
    if kind == "active-pending":
        assert ("Latest run in progress during read-summary; no result was published for this step." in dom) == (selected == "a")
    note = "latest run " + ("interrupted" if kind == "interrupted" else "in progress")
    assert (note in run_table) == (selected == "a" and kind in ("active", "interrupted"))
    if kind == "active-before-sample":
        assert "step: starting" in dom and "step: null" not in dom
    if kind == "active-unlogged-prior-badlog":
        assert f"Run log for {prior_run_id} is unavailable (log malformed or empty)" in dom
        assert "Run log status unavailable" not in dom
    if kind == "active-unlogged-prior-interrupted":
        assert f"Earlier run ({prior_run_id}) stopped during read-summary" in dom


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless Trust/QC check")
@pytest.mark.parametrize("selected", ("a", "c"))
@pytest.mark.parametrize("log_state", ("interrupted", "before-step", "unavailable"))
def test_interrupted_first_step_without_result_is_sample_scoped(tmp_path, selected, log_state) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    source_a, input_a = "a" * 64, "b" * 64
    source_b, input_b = "c" * 64, "d" * 64
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    for source_id, input_id in ((source_a, input_a), (source_b, input_b)):
        (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
            "source_id": source_id, "example": True,
            "inputs": [{"input_id": input_id, "used_utc": "2026-10-16T00:00:00Z"}],
        }))
        manifest = store.inputs_dir / input_id / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"input_id": input_id}))
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id, step="bam-qc"):
        log = store.run_log(run_id)
        log.append("run_start", command="run")
        log.append("sample", source_id=source_a, input_id=input_a)
        if log_state != "before-step":
            log.append("step_start", step="bam-qc")
    if log_state == "unavailable":
        (store.runs_dir / f"{run_id}.jsonl").write_bytes(b"x" * (1024 * 1024 + 1))
    api = api_module.LabApi(store)
    for source_id, input_id in ((source_a, input_a), (source_b, input_b)):
        payload = api.sample(source_id[:12])
        assert payload["found"] and payload["full_input_id"] == input_id
        assert payload["algorithms"] == []
    with RunningLabServer(store) as running:
        id12 = (source_a if selected == "a" else source_b)[:12]
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
             running.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
            ok=lambda page: 'data-ready="1"' in page and "<h2>Run</h2>" in page,
        )
    run_panel = dom.split("<h2>Run</h2>", 1)[1].split("</section>", 1)[0]
    assert ("Latest run interrupted during bam-qc; no result was published for this step." in run_panel) == (selected == "a" and log_state == "interrupted")
    if log_state == "before-step":
        assert api.status()["interrupted"] == {"run_id": run_id, "step": None,
                                               "source_id": source_a, "input_id": input_a}
        assert "The last run (" in dom and "stopped during a step" in dom
        assert "no result was published for this step" not in run_panel
        assert "during null" not in dom
    assert ("status is unavailable (log unreadable, unsafe, or over 1 MiB)" in dom) == (log_state == "unavailable")
    assert "Run log line →" not in run_panel


def _selected_sex_result(source_id: str, input_id: str, identity: str) -> ResultEnvelope:
    return ResultEnvelope(
        algorithm_id="sex-check", algorithm_version="1", status="ok",
        provenance=provenance(
            tool="test", source_id=source_id, input_id=input_id,
            parameters_sha256=identity, reference_id="synth",
        ),
        metrics=[Metric(metric_id="call", value="male")],
    )


def test_run_log_line_is_authenticated_matched_and_allowlisted(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source_id, input_id, identity = "a" * 64, "b" * 64, "c" * 64
    id12 = source_id[:12]
    result = _selected_sex_result(source_id, input_id, identity)
    attempt_id = store.publish_result(result, input_id=input_id)
    run_id = "r-20261015-0101-abcd"
    log = store.run_log(run_id)
    log.append("run_start", command="run", flags={"private": "/secret/path"})
    log.append("sample", source_id=source_id, input_id=input_id, sample=id12)
    log.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id,
               attempt_id=attempt_id, status="ok", duration_s=1.25, first_line="/secret/path")
    sample = SimpleNamespace(full=SimpleNamespace(
        input_id=input_id,
        results={"sex-check": result},
    ))
    monkeypatch.setattr(api_module.LabApi, "_resolve", lambda _self, _id: (source_id, []))
    monkeypatch.setattr(api_module.views, "load_sample", lambda _store, _source: sample)
    with RunningLabServer(store) as running:
        path = f"/api/samples/{id12}/run-log/sex-check"
        assert _request(running, "GET", path)[0].status == 401
        cookie = _session(running)
        response, data = _get(running, path, cookie)
        assert response.status == 200
        payload = json.loads(data)
        assert payload == {"found": True, "run_id": run_id, "line": 3, "step": "sex-check",
                           "utc": payload["utc"], "status": "ok", "skip_reason": None,
                           "duration_s": 1.25}
        assert "/secret/path" not in data.decode()
        sample.full.results["sex-check"] = _selected_sex_result(source_id, input_id, "d" * 64)
        assert _get(running, path, cookie)[0].status == 404
        sample.full.results["sex-check"] = result
        sample.full.input_id = "e" * 64
        assert _get(running, path, cookie)[0].status == 404
        assert _get(running, path + "?root=/secret/path", cookie)[0].status == 400


def test_run_log_line_uses_only_the_selected_attempt(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source_id, input_id, identity = "a" * 64, "b" * 64, "c" * 64
    result = _selected_sex_result(source_id, input_id, identity)
    first = store.publish_result(result, input_id=input_id)
    old = store.run_log("r-20261015-0101-ffff")
    old.append("sample", source_id=source_id, input_id=input_id)
    old.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id,
               attempt_id=first, status="ok")
    second = store.publish_result(result, input_id=input_id)
    new = store.run_log("r-20261015-0101-0001")
    new.append("sample", source_id=source_id, input_id=input_id)
    new.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id,
               attempt_id=second, status="ok")
    os.utime(old.path, ns=(200_000_000_000, 200_000_000_000))
    os.utime(new.path, ns=(100_000_000_000, 100_000_000_000))
    sample = SimpleNamespace(full=SimpleNamespace(input_id=input_id, results={"sex-check": result}))
    monkeypatch.setattr(api_module.LabApi, "_resolve", lambda _self, _id: (source_id, []))
    monkeypatch.setattr(api_module.views, "load_sample", lambda _store, _source: sample)
    api = api_module.LabApi(store)
    assert api.run_log_line(source_id[:12], "sex-check")["run_id"] == new.run_id
    latest = store.run_log("r-20261015-0101-0000")
    latest.append("sample", source_id=source_id, input_id=input_id)
    latest.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id,
                  attempt_id=second, status="ok")
    os.utime(latest.path, ns=(300_000_000_000, 300_000_000_000))
    assert api.run_log_line(source_id[:12], "sex-check")["run_id"] == latest.run_id
    latest.path.unlink()
    new.path.unlink()
    legacy = store.run_log("r-20261015-0101-bbbb")
    legacy.append("sample", source_id=source_id, input_id=input_id)
    legacy.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id, status="ok")
    assert api.run_log_line(source_id[:12], "sex-check") == {"found": False}


def test_run_log_reader_refuses_symlinked_and_oversized_logs(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path / "root")
    store.runs_dir.mkdir(parents=True)
    run_id = "r-20261015-0101-abcd"
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"event": "step_end", "step": "sex-check", "status": "ok"}))
    (store.runs_dir / f"{run_id}.jsonl").symlink_to(outside)
    assert api_module._read_run_log(store.runs_dir / f"{run_id}.jsonl") is None
    link = tmp_path / "linked-root"
    link.symlink_to(store.root, target_is_directory=True)
    assert api_module._read_run_log(link / "lab" / "runs" / f"{run_id}.jsonl") is None
    oversized = store.runs_dir / "oversized.jsonl"
    oversized.write_bytes(b"x" * 9)
    assert api_module._read_run_log(oversized, limit=8) is None
    oversized.write_bytes(b"x")
    reads = iter((b"1234", b"56789"))
    monkeypatch.setattr(api_module.os, "read", lambda _fd, _size: next(reads))
    assert api_module._read_run_log(oversized, limit=8) is None
