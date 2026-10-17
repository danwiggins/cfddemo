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
        attempts/<attempt_id>/{result.json, artifacts/, artifacts.json,
                               upstream_selections.json?}
        selected.json             {attempt_id, publication_order,
                                   last_success_attempt_id?}, switched by os.replace

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
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes

from . import STORE_VERSION
from .envelope import ResultEnvelope
from .file_access import open_regular_no_follow
from .identity import ALGORITHM_ID_PATTERN, SHA256_PATTERN
from .problems import LabProblem

HEARTBEAT_INTERVAL_S = 10.0
HEARTBEAT_FRESH_S = 30.0
KEEP_FAILED_ATTEMPTS = 3
STAGING_PREFIX = ".staging-"
ARTIFACT_INDEX_SCHEMA = "traceback.lab.artifact-index.v1"
UPSTREAM_SELECTIONS_SCHEMA = "traceback.lab.upstream-selections.v1"
MAX_UPSTREAM_SELECTIONS_BYTES = 16 * 1024
MAX_RESULT_SELECTION_BYTES = 4 * 1024

MAX_EXECUTION_PLAN_BYTES = 64 * 1024
_RUN_ID = r"^r-\d{8}-\d{4}-[0-9a-f]{4,8}$"
_ATTEMPT_ID = r"^a-\d{8}T\d{12}-[0-9a-f]{8}$"


def _validated_upstream_selections(value: Any) -> tuple[dict[str, str], ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        return None
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "algorithm_id", "input_id", "parameters_sha256", "attempt_id",
        }:
            return None
        algorithm_id = item["algorithm_id"]
        input_id = item["input_id"]
        parameters_sha256 = item["parameters_sha256"]
        attempt_id = item["attempt_id"]
        if (not isinstance(algorithm_id, str) or not ALGORITHM_ID_PATTERN.fullmatch(algorithm_id)
                or not isinstance(input_id, str) or not SHA256_PATTERN.fullmatch(input_id)
                or not isinstance(parameters_sha256, str) or not SHA256_PATTERN.fullmatch(parameters_sha256)
                or not isinstance(attempt_id, str) or not re.fullmatch(_ATTEMPT_ID, attempt_id)):
            return None
        rows.append(dict(item))
    if [row["algorithm_id"] for row in rows] != sorted({row["algorithm_id"] for row in rows}):
        return None
    return tuple(rows)


class ResultOrderUnavailable(OSError):
    """Attempt publication order cannot be read safely."""


class ResultPublishUncertain(LabProblem):
    """The selector is visible, but its durability was not confirmed."""

    def __init__(
        self,
        *,
        input_id: str,
        algorithm_id: str,
        parameters_sha256: str,
        attempt_id: str,
        generation_notified: bool,
    ) -> None:
        if generation_notified:
            notification = "the generation counter advanced"
            fix = "Inspect the selected result and run history before retrying."
        else:
            notification = "the generation notification could not be confirmed"
            fix = (
                "Manually reload or reopen the Lab view, then inspect the selected "
                "result and run history before retrying."
            )
        super().__init__(
            "TBX-LAB-020",
            summary="Result selection durability is unconfirmed",
            cause=f"The selected result is visible, but its directory sync failed; {notification}.",
            fix=fix,
            data={
                "input_id": input_id,
                "algorithm_id": algorithm_id,
                "parameters_sha256": parameters_sha256,
                "attempt_id": attempt_id,
                "generation_notified": generation_notified,
            },
        )


