"""S6d: the Lab guide's journey block runs verbatim; doctor and --example behave."""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

import pytest

from tests.lab.test_landing import _body, needs_chrome
from traceback_lab.doctor import lab_checks
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "LAB-GUIDE.md"
EXAMPLE_BUDGET_S = 180  # CI fails above 3 minutes on the Ubuntu runner
SERVE_BANNER_TIMEOUT_S = 120


def journey() -> list[list[str]]:
    text = GUIDE.read_text()
    block = re.search(r"<!-- lab-journey -->\s*```bash\n(.*?)```", text, re.S)
    assert block, "the guide's journey block is missing"
    return [shlex.split(line) for line in block.group(1).strip().splitlines()]


def _serve_banner(process, stdout_path: Path, stderr_path: Path, *, timeout: float) -> list[str]:
    """Read the two serve banner lines without blocking past the startup budget."""

    deadline = time.monotonic() + timeout
    while True:
        exited = process.poll() is not None
        with stdout_path.open("rb") as output:
            parts = output.read(8192).split(b"\n")
        if len(parts) >= 3:
            if exited:
                break
            return [line.decode("utf-8", errors="replace").rstrip("\r") for line in parts[:2]]
        remaining = deadline - time.monotonic()
        if exited or remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
    raise AssertionError(
        f"serve banner unavailable within {timeout:g}s (exit={process.poll()}): "
        f"stdout={stdout_path.read_text(errors='replace')[-2000:]!r}; "
        f"stderr={stderr_path.read_text(errors='replace')[-2000:]!r}"
    )


def test_serve_banner_deadline_reaches_cleanup_for_a_silent_process(tmp_path) -> None:
    import subprocess
    import sys

    stdout_path = tmp_path / "silent-stdout.log"
    stderr_path = tmp_path / "silent-stderr.log"
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], stdout=stdout, stderr=stderr,
            stdin=subprocess.DEVNULL,
        )
        try:
            with pytest.raises(AssertionError, match="serve banner unavailable within"):
                _serve_banner(process, stdout_path, stderr_path, timeout=0.2)
        finally:
            process.terminate()
            process.wait(timeout=5)


def test_serve_banner_requires_two_complete_lines(tmp_path) -> None:
    from types import SimpleNamespace

    stdout_path = tmp_path / "partial-stdout.log"
    stderr_path = tmp_path / "partial-stderr.log"
    stderr_path.write_text("")
    running = SimpleNamespace(poll=lambda: None)
    stdout_path.write_bytes(b"ROOT  /tmp/root\nhttp://127.0.0.1:5")
    with pytest.raises(AssertionError, match="serve banner unavailable within"):
        _serve_banner(running, stdout_path, stderr_path, timeout=0.01)
    with stdout_path.open("ab") as output:
        output.write(b"4211/#bootstrap=token\n")
    assert _serve_banner(running, stdout_path, stderr_path, timeout=0) == [
        "ROOT  /tmp/root", "http://127.0.0.1:54211/#bootstrap=token",
    ]


def test_journey_block_is_the_documented_four_steps() -> None:
    commands = journey()
    assert commands == [
        ["uv", "sync"],
        ["uv", "run", "traceback", "lab", "doctor"],
        ["uv", "run", "traceback", "lab", "run", "--example"],
        ["uv", "run", "traceback", "lab", "serve", "--open"],
    ]


