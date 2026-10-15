"""The Lab store under ``ROOT/lab``: layout, writer lock, atomic publish, run log.

Layout (D11, D19, D22, D35)::

    ROOT/lab/STORE_VERSION        integer; newer than this code -> TBX-LAB-018
    ROOT/lab/.lock                flock held by the one active writer
    ROOT/lab/active.json          the writer's heartbeat (every 10 s); readers
                                  never touch the lock
    ROOT/lab/generation           integer bumped by every publish
    ROOT/lab/runs/<run_id>.jsonl  one line per step event; hashes, never paths
    ROOT/lab/sources/<source_id>.json
    ROOT/lab/inputs/index.json    hash cache (realpath, size, mtime_ns) -> digest
    ROOT/lab/inputs/<input_id>/   aligned or derived input (published as a unit)
    ROOT/lab/results/<input_id>/<algorithm_id>/<parameters_sha256>/
        attempts/<attempt_id>/{result.json, artifacts/}
        selected.json             {attempt_id}, switched by os.replace

Every multi-file write is staged in a ``.staging-<random>`` sibling and
published with one ``rename_directory_exclusive_at`` call, so readers see the
old state or the new state, never a mix.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes

from . import STORE_VERSION
from .envelope import ResultEnvelope
from .identity import ALGORITHM_ID_PATTERN, SHA256_PATTERN
from .problems import LabProblem

HEARTBEAT_INTERVAL_S = 10.0
HEARTBEAT_FRESH_S = 30.0
KEEP_FAILED_ATTEMPTS = 3
STAGING_PREFIX = ".staging-"
_RUN_ID = r"^r-\d{8}-\d{4}-[0-9a-f]{4,8}$"
_ATTEMPT_ID = r"^a-\d{8}T\d{12}-[0-9a-f]{8}$"


def _utc(epoch: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_atomic(path: Path, content: bytes) -> None:
    """Write ``content`` by temp file + ``os.replace`` in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def new_run_id(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    return f"r-{stamp.strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"


def new_attempt_id(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    # Microseconds keep attempt ids in publish order for pruning.
    return f"a-{stamp.strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class ActiveRun:
    """What ``active.json`` says, and whether it is believable now."""

    run_id: str
    pid: int
    command: str
    step: str
    started_utc: str
    heartbeat_utc: str
    heartbeat_age_s: float
    pid_alive: bool

    @property
    def active(self) -> bool:
        return self.pid_alive and self.heartbeat_age_s < HEARTBEAT_FRESH_S

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "command": self.command,
            "step": self.step,
            "started_utc": self.started_utc,
            "heartbeat_utc": self.heartbeat_utc,
            "heartbeat_age_s": round(self.heartbeat_age_s, 1),
            "active": self.active,
        }


class LabStore:
    """Paths and primitives for one ``ROOT/lab``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.lab = self.root / "lab"

    # -- layout -----------------------------------------------------------
    @property
    def version_file(self) -> Path:
        return self.lab / "STORE_VERSION"

    @property
    def lock_file(self) -> Path:
        return self.lab / ".lock"

    @property
    def active_file(self) -> Path:
        return self.lab / "active.json"

    @property
    def generation_file(self) -> Path:
        return self.lab / "generation"

    @property
    def runs_dir(self) -> Path:
        return self.lab / "runs"

    @property
    def sources_dir(self) -> Path:
        return self.lab / "sources"

    @property
    def inputs_dir(self) -> Path:
        return self.lab / "inputs"

    @property
    def results_dir(self) -> Path:
        return self.lab / "results"

    @property
    def hash_cache_file(self) -> Path:
        return self.inputs_dir / "index.json"

    def result_dir(self, input_id: str, algorithm_id: str, parameters_sha256: str) -> Path:
        for value, name in ((input_id, "input_id"), (parameters_sha256, "parameters_sha256")):
            if not SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"{name} must be a sha256 hex digest")
        if not ALGORITHM_ID_PATTERN.fullmatch(algorithm_id):
            raise ValueError("algorithm id must match ^[a-z0-9-]{1,40}$")
        return self.results_dir / input_id / algorithm_id / parameters_sha256

    # -- version ----------------------------------------------------------
    def stored_version(self) -> int | None:
        try:
            text = self.version_file.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        try:
            return int(text)
        except ValueError as exc:
            raise LabProblem(
                "TBX-LAB-018",
                cause=f"ROOT/lab/STORE_VERSION is not an integer ({text[:20]!r})",
                fix="Restore STORE_VERSION from a backup, or move ROOT/lab aside",
                data={"store_version": None, "code_version": STORE_VERSION},
            ) from exc

    def check_version(self) -> None:
        """Refuse a store written by a newer checkout (read-only)."""

        stored = self.stored_version()
        if stored is not None and stored > STORE_VERSION:
            raise LabProblem(
                "TBX-LAB-018",
                cause=f"store version {stored} is newer than this checkout's {STORE_VERSION}",
                fix="Update the checkout (git pull && uv sync); the store is never downgraded",
                data={"store_version": stored, "code_version": STORE_VERSION},
            )

    def ensure(self) -> None:
        """Create ``ROOT/lab`` and its version file (writers only)."""

        self.check_version()
        self.lab.mkdir(parents=True, exist_ok=True)
        if self.stored_version() is None:
            write_atomic(self.version_file, f"{STORE_VERSION}\n".encode("ascii"))

    # -- generation counter ----------------------------------------------
    def generation(self) -> int:
        try:
            return int(self.generation_file.read_text(encoding="ascii").strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    def bump_generation(self) -> int:
        """Increment the publish counter (callers hold the writer lock)."""

        value = self.generation() + 1
        write_atomic(self.generation_file, f"{value}\n".encode("ascii"))
        return value

    # -- active heartbeat -------------------------------------------------
    def read_active(self, *, now: float | None = None) -> ActiveRun | None:
        """Parse ``active.json`` without touching the lock (D22)."""

        try:
            data = json.loads(self.active_file.read_bytes())
            heartbeat = datetime.strptime(data["heartbeat_utc"], "%Y-%m-%dT%H:%M:%SZ")
            pid = int(data["pid"])
        except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
            return None
        epoch = heartbeat.replace(tzinfo=UTC).timestamp()
        current = time.time() if now is None else now
        return ActiveRun(
            run_id=str(data.get("run_id", "")),
            pid=pid,
            command=str(data.get("command", "")),
            step=str(data.get("step", "")),
            started_utc=str(data.get("started_utc", "")),
            heartbeat_utc=str(data["heartbeat_utc"]),
            heartbeat_age_s=max(0.0, current - epoch),
            pid_alive=pid_alive(pid),
        )

    def status(self) -> dict[str, Any]:
        """``/api/status``: two small reads, never a directory scan, never the lock."""

        active = self.read_active()
        return {
            "generation": self.generation(),
            "active": active.public() if active is not None and active.active else None,
        }

    @contextmanager
    def writer(
        self,
        command: str,
        run_id: str,
        *,
        step: str = "start",
        heartbeat_s: float = HEARTBEAT_INTERVAL_S,
    ) -> Iterator[WriterLock]:
        """Hold the exclusive, non-blocking writer lock for the whole command."""

        self.ensure()
        handle = self.lock_file.open("a+b")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise self._busy_problem() from exc
            lock = WriterLock(self, command, run_id, step, heartbeat_s)
            lock.start()
            try:
                yield lock
            finally:
                lock.stop()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _busy_problem(self) -> LabProblem:
        active = self.read_active()
        if active is None:
            return LabProblem(
                "TBX-LAB-012",
                cause="another Lab writer holds ROOT/lab/.lock (no heartbeat yet)",
                fix="Wait for it to finish (traceback lab runs), or stop that process",
                data={"holder": None},
            )
        started = active.started_utc
        age_min = None
        try:
            started_epoch = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            age_min = int((time.time() - started_epoch.timestamp()) // 60)
        except ValueError:
            pass
        age_text = f" ({age_min} min ago)" if age_min is not None else ""
        return LabProblem(
            "TBX-LAB-012",
            cause=(
                f"another Lab writer is running: `lab {active.command}` {active.run_id}, "
                f"pid {active.pid}, step {active.step}, started {started}{age_text}"
            ),
            fix="Wait for it to finish (traceback lab runs), or stop that process",
            data={
                "holder": {
                    "command": active.command,
                    "run_id": active.run_id,
                    "pid": active.pid,
                    "step": active.step,
                    "started_utc": started,
                    "age_min": age_min,
                }
            },
        )

    # -- atomic directory publish (D35) ------------------------------------
    def publish_directory(
        self, parent: Path, name: str, build: Callable[[Path], None]
    ) -> Path:
        """Stage a directory with ``build(staging)`` and publish it as ``parent/name``.

        Raises ``FileExistsError`` if ``parent/name`` already exists.  Any
        failure removes the staging directory.
        """

        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f"{STAGING_PREFIX}{secrets.token_hex(8)}"
        staging.mkdir()
        published = False
        try:
            build(staging)
            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            _fsync_dir(staging)
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                rename_directory_exclusive_at(descriptor, staging.name, name)
                published = True
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)
        return parent / name

    def orphaned_staging(self) -> list[Path]:
        """``.staging-*`` directories left by a killed writer (doctor reports them)."""

        found: list[Path] = []
        for base in (self.inputs_dir, self.results_dir, self.lab / "cohorts", self.lab / "assets"):
            if base.is_dir():
                found.extend(path for path in base.rglob(f"{STAGING_PREFIX}*") if path.is_dir())
        return sorted(found)

    # -- results ------------------------------------------------------------
    def publish_result(
        self,
        envelope: ResultEnvelope,
        *,
        input_id: str,
        artifacts: Mapping[str, bytes] | None = None,
        artifacts_dir: Path | None = None,
        attempt_id: str | None = None,
    ) -> str:
        """Publish one immutable attempt, then select it (callers hold the lock).

        ``artifacts_dir`` (a directory on the same volume, e.g. a worker's
        scratch) is moved into the attempt as ``artifacts/`` instead of being
        copied, so large columnar artifacts never pass through memory.
        """

        directory = self.result_dir(
            input_id, envelope.algorithm_id, envelope.provenance.parameters_sha256
        )
        if envelope.provenance.input_id != input_id:
            raise ValueError("a result is stored under the input it consumed (D19)")
        attempts = directory / "attempts"
        attempt = attempt_id or new_attempt_id()
        files = dict(artifacts or {})
        for relative in files:
            parts = Path(relative).parts
            if Path(relative).is_absolute() or ".." in parts or not parts:
                raise ValueError("artifact paths are relative and contained")

        def build(staging: Path) -> None:
            (staging / "result.json").write_bytes(envelope.to_bytes())
            if artifacts_dir is not None:
                for path in artifacts_dir.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("artifacts may not contain symlinks")
                os.rename(artifacts_dir, staging / "artifacts")
            for relative, content in files.items():
                target = staging / "artifacts" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

        previous = self.selected_attempt(
            input_id, envelope.algorithm_id, envelope.provenance.parameters_sha256
        )
        self.publish_directory(attempts, attempt, build)
        write_atomic(
            directory / "selected.json", canonical_json_bytes({"attempt_id": attempt})
        )
        self._prune_attempts(
            attempts, keep={attempt, *([previous.name] if previous is not None else [])}
        )
        self.bump_generation()
        return attempt

    @staticmethod
    def _attempt_status(path: Path) -> str | None:
        try:
            return str(json.loads((path / "result.json").read_bytes()).get("status"))
        except (OSError, ValueError, AttributeError):
            return None

    def _prune_attempts(self, attempts: Path, *, keep: set[str]) -> None:
        """Keep the selected attempt, the one it replaced (a reader may still
        hold it) and the newest ``KEEP_FAILED_ATTEMPTS`` failed attempts for
        Trust/QC (D35).  Older superseded attempts are removed."""

        others = sorted(
            (
                path
                for path in attempts.iterdir()
                if path.is_dir()
                and path.name not in keep
                and not path.name.startswith(STAGING_PREFIX)
            ),
            key=lambda path: path.name,
        )
        failed = [path for path in others if self._attempt_status(path) == "failed"]
        retained = {path.name for path in failed[-KEEP_FAILED_ATTEMPTS:]}
        for stale in others:
            if stale.name not in retained:
                shutil.rmtree(stale, ignore_errors=True)

    def selected_artifacts(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> Path | None:
        attempt = self.selected_attempt(input_id, algorithm_id, parameters_sha256)
        return attempt / "artifacts" if attempt is not None else None

    def selected_attempt(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> Path | None:
        """The selected attempt directory, read via ``selected.json``."""

        directory = self.result_dir(input_id, algorithm_id, parameters_sha256)
        try:
            data = json.loads((directory / "selected.json").read_bytes())
            attempt = str(data["attempt_id"])
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None
        if not re.fullmatch(_ATTEMPT_ID, attempt):
            return None
        path = directory / "attempts" / attempt
        return path if path.is_dir() else None

    def read_result(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> ResultEnvelope | None:
        attempt = self.selected_attempt(input_id, algorithm_id, parameters_sha256)
        if attempt is None:
            return None
        return ResultEnvelope.from_bytes((attempt / "result.json").read_bytes())

    # -- hash cache (D11) ---------------------------------------------------
    def load_hash_cache(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.hash_cache_file.read_bytes())
        except (FileNotFoundError, ValueError):
            return {}
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        return {str(key): value for key, value in entries.items() if isinstance(value, dict)}

    def save_hash_cache(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        """Rewrite the cache (callers hold the writer lock)."""

        write_atomic(
            self.hash_cache_file,
            canonical_json_bytes({"schema": "traceback.lab.hash-cache.v1", "entries": dict(entries)}),
        )

    @staticmethod
    def hash_cache_key(path: Path) -> str:
        real = os.path.realpath(path)
        info = os.stat(real)
        return f"{real}\0{info.st_size}\0{info.st_mtime_ns}"

    # -- run log (D22) ------------------------------------------------------
    def run_log(self, run_id: str) -> RunLog:
        return RunLog(self, run_id)

    def run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        names = [path.stem for path in self.runs_dir.glob("r-*.jsonl")]
        return sorted((name for name in names if re.fullmatch(_RUN_ID, name)), reverse=True)


class WriterLock:
    """The held writer lock: heartbeat thread plus the current step."""

    def __init__(
        self, store: LabStore, command: str, run_id: str, step: str, interval_s: float
    ) -> None:
        self.store = store
        self.command = command
        self.run_id = run_id
        self._step = step
        self._interval = interval_s
        self._started = _utc()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()

    @property
    def step(self) -> str:
        return self._step

    def set_step(self, step: str) -> None:
        self._step = step
        self.beat()

    def beat(self) -> None:
        with self._write_lock:
            if self._stop.is_set():
                return
            write_atomic(
                self.store.active_file,
                canonical_json_bytes(
                    {
                        "run_id": self.run_id,
                        "pid": os.getpid(),
                        "command": self.command,
                        "step": self._step,
                        "started_utc": self._started,
                        "heartbeat_utc": _utc(),
                    }
                ),
            )

    def start(self) -> None:
        self.beat()

        def loop() -> None:
            while not self._stop.wait(self._interval):
                try:
                    self.beat()
                except OSError:
                    pass

        self._thread = threading.Thread(target=loop, name="lab-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._write_lock:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.store.active_file.unlink()
        except FileNotFoundError:
            pass


class RunLog:
    """``ROOT/lab/runs/<run_id>.jsonl``: append-only step events, no paths."""

    def __init__(self, store: LabStore, run_id: str) -> None:
        if not re.fullmatch(_RUN_ID, run_id):
            raise ValueError("run ids look like r-YYYYMMDD-HHMM-xxxx")
        self.store = store
        self.run_id = run_id
        self.path = store.runs_dir / f"{run_id}.jsonl"

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        line = {"event": event, "run_id": self.run_id, "utc": _utc(), **fields}
        self.store.runs_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, canonical_json_bytes(line) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return line

    def events(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for text in raw.splitlines():
            try:
                value = json.loads(text)
            except ValueError:
                continue  # a torn last line after a crash
            if isinstance(value, dict):
                events.append(value)
        return events


def summarize_run(events: list[dict[str, Any]], store: LabStore) -> dict[str, Any]:
    """One row of ``lab runs``: command, sample, status and step counts."""

    start = next((event for event in events if event.get("event") == "run_start"), {})
    sample = next((event.get("sample") for event in events if event.get("event") == "sample"), None)
    end = next((event for event in reversed(events) if event.get("event") == "run_end"), None)
    steps_end = [event for event in events if event.get("event") == "step_end"]
    counts: dict[str, int] = {}
    for event in steps_end:
        status = str(event.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    run_id = str(start.get("run_id") or (events[0].get("run_id") if events else ""))
    if end is not None:
        state = str(end.get("status", "ended"))
    else:
        active = store.read_active()
        state = "running" if active is not None and active.active and active.run_id == run_id else "interrupted"
    open_steps: dict[str, None] = {}
    for event in events:
        step = str(event.get("step", ""))
        if event.get("event") == "step_start":
            open_steps[step] = None
        elif event.get("event") == "step_end":
            open_steps.pop(step, None)
    return {
        "run_id": run_id,
        "command": start.get("command"),
        "sample": sample or start.get("sample"),
        "started_utc": start.get("utc"),
        "ended_utc": end.get("utc") if end else None,
        "status": state,
        "exit_code": end.get("exit_code") if end else None,
        "steps": counts,
        "open_step": list(open_steps)[-1] if open_steps else None,
    }


__all__ = [
    "ActiveRun",
    "HEARTBEAT_FRESH_S",
    "HEARTBEAT_INTERVAL_S",
    "LabStore",
    "RunLog",
    "STAGING_PREFIX",
    "WriterLock",
    "new_attempt_id",
    "new_run_id",
    "pid_alive",
    "summarize_run",
    "write_atomic",
]
