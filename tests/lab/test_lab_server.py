"""S6a: the Lab loopback server — session reuse, Host checks, containment, CSP."""

from __future__ import annotations

from traceback_lab.interpretation.engine import load_library

import http.client
import io
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
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

from traceback_lab.envelope import Depth, Metric, ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_lab.store import ResultOrderUnavailable
from traceback_lab import views
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


def test_selection_read_unavailable_is_sanitized_and_actionable(tmp_path, monkeypatch) -> None:
    with RunningLabServer(LabStore(tmp_path)) as running:
        cookie = _session(running)

        def fail_selection(_id12):
            raise ResultOrderUnavailable("private path /sensitive/results/selected.json")

        monkeypatch.setattr(running.app.api, "sample", fail_selection)
        response, body = _get(running, "/api/samples/" + "a" * 12, cookie)
    assert response.status == 503
    error = json.loads(body)["error"]
    assert error["code"] == "TBX-LAB-WEB-SELECTION-UNAVAILABLE"
    assert "Retry" in error["fix"] and "restore" in error["fix"]
    assert b"/sensitive/" not in body


def test_binds_loopback_and_sends_the_narrow_csp(server) -> None:
    assert server.httpd.server_address[0] == "127.0.0.1"
    response, body = _request(server, "GET", "/")
    assert response.status == 200
    assert response.getheader("Content-Security-Policy") == CSP
    assert "'unsafe-inline'" not in CSP
    assert b"<script>" not in body and b"style=" not in body


def test_asset_connections_fit_the_listen_queue_when_accept_is_delayed(tmp_path) -> None:
    """A browser's burst of asset connections must survive a busy accept loop."""

    with RunningLabServer(LabStore(tmp_path)) as running:
        original_accept = running.httpd.get_request
        accepting = threading.Event()
        release = threading.Event()

        def delayed_accept():
            accepting.set()
            assert release.wait(10), "asset burst did not release the accept loop"
            return original_accept()

        running.httpd.get_request = delayed_accept
        sockets = []
        try:
            sockets.append(socket.create_connection(("127.0.0.1", running.port), timeout=1))
            assert accepting.wait(2), "server did not enter accept"
            # The first connection is held at accept; further requests queue at
            # the kernel listen socket, just as CSS and script requests do.
            for _ in range(12):
                sockets.append(socket.create_connection(("127.0.0.1", running.port), timeout=0.5))
            assert len(sockets) == 13
        finally:
            for connection in sockets:
                connection.close()
            release.set()


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


def test_missing_library_metric_is_counted_in_trust_qc(server, monkeypatch, tmp_path) -> None:
    """D7: a real read-side join keeps an unknown metric and counts it on both routes."""

    if _chrome() is None:
        pytest.skip("no Chrome/Chromium for the D7 DOM check")
    source_id = next(iter(server.app.api._id12s()))
    sample = views.load_sample(server.app.store, source_id)
    assert sample is not None
    result = sample.full.results["fragment-size"]
    assert result.status == "ok"
    sample.full.results["fragment-size"] = result.model_copy(update={
        "metrics": [*result.metrics, Metric(metric_id="missing_library_fixture", value=0.123)],
    })
    original = views.load_sample
    monkeypatch.setattr(api_module.views, "load_sample",
                        lambda store, source, **kwargs: sample if source == source_id else original(store, source, **kwargs))

    payload = server.app.api.sample(sample.id12)
    assert payload["library_health"]["no_library_entry"] == 1
    unknown = next(metric for algorithm in payload["algorithms"]
                   if algorithm["algorithm_id"] == "fragment-size"
                   for metric in algorithm["metrics"] if metric["metric_id"] == "missing_library_fixture")
    assert (unknown["state"], unknown["reason"], unknown["value"]) == (
        "no_reference", "no_library_entry", 0.123)

    for route, expected in (("findings", "missing_library_fixture"),
                            ("trust-qc", "no_library_entry: 1")):
        profile = tmp_path / route
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{sample.id12}/{route}"],
            profile, ok=lambda page, expected=expected: 'data-ready="1"' in page and expected in page,
        )
        assert expected in dom
        if route == "findings":
            assert "No reference" in dom and "no library entry" in dom


def _sample_with_unvalidated_wps(server, monkeypatch):
    """Keep the real stored sample and add WPS measurements at each control depth."""
    api = api_module.LabApi(server.app.store)
    sample = views.load_sample(server.app.store, next(iter(api._id12s())))
    assert sample is not None
    for view in [sample.full, *sample.derived]:
        source = next(envelope for envelope in view.results.values() if envelope.status == "ok")
        value = 0.7 if view is sample.full else 0.4
        wps = source.model_copy(update={
            "algorithm_id": "wps",
            "depth": Depth(eligible_reads=1_000_000, approx_depth_x=200 * view.fraction),
            "metrics": [Metric(metric_id="qualifying_fragment_depth_x", value=value)],
            "panels": [],
        })
        # The read-side sample depth comes from the first result; put the
        # injected WPS measurement first so this fixture exercises its gate.
        view.results = {"wps": wps, **{key: result for key, result in view.results.items() if key != "wps"}}
    original = views.load_sample
    monkeypatch.setattr(api_module.views, "load_sample", lambda store, source_id, **kwargs: sample if source_id == sample.source_id else original(store, source_id, **kwargs))
    return sample.id12


def test_sample_api_keeps_unvalidated_wps_values_without_stability_verdict(server, monkeypatch) -> None:
    id12 = _sample_with_unvalidated_wps(server, monkeypatch)
    payload = json.loads(_get(server, f"/api/samples/{id12}", _session(server))[1])
    panel = next(p for p in payload["stability"] if p["algorithm_id"] == "wps" and p["metric_id"] == "qualifying_fragment_depth_x")
    assert panel["verdict"] == "stability not validated"
    assert panel["points"][0]["value"] == 0.4
    assert panel["points"][0]["state"] == "ok_unvalidated"
    assert panel["points"][0]["deviation"] is None
    assert panel["split"]["stable"] is None
    assert not any(p["algorithm_id"] == "wps" and p["metric_id"] == "qualifying_fragment_depth_x" for p in payload.get("defended", {}).get("rows", []))


def test_stability_dom_names_unvalidated_wps_without_false_verdict(server, tmp_path, monkeypatch) -> None:
    if _chrome() is None:
        pytest.skip("no Chrome/Chromium for the headless Stability check")
    id12 = _sample_with_unvalidated_wps(server, monkeypatch)
    dom = _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
         server.issue_link() + f"&route=/sample/{id12}/stability"], tmp_path / "profile",
        ok=lambda page: 'data-ready="1"' in page and "Stability: every point" in page,
    )
    assert ("<strong>WPS-eligible 120–180 bp aligned-fragment depth</strong>"
            " — stability not validated") in dom
    assert "no split-half stability verdict" in dom
    assert "No validated stability deviation scale; measured values remain in the table." in dom
    assert "0.4" in dom


def test_sample_api_explains_unavailable_somalier(server, monkeypatch) -> None:
    from traceback_lab.envelope import ResultEnvelope

    original = api_module.views.load_sample

    def with_identity(store, source_id, **kwargs):
        sample = original(store, source_id, **kwargs)
        assert sample is not None
        provenance = next(iter(sample.full.results.values())).provenance
        sample.full.results["identity"] = ResultEnvelope(
            algorithm_id="identity", algorithm_version="1", status="skipped",
            skip_reason="missing_toolchain", skip_message="Somalier unavailable",
            provenance=provenance,
        )
        return sample

    monkeypatch.setattr(api_module.views, "load_sample", with_identity)
    monkeypatch.setattr(api_module, "somalier_tool", lambda: None)
    cookie = _session(server)
    id12 = json.loads(_get(server, "/api/samples", cookie)[1])["samples"][0]["id12"]
    response, data = _get(server, f"/api/samples/{id12}", cookie)
    payload = json.loads(data)
    assert response.status == 200
    assert payload["identity_pairs"] == []
    assert "Somalier is unavailable" in payload["identity_pairs_reason"]


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


