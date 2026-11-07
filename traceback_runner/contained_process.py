"""Run one external tool in its own process group with bounded logs.

The child starts a new session, so it and everything it spawns share one
process group.  On timeout, interrupt or an abort request (for example a lost
worker lease) the whole group gets SIGTERM, then SIGKILL after a grace period.
When the child exits normally, any process it left behind in its group is
killed too.  The leader is not reaped until after that group kill, so its
process-group ID cannot have been reused by an unrelated process.

Logs are bounded: each stream keeps its first and last ``log_limit_bytes / 2``
bytes and counts the rest.  Nothing is written to disk here; the caller decides
what to keep.

The argv, working directory and environment are taken exactly as given: no
``PATH`` lookup, no inherited environment, no shell.

``run_contained_pipeline`` is the N-process sibling: ``a | b | c`` in one
process group with the same SIGTERM, grace, SIGKILL semantics, one deadline,
one kill of the whole group, a bounded stderr tail per stage and per-stage peak
RSS from ``os.wait4``.  The Lab's algorithm workers and its alignment pipeline
run through it.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import IO, Any, BinaryIO, Literal, Protocol

DEFAULT_LOG_LIMIT_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024


class ContainedProcessError(ValueError):
    """The requested execution is malformed and was not started."""


class _BoundedBuffer:
    """Keep the head and tail of a byte stream; count everything."""

    def __init__(self, limit: int) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self._tail += chunk
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > len(self._head) + len(self._tail)

    def value(self) -> bytes:
        if not self.truncated:
            return bytes(self._head + self._tail)
        omitted = self.total - len(self._head) - len(self._tail)
        marker = f"\n[... {omitted} bytes omitted ...]\n".encode("ascii")
        return bytes(self._head) + marker + bytes(self._tail)


class _FileSink:
    """Write stdout to a new file, at most ``limit`` bytes; count the rest."""

    def __init__(self, path: Path, limit: int) -> None:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self._handle = os.fdopen(descriptor, "wb")
        self._limit = limit
        self._written = 0
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._limit - self._written
        if room > 0:
            part = chunk[:room]
            self._handle.write(part)
            self._written += len(part)

    def close(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()

    @property
    def truncated(self) -> bool:
        return self.total > self._written

    def value(self) -> bytes:
        return b""


def _drain(stream: BinaryIO, buffer: _BoundedBuffer | _FileSink) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK):
            buffer.add(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


@dataclass(frozen=True)
class ContainedResult:
    argv: tuple[str, ...]
    outcome: Literal["exited", "timeout", "aborted"]
    returncode: int | None
    duration_seconds: float
    stdout: bytes
    stderr: bytes
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def succeeded(self) -> bool:
        return self.outcome == "exited" and self.returncode == 0


def _exited_unreaped(pid: int, timeout: float) -> bool:
    """Wait up to ``timeout`` for ``pid`` to exit, WITHOUT reaping it.

    Linux uses ``waitid(WNOWAIT)``; macOS and the BSDs use a kqueue
    ``NOTE_EXIT`` filter.  A zombie keeps its PID and process-group ID, so a
    group kill after this returns can only reach the child's own group.
    """

    if hasattr(os, "waitid"):
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = os.waitid(
                    os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                return True
            if info is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
    kq = select.kqueue()
    try:
        event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )
        try:
            fired = kq.control([event], 1, max(timeout, 0.0))
        except ProcessLookupError:
            return True  # already a zombie: registration reports ESRCH
        return bool(fired)
    finally:
        kq.close()


def _finish_readers(
    process: subprocess.Popen[bytes],
    readers: list[threading.Thread],
    stdout_buffer: _BoundedBuffer | _FileSink,
    grace_seconds: float,
) -> None:
    for reader in readers:
        reader.join(timeout=grace_seconds)
    for stream in (process.stdout, process.stderr):
        if stream is not None and len(readers) < 2:
            try:
                stream.close()  # a reader that never started never closed it
            except OSError:
                pass
    if isinstance(stdout_buffer, _FileSink):
        stdout_buffer.close()


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _validate(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> tuple[str, ...]:
    items = tuple(argv)
    if not items:
        raise ContainedProcessError("argv is empty")
    if any(not isinstance(item, str) or "\x00" in item for item in items):
        raise ContainedProcessError("argv items must be NUL-free strings")
    if not os.path.isabs(items[0]):
        raise ContainedProcessError("the executable must be an absolute path")
    if not os.path.isabs(cwd) or not Path(cwd).is_dir():
        raise ContainedProcessError("the working directory must be an absolute directory")
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
        ):
            raise ContainedProcessError("environment entries must be NUL-free strings")
    return items


def run_contained(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout_seconds: float,
    log_limit_bytes: int = DEFAULT_LOG_LIMIT_BYTES,
    should_abort: Callable[[], bool] | None = None,
    grace_seconds: float = 5.0,
    poll_seconds: float = 0.2,
    stdout_path: Path | None = None,
    stdout_limit_bytes: int = 256 * 1024 * 1024,
) -> ContainedResult:
    """Run ``argv`` in a fresh process group; never leave the group running.

    ``KeyboardInterrupt`` (or any other exception raised while waiting) kills
    the group and is re-raised.  With ``stdout_path`` (a new file, absolute)
    stdout is data, not a log: it is written there, at most
    ``stdout_limit_bytes``, and ``stdout_truncated`` reports an overflow.
    """

    items = _validate(argv, env, cwd)
    if timeout_seconds <= 0:
        raise ContainedProcessError("timeout must be positive")
    if log_limit_bytes < 2:
        raise ContainedProcessError("log limit must be at least 2 bytes")
    if stdout_path is not None and not os.path.isabs(stdout_path):
        raise ContainedProcessError("stdout_path must be absolute")
    stdout_buffer: _BoundedBuffer | _FileSink = (
        _BoundedBuffer(log_limit_bytes)
        if stdout_path is None
        else _FileSink(Path(stdout_path), stdout_limit_bytes)
    )
    stderr_buffer = _BoundedBuffer(log_limit_bytes)
    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - absolute argv, no shell
            items,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        if isinstance(stdout_buffer, _FileSink):
            stdout_buffer.close()
        raise ContainedProcessError(f"could not start the executable: {exc}") from exc
    pgid = process.pid  # session leader: its PID is the group ID
    readers: list[threading.Thread] = []
    outcome: Literal["exited", "timeout", "aborted"] = "exited"
    try:
        # Inside the guard: an interrupt while the readers start still kills
        # the group.
        for stream, buffer in ((process.stdout, stdout_buffer), (process.stderr, stderr_buffer)):
            reader = threading.Thread(target=_drain, args=(stream, buffer), daemon=True)
            reader.start()
            readers.append(reader)
        deadline = started + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            if _exited_unreaped(process.pid, min(poll_seconds, remaining)):
                break
            if should_abort is not None and should_abort():
                outcome = "aborted"
                break
        if outcome != "exited":
            _signal_group(pgid, signal.SIGTERM)
            _exited_unreaped(process.pid, grace_seconds)
        # Exited normally or not: nothing of the group may outlive this call.
        _signal_group(pgid, signal.SIGKILL)
        process.wait()
    except BaseException:
        _signal_group(pgid, signal.SIGKILL)
        process.wait()
        _finish_readers(process, readers, stdout_buffer, grace_seconds)
        raise
    _finish_readers(process, readers, stdout_buffer, grace_seconds)
    return ContainedResult(
        argv=items,
        outcome=outcome,
        returncode=process.returncode if outcome == "exited" else None,
        duration_seconds=time.monotonic() - started,
        stdout=stdout_buffer.value(),
        stderr=stderr_buffer.value(),
        stdout_total_bytes=stdout_buffer.total,
        stderr_total_bytes=stderr_buffer.total,
        stdout_truncated=stdout_buffer.truncated,
        stderr_truncated=stderr_buffer.truncated,
    )


# --------------------------------------------------------------------------
# N-process pipelines in one process group (Lab D9, D20)

GRACE_S = 5.0
POLL_S = 0.02
DISK_SAMPLE_S = 2.0
RSS_SAMPLE_S = 0.5
FINAL_DISK_SAMPLE_S = 1.0


class LineScrubber(Protocol):
    """What ``BoundedStderr.scrubbed`` and ``PipelineResult`` need from a scrubber."""

    def line(self, text: str) -> str: ...

    def lines(self, lines: Iterable[str]) -> list[str]: ...


class BoundedStderr:
    """Keep the head and tail of a stream's lines, bounded in memory.

    Raw lines live only in memory; callers persist ``scrubbed()`` only.
    """

    def __init__(self, *, head: int = 20, tail: int = 50) -> None:
        self._head_limit = head
        self._head: list[str] = []
        self._tail: deque[str] = deque(maxlen=tail)
        self.dropped = 0
        self.total = 0

    def add(self, line: str) -> None:
        self.total += 1
        line = line.rstrip("\n")
        if len(self._head) < self._head_limit:
            self._head.append(line)
            return
        if len(self._tail) == self._tail.maxlen:
            self.dropped += 1
        self._tail.append(line)

    def raw_lines(self) -> list[str]:
        if self.dropped:
            return [*self._head, f"… {self.dropped} lines omitted …", *self._tail]
        return [*self._head, *self._tail]

    def scrubbed(self, scrubber: LineScrubber, *, last: int | None = None) -> list[str]:
        lines = scrubber.lines(self.raw_lines())
        return lines[-last:] if last else lines


@dataclass(frozen=True)
class Stage:
    name: str
    argv: Sequence[str]

    def __post_init__(self) -> None:
        if not self.argv:
            raise ContainedProcessError("a stage needs an argv")
        if not os.path.isabs(self.argv[0]):
            raise ContainedProcessError(
                "stage executables are absolute paths (no PATH lookup, no shell)"
            )


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
    timed_out: bool = False
    interrupted: bool = False
    sampled_tree_peak_rss_bytes: int | None = None
    aborted: bool = False  # ``on_tree_rss_sample`` asked for the group to stop

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.interrupted
            and not self.aborted
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
        return max(values) if values else None

    def first_line(self, scrubber: LineScrubber) -> str:
        stage = self.failed_stage
        if self.timed_out:
            return "timed out"
        if stage is None:
            return ""
        lines = [line for line in stage.stderr.scrubbed(scrubber) if line.strip()]
        detail = lines[-1] if lines else "no stderr"
        return scrubber.line(f"{stage.name} exited {stage.returncode}: {detail}")

    def stderr_tail(self, scrubber: LineScrubber, last: int = 50) -> list[str]:
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

    # Seed from observed members of the worker's process group, then include
    # their descendants even if a descendant starts a new session.
    included = {pid for pid, (_ppid, pgid) in processes.items() if pgid == root_pid}
    while True:
        children = {pid for pid, (ppid, _pgid) in processes.items() if ppid in included}
        expanded = included | children
        if expanded == included:
            break
        included = expanded
    return included


def _proc_tree_rss_bytes(root_pid: int) -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        raise OSError("/proc unavailable")
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
    except OSError as exc:
        raise OSError("/proc enumeration unavailable") from exc
    if not processes:
        return None
    resident_pages = 0
    measured = False
    for pid in _tree_members(root_pid, processes):
        try:
            resident_pages += int((proc / str(pid) / "statm").read_text().split()[1])
            measured = True
        except (OSError, ValueError, IndexError):
            continue
    if not measured or resident_pages == 0:
        return None
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
    total = sum(rss_kib.get(pid, 0) for pid in _tree_members(root_pid, processes))
    return total * 1024 if total > 0 else None


def _process_tree_rss_bytes(root_pid: int) -> int | None:
    """Sample the worker and native descendants, including new sessions.

    The caller separately records each direct child's wait4 maximum, which
    may include descendants that child waited for. Linux uses /proc resident
    pages; macOS uses ps RSS (KiB). A sampler failure leaves wait4 intact.
    """

    try:
        return _proc_tree_rss_bytes(root_pid)
    except OSError:
        return _ps_tree_rss_bytes(root_pid)


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


def _signal_pipeline(
    leader: subprocess.Popen[bytes], processes: Sequence[subprocess.Popen[bytes]], sig: int
) -> None:
    """Signal the whole group.  macOS refuses ``killpg`` (EPERM) while the group
    holds an unreaped zombie, so fall back to the direct children; the final
    group kill after reaping still reaches any grandchild."""

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

    _signal_pipeline(leader, processes, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_S
    while time.monotonic() < deadline:
        for process, outcome in zip(processes, outcomes):
            _reap(process, outcome, block=False)
        if all(process.returncode is not None for process in processes):
            return
        time.sleep(POLL_S)
    _signal_pipeline(leader, processes, signal.SIGKILL)


def run_contained_pipeline(
    stages: Sequence[Stage],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
    echo: Callable[[str], None] | None = None,
    disk_path: Path | None = None,
    disk_usage: Callable[[], int] | None = None,
    on_tick: Callable[[float], None] | None = None,
    preexec_fn: Callable[[], None] | None = None,
    tree_rss_sampler: Callable[[int], int | None] | None = None,
    on_tree_rss_sample: Callable[[int], bool] | None = None,
) -> PipelineResult:
    """Run ``stages`` as one pipeline in one process group.

    The first stage starts as a new process-group leader and every later one
    joins its group, stdout piped to the next stage's stdin.  On a non-zero
    exit, the deadline, an interrupt or an abort the group gets SIGTERM, then
    SIGKILL after ``GRACE_S``; after a clean finish the group is killed too,
    so no grandchild outlives the call.  ``KeyboardInterrupt`` kills the group
    and is re-raised.

    ``stdin_path`` (absolute) feeds the first stage; otherwise it reads
    ``/dev/null``.  ``stdout_path`` (absolute, new) receives the last stage's
    stdout; otherwise it is discarded.  Each stage's stderr is kept in a
    bounded head-and-tail buffer; callers persist only the scrubbed text.
    ``echo`` may stream raw stderr to the operator's own terminal.

    Per-stage peak RSS comes from ``os.wait4``.  The tree RSS (the group and
    descendants in new sessions) is sampled every ``RSS_SAMPLE_S``, by
    ``tree_rss_sampler`` when given; ``on_tree_rss_sample`` sees every sample
    and may return True to stop the group (``PipelineResult.aborted``).
    ``preexec_fn`` runs in every child between fork and exec.  ``disk_path``
    or ``disk_usage`` is sampled for peak disk.
    """

    if not stages:
        raise ContainedProcessError("a pipeline needs at least one stage")
    if timeout_s <= 0:
        raise ContainedProcessError("timeout must be positive")
    if stdin_path is not None and not Path(stdin_path).is_absolute():
        raise ContainedProcessError("stdin_path must be absolute")
    if stdout_path is not None and not Path(stdout_path).is_absolute():
        raise ContainedProcessError("stdout_path must be absolute")
    outcomes = [StageOutcome(stage.name) for stage in stages]
    processes: list[subprocess.Popen[bytes]] = []
    threads: list[threading.Thread] = []
    started = time.monotonic()
    timed_out = False
    interrupted = False
    aborted = False
    peak_disk: int | None = None
    peak_tree_rss: int | None = None
    rss_samples: SimpleQueue[int | None] = SimpleQueue()
    rss_thread: threading.Thread | None = None
    rss_active = threading.Event()
    rss_active.set()
    disk_samples: SimpleQueue[int | None] = SimpleQueue()
    disk_thread: threading.Thread | None = None
    source: IO[bytes] | None = None
    sink: IO[bytes] | None = None
    leader: subprocess.Popen[bytes] | None = None

    def sample_disk() -> None:
        try:
            size = disk_usage() if disk_usage is not None else (
                directory_bytes(disk_path) if disk_path is not None else None
            )
        except Exception:  # noqa: BLE001 - optional telemetry cannot fail the worker
            size = None
        disk_samples.put(size)

    try:
        if stdin_path is not None:
            source = Path(stdin_path).open("rb")
        if stdout_path is not None:
            sink = Path(stdout_path).open("xb")
        previous_stdout: IO[bytes] | None = None
        for position, stage in enumerate(stages):
            last = position == len(stages) - 1
            if previous_stdout is not None:
                stdin: IO[bytes] | int = previous_stdout
            elif source is not None:
                stdin = source
            else:
                stdin = subprocess.DEVNULL
            process = subprocess.Popen(  # noqa: S603 - absolute argv, never a shell
                list(stage.argv),
                stdin=stdin,
                stdout=(sink if sink is not None else subprocess.DEVNULL) if last else subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=dict(env) if env is not None else None,
                process_group=0 if leader is None else leader.pid,
                close_fds=True,
                preexec_fn=preexec_fn,
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
        leader_pid = leader.pid
        sampler = tree_rss_sampler
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
            stop = False
            try:
                while True:
                    rss = rss_samples.get_nowait()
                    if rss is not None:
                        peak_tree_rss = rss if peak_tree_rss is None else max(peak_tree_rss, rss)
                        if on_tree_rss_sample is not None and on_tree_rss_sample(rss):
                            stop = True
                    next_rss = time.monotonic() + RSS_SAMPLE_S
            except Empty:
                pass
            if stop and any(process.returncode is None for process in processes):
                aborted = True
                _terminate(leader, processes, outcomes)
                break
            if now >= next_rss and (rss_thread is None or not rss_thread.is_alive()):
                def sample_rss() -> None:
                    try:
                        if not rss_active.is_set():
                            rss = None
                        elif sampler is not None:
                            rss = sampler(leader_pid)
                        else:
                            rss = _process_tree_rss_bytes(leader_pid)
                    except Exception:  # noqa: BLE001 - optional telemetry cannot fail the worker
                        rss = None
                    if rss_active.is_set():
                        rss_samples.put(rss)

                rss_thread = threading.Thread(target=sample_rss, daemon=True)
                try:
                    rss_thread.start()
                except RuntimeError:
                    next_rss = time.monotonic() + RSS_SAMPLE_S
            try:
                while True:
                    size = disk_samples.get_nowait()
                    if size is not None:
                        peak_disk = size if peak_disk is None else max(peak_disk, size)
                    next_disk = time.monotonic() + DISK_SAMPLE_S
            except Empty:
                pass
            if ((disk_path is not None or disk_usage is not None) and now >= next_disk
                    and (disk_thread is None or not disk_thread.is_alive())):
                disk_thread = threading.Thread(target=sample_disk, daemon=True)
                try:
                    disk_thread.start()
                except RuntimeError:
                    next_disk = time.monotonic() + DISK_SAMPLE_S
            now = time.monotonic()
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
        # A process-group number may be reused after its last member exits.
        # Discard any sample still being enumerated as the group is reaped.
        rss_active.clear()
        if leader is not None and any(process.returncode is None for process in processes):
            _terminate(leader, processes, outcomes)
        for position, process in enumerate(processes):
            _reap(process, outcomes[position], block=True)
        worker_ended = time.monotonic()
        if leader is not None:
            # Every direct child is reaped; end the group so no grandchild
            # outlives the step (SIGKILL the group, then reap the leader).
            _signal_group(leader.pid, signal.SIGKILL)
            leader.wait()
        for thread in threads:
            thread.join(timeout=5)
        if source is not None:
            source.close()
        if sink is not None:
            sink.close()
    if disk_path is not None or disk_usage is not None:
        # A periodic scan may have started before the worker's last write.
        # Only a fresh post-exit scan can serve as the final reading.
        final_disk_thread = threading.Thread(target=sample_disk, daemon=True)
        try:
            final_disk_thread.start()
        except RuntimeError:
            pass  # optional telemetry must not discard a completed worker
        else:
            final_disk_thread.join(timeout=FINAL_DISK_SAMPLE_S)
        try:
            while True:
                size = disk_samples.get_nowait()
                if size is not None:
                    peak_disk = size if peak_disk is None else max(peak_disk, size)
        except Empty:
            pass
    try:
        while True:
            rss = rss_samples.get_nowait()
            if rss is not None:
                peak_tree_rss = rss if peak_tree_rss is None else max(peak_tree_rss, rss)
    except Empty:
        pass
    return PipelineResult(
        stages=outcomes,
        duration_s=worker_ended - started,
        peak_disk_bytes=peak_disk,
        sampled_tree_peak_rss_bytes=peak_tree_rss,
        timed_out=timed_out,
        interrupted=interrupted,
        aborted=aborted,
    )


__all__ = [
    "DEFAULT_LOG_LIMIT_BYTES",
    "BoundedStderr",
    "ContainedProcessError",
    "ContainedResult",
    "LineScrubber",
    "PipelineResult",
    "Stage",
    "StageOutcome",
    "directory_bytes",
    "run_contained",
    "run_contained_pipeline",
]
