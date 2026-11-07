"""``traceback lab serve --port N``: a fixed loopback port, refused when unbindable."""

from __future__ import annotations

import io
import json
import socket
import threading
from pathlib import Path

import pytest

from traceback_lab.cli import build_parser
from traceback_lab.serve_command import PORT_MAX, PORT_MIN, port_number, serve_command
from traceback_lab.store import LabStore
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli

from tests.lab.test_lab_server import _request


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def busy_port():
    """A loopback port another socket is already listening on."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        yield holder.getsockname()[1]


def _serve_lines(root: Path, port: int | None, *, timeout: float = 60) -> list[str]:
    class Args:
        open_browser = False

    if port is not None:
        Args.port = port
    out = io.StringIO()
    stop = threading.Event()
    ready = threading.Event()
    seen: list[RunningLabServer] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            serve_command(
                Args(), LabStore(root), out=out, stdin=io.StringIO(""), stop=stop,
                ready=lambda server: (seen.append(server), ready.set()),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        ready.wait(timeout=timeout)
    finally:
        stop.set()
        thread.join(timeout=5)
    lines = out.getvalue().splitlines()
    assert not thread.is_alive() and not errors and seen, (lines, errors)
    if port is not None:
        assert seen[0].port == port
    return lines


def test_port_parses_in_range_and_defaults_to_ephemeral() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve"]).port == 0
    assert parser.parse_args(["serve", "--port", "8765"]).port == 8765
    assert port_number(str(PORT_MIN)) == PORT_MIN and port_number(str(PORT_MAX)) == PORT_MAX


def test_fixed_port_is_used_in_the_link(tmp_path) -> None:
    port = _free_port()
    lines = _serve_lines(tmp_path / "empty", port)
    assert lines[0] == f"ROOT  {tmp_path / 'empty'}"
    assert lines[2].startswith(f"http://127.0.0.1:{port}/#bootstrap=")


def test_default_stays_ephemeral(tmp_path) -> None:
    lines = _serve_lines(tmp_path / "empty", None)
    assert lines[2].startswith("http://127.0.0.1:") and "#bootstrap=" in lines[2]
    assert not lines[2].startswith("http://127.0.0.1:0/")


def test_busy_port_is_refused_with_tbx_lab_026_in_human_output(tmp_path, busy_port, capsys) -> None:
    code = signed_cli.main(["lab", "serve", "--root", str(tmp_path), "--port", str(busy_port)])
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 3
    assert lines[0] == f"ROOT  {tmp_path}"
    assert "#bootstrap=" not in captured.out
    assert "CODE       TBX-LAB-026" in lines
    assert any(line.startswith("CAUSE      ") and f"127.0.0.1:{busy_port}" in line for line in lines)
    assert any(line.startswith("FIX        ") and "--port" in line for line in lines)
    assert "RETRYABLE  yes" in lines
    assert "DOCS       docs/LAB-GUIDE.md#tbx-lab-026" in lines


def test_busy_port_is_refused_with_tbx_lab_026_in_json(tmp_path, busy_port, capsys) -> None:
    code = signed_cli.main(["lab", "serve", "--root", str(tmp_path), "--port", str(busy_port), "--json"])
    captured = capsys.readouterr()
    assert code == 3
    payload = json.loads(captured.out)  # exactly one object; no ROOT line leaked ahead of it
    assert payload["status"] == "blocked"
    data = payload["data"]
    assert data["code"] == "TBX-LAB-026"
    assert data["root"] == str(tmp_path)
    assert data["port"] == busy_port
    assert data["retryable"] is True
    assert data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-026"
    assert data["errno"] == "EADDRINUSE"
    assert str(tmp_path.parent) not in json.dumps({k: v for k, v in data.items() if k != "root"})


@pytest.mark.parametrize("value", ["80", "0", "65536", "-1", "abc", "8080.5"])
def test_out_of_range_or_non_integer_port_is_a_usage_error(tmp_path, capsys, value) -> None:
    code = signed_cli.main(["lab", "serve", "--root", str(tmp_path), "--port", value])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "argument --port" in captured.err and "1024-65535" in captured.err


def test_out_of_range_port_keeps_the_json_usage_contract(tmp_path, capsys) -> None:
    code = signed_cli.main(["lab", "serve", "--root", str(tmp_path), "--port", "1023", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["status"] == "usage"
    assert payload["data"]["root"] == str(tmp_path) and "argument --port" in payload["data"]["usage"]


def test_fixed_port_binds_loopback_only_and_refuses_a_foreign_host(tmp_path) -> None:
    port = _free_port()
    with RunningLabServer(LabStore(tmp_path), port=port) as running:
        assert running.port == port
        assert running.httpd.socket.getsockname() == ("127.0.0.1", port)
        ok, _ = _request(running, "GET", "/")
        assert ok.status == 200
        for host in ("evil.example:80", f"evil.example:{port}", f"localhost:{port}", "127.0.0.1"):
            denied, body = _request(running, "GET", "/", headers={"Host": host})
            assert denied.status == 403, (host, denied.status, body)
        forwarded, _ = _request(running, "GET", "/api/status", headers={"X-Forwarded-For": "10.0.0.1"})
        assert forwarded.status in (401, 403)
