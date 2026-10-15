"""S6a: the Lab loopback server — session reuse, Host checks, containment, CSP."""

from __future__ import annotations

from traceback_lab.interpretation.engine import load_library

import http.client
import io
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

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


def test_serve_command_prints_root_first_and_warns_on_an_empty_root(tmp_path) -> None:
    from traceback_lab.serve_command import serve_command

    class Args:
        open_browser = False

    out = io.StringIO()
    stop = threading.Event()
    seen = []
    thread = threading.Thread(
        target=serve_command,
        args=(Args(), LabStore(tmp_path / "empty")),
        kwargs={"out": out, "stdin": io.StringIO(""), "stop": stop, "ready": lambda s: (seen.append(s), stop.set())},
    )
    thread.start()
    thread.join(timeout=20)
    lines = out.getvalue().splitlines()
    assert lines[0] == f"ROOT  {tmp_path / 'empty'}"
    assert "has no Lab runs" in lines[1]
    assert lines[2].startswith("http://127.0.0.1:") and "#bootstrap=" in lines[2]


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


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless CSP check")
def test_headless_page_renders_a_chart_with_zero_csp_violations(server, tmp_path) -> None:
    dom = ""
    id12 = api_module.LabApi(server.app.store).samples()["samples"][0]["id12"]
    for attempt in range(3):  # a loaded CI host can outrun the virtual-time budget once
        link = server.issue_link() + f"&route=/sample/{id12}/trust-qc"
        completed = subprocess.run(
            [
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={tmp_path / f'profile-{attempt}'}", "--virtual-time-budget=15000",
                "--window-size=1280,800", "--dump-dom", link,
            ],
            capture_output=True, text=True, timeout=120,
        )
        dom = completed.stdout
        if 'data-rendered="1"' in dom:
            break
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
