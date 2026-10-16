"""S6a: the Lab loopback server — session reuse, Host checks, containment, CSP."""

from __future__ import annotations

from traceback_lab.interpretation.engine import load_library

import http.client
import io
import json
import os
import signal
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import websocket

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
    text = json.dumps(payload)
    assert "/Users/" not in text and "chunks" not in text.lower().replace("excluded_chunks", "")


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


def headless_dom(argv: list[str], *, ok, timeout: float = 60) -> str:
    """Dump one page and preserve evidence when Chrome does not reach readiness.

    A timeout kills Chrome's process group on POSIX, including children that
    otherwise keep its temporary profile open. Persistent failure is reported
    with its process and DOM state rather than hidden by another launch.
    """

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            argv, stdout=stdout, stderr=errors, start_new_session=os.name != "nt",
        )
        timed_out = False
        cleanup_error = None
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=5,
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
        if os.name != "nt" and not timed_out:
            # Chrome may exit while a child still holds its profile and output
            # handles. The isolated session belongs to this one invocation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout.seek(0)
        dom = stdout.read().decode("utf-8", errors="replace")
        errors.seek(0, os.SEEK_END)
        errors.seek(max(0, errors.tell() - 4096))
        stderr = errors.read().decode("utf-8", errors="replace")
    if not timed_out and process.returncode == 0 and ok(dom):
        return dom
    raise AssertionError(
        f"Chrome page not ready: timed_out={timed_out}, exit={process.returncode}, "
        f"dom_bytes={len(dom.encode())}, data_ready={_body_attr(dom, 'ready')}, "
        f"chart_rendered={_body_attr(dom, 'rendered')}, measured={_body_attr(dom, 'measured')}, "
        f"csp_violations={_body_attr(dom, 'csp-violations')}, clia_loading={'Loading…' in dom}, "
        f"cleanup_error={cleanup_error!r}, stderr_tail={stderr[-1000:]!r}"
    )


def _body_attr(dom: str, name: str) -> str | None:
    import re

    match = re.search(rf'<body[^>]*data-{re.escape(name)}="([^"]*)"', dom)
    return match.group(1) if match else None


def test_headless_dom_reports_process_and_readiness_failure() -> None:
    with pytest.raises(AssertionError, match=r"exit=4, dom_bytes=0, data_ready=None") as caught:
        headless_dom([sys.executable, "-c", "import sys; print('browser failed', file=sys.stderr); sys.exit(4)"],
                     ok=lambda _: True)
    assert "browser failed" in str(caught.value)


def test_headless_dom_kills_a_timed_out_process() -> None:
    script = (
        "import subprocess,sys,time; "
        "print('<body data-ready=\"1\" data-rendered=\"1\" data-csp-violations=\"0\">', flush=True); "
        "print('browser stalled', file=sys.stderr, flush=True); "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "time.sleep(30)"
    )
    with pytest.raises(AssertionError, match=r"timed_out=True") as caught:
        headless_dom([sys.executable, "-c", script], ok=lambda d: 'data-ready="1"' in d, timeout=5)
    diagnostic = str(caught.value)
    assert "data_ready=1" in diagnostic and "chart_rendered=1" in diagnostic
    assert "csp_violations=0" in diagnostic and "browser stalled" in diagnostic


def test_headless_dom_does_not_wait_for_descendant_capture_handles(tmp_path, monkeypatch) -> None:
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    killed_groups = []
    if os.name != "nt":
        real_killpg = os.killpg

        def record_group_kill(group: int, sig: int) -> None:
            killed_groups.append((group, sig))
            real_killpg(group, sig)

        monkeypatch.setattr(os, "killpg", record_group_kill)
    script = (
        "import os,pathlib,subprocess,sys; "
        f"pathlib.Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "print('<body data-ready=\"1\">', flush=True)"
    )
    try:
        dom = headless_dom([sys.executable, "-c", script], ok=lambda d: 'data-ready="1"' in d, timeout=5)
        assert 'data-ready="1"' in dom
        if os.name != "nt":
            assert killed_groups[-1] == (int(parent_pid.read_text()), signal.SIGKILL)
    finally:
        if os.name == "nt" and child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def _wait_for_chart(read_state, *, timeout: float, clock=None, pause=None) -> dict:
    """Wait for the renderer's marker, rather than an earlier page-ready flag."""

    clock = clock or time.monotonic
    pause = pause or time.sleep
    deadline = clock() + timeout
    while True:
        state = read_state()
        if state.get("rendered"):
            return state
        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(f"Chrome chart not rendered before timeout: {state!r}")
        pause(min(0.2, remaining))


