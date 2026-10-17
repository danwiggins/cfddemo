"""Cohort-relative arm normalization, before arm-bin aggregation is defined.

Callers supply arm log2 summaries computed with one pinned arm-definition asset.
This module does not derive arm boundaries, estimate a healthy distribution from
unmatched samples, or assign a clinical cutoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from .identity import SHA256_PATTERN

MIN_ELIGIBLE_READS = 2_000_000
MIN_REFERENCE_N = 10
ArmScoreState = Literal[
    "ok", "missing_asset", "insufficient_depth", "missing_target",
    "missing_protocol", "no_reference", "zero_variance",
]


@dataclass(frozen=True)
class ArmObservation:
    source_id: str
    arm_id: str
    protocol_key: str
    log2: float | None


@dataclass(frozen=True)
class ArmScore:
    state: ArmScoreState
    z: float | None
    reference_n: int
    reference_mean: float | None = None
    reference_sd: float | None = None


def _finite(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite or null")
    return float(value)


def score_arm(
    *,
    target_source_id: str,
    arm_id: str,
    target_log2: float | None,
    target_protocol_key: str | None,
    eligible_reads: int | None,
    arm_asset_sha256: str | None,
    observations: Sequence[ArmObservation],
) -> ArmScore:
    """Standardize one arm against compatible controls, excluding the source.

    The empirical reference SD uses the cohort distribution (denominator n).
    There is no abnormality threshold. Every source is counted at most once;
    incompatible protocols and missing arm values cannot enter the reference.
    """

    if not SHA256_PATTERN.fullmatch(target_source_id):
        raise ValueError("target_source_id must be a SHA-256 digest")
    if not arm_id:
        raise ValueError("arm_id is required")
    target = _finite(target_log2, "target_log2")
    if arm_asset_sha256 is None:
        return ArmScore("missing_asset", None, 0)
    if not SHA256_PATTERN.fullmatch(arm_asset_sha256):
        raise ValueError("arm_asset_sha256 must be a SHA-256 digest")
    if eligible_reads is None or eligible_reads < MIN_ELIGIBLE_READS:
        return ArmScore("insufficient_depth", None, 0)
    if target is None:
        return ArmScore("missing_target", None, 0)
    if target_protocol_key is None:
        return ArmScore("missing_protocol", None, 0)
    if not SHA256_PATTERN.fullmatch(target_protocol_key):
        raise ValueError("target_protocol_key must be a SHA-256 digest")

    seen: set[str] = set()
    values: list[float] = []
    for item in observations:
        if not SHA256_PATTERN.fullmatch(item.source_id):
            raise ValueError("reference source_id must be a SHA-256 digest")
        if item.source_id in seen:
            raise ValueError("reference sources must be distinct")
        seen.add(item.source_id)
        value = _finite(item.log2, "reference log2")
        if (item.source_id != target_source_id and item.arm_id == arm_id
                and item.protocol_key == target_protocol_key and value is not None):
            values.append(value)
    if len(values) < MIN_REFERENCE_N:
        return ArmScore("no_reference", None, len(values))

    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
    sd = math.sqrt(variance)
    if sd == 0:
        return ArmScore("zero_variance", None, len(values), mean, sd)
    return ArmScore("ok", (target - mean) / sd, len(values), mean, sd)


__all__ = ["ArmObservation", "ArmScore", "MIN_ELIGIBLE_READS", "MIN_REFERENCE_N", "score_arm"]
