"""The algorithm plug-in contract and the registry entry list.

An algorithm declares **measurement identity only** (D3): id, version, family,
requirements, what it consumes, parameters and tool pins.  Tier, footnote,
license text, reference and next steps live in the interpretation library and
are joined at read time, so editing them never re-keys a result.

Adding an algorithm means one module in ``traceback_lab/algorithms/``, one
line in ``ALGORITHMS``, library entries, and tests.  No other shared file
changes.
"""

from __future__ import annotations

import difflib
import importlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .identity import ALGORITHM_ID_PATTERN, parameters_sha256

Family = Literal["qc", "fragmentomics", "copy-number", "methylation", "non-human", "composite"]
Consumes = Literal["read_summary", "meth_calls", "counting_bam", "full_bam"]
Cost = Literal["fast", "slow"]

FAMILIES: tuple[str, ...] = (
    "qc",
    "fragmentomics",
    "copy-number",
    "methylation",
    "non-human",
    "composite",
)
CONSUMES: tuple[str, ...] = ("read_summary", "meth_calls", "counting_bam", "full_bam")
COSTS: tuple[str, ...] = ("fast", "slow")

# Registry order is execution order (D20).  Each entry is "module:attribute"
# under ``traceback_lab.algorithms`` (a dotted module name is used as given);
# the attribute is an Algorithm instance.
ALGORITHMS: tuple[str, ...] = (
    "read_summary:READ_SUMMARY",
    "bam_qc:BAM_QC",
    "depth:DEPTH",
    "fragment_size:FRAGMENT_SIZE",
    "short_read_fragment_size:SHORT_READ_FRAGMENT_SIZE",
    "copy_number:COPY_NUMBER",
    "instability:INSTABILITY",
    "long_tail:LONG_TAIL",
    "sex_check:SEX_CHECK",
    "identity:IDENTITY",
    "mtdna:MTDNA",
    "cell_origin:CELL_ORIGIN",
    "end_motifs:END_MOTIFS",
    "breakpoint_motifs:BREAKPOINT_MOTIFS",
    "delfi_ratio:DELFI_RATIO",
    "meth_calls:METH_CALLS",
    "modbase_qc:MODBASE_QC",
    "global_5mc:GLOBAL_5MC",
    "global_5hmc:GLOBAL_5HMC",
    "pmd_score:PMD_SCORE",
    "hypometh_reads:HYPOMETH_READS",
    "frag_x_meth:FRAG_X_METH",
)


@dataclass(frozen=True)
class Requirements:
    mm_tags: bool = False
    min_eligible_reads: int = 0
    platforms: frozenset[str] = frozenset({"ont"})
    reference: str = "hg38"


@dataclass(frozen=True)
class ToolPin:
    name: str
    version: str
    license_id: str | None = None

    def identity(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class Ready:
    pass


@dataclass(frozen=True)
class Skip:
    reason_code: str
    message: str


@dataclass(frozen=True)
class InputContext:
    """Everything an algorithm may read about the input it runs on.

    Later slice issues extend this with the read summary, the row mask of a
    derived input and the methylation-call store; algorithms receive what
    their ``consumes`` declares (D33).
    """

    root: Path
    source_id: str
    input_id: str
    reference_id: str
    reference_sha256: str
    input_dir: Path
    eligible_reads: int | None = None
    approx_depth_x: float | None = None
    derived: Mapping[str, Any] | None = None
    example: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)


def context_to_json(ctx: InputContext) -> dict[str, Any]:
    return {
        "root": str(ctx.root),
        "source_id": ctx.source_id,
        "input_id": ctx.input_id,
        "reference_id": ctx.reference_id,
        "reference_sha256": ctx.reference_sha256,
        "input_dir": str(ctx.input_dir),
        "eligible_reads": ctx.eligible_reads,
        "approx_depth_x": ctx.approx_depth_x,
        "derived": dict(ctx.derived) if ctx.derived is not None else None,
        "example": ctx.example,
        "extras": dict(ctx.extras),
    }


def context_from_json(data: Mapping[str, Any]) -> InputContext:
    return InputContext(
        root=Path(data["root"]),
        source_id=data["source_id"],
        input_id=data["input_id"],
        reference_id=data["reference_id"],
        reference_sha256=data["reference_sha256"],
        input_dir=Path(data["input_dir"]),
        eligible_reads=data.get("eligible_reads"),
        approx_depth_x=data.get("approx_depth_x"),
        derived=data.get("derived"),
        example=bool(data.get("example", False)),
        extras=data.get("extras") or {},
    )


@dataclass
class AlgorithmOutput:
    """What ``run`` returns: metrics and panels for the envelope, plus artifact bytes."""

    metrics: list[dict[str, Any]] = field(default_factory=list)
    panels: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, bytes] = field(default_factory=dict)


@runtime_checkable
class Algorithm(Protocol):
    id: str
    version: str
    family: Family
    requires: Requirements
    consumes: Consumes
    cost: Cost
    parameters: Mapping[str, Any]
    tools: Sequence[ToolPin]
    upstream: Sequence[str]

    def preflight(self, ctx: InputContext) -> Ready | Skip: ...

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput: ...


def execution_identity(
    algorithm: Algorithm,
    *,
    reference_sha256: str,
    asset_sha256s: Iterable[str] = (),
    upstream: Iterable[Mapping[str, str]] = (),
    tool_pins: Iterable[Mapping[str, str]] | None = None,
) -> str:
    """``parameters_sha256`` for one algorithm on one reference (D19)."""

    return parameters_sha256(
        algorithm_id=algorithm.id,
        algorithm_version=algorithm.version,
        parameters=algorithm.parameters,
        tools=list(tool_pins) if tool_pins is not None else [tool.identity() for tool in algorithm.tools],
        reference_sha256=reference_sha256,
        asset_sha256s=asset_sha256s,
        upstream=upstream,
    )


