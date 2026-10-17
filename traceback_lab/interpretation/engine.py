"""Interpretation library v0: loader, read-time join and the D7/D8 state machine.

Nothing here touches a stored result or an execution identity (D3): the
library is joined to results when they are read, and its version is reported
next to them (``interpretation_version``).

States (D7), in precedence order once the algorithm itself is ``ok``:

1. ``not_interpretable`` (reason: depth / qc / no_pon / below_lod / missing_channel)
2. ``no_reference`` (reason: none_published / cohort_too_small / no_library_entry)
3. a range state: ``in_range``, ``above_typical``, ``markedly_above``,
   ``below_typical``, ``markedly_below``

Reference rules (D8): ``published_range`` gives in/above/below (never
"markedly"); ``internal_cohort`` needs >= 10 eligible members, uses min/max
with an "exploratory (n=N)" tag below 30, and empirical percentiles with a
bootstrap interval of the 95th-percentile cut-off from 30.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
LIBRARY_FILE = HERE / "library.yaml"
LICENSES_FILE = HERE / "licenses.yaml"

TIERS = ("PROVEN", "NEWER", "SPECULATIVE")
REFERENCE_KINDS = ("none", "published_mean", "published_range", "internal_cohort", "provisional_external")
STABILITY_KINDS = ("proportion", "relative", "absolute", "scales_with_depth", "unvalidated")
VALUE_KINDS = ("proportion", "positive", "count", "signed", "vector", "categorical")
NOT_INTERPRETABLE_REASONS = ("depth", "qc", "no_pon", "below_lod", "missing_channel")
NO_REFERENCE_REASONS = ("none_published", "cohort_too_small", "no_library_entry")
RANGE_STATES = ("in_range", "above_typical", "markedly_above", "below_typical", "markedly_below")
OUT_OF_RANGE = ("markedly_above", "markedly_below", "above_typical", "below_typical")
COHORT_MIN = 10
COHORT_FULL = 30
BOOTSTRAP_RESAMPLES = 1000
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_DEFAULT_STABILITY = {
    "proportion": {"kind": "proportion"},
    "positive": {"kind": "relative", "tolerance": 0.10},
    "count": {"kind": "relative", "tolerance": 0.10},
}


class LibraryError(ValueError):
    """The library file is invalid; the message names the entry."""


@dataclass(frozen=True)
class Entry:
    algorithm_id: str
    metric_id: str
    raw: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def tier(self) -> str:
        return str(self.raw["tier"])

    @property
    def headline(self) -> bool:
        return bool(self.raw.get("headline", False))

    @property
    def citation_verified(self) -> bool:
        checked = self.raw.get("citation_checked")
        return isinstance(checked, Mapping) and all(checked.get(key) for key in ("by", "date"))

    @property
    def stability(self) -> dict[str, Any] | None:
        value = self.raw.get("stability")
        return dict(value) if value else None


@dataclass(frozen=True)
class Library:
    version: str
    entries: Mapping[tuple[str, str], Entry]
    licenses: Mapping[str, Mapping[str, str]]
    hidden_algorithms: frozenset[str] = frozenset()

    def entry(self, algorithm_id: str, metric_id: str) -> Entry | None:
        return self.entries.get((algorithm_id, metric_id))

    def headline(self, algorithm_id: str) -> Entry | None:
        for (alg, _), entry in self.entries.items():
            if alg == algorithm_id and entry.headline:
                return entry
        return None

    def algorithms(self) -> list[str]:
        seen: list[str] = []
        for alg, _ in self.entries:
            if alg not in seen:
                seen.append(alg)
        return seen


def _require(condition: bool, where: str, message: str) -> None:
    if not condition:
        raise LibraryError(f"{where}: {message}")


def _validate_entry(raw: Mapping[str, Any], licenses: Mapping[str, Any]) -> Entry:
    where = f"{raw.get('algorithm_id')}/{raw.get('metric_id')}"
    for key in ("algorithm_id", "metric_id", "label", "tier", "reference", "headline", "value_kind"):
        _require(key in raw, where, f"missing {key}")
    _require(isinstance(raw["headline"], bool), where, "headline must be true or false")
    _require(raw["tier"] in TIERS, where, f"tier must be one of {TIERS}")
    _require(raw.get("origin_tier", raw["tier"]) in TIERS, where, "origin_tier must be a tier")
    _require(raw["value_kind"] in VALUE_KINDS, where, f"value_kind must be one of {VALUE_KINDS}")
    reference = raw["reference"]
    _require(isinstance(reference, Mapping) and reference.get("kind") in REFERENCE_KINDS, where, "bad reference kind")
    if reference["kind"] == "published_range":
        _require(
            isinstance(reference.get("lo"), (int, float)) and isinstance(reference.get("hi"), (int, float))
            and reference["lo"] <= reference["hi"],
            where,
            "published_range needs lo <= hi",
        )
    if reference["kind"] == "published_mean":
        _require(isinstance(reference.get("value"), (int, float)), where, "published_mean needs a value")
    for license_id in raw.get("license_ids") or []:
        _require(license_id in licenses, where, f"unknown license_id {license_id!r}")
    if raw.get("min_depth_x") is not None:
        _require(isinstance(raw["min_depth_x"], (int, float)) and raw["min_depth_x"] >= 0, where, "min_depth_x >= 0")
    if raw.get("performance") is None:
        _require(bool(raw.get("performance_missing_reason")), where, "performance or performance_missing_reason")
    citations = raw.get("citations", [])
    _require(isinstance(citations, list) and all(isinstance(citation, str) and citation.strip()
                                                for citation in citations),
             where, "citations must be nonempty strings")
    checked = raw.get("citation_checked")
    if checked is not None:
        _require(isinstance(checked, Mapping) and checked.get("by") and checked.get("date"), where, "citation_checked needs by and date")
    stability = raw.get("stability")
    if raw["value_kind"] in ("vector", "categorical"):
        stability = None
    elif stability is None:
        if raw["value_kind"] in _DEFAULT_STABILITY:
            stability = dict(_DEFAULT_STABILITY[raw["value_kind"]])
        else:
            _require(
                raw["value_kind"] == "signed" and raw["tier"] == "SPECULATIVE" and reference["kind"] == "none",
                where,
                "a signed metric needs an explicit stability unless it is speculative with no reference",
            )
    if stability is not None:
        _require(stability.get("kind") in STABILITY_KINDS, where, f"stability kind in {STABILITY_KINDS}")
        if stability["kind"] in ("absolute", "relative", "scales_with_depth"):
            tolerance = stability.get("tolerance")
            if stability["kind"] == "relative" and tolerance is None:
                stability["tolerance"] = 0.10
            else:
                _require(isinstance(tolerance, (int, float)) and tolerance > 0, where, "stability tolerance > 0")
    band = raw.get("qc_band")
    if band is not None:
        _require(band.get("direction") in ("higher_is_better", "lower_is_better"), where, "qc_band direction")
        _require(isinstance(band.get("pass"), (int, float)) and isinstance(band.get("warn"), (int, float)), where, "qc_band pass/warn")
    data = dict(raw)
    data["stability"] = stability
    return Entry(str(raw["algorithm_id"]), str(raw["metric_id"]), data)


def parse_library(library_text: str, licenses_text: str) -> Library:
    """Parse and validate (``yaml.safe_load`` only)."""

    try:
        library = yaml.safe_load(library_text)
        licenses_doc = yaml.safe_load(licenses_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" (line {mark.line + 1})" if mark is not None else ""
        raise LibraryError(f"library YAML does not parse{line}") from exc
    _require(isinstance(licenses_doc, Mapping) and isinstance(licenses_doc.get("licenses"), Mapping), "licenses", "missing licenses map")
    licenses = {str(key): dict(value) for key, value in licenses_doc["licenses"].items()}
    for key, value in licenses.items():
        _require(bool(value.get("headline")) and bool(value.get("body")), f"license {key}", "needs headline and body")
    _require(isinstance(library, Mapping), "library", "not a mapping")
    version = str(library.get("version", ""))
    _require(bool(_SEMVER.fullmatch(version)), "library", "version must be semver (x.y.z)")
    entries: dict[tuple[str, str], Entry] = {}
    for raw in library.get("entries") or []:
        entry = _validate_entry(raw, licenses)
        key = (entry.algorithm_id, entry.metric_id)
        _require(key not in entries, f"{key[0]}/{key[1]}", "duplicate entry")
        entries[key] = entry
    headlines: dict[str, int] = {}
    for (alg, _), entry in entries.items():
        headlines[alg] = headlines.get(alg, 0) + int(entry.headline)
    for alg, count in headlines.items():
        _require(count == 1, alg, f"exactly one headline metric is required, found {count}")
    return Library(
        version=version,
        entries=entries,
        licenses=licenses,
        hidden_algorithms=frozenset(library.get("hidden_algorithms") or ()),
    )


@lru_cache(maxsize=4)
def _load_cached(library_path: str, licenses_path: str, mtimes: tuple[int, int]) -> Library:
    return parse_library(Path(library_path).read_text(), Path(licenses_path).read_text())


def load_library(library_path: Path = LIBRARY_FILE, licenses_path: Path = LICENSES_FILE) -> Library:
    """Load (cached by mtime) and validate the shipped library."""

    mtimes = (library_path.stat().st_mtime_ns, licenses_path.stat().st_mtime_ns)
    return _load_cached(str(library_path), str(licenses_path), mtimes)


# ----------------------------------------------------------------- states
@dataclass(frozen=True)
class Cohort:
    """Eligible cohort values for one metric (D8: same cohort_compat_key, status ok).

    A sample that is itself a member is passed with its own value removed
    (leave-one-out) by the caller.
    """

    values: Sequence[float]
    version: str | None = None
    kind: str = "internal_cohort"  # or provisional_external

    @property
    def n(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class Interpretation:
    state: str
    reason: str | None = None
    reference_kind: str = "none"
    exploratory_n: int | None = None
    percentile: float | None = None
    interval: tuple[float, float] | None = None
    severity: float = 0.0
    caption: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def out_of_range(self) -> bool:
        return self.state in OUT_OF_RANGE


def percentile_of(value: float, values: Sequence[float]) -> float:
    """Empirical percentile of ``value`` among ``values``: the inverse of numpy's
    default (linear) percentile, clamped to 0-100.  Exact ties take the
    mid-rank of the tied block (numpy's forward quantile does not fix a
    convention for the inverse)."""

    data = np.sort(np.asarray(values, dtype=float))
    ranks = np.linspace(0.0, 100.0, data.size)
    if value < data[0]:
        return 0.0
    if value > data[-1]:
        return 100.0
    # Ties: the mid-rank of the tied block (a value equal to every member is p = 50).
    exact = data == value
    if exact.any():
        return float(ranks[exact].mean())
    return float(np.interp(value, data, ranks))


def bootstrap_p95_interval(values: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = 0) -> tuple[float, float]:
    """90% bootstrap CI of the 95th-percentile threshold (1000 resamples, seed 0)."""

    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    draws = rng.choice(data, size=(resamples, data.size), replace=True)
    cuts = np.percentile(draws, 95, axis=1)
    return float(np.percentile(cuts, 5)), float(np.percentile(cuts, 95))


def classify(
    value: Any,
    entry: Entry | None,
    *,
    depth_x: float | None = None,
    not_interpretable: str | None = None,
    cohort: Cohort | None = None,
    include_interval: bool = True,
) -> Interpretation:
    """The D7/D8 state of one metric value (the algorithm itself is ``ok``)."""

    reference = entry["reference"] if entry is not None else {}
    kind = reference.get("kind", "none")
    if not_interpretable is not None:
        if not_interpretable not in NOT_INTERPRETABLE_REASONS:
            raise ValueError(f"unknown not_interpretable reason {not_interpretable!r}")
        return Interpretation("not_interpretable", not_interpretable, kind)
    if entry is None:
        return Interpretation("no_reference", "no_library_entry", caption="No library entry for this metric.")
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        if reference.get("missing_note"):
            # The producer leaves this metric empty by design (e.g. duplicates
            # are not marked on native ONT), not for lack of depth.
            return Interpretation("no_reference", "none_published", kind, caption=str(reference["missing_note"]))
        caption = entry.get("missing_value_caption") or "Metric unavailable at this depth."
        return Interpretation("not_interpretable", "depth", kind, caption=str(caption))
    minimum = entry.get("min_depth_x")
    if minimum is not None and depth_x is not None and depth_x < minimum:
        return Interpretation(
            "not_interpretable",
            "depth",
            kind,
            caption=f"Needs {minimum:g}x; this input is {depth_x:.3g}x. The estimate stays visible.",
        )
    if entry["value_kind"] in ("vector", "categorical") or not isinstance(value, (int, float)):
        return Interpretation("no_reference", "none_published", kind)
    number = float(value)
    if kind in ("none", "published_mean"):
        caption = (
            f"Published {reference.get('statistic', 'mean')} {reference['value']:g} {reference.get('unit', '')} "
            f"({reference.get('source', '')}{', n = ' + str(reference['n']) if reference.get('n') else ''}), no interval"
            if kind == "published_mean"
            else reference.get("missing_note") or "No published reference."
        )
        return Interpretation("no_reference", "none_published", kind, caption=caption.replace(" )", ")"))
    if kind == "published_range":
        lo, hi = float(reference["lo"]), float(reference["hi"])
        width = (hi - lo) or abs(hi) or 1.0
        caption = f"Published range {lo:g}–{hi:g} {reference.get('unit', '')} ({reference.get('source', '')}, n = {reference.get('n', '?')}). Not a percentile."
        if number > hi:
            return Interpretation("above_typical", None, kind, interval=(lo, hi) if include_interval else None, severity=(number - hi) / width, caption=caption)
        if number < lo:
            return Interpretation("below_typical", None, kind, interval=(lo, hi) if include_interval else None, severity=(lo - number) / width, caption=caption)
        return Interpretation("in_range", None, kind, interval=(lo, hi) if include_interval else None, caption=caption)
    # internal_cohort / provisional_external
    if cohort is None or cohort.n < COHORT_MIN:
        n = 0 if cohort is None else cohort.n
        return Interpretation(
            "no_reference",
            "cohort_too_small",
            kind,
            caption=f"No reference yet (cohort n = {n} of {COHORT_MIN}).",
            details={"cohort_n": n},
        )
    external = cohort.kind == "provisional_external"
    suffix = " Protocol-mismatched external reference." if external else ""
    values = np.asarray(cohort.values, dtype=float)
    if cohort.n < COHORT_FULL:
        low, high = float(values.min()), float(values.max())
        span = (high - low) or abs(high) or 1.0
        caption = f"Cohort min–max, n = {cohort.n}, exploratory. Not a percentile; no confidence interval.{suffix}"
        if number > high:
            state, severity = "above_typical", (number - high) / span
        elif number < low:
            state, severity = "below_typical", (low - number) / span
        else:
            state, severity = "in_range", 0.0
        return Interpretation(state, None, kind, exploratory_n=cohort.n, interval=(low, high) if include_interval else None, severity=severity, caption=caption)
    p = percentile_of(number, values)
    if p > 99:
        state = "markedly_above"
    elif p > 95:
        state = "above_typical"
    elif p < 1:
        state = "markedly_below"
    elif p < 5:
        state = "below_typical"
    else:
        state = "in_range"
    interval = bootstrap_p95_interval(values) if include_interval else None
    caption = (
        f"p = {p:.1f} against n = {cohort.n}."
        + (f" Whisker: 90% bootstrap interval of the 95th-percentile cut-off ({BOOTSTRAP_RESAMPLES} resamples)." if include_interval else "")
        + suffix
    )
    return Interpretation(state, None, kind, percentile=p, interval=interval, severity=abs(p - 50), caption=caption)


def qc_band(value: Any, entry: Entry | None) -> str | None:
    """Trust/QC band: pass / warn / fail (provisional team defaults)."""

    band = entry.get("qc_band") if entry is not None else None
    if band is None or not isinstance(value, (int, float)):
        return None
    if band["direction"] == "higher_is_better":
        return "pass" if value >= band["pass"] else "warn" if value >= band["warn"] else "fail"
    return "pass" if value < band["pass"] else "warn" if value <= band["warn"] else "fail"


_TIER_ORDER = {tier: index for index, tier in enumerate(TIERS)}
_STATE_ORDER = {state: index for index, state in enumerate(OUT_OF_RANGE)}


def findings_sort_key(tier: str, interpretation: Interpretation) -> tuple[int, int, float]:
    """D6/D7: state group, then tier (PROVEN first), then severity (largest first)."""

    return (_STATE_ORDER.get(interpretation.state, 99), _TIER_ORDER.get(tier, 9), -interpretation.severity)


@dataclass(frozen=True)
class InterpretedMetric:
    algorithm_id: str
    metric_id: str
    value: Any
    unit: str | None
    n: int | None
    entry: Entry | None
    interpretation: Interpretation

    @property
    def label(self) -> str:
        return str(self.entry["label"]) if self.entry else self.metric_id

    @property
    def tier(self) -> str | None:
        return self.entry.tier if self.entry else None


def interpret_result(
    envelope: Any,
    library: Library,
    *,
    depth_x: float | None = None,
    cohorts: Mapping[str, Cohort] | None = None,
    not_interpretable: Mapping[str, str] | None = None,
    include_interval: bool = True,
) -> list[InterpretedMetric]:
    """Join one result's metrics to the library.  A failed or skipped algorithm
    shows no metrics (D7).  A metric without an entry is kept as
    ``no_reference(no_library_entry)``, never dropped."""

    if envelope.status != "ok":
        return []
    values = {metric.metric_id: metric.value for metric in envelope.metrics}
    out: list[InterpretedMetric] = []
    for metric in envelope.metrics:
        entry = library.entry(envelope.algorithm_id, metric.metric_id)
        reason = (not_interpretable or {}).get(metric.metric_id)
        if reason is None and entry is not None and entry.get("requires_pon") and values.get("pon_policy") in (None, "none"):
            reason = "no_pon"  # Appendix A: without a PoN the value stays visible
        interpretation = classify(
            metric.value,
            entry,
            depth_x=depth_x,
            not_interpretable=reason,
            cohort=(cohorts or {}).get(metric.metric_id),
            include_interval=include_interval,
        )
        out.append(
            InterpretedMetric(
                envelope.algorithm_id, metric.metric_id, metric.value, metric.unit, metric.n, entry, interpretation
            )
        )
    return out


def license_callouts(entries: Iterable[Entry | None], library: Library) -> list[dict[str, str]]:
    """Every license used by ``entries``, once each, in first-use order."""

    seen: list[str] = []
    for entry in entries:
        for license_id in (entry.get("license_ids") or []) if entry else []:
            if license_id not in seen:
                seen.append(license_id)
    return [{"license_id": key, **library.licenses[key]} for key in seen]


__all__ = [
    "COHORT_MIN",
    "OUT_OF_RANGE",
    "Cohort",
    "Entry",
    "Interpretation",
    "InterpretedMetric",
    "Library",
    "LibraryError",
    "bootstrap_p95_interval",
    "classify",
    "findings_sort_key",
    "interpret_result",
    "license_callouts",
    "load_library",
    "parse_library",
    "percentile_of",
    "qc_band",
]
