"""``traceback serve`` (golden-path B6): ROOT's jobs and catalog over loopback.

An operator session reads the catalog; a reader session gets 403
``TBX-AUTH-007`` on every explorer and jobs route (H1).  Every record here is
generated, unqualified, local and not for clinical use.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

import pytest

from tests.web.test_loopback_server import _exchange, _request
from traceback_runner import cli
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_catalog import explorer_paths
from traceback_runner.web import server as server_module
from traceback_runner.web.auth import READER_SESSION
from traceback_runner.web.server import RunningLocalWebService

AUTH_007 = {"error": {"code": "TBX-AUTH-007"}}


def _main_json(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


def _main_text(*argv: object) -> tuple[int, str]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv)])
    return code, stream.getvalue()


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory):
    """One ROOT with one run, imported into the catalog."""

    work = tmp_path_factory.mktemp("serve-base")
    inputs = create_local_golden_path_inputs(work / "inputs")
    root = work / "root"
    code, payload = _main_json(
        "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    code, payload = _main_json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == 0, payload
    record_id, job_id = payload["data"]["record_id"], payload["data"]["job_id"]
    code, payload = _main_json("catalog", "import", root / "records" / record_id, "--root", root)
    assert code == 0, payload
    return root, payload["data"]["result_id"], job_id


@pytest.fixture
def world(base, tmp_path: Path):
    root, result_id, job_id = base
    copy = tmp_path / "root"
    shutil.copytree(root, copy, symlinks=True)
    return copy, result_id, job_id


@contextmanager
def _serving(root: Path) -> Iterator[tuple[RunningLocalWebService, io.StringIO]]:
    """Run ``cli._serve`` in a thread with closed (non-TTY) stdin."""

    stop = threading.Event()
    ready: queue.Queue[RunningLocalWebService | None] = queue.Queue()
    out = io.StringIO()
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["code"] = cli._serve(
                argparse.Namespace(root=root, ipv6=False),
                out=out,
                stdin=io.StringIO(""),
                stop=stop,
                ready=ready.put,
            )
        except BaseException as exc:  # surfaced below
            result["error"] = exc
            ready.put(None)

    thread = threading.Thread(target=target, name="test-serve")
    thread.start()
    service = ready.get(timeout=60)
    try:
        assert service is not None, result
        yield service, out
    finally:
        stop.set()
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert result == {"code": 0}, result
    assert out.getvalue().rstrip().endswith("Stopped; the web lock for ROOT is released.")


def _get(service: RunningLocalWebService, path: str, cookie: str) -> tuple[int, dict]:
    status, _, content = _request(service, "GET", path, headers={"Cookie": cookie})
    return status, json.loads(content) if content else {}


def _reader_cookie(service: RunningLocalWebService) -> str:
    """A reader session on serve's own server (serve itself never issues one)."""

    runtime = server_module._RUNTIMES[service._runtime_id]
    code = runtime.boundary.issue_bootstrap(
        kind=READER_SESSION, launch_credential_sha256=bytes(32)
    )
    cookie, _ = _exchange(service, code)
    return cookie


def test_operator_session_lists_the_record_and_the_job(world) -> None:
    root, result_id, job_id = world
    with _serving(root) as (service, out):
        first = out.getvalue().splitlines()[0]
        assert first == service.launch_url
        assert first.startswith(f"{service.base_url}/#bootstrap=") and "?" not in first
        assert "Serving 1 cataloged record view(s). Unqualified, local" in out.getvalue()
        cookie, _ = _exchange(service)
        status, page = _get(service, "/api/v1/explorer/catalog?limit=10", cookie)
        assert status == 200
        rows = {item["ref"]["result_id"]: item for item in page["results"]}
        assert rows[result_id]["ref"]["qualification_state"] == "development_unqualified"
        assert rows[result_id]["has_registered_view"] is True
        status, _ = _get(service, f"/api/v1/explorer/results/{result_id}", cookie)
        assert status == 200
        status, jobs = _get(service, "/api/v1/jobs", cookie)
        assert status == 200
        assert job_id in {job["job_id"] for job in jobs["jobs"]}
    # The anchor, lease and store were released: the same ROOT serves again.
    with _serving(root):
        pass


def test_reader_session_cannot_reach_the_explorer_or_jobs(world) -> None:
    root, result_id, job_id = world
    with _serving(root) as (service, _):
        reader = _reader_cookie(service)
        for path in (
            "/api/v1/explorer/catalog?limit=10",
            f"/api/v1/explorer/results/{result_id}",
            f"/api/v1/explorer/compare?left={result_id}&right={result_id}",
            "/api/v1/jobs",
            f"/api/v1/jobs/{job_id}",
        ):
            assert _get(service, path, reader) == (403, AUTH_007), path
        # serve wires no reader registry: no longitudinal source, no launch route.
        assert server_module._RUNTIMES[service._runtime_id].reader is None


