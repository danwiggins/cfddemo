"""Per-algorithm memory cap for Lab runs: one algorithm must not OOM the workstation.

Each algorithm already runs in its own worker process group with a timeout
(``runner.run_plan`` via ``pipeline.run_pipeline``).  This module decides how
much memory that worker may use and how the limit is applied:

* **Cap derivation.**  The default cap for an algorithm is
  ``min(max(floor, 2 x highest peak RSS of its ok steps), ceiling)``.  The
  floor is the registry's ``MEMORY_FLOOR_BYTES`` entry, else 2 GiB; the
  ceiling is ``min(0.8 x physical RAM, 64 GiB)``.  History counts only steps
  that ended ``ok`` (never failed, timed-out, memory-killed or uncapped
  failures), so a runaway cannot raise its own cap by being rerun.  The
  history lives in ``ROOT/lab/memory-history.json`` (``{algorithm: max ok
  peak}``), maintained at the end of each run under the writer lock and
  rebuilt from the run logs once when absent.  ``--memory-cap BYTES``
  replaces the derived cap for every algorithm; ``--no-memory-cap`` disables.
* **Enforcement.**  On every platform the runner's resident-set sampler
  kills the worker's process group as soon as the sampled tree RSS exceeds
  the cap.  On Linux the worker additionally sets ``RLIMIT_AS``
  (``RLIMIT_DATA`` where ``RLIMIT_AS`` is unavailable) in the child before
  ``exec``.  That limit counts virtual address space, which numpy's OpenBLAS
  inflates by roughly 64 MiB per BLAS thread at import, so the worker pins
  the BLAS thread count (``min(4, cpu_count)`` unless the algorithm declares
  ``blas_threads``) and the rlimit is the cap plus 512 MiB plus 64 MiB per
  thread of import headroom; the sampler enforces the cap itself.  macOS
  refuses any address-space limit below the process's already reserved
  address space (hundreds of GiB on arm64; ``setrlimit`` returns ``EINVAL``)
  and does not enforce the accepted values, so no rlimit is set there.
  The aligner (samtools/minimap2 ingest) is not an algorithm and never gets
  a cap.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from traceback_runner.serialization import canonical_json_bytes

from .file_access import open_regular_no_follow

if TYPE_CHECKING:  # the worker imports this module; the store pulls in the signed CLI
    from .runner import StepResult
    from .store import LabStore

GIB = 1 << 30
MIB = 1 << 20
DEFAULT_MEMORY_CAP_BYTES = 2 * GIB
HISTORY_MULTIPLIER = 2
MIN_MEMORY_CAP_BYTES = 256 * MIB
MAX_MEMORY_CAP_BYTES = 1 << 50  # 1 PiB: anything larger is a typo
ABSOLUTE_CEILING_BYTES = 64 * GIB
RAM_CEILING_FRACTION = 0.8

# Linux RLIMIT_AS headroom over the cap: interpreter + shared libraries, plus
# what OpenBLAS reserves per BLAS thread when numpy loads.
IMPORT_HEADROOM_BYTES = 512 * MIB
PER_THREAD_HEADROOM_BYTES = 64 * MIB
DEFAULT_BLAS_THREADS_MAX = 4
BLAS_THREAD_VARIABLES = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

HISTORY_FILE = "memory-history.json"  # under ROOT/lab
HISTORY_SCHEMA = "traceback.lab.memory-history.v1"
MAX_HISTORY_BYTES = 256 * 1024

# Exit code the worker uses when an allocation is refused under the cap.
WORKER_MEMORY_EXIT = 77

_SIZE = re.compile(r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]*)")
_UNITS = {"": 1, "B": 1, "KIB": 1 << 10, "MIB": MIB, "GIB": GIB, "TIB": 1 << 40}
# A refused allocation under the rlimit, as Python, numpy/OpenBLAS, glibc and
# C++ tools report it.
_ALLOCATION_FAILURE = re.compile(
    r"MemoryError|Cannot allocate memory|bad_alloc|[Oo]ut of memory|ENOMEM|Unable to allocate"
    r"|Memory allocation (?:still )?failed"
)


class MemoryCapError(RuntimeError):
    """The worker could not be started under its cap (``preexec_fn`` failed)."""


def pipeline_options(cap_bytes: int | None, rlimit_bytes: int | None = None) -> dict[str, Any]:
    """``run_contained_pipeline`` keyword arguments that apply a cap of ``cap_bytes``.

    ``on_tree_rss_sample`` asks the pipeline to stop the group when a sample
    exceeds the cap (the pipeline reaps before it kills, so a group that
    finished meanwhile keeps its own exit codes; ``PipelineResult.aborted``
    is the kill).  Where the kernel enforces an address-space limit,
    ``preexec_fn`` sets it to ``rlimit_bytes`` (default: the cap) in each
    child before exec.  No cap: no options.
    """

    if cap_bytes is None:
        if rlimit_bytes is not None:
            raise ValueError("memory rlimit needs a cap")
        return {}
    if cap_bytes <= 0:
        raise ValueError("memory cap must be positive")
    if rlimit_bytes is None:
        rlimit_bytes = cap_bytes
    elif rlimit_bytes < cap_bytes:
        raise ValueError("memory rlimit is never below the cap")
    options: dict[str, Any] = {"on_tree_rss_sample": lambda rss: rss > cap_bytes}
    limit_name = select_memory_limit()
    if limit_name is not None:
        limit = rlimit_bytes

        def limit_child() -> None:
            # Between fork and exec: only the rlimit call, nothing that takes a lock.
            apply_memory_limit(limit, limit_name)

        options["preexec_fn"] = limit_child
    return options


def start_failure(exc: BaseException, options: Mapping[str, Any]) -> MemoryCapError | None:
    """A pipeline start failure under ``pipeline_options`` with a ``preexec_fn``."""

    if "preexec_fn" not in options or not isinstance(exc, (OSError, subprocess.SubprocessError)):
        return None
    return MemoryCapError(f"worker could not start under the memory cap: {type(exc).__name__}: {exc}")


def parse_memory_cap(text: str) -> int:
    """``--memory-cap`` value: bytes, or a number with a KiB/MiB/GiB/TiB suffix."""

    match = _SIZE.fullmatch(text.strip())
    if match is None:
        raise ValueError("--memory-cap is a size such as 4GiB, 512MiB or a byte count")
    unit = match.group("unit").upper()
    if unit not in _UNITS:
        raise ValueError("--memory-cap accepts the suffixes KiB, MiB, GiB and TiB")
    value = int(float(match.group("number")) * _UNITS[unit])
    if value < MIN_MEMORY_CAP_BYTES:
        raise ValueError("--memory-cap is at least 256MiB")
    if value > MAX_MEMORY_CAP_BYTES:
        raise ValueError("--memory-cap is at most 1024TiB")
    return value


def format_bytes(value: int) -> str:
    """``2.0 GiB`` / ``512.0 MiB`` for operator lines; exact bytes stay in JSON."""

    if value >= GIB:
        return f"{value / GIB:.1f} GiB"
    return f"{value / MIB:.1f} MiB"


def physical_ram_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def derived_ceiling(ram_bytes: int | None) -> int:
    """The largest derived cap: 0.8 x physical RAM, never above 64 GiB."""

    if ram_bytes is None or ram_bytes <= 0:
        return ABSOLUTE_CEILING_BYTES
    return max(MIN_MEMORY_CAP_BYTES, min(int(ram_bytes * RAM_CEILING_FRACTION), ABSOLUTE_CEILING_BYTES))


# -- BLAS threads and the Linux address-space headroom ----------------------------------------
def blas_threads(algorithm: Any = None, cpu_count: int | None = None) -> int:
    """BLAS threads for one worker: the algorithm's ``blas_threads`` else ``min(4, cpus)``."""

    declared = getattr(algorithm, "blas_threads", None)
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, min(DEFAULT_BLAS_THREADS_MAX, cpus))