def test_invalid_method_catalog_on_disk_keeps_last_good_without_path(monkeypatch, tmp_path) -> None:
    import yaml

    holder = api_module.LibraryHolder()
    good = holder.get()
    edited = yaml.safe_load(api_module.LIBRARY_FILE.read_text())
    edited["method_catalog"]["bam-qc"]["requirements"] = ["duplicate", "duplicate"]
    broken = tmp_path / "private" / "library.yaml"
    broken.parent.mkdir()
    broken.write_text(yaml.safe_dump(edited))
    monkeypatch.setattr(api_module, "LIBRARY_FILE", broken)
    assert holder.get() is good
    error = holder.health()["error"]
    assert "method_catalog/bam-qc" in error and "requirements" in error
    assert str(tmp_path) not in error


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


# Harness budgets. A hosted runner with three xdist workers each driving a
# Chrome can run at several times its core count; waits scale with the
# one-minute load average per CPU, never below the base and never above the
# cap, so an unloaded host keeps the historical timing and a real failure
# still surfaces well inside the 600-second test timeout.
_LOAD_SCALE_CAP = 2.0
_DISCOVERY_BUDGET = (15.0, 30.0)  # base seconds, cap seconds
_DOM_BUDGET_CAP = 240.0  # one _dom_cdp call stays well inside the 600 s pytest timeout
_COMMAND_BUDGET = (15.0, 30.0)  # one CDP response: base seconds, cap seconds
_DISCOVERY_PHASES = ("chrome-not-started", "devtools-unreachable", "no-page-target", "page-target-not-ours")


# Socket-level loss of the DevTools connection. A command that merely ran past
# its deadline (TimeoutError from _cdp_command, or the socket read timeout
# websocket-client raises at that same deadline) is a stalled renderer and
# must fail the test, so neither is in this tuple.
_TRANSPORT_ERRORS = (websocket.WebSocketConnectionClosedException, ConnectionError)


def _host_load() -> tuple[float | None, int]:
    """One-minute load average (None where the platform has none) and CPU count."""

    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    return load, os.cpu_count() or 1


def _scaled_budget(base: float, *, cap: float, load: float | None, cpus: int) -> float:
    """Grow ``base`` by load-per-CPU above 1.0, at most ``_LOAD_SCALE_CAP``, capped."""

    factor = 1.0
    if load is not None and cpus > 0:
        factor = max(1.0, min(_LOAD_SCALE_CAP, load / cpus))
    return max(min(base * factor, cap), min(base, cap))


def _wait_for_dom(read_state, *, timeout: float, clock=None, pause=None, load: float | None = None,
                  recover=None) -> dict:
    """Wait for a caller's actual DOM predicate within one deadline.

    The failure names the last observed state, the budget it used and the
    host load average so a loaded-runner stall reads differently from a page
    that never satisfied the predicate. When ``recover`` is given, ONE poll
    that loses the DevTools socket (a transport error, never a command
    timeout) re-attaches through it and the wait continues inside the same
    deadline; the predicate itself is never relaxed.
    """

    clock = clock or time.monotonic
    pause = pause or time.sleep
    start = clock()
    deadline = start + timeout
    state: dict = {"observed": False}
    recovered: str | None = None
    while True:
        try:
            state = read_state()
        except (TimeoutError, websocket.WebSocketException, ConnectionError) as exc:
            if (recover is None or recovered is not None or deadline - clock() <= 0
                    or not isinstance(exc, _TRANSPORT_ERRORS)):
                raise AssertionError(
                    f"Chrome DOM poll failed: {exc!r}; last state={state!r}; recovered={recovered!r}; "
                    f"budget={timeout:.1f}s, elapsed={clock() - start:.1f}s, load1={load!r}"
                ) from exc
            recovered = repr(exc)
            recover()
            continue
        if state.get("matched"):
            return state
        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(
                f"Chrome DOM predicate not met before timeout: {state!r}; recovered={recovered!r}; "
                f"budget={timeout:.1f}s, elapsed={clock() - start:.1f}s, load1={load!r}"
            )
        pause(min(0.2, remaining))


class ChromeTargetUnavailable(AssertionError):
    """DevTools never listed the harness page; ``phase`` says where it stalled."""

    def __init__(self, phase: str, detail: str, *, elapsed: float, budget: float) -> None:
        assert phase in _DISCOVERY_PHASES, phase
        self.phase = phase
        super().__init__(
            f"Chrome did not expose its Lab page through DevTools: phase={phase} ({detail}); "
            f"waited {elapsed:.1f}s of {budget:.1f}s"
        )


def _discover_page_target(*, read_port, fetch_targets, is_ours, process_status, budget: float,
                          clock=None, pause=None) -> tuple[str, str]:
    """Poll DevTools until a page target of ours appears; return (port, websocket url).

    ``read_port`` yields the DevToolsActivePort port or None; ``fetch_targets``
    returns the ``/json/list`` payload for a port or raises OSError/ValueError;
    ``process_status`` is Chrome's return code or None while it runs. The
    poll interval grows from 50 ms to 500 ms. A failure records which phase
    the discovery was in on its last observation.
    """

    clock = clock or time.monotonic
    pause = pause or time.sleep
    start = clock()
    deadline = start + budget
    interval = 0.05
    while True:
        returncode = process_status()
        port = read_port()
        if port is None:
            phase, detail = "chrome-not-started", f"no complete DevToolsActivePort; returncode={returncode!r}"
        else:
            try:
                targets = fetch_targets(port)
            except (OSError, ValueError) as exc:
                phase, detail = "devtools-unreachable", f"port {port}: {exc!r}"
            else:
                pages = [target for target in targets if target.get("type") == "page"]
                ours = next((target for target in pages
                             if target.get("webSocketDebuggerUrl") and is_ours(target.get("url", ""))), None)
                if ours is not None:
                    return port, ours["webSocketDebuggerUrl"]
                if pages:
                    phase = "page-target-not-ours"
                    detail = f"port {port}: page urls={[target.get('url') for target in pages]!r}"
                else:
                    phase = "no-page-target"
                    detail = f"port {port}: target types={[target.get('type') for target in targets]!r}"
        remaining = deadline - clock()
        if returncode is not None or remaining <= 0:
            raise ChromeTargetUnavailable(phase, detail, elapsed=clock() - start, budget=budget)
        pause(min(interval, remaining))
        interval = min(0.5, interval * 1.5)


def _attach_with_retry(attach, rediscover):
    """Attach to a DevTools page; one transient socket failure re-discovers and retries once."""

    try:
        return attach()
    except (OSError, websocket.WebSocketException) as first:
        try:
            return attach(rediscover())
        except (OSError, websocket.WebSocketException) as second:
            raise AssertionError(
                f"DevTools page attach failed twice: first={first!r}; retry={second!r}"
            ) from second


def _cdp_command(connection, method: str, params: dict, request_id: int, deadline: float,
                 events: deque[str], *, clock=None, cap: float = _COMMAND_BUDGET[0],
                 seen_events: set[str] | None = None) -> dict:
    """Bound one response to ``cap`` seconds even if unrelated console events keep arriving.

    A reply to an earlier, timed-out request id is skipped like any other
    unrelated message, so a later command never reads a stale result. With
    ``seen_events``, a console or log event already recorded is not recorded
    twice: ``Runtime.enable`` and ``Log.enable`` replay the page's existing
    console messages and log entries on a new session.
    """

    clock = clock or time.monotonic
    command_deadline = min(deadline, clock() + cap)
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
            if seen_events is not None:
                params = message.get("params") or {}
                if message["method"] == "Runtime.exceptionThrown":
                    # exceptionId and the timestamp are per-session; a replay
                    # after re-attach differs only in those.
                    details = params.get("exceptionDetails") or {}
                    params = {field: details.get(field) for field in ("text", "url", "lineNumber", "columnNumber")}
                    params["description"] = (details.get("exception") or {}).get("description")
                key = json.dumps([message["method"], params], sort_keys=True)
                if key in seen_events:
                    continue
                seen_events.add(key)
            events.append(json.dumps(message, ensure_ascii=False)[-500:])


def _read_debug_endpoint(path: Path) -> tuple[str, str] | None:
    """(port, browser websocket path) from DevToolsActivePort, or None while startup is ongoing."""

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].isdecimal() or not lines[1].startswith("/devtools/browser/"):
        return None
    return lines[0], lines[1]


def _read_debug_port(path: Path) -> str | None:
    """An empty or partly written Chrome port file means startup is ongoing."""

    endpoint = _read_debug_endpoint(path)
    return None if endpoint is None else endpoint[0]


