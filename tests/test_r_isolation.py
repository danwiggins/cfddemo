"""Isolated R execution (CN1): scrubbed environment, process-group kill, bounded logs.

Synthetic only.  A fake ``Rscript`` (a Python script) records what it was given
and plays the failure modes.  The real-R tests run only when the copy-number
toolchain is installed on this machine.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from traceback_runner import r_isolation
from traceback_runner.contained_process import ContainedProcessError, run_contained
from traceback_runner.r_isolation import (
    BOOTSTRAP_NAME,
    LIBPATHS_MISMATCH_EXIT,
    R_BOOTSTRAP,
    RInvocation,
    RIsolationError,
    isolated_r_argv,
    isolated_r_environment,
    run_isolated_r,
)

FAKE_RSCRIPT = """\
#!{python}
import json, os, subprocess, sys, time
mode = os.environ.get("FAKE_MODE", "record")
cwd = os.getcwd()
if mode == "record":
    with open(os.path.join(cwd, "record.json"), "w") as handle:
        json.dump({{"argv": sys.argv, "env": dict(os.environ), "cwd": cwd}}, handle)
    print("fake R ran")
elif mode == "libpaths":
    sys.stderr.write("TRACEBACK_R_LIBPATHS_MISMATCH: /elsewhere\\n")
    sys.exit(86)
elif mode == "fail":
    sys.stderr.write("Error in library(x)\\n")
    sys.exit(1)
elif mode == "spew":
    sys.stderr.write("E" * 3_000_000)
    sys.stdout.write("O" * 3_000_000)
elif mode in ("hang", "orphan"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.path.join(cwd, "child.pid.tmp"), "w") as handle:
        handle.write(str(child.pid))
    os.replace(os.path.join(cwd, "child.pid.tmp"), os.path.join(cwd, "child.pid"))
    if mode == "hang":
        time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ask ps for its state.
    import subprocess

    state = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fake_r(tmp_path: Path) -> dict[str, Path]:
    prefix = tmp_path / "env"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib" / "R" / "library").mkdir(parents=True)
    rscript = prefix / "bin" / "Rscript"
    rscript.write_text(FAKE_RSCRIPT.format(python=sys.executable), encoding="utf-8")
    rscript.chmod(rscript.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "method.R"
    script.write_text("cat('method')\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return {"rscript": rscript, "script": script, "work": work, "prefix": prefix}


def _invocation(fake_r: dict[str, Path], **changes) -> RInvocation:
    values = {
        "rscript": fake_r["rscript"],
        "script": fake_r["script"],
        "args": ("--id", "sample", "--outDir", str(fake_r["work"] / "out")),
        "library_paths": (fake_r["prefix"] / "lib" / "R" / "library",),
        "work_dir": fake_r["work"],
        "timeout_seconds": 30,
    }
    values.update(changes)
    return RInvocation(**values)


def test_runs_vanilla_with_a_scrubbed_environment(fake_r, monkeypatch) -> None:
    monkeypatch.setenv("R_LIBS_USER", "/home/someone/R")
    monkeypatch.setenv("R_PROFILE_USER", "/home/someone/.Rprofile")
    monkeypatch.setenv("TRACEBACK_SENTINEL", "leaked")
    invocation = _invocation(fake_r)
    result = run_isolated_r(invocation, environment_overrides={"FAKE_MODE": "record"})
    assert result.succeeded, result.process.stderr
    record = json.loads((fake_r["work"] / "record.json").read_text())
    assert record["argv"][1:] == [
        "--vanilla",
        str(fake_r["work"] / BOOTSTRAP_NAME),
        "--id",
        "sample",
        "--outDir",
        str(fake_r["work"] / "out"),
    ]
    env = record["env"]
    # Built from nothing: nothing inherited from the operator's shell.
    env.pop("__CF_USER_TEXT_ENCODING", None)  # added by macOS to every process
    expected = isolated_r_environment(invocation) | {"FAKE_MODE": "record"}
    assert env == expected
    assert "TRACEBACK_SENTINEL" not in env
    assert env["R_LIBS_USER"] == "" and env["R_LIBS_SITE"] == ""
    assert env["R_PROFILE_USER"] == "/dev/null" and env["R_ENVIRON_USER"] == "/dev/null"
    for name in r_isolation.SINGLE_THREAD_VARIABLES:
        assert env[name] == "1"
    assert env["TRACEBACK_R_SEED"] == str(r_isolation.DEFAULT_SEED)
    assert env["TRACEBACK_R_LIBPATHS"] == str(invocation.library_paths[0])
    assert env["TRACEBACK_R_SCRIPT"] == str(fake_r["script"])
    assert env["HOME"].startswith(str(fake_r["work"]))
    assert env["TMPDIR"].startswith(str(fake_r["work"]))
    assert env["PATH"].split(":")[0] == str(fake_r["rscript"].parent)
    assert Path(record["cwd"]).resolve() == fake_r["work"].resolve()
    assert (fake_r["work"] / BOOTSTRAP_NAME).read_text() == R_BOOTSTRAP
    assert result.process.stdout == b"fake R ran\n"


def test_bootstrap_asserts_libpaths_and_fixes_the_rng() -> None:
    assert ".libPaths()" in R_BOOTSTRAP
    assert f"status = {LIBPATHS_MISMATCH_EXIT}L" in R_BOOTSTRAP
    assert 'RNGkind("Mersenne-Twister", "Inversion", "Rejection")' in R_BOOTSTRAP
    assert 'set.seed(as.integer(Sys.getenv("TRACEBACK_R_SEED")))' in R_BOOTSTRAP
    assert R_BOOTSTRAP.index("quit(") < R_BOOTSTRAP.index("source(")


def test_libpaths_mismatch_is_reported(fake_r) -> None:
    result = run_isolated_r(_invocation(fake_r), environment_overrides={"FAKE_MODE": "libpaths"})
    assert result.libpaths_mismatch
    assert not result.succeeded


def test_ordinary_failure_is_not_a_libpaths_mismatch(fake_r) -> None:
    result = run_isolated_r(_invocation(fake_r), environment_overrides={"FAKE_MODE": "fail"})
    assert result.process.returncode == 1
    assert not result.libpaths_mismatch
    assert b"Error in library" in result.process.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"rscript": Path("bin/Rscript")},
        {"script": Path("method.R")},
        {"library_paths": ()},
        {"library_paths": (Path("relative/lib"),)},
        {"seed": 2**31},
    ],
)
def test_malformed_invocations_never_run(fake_r, changes) -> None:
    with pytest.raises(RIsolationError):
        run_isolated_r(_invocation(fake_r, **changes), environment_overrides={"FAKE_MODE": "record"})
    assert not (fake_r["work"] / "record.json").exists()


