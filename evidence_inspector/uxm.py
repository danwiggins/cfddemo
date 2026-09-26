"""Bounded fragment-level UXM classification over validated CpG calls.

The public entry point consumes an iterable once and keeps at most the
configured number of fragment-marker groups and unique CpGs per group. Raw
fragment digests are retained only in local ``FragmentMarkerObservation``
objects; diagnostics and aggregate marker weights contain no read identifiers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .cell_origin_models import (
    CpgCallState,
    FragmentMarkerObservation,
    GenomicMarker,
    MarkerCountRow,
    ModkitCpgCall,
    Strand,
    UxmState,
    classify_uxm,
)

DEFAULT_MAXIMUM_CALLS = 1_000_000
DEFAULT_MAXIMUM_GROUPS = 100_000
DEFAULT_MAXIMUM_CPGS_PER_GROUP = 10_000
METHYLATED_MODIFICATION_CODES = frozenset({"m", "h"})

_MISSING = object()


class UxmStopReason(StrEnum):
    COMPLETE_INPUT = "complete_input"
    CALL_CAP = "call_cap"
    GROUP_CAP = "group_cap"


@dataclass(frozen=True, slots=True)
class UxmDiagnostics:
    """Aggregate-only diagnostics safe to publish without raw identifiers."""

    inspected_calls: int
    retained_unique_cpg_calls: int
    duplicate_cpg_calls: int
    excluded_failed_calls: int
    excluded_non_c_calls: int
    excluded_unsupported_modification_calls: int
    excluded_invalid_calls: int
    excluded_outside_marker_calls: int
    excluded_ambiguous_marker_calls: int
    excluded_unknown_marker_calls: int
    excluded_conflicting_duplicate_groups: int
    excluded_oversized_groups: int
    fragment_marker_group_count: int
    classified_fragment_marker_count: int
    excluded_fewer_than_four_cpgs: int
    stop_reason: UxmStopReason
    partial_input: bool


@dataclass(frozen=True, slots=True)
class MarkerWeight:
    """Coverage weight for one marker after UXM classification."""

    marker_id: str
    classified_fragment_count: int
    normalized_weight: float


@dataclass(frozen=True, slots=True)
class UxmClassificationResult:
    """Local observations plus aggregate counts, weights, and safe diagnostics."""

    observations: tuple[FragmentMarkerObservation, ...]
    marker_counts: tuple[MarkerCountRow, ...]
    marker_weights: tuple[MarkerWeight, ...]
    diagnostics: UxmDiagnostics


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is _MISSING:
        raise KeyError(name)
    return default


def _call_passed(call: Any) -> bool:
    passed = _field(call, "passed", None)
    if passed is not None:
        return passed is True
    failed = _field(call, "failed", None)
    if failed is not None:
        return failed is False
    status = _field(call, "filter_status", None)
    if status is None:
        status = _field(call, "status", None)
    if status is None:
        return True
    return isinstance(status, str) and status.casefold() in {
        ".",
        "pass",
        "passed",
    }


def _canonical_base(call: Any) -> str:
    value = _field(call, "canonical_base", "C")
    return value.upper() if isinstance(value, str) else ""


def _normalize_call(call: Any) -> tuple[ModkitCpgCall | None, str | None]:
    """Normalize a validated m/h call into the combined methylated-C contract."""

    if not _call_passed(call):
        return None, "failed"
    if _canonical_base(call) != "C":
        return None, "non_c"
    code = _field(call, "modification_code", None)
    if code not in METHYLATED_MODIFICATION_CODES:
        return None, "unsupported_modification"
    try:
        state_value = _field(call, "state")
        state = (
            state_value
            if isinstance(state_value, CpgCallState)
            else CpgCallState(state_value)
        )
        strand_value = _field(call, "strand")
        strand = (
            strand_value
            if isinstance(strand_value, Strand)
            else Strand(strand_value)
        )
        normalized = ModkitCpgCall(
            fragment_digest=_field(call, "fragment_digest"),
            chromosome=_field(call, "chromosome"),
            position0=_field(call, "position0"),
            strand=strand,
            # The shared contract records combined modified cytosine as ``m``.
            # ``h`` is accepted above and intentionally collapsed into that
            # combined state before constructing an observation.
            modification_code="m",
            modified_probability=_field(call, "modified_probability"),
            state=state,
        )
    except (KeyError, TypeError, ValueError):
        return None, "invalid"
    return normalized, None


def _explicit_marker_id(call: Any) -> str | None:
    value = _field(call, "marker_id", None)
    return value if isinstance(value, str) and value else None


def _matching_markers(
    call: ModkitCpgCall,
    markers: Sequence[GenomicMarker],
) -> tuple[GenomicMarker, ...]:
    return tuple(
        marker
        for marker in markers
        if marker.chromosome == call.chromosome
        and marker.start0 <= call.position0 < marker.end0
    )


def _marker_for_call(
    raw_call: Any,
    call: ModkitCpgCall,
    markers: Sequence[GenomicMarker],
    marker_by_id: Mapping[str, GenomicMarker],
) -> tuple[GenomicMarker | None, str | None]:
    explicit_id = _explicit_marker_id(raw_call)
    if explicit_id is not None:
        marker = marker_by_id.get(explicit_id)
        if marker is None:
            return None, "unknown_marker"
        if (
            marker.chromosome != call.chromosome
            or not marker.start0 <= call.position0 < marker.end0
        ):
            return None, "unknown_marker"
        matches = _matching_markers(call, markers)
        if len(matches) != 1 or matches[0] != marker:
            return None, "ambiguous_marker"
        return marker, None

    matches = _matching_markers(call, markers)
    if not matches:
        return None, "outside_marker"
    if len(matches) != 1:
        return None, "ambiguous_marker"
    return matches[0], None


def _validate_limits(
    maximum_calls: int,
    maximum_groups: int,
    maximum_cpgs_per_group: int,
) -> None:
    limits = {
        "maximum_calls": maximum_calls,
        "maximum_groups": maximum_groups,
        "maximum_cpgs_per_group": maximum_cpgs_per_group,
    }
    for name, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")


def aggregate_marker_counts(
    observations: Iterable[FragmentMarkerObservation],
    *,
    marker_order: Sequence[GenomicMarker] = (),
) -> tuple[tuple[MarkerCountRow, ...], tuple[MarkerWeight, ...]]:
    """Aggregate U/X/M counts and normalized coverage weights by marker.

    Markers without classified observations are omitted because the strict
    ``MarkerCountRow`` contract does not permit a zero denominator.
    """

    counts: dict[str, Counter[UxmState]] = {}
    first_seen: list[str] = []
    for observation in observations:
        marker_id = observation.marker.marker_id
        if marker_id not in counts:
            counts[marker_id] = Counter()
            first_seen.append(marker_id)
        counts[marker_id][observation.state] += 1

    ordered_ids = [
        marker.marker_id for marker in marker_order if marker.marker_id in counts
    ]
    ordered_ids.extend(
        marker_id for marker_id in first_seen if marker_id not in ordered_ids
    )
    rows = tuple(
        MarkerCountRow(
            marker_id=marker_id,
            u_count=counts[marker_id][UxmState.U],
            x_count=counts[marker_id][UxmState.X],
            m_count=counts[marker_id][UxmState.M],
            classified_fragment_count=sum(counts[marker_id].values()),
            u_fraction=(
                counts[marker_id][UxmState.U]
                / sum(counts[marker_id].values())
            ),
        )
        for marker_id in ordered_ids
    )
    total = sum(row.classified_fragment_count for row in rows)
    weights = tuple(
        MarkerWeight(
            marker_id=row.marker_id,
            classified_fragment_count=row.classified_fragment_count,
            normalized_weight=row.classified_fragment_count / total,
        )
        for row in rows
    )
    return rows, weights


def classify_uxm_calls(
    calls: Iterable[Any],
    markers: Sequence[GenomicMarker],
    *,
    maximum_calls: int = DEFAULT_MAXIMUM_CALLS,
    maximum_groups: int = DEFAULT_MAXIMUM_GROUPS,
    maximum_cpgs_per_group: int = DEFAULT_MAXIMUM_CPGS_PER_GROUP,
) -> UxmClassificationResult:
    """Classify a bounded stream of per-CpG calls by fragment and marker.

    Input calls may be ``ModkitCpgCall`` objects or mapping/attribute objects
    with the same fields. Optional ``canonical_base`` and pass/fail fields are
    checked defensively. Optional ``marker_id`` assignments must resolve to the
    exact registered interval. Calls without an assignment are joined by
    coordinate only when exactly one marker contains the locus.
    """

    _validate_limits(maximum_calls, maximum_groups, maximum_cpgs_per_group)
    if not markers:
        raise ValueError("at least one marker is required")
    marker_by_id = {marker.marker_id: marker for marker in markers}
    if len(marker_by_id) != len(markers):
        raise ValueError("marker IDs must be unique")

    groups: dict[tuple[str, str], dict[int, ModkitCpgCall]] = {}
    keys_by_fragment: dict[str, set[tuple[str, str]]] = {}
    invalid_fragments: set[str] = set()
    invalid_groups: set[tuple[str, str]] = set()
    counters: Counter[str] = Counter()
    stop_reason = UxmStopReason.COMPLETE_INPUT

    iterator = iter(calls)
    while counters["inspected_calls"] < maximum_calls:
        try:
            raw_call = next(iterator)
        except StopIteration:
            break
        counters["inspected_calls"] += 1
        call, exclusion = _normalize_call(raw_call)
        if call is None:
            counters[f"excluded_{exclusion}_calls"] += 1
            continue
        if call.fragment_digest in invalid_fragments:
            continue

        marker, marker_exclusion = _marker_for_call(
            raw_call,
            call,
            markers,
            marker_by_id,
        )
        if marker is None:
            counters[f"excluded_{marker_exclusion}_calls"] += 1
            if marker_exclusion in {"unknown_marker", "ambiguous_marker"}:
                invalid_fragments.add(call.fragment_digest)
                for key in keys_by_fragment.pop(call.fragment_digest, set()):
                    removed = groups.pop(key, None)
                    if removed is not None:
                        counters["retained_unique_cpg_calls"] -= len(removed)
                    invalid_groups.discard(key)
            continue

        key = (call.fragment_digest, marker.marker_id)
        if key in invalid_groups:
            continue
        if key not in groups:
            if len(groups) >= maximum_groups:
                stop_reason = UxmStopReason.GROUP_CAP
                break
            groups[key] = {}
            keys_by_fragment.setdefault(call.fragment_digest, set()).add(key)

        loci = groups[key]
        existing = loci.get(call.position0)
        if existing is not None:
            counters["duplicate_cpg_calls"] += 1
            if existing.state != call.state:
                counters["excluded_conflicting_duplicate_groups"] += 1
                removed = groups.pop(key)
                counters["retained_unique_cpg_calls"] -= len(removed)
                keys_by_fragment[call.fragment_digest].discard(key)
                invalid_groups.add(key)
            continue
        if len(loci) >= maximum_cpgs_per_group:
            counters["excluded_oversized_groups"] += 1
            removed = groups.pop(key)
            counters["retained_unique_cpg_calls"] -= len(removed)
            keys_by_fragment[call.fragment_digest].discard(key)
            invalid_groups.add(key)
            continue
        loci[call.position0] = call
        counters["retained_unique_cpg_calls"] += 1
    else:
        stop_reason = UxmStopReason.CALL_CAP

    observations: list[FragmentMarkerObservation] = []
    excluded_fewer_than_four = 0
    marker_position = {
        marker.marker_id: position for position, marker in enumerate(markers)
    }
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (marker_position[item[0][1]], item[0][0]),
    )
    for (fragment_digest, marker_id), loci in ordered_groups:
        if len(loci) < 4:
            excluded_fewer_than_four += 1
            continue
        cpg_calls = tuple(
            loci[position] for position in sorted(loci)
        )
        methylated_count = sum(
            call.state == CpgCallState.METHYLATED for call in cpg_calls
        )
        fraction = methylated_count / len(cpg_calls)
        observations.append(
            FragmentMarkerObservation(
                fragment_digest=fragment_digest,
                marker=marker_by_id[marker_id],
                cpg_calls=cpg_calls,
                callable_cpg_count=len(cpg_calls),
                methylated_cpg_count=methylated_count,
                methylation_fraction=fraction,
                state=classify_uxm(fraction, len(cpg_calls)),
            )
        )

    observation_tuple = tuple(observations)
    marker_counts, marker_weights = aggregate_marker_counts(
        observation_tuple,
        marker_order=markers,
    )
    diagnostics = UxmDiagnostics(
        inspected_calls=counters["inspected_calls"],
        retained_unique_cpg_calls=counters["retained_unique_cpg_calls"],
        duplicate_cpg_calls=counters["duplicate_cpg_calls"],
        excluded_failed_calls=counters["excluded_failed_calls"],
        excluded_non_c_calls=counters["excluded_non_c_calls"],
        excluded_unsupported_modification_calls=counters[
            "excluded_unsupported_modification_calls"
        ],
        excluded_invalid_calls=counters["excluded_invalid_calls"],
        excluded_outside_marker_calls=counters[
            "excluded_outside_marker_calls"
        ],
        excluded_ambiguous_marker_calls=counters[
            "excluded_ambiguous_marker_calls"
        ],
        excluded_unknown_marker_calls=counters[
            "excluded_unknown_marker_calls"
        ],
        excluded_conflicting_duplicate_groups=counters[
            "excluded_conflicting_duplicate_groups"
        ],
        excluded_oversized_groups=counters["excluded_oversized_groups"],
        fragment_marker_group_count=len(groups),
        classified_fragment_marker_count=len(observation_tuple),
        excluded_fewer_than_four_cpgs=excluded_fewer_than_four,
        stop_reason=stop_reason,
        partial_input=stop_reason != UxmStopReason.COMPLETE_INPUT,
    )
    return UxmClassificationResult(
        observations=observation_tuple,
        marker_counts=marker_counts,
        marker_weights=marker_weights,
        diagnostics=diagnostics,
    )


__all__ = [
    "DEFAULT_MAXIMUM_CALLS",
    "DEFAULT_MAXIMUM_CPGS_PER_GROUP",
    "DEFAULT_MAXIMUM_GROUPS",
    "METHYLATED_MODIFICATION_CODES",
    "MarkerWeight",
    "UxmClassificationResult",
    "UxmDiagnostics",
    "UxmStopReason",
    "aggregate_marker_counts",
    "classify_uxm_calls",
]
