"""D32 stability functions: is a value at one depth "the same" as at another?

Each scalar metric's library entry declares ``stability: {kind, tolerance}``.

- ``proportion`` (values in [0, 1]): stable if |a − b| ≤ max(0.01,
  1.96 · √(p̄(1−p̄)(1/n_a + 1/n_b))), with p̄ the pooled value and n each
  side's own count (the 0.01 floor alone when a count is missing);
- ``relative``: stable if |a − b| / ((|a| + |b|)/2) ≤ tolerance (default
  0.10); both zero is stable; a zero mean with one side non-zero falls back to
  ``absolute`` with the same tolerance;
- ``absolute`` (signed values): stable if |a − b| ≤ tolerance;
- ``scales_with_depth`` (Lab addition for counts and depth): the value at
  fraction f is expected to be f × the full value, so the relative rule is
  applied to a / f_a against b / f_b;
- **with intervals:** when both values carry a CI of the same level, stable if
  |a − b| ≤ √(hw_a² + hw_b²); this replaces "within the full-depth CI";
- **missing:** a null on either side gives no verdict (``None``);
- vectors and panels get no verdict (``None``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROPORTION_FLOOR = 0.01
DEFAULT_RELATIVE = 0.10
Z = 1.96


@dataclass(frozen=True)
class Side:
    value: Any
    n: int | None = None
    ci: tuple[float, float] | None = None
    ci_level: float | None = None
    fraction: float = 1.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _relative(a: float, b: float, tolerance: float) -> bool:
    if a == 0 and b == 0:
        return True
    mean = (abs(a) + abs(b)) / 2
    if mean == 0:
        return abs(a - b) <= tolerance
    return abs(a - b) / mean <= tolerance


def stable(spec: Mapping[str, Any] | None, a: Side, b: Side) -> bool | None:
    """``True`` stable, ``False`` unstable, ``None`` no verdict."""

    if spec is None or spec.get("kind") == "unvalidated":
        return None
    x, y = _number(a.value), _number(b.value)
    if x is None or y is None:
        return None
    if a.ci is not None and b.ci is not None and a.ci_level is not None and a.ci_level == b.ci_level:
        half_a = (a.ci[1] - a.ci[0]) / 2
        half_b = (b.ci[1] - b.ci[0]) / 2
        return abs(x - y) <= math.sqrt(half_a**2 + half_b**2)
    kind = spec.get("kind")
    tolerance = spec.get("tolerance")
    if kind == "proportion":
        bound = PROPORTION_FLOOR
        if a.n and b.n:
            pooled = (x * a.n + y * b.n) / (a.n + b.n)
            pooled = min(max(pooled, 0.0), 1.0)
            bound = max(PROPORTION_FLOOR, Z * math.sqrt(pooled * (1 - pooled) * (1 / a.n + 1 / b.n)))
        return abs(x - y) <= bound
    if kind == "relative":
        return _relative(x, y, float(tolerance if tolerance is not None else DEFAULT_RELATIVE))
    if kind == "absolute":
        if tolerance is None:
            raise ValueError("an absolute stability needs a tolerance")
        return abs(x - y) <= float(tolerance)
    if kind == "scales_with_depth":
        if a.fraction <= 0 or b.fraction <= 0:
            return None
        return _relative(x / a.fraction, y / b.fraction, float(tolerance if tolerance is not None else DEFAULT_RELATIVE))
    raise ValueError(f"unknown stability kind {kind!r}")


def deviation(spec: Mapping[str, Any] | None, value: Any, full: Any, fraction: float = 1.0) -> tuple[float | None, str]:
    """The Stability-lens y value: relative deviation in % from the full-depth
    value, or the signed difference in the metric's unit when the metric is
    ``absolute`` or the full value is 0 (a relative deviation is undefined)."""

    if spec is not None and spec.get("kind") == "unvalidated":
        return None, "none"
    x, y = _number(value), _number(full)
    if x is None or y is None:
        return None, "none"
    kind = (spec or {}).get("kind")
    if kind == "scales_with_depth" and fraction > 0:
        x = x / fraction
    if kind == "absolute" or y == 0:
        return x - y, "unit"
    return (x - y) / abs(y) * 100.0, "percent"


def band_at(spec: Mapping[str, Any] | None, a: Side, full: Side) -> tuple[float, float, str] | None:
    """The D32 acceptance interval for ``a`` against ``full``, on the Stability
    lens axis (``deviation``): stable iff lo <= deviation <= hi (up to the
    pooled-proportion approximation, which uses the full value for p̄)."""

    y = _number(full.value)
    if spec is None or spec.get("kind") == "unvalidated" or y is None:
        return None
    kind = spec.get("kind")
    tolerance = spec.get("tolerance")
    if a.ci is not None and full.ci is not None and a.ci_level is not None and a.ci_level == full.ci_level:
        width = math.sqrt(((a.ci[1] - a.ci[0]) / 2) ** 2 + ((full.ci[1] - full.ci[0]) / 2) ** 2)
        return _as_axis(width, y, kind)
    if kind == "absolute":
        return -float(tolerance), float(tolerance), "unit"
    if kind == "proportion":
        bound = PROPORTION_FLOOR
        if a.n and full.n:
            p = min(max(y, 0.0), 1.0)
            bound = max(PROPORTION_FLOOR, Z * math.sqrt(p * (1 - p) * (1 / a.n + 1 / full.n)))
        return _as_axis(bound, y, kind)
    t = float(tolerance if tolerance is not None else DEFAULT_RELATIVE)
    if y == 0:
        return -t, t, "unit"
    if t >= 2:
        return -100.0, math.inf, "percent"
    return ((2 - t) / (2 + t) - 1) * 100.0, ((2 + t) / (2 - t) - 1) * 100.0, "percent"


def _as_axis(width: float, full: float, kind: Any) -> tuple[float, float, str]:
    if kind == "absolute" or full == 0:
        return -width, width, "unit"
    return -width / abs(full) * 100.0, width / abs(full) * 100.0, "percent"


__all__ = ["Side", "band_at", "deviation", "stable"]