def test_non_normalised_path_is_refused(fake_r) -> None:
    sneaky = Path(str(fake_r["prefix"]) + "/bin/../bin/Rscript")
    with pytest.raises(RIsolationError, match="normalised"):
        run_isolated_r(_invocation(fake_r, rscript=sneaky))


def test_pinned_digests_are_checked_before_exec(fake_r) -> None:
    import hashlib

    good = hashlib.sha256(fake_r["rscript"].read_bytes()).hexdigest()
    script_good = hashlib.sha256(fake_r["script"].read_bytes()).hexdigest()
    ok = run_isolated_r(
        _invocation(fake_r, rscript_sha256=good, script_sha256=script_good),
        environment_overrides={"FAKE_MODE": "record"},
    )
    assert ok.succeeded
    (fake_r["work"] / "record.json").unlink()
    with pytest.raises(RIsolationError, match="pinned SHA-256"):
        run_isolated_r(
            _invocation(fake_r, rscript_sha256="0" * 64),
            environment_overrides={"FAKE_MODE": "record"},
        )
    with pytest.raises(RIsolationError, match="pinned SHA-256"):
        run_isolated_r(
            _invocation(fake_r, script_sha256="0" * 64),
            environment_overrides={"FAKE_MODE": "record"},
        )
    assert not (fake_r["work"] / "record.json").exists()


def test_overrides_cannot_replace_isolation_variables(fake_r) -> None:
    with pytest.raises(RIsolationError):
        run_isolated_r(_invocation(fake_r), environment_overrides={"R_LIBS_USER": "/x"})


def test_timeout_kills_the_whole_process_group(fake_r) -> None:
    started = time.monotonic()
    result = run_isolated_r(
        _invocation(fake_r, timeout_seconds=1.5), environment_overrides={"FAKE_MODE": "hang"}
    )
    assert result.process.outcome == "timeout"
    assert result.process.returncode is None
    assert time.monotonic() - started < 20
    child = int((fake_r["work"] / "child.pid").read_text())
    assert _wait_dead(child), "the grandchild survived the timeout"


def test_children_left_behind_after_a_normal_exit_are_killed(fake_r) -> None:
    result = run_isolated_r(_invocation(fake_r), environment_overrides={"FAKE_MODE": "orphan"})
    assert result.process.outcome == "exited" and result.process.returncode == 0
    child = int((fake_r["work"] / "child.pid").read_text())
    assert _wait_dead(child), "a background child outlived the run"


def test_abort_kills_the_group(fake_r) -> None:
    calls = []

    def lease_lost() -> bool:
        calls.append(1)
        return (fake_r["work"] / "child.pid").exists()

    result = run_isolated_r(
        _invocation(fake_r), environment_overrides={"FAKE_MODE": "hang"}, should_abort=lease_lost
    )
    assert result.process.outcome == "aborted"
    child = int((fake_r["work"] / "child.pid").read_text())
    assert _wait_dead(child)


def test_interrupt_kills_the_group_and_propagates(fake_r) -> None:
    def interrupt() -> bool:
        if (fake_r["work"] / "child.pid").exists():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        run_isolated_r(
            _invocation(fake_r), environment_overrides={"FAKE_MODE": "hang"}, should_abort=interrupt
        )
    child = int((fake_r["work"] / "child.pid").read_text())
    assert _wait_dead(child)