@pytest.fixture(scope="module")
def journey_root(tmp_path_factory) -> dict:
    """Run the guide's doctor and --example steps verbatim (via the CLI entry point)."""

    import os

    root = tmp_path_factory.mktemp("journey") / "root"
    run_cwd = tmp_path_factory.mktemp("journey-run-cwd")
    previous = os.environ.get("TRACEBACK_ROOT")
    os.environ["TRACEBACK_ROOT"] = str(root)
    outputs = {}
    try:
        for argv in journey():
            if argv[:2] == ["uv", "sync"] or argv[-2:] == ["serve", "--open"]:
                continue
            assert argv[:3] == ["uv", "run", "traceback"]
            import contextlib
            import io

            buffer = io.StringIO()
            started = time.monotonic()
            previous_cwd = Path.cwd()
            try:
                if argv[-2:] == ["run", "--example"]:
                    os.chdir(run_cwd)
                with contextlib.redirect_stdout(buffer):
                    code = signed_cli.main(argv[3:])
            finally:
                os.chdir(previous_cwd)
            outputs[" ".join(argv[3:])] = (code, buffer.getvalue(), time.monotonic() - started)
    finally:
        if previous is None:
            os.environ.pop("TRACEBACK_ROOT", None)
        else:
            os.environ["TRACEBACK_ROOT"] = previous
    return {"root": root, "run_cwd": run_cwd, "outputs": outputs}


def test_doctor_step_passes_with_fixes_on_every_warning(journey_root) -> None:
    code, out, _ = journey_root["outputs"]["lab doctor"]
    assert code == 0, out
    assert out.splitlines()[0] == f"ROOT  {journey_root['root']}"
    for check in lab_checks(LabStore(journey_root["root"])):
        if check["status"] != "pass":
            assert check.get("fix"), check


def test_example_step_runs_within_budget_and_prints_the_summary(journey_root) -> None:
    code, out, elapsed = journey_root["outputs"]["lab run --example"]
    assert code == 0, out
    assert elapsed < EXAMPLE_BUDGET_S, f"lab run --example took {elapsed:.0f} s"
    assert out.splitlines()[0] == f"ROOT  {journey_root['root']}"
    block = out[out.index("ROOT    ") :].splitlines()
    assert block[0] == f"ROOT    {journey_root['root']}"
    sample = next(line for line in block if line.startswith("SAMPLE  "))
    assert re.match(r"SAMPLE  [0-9a-f]{12,} ", sample)
    assert any(line == "NEXT    traceback lab serve --open" for line in block)
    assert re.search(r"skipped \(example_reference: [a-z-, ]*copy-number", out)
    record = json.loads(next((journey_root["root"] / "lab" / "sources").glob("*.json")).read_text())
    assert record["example"] is True