def pin_blas_threads(env: dict[str, str], threads: int) -> int:
    """Set the BLAS thread variables that are not already set; returns the effective count.

    An operator-set variable is kept (and bounds the headroom instead).
    """

    effective = threads
    for name in BLAS_THREAD_VARIABLES:
        present = env.get(name)
        if present is None or not present.isdecimal() or int(present) < 1:
            env[name] = str(threads)
        else:
            effective = max(effective, int(present))
    return effective


def address_space_limit(cap_bytes: int, threads: int) -> int:
    """The Linux rlimit for a worker: the cap plus import headroom (the sampler enforces the cap)."""

    return cap_bytes + IMPORT_HEADROOM_BYTES + PER_THREAD_HEADROOM_BYTES * max(1, threads)


def select_memory_limit(sysname: str | None = None, *, resource_module: Any | None = None) -> str | None:
    """Which rlimit the worker sets before exec: ``RLIMIT_AS``, ``RLIMIT_DATA`` or none.

    Linux honours ``RLIMIT_AS``; ``RLIMIT_DATA`` is the fallback where the
    platform's ``resource`` module lacks it.  Darwin returns ``None``: its
    kernel rejects address-space limits below the reserved address space
    (EINVAL) and does not enforce those it accepts, so only the runner's RSS
    sampler can cap a worker there.
    """

    name = sysname if sysname is not None else os.uname().sysname
    if name == "Darwin":
        return None
    if resource_module is None:
        import resource as resource_module  # noqa: PLC0415 - POSIX only
    if hasattr(resource_module, "RLIMIT_AS"):
        return "RLIMIT_AS"
    if hasattr(resource_module, "RLIMIT_DATA"):
        return "RLIMIT_DATA"
    return None


