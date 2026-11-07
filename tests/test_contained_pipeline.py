"""contained_process.run_contained_pipeline: N stages, one group, nothing left behind.

``run_contained`` (the signed single-process path) is unchanged; the last tests
pin its result structure.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from traceback_runner import contained_process
from traceback_runner.contained_process import (
    ContainedProcessError,
    ContainedResult,
    PipelineResult,
    Stage,
    StageOutcome,
    run_contained,
    run_contained_pipeline,
)

PY = sys.executable


def _gone(pid: int, wait_s: float = 5.0) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class _Scrubber:
    def line(self, text: str) -> str:
        return text.replace(str(Path.home()), "<home>")

    def lines(self, lines):
        return [self.line(line) for line in lines]


def test_three_stage_pipe_happy_path(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    result = run_contained_pipeline(
        [
            Stage("emit", [PY, "-c", "print('\\n'.join(str(i) for i in range(1000)))"]),
            Stage("filter", [PY, "-c", "import sys; [print(l, end='') for l in sys.stdin if l.strip().endswith('7')]"]),
            Stage("count", [PY, "-c", "import sys; print(sum(1 for _ in sys.stdin))"]),
        ],
        timeout_s=60,
        stdout_path=out,
    )
    assert result.ok and not result.timed_out and not result.aborted
    assert out.read_text().strip() == "100"
    assert [stage.name for stage in result.stages] == ["emit", "filter", "count"]
    assert [stage.returncode for stage in result.stages] == [0, 0, 0]
    assert result.duration_s > 0
    assert all(stage.exited_at is not None for stage in result.stages)


def test_middle_stage_failure_records_every_stage(tmp_path: Path) -> None:
    result = run_contained_pipeline(
        [
            Stage("emit", [PY, "-c", "import signal, time\nsignal.signal(signal.SIGPIPE, signal.SIG_DFL)\nwhile True:\n print('x' * 100, flush=True)\n time.sleep(0.001)"]),
            Stage("middle", [PY, "-c", "import sys; sys.stdin.readline(); print('middle: bad input', file=sys.stderr); sys.exit(3)"]),
            Stage("sink", [PY, "-c", "import sys; print('sink started', file=sys.stderr); sys.stdin.read()"]),
        ],
        timeout_s=60,
    )
    assert not result.ok
    codes = {stage.name: stage.returncode for stage in result.stages}
    assert codes["middle"] == 3
    assert codes["emit"] in (-signal.SIGPIPE, -signal.SIGTERM, -signal.SIGKILL)
    assert codes["sink"] in (0, -signal.SIGTERM, -signal.SIGKILL)
    assert result.failed_stage is not None and result.failed_stage.name == "middle"
    tails = {stage.name: stage.stderr.raw_lines() for stage in result.stages}
    assert tails["middle"] == ["middle: bad input"]
    assert tails["sink"] == ["sink started"]
    assert tails["emit"] == []
    assert result.first_line(_Scrubber()) == "middle exited 3: middle: bad input"
    tail = result.stderr_tail(_Scrubber())
    assert "[middle] middle: bad input" in tail and "[sink] sink started" in tail


def test_deadline_kills_the_whole_group_with_no_orphan(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    grandchild_script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'a').write(str(child.pid) + '\\n')\n"
        "time.sleep(600)\n"
    )
    result = run_contained_pipeline(
        [
            Stage("leader", [PY, "-c", grandchild_script]),
            Stage("reader", [PY, "-c", "import sys, time; time.sleep(600)"]),
        ],
        timeout_s=2,
    )
    assert result.timed_out and not result.ok
    assert [stage.returncode for stage in result.stages] == [-signal.SIGTERM, -signal.SIGTERM]
    assert result.duration_s < 20
    pids = [int(line) for line in pidfile.read_text().split()]
    assert len(pids) == 1
    assert _gone(pids[0]), "the grandchild survived the group kill"


def test_interrupt_while_readers_start_kills_the_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    class InterruptedThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(contained_process.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(contained_process.threading, "Thread", InterruptedThread)
    with pytest.raises(KeyboardInterrupt):
        run_contained_pipeline(
            [
                Stage("first", [PY, "-c", "import time; time.sleep(60)"]),
                Stage("second", [PY, "-c", "import time; time.sleep(60)"]),
            ],
            timeout_s=30,
        )
    (process,) = started  # the interrupt lands before the second stage starts
    assert process.returncode is not None, "the child was not reaped"
    assert _gone(process.pid)


def test_interrupt_in_the_poll_loop_kills_every_stage(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    script = f"import os, sys, time; open({str(pidfile)!r}, 'a').write(str(os.getpid()) + '\\n'); time.sleep(600)"

    def tick(elapsed: float) -> None:
        if pidfile.exists() and len(pidfile.read_text().split()) == 2 and elapsed > 0.3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_contained_pipeline(
            [Stage("a", [PY, "-c", script]), Stage("b", [PY, "-c", script])],
            timeout_s=60, on_tick=tick,
        )
    pids = [int(line) for line in pidfile.read_text().split()]
    assert len(pids) == 2 and all(_gone(pid) for pid in pids)


def test_first_stage_reads_stdin_from_a_file(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("alpha\nbeta\ngamma\n")
    out = tmp_path / "out.txt"
    result = run_contained_pipeline(
        [
            Stage("upper", [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]),
            Stage("count", [PY, "-c", "import sys; data = sys.stdin.read(); print(data, end=''); print(len(data.splitlines()))"]),
        ],
        timeout_s=30, stdin_path=source, stdout_path=out,
    )
    assert result.ok
    assert out.read_text() == "ALPHA\nBETA\nGAMMA\n3\n"


def test_stdin_defaults_to_devnull(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    result = run_contained_pipeline(
        [Stage("read", [PY, "-c", "import sys; print(len(sys.stdin.read()))"])],
        timeout_s=30, stdout_path=out,
    )
    assert result.ok and out.read_text().strip() == "0"


def test_stdin_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ContainedProcessError):
        run_contained_pipeline([Stage("x", [PY, "-c", "pass"])], timeout_s=5, stdin_path=Path("relative.txt"))


def test_peak_rss_is_recorded_per_stage() -> None:
    result = run_contained_pipeline(
        [
            Stage("small", [PY, "-c", "import sys; sys.stdout.write('go\\n')"]),
            Stage("large", [PY, "-c", "import sys; sys.stdin.read(); blob = bytearray(60_000_000); blob[:] = b'x' * len(blob)"]),
        ],
        timeout_s=60,
    )
    assert result.ok
    rss = {stage.name: stage.peak_rss_bytes for stage in result.stages}
    assert rss["small"] and rss["small"] > 0
    assert rss["large"] and rss["large"] >= rss["small"] + 50_000_000
    assert result.peak_rss_bytes == rss["large"]


def test_preexec_fn_runs_in_every_child(tmp_path: Path) -> None:
    marks = tmp_path / "preexec.pids"

    def mark() -> None:
        # Between fork and exec: only raw descriptor calls, nothing that takes a lock.
        fd = os.open(str(marks), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)

    out = tmp_path / "out.txt"
    result = run_contained_pipeline(
        [
            Stage("a", [PY, "-c", "import os; print(os.getpid())"]),
            Stage("b", [PY, "-c", "import os, sys; sys.stdout.write(sys.stdin.read()); print(os.getpid())"]),
        ],
        timeout_s=30, stdout_path=out, preexec_fn=mark,
    )
    assert result.ok
    assert sorted(marks.read_text().split()) == sorted(out.read_text().split())
    assert len(out.read_text().split()) == 2


def test_tree_rss_sampler_hook_and_per_sample_abort() -> None:
    samples: list[int] = []

    def sampler(_pid: int) -> int:
        return 123_456

    def on_sample(value: int) -> bool:
        samples.append(value)
        return len(samples) >= 2

    started = time.monotonic()
    result = run_contained_pipeline(
        [
            Stage("a", [PY, "-c", "import time; time.sleep(600)"]),
            Stage("b", [PY, "-c", "import time; time.sleep(600)"]),
        ],
        timeout_s=120, tree_rss_sampler=sampler, on_tree_rss_sample=on_sample,
    )
    assert result.aborted and not result.ok and not result.timed_out
    assert time.monotonic() - started < 30
    assert samples[:2] == [123_456, 123_456]
    assert result.sampled_tree_peak_rss_bytes == 123_456
    assert [stage.returncode for stage in result.stages] == [-signal.SIGTERM, -signal.SIGTERM]


def test_pipeline_result_fields_are_the_lab_record_fields() -> None:
    assert [f.name for f in dataclasses.fields(PipelineResult)] == [
        "stages", "duration_s", "peak_disk_bytes", "timed_out", "interrupted",
        "sampled_tree_peak_rss_bytes", "aborted",
    ]
    assert [f.name for f in dataclasses.fields(StageOutcome)] == [
        "name", "returncode", "peak_rss_bytes", "stderr", "exited_at",
    ]
    assert PipelineResult([StageOutcome("w", returncode=0)], 0.1, None).ok


def test_single_stage_pipeline_matches_run_contained(tmp_path: Path) -> None:
    argv = [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(4)"]
    single = run_contained(argv, env={}, cwd=tmp_path, timeout_seconds=30)
    out = tmp_path / "out.txt"
    piped = run_contained_pipeline([Stage("one", argv)], timeout_s=30, env={}, cwd=tmp_path, stdout_path=out)
    assert single.outcome == "exited" and single.returncode == 4
    assert single.stdout == b"out\n" and single.stderr == b"err\n"
    assert not single.succeeded
    assert piped.stages[0].returncode == 4 and not piped.ok
    assert out.read_bytes() == b"out\n" and piped.stages[0].stderr.raw_lines() == ["err"]


def test_run_contained_result_structure_is_unchanged(tmp_path: Path) -> None:
    assert [f.name for f in dataclasses.fields(ContainedResult)] == [
        "argv", "outcome", "returncode", "duration_seconds", "stdout", "stderr",
        "stdout_total_bytes", "stderr_total_bytes", "stdout_truncated", "stderr_truncated",
    ]
    result = run_contained([PY, "-c", "print('hi')"], env={"A": "1"}, cwd=tmp_path, timeout_seconds=30)
    assert dataclasses.asdict(result) == {
        "argv": (PY, "-c", "print('hi')"),
        "outcome": "exited",
        "returncode": 0,
        "duration_seconds": result.duration_seconds,
        "stdout": b"hi\n",
        "stderr": b"",
        "stdout_total_bytes": 3,
        "stderr_total_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    assert result.succeeded
    # The signed path still refuses stdin: a single process reads /dev/null.
    reads = run_contained([PY, "-c", "import sys; print(len(sys.stdin.read()))"], env={}, cwd=tmp_path, timeout_seconds=30)
    assert reads.stdout == b"0\n"


def test_run_contained_signature_is_unchanged() -> None:
    import inspect

    assert list(inspect.signature(run_contained).parameters) == [
        "argv", "env", "cwd", "timeout_seconds", "log_limit_bytes", "should_abort",
        "grace_seconds", "poll_seconds", "stdout_path", "stdout_limit_bytes",
    ]


def test_pipeline_rejects_bad_requests(tmp_path: Path) -> None:
    with pytest.raises(ContainedProcessError):
        run_contained_pipeline([], timeout_s=5)
    with pytest.raises(ContainedProcessError):
        run_contained_pipeline([Stage("x", [PY, "-c", "pass"])], timeout_s=0)
    with pytest.raises(ValueError):  # ContainedProcessError is a ValueError
        Stage("rel", ["python", "-c", "pass"])
    with pytest.raises(ContainedProcessError):
        run_contained_pipeline([Stage("x", [PY, "-c", "pass"])], timeout_s=5, stdout_path=Path("out.txt"))


def test_threads_are_joined_and_sink_closed_on_success(tmp_path: Path) -> None:
    before = threading.active_count()
    out = tmp_path / "out.txt"
    result = run_contained_pipeline(
        [Stage("a", [PY, "-c", "import sys; print('x'); print('e', file=sys.stderr)"])],
        timeout_s=30, stdout_path=out,
    )
    assert result.ok and out.read_text() == "x\n"
    time.sleep(0.1)
    assert threading.active_count() <= before + 1  # at most a finishing sampler