def test_example_rerun_reuses_everything(journey_root, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TRACEBACK_ROOT", str(journey_root["root"]))
    started = time.monotonic()
    code = signed_cli.main(["lab", "run", "--example", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and time.monotonic() - started < 30
    assert all(step["cached"] for step in payload["data"]["steps"] if step["status"] == "ok")


def test_example_refuses_input_paths(tmp_path, capsys) -> None:
    code = signed_cli.main(["lab", "run", "--example", str(tmp_path), "--root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-005"


def test_no_input_prints_the_three_line_quickstart(tmp_path, capsys) -> None:
    code = signed_cli.main(["lab", "run", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 2
    assert out.splitlines()[-3:] == ["  traceback lab doctor", "  traceback lab run --example", "  traceback lab serve --open"]


def test_serve_uses_the_run_root_across_cwds(journey_root, tmp_path) -> None:
    """The guide's run and serve commands share TRACEBACK_ROOT across directories."""

    import os
    import signal
    import subprocess

    assert journey_root["run_cwd"] != REPO
    argv = next(a for a in journey() if a[-2:] == ["serve", "--open"])
    env = {**os.environ, "TRACEBACK_ROOT": str(journey_root["root"]), "BROWSER": "true", "PYTHONPATH": str(REPO)}
    stdout_path = tmp_path / "root-stdout.log"
    stderr_path = tmp_path / "root-stderr.log"
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            argv, cwd=REPO, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            lines = _serve_banner(process, stdout_path, stderr_path, timeout=SERVE_BANNER_TIMEOUT_S)
            assert lines[0] == f"ROOT  {journey_root['root']}"
            assert re.match(r"^http://127\.0\.0\.1:\d+/#bootstrap=[A-Za-z0-9_-]{43,}$", lines[1]), lines
            assert "has no Lab runs" not in stdout_path.read_text()
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@needs_chrome
def test_serve_step_reaches_the_landing_with_curve_repeatability_and_example_pill(journey_root, tmp_path) -> None:
    """The guide's own serve command, launched as a process (BROWSER=true keeps --open headless)."""

    import os
    import signal
    import subprocess

    from tests.lab.test_lab_server import _chrome

    argv = next(a for a in journey() if a[-2:] == ["serve", "--open"])
    env = {**os.environ, "TRACEBACK_ROOT": str(journey_root["root"]), "BROWSER": "true", "PYTHONPATH": str(REPO)}
    stdout_path = tmp_path / "serve-stdout.log"
    stderr_path = tmp_path / "serve-stderr.log"
    stdout = stdout_path.open("w")
    stderr = stderr_path.open("w")
    try:
        process = subprocess.Popen(
            argv,
            cwd=REPO, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    try:
        lines = _serve_banner(process, stdout_path, stderr_path, timeout=SERVE_BANNER_TIMEOUT_S)
        assert lines[0] == f"ROOT  {journey_root['root']}", (lines, stderr_path.read_text()[-2000:])
        link = lines[1]
        assert re.match(r"^http://127\.0\.0\.1:\d+/#bootstrap=[A-Za-z0-9_-]{43,}$", link), lines
        from tests.lab.test_lab_server import _dom_cdp

        profile = tmp_path / "profile"
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=1280,800", link], profile,
            ok=lambda d: 'data-ready="1"' in d and "Depth → utility" in d,
        )
    finally:
        try:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            stdout.close()
            stderr.close()
    body = _body(dom)
    assert body["csp-violations"] == "0"
    assert "Depth → utility" in dom and 'class="curve-grid"' in dom
    assert "REPEATABILITY" in dom and "Median split-half" in dom
    assert "EXAMPLE (synthetic)" in dom
    assert "Not in the synthetic example: copy-number needs hg38 assets." in dom


def test_doctor_blocks_only_on_the_example_journey(tmp_path, monkeypatch, capsys) -> None:
    import shutil

    from traceback_lab import tools

    only = tmp_path / "bin"
    only.mkdir()
    (only / "samtools").symlink_to(shutil.which("samtools"))
    monkeypatch.setenv("PATH", str(only))
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(only / name) if (only / name).exists() else None)
    code = signed_cli.main(["lab", "doctor", "--root", str(tmp_path / "r"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and payload["status"] == "blocked"
    minimap2 = next(check for check in payload["data"]["checks"] if check["name"] == "lab_minimap2")
    assert minimap2["status"] == "blocked" and minimap2["fix"] == "brew install minimap2"
    assert payload["data"]["setup_steps"][0] == "brew install minimap2"


def test_example_dry_run_writes_nothing(tmp_path, capsys) -> None:
    root = tmp_path / "fresh"
    code = signed_cli.main(["lab", "run", "--example", "--dry-run", "--root", str(root), "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["data"]["example"] is True
    assert not root.exists()


def test_a_foreign_lab_example_reference_is_refused(tmp_path, capsys, synthetic_world) -> None:
    from traceback_runner.references import register_reference

    root = tmp_path / "root"
    register_reference(root, synthetic_world["fasta"], "lab-example")
    code = signed_cli.main(["lab", "run", "--example", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-004"


def test_doctor_json_carries_no_host_path_beyond_root(tmp_path, capsys) -> None:
    root = tmp_path / "r"
    signed_cli.main(["lab", "doctor", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    data = dict(payload["data"])
    assert data.pop("root") == str(root)
    assert str(tmp_path) not in json.dumps(data)


def test_doctor_blocks_an_unwritable_root(tmp_path, capsys) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root writes everywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code = signed_cli.main(["lab", "doctor", "--root", str(locked / "root"), "--json"])
        payload = json.loads(capsys.readouterr().out)
    finally:
        locked.chmod(0o700)
    assert code == 3
    assert next(c for c in payload["data"]["checks"] if c["name"] == "root")["status"] == "blocked"