def apply_memory_limit(cap_bytes: int, limit_name: str | None) -> str | None:
    """Set rlimit ``limit_name`` (from ``select_memory_limit``) to ``cap_bytes`` here.

    Called in the worker child between fork and exec; ``None`` sets nothing.
    A refused limit (``ValueError``/``OSError``) is ignored: the RSS sampler
    still applies.  Returns the name set.
    """

    name = limit_name
    if name is None:
        return None
    import resource  # noqa: PLC0415 - POSIX only

    limit = getattr(resource, name)
    try:
        soft, hard = resource.getrlimit(limit)
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < cap_bytes else cap_bytes
        resource.setrlimit(limit, (min(cap_bytes, new_hard), new_hard))
    except (ValueError, OSError):
        return None
    return name


def looks_like_allocation_failure(exit_code: int | None, stderr_tail: Iterable[str]) -> bool:
    """A worker that died of a refused allocation under the rlimit."""

    if exit_code == WORKER_MEMORY_EXIT:
        return True
    return any(_ALLOCATION_FAILURE.search(line) for line in stderr_tail)


def failure_reason(
    *, capped: bool, memory_exceeded: bool, timed_out: bool, exit_code: int | None, stderr_tail: Iterable[str],
) -> str | None:
    """``"memory"`` only for a capped step the cap stopped: our sampler kill, the
    worker's memory exit, or a non-zero exit whose stderr names a refused
    allocation.  A timeout or an exit 0 is never a memory failure."""

    if not capped or timed_out or exit_code == 0:
        return None
    if memory_exceeded or exit_code == WORKER_MEMORY_EXIT:
        return "memory"
    if exit_code is not None and looks_like_allocation_failure(None, stderr_tail):
        return "memory"
    return None


# -- history ---------------------------------------------------------------------------------
def _algorithm_of(step: Any) -> str | None:
    if not isinstance(step, str) or not step:
        return None
    return step.split("@", 1)[0]


def _peak_of(event: Mapping[str, Any]) -> int:
    peak = 0
    for key in ("peak_rss_bytes", "peak_tree_rss_bytes"):
        value = event.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > peak:
            peak = value
    return peak


def history_path(store: LabStore) -> Path:
    return store.lab / HISTORY_FILE


def rebuild_history(store: LabStore) -> tuple[dict[str, int], int]:
    """Highest ok peak RSS per algorithm across this ROOT's run logs, plus unreadable logs skipped.

    Derived-input steps (``alg@0.5``) count for their algorithm.  Only
    ``status == "ok"`` steps count: a failed, timed-out, memory-killed or
    uncapped-and-failed step's peak must not raise the next cap.
    """

    history: dict[str, int] = {}
    skipped = 0
    for run_id in store.run_ids():
        try:
            events = store.run_log(run_id).events()
        except OSError:
            skipped += 1
            continue
        for event in events:
            if event.get("event") != "step_end" or event.get("status") != "ok":
                continue
            algorithm_id = _algorithm_of(event.get("step"))
            if algorithm_id is None:
                continue
            peak = _peak_of(event)
            if peak > history.get(algorithm_id, 0):
                history[algorithm_id] = peak
    return history, skipped