def _chart_dom_cdp(argv: list[str], profile: Path, *, timeout: float = 90) -> str:
    """Wait for one Chrome page's actual chart marker through DevTools."""

    deadline = time.monotonic() + timeout
    events: list[str] = []
    dom = ""
    failure: Exception | None = None
    cleanup_error = None
    connection = None
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [*argv[:-1], "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
             "--remote-allow-origins=*", argv[-1]],
            stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
        )
        try:
            active_port = profile / "DevToolsActivePort"
            target_url = None
            direct_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline and process.poll() is None:
                if active_port.is_file():
                    port = active_port.read_text().splitlines()[0]
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
            connection = websocket.create_connection(target_url, timeout=5, origin="http://localhost")
            connection.settimeout(5)
            command_id = 0

            def command(method: str, params: dict | None = None):
                nonlocal command_id
                command_id += 1
                request_id = command_id
                connection.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(connection.recv())
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise AssertionError(f"DevTools {method} failed: {message['error']!r}")
                        return message.get("result", {})
                    if message.get("method") in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"):
                        events.append(json.dumps(message, ensure_ascii=False)[-500:])

            def evaluate(expression: str):
                result = command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                if "exceptionDetails" in result:
                    raise AssertionError(f"DevTools evaluation failed: {result['exceptionDetails']!r}")
                return result["result"].get("value")

            command("Runtime.enable")
            command("Log.enable")
            state_expression = """(() => ({
              ready: document.body?.dataset.ready ?? null,
              rendered: !!document.querySelector('.echart[data-rendered="1"]'),
              csp: document.body?.dataset.cspViolations ?? null,
              fallback: [...document.querySelectorAll('.chart-wrap .problem')].map(el => el.textContent).join('; '),
              loading: document.body?.innerText.includes('Loading…') ?? false
            }))()"""
            _wait_for_chart(lambda: evaluate(state_expression), timeout=max(0, deadline - time.monotonic()))
            dom = evaluate("document.documentElement.outerHTML")
        except Exception as exc:
            failure = exc
            if connection is not None:
                try:
                    dom = evaluate("document.documentElement.outerHTML")
                except Exception:
                    pass
        finally:
            if connection is not None:
                connection.close()
            if os.name == "nt":
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
            f"Chrome chart wait failed: {failure!r}; cleanup_error={cleanup_error!r}; "
            f"data_ready={_body_attr(dom, 'ready')}, csp_violations={_body_attr(dom, 'csp-violations')}, "
            f"fallback={'Chart could not be drawn' in dom}, console_tail={events[-8:]!r}, "
            f"stderr_tail={stderr_tail!r}"
        ) from failure
    return dom


def test_chart_wait_does_not_accept_ready_page_without_rendered_chart() -> None:
    now = [0.0]
    state = {"ready": "1", "rendered": False, "csp": "0", "fallback": "Chart could not be drawn"}

    def pause(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(AssertionError, match="Chrome chart not rendered before timeout") as caught:
        _wait_for_chart(lambda: state, timeout=0.5, clock=lambda: now[0], pause=pause)
    assert "'ready': '1'" in str(caught.value) and "Chart could not be drawn" in str(caught.value)


def test_chart_wait_accepts_a_chart_that_renders_after_page_readiness() -> None:
    now = [0.0]
    observations = iter((False, False, True))

    def read_state() -> dict:
        return {"ready": "1", "rendered": next(observations), "csp": "0"}

    def pause(seconds: float) -> None:
        now[0] += seconds

    assert _wait_for_chart(read_state, timeout=1, clock=lambda: now[0], pause=pause)["rendered"]


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless CSP check")
def test_headless_page_renders_a_chart_with_zero_csp_violations(server, tmp_path) -> None:
    id12 = api_module.LabApi(server.app.store).samples()["samples"][0]["id12"]
    dom = _chart_dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
         server.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
    )
    assert 'data-ready="1"' in dom, dom[:500]
    assert 'data-csp-violations="0"' in dom
    assert 'data-rendered="1"' in dom  # the ECharts density panel drew under the CSP
    assert "Data table" in dom
    assert "Research use only. Not CLIA-validated. Not a diagnosis." in dom
    assert "FLAG: PII" in dom


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
