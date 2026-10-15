"""Defended results and the computed headline sentence (Front end § Landing, D24).

A **defended result** is one algorithm's headline metric that meets all of:

1. the algorithm is ``ok`` at full depth;
2. the metric's state is not ``not_interpretable`` (``in_range`` and
   ``no_reference`` qualify: "defended" describes the measurement);
3. it is ``ok, usable`` at the 0.5 fraction (D24; full depth is trivially usable);
4. split halves A and B are stable against each other (D32);
5. its tier is PROVEN or NEWER;
6. its library entry is citation-checked (D27);
7. where the algorithm has a positive control (copy-number → synthetic CNA
   spike), the spike at TF 0.10 is recovered within ±0.05.

Ranking when more than five qualify: tier (PROVEN first), range states before
``no_reference``, smaller relative split-half |A−B|, registry order.  At most
five rows, one per algorithm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_ROWS = 5
UNLOCK_DEPTHS = (0.5, 1.0, 2.0, 5.0, 10.0)
CONTROLLED = {"copy-number": 0.10}
_TIER = {"PROVEN": 0, "NEWER": 1}
RANGE_STATES = ("in_range", "above_typical", "markedly_above", "below_typical", "markedly_below")


@dataclass
class Candidate:
    """Everything the rule needs about one algorithm's headline metric."""

    algorithm_id: str
    label: str
    value: Any
    unit: str | None
    tier: str | None
    status: str  # full-depth algorithm status
    state: str | None  # D7 state of the headline metric at full depth
    usable_at_half: bool | None  # D24 cell at 0.5
    split_stable: bool | None
    split_relative_diff: float | None
    citation_verified: bool
    control_recovered: bool | None = None  # None: the algorithm has no control
    order: int = 0
    license_ids: list[str] = field(default_factory=list)


def exclusion(candidate: Candidate) -> str | None:
    """The first rule a candidate fails (reason code), or None if defended."""

    if candidate.status != "ok":
        return "not_ok"
    if candidate.state in (None, "not_interpretable"):
        return "not_interpretable"
    if not candidate.usable_at_half:
        return "unstable_at_half"
    if candidate.split_stable is not True:
        return "split_unstable"
    if candidate.tier not in _TIER:
        return "speculative"
    if not candidate.citation_verified:
        return "citation_unverified"
    if candidate.algorithm_id in CONTROLLED and candidate.control_recovered is not True:
        return "control_not_recovered"
    return None


def defended(candidates: Sequence[Candidate]) -> dict[str, Any]:
    qualified = [candidate for candidate in candidates if exclusion(candidate) is None]
    qualified.sort(
        key=lambda c: (
            _TIER[c.tier or "NEWER"],
            0 if c.state in RANGE_STATES else 1,
            c.split_relative_diff if c.split_relative_diff is not None else float("inf"),
            c.order,
        )
    )
    reasons: dict[str, int] = {}
    for candidate in candidates:
        reason = exclusion(candidate)
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
    usable_but_speculative = sum(
        1
        for c in candidates
        if c.status == "ok" and c.state not in (None, "not_interpretable") and c.usable_at_half and c.tier not in _TIER
    )
    return {
        "rows": [_row(c) for c in qualified[:MAX_ROWS]],
        "qualified": len(qualified),
        "none_reasons": {
            "usable_but_speculative": usable_but_speculative,
            "unstable_at_half": reasons.get("unstable_at_half", 0) + reasons.get("split_unstable", 0),
            "citation_unverified": reasons.get("citation_unverified", 0),
            "not_interpretable": reasons.get("not_interpretable", 0),
            "control_not_recovered": reasons.get("control_not_recovered", 0),
        },
    }


def _row(candidate: Candidate) -> dict[str, Any]:
    evidence = ["stable to 0.5×"]
    if candidate.split_relative_diff is not None:
        evidence.append(f"split-half Δ {candidate.split_relative_diff * 100:.1f}%")
    if candidate.algorithm_id in CONTROLLED:
        evidence.append("control recovered")
    return {
        "algorithm_id": candidate.algorithm_id,
        "label": candidate.label,
        "value": candidate.value,
        "unit": candidate.unit,
        "state": candidate.state,
        "tier": candidate.tier,
        "evidence": " · ".join(evidence),
        "license_ids": candidate.license_ids,
    }


def headline(
    depth_x: float | None,
    usable: int,
    total: int,
    defended_count: int,
    min_depths: Mapping[str, float | None],
    *,
    downsampled: bool = True,
    failed: int = 0,
) -> dict[str, Any]:
    """The computed sentence, never free text, plus its unlock clause."""

    depth_text = f"{depth_x:.3g}x" if depth_x is not None else "unknown depth"
    if not downsampled:
        sentence = f"At {depth_text}, {usable} of {total} algorithms ran; stability not measured."
    elif defended_count == 0:
        sentence = f"At {depth_text}, {usable} of {total} algorithms are usable and no result is defended at this depth."
    else:
        noun = "result is" if defended_count == 1 else "results are"
        sentence = f"At {depth_text}, {usable} of {total} algorithms are usable and {defended_count} {noun} defended."
    if failed:
        sentence = sentence[:-1] + f" ({failed} algorithm{'s' if failed != 1 else ''} failed: see Trust/QC)."
    unlock = None
    if depth_x is not None:
        above = [depth for depth in UNLOCK_DEPTHS if depth > depth_x][:2]
        if len(above) == 2:
            first, second = above
            values = [v for v in min_depths.values() if v is not None]
            a = sum(1 for v in values if depth_x < v <= first)
            b = sum(1 for v in values if first < v <= second)
            unlock = {
                "text": f"Literature minimums suggest {first:g}x would add {a} and {second:g}x would add {b} more.",
                "depths": [first, second],
                "counts": [a, b],
                "note": "literature, not measured",
            }
    return {"sentence": sentence, "unlock": unlock}


__all__ = ["Candidate", "MAX_ROWS", "defended", "exclusion", "headline"]