def _list_processes() -> list[tuple[int, int, str, str]]:
    """(pid, pgid, stat, command) for every process; ``-ww`` keeps full command lines.

    BSD ps cuts ``command`` to the terminal width (about 79 columns without a
    tty), which would hide ``--user-data-dir``. ``stat`` lets zombies, which
    are already dead, be ignored.
    """

    listing = subprocess.run(
        ["ps", "-ww", "-A", "-o", "pid=,pgid=,stat=,command="],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout
    rows = []
    for row in listing.splitlines():
        fields = row.split(None, 3)
        if len(fields) >= 3 and fields[0].isdecimal() and fields[1].isdecimal():
            rows.append((int(fields[0]), int(fields[1]), fields[2], fields[3] if len(fields) == 4 else ""))
    return rows


def _sweep_launch_group(pgid: int, *, clock=None, pause=None,
                        settle: float = 2.0) -> tuple[list[int], list[int]]:
    """Kill, by pid, every live (non-zombie) member of OUR launch group ``pgid``.

    ``pgid`` is only ever the group ``start_new_session`` created for our own
    Chrome, so pgid == our leader's pid. A process-group id is not reused
    while the group has members, so every live member descends from our
    launch, whatever its command line (``chrome_crashpad_handler`` carries no
    ``--user-data-dir``). Members are signalled one by one because a reaped
    leader's pid is never a group target. Returns (killed, survivors):
    survivors are live members still listed after ``settle`` seconds. Raises
    if ps fails.
    """

    clock = clock or time.monotonic
    pause = pause or time.sleep

    def members() -> list[int]:
        return [pid for pid, group, stat, _ in _list_processes() if group == pgid and not stat.startswith("Z")]

    killed = []
    for pid in members():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        killed.append(pid)
    deadline = clock() + settle
    while True:
        survivors = members()
        if not survivors or clock() >= deadline:
            return killed, survivors
        pause(0.1)


def _stop_browser(process, *, sweep: str = "never", notes: list[str] | None = None) -> str | None:
    """Kill one launched Chrome (whole group on POSIX); return a cleanup error or None.

    A leader that is still running is killed with its group. A leader that
    already exited is never used as a group target, so its group is swept by
    pid instead (``_sweep_launch_group``) when ``sweep`` is "if-exited".
    "always" also sweeps after a group kill, to prove no member survives
    before the profile is reused. A member that survives, or a ps failure,
    is a cleanup error, because the profile may still be in use.
    """

    assert sweep in ("never", "if-exited", "always"), sweep

    cleanup_error = None
    returncode = process.poll()
    if returncode is not None:
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
    if os.name != "nt" and (sweep == "always" or (sweep == "if-exited" and returncode is not None)):
        # start_new_session made the leader's pid the group id at launch.
        try:
            killed, survivors = _sweep_launch_group(process.pid)
        except Exception as exc:
            return cleanup_error or f"launch group sweep failed: {exc!r}"
        if killed and notes is not None:
            notes.append(f"killed launch group {process.pid} members {killed}")
        if survivors:
            return cleanup_error or f"launch group {process.pid} still has live members {survivors}"
    return cleanup_error


def _stderr_tail(stderr) -> str:
    stderr.seek(0, os.SEEK_END)
    stderr.seek(max(0, stderr.tell() - 4096))
    return stderr.read().decode("utf-8", errors="replace")[-1000:]


def _dom_cdp(argv: list[str], profile: Path, *, ok, timeout: float = 90,
             action: str | None = None, interact=None,
             viewport: tuple[int, int] | None = None) -> str:
    """Wait for one Chrome page, optionally setting its CSS viewport before load.

    Chrome can clamp ``--window-size=360,740`` to a wider inner viewport. CDP
    emulation before navigation lets load-time layout probes test actual 360px.

    Chrome always gets ``--remote-debugging-port=0``; the ephemeral port and
    the browser identity come from ``DevToolsActivePort`` inside ``profile``,
    which is removed before every launch so a stale file from a killed
    browser can never point at another worker's Chrome. If Chrome never
    brings DevTools up within the discovery budget it is killed and launched
    once more; the page attach also retries once on a transient socket error.
    """

    profile_flag = f"--user-data-dir={profile}"
    for flag in argv:
        if flag.startswith("--user-data-dir="):
            assert flag == profile_flag, f"profile {profile} is not the --user-data-dir Chrome was given"
            break
    else:
        argv = [argv[0], profile_flag, *argv[1:]]
    page_url = argv[-1]
    destination = urllib.parse.urlsplit(page_url)
    origin = f"{destination.scheme}://{destination.netloc}"

    def is_ours(url: str) -> bool:
        if viewport is not None:
            return url == "about:blank"
        return url == page_url or url.startswith(origin + "/") or url.startswith(origin + "?") or url.startswith(origin + "#")

    load, cpus = _host_load()
    dom_budget = _scaled_budget(timeout, cap=max(timeout, min(2 * timeout, _DOM_BUDGET_CAP)), load=load, cpus=cpus)
    discovery_budget = _scaled_budget(_DISCOVERY_BUDGET[0], cap=_DISCOVERY_BUDGET[1], load=load, cpus=cpus)
    command_budget = _scaled_budget(_COMMAND_BUDGET[0], cap=_COMMAND_BUDGET[1], load=load, cpus=cpus)
    start = time.monotonic()
    deadline = start + dom_budget
    events: deque[str] = deque(maxlen=8)
    dom = ""
    failure: Exception | None = None
    cleanup_error = None
    connection = None
    launches: list[str] = []
    launched = 0
    seen_events: set[str] = set()
    active_port = profile / "DevToolsActivePort"
    direct_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    launch_argv = [*argv[:-1], "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                   "about:blank" if viewport else page_url]

    def fetch_targets(port: str) -> list[dict]:
        endpoint = _read_debug_endpoint(active_port)
        with direct_http.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            browser = json.load(response)
        browser_path = urllib.parse.urlsplit(browser.get("webSocketDebuggerUrl", "")).path
        if endpoint is None or browser_path != endpoint[1]:
            raise ValueError(f"DevTools on port {port} is not the browser that wrote DevToolsActivePort")
        with direct_http.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
            return json.load(response)

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = None
        try:
            for attempt in (1, 2):
                launched = attempt
                try:
                    active_port.unlink()
                except FileNotFoundError:
                    pass
                process = subprocess.Popen(
                    launch_argv, stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
                )
                try:
                    port, target_url = _discover_page_target(
                        read_port=lambda: _read_debug_port(active_port), fetch_targets=fetch_targets,
                        is_ours=is_ours, process_status=process.poll,
                        budget=max(0.0, min(discovery_budget, deadline - time.monotonic())),
                    )
                except ChromeTargetUnavailable as exc:
                    launches.append(f"attempt {attempt}: {exc}")
                    if attempt == 1 and exc.phase == "chrome-not-started" and deadline - time.monotonic() > 1:
                        stop_error = _stop_browser(process, sweep="always", notes=launches)
                        process = None
                        if stop_error is not None:
                            # The first launch may still hold the profile: never
                            # clear its lock or start a second Chrome on it.
                            cleanup_error = stop_error
                            raise AssertionError(
                                f"{exc} [{'; '.join(launches)}]; relaunch skipped: {stop_error}"
                            ) from exc
                        for singleton in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                            try:
                                (profile / singleton).unlink()
                            except FileNotFoundError:
                                pass
                        continue
                    raise AssertionError(f"{exc} [{'; '.join(launches)}]") from exc
                break
            target = urllib.parse.urlsplit(target_url)
            if target.scheme != "ws" or target.hostname != "127.0.0.1" or target.port != int(port):
                raise AssertionError("Chrome DevTools target is not on the expected loopback port")

            def attach(url: str = target_url):
                connect_timeout = min(5, max(0.1, deadline - time.monotonic()))
                # websocket-client 1.9.2 ignores http_no_proxy unless a proxy host
                # option is also supplied. An already-connected local socket avoids
                # proxy environment variables without broadening Chrome's origins.
                direct_socket = socket.create_connection(("127.0.0.1", target.port), timeout=connect_timeout)
                try:
                    return websocket.create_connection(
                        url, timeout=connect_timeout, suppress_origin=True,
                        http_no_proxy=["127.0.0.1", "localhost"], socket=direct_socket,
                    )
                except Exception:
                    direct_socket.close()
                    raise

            def rediscover() -> str:
                _, url = _discover_page_target(
                    read_port=lambda: _read_debug_port(active_port), fetch_targets=fetch_targets,
                    is_ours=is_ours, process_status=process.poll,
                    budget=max(0.0, min(5.0, deadline - time.monotonic())),
                )
                assert urllib.parse.urlsplit(url).port == target.port, "DevTools page moved to another port"
                return url

            connection = _attach_with_retry(attach, rediscover)
            command_id = 0

            def command(method: str, params: dict | None = None, *, until: float | None = None):
                nonlocal command_id
                command_id += 1
                return _cdp_command(connection, method, params or {}, command_id,
                                    deadline if until is None else until, events, cap=command_budget,
                                    seen_events=seen_events)

            def evaluate(expression: str, *, until: float | None = None):
                result = command("Runtime.evaluate", {"expression": expression, "returnByValue": True}, until=until)
                if "exceptionDetails" in result:
                    raise AssertionError(f"DevTools evaluation failed: {result['exceptionDetails']!r}")
                return result["result"].get("value")

            command("Runtime.enable")
            command("Log.enable")
            if viewport is not None:
                width, height = viewport
                command("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
                })
                assert evaluate("window.innerWidth") == width, "CDP did not set the requested CSS viewport"
                assert destination.scheme == "http" and destination.hostname == "127.0.0.1"
                command("Page.navigate", {"url": page_url})
            action_done = False
            def read_state() -> dict:
                nonlocal dom, action_done
                dom = evaluate("document.documentElement?.outerHTML ?? ''")
                if action and not action_done and 'data-ready="1"' in dom:
                    # Once sent, an action whose reply never arrived has an
                    # unknown outcome: fail (never re-attach and resend or skip it).
                    # A delivered reply other than true keeps the existing retry.
                    try:
                        action_done = evaluate(action) is True
                    except (TimeoutError, websocket.WebSocketException, ConnectionError) as exc:
                        raise AssertionError(f"one-time page action got no reply; not re-sent: {exc!r}") from exc
                    if action_done:
                        dom = evaluate("document.documentElement?.outerHTML ?? ''")
                return {
                    "matched": bool(ok(dom)), "ready": _body_attr(dom, "ready"),
                    "rendered": 'data-rendered="1"' in dom,
                    "csp": _body_attr(dom, "csp-violations"),
                    "fallback": "Chart could not be drawn" in dom,
                    "loading": "Loading…" in dom,
                }

            def reattach() -> None:
                # The page target outlives its socket: a fresh socket to the
                # same webSocketDebuggerUrl sees the same document and state.
                nonlocal connection
                stale, connection = connection, None
                try:
                    stale.close()
                except Exception:
                    pass
                connection = _attach_with_retry(attach, lambda: target_url)
                command("Runtime.enable")
                command("Log.enable")

            # Emulation.setDeviceMetricsOverride belongs to the CDP session: a
            # new socket would relayout the page at Chrome's own width, so a
            # viewport test never re-attaches after emulation and fails instead.
            _wait_for_dom(read_state, timeout=max(0, deadline - time.monotonic()), load=load,
                          recover=None if viewport is not None else reattach)
            if interact is not None:
                interact(command, evaluate)
                dom = evaluate("document.documentElement?.outerHTML ?? ''")
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
            if process is not None:
                cleanup_error = _stop_browser(process, sweep="if-exited", notes=launches) or cleanup_error
            stderr_tail = _stderr_tail(stderr)
    if failure or cleanup_error:
        raise AssertionError(
            f"Chrome DOM wait failed: {failure!r}; cleanup_error={cleanup_error!r}; "
            f"data_ready={_body_attr(dom, 'ready')}, csp_violations={_body_attr(dom, 'csp-violations')}, "
            f"fallback={'Chart could not be drawn' in dom}, console_tail={list(events)!r}, "
            f"stderr_tail={stderr_tail!r}; budget={dom_budget:.1f}s (base {timeout}s, "
            f"discovery {discovery_budget:.1f}s, command {command_budget:.1f}s, load1={load!r}, cpus={cpus}), "
            f"elapsed={time.monotonic() - start:.1f}s, launches={launched}, harness_notes={launches!r}"
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


def test_wait_budget_scales_with_load_per_cpu_between_base_and_cap() -> None:
    assert _scaled_budget(90, cap=180, load=None, cpus=4) == 90
    assert _scaled_budget(90, cap=180, load=2.0, cpus=4) == 90
    assert _scaled_budget(90, cap=180, load=6.0, cpus=4) == 135
    assert _scaled_budget(90, cap=180, load=40.0, cpus=4) == 180
    assert _scaled_budget(15, cap=30, load=9.0, cpus=3) == 30
    assert _scaled_budget(240, cap=300, load=12.0, cpus=3) == 300
    assert _scaled_budget(90, cap=180, load=1.0, cpus=0) == 90


def test_dom_wait_failure_reports_budget_elapsed_and_load() -> None:
    now = [10.0]
    state = {"ready": None, "rendered": False, "matched": False, "csp": None, "fallback": False, "loading": True}

    def pause(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(AssertionError) as caught:
        _wait_for_dom(lambda: state, timeout=1.5, clock=lambda: now[0], pause=pause, load=7.25)
    message = str(caught.value)
    assert "'loading': True" in message and "'ready': None" in message
    assert "budget=1.5s, elapsed=1.5s, load1=7.25" in message


def test_dom_wait_reattaches_once_after_a_lost_socket_and_keeps_the_predicate() -> None:
    now = [0.0]
    recoveries: list[float] = []
    observations = iter((
        websocket.WebSocketConnectionClosedException("socket is already closed."),
        {"matched": False, "ready": None},
        {"matched": True, "ready": "1"},
    ))

    def read_state() -> dict:
        item = next(observations)
        if isinstance(item, Exception):
            now[0] += 2
            raise item
        return item

    def pause(seconds: float) -> None:
        now[0] += seconds

    state = _wait_for_dom(read_state, timeout=30, clock=lambda: now[0], pause=pause, load=9.0,
                          recover=lambda: recoveries.append(now[0]))
    assert state == {"matched": True, "ready": "1"} and recoveries == [2]


def test_dom_wait_reports_a_second_lost_socket_with_the_last_state() -> None:
    now = [0.0]
    observations = iter((
        ConnectionResetError(54, "Connection reset by peer"),
        {"matched": False, "ready": "1", "loading": True},
        websocket.WebSocketConnectionClosedException("socket is already closed."),
    ))

    def read_state() -> dict:
        now[0] += 1
        item = next(observations)
        if isinstance(item, Exception):
            raise item
        return item

    def pause(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(AssertionError) as caught:
        _wait_for_dom(read_state, timeout=30, clock=lambda: now[0], pause=pause, load=4.5, recover=lambda: None)
    message = str(caught.value)
    assert message.startswith("Chrome DOM poll failed: WebSocketConnectionClosedException('socket is already closed.')")
    assert "last state={'matched': False, 'ready': '1', 'loading': True}" in message
    assert "recovered=\"ConnectionResetError(54, 'Connection reset by peer')\"" in message
    assert "budget=30.0s, elapsed=" in message and "load1=4.5" in message


@pytest.mark.parametrize("stall", [
    TimeoutError("DevTools Runtime.evaluate exceeded its deadline"),
    websocket.WebSocketTimeoutException("timed out"),
])
def test_dom_wait_never_recovers_from_a_command_that_ran_past_its_deadline(stall) -> None:
    recoveries: list[int] = []

    def read_state() -> dict:
        raise stall

    with pytest.raises(AssertionError, match=r"poll failed: .*last state=\{'observed': False\}; recovered=None"):
        _wait_for_dom(read_state, timeout=5, clock=lambda: 0.0, pause=lambda seconds: None,
                      recover=lambda: recoveries.append(1))
    assert recoveries == []


def test_dom_wait_without_recovery_fails_on_the_first_lost_socket() -> None:
    def read_state() -> dict:
        raise websocket.WebSocketConnectionClosedException("socket is already closed.")

    with pytest.raises(AssertionError, match=r"poll failed: .*last state=\{'observed': False\}; recovered=None"):
        _wait_for_dom(read_state, timeout=5, clock=lambda: 0.0, pause=lambda seconds: None)


def test_cdp_command_records_a_replayed_console_or_log_event_once() -> None:
    entry = {"source": "javascript", "level": "error", "text": "synthetic", "timestamp": 1.0}
    console = {"type": "log", "args": [{"type": "string", "value": "synthetic"}], "executionContextId": 1, "timestamp": 1.5}
    replies = iter((
        {"method": "Log.entryAdded", "params": {"entry": entry}},
        {"method": "Runtime.consoleAPICalled", "params": console},
        {"id": 1, "result": {}},
        {"method": "Runtime.consoleAPICalled", "params": console},
        {"method": "Log.entryAdded", "params": {"entry": entry}},
        {"method": "Log.entryAdded", "params": {"entry": {**entry, "timestamp": 2.0}}},
        {"method": "Runtime.consoleAPICalled", "params": {**console, "timestamp": 2.5}},
        {"id": 2, "result": {}},
    ))

    class ReplayingConnection:
        def settimeout(self, seconds: float) -> None:
            pass

        def send(self, payload: str) -> None:
            pass

        def recv(self) -> str:
            return json.dumps(next(replies))

    events: deque[str] = deque(maxlen=8)
    seen: set[str] = set()
    _cdp_command(ReplayingConnection(), "Runtime.enable", {}, 1, 90, events, clock=lambda: 0.0, seen_events=seen)
    _cdp_command(ReplayingConnection(), "Log.enable", {}, 2, 90, events, clock=lambda: 0.0, seen_events=seen)
    recorded = [json.loads(event) for event in events]
    assert [(event["method"], (event["params"].get("entry") or event["params"])["timestamp"]) for event in recorded] == [
        ("Log.entryAdded", 1.0), ("Runtime.consoleAPICalled", 1.5),
        ("Log.entryAdded", 2.0), ("Runtime.consoleAPICalled", 2.5),
    ]


def test_cdp_command_records_a_replayed_exception_once_despite_new_session_ids() -> None:
    def thrown(exception_id: int, timestamp: float, line: int) -> dict:
        return {"method": "Runtime.exceptionThrown", "params": {"timestamp": timestamp, "exceptionDetails": {
            "exceptionId": exception_id, "text": "Uncaught", "url": "http://127.0.0.1:8/app.js",
            "lineNumber": line, "columnNumber": 4, "exception": {"description": "TypeError: synthetic"}}}}

    replies = iter((thrown(1, 10.0, 7), {"id": 1, "result": {}}, thrown(9, 20.0, 7), thrown(10, 21.0, 8), {"id": 2, "result": {}}))

    class ReplayingConnection:
        def settimeout(self, seconds: float) -> None:
            pass

        def send(self, payload: str) -> None:
            pass

        def recv(self) -> str:
            return json.dumps(next(replies))

    events: deque[str] = deque(maxlen=8)
    seen: set[str] = set()
    for request_id in (1, 2):
        _cdp_command(ReplayingConnection(), "Runtime.enable", {}, request_id, 90, events, clock=lambda: 0.0, seen_events=seen)
    assert [json.loads(event)["params"]["exceptionDetails"]["lineNumber"] for event in events] == [7, 8]


class _ScriptedDevTools:
    """A fake Chrome session: one alive browser, sockets whose replies a script decides."""

    def __init__(self, monkeypatch, script) -> None:
        self.sockets: list[str] = []
        self.sent: list[tuple[int, str, dict]] = []
        self.signalled: list[int] = []
        browser = SimpleNamespace(pid=4242, poll=lambda: None, wait=lambda timeout: 0, kill=lambda: None)
        outer = self

        class Connection:
            def __init__(self) -> None:
                self.index = len(outer.sockets)
                self.queue: list[str] = []

            def settimeout(self, seconds: float) -> None:
                pass

            def send(self, payload: str) -> None:
                message = json.loads(payload)
                outer.sent.append((self.index, message["method"], message["params"]))
                result = script(self.index, message["method"], message["params"])
                self.queue.append("RAISE" if result == "RAISE" else json.dumps({"id": message["id"], "result": result}))

            def recv(self) -> str:
                reply = self.queue.pop(0)
                if reply.startswith("RAISE"):
                    raise outer.errors.pop(0)
                return reply

            def close(self) -> None:
                pass

        self.errors: list[Exception] = []

        def connect(url, **kwargs):
            outer.sockets.append(url)
            return Connection()

        module = sys.modules[__name__]
        monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: browser)
        monkeypatch.setattr(module, "_discover_page_target",
                            lambda **kwargs: ("4321", "ws://127.0.0.1:4321/devtools/page/A"))
        monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(websocket, "create_connection", connect)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: self.signalled.append(pgid))
        monkeypatch.setattr(module, "_list_processes", lambda: [])


def _drop_first_dom_poll(fake: _ScriptedDevTools, *, width: int | None = None):
    polls = [0]

    def script(index: int, method: str, params: dict):
        if method != "Runtime.evaluate":
            return {}
        expression = params["expression"]
        if expression == "window.innerWidth":
            return {"result": {"value": width}}
        if expression.startswith("document.documentElement"):
            polls[0] += 1
            if polls[0] == 1:
                fake.errors.append(websocket.WebSocketConnectionClosedException("socket is already closed."))
                return "RAISE"
            return {"result": {"value": '<html><body data-ready="1">synthetic-ok</body></html>'}}
        return {"result": {"value": True}}

    return script


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_cdp_reattaches_once_to_the_same_page_without_emulation(tmp_path, monkeypatch) -> None:
    holder: dict = {}
    fake = _ScriptedDevTools(monkeypatch, lambda *args: holder["script"](*args))
    holder["script"] = _drop_first_dom_poll(fake)
    dom = _dom_cdp(["chrome", "http://127.0.0.1:8/x"], tmp_path / "profile",
                   ok=lambda dom: "synthetic-ok" in dom, timeout=5)
    assert "synthetic-ok" in dom
    assert fake.sockets == ["ws://127.0.0.1:4321/devtools/page/A"] * 2
    assert [method for index, method, _ in fake.sent if index == 1][:2] == ["Runtime.enable", "Log.enable"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_cdp_never_reattaches_after_viewport_emulation(tmp_path, monkeypatch) -> None:
    holder: dict = {}
    fake = _ScriptedDevTools(monkeypatch, lambda *args: holder["script"](*args))
    holder["script"] = _drop_first_dom_poll(fake, width=360)
    with pytest.raises(AssertionError) as caught:
        _dom_cdp(["chrome", "http://127.0.0.1:8/x"], tmp_path / "profile",
                 ok=lambda dom: "synthetic-ok" in dom, timeout=5, viewport=(360, 740))
    message = str(caught.value)
    assert "Chrome DOM poll failed: WebSocketConnectionClosedException" in message and "recovered=None" in message
    assert "budget=" in message and "load1=" in message
    assert fake.sockets == ["ws://127.0.0.1:4321/devtools/page/A"]
    assert [method for _, method, _ in fake.sent].count("Emulation.setDeviceMetricsOverride") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_cdp_one_time_action_without_a_reply_fails_and_is_never_resent(tmp_path, monkeypatch) -> None:
    holder: dict = {}
    fake = _ScriptedDevTools(monkeypatch, lambda *args: holder["script"](*args))
    action = "(() => { synthetic_click(); return true; })()"

    def script(index: int, method: str, params: dict):
        if method != "Runtime.evaluate":
            return {}
        if params["expression"] == action:
            fake.errors.append(websocket.WebSocketConnectionClosedException("socket is already closed."))
            return "RAISE"
        return {"result": {"value": '<html><body data-ready="1">waiting</body></html>'}}

    holder["script"] = script
    with pytest.raises(AssertionError, match="one-time page action got no reply; not re-sent"):
        _dom_cdp(["chrome", "http://127.0.0.1:8/x"], tmp_path / "profile",
                 ok=lambda dom: "never" in dom, timeout=5, action=action)
    assert [params.get("expression") for _, _, params in fake.sent].count(action) == 1
    assert len(fake.sockets) == 1


def test_cdp_command_cap_is_the_scaled_budget_not_a_fixed_fifteen_seconds() -> None:
    now = [0.0]

    class SilentConnection:
        def settimeout(self, seconds: float) -> None:
            assert 0 < seconds <= 27

        def send(self, payload: str) -> None:
            pass

        def recv(self) -> str:
            now[0] += 3
            return json.dumps({"id": 0, "result": {"stale": True}})

    with pytest.raises(TimeoutError, match="exceeded its deadline"):
        _cdp_command(SilentConnection(), "Runtime.evaluate", {}, 1, 90, deque(maxlen=8),
                     clock=lambda: now[0], cap=27)
    assert now[0] == 27


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def _discover(clock: _FakeClock, **kwargs):
    defaults = dict(read_port=lambda: "4321", fetch_targets=lambda port: [], is_ours=lambda url: url == "http://127.0.0.1:8/x",
                    process_status=lambda: None, budget=2.0, clock=clock, pause=clock.pause)
    return _discover_page_target(**{**defaults, **kwargs})


def test_target_discovery_names_a_browser_that_never_started() -> None:
    clock = _FakeClock()
    with pytest.raises(ChromeTargetUnavailable) as caught:
        _discover(clock, read_port=lambda: None)
    assert caught.value.phase == "chrome-not-started"
    assert "phase=chrome-not-started (no complete DevToolsActivePort; returncode=None); waited 2.0s of 2.0s" in str(caught.value)
    assert clock.pauses[0] == 0.05 and max(clock.pauses) <= 0.5 and sum(clock.pauses) == pytest.approx(2.0)


def test_target_discovery_stops_as_soon_as_chrome_exits() -> None:
    clock = _FakeClock()
    with pytest.raises(ChromeTargetUnavailable, match=r"returncode=21\); waited 0.0s of 2.0s"):
        _discover(clock, read_port=lambda: None, process_status=lambda: 21)
    assert clock.pauses == []


def test_target_discovery_names_an_unreachable_devtools_port() -> None:
    clock = _FakeClock()

    def refused(port: str) -> list[dict]:
        raise OSError(61, "Connection refused")

    with pytest.raises(ChromeTargetUnavailable) as caught:
        _discover(clock, fetch_targets=refused)
    assert caught.value.phase == "devtools-unreachable"
    assert "port 4321: ConnectionRefusedError(61, 'Connection refused')" in str(caught.value)


def test_target_discovery_names_a_browser_without_a_page_target() -> None:
    clock = _FakeClock()
    with pytest.raises(ChromeTargetUnavailable) as caught:
        _discover(clock, fetch_targets=lambda port: [{"type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/"}])
    assert caught.value.phase == "no-page-target"
    assert "target types=['browser_ui']" in str(caught.value)


def test_target_discovery_names_a_page_target_that_is_not_ours() -> None:
    clock = _FakeClock()
    stranger = [{"type": "page", "url": "http://127.0.0.1:9/other", "webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/A"}]
    with pytest.raises(ChromeTargetUnavailable) as caught:
        _discover(clock, fetch_targets=lambda port: stranger)
    assert caught.value.phase == "page-target-not-ours"
    assert "page urls=['http://127.0.0.1:9/other']" in str(caught.value)


def test_target_discovery_returns_our_page_once_it_appears() -> None:
    clock = _FakeClock()
    listings = iter((
        [],
        [{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/A"}],
        [{"type": "page", "url": "http://127.0.0.1:8/x", "webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/A"}],
    ))
    assert _discover(clock, fetch_targets=lambda port: next(listings)) == ("4321", "ws://127.0.0.1:4321/devtools/page/A")
    assert len(clock.pauses) == 2 and all(0 < seconds <= 0.5 for seconds in clock.pauses)


def test_page_attach_retries_once_after_a_transient_socket_failure() -> None:
    attempts: list[str] = []

    def attach(url: str = "ws://127.0.0.1:1/devtools/page/first"):
        attempts.append(url)
        if len(attempts) == 1:
            raise websocket.WebSocketBadStatusException("Handshake status 500", 500)
        return "connection"

    assert _attach_with_retry(attach, lambda: "ws://127.0.0.1:1/devtools/page/second") == "connection"
    assert attempts == ["ws://127.0.0.1:1/devtools/page/first", "ws://127.0.0.1:1/devtools/page/second"]


def test_page_attach_gives_up_after_the_second_socket_failure() -> None:
    def attach(url: str = "ws://127.0.0.1:1/devtools/page/first"):
        raise ConnectionRefusedError(61, "Connection refused")

    with pytest.raises(AssertionError, match="attach failed twice: first=ConnectionRefusedError.*retry=ConnectionRefusedError"):
        _attach_with_retry(attach, lambda: "ws://127.0.0.1:1/devtools/page/second")


class _ProcessTable:
    def __init__(self, rows: dict[int, tuple[int, str, str]], *, dies: bool) -> None:
        self.rows, self.dies, self.signalled = rows, dies, []

    def listing(self) -> list[tuple[int, int, str, str]]:
        return [(pid, group, stat, command) for pid, (group, stat, command) in self.rows.items()]

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.rows:
            raise ProcessLookupError(pid)
        self.signalled.append(pid)
        if self.dies:
            del self.rows[pid]


_CRASHPAD_ONLY = "crashpad-only"


def _synthetic_process_table(monkeypatch, profile: Path, *, dies: bool = True, rows=None) -> _ProcessTable:
    if rows == _CRASHPAD_ONLY:
        # The only live member of the launch group lacks --user-data-dir.
        rows = {
            5005: (1234, "S", "/synthetic/chrome_crashpad_handler --database=/synthetic/Crashpad --monitor-self"),
            5003: (77, "S", f"/synthetic/Chrome Helper --user-data-dir={profile}"),
            5004: (1234, "Z", "<defunct>"),
        }
    table = _ProcessTable(rows or {
        5001: (1234, "S", f"/synthetic/Chrome Helper --type=renderer --user-data-dir={profile} --lang=en"),
        5002: (1234, "S", "/synthetic/Chrome Helper --type=gpu-process"),
        5003: (77, "S", f"/synthetic/Chrome Helper --user-data-dir={profile}"),
        5004: (1234, "Z", "<defunct>"),
    }, dies=dies)
    monkeypatch.setattr(sys.modules[__name__], "_list_processes", table.listing)
    monkeypatch.setattr(os, "kill", table.kill)
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("reaped browser PID was signalled"))
    return table


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_end_of_call_sweep_runs_only_for_a_leader_that_already_exited(tmp_path, monkeypatch) -> None:
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append(pgid))
    monkeypatch.setattr(sys.modules[__name__], "_list_processes",
                        lambda: pytest.fail("a group kill of a running leader needs no ps listing"))
    running = SimpleNamespace(pid=4242, poll=lambda: None, wait=lambda timeout: 0)
    assert _stop_browser(running, sweep="if-exited") is None and signalled == [4242]

    table = _synthetic_process_table(monkeypatch, tmp_path / "profile", rows=_CRASHPAD_ONLY)
    notes: list[str] = []
    exited = SimpleNamespace(pid=1234, poll=lambda: 0, wait=lambda timeout: 0)
    assert _stop_browser(exited, sweep="if-exited", notes=notes) is None
    assert table.signalled == [5005] and notes == ["killed launch group 1234 members [5005]"]


def test_process_listing_asks_ps_for_full_width_command_lines(monkeypatch) -> None:
    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(stdout=" 5001  1234 S    /synthetic/Chrome --user-data-dir=/p --x\n 5004 1234 Z <defunct>\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert _list_processes() == [(5001, 1234, "S", "/synthetic/Chrome --user-data-dir=/p --x"), (5004, 1234, "Z", "<defunct>")]
    assert seen[0][:3] == ["ps", "-ww", "-A"] and "pid=,pgid=,stat=,command=" in seen[0]


def test_launch_group_sweep_kills_every_live_member_of_the_launch_group(tmp_path, monkeypatch) -> None:
    table = _synthetic_process_table(monkeypatch, tmp_path / "profile")
    clock = _FakeClock()
    assert _sweep_launch_group(1234, clock=clock, pause=clock.pause) == ([5001, 5002], [])
    assert table.signalled == [5001, 5002] and 5003 in table.rows


def test_launch_group_sweep_kills_a_member_without_user_data_dir(tmp_path, monkeypatch) -> None:
    table = _synthetic_process_table(monkeypatch, tmp_path / "profile", rows=_CRASHPAD_ONLY)
    clock = _FakeClock()
    assert _sweep_launch_group(1234, clock=clock, pause=clock.pause) == ([5005], [])
    assert table.signalled == [5005] and 5003 in table.rows


def test_launch_group_sweep_reports_members_that_survive_the_settle_time(tmp_path, monkeypatch) -> None:
    _synthetic_process_table(monkeypatch, tmp_path / "profile", dies=False)
    clock = _FakeClock()
    assert _sweep_launch_group(1234, clock=clock, pause=clock.pause, settle=1.0) == (
        [5001, 5002], [5001, 5002])
    assert 1.0 <= clock.now <= 1.2


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("rows, survivors", [(None, "[5001, 5002]"), (_CRASHPAD_ONLY, "[5005]")])
def test_cdp_skips_the_relaunch_while_the_first_launch_group_survives(tmp_path, monkeypatch, rows, survivors) -> None:
    launched: list[list[str]] = []
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("synthetic-host-1234")
    _synthetic_process_table(monkeypatch, profile, dies=False, rows=rows)
    browser = SimpleNamespace(pid=1234, poll=lambda: 1, wait=lambda timeout: 1)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: launched.append(argv) or browser)
    monkeypatch.setattr(sys.modules[__name__], "_host_load", lambda: (100.0, 1))
    with pytest.raises(AssertionError) as caught:
        _dom_cdp(["chrome", "http://127.0.0.1:8/x"], profile, ok=lambda dom: True, timeout=5)
    message = str(caught.value)
    assert len(launched) == 1 and os.path.lexists(profile / "SingletonLock")
    assert f"relaunch skipped: launch group 1234 still has live members {survivors}" in message
    assert "phase=chrome-not-started" in message and "launches=1" in message


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_cdp_relaunches_once_when_chrome_never_brings_devtools_up(tmp_path, monkeypatch) -> None:
    launched: list[list[str]] = []
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("1234\n/devtools/browser/stale\n")

    class ExitedBrowser:
        pid = 1234

        def __init__(self, argv) -> None:
            launched.append(argv)
            assert not (profile / "DevToolsActivePort").exists(), "stale DevToolsActivePort survived into a launch"
            if len(launched) == 2:
                assert not os.path.lexists(profile / "SingletonLock"), "relaunch shares the first launch's profile lock"

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float) -> int:
            return 1

    table = _synthetic_process_table(monkeypatch, profile)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: ExitedBrowser(argv))
    (profile / "SingletonLock").symlink_to("synthetic-host-1234")
    with pytest.raises(AssertionError) as caught:
        _dom_cdp(["chrome", "http://127.0.0.1:8/x"], profile, ok=lambda dom: True, timeout=5)
    message = str(caught.value)
    assert len(launched) == 2 and launched[0] == launched[1]
    # Every live member of the first launch's group is killed by pid (the
    # helper without --user-data-dir too) although the leader exited; another
    # group on the same profile and the zombie are left alone.
    assert table.signalled == [5001, 5002]
    assert not os.path.lexists(profile / "SingletonLock")
    assert "harness_notes=" in message and "killed launch group 1234 members [5001, 5002]" in message
    assert launched[0][:2] == ["chrome", f"--user-data-dir={profile}"] and launched[0][-1] == "http://127.0.0.1:8/x"
    assert "--remote-debugging-port=0" in launched[0] and not any(flag.startswith("--remote-debugging-port=") and flag != "--remote-debugging-port=0" for flag in launched[0])
    assert "attempt 1: Chrome did not expose its Lab page through DevTools: phase=chrome-not-started" in message
    assert "attempt 2: Chrome did not expose" in message and "returncode=1" in message
    assert "launches=2" in message and "budget=" in message and "load1=" in message


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
def test_reaped_browser_is_never_a_group_target_on_a_loaded_host(tmp_path, monkeypatch) -> None:
    # A load-scaled budget opens the relaunch path even for timeout=1; the
    # reaped leader must still never be signalled as a group.
    monkeypatch.setattr(sys.modules[__name__], "_host_load", lambda: (100.0, 1))
    test_cdp_does_not_signal_an_already_reaped_browser(tmp_path, monkeypatch)


def test_cdp_refuses_a_profile_that_differs_from_the_chrome_flag(tmp_path) -> None:
    with pytest.raises(AssertionError, match="is not the --user-data-dir Chrome was given"):
        _dom_cdp(["chrome", "--user-data-dir=/elsewhere", "http://127.0.0.1:8/x"], tmp_path / "profile", ok=lambda dom: True, timeout=1)


def test_cdp_target_match_is_the_launch_origin_or_about_blank_under_emulation(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        raise ChromeTargetUnavailable("no-page-target", "captured", elapsed=0.0, budget=0.0)

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: SimpleNamespace(pid=1, poll=lambda: 1, wait=lambda timeout: 1))
    monkeypatch.setattr(sys.modules[__name__], "_discover_page_target", capture)
    with pytest.raises(AssertionError, match="phase=no-page-target"):
        _dom_cdp(["chrome", "http://127.0.0.1:8/?token=t#/route"], tmp_path / "profile", ok=lambda dom: True, timeout=1)
    is_ours = seen["is_ours"]
    assert is_ours("http://127.0.0.1:8/?token=t#/route") and is_ours("http://127.0.0.1:8/#/other")
    assert not is_ours("http://127.0.0.1:9/?token=t") and not is_ours("about:blank") and not is_ours("http://127.0.0.1:80/")
    with pytest.raises(AssertionError, match="phase=no-page-target"):
        _dom_cdp(["chrome", "http://127.0.0.1:8/"], tmp_path / "profile", ok=lambda dom: True, timeout=1, viewport=(360, 740))
    assert seen["is_ours"]("about:blank") and not seen["is_ours"]("http://127.0.0.1:8/")


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless CSP check")
def test_headless_page_renders_a_chart_with_zero_csp_violations(server, tmp_path) -> None:
    id12 = api_module.LabApi(server.app.store).samples()["samples"][0]["id12"]
    dom = _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1280,800",
         server.issue_link() + f"&route=/sample/{id12}/trust-qc"], tmp_path / "profile",
        ok=lambda page: 'data-ready="1"' in page and 'class="echart"' in page and 'data-rendered="1"' in page
            and "step_end read-summary status=ok" in page,
        action="(() => { const button = document.querySelector('#main button[aria-label=\"Run log line for read-summary\"]'); if (!button) return false; button.click(); return true; })()",
    )
    assert 'data-ready="1"' in dom, dom[:500]
    assert 'data-csp-violations="0"' in dom
    assert 'data-rendered="1"' in dom  # the ECharts density panel drew under the CSP
    assert "Data table" in dom
    assert "Run log line →" in dom
    assert 'aria-label="Run log line for read-summary"' in dom
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