def validate_algorithm(algorithm: Any) -> Algorithm:
    """Refuse a malformed plug-in at load time, not mid-run."""

    if not isinstance(algorithm, Algorithm):
        raise TypeError(f"{algorithm!r} does not implement the Algorithm protocol")
    if not ALGORITHM_ID_PATTERN.fullmatch(algorithm.id):
        raise ValueError(f"algorithm id {algorithm.id!r} must match ^[a-z0-9-]{{1,40}}$")
    if not isinstance(algorithm.version, str) or not algorithm.version:
        raise ValueError(f"{algorithm.id}: version must be a non-empty string")
    if algorithm.family not in FAMILIES:
        raise ValueError(f"{algorithm.id}: unknown family {algorithm.family!r}")
    if algorithm.consumes not in CONSUMES:
        raise ValueError(f"{algorithm.id}: unknown consumes {algorithm.consumes!r}")
    if algorithm.cost not in COSTS:
        raise ValueError(f"{algorithm.id}: cost must be fast or slow")
    if algorithm.requires.min_eligible_reads < 0:
        raise ValueError(f"{algorithm.id}: min_eligible_reads must be >= 0")
    return algorithm


def load_entry(entry: str) -> Algorithm:
    """Import one ``module:attribute`` entry and validate it."""

    module_name, _, attribute = entry.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"registry entry {entry!r} must be 'module:attribute'")
    qualified = module_name if "." in module_name else f"traceback_lab.algorithms.{module_name}"
    module = importlib.import_module(qualified)
    return validate_algorithm(getattr(module, attribute))


def registry_entries(entries: Sequence[str] | None = None) -> tuple[tuple[str, Algorithm], ...]:
    """``(entry, algorithm)`` pairs in registry order (``ALGORITHMS`` by default)."""

    chosen = ALGORITHMS if entries is None else entries
    algorithms = load_registry(chosen)
    return tuple(zip(chosen, algorithms))


def load_registry(entries: Sequence[str] | None = None) -> tuple[Algorithm, ...]:
    """Import each entry and validate ids, uniqueness and upstream order."""

    entries = ALGORITHMS if entries is None else entries
    loaded: list[Algorithm] = []
    seen: set[str] = set()
    for entry in entries:
        algorithm = load_entry(entry)
        if algorithm.id in seen:
            raise ValueError(f"duplicate algorithm id {algorithm.id!r}")
        missing = [name for name in algorithm.upstream if name not in seen]
        if missing:
            raise ValueError(
                f"{algorithm.id}: upstream {missing} must appear earlier in the registry"
            )
        seen.add(algorithm.id)
        loaded.append(algorithm)
    return tuple(loaded)


@dataclass(frozen=True)
class PlanItem:
    algorithm: Algorithm
    upstream_only: bool  # added because a selected algorithm needs it


def intermediate_ids(registry: Sequence[Algorithm]) -> frozenset[str]:
    """Only column producers whose full-input rows can be masked for derived inputs.

    An ordinary upstream dependency (for example copy-number -> instability)
    must instead run on the derived input, so its result matches that input.
    """

    reusable = {"read-summary", "meth-calls"}
    return frozenset(algorithm.id for algorithm in registry if algorithm.id in reusable)


def plan(
    registry: Sequence[Algorithm],
    *,
    only: Sequence[str] | None = None,
    profile: str = "all",
) -> tuple[PlanItem, ...]:
    """Which algorithms run, in registry order (D20).

    ``only`` overrides ``profile``.  Upstream intermediates a selected
    algorithm needs are added and marked ``upstream_only``.  An unknown id is
    TBX-LAB-001 with a did-you-mean suggestion.
    """

    by_id = {algorithm.id: algorithm for algorithm in registry}
    if only:
        unknown = [name for name in only if name not in by_id]
        if unknown:
            name = unknown[0]
            close = difflib.get_close_matches(name, list(by_id), n=1, cutoff=0.6)
            hint = f"Did you mean {close[0]}? " if close else ""
            from .problems import LabProblem  # lazy: keeps worker start-up off the signed CLI import

            raise LabProblem(
                "TBX-LAB-001",
                cause=f"unknown algorithm id {name!r}",
                fix=f"{hint}`traceback lab algorithms` lists ids",
                data={"unknown": unknown, "suggestion": close[0] if close else None},
            )
        selected = set(only)
    else:
        if profile not in {"all", "fast"}:
            raise ValueError("profile must be all or fast")
        selected = {
            algorithm.id
            for algorithm in registry
            if profile == "all" or algorithm.cost == "fast"
        }
    needed = set(selected)
    frontier = list(selected)
    while frontier:
        for name in by_id[frontier.pop()].upstream:
            if name not in needed:
                needed.add(name)
                frontier.append(name)
    return tuple(
        PlanItem(algorithm, upstream_only=algorithm.id not in selected)
        for algorithm in registry
        if algorithm.id in needed
    )


__all__ = [
    "ALGORITHMS",
    "Algorithm",
    "AlgorithmOutput",
    "CONSUMES",
    "COSTS",
    "FAMILIES",
    "InputContext",
    "PlanItem",
    "Ready",
    "Requirements",
    "Skip",
    "ToolPin",
    "context_from_json",
    "context_to_json",
    "execution_identity",
    "intermediate_ids",
    "load_entry",
    "load_registry",
    "registry_entries",
    "plan",
    "validate_algorithm",
]