class ResultSelectionNotificationFailed(LabProblem):
    """A cached result was reselected durably, but readers may need a reload."""

    def __init__(
        self, *, input_id: str, algorithm_id: str, parameters_sha256: str, attempt_id: str,
    ) -> None:
        super().__init__(
            "TBX-LAB-020",
            summary="Cached result selection changed but notification failed",
            cause="The selected result is visible, but the Lab generation update could not be confirmed.",
            fix="Manually reload or reopen the Lab view and inspect the selected result.",
            data={
                "input_id": input_id,
                "algorithm_id": algorithm_id,
                "parameters_sha256": parameters_sha256,
                "attempt_id": attempt_id,
            },
        )


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
    def last_run_file(self) -> Path:
        return self.lab / "last_run.json"

    def last_run_id(self) -> str | None:
        try:
            value = json.loads(self.last_run_file.read_bytes()).get("run_id")
        except (FileNotFoundError, ValueError, AttributeError, OSError):
            return None
        return value if isinstance(value, str) and re.fullmatch(_RUN_ID, value) else None

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
            # One small pointer so readers find the newest run without a scan.
            write_atomic(self.last_run_file, canonical_json_bytes({"run_id": run_id, "command": command}))
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
    @staticmethod
    def _load_artifact_index(path: Path) -> dict[str, Any] | None:
        index_path = path / "artifacts.json"
        try:
            info = os.lstat(index_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ResultOrderUnavailable("Lab attempt metadata is unreadable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ResultOrderUnavailable("Lab attempt metadata is not a regular file")
        try:
            index = json.loads(index_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ResultOrderUnavailable("Lab attempt metadata is unreadable or invalid") from exc
        if (
            not isinstance(index, dict)
            or set(index) not in ({"schema", "files"}, {"schema", "files", "ordinal"})
            or index.get("schema") != ARTIFACT_INDEX_SCHEMA
            or not isinstance(index.get("files"), dict)
        ):
            raise ResultOrderUnavailable("Lab attempt metadata has invalid fields")
        ordinal = index.get("ordinal")
        if "ordinal" in index and (
            not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0
        ):
            raise ResultOrderUnavailable("Lab attempt publication ordinal is invalid")
        return index

    @classmethod
    def _attempt_order(cls, path: Path) -> int:
        index = cls._load_artifact_index(path)
        return 0 if index is None else int(index.get("ordinal", 0))

    @classmethod
    def _next_attempt_order(cls, attempts: Path) -> int:
        try:
            info = os.lstat(attempts)
        except FileNotFoundError:
            return 1
        except OSError as exc:
            raise ResultOrderUnavailable("Lab attempts are unreadable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ResultOrderUnavailable("Lab attempts path is not a directory")
        try:
            with os.scandir(attempts) as entries:
                orders = [
                    cls._attempt_order(Path(entry.path))
                    for entry in entries
                    if not entry.name.startswith(STAGING_PREFIX) and entry.is_dir(follow_symlinks=False)
                ]
            return 1 + max(orders, default=0)
        except ResultOrderUnavailable:
            raise
        except OSError as exc:
            raise ResultOrderUnavailable("Lab attempt order is unreadable") from exc

    @staticmethod
    def _publication_order(selection: dict[str, Any]) -> int | None:
        if "publication_order" not in selection:
            return None  # selected.json from an older store
        order = selection["publication_order"]
        if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
            raise ValueError("Lab result publication order is invalid")
        return order

    def _result_selection_orders(self, input_id: str, algorithm_id: str) -> dict[str, int | None]:
        """Read selection order across identities without using the clock."""

        algorithm_dir = self.results_dir / input_id / algorithm_id
        orders: dict[str, int | None] = {}
        try:
            with os.scandir(algorithm_dir) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    selector = Path(entry.path) / "selected.json"
                    try:
                        if selector.is_symlink():
                            raise ValueError("Lab result selection is a symlink")
                        selection = json.loads(selector.read_bytes())
                    except FileNotFoundError:
                        continue
                    if not isinstance(selection, dict):
                        raise ValueError("Lab result selection is invalid")
                    orders[entry.name] = self._publication_order(selection)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ResultOrderUnavailable("Lab result publication order is unreadable") from exc
        return orders

    def _next_result_order(self, input_id: str, algorithm_id: str) -> int:
        orders = self._result_selection_orders(input_id, algorithm_id)
        return max((order or 0 for order in orders.values()), default=0) + 1

    def _switch_result_selection(
        self, directory: Path, selection: dict[str, Any], *,
        input_id: str, algorithm_id: str, parameters_sha256: str,
    ) -> None:
        attempt_id = selection["attempt_id"]
        write_atomic(directory / "selected.json", canonical_json_bytes(selection))
        try:
            _fsync_dir(directory)
        except OSError as exc:
            # The replacement selector may already be visible to readers.
            # Notify the UI without allowing a counter failure to hide the
            # selector's uncertain durability or trigger pruning.
            generation_notified = False
            try:
                self.bump_generation()
            except Exception:  # noqa: BLE001 - preserve the original publication failure
                pass
            else:
                generation_notified = True
            raise ResultPublishUncertain(
                input_id=input_id,
                algorithm_id=algorithm_id,
                parameters_sha256=parameters_sha256,
                attempt_id=attempt_id,
                generation_notified=generation_notified,
            ) from exc

    def reselect_cached_result(
        self, input_id: str, algorithm_id: str, parameters_sha256: str,
    ) -> bool:
        """Make a validated cached success current, without rewriting its attempt."""

        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        if selected is None or selected[1].status != "ok":
            raise ResultOrderUnavailable("Cached result selection is invalid")
        orders = self._result_selection_orders(input_id, algorithm_id)
        for identity in orders:
            try:
                candidate = self._selected_result(input_id, algorithm_id, identity)
            except ValueError as exc:
                raise ResultOrderUnavailable("Another result selection is invalid") from exc
            if candidate is None:
                raise ResultOrderUnavailable("Another result selection is invalid")
        if parameters_sha256 not in orders:
            raise ResultOrderUnavailable("Cached result selection is missing")
        current_order = orders[parameters_sha256]
        highest = max((order or 0 for order in orders.values()), default=0)
        if len(orders) == 1 or (
            current_order is not None
            and current_order == highest
            and sum(order == highest for order in orders.values()) == 1
        ):
            return False

        directory = self.result_dir(input_id, algorithm_id, parameters_sha256)
        attempt_id = selected[0].name
        self._switch_result_selection(
            directory,
            {
                "attempt_id": attempt_id,
                "last_success_attempt_id": attempt_id,
                "publication_order": highest + 1,
            },
            input_id=input_id,
            algorithm_id=algorithm_id,
            parameters_sha256=parameters_sha256,
        )
        try:
            self.bump_generation()
        except OSError as exc:
            raise ResultSelectionNotificationFailed(
                input_id=input_id, algorithm_id=algorithm_id,
                parameters_sha256=parameters_sha256, attempt_id=attempt_id,
            ) from exc
        return True

    def publish_result(
        self,
        envelope: ResultEnvelope,
        *,
        input_id: str,
        artifacts: Mapping[str, bytes] | None = None,
        artifacts_dir: Path | None = None,
        attempt_id: str | None = None,
        upstream_selections: Sequence[Mapping[str, str]] | None = None,
        execution_plan: Mapping[str, Any] | None = None,
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
        ordinal = self._next_attempt_order(attempts)
        publication_order = self._next_result_order(input_id, envelope.algorithm_id)
        files = dict(artifacts or {})
        selected_upstream = _validated_upstream_selections(upstream_selections) if upstream_selections is not None else None
        if upstream_selections is not None and selected_upstream is None:
            raise ValueError("upstream selections must name distinct verified attempts")
        upstream_bytes = None
        if selected_upstream is not None:
            upstream_bytes = canonical_json_bytes({
                "schema": UPSTREAM_SELECTIONS_SCHEMA, "selections": selected_upstream,
            })
            if len(upstream_bytes) > MAX_UPSTREAM_SELECTIONS_BYTES:
                raise ValueError("upstream selections exceed the readable metadata limit")
        plan_bytes = canonical_json_bytes(execution_plan) if execution_plan is not None else None
        if plan_bytes is not None and len(plan_bytes) > MAX_EXECUTION_PLAN_BYTES:
            raise ValueError("execution plan exceeds the readable metadata limit")
        for relative in files:
            parts = Path(relative).parts
            if Path(relative).is_absolute() or ".." in parts or not parts:
                raise ValueError("artifact paths are relative and contained")

        def build(staging: Path) -> None:
            (staging / "result.json").write_bytes(envelope.to_bytes())
            if upstream_bytes is not None:
                (staging / "upstream_selections.json").write_bytes(upstream_bytes)
            if plan_bytes is not None:
                (staging / "execution_plan.json").write_bytes(plan_bytes)
            if artifacts_dir is not None:
                for path in artifacts_dir.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("artifacts may not contain symlinks")
                os.rename(artifacts_dir, staging / "artifacts")
            for relative, content in files.items():
                target = staging / "artifacts" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            artifact_root = staging / "artifacts"
            inventory: dict[str, int] = {}
            if artifact_root.exists():
                for path in artifact_root.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("artifacts may not contain symlinks")
                    if path.is_file():
                        inventory[path.relative_to(artifact_root).as_posix()] = path.stat().st_size
            for panel in envelope.panels:
                if (
                    not panel.data_ref.startswith("artifacts/")
                    or panel.data_ref.removeprefix("artifacts/") not in inventory
                ):
                    raise ValueError("panel data_ref was not published with its artifact")
            (staging / "artifacts.json").write_bytes(canonical_json_bytes({
                "schema": ARTIFACT_INDEX_SCHEMA, "files": inventory, "ordinal": ordinal,
            }))

        previous = self.selected_attempt(
            input_id, envelope.algorithm_id, envelope.provenance.parameters_sha256
        )
        last_success = attempt if envelope.status == "ok" else None
        if last_success is None and previous is not None and self._attempt_status(previous) == "ok":
            last_success = previous.name
        if last_success is None:
            try:
                selected = json.loads((directory / "selected.json").read_bytes())
                candidate = selected.get("last_success_attempt_id")
                if (
                    isinstance(candidate, str)
                    and re.fullmatch(_ATTEMPT_ID, candidate)
                    and self._attempt_status(attempts / candidate) == "ok"
                ):
                    last_success = candidate
            except (OSError, ValueError, AttributeError):
                pass
        self.publish_directory(attempts, attempt, build)
        selection = {"attempt_id": attempt, "publication_order": publication_order}
        if last_success is not None:
            selection["last_success_attempt_id"] = last_success
        self._switch_result_selection(
            directory, selection,
            input_id=input_id,
            algorithm_id=envelope.algorithm_id,
            parameters_sha256=envelope.provenance.parameters_sha256,
        )
        keep = {attempt, *([previous.name] if previous is not None else [])}
        if last_success is not None:
            keep.add(last_success)
        self._prune_attempts(attempts, keep=keep)
        self.bump_generation()
        return attempt

    @staticmethod
    def _attempt_status(path: Path) -> str | None:
        try:
            return str(json.loads((path / "result.json").read_bytes()).get("status"))
        except (OSError, ValueError, AttributeError):
            return None

    def _prune_attempts(self, attempts: Path, *, keep: set[str]) -> None:
        """Keep selected/replaced attempts, the last success, and the newest
        failed attempts for Trust/QC (D35). Unknown order never triggers deletion."""

        try:
            with os.scandir(attempts) as entries:
                published = [
                    Path(entry.path)
                    for entry in entries
                    if not entry.name.startswith(STAGING_PREFIX) and entry.is_dir(follow_symlinks=False)
                ]
            candidates = sorted(
                published, key=lambda path: (self._attempt_order(path), path.name),
            )
            by_status = [(path, self._attempt_status(path)) for path in candidates]
            failed = [path for path, status in by_status if status == "failed"]
            successful = [path for path, status in by_status if status == "ok"]
            retained = set(keep)
            retained.update(path.name for path in failed[-KEEP_FAILED_ATTEMPTS:])
            # Legacy attempts have no reliable ordinal; retain their successes.
            retained.update(path.name for path in successful if self._attempt_order(path) == 0)
            if successful and not any(path.name in retained for path in successful):
                retained.add(successful[-1].name)
        except OSError:
            return
        for stale in candidates:
            if stale.name not in retained:
                shutil.rmtree(stale, ignore_errors=True)

    def selected_artifacts(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> Path | None:
        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        if selected is None:
            return None
        artifacts = selected[0] / "artifacts"
        return artifacts if artifacts.is_dir() and not artifacts.is_symlink() else None

    def selected_result_artifacts(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> tuple[ResultEnvelope, Path] | None:
        """Return the result and artifacts from one verified selected attempt."""
        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        if selected is None:
            return None
        artifacts = selected[0] / "artifacts"
        if not artifacts.is_dir() or artifacts.is_symlink():
            return None
        return selected[1], artifacts

    def selected_attempt(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> Path | None:
        """Return only a complete selected attempt bound to this exact identity."""

        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        return selected[0] if selected is not None else None

    def selected_upstream_selections(
        self, input_id: str, algorithm_id: str, parameters_sha256: str,
    ) -> tuple[dict[str, str], ...] | None:
        """Read the consumed attempts of one selected result; None is unsafe/legacy."""

        attempt = self.selected_attempt(input_id, algorithm_id, parameters_sha256)
        if attempt is None:
            return None
        return self.upstream_selections_at(attempt)

    @staticmethod
    def upstream_selections_at(attempt: Path) -> tuple[dict[str, str], ...] | None:
        """Read consumed upstream attempts from a validated attempt snapshot."""

        path = attempt / "upstream_selections.json"
        try:
            descriptor = open_regular_no_follow(path)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_UPSTREAM_SELECTIONS_BYTES:
                    return None
                raw = stream.read(MAX_UPSTREAM_SELECTIONS_BYTES + 1)
                if len(raw) > MAX_UPSTREAM_SELECTIONS_BYTES:
                    return None
                data = json.loads(raw)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        if not isinstance(data, dict) or data.get("schema") != UPSTREAM_SELECTIONS_SCHEMA:
            return None
        return _validated_upstream_selections(data.get("selections"))

    @staticmethod
    def _artifact_matches(root: Path, relative: str, size: int) -> bool:
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            return False
        path = root
        for part in Path(relative).parts:
            path = path / part
            if path.is_symlink():
                return False
        try:
            return path.is_file() and path.stat().st_size == size
        except OSError:
            return False

    @classmethod
    def _legacy_artifacts_complete(cls, path: Path, algorithm_id: str) -> bool:
        """Validate old attempts without an inventory, including both upstream layouts."""

        root = path / "artifacts"
        if root.is_symlink():
            return False
        if algorithm_id == "read-summary":
            from .read_summary import COLUMNS, SCHEMA

            meta_file = root / "columns" / "meta.json"
            try:
                if meta_file.parent.is_symlink() or meta_file.is_symlink():
                    return False
                meta = json.loads(meta_file.read_bytes())
                rows = meta["rows"]
                if (
                    meta.get("schema") != SCHEMA
                    or meta.get("columns") != COLUMNS
                    or not isinstance(rows, int)
                    or isinstance(rows, bool)
                    or rows < 0
                ):
                    return False
            except (OSError, ValueError, KeyError, TypeError):
                return False
            return all(
                cls._artifact_matches(root, f"columns/{name}.bin", rows * int(dtype[2:]))
                for name, dtype in COLUMNS.items()
            )
        if algorithm_id == "meth-calls":
            from .meth_calls import CHUNK_CALLS, DTYPE, SCHEMA

            meta_file = root / "calls" / "meta.json"
            try:
                if meta_file.parent.is_symlink() or meta_file.is_symlink():
                    return False
                meta = json.loads(meta_file.read_bytes())
                chunks = meta["chunks"]
                invalid = meta["invalid_mm"]
                if (
                    meta.get("schema") != SCHEMA
                    or meta.get("dtype") != [list(field) for field in DTYPE.descr]
                    or not isinstance(chunks, list)
                    or not all(isinstance(n, int) and not isinstance(n, bool) and 0 < n <= CHUNK_CALLS for n in chunks)
                    or not isinstance(invalid, int)
                    or isinstance(invalid, bool)
                    or invalid < 0
                ):
                    return False
            except (OSError, ValueError, KeyError, TypeError):
                return False
            return (
                all(
                    cls._artifact_matches(root, f"calls/calls-{index:05d}.bin", n * DTYPE.itemsize)
                    for index, n in enumerate(chunks)
                )
                and cls._artifact_matches(root, "calls/invalid_rows.bin", invalid * 4)
            )
        return True

    def _selected_result(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> tuple[Path, ResultEnvelope, int | None] | None:
        directory = self.result_dir(input_id, algorithm_id, parameters_sha256)
        selector = directory / "selected.json"
        try:
            if selector.is_symlink():
                return None
            data = json.loads(selector.read_bytes())
            attempt_id = str(data["attempt_id"])
            publication_order = self._publication_order(data)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return None
        if not re.fullmatch(_ATTEMPT_ID, attempt_id):
            return None
        attempts = directory / "attempts"
        path = attempts / attempt_id
        result_file = path / "result.json"
        try:
            if attempts.is_symlink() or path.is_symlink() or result_file.is_symlink():
                return None
            envelope = ResultEnvelope.from_bytes(result_file.read_bytes())
        except (OSError, ValueError, TypeError):
            return None
        if (
            envelope.provenance.input_id != input_id
            or envelope.algorithm_id != algorithm_id
            or envelope.provenance.parameters_sha256 != parameters_sha256
        ):
            return None
        artifact_root = path / "artifacts"
        try:
            index = self._load_artifact_index(path)
        except ResultOrderUnavailable:
            return None
        if index is not None:
            if artifact_root.is_symlink():
                return None
            for relative, size in index["files"].items():
                if (
                    not isinstance(relative, str)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not self._artifact_matches(artifact_root, relative, size)
                ):
                    return None
        elif envelope.status == "ok" and not self._legacy_artifacts_complete(path, algorithm_id):
            return None
        for panel in envelope.panels:
            artifact = path
            for part in Path(panel.data_ref).parts:
                artifact = artifact / part
                if artifact.is_symlink():
                    return None
            if not artifact.is_file():
                return None
        return path, envelope, publication_order

    def selected_result_with_order(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> tuple[ResultEnvelope, int | None] | None:
        """Return a validated selection and its cross-identity publication order."""

        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        return (selected[1], selected[2]) if selected is not None else None

    def selected_result_with_attempt_and_order(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> tuple[str, ResultEnvelope, int | None] | None:
        """Read an immutable attempt ID alongside the selected result."""

        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        return (selected[0].name, selected[1], selected[2]) if selected is not None else None

    def selection_order_hint(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> int | None:
        """Read a selector's order without treating its attempt as a valid result."""

        try:
            selector = self.result_dir(input_id, algorithm_id, parameters_sha256) / "selected.json"
            descriptor = open_regular_no_follow(selector)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size > MAX_RESULT_SELECTION_BYTES):
                    return None
                raw = stream.read(MAX_RESULT_SELECTION_BYTES + 1)
                if len(raw) > MAX_RESULT_SELECTION_BYTES:
                    return None
                data = json.loads(raw)
            if (not isinstance(data, dict) or not isinstance(data.get("attempt_id"), str)
                    or not re.fullmatch(_ATTEMPT_ID, data["attempt_id"])):
                return None
            return self._publication_order(data)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def selected_result_snapshot(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> tuple[Path, ResultEnvelope, int | None] | None:
        """Return one validated attempt path, envelope and publication order."""

        return self._selected_result(input_id, algorithm_id, parameters_sha256)

    def read_result(
        self, input_id: str, algorithm_id: str, parameters_sha256: str
    ) -> ResultEnvelope | None:
        selected = self._selected_result(input_id, algorithm_id, parameters_sha256)
        return selected[1] if selected is not None else None

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
    "open_regular_no_follow",
    "pid_alive",
    "summarize_run",
    "write_atomic",
]