def test_second_serve_on_the_same_root_is_refused(world) -> None:
    root, _, _ = world
    with _serving(root):
        code, text = _main_text("serve", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert "TBX-SERVE-003" in text and "already running" in text


def test_two_roots_serve_at_once(world, tmp_path: Path) -> None:
    root, result_id, _ = world
    other = tmp_path / "other-root"
    shutil.copytree(root, other, symlinks=True)
    with _serving(root) as (first, _), _serving(other) as (second, _):
        assert first.config.port != second.config.port
        for service in (first, second):
            cookie, _ = _exchange(service)
            status, page = _get(service, "/api/v1/explorer/catalog?limit=10", cookie)
            assert status == 200
            assert result_id in {item["ref"]["result_id"] for item in page["results"]}


def test_missing_runner_database_or_catalog_is_refused_without_a_listener(world) -> None:
    root, _, _ = world
    (root / "runner" / "runner.sqlite3").rename(root / "runner" / "moved.sqlite3")
    code, text = _main_text("serve", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND
    assert "TBX-SERVE-001" in text and "traceback run" in text
    assert "docs/OPERATOR-GUIDE.md#tbx-serve-001" in text
    (root / "runner" / "moved.sqlite3").rename(root / "runner" / "runner.sqlite3")
    shutil.rmtree(root / "catalog")
    code, text = _main_text("serve", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND
    assert "TBX-SERVE-002" in text and "catalog import" in text
    assert not (root / "web").exists()


def test_empty_root_is_refused_and_left_untouched(tmp_path: Path) -> None:
    root = tmp_path / "nothing"
    code, text = _main_text("serve", "--root", root)
    assert code == cli.ExitCode.NOT_FOUND and "TBX-SERVE-001" in text
    assert not root.exists()


@pytest.mark.parametrize(
    ("damage", "code"),
    [("authority", "TBX-AUTH-LOCAL-001"), ("trust", "TBX-AUTH-LOCAL-002")],
)
def test_catalog_without_valid_authority_or_trust_exits_3(world, damage: str, code: str) -> None:
    root, _, _ = world
    if damage == "authority":
        shutil.rmtree(root / "authority")
    else:
        shutil.rmtree(root / "trust" / "result-trust-registry")
    status, text = _main_text("serve", "--root", root)
    assert status == cli.ExitCode.BLOCKED
    assert code in text
    assert not (root / "web").exists()  # no listener was started


def test_tampered_authority_store_exits_3(world) -> None:
    root, _, _ = world
    pins = root / "authority" / "ref" / "pins.json"
    os.chmod(pins, 0o600)
    pins.write_bytes(pins.read_bytes().replace(b'"', b"'", 1))
    status, text = _main_text("serve", "--root", root)
    assert status == cli.ExitCode.BLOCKED and "TBX-AUTH-LOCAL-001" in text


def test_damaged_artifact_is_skipped_and_its_row_is_unavailable(world) -> None:
    root, result_id, _ = world
    artifact, _ = explorer_paths(root, result_id)
    artifact.write_bytes(artifact.read_bytes()[:-10])
    with _serving(root) as (service, out):
        assert "1 invalid explorer file(s) skipped" in out.getvalue()
        cookie, _ = _exchange(service)
        status, page = _get(service, "/api/v1/explorer/catalog?limit=10", cookie)
        assert status == 200
        rows = {item["ref"]["result_id"]: item for item in page["results"]}
        assert rows[result_id]["has_registered_view"] is False
        status, problem = _get(service, f"/api/v1/explorer/results/{result_id}", cookie)
        assert status == 404 and "TBX-INTERNAL" not in json.dumps(problem)


def test_background_serve_ignores_closed_stdin_and_stops_on_sigterm(world) -> None:
    root, _, _ = world
    process = subprocess.Popen(
        [sys.executable, "-m", "traceback_runner", "serve", "--root", str(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        first = process.stdout.readline()
        assert first.startswith("http://127.0.0.1:") and "/#bootstrap=" in first
        time.sleep(1.0)
        assert process.poll() is None  # end of stdin does not stop it
        process.send_signal(signal.SIGTERM)
        rest, errors = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, errors
    assert rest.rstrip().endswith("Stopped; the web lock for ROOT is released.")
    assert errors == ""