def test_logs_are_bounded(fake_r) -> None:
    result = run_isolated_r(
        _invocation(fake_r, log_limit_bytes=64 * 1024), environment_overrides={"FAKE_MODE": "spew"}
    )
    process = result.process
    assert process.succeeded
    assert process.stderr_total_bytes == 3_000_000 and process.stdout_total_bytes == 3_000_000
    assert process.stderr_truncated and process.stdout_truncated
    assert len(process.stderr) < 64 * 1024 + 100
    assert b"bytes omitted" in process.stderr


def test_contained_process_refuses_relative_executables(tmp_path: Path) -> None:
    with pytest.raises(ContainedProcessError):
        run_contained(["true"], env={}, cwd=tmp_path, timeout_seconds=5)
    with pytest.raises(ContainedProcessError):
        run_contained(["/bin/echo"], env={}, cwd=Path("relative"), timeout_seconds=5)
    with pytest.raises(ContainedProcessError):
        run_contained(["/bin/echo"], env={"A": "x\x00"}, cwd=tmp_path, timeout_seconds=5)


def test_contained_process_streams_stdout_to_a_bounded_file(tmp_path: Path) -> None:
    target = tmp_path / "counts.wig"
    result = run_contained(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"],
        env={},
        cwd=tmp_path,
        timeout_seconds=30,
        stdout_path=target,
        stdout_limit_bytes=4096,
    )
    assert result.succeeded and result.stdout == b""
    assert result.stdout_total_bytes == 5000 and result.stdout_truncated
    assert target.read_bytes() == b"x" * 4096
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        run_contained(
            ["/bin/echo"], env={}, cwd=tmp_path, timeout_seconds=5, stdout_path=target
        )


def test_isolated_argv_is_absolute_and_vanilla(fake_r) -> None:
    argv = isolated_r_argv(_invocation(fake_r))
    assert os.path.isabs(argv[0]) and argv[1] == "--vanilla"


def test_sigterm_is_used_before_sigkill(fake_r, monkeypatch) -> None:
    sent: list[int] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, signum: int) -> None:
        sent.append(signum)
        real_killpg(pgid, signum)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    run_isolated_r(
        _invocation(fake_r, timeout_seconds=1.0), environment_overrides={"FAKE_MODE": "hang"}
    )
    assert sent[0] == signal.SIGTERM and sent[-1] == signal.SIGKILL


# --------------------------------------------------------------------------
# Real R, only where the copy-number toolchain is installed.


def _real_toolchain():
    from traceback_runner.toolchain import resolve_copy_number_toolchain

    try:
        return resolve_copy_number_toolchain()
    except Exception:
        return None


REAL = _real_toolchain()
requires_toolchain = pytest.mark.skipif(
    REAL is None, reason="the copy-number toolchain is not installed here"
)


def _real_invocation(tmp_path: Path, body: str, **changes) -> RInvocation:
    assert REAL is not None
    script = tmp_path / "probe.R"
    script.write_text(body, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    values = {
        "rscript": REAL.rscript,
        "script": script,
        "args": ("--flag", "value with space"),
        "library_paths": (REAL.r_library,),
        "work_dir": work,
        "timeout_seconds": 120,
    }
    values.update(changes)
    return RInvocation(**values)


@requires_toolchain
def test_real_r_sees_only_the_declared_library_and_its_arguments(tmp_path: Path) -> None:
    body = (
        'cat(jsonlite_free <- paste(c(.libPaths(), commandArgs(trailingOnly = TRUE),'
        ' Sys.getenv("R_LIBS_USER"), Sys.getenv("OMP_NUM_THREADS"), runif(1)),'
        ' collapse = "|"), "\\n")\n'
    )
    first = run_isolated_r(_real_invocation(tmp_path, body))
    second = run_isolated_r(_real_invocation(tmp_path, body))
    assert first.succeeded, first.process.stderr
    fields = first.process.stdout.decode().strip().split("|")
    assert len(fields) == 6
    assert Path(fields[0]).resolve() == REAL.r_library.resolve()
    assert fields[1:3] == ["--flag", "value with space"]
    # R's own Renviron fills an empty R_LIBS_USER with a default under HOME;
    # HOME is private to the work directory, so it names nothing that exists.
    assert fields[3] == "" or fields[3].startswith(str(tmp_path / "work" / "home"))
    assert not Path(fields[3]).exists()
    assert fields[4] == "1"
    assert first.process.stdout == second.process.stdout  # fixed seed


@requires_toolchain
def test_real_r_refuses_an_undeclared_library_path(tmp_path: Path) -> None:
    result = run_isolated_r(
        _real_invocation(tmp_path, "cat('should not run')\n", library_paths=(tmp_path,))
    )
    assert result.libpaths_mismatch
    assert b"should not run" not in result.process.stdout
