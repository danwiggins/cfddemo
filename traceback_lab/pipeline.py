"""N-process pipelines in one process group, never leaving anything behind (D9, D20).

``contained_process.run_contained`` runs one tool with stdin ``/dev/null``, so
it cannot run ``samtools cat | samtools fastq | minimap2 | samtools sort``.
This module starts the first process as a new process-group leader and every
later one with ``process_group=<leader pid>``, so the whole pipeline is one
group.  On a non-zero exit, a timeout or an interrupt the group gets SIGTERM,
then SIGKILL after a grace period (``toolchain.kill_process_group``).  After a
clean finish the group is killed too, so no grandchild outlives the step.

Each process's stderr is kept in a bounded head-and-tail buffer.  Callers
persist only the scrubbed text (D34).  ``echo`` may stream raw stderr to the
operator's own terminal (``--verbose``); that text is never stored.

Per-process peak RSS comes from ``os.wait4``; the caller may pass a directory
whose size is sampled for peak disk.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from traceback_runner.toolchain import kill_process_group

from .scrub import BoundedStderr, Scrubber

GRACE_S = 5.0
POLL_S = 0.02
DISK_SAMPLE_S = 2.0
RSS_SAMPLE_S = 0.5


@dataclass(frozen=True)
class Stage:
    name: str
    argv: Sequence[str]

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a stage needs an argv")
        if not os.path.isabs(self.argv[0]):
            raise ValueError("stage executables are absolute paths (no PATH lookup, no shell)")


@dataclass
class StageOutcome:
    name: str
    returncode: int | None = None
    peak_rss_bytes: int | None = None
    stderr: BoundedStderr = field(default_factory=BoundedStderr)
    exited_at: float | None = None  # monotonic time the exit was observed


@dataclass
class PipelineResult:
    stages: list[StageOutcome]
    duration_s: float
    peak_disk_bytes: int | None
    sampled_tree_peak_rss_bytes: int | None = None
    timed_out: bool = False
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.interrupted
            and all(stage.returncode == 0 for stage in self.stages)
        )

    @property
    def failed_stage(self) -> StageOutcome | None:
        """The upstreammost independent non-zero failure in the pipeline.

        Stages killed by us (SIGTERM/SIGKILL) or by a broken pipe (SIGPIPE)
        are consequences. A reader may reject truncated input after its
        writer fails; selecting by observed reap time is scheduling-dependent
        and cannot establish which process actually exited first. If every
        failure is a signal consequence, return the first such stage only as
        context.
        """

        consequences = {-signal.SIGTERM, -signal.SIGKILL, -signal.SIGPIPE}
        for stage in self.stages:
            if stage.returncode not in (0, None) and stage.returncode not in consequences:
                return stage
        for stage in self.stages:
            if stage.returncode not in (0, None):
                return stage
        return None

    @property
    def peak_rss_bytes(self) -> int | None:
        values = [stage.peak_rss_bytes for stage in self.stages if stage.peak_rss_bytes]
        if self.sampled_tree_peak_rss_bytes:
            values.append(self.sampled_tree_peak_rss_bytes)
        return max(values) if values else None

    def first_line(self, scrubber: Scrubber) -> str:
        stage = self.failed_stage
        if self.timed_out:
            return "timed out"
        if stage is None:
            return ""
        lines = [line for line in stage.stderr.scrubbed(scrubber) if line.strip()]
        detail = lines[-1] if lines else "no stderr"
        return scrubber.line(f"{stage.name} exited {stage.returncode}: {detail}")

    def stderr_tail(self, scrubber: Scrubber, last: int = 50) -> list[str]:
        lines: list[str] = []
        for stage in self.stages:
            scrubbed = stage.stderr.scrubbed(scrubber)
            lines.extend(f"[{stage.name}] {line}" for line in scrubbed)
        return [scrubber.line(line) for line in lines[-last:]]


def _rss_bytes(rusage: Any) -> int:
    # ru_maxrss is bytes on macOS and KiB on Linux.
    value = int(rusage.ru_maxrss)
    return value if os.uname().sysname == "Darwin" else value * 1024


def directory_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _tree_members(root_pid: int, processes: Mapping[int, tuple[int, int]]) -> set[int]:
    """Include descendants in new sessions and children still in the group."""

    included = {root_pid}
    while True:
        children = {pid for pid, (ppid, _pgid) in processes.items() if ppid in included}
        expanded = included | children
        if expanded == included:
            break
        included = expanded
    included.update(pid for pid, (_ppid, pgid) in processes.items() if pgid == root_pid)
    return included


def _proc_tree_rss_bytes(root_pid: int) -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    processes: dict[int, tuple[int, int]] = {}
    try:
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                stat = (entry / "stat").read_text()
                fields = stat[stat.rfind(")") + 2 :].split()
                processes[int(entry.name)] = (int(fields[1]), int(fields[2]))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return None
    resident_pages = 0
    for pid in _tree_members(root_pid, processes):
        try:
            resident_pages += int((proc / str(pid) / "statm").read_text().split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def _ps_tree_rss_bytes(root_pid: int) -> int | None:
    """macOS fallback: ps RSS is KiB; cap command time and parsed output."""

    try:
        listing = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,rss="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0 or len(listing.stdout) > 4_000_000:
        return None
    processes: dict[int, tuple[int, int]] = {}
    rss_kib: dict[int, int] = {}
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        try:
            pid, ppid, pgid, rss = map(int, fields)
        except ValueError:
            continue
        processes[pid] = (ppid, pgid)
        rss_kib[pid] = rss
    return sum(rss_kib.get(pid, 0) for pid in _tree_members(root_pid, processes)) * 1024


def _process_tree_rss_bytes(root_pid: int) -> int | None:
    """Sample the worker and native descendants, including new sessions.

    wait4 covers direct children only. Linux uses /proc resident pages;
    macOS uses bounded ps RSS (KiB). A sampler failure leaves wait4 intact.
    """

    sampled = _proc_tree_rss_bytes(root_pid)
    return sampled if sampled is not None else _ps_tree_rss_bytes(root_pid)


def _pump(stream: IO[bytes], buffer: BoundedStderr, echo: Callable[[str], None] | None) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        buffer.add(line)
        if echo is not None:
            try:
                echo(line)
            except Exception:  # noqa: BLE001 - a broken terminal never fails the step
                pass
    stream.close()


def _signal_group(
    leader: subprocess.Popen[bytes], processes: Sequence[subprocess.Popen[bytes]], sig: int
) -> None:
    """Signal the whole group.  macOS refuses ``killpg`` (EPERM) while the group
    holds an unreaped zombie, so fall back to the direct children; the final
    ``kill_process_group`` after reaping still reaches any grandchild."""

    try:
        os.killpg(leader.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        for process in processes:
            if process.returncode is None:
                try:
                    process.send_signal(sig)
                except (ProcessLookupError, PermissionError):
                    pass


def _reap(process: subprocess.Popen[bytes], outcome: StageOutcome, *, block: bool) -> bool:
    """Reap one direct child if it exited; record code, RSS and exit time."""

    if process.returncode is not None:
        return False
    try:
        pid, status, rusage = os.wait4(process.pid, 0 if block else os.WNOHANG)
    except ChildProcessError:  # reaped elsewhere
        process.poll()
        if process.returncode is None:
            process.returncode = -signal.SIGKILL
    else:
        if pid == 0:
            return False
        process.returncode = os.waitstatus_to_exitcode(status)
        outcome.peak_rss_bytes = _rss_bytes(rusage)
    outcome.returncode = process.returncode
    outcome.exited_at = time.monotonic()
    return True


def _terminate(
    leader: subprocess.Popen[bytes],
    processes: Sequence[subprocess.Popen[bytes]],
    outcomes: Sequence[StageOutcome],
) -> None:
    """SIGTERM the group, reap for up to the grace period, then SIGKILL the rest."""

    _signal_group(leader, processes, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_S
    while time.monotonic() < deadline:
        for process, outcome in zip(processes, outcomes):
            _reap(process, outcome, block=False)
        if all(process.returncode is not None for process in processes):
            return
        time.sleep(POLL_S)
    _signal_group(leader, processes, signal.SIGKILL)


def run_pipeline(
    stages: Sequence[Stage],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdout_path: Path | None = None,
    echo: Callable[[str], None] | None = None,
    disk_path: Path | None = None,
    disk_usage: Callable[[], int] | None = None,
    on_tick: Callable[[float], None] | None = None,
) -> PipelineResult:
    """Run ``stages`` as one pipeline in one process group.

    ``KeyboardInterrupt`` kills the group and is re-raised.  ``stdout_path``
    (absolute, new) receives the last stage's stdout; otherwise it is
    discarded.
    """

    if not stages:
        raise ValueError("a pipeline needs at least one stage")
    if timeout_s <= 0:
        raise ValueError("timeout must be positive")
    outcomes = [StageOutcome(stage.name) for stage in stages]
    processes: list[subprocess.Popen[bytes]] = []
    threads: list[threading.Thread] = []
    started = time.monotonic()
    timed_out = False
    interrupted = False
    peak_disk: int | None = None
    peak_tree_rss: int | None = None
    sink: IO[bytes] | None = None
    leader: subprocess.Popen[bytes] | None = None
    try:
        if stdout_path is not None:
            if not stdout_path.is_absolute():
                raise ValueError("stdout_path must be absolute")
            sink = stdout_path.open("xb")
        previous_stdout: IO[bytes] | None = None
        for position, stage in enumerate(stages):
            last = position == len(stages) - 1
            process = subprocess.Popen(  # noqa: S603 - absolute argv, never a shell
                list(stage.argv),
                stdin=previous_stdout if previous_stdout is not None else subprocess.DEVNULL,
                stdout=(sink if sink is not None else subprocess.DEVNULL) if last else subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=dict(env) if env is not None else None,
                process_group=0 if leader is None else leader.pid,
                close_fds=True,
            )
            if previous_stdout is not None:
                previous_stdout.close()  # the child holds its own copy
            previous_stdout = process.stdout if not last else None
            if leader is None:
                leader = process
            processes.append(process)
            assert process.stderr is not None
            thread = threading.Thread(
                target=_pump,
                args=(process.stderr, outcomes[position].stderr, echo),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        assert leader is not None
        deadline = started + timeout_s
        next_disk = 0.0
        next_rss = 0.0
        while any(process.returncode is None for process in processes):
            failed = False
            for position in range(len(processes)):
                if _reap(processes[position], outcomes[position], block=False):
                    failed = failed or processes[position].returncode != 0
            if failed:
                # One failed stage fails the pipeline: stop the rest now.
                _terminate(leader, processes, outcomes)
                break
            now = time.monotonic()
            if now >= next_rss:
                rss = _process_tree_rss_bytes(leader.pid)
                if rss is not None:
                    peak_tree_rss = rss if peak_tree_rss is None else max(peak_tree_rss, rss)
                next_rss = time.monotonic() + RSS_SAMPLE_S
            if (disk_path is not None or disk_usage is not None) and now >= next_disk:
                size = disk_usage() if disk_usage is not None else directory_bytes(disk_path)
                peak_disk = size if peak_disk is None else max(peak_disk, size)
                next_disk = time.monotonic() + DISK_SAMPLE_S
            if on_tick is not None:
                on_tick(now - started)
            if any(process.returncode is None for process in processes) and now > deadline:
                timed_out = True
                _terminate(leader, processes, outcomes)
                break
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        if leader is not None and any(process.returncode is None for process in processes):
            _terminate(leader, processes, outcomes)
        for position, process in enumerate(processes):
            _reap(process, outcomes[position], block=True)
        if leader is not None:
            # Every direct child is reaped; end the group so no grandchild
            # outlives the step (toolchain.kill_process_group: SIGKILL + reap).
            try:
                kill_process_group(leader)
            except PermissionError:
                pass
        for thread in threads:
            thread.join(timeout=5)
        if sink is not None:
            sink.close()
    if disk_path is not None or disk_usage is not None:
        size = disk_usage() if disk_usage is not None else directory_bytes(disk_path)
        peak_disk = size if peak_disk is None else max(peak_disk, size)
    return PipelineResult(
        stages=outcomes,
        duration_s=time.monotonic() - started,
        peak_disk_bytes=peak_disk,
        sampled_tree_peak_rss_bytes=peak_tree_rss,
        timed_out=timed_out,
        interrupted=interrupted,
    )


__all__ = [
    "PipelineResult",
    "Stage",
    "StageOutcome",
    "directory_bytes",
    "run_pipeline",
]
