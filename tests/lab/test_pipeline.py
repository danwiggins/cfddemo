"""pipeline.py: one process group, failure propagation, no orphans."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.pipeline import PipelineResult, Stage, StageOutcome, run_pipeline
from traceback_lab.scrub import Scrubber

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


def test_three_stage_pipe_succeeds(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    result = run_pipeline(
        [
            Stage("emit", [PY, "-c", "print('\\n'.join(str(i) for i in range(1000)))"]),
            Stage("filter", [PY, "-c", "import sys; [print(l, end='') for l in sys.stdin if l.strip().endswith('7')]"]),
            Stage("count", [PY, "-c", "import sys; print(sum(1 for _ in sys.stdin))"]),
        ],
        timeout_s=60,
        stdout_path=out,
    )
    assert result.ok
    assert out.read_text().strip() == "100"
    assert all(stage.returncode == 0 for stage in result.stages)
    assert result.peak_rss_bytes and result.peak_rss_bytes > 0


def test_middle_failure_fails_the_pipeline_and_names_the_stage(tmp_path: Path) -> None:
    result = run_pipeline(
        [
            # Like samtools, the writer dies of SIGPIPE when its reader goes away.
            Stage("emit", [PY, "-c", "import signal, time\nsignal.signal(signal.SIGPIPE, signal.SIG_DFL)\nwhile True:\n print('x' * 100, flush=True)\n time.sleep(0.001)"]),
            Stage("aligner", [PY, "-c", "import sys; sys.stdin.readline(); print('[E] index corrupt at /Users/x/ref.mmi', file=sys.stderr); sys.exit(3)"]),
            Stage("sink", [PY, "-c", "import sys; sys.stdin.read()"]),
        ],
        timeout_s=60,
    )
    assert not result.ok
    assert result.failed_stage is not None and result.failed_stage.name == "aligner"
    line = result.first_line(Scrubber(home=None, login=None))
    assert line == "aligner exited 3: [E] index corrupt at <path>"
    assert result.duration_s < 30


def test_timeout_kills_the_group_including_a_grandchild(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    started = time.time()
    result = run_pipeline([Stage("hang", [PY, "-c", script])], timeout_s=2)
    assert result.timed_out and not result.ok
    assert time.time() - started < 20
    grandchild = int(pidfile.read_text())
    assert _gone(grandchild)


def test_interrupt_kills_the_group_and_reraises(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"

    def tick(elapsed: float) -> None:
        if pidfile.exists() and elapsed > 0.5:
            raise KeyboardInterrupt

    script = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(600)"
    with pytest.raises(KeyboardInterrupt):
        run_pipeline([Stage("sleep", [PY, "-c", script])], timeout_s=60, on_tick=tick)
    assert _gone(int(pidfile.read_text()))


def test_stage_executables_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        Stage("rel", ["samtools", "view"])


def test_stderr_is_echoed_raw_but_kept_bounded(tmp_path: Path) -> None:
    seen: list[str] = []
    script = "import sys\nfor i in range(500): print(f'line {i}', file=sys.stderr)"
    result = run_pipeline([Stage("noisy", [PY, "-c", script])], timeout_s=30, echo=seen.append)
    assert len(seen) == 500
    raw = result.stages[0].stderr.raw_lines()
    assert len(raw) == 20 + 1 + 50 and raw[-1] == "line 499"


def test_disk_sampling(tmp_path: Path) -> None:
    target = tmp_path / "staging"
    target.mkdir()
    script = f"open({str(target / 'blob')!r}, 'wb').write(b'x' * 2_000_000)"
    result = run_pipeline([Stage("write", [PY, "-c", script])], timeout_s=30, disk_path=target)
    assert result.peak_disk_bytes is not None and result.peak_disk_bytes >= 2_000_000


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_disk_sampler_failure_does_not_discard_successful_step(error_type: type[Exception]) -> None:
    def unavailable() -> int:
        raise error_type("scratch changed during sampling")

    result = run_pipeline(
        [Stage("ok", [PY, "-c", "print('done')"])], timeout_s=30,
        disk_usage=unavailable,
    )
    assert result.ok
    assert result.peak_disk_bytes is None


def test_rss_sampler_failure_preserves_worker_result(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    def unavailable(_pid: int) -> int:
        raise ValueError("process enumeration unavailable")

    monkeypatch.setattr(pipeline, "_process_tree_rss_bytes", unavailable)
    result = run_pipeline([Stage("ok", [PY, "-c", "print('done')"])], timeout_s=30)
    assert result.ok
    assert result.peak_rss_bytes is not None and result.peak_rss_bytes > 0
    assert result.sampled_tree_peak_rss_bytes is None


def test_sampler_thread_start_failure_preserves_worker_result(monkeypatch: pytest.MonkeyPatch) -> None:
    original_start = threading.Thread.start

    def reject_sampler(self: threading.Thread) -> None:
        name = getattr(getattr(self, "_target", None), "__name__", "")
        if name in {"sample_rss", "sample_disk"}:
            raise RuntimeError("cannot start another telemetry thread")
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", reject_sampler)
    result = run_pipeline(
        [Stage("ok", [PY, "-c", "print('done')"])], timeout_s=10,
        disk_usage=lambda: 1,
    )
    assert result.ok
    assert result.peak_rss_bytes is not None and result.peak_rss_bytes > 0
    assert result.sampled_tree_peak_rss_bytes is None
    assert result.peak_disk_bytes is None


def test_final_disk_thread_start_failure_keeps_earlier_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    monkeypatch.setattr(pipeline, "DISK_SAMPLE_S", 1000.0)
    original_start = threading.Thread.start
    disk_starts = 0

    def reject_final(self: threading.Thread) -> None:
        nonlocal disk_starts
        if getattr(getattr(self, "_target", None), "__name__", "") == "sample_disk":
            disk_starts += 1
            if disk_starts == 2:
                raise RuntimeError("cannot start final disk scan")
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", reject_final)
    result = run_pipeline(
        [Stage("ok", [PY, "-c", "import time; time.sleep(0.2)"])],
        timeout_s=10, disk_usage=lambda: 7,
    )
    assert result.ok
    assert disk_starts == 2
    assert result.peak_disk_bytes == 7


def test_slow_rss_sampler_does_not_delay_worker_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    release = threading.Event()
    finished = threading.Event()
    def slow(_pid: int) -> None:
        release.wait(30)
        finished.set()

    monkeypatch.setattr(pipeline, "_process_tree_rss_bytes", slow)
    try:
        result = run_pipeline([Stage("ok", [PY, "-c", "print('done')"])], timeout_s=10)
        assert not finished.is_set()
    finally:
        release.set()
    assert result.ok


def test_slow_disk_sampler_does_not_delay_worker_polling() -> None:
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def slow() -> int:
        nonlocal calls
        calls += 1
        release.wait(30)
        finished.set()
        return 1

    try:
        result = run_pipeline(
            [Stage("ok", [PY, "-c", "print('done')"])], timeout_s=10,
            disk_usage=slow,
        )
        assert not finished.is_set()
    finally:
        release.set()
    assert result.ok
    assert calls >= 2
    assert result.peak_disk_bytes is None


def test_incomplete_final_disk_scan_keeps_earlier_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    monkeypatch.setattr(pipeline, "DISK_SAMPLE_S", 1000.0)
    release = threading.Event()
    calls = 0

    def disk_usage() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        release.wait(30)
        return 2_000_000

    try:
        result = run_pipeline(
            [Stage("ok", [PY, "-c", "import time; time.sleep(0.2)"])],
            timeout_s=10,
            disk_usage=disk_usage,
        )
    finally:
        release.set()
    assert result.ok
    assert calls == 2
    assert result.peak_disk_bytes == 1


def test_final_disk_scan_starts_after_worker_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    monkeypatch.setattr(pipeline, "DISK_SAMPLE_S", 1000.0)
    calls = 0

    def disk_usage() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.4)
            return 1
        return 2_000_000

    result = run_pipeline(
        [Stage("ok", [PY, "-c", "import time; time.sleep(0.2)"])],
        timeout_s=10,
        disk_usage=disk_usage,
    )
    assert result.ok
    assert calls == 2
    assert result.peak_disk_bytes == 2_000_000


def test_final_disk_reporting_wait_is_not_worker_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    monkeypatch.setattr(pipeline, "DISK_SAMPLE_S", 1000.0)
    release = threading.Event()
    calls = 0

    def disk_usage() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1
        release.wait(30)
        return 2

    started = time.monotonic()
    try:
        result = run_pipeline(
            [Stage("ok", [PY, "-c", "import time; time.sleep(0.2)"])],
            timeout_s=10, disk_usage=disk_usage,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert result.ok
    assert calls == 2
    assert elapsed - result.duration_s >= 0.8


def test_ps_sampler_does_not_report_zero_for_unmeasured_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    import traceback_lab.pipeline as pipeline

    monkeypatch.setattr(pipeline.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=0, stdout=""))
    assert pipeline._ps_tree_rss_bytes(12345) is None


def test_tree_rss_keeps_sampling_after_pipeline_leader_exits() -> None:
    first = "import time; print('ready', flush=True); time.sleep(0.2)"
    second = (
        "import sys, time; sys.stdin.read(); "
        "blob = bytearray(40_000_000); blob[:] = b'x' * len(blob); time.sleep(2)"
    )
    result = run_pipeline(
        [Stage("leader", [PY, "-c", first]), Stage("downstream", [PY, "-c", second])],
        timeout_s=30,
    )
    assert result.ok
    assert result.sampled_tree_peak_rss_bytes is not None
    assert result.sampled_tree_peak_rss_bytes >= 40_000_000


def test_direct_and_sampled_tree_memory_remain_distinct() -> None:
    result = PipelineResult(
        stages=[StageOutcome("worker", peak_rss_bytes=100)],
        duration_s=1.0, peak_disk_bytes=None,
        sampled_tree_peak_rss_bytes=200,
    )
    assert result.peak_rss_bytes == 100
    assert result.sampled_tree_peak_rss_bytes == 200


def test_process_group_seed_includes_orphan_descendants() -> None:
    from traceback_lab.pipeline import _tree_members

    # The intermediate process was reparented, but still has the worker's
    # process group. Its own child must also be counted.
    assert _tree_members(1, {1: (0, 1), 2: (0, 1), 3: (2, 3)}) == {1, 2, 3}


def test_process_group_seed_excludes_unrelated_pid() -> None:
    from traceback_lab.pipeline import _tree_members

    assert _tree_members(1, {1: (0, 9), 2: (0, 1), 3: (1, 9)}) == {2}


def test_simulate_commands_accept_verbose() -> None:
    from traceback_lab.cli import build_parser

    for kind in ("cna", "split"):
        args = build_parser().parse_args(["simulate", kind, "latest", "--verbose"])
        assert args.verbose is True


@pytest.mark.parametrize("force_ps", [False, True])
def test_tree_rss_includes_new_session_child(monkeypatch: pytest.MonkeyPatch, force_ps: bool) -> None:
    if force_ps:
        from traceback_lab import pipeline

        def no_proc(_pid: int) -> int:
            raise OSError("/proc unavailable")

        monkeypatch.setattr(pipeline, "_proc_tree_rss_bytes", no_proc)
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; blob = bytearray(40_000_000); "
        "blob[:] = b\"x\" * len(blob); time.sleep(3)'], start_new_session=True)\n"
        "child.wait()\n"
    )
    result = run_pipeline([Stage("parent", [PY, "-c", script])], timeout_s=120)
    assert result.ok
    assert result.sampled_tree_peak_rss_bytes is not None
    assert result.sampled_tree_peak_rss_bytes >= 40_000_000


def test_proc_enumeration_failure_uses_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    from traceback_lab.pipeline import _process_tree_rss_bytes

    original = Path.iterdir

    def scan(path: Path):
        if path == Path("/proc"):
            raise OSError("synthetic proc failure")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", scan)
    assert (_process_tree_rss_bytes(os.getpid()) or 0) > 0


def test_empty_proc_sample_does_not_spawn_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    from traceback_lab import pipeline

    monkeypatch.setattr(pipeline, "_proc_tree_rss_bytes", lambda _pid: None)

    def unexpected_ps(_pid: int) -> int:
        raise AssertionError("ps fallback is only for unavailable /proc")

    monkeypatch.setattr(pipeline, "_ps_tree_rss_bytes", unexpected_ps)
    assert pipeline._process_tree_rss_bytes(os.getpid()) is None

def test_upstream_independent_failure_wins_despite_reversed_reap_times() -> None:
    """Observed reap times cannot prove which stage exited first."""

    result = PipelineResult(
        stages=[
            StageOutcome("minimap2", returncode=1, exited_at=2.10),
            StageOutcome("samtools sort", returncode=1, exited_at=2.00),
        ],
        duration_s=0.2,
        peak_disk_bytes=None,
    )
    assert result.failed_stage is result.stages[0]


@pytest.mark.parametrize(
    "consequence", [-signal.SIGPIPE, -signal.SIGTERM, -signal.SIGKILL]
)
def test_upstream_signal_consequence_does_not_hide_downstream_failure(
    consequence: int,
) -> None:
    result = PipelineResult(
        stages=[
            StageOutcome("writer", returncode=consequence, exited_at=2.00),
            StageOutcome("reader", returncode=1, exited_at=2.10),
        ],
        duration_s=0.2,
        peak_disk_bytes=None,
    )
    assert result.failed_stage is result.stages[1]


def test_only_signal_consequences_fall_back_to_first_failed_stage() -> None:
    result = PipelineResult(
        stages=[
            StageOutcome("writer", returncode=-signal.SIGPIPE, exited_at=2.10),
            StageOutcome("reader", returncode=-signal.SIGTERM, exited_at=2.00),
        ],
        duration_s=0.2,
        peak_disk_bytes=None,
    )
    assert result.failed_stage is result.stages[0]
    pending = PipelineResult(
        stages=[StageOutcome("pending"), StageOutcome("ok", returncode=0)],
        duration_s=0.2,
        peak_disk_bytes=None,
    )
    assert pending.failed_stage is None