def _read_history_file(path: Path) -> dict[str, int] | None:
    try:
        descriptor = open_regular_no_follow(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HISTORY_BYTES:
            return None
        raw = os.read(descriptor, MAX_HISTORY_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("schema") != HISTORY_SCHEMA:
        return None
    peaks = data.get("max_ok_peak_bytes")
    if not isinstance(peaks, dict):
        return None
    history: dict[str, int] = {}
    for key, value in peaks.items():
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            history[key] = value
    return history


def save_history(store: LabStore, history: Mapping[str, int]) -> None:
    from .store import write_atomic  # noqa: PLC0415 - keeps the worker's imports light

    payload = {"schema": HISTORY_SCHEMA, "max_ok_peak_bytes": dict(sorted(history.items()))}
    write_atomic(history_path(store), canonical_json_bytes(payload) + b"\n")


def load_history(store: LabStore) -> tuple[dict[str, int], list[str]]:
    """The stored history, or a one-time rebuild from the run logs (never fatal)."""

    stored = _read_history_file(history_path(store))
    if stored is not None:
        return stored, []
    history, skipped = rebuild_history(store)
    notices = [f"memory history rebuilt from {len(store.run_ids())} run log(s)"]
    if skipped:
        notices.append(f"memory history: {skipped} unreadable run log(s) skipped")
    try:
        save_history(store, history)
    except OSError as exc:
        notices.append(f"memory history not saved ({type(exc).__name__}); rebuilt next run")
    return history, notices


def update_history(store: LabStore, results: Iterable[StepResult]) -> dict[str, int]:
    """Fold this run's ok steps into ``memory-history.json`` (writer lock held)."""

    history, _notices = load_history(store)
    changed = False
    for result in results:
        if result.status != "ok" or result.cached:
            continue
        peak = max(result.peak_rss_bytes or 0, result.peak_tree_rss_bytes or 0)
        if peak > history.get(result.algorithm_id, 0):
            history[result.algorithm_id] = peak
            changed = True
    if changed:
        save_history(store, history)
    return history


# -- policy ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class MemoryCap:
    """One algorithm's cap: ``bytes`` is ``None`` when disabled."""

    bytes: int | None
    basis: str  # history | floor | default | ceiling | override | disabled
    observed_bytes: int | None = None  # the ok-history peak behind a history/ceiling basis

    def log_fields(self) -> dict[str, Any]:
        return {
            "memory_cap_bytes": self.bytes,
            "memory_cap_basis": self.basis,
            "memory_cap_observed_bytes": self.observed_bytes,
        }


@dataclass(frozen=True)
class MemoryCapPolicy:
    """How ``lab run`` caps each algorithm; built once per run from the ROOT."""

    override_bytes: int | None = None
    disabled: bool = False
    history: Mapping[str, int] = field(default_factory=dict)
    floors: Mapping[str, int] = field(default_factory=dict)
    ceiling_bytes: int = ABSOLUTE_CEILING_BYTES
    notices: tuple[str, ...] = ()

    @classmethod
    def for_store(
        cls, store: LabStore, *, override_bytes: int | None = None, disabled: bool = False,
        floors: Mapping[str, int] | None = None, ram_bytes: int | None = None,
    ) -> MemoryCapPolicy:
        if floors is None:
            from . import registry  # noqa: PLC0415 - registry does not import this module

            floors = registry.MEMORY_FLOOR_BYTES
        history: dict[str, int] = {}
        notices: list[str] = []
        if not disabled and override_bytes is None:
            history, notices = load_history(store)
        ceiling = derived_ceiling(physical_ram_bytes() if ram_bytes is None else ram_bytes)
        return cls(
            override_bytes=override_bytes, disabled=disabled, history=history, floors=floors,
            ceiling_bytes=ceiling, notices=tuple(notices),
        )

    def for_algorithm(self, algorithm_id: str) -> MemoryCap:
        if self.disabled:
            return MemoryCap(None, "disabled")
        if self.override_bytes is not None:
            return MemoryCap(self.override_bytes, "override")
        floor = self.floors.get(algorithm_id)
        basis = "default" if floor is None else "floor"
        cap = DEFAULT_MEMORY_CAP_BYTES if floor is None else max(floor, MIN_MEMORY_CAP_BYTES)
        observed = self.history.get(algorithm_id)
        if observed is not None and observed * HISTORY_MULTIPLIER > cap:
            cap, basis = observed * HISTORY_MULTIPLIER, "history"
        if cap > self.ceiling_bytes:
            return MemoryCap(self.ceiling_bytes, "ceiling", observed)
        return MemoryCap(cap, basis, observed if basis == "history" else None)

    def flags(self) -> dict[str, Any]:
        """The run log / ``--json`` record of the operator's choice."""

        return {"memory_cap_bytes": self.override_bytes, "no_memory_cap": self.disabled}


__all__ = [
    "ABSOLUTE_CEILING_BYTES",
    "BLAS_THREAD_VARIABLES",
    "DEFAULT_MEMORY_CAP_BYTES",
    "HISTORY_FILE",
    "HISTORY_MULTIPLIER",
    "MIN_MEMORY_CAP_BYTES",
    "MemoryCap",
    "MemoryCapError",
    "MemoryCapPolicy",
    "WORKER_MEMORY_EXIT",
    "address_space_limit",
    "apply_memory_limit",
    "blas_threads",
    "derived_ceiling",
    "failure_reason",
    "format_bytes",
    "history_path",
    "load_history",
    "looks_like_allocation_failure",
    "parse_memory_cap",
    "pin_blas_threads",
    "pipeline_options",
    "rebuild_history",
    "save_history",
    "select_memory_limit",
    "start_failure",
    "update_history",
]
