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
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

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


__all__ = [
    "DEFAULT_LOG_LIMIT_BYTES",
    "ContainedProcessError",
    "ContainedResult",
    "run_contained",
]
