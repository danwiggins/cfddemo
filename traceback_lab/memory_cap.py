"""Per-algorithm memory cap for Lab runs: one algorithm cannot OOM the workstation.

Each algorithm already runs in its own worker process group with a timeout
(``runner.run_plan`` via ``pipeline.run_pipeline``).  This module decides how
much memory that worker may use and how the limit is applied:

* **Cap derivation.**  The default cap for an algorithm is
  ``max(floor, 2 x highest peak RSS recorded for it in this ROOT's run logs)``,
  where the floor is the registry's ``MEMORY_FLOOR_BYTES`` entry for the
  algorithm, else 2 GiB.  History excludes steps that the cap itself killed,
  so a runaway algorithm cannot double its own cap on every rerun.
  ``--memory-cap BYTES`` replaces the derived cap for every algorithm;
  ``--no-memory-cap`` disables it.
* **Enforcement.**  On Linux the worker sets ``RLIMIT_AS`` (``RLIMIT_DATA``
  where ``RLIMIT_AS`` is unavailable) in the child before ``exec``, so every
  allocation past the cap fails and every descendant inherits the limit.
  On macOS the kernel refuses any address-space limit below the process's
  already reserved address space (hundreds of GiB on arm64; ``setrlimit``
  returns ``EINVAL``) and does not enforce the accepted values, so no rlimit
  is set there.  On both platforms the runner's resident-set sampler kills
  the worker's process group as soon as the sampled tree RSS exceeds the
  cap.  The aligner (samtools/minimap2 ingest) is not an algorithm and never
  gets a cap.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the worker imports this module; the store pulls in the signed CLI
    from .store import LabStore

GIB = 1 << 30
MIB = 1 << 20
DEFAULT_MEMORY_CAP_BYTES = 2 * GIB
HISTORY_MULTIPLIER = 2
MIN_MEMORY_CAP_BYTES = MIB
MAX_MEMORY_CAP_BYTES = 1 << 50  # 1 PiB: anything larger is a typo

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
        raise ValueError("--memory-cap is at least 1MiB")
    if value > MAX_MEMORY_CAP_BYTES:
        raise ValueError("--memory-cap is at most 1024TiB")
    return value


def format_bytes(value: int) -> str:
    """``2.0 GiB`` / ``512.0 MiB`` for operator lines; exact bytes stay in JSON."""

    if value >= GIB:
        return f"{value / GIB:.1f} GiB"
    return f"{value / MIB:.1f} MiB"


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


def looks_like_allocation_failure(exit_code: int | None, stderr_tail: list[str]) -> bool:
    """A worker that died of a refused allocation under the rlimit."""

    if exit_code == WORKER_MEMORY_EXIT:
        return True
    return any(_ALLOCATION_FAILURE.search(line) for line in stderr_tail)


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


def memory_history(store: LabStore) -> dict[str, int]:
    """Highest recorded peak RSS per algorithm across this ROOT's run logs.

    Derived-input steps (``alg@0.5``) count for their algorithm.  Steps the
    cap killed (``failure_reason == "memory"``) are excluded: their peak is
    the cap, and doubling it would let the cap escalate on every rerun.
    """

    history: dict[str, int] = {}
    for run_id in store.run_ids():
        for event in store.run_log(run_id).events():
            if event.get("event") != "step_end" or event.get("failure_reason") == "memory":
                continue
            algorithm_id = _algorithm_of(event.get("step"))
            if algorithm_id is None:
                continue
            peak = _peak_of(event)
            if peak > history.get(algorithm_id, 0):
                history[algorithm_id] = peak
    return history


@dataclass(frozen=True)
class MemoryCap:
    """One algorithm's cap: ``bytes`` is ``None`` when disabled."""

    bytes: int | None
    basis: str  # history | floor | default | override | disabled
    observed_bytes: int | None = None  # the history peak a "history" basis doubled

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

    @classmethod
    def for_store(
        cls, store: LabStore, *, override_bytes: int | None = None, disabled: bool = False,
        floors: Mapping[str, int] | None = None,
    ) -> MemoryCapPolicy:
        if floors is None:
            from . import registry  # noqa: PLC0415 - registry does not import this module

            floors = registry.MEMORY_FLOOR_BYTES
        history = {} if disabled or override_bytes is not None else memory_history(store)
        return cls(override_bytes=override_bytes, disabled=disabled, history=history, floors=floors)

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
            return MemoryCap(observed * HISTORY_MULTIPLIER, "history", observed)
        return MemoryCap(cap, basis)

    def flags(self) -> dict[str, Any]:
        """The run log / ``--json`` record of the operator's choice."""

        return {"memory_cap_bytes": self.override_bytes, "no_memory_cap": self.disabled}


__all__ = [
    "DEFAULT_MEMORY_CAP_BYTES",
    "HISTORY_MULTIPLIER",
    "MemoryCap",
    "MemoryCapPolicy",
    "WORKER_MEMORY_EXIT",
    "apply_memory_limit",
    "format_bytes",
    "looks_like_allocation_failure",
    "memory_history",
    "parse_memory_cap",
    "select_memory_limit",
]