def test_torn_final_log_append_preserves_interrupted_status(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id, step="bam-qc"):
        log = store.run_log(run_id)
        log.append("run_start", command="run")
        log.append("sample", source_id="a" * 64, input_id="b" * 64)
        log.append("step_start", step="bam-qc")
    with log.path.open("ab") as stream:
        stream.write(b'{"event":"step_end"')
    status = api_module.LabApi(store).status()
    assert status["run_log_unavailable"] is None
    assert status["interrupted"] == {"run_id": run_id, "step": "bam-qc",
                                     "source_id": "a" * 64, "input_id": "b" * 64}


def test_run_log_reader_only_ignores_an_unterminated_final_fragment(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    run_id = "r-20261015-0101-abcd"
    log = store.run_log(run_id)
    log.append("run_start", command="run")
    prefix = log.path.read_bytes()
    log.path.write_bytes(prefix + b'{"event":')
    assert api_module._run_events(store, run_id)[1] is None
    log.path.write_bytes(prefix + b'{"event":\n')
    assert api_module._run_events(store, run_id) == ([], "malformed_or_empty")
    log.path.write_bytes(prefix + b'{"event":\n' + prefix)
    assert api_module._run_events(store, run_id) == ([], "malformed_or_empty")
    log.path.write_bytes(prefix + b'{"event":"step_end","run_id":"' + b"e" * 25 + b'"}')
    assert api_module._run_events(store, run_id) == ([], "malformed_or_empty")


@pytest.mark.parametrize(("damage", "reason"), (
    ("missing", "missing"),
    ("oversized", "unreadable_or_over_1mib"),
    ("too_many_lines", "over_4096_lines"),
    ("malformed", "malformed_or_empty"),
    ("deeply_nested", "malformed_or_empty"),
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
    if damage == "missing":
        path.unlink()
    elif damage == "oversized":
        path.write_bytes(b"x" * (1024 * 1024 + 1))
    elif damage == "too_many_lines":
        path.write_bytes(b"{}\n" * 4097)
    elif damage == "malformed":
        path.write_bytes(b'{"event":"run_start"}\n{broken\n')
    elif damage == "deeply_nested":
        payload = b"[" * 4000 + b"0" + b"]" * 4000
        with pytest.raises(RecursionError):
            json.loads(payload)
        path.write_bytes(payload + b"\n")
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


@pytest.mark.parametrize(("command", "step"), (
    ("cohort add", None), ("cohort remove", None),
    ("asset register-clock-map", "epi-clock-map"),
    ("asset griffin-register", "griffin"),
    ("asset end-catalogues-register", "aligned-ends"),
    ("microbial db add", "database"),
    ("fetal register", "fetal-fraction"),
    ("fetal chrY register", "fetal-fraction-chry"),
    ("donor register", "dd-cfdna-chry"),
))
def test_status_does_not_require_run_logs_from_other_writer_commands(tmp_path, command, step) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer(command, run_id, step=step):
        active = api_module.LabApi(store).status()
        assert active["active"]["command"] == command
        assert active["active"]["step"] == step
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


def test_status_does_not_echo_malformed_active_run_id(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    monkeypatch.setattr(store, "status", lambda: {"generation": 0, "active": {
        "command": "run", "run_id": "invalid/secret", "step": "bam-qc",
    }})
    status = api_module.LabApi(store).status()
    assert status["run_log_unavailable"] == {"run_id": None, "reason": "invalid_run_id"}
    assert status["active"]["run_id"] is None
    assert "invalid/secret" not in json.dumps(status)


def test_status_does_not_echo_malformed_nonanalysis_run_id(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    monkeypatch.setattr(store, "status", lambda: {"generation": 0, "active": {
        "command": "cohort add", "run_id": "invalid/secret", "step": "start",
    }})
    status = api_module.LabApi(store).status()
    assert status["active"]["run_id"] is None
    assert "invalid/secret" not in json.dumps(status)


def test_status_keeps_derived_step_and_hides_malformed_heartbeat_labels(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    monkeypatch.setattr(store, "status", lambda: {"generation": 0, "active": {
        "command": "run", "run_id": run_id, "step": "bam-qc@split-A",
    }})
    assert api_module.LabApi(store).status()["active"]["step"] == "bam-qc@split-A"
    monkeypatch.setattr(store, "status", lambda: {"generation": 0, "active": {
        "command": "private/sentinel", "run_id": run_id, "step": "private/sentinel",
    }})
    status = api_module.LabApi(store).status()
    assert status["active"]["command"] is None and status["active"]["step"] is None
    assert "private/sentinel" not in json.dumps(status)


def test_status_hides_malformed_interrupted_step(tmp_path) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261015-0101-abcd"
    with store.writer("run", run_id):
        log = store.run_log(run_id)
        log.append("run_start", command="run")
        log.append("step_start", step="private/sentinel")
    status = api_module.LabApi(store).status()
    assert status["interrupted"]["step"] is None
    assert "private/sentinel" not in json.dumps(status)


def test_sample_api_hides_malformed_result_name_but_reports_damage(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source_id, input_id = "a" * 64, "b" * 64
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": True,
        "inputs": [{"input_id": input_id, "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    manifest = store.inputs_dir / input_id / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"input_id": input_id}))
    malformed = store.results_dir / input_id / "private.sentinel" / ("c" * 64)
    malformed.mkdir(parents=True)
    (malformed / "selected.json").write_text('{"attempt_id":"invalid"}')
    known = store.results_dir / input_id / "fragment-size" / ("d" * 64)
    known.mkdir(parents=True)
    (known / "selected.json").write_text('{"attempt_id":"invalid"}')
    api = api_module.LabApi(store)
    sample = api.sample(source_id[:12])
    row = api.samples()["samples"][0]
    assert sample["malformed_result_selections"] and row["malformed_result_selections"]
    assert sample["unreadable_results"] == row["unreadable_results"] == ["fragment-size"]
    assert "private.sentinel" not in json.dumps(sample)
    assert "private.sentinel" not in json.dumps(row)


def test_status_reports_deeply_nested_latest_run_pointer_without_crashing(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.last_run_file.write_bytes(b"[" * 1200 + b"0" + b"]" * 1200)
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
@pytest.mark.parametrize("kind", ("active", "active-pending", "active-derived", "active-before-sample", "interrupted",
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
            "catalog": [{"algorithm_id": "bam-qc", "label": "BAM QC", "status": "ok",
                         "skip_reason": None}],
            "library_health": {"no_library_entry": 0, "unverified_citations": 0,
                               "version": "fixture", "error": None},
        }
    marker = {"run_id": "r-20261015-0101-abcd", "command": "run", "step": "bam-qc",
              "source_id": source_a, "input_id": input_a}
    if kind == "active-pending":
        marker["step"] = "read-summary"
    if kind == "active-derived":
        marker["step"] = "bam-qc@split-A"
    shown_marker = {key: value for key, value in marker.items()
                    if kind not in ("active-before-sample", "active-unlogged-prior-badlog",
                                    "active-unlogged-prior-interrupted") or key not in ("source_id", "input_id")}
    if kind == "active-before-sample":
        shown_marker["step"] = None
    prior_run_id = "r-20261015-0101-eeee"
    if kind.startswith("active-unlogged"):
        shown_marker = {"run_id": "r-20261015-0101-dddd", "command": "cohort add", "step": "cohort add"}
    with RunningLabServer(LabStore(tmp_path / "root")) as running:
        running.app.api.status = lambda: {
            "generation": 1, "active": {**shown_marker, "started_utc": "2026-10-16T00:00:00Z"} if kind.startswith("active") else None,
            "interrupted": shown_marker if kind == "interrupted" else (
                {"run_id": prior_run_id, "step": "read-summary", "source_id": source_a, "input_id": input_a}
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
    assert re.search(r'<th scope="row">bam-qc</th><td[^>]*>ok(?:\s|<)', run_table)
    if kind == "active-pending":
        assert ("Latest run in progress during read-summary; no result was published for this step." in dom) == (selected == "a")
    if kind == "active-derived":
        assert "step: bam-qc@split-A" in dom
        assert "no result was published for this step" not in run_table
    note = "latest run " + ("interrupted" if kind == "interrupted" else "in progress")
    assert (note in run_table) == (selected == "a" and kind in ("active", "interrupted"))
    if kind == "active-before-sample":
        assert "step: starting" in dom and "step: null" not in dom
    if kind.startswith("active-unlogged"):
        assert "Lab cohort add is in progress" in dom
        assert "A Lab run is in progress" not in dom
    if kind == "active-unlogged-prior-badlog":
        assert f"Run log for {prior_run_id} is unavailable (log malformed or empty)" in dom
        assert "Run log status unavailable" not in dom
    if kind == "active-unlogged-prior-interrupted":
        assert f"Earlier run ({prior_run_id}) stopped during read-summary" in dom
        assert ("Earlier run interrupted during read-summary; no result was published for this step." in dom) == (selected == "a")


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
        pinned = f"{path}/{input_id}/{identity}/{attempt_id}"
        assert json.loads(_get(running, pinned, cookie)[1])["run_id"] == run_id
        assert _get(running, f"{path}/{'e' * 64}/{identity}/{attempt_id}", cookie)[0].status == 404
        assert _get(running, f"{path}/{input_id}/{'d' * 64}/{attempt_id}", cookie)[0].status == 404
        assert _get(running, f"{path}/{input_id}/{identity}/a-20261015T010100000000-deadbeef", cookie)[0].status == 404
        with log.path.open("ab") as stream:
            stream.write(b'{"event":"step_end"')
        assert json.loads(_get(running, path, cookie)[1])["line"] == 3
        assert "/secret/path" not in data.decode()
        sample.full.results["sex-check"] = _selected_sex_result(source_id, input_id, "d" * 64)
        assert _get(running, path, cookie)[0].status == 404
        assert _get(running, pinned, cookie)[0].status == 404
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
    new.append("run_start", command="run")
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
    assert api.run_log_line(source_id[:12], "sex-check", input_id, identity, first) == {"found": False}
    assert api.run_log_line(source_id[:12], "sex-check", input_id, identity, second)["run_id"] == new.run_id
    latest = store.run_log("r-20261015-0101-0000")
    latest.append("run_start", command="run")
    latest.append("sample", source_id=source_id, input_id=input_id)
    latest.append("step_end", step="sex-check", parameters_sha256=identity, input_id=input_id,
                  attempt_id=second, status="ok")
    os.utime(latest.path, ns=(300_000_000_000, 300_000_000_000))
    assert api.run_log_line(source_id[:12], "sex-check")["run_id"] == latest.run_id
    latest.path.unlink()
    new.path.unlink()
    legacy = store.run_log("r-20261015-0101-bbbb")
    legacy.append("run_start", command="run")
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
