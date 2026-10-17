"""Read-side views of one sample: results, interpretation, depth curve, stability, controls.

Everything here is computed at read time from the store and the
interpretation library (D3): nothing is written.  The Lab server (S6a/S6c)
serves these structures; the CLI can print them too.

Depth→utility curve (D24): one row per (non-hidden) algorithm, keyed by its
``headline: true`` metric.  Cells: one per measured fraction (downsample
inputs plus the full input).  States: ``failed``, ``skipped``,
``not_interpretable``, ``ok_unstable`` or ``ok_usable``.  "Usable" means not
skipped, not not-interpretable, and stable against the full-depth value under
the metric's D32 stability function (the full-depth cell is usable by
definition).  Above this run's depth there are no cells; the library's
``min_depth_x`` is drawn as a "literature, not measured" marker.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .envelope import ResultEnvelope
from .ingest import id12_for, input_manifest, source_record
from .interpretation.engine import Library, classify, load_library
from .problems import LabProblem
from .registry import load_registry
from .sample_metadata import sex_assertion, sex_concordance
from .stability import Side, band_at, deviation, stable
from .store import LabStore

CELL_STATES = ("ok_usable", "ok_unstable", "not_interpretable", "skipped", "failed")
_ROW_ORDER = {state: index for index, state in enumerate(CELL_STATES)}
_TIER_ORDER = {"PROVEN": 0, "NEWER": 1, "SPECULATIVE": 2, None: 3}


def selected_results(
    store: LabStore, input_id: str, identities: Mapping[str, str] | None = None,
    *, issues: dict[str, set[str]] | None = None,
) -> dict[str, ResultEnvelope]:
    """The selected result per algorithm for one input.

    Without ``identities`` the latest published execution identity wins; with them (a
    derived input compared to its full input) only the result computed under
    the full input's own ``parameters_sha256`` is used, so a stale derived
    result from older parameters is never compared to a newer full result.
    Multiple legacy identities with no recorded publication order are ambiguous
    and omitted instead of guessing from timestamp-shaped attempt ids.
    """

    base = store.results_dir / input_id
    out: dict[str, tuple[int | None, ResultEnvelope]] = {}
    ambiguous: set[str] = set()
    unreadable: set[str] = set()
    if not base.is_dir():
        return {}
    for algorithm_dir in sorted(base.iterdir()):
        if not algorithm_dir.is_dir():
            continue
        wanted = None if identities is None else identities.get(algorithm_dir.name)
        if identities is not None and wanted is None:
            continue
        for identity_dir in sorted(algorithm_dir.iterdir()):
            if wanted is not None and identity_dir.name != wanted:
                continue
            try:
                selected = store.selected_result_with_order(
                    input_id, algorithm_dir.name, identity_dir.name,
                )
            except (OSError, ValueError):
                selected = None
            if selected is None:
                # A present but invalid selector may be the newest identity.
                # Showing another identity would silently return stale data.
                try:
                    os.lstat(identity_dir / "selected.json")
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
                unreadable.add(algorithm_dir.name)
                continue
            envelope, order = selected
            current = out.get(algorithm_dir.name)
            if current is None or (order is not None and (current[0] is None or order > current[0])):
                out[algorithm_dir.name] = (order, envelope)
                ambiguous.discard(algorithm_dir.name)
            elif order == current[0]:
                ambiguous.add(algorithm_dir.name)
    if issues is not None:
        issues["unreadable"] = unreadable
        issues["ambiguous"] = ambiguous - unreadable
    return {
        key: value for key, (_, value) in out.items()
        if key not in ambiguous and key not in unreadable
    }


@dataclass
class InputView:
    label: str  # "full", "0.5", "split-A", "cna-0.1"
    input_id: str
    transform: str | None
    parameters: Mapping[str, Any]
    results: dict[str, ResultEnvelope]
    fraction: float = 1.0
    unreadable: set[str] = field(default_factory=set)
    ambiguous: set[str] = field(default_factory=set)

    def depth_x(self) -> float | None:
        for envelope in self.results.values():
            if envelope.depth is not None:
                return envelope.depth.approx_depth_x
        return None


@dataclass
class SampleView:
    source_id: str
    id12: str
    example: bool
    full: InputView
    derived: list[InputView] = field(default_factory=list)
    operator_metadata: dict[str, str] | None = None
    operator_metadata_problem: LabProblem | None = None

    def by_transform(self, transform: str) -> list[InputView]:
        return [item for item in self.derived if item.transform == transform]


def load_sample(store: LabStore, source_id: str) -> SampleView | None:
    record = source_record(store, source_id)
    if record is None:
        return None
    inputs = sorted(record.get("inputs", []), key=lambda entry: entry.get("used_utc", ""), reverse=True)
    full_input = next((entry["input_id"] for entry in inputs if input_manifest(store, entry["input_id"])), None)
    if full_input is None:
        return None
    full = InputView("full", full_input, None, {}, selected_results(store, full_input))
    identities = {alg: envelope.provenance.parameters_sha256 for alg, envelope in full.results.items()}
    derived = []
    for entry in record.get("derived", []):
        if entry.get("parent_input_id") != full_input:
            continue
        fraction = 0.5 if entry["transform"] == "split" else float(entry["parameters"].get("fraction", 1.0))
        issues: dict[str, set[str]] = {}
        derived.append(
            InputView(
                entry["label"],
                entry["input_id"],
                entry["transform"],
                entry.get("parameters", {}),
                selected_results(store, entry["input_id"], identities, issues=issues),
                fraction=fraction if entry["transform"] in ("downsample", "split") else 1.0,
                unreadable=issues.get("unreadable", set()),
                ambiguous=issues.get("ambiguous", set()),
            )
        )
    try:
        assertion = sex_assertion(record)
        metadata_problem = None
    except LabProblem as problem:
        assertion = None
        metadata_problem = problem
    return SampleView(
        source_id, id12_for(store, source_id), bool(record.get("example")), full, derived,
        assertion, metadata_problem,
    )


def sex_check_view(sample: SampleView) -> dict[str, Any]:
    """Compare a local operator assertion with an already measured definite call."""

    declared = sample.operator_metadata["sex"] if sample.operator_metadata else None
    observed = None
    result = sample.full.results.get("sex-check")
    if result is not None and result.status == "ok":
        observed = next((str(row.value) for row in result.metrics if row.metric_id == "call" and row.value is not None), None)
    if sample.operator_metadata_problem is not None:
        problem = sample.operator_metadata_problem
        return {
            "state": "no_verdict", "reason": "malformed_metadata", "declared": None,
            "observed": observed,
            "problem": {"code": problem.code, "summary": problem.summary,
                        "cause": problem.cause, "fix": problem.fix, "docs": problem.docs},
        }
    return sex_concordance(declared, observed)


def _value(envelope: ResultEnvelope | None, metric_id: str) -> Any:
    if envelope is None or envelope.status != "ok":
        return None
    for metric in envelope.metrics:
        if metric.metric_id == metric_id:
            return metric
    return None


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _side(metric: Any, fraction: float) -> Side:
    if metric is None:
        return Side(None, fraction=fraction)
    return Side(metric.value, n=metric.n, ci=metric.ci, ci_level=metric.ci_level, fraction=fraction)


def _split_half_status(view: InputView | None, algorithm_id: str, metric_id: str,
                       full_value: Any, spec: Mapping[str, Any], *, other_half_recorded: bool = False) -> dict[str, Any]:
    if view is None:
        if other_half_recorded:
            return {"state": "unavailable", "reason": "split half unavailable", "value": None, "deviation": None}
        return {"state": "not_recorded", "reason": "no split-half input recorded", "value": None, "deviation": None}
    envelope = view.results.get(algorithm_id)
    if envelope is None:
        if algorithm_id in getattr(view, "unreadable", ()):
            return {"state": "unavailable", "reason": "result unreadable", "value": None, "deviation": None}
        if algorithm_id in getattr(view, "ambiguous", ()):
            return {"state": "unavailable", "reason": "result selection ambiguous", "value": None, "deviation": None}
        return {"state": "unavailable", "reason": "selected result unavailable", "value": None, "deviation": None}
    if envelope.status != "ok":
        reason = envelope.skip_reason if envelope.status == "skipped" else "algorithm failed"
        return {"state": envelope.status, "reason": reason, "value": None, "deviation": None}
    current = _value(envelope, metric_id)
    try:
        current_finite = current is not None and _numeric(current.value) and math.isfinite(current.value)
    except OverflowError:
        current_finite = False
    if not current_finite:
        return {"state": "not_interpretable", "reason": "metric value unavailable", "value": None, "deviation": None}
    difference, unit = deviation(spec, current.value, full_value, 0.5)
    return {"state": "ok", "reason": None, "value": current.value, "deviation": difference, "unit": unit}


def cell_state(
    view: InputView, full: InputView, algorithm_id: str, metric_id: str, library: Library
) -> dict[str, Any]:
    envelope = view.results.get(algorithm_id)
    if envelope is None:
        return {"state": None}
    if envelope.status == "failed":
        return {"state": "failed", "reason": envelope.failure.first_line if envelope.failure else None}
    if envelope.status == "skipped":
        return {"state": "skipped", "reason": envelope.skip_reason}
    entry = library.entry(algorithm_id, metric_id)
    metric = _value(envelope, metric_id)
    if metric is None:
        return {"state": "not_interpretable", "reason": "missing_metric"}
    values = {m.metric_id: m.value for m in envelope.metrics}
    reason = "no_pon" if entry is not None and entry.get("requires_pon") and values.get("pon_policy") in (None, "none") else None
    interpretation = classify(metric.value, entry, depth_x=view.depth_x(), not_interpretable=reason)
    if interpretation.state == "not_interpretable":
        return {"state": "not_interpretable", "reason": interpretation.reason, "value": metric.value}
    if view is full:
        return {"state": "ok_usable", "value": metric.value}
    full_metric = _value(full.results.get(algorithm_id), metric_id)
    verdict = stable(entry.stability if entry else None, _side(metric, view.fraction), _side(full_metric, 1.0))
    return {
        "state": "ok_usable" if verdict else "ok_unstable",
        "value": metric.value,
        "verdict": verdict,
    }


def depth_curve(sample: SampleView, library: Library | None = None) -> dict[str, Any]:
    library = library or load_library()
    registry_order = [algorithm.id for algorithm in load_registry()]
    columns = sorted(sample.by_transform("downsample"), key=lambda view: view.fraction) + [sample.full]
    run_depth = sample.full.depth_x()
    rows = []
    for algorithm_id in registry_order:
        if algorithm_id in library.hidden_algorithms:
            continue
        headline = library.headline(algorithm_id)
        if headline is None:
            continue
        cells = []
        for view in columns:
            state = cell_state(view, sample.full, algorithm_id, headline.metric_id, library)
            depth = view.depth_x() if view is not sample.full else run_depth
            cells.append({"label": view.label, "fraction": view.fraction, "depth_x": depth, **state})
        full_state = cells[-1]["state"]
        rows.append(
            {
                "algorithm_id": algorithm_id,
                "metric_id": headline.metric_id,
                "label": headline["label"],
                "tier": headline.tier,
                "min_depth_x": headline.get("min_depth_x"),
                "literature_marker": "literature, not measured" if headline.get("min_depth_x") else None,
                "cells": cells,
                "full_state": full_state,
            }
        )
    rows.sort(
        key=lambda row: (
            _ROW_ORDER.get(row["full_state"], 9),
            _TIER_ORDER.get(row["tier"], 9),
            registry_order.index(row["algorithm_id"]),
        )
    )
    return {
        "run_depth_x": run_depth,
        "fractions": [view.fraction for view in columns],
        "rows": rows,
        "downsampled": bool(sample.by_transform("downsample")),
    }


def stability_panels(sample: SampleView, library: Library | None = None) -> list[dict[str, Any]]:
    """One small-multiple per scalar metric: deviation from full depth at each
    fraction, the D32 tolerance band, and split halves A/B at 50%."""

    library = library or load_library()
    order = [algorithm.id for algorithm in load_registry()]
    downs = sorted(sample.by_transform("downsample"), key=lambda view: view.fraction)
    halves = {view.parameters.get("half"): view for view in sample.by_transform("split")}
    panels = []
    for algorithm_id in order:
        if algorithm_id in library.hidden_algorithms:
            continue
        full_envelope = sample.full.results.get(algorithm_id)
        if full_envelope is None or full_envelope.status != "ok":
            continue
        for metric in full_envelope.metrics:
            entry = library.entry(algorithm_id, metric.metric_id)
            spec = entry.stability if entry else None
            if spec is None or not isinstance(metric.value, (int, float)) or isinstance(metric.value, bool):
                continue
            points = []
            for view in [*downs, sample.full]:
                state = cell_state(view, sample.full, algorithm_id, metric.metric_id, library)
                envelope = view.results.get(algorithm_id)
                current = _value(envelope, metric.metric_id) if envelope is not None else None
                y, unit = deviation(spec, current.value if current else None, metric.value, view.fraction)
                interval = band_at(spec, _side(current, view.fraction), _side(metric, 1.0)) if current else None
                points.append(
                    {
                        "fraction": view.fraction,
                        "label": view.label,
                        "state": state.get("state"),
                        "reason": state.get("reason"),
                        "value": current.value if current else None,
                        "deviation": y,
                        "unit": unit,
                        "band": {"lo": interval[0], "hi": interval[1], "unit": interval[2]} if interval else None,
                    }
                )
            split = None
            split_status = {
                half: _split_half_status(halves.get(half), algorithm_id, metric.metric_id, metric.value, spec,
                                         other_half_recorded=bool(halves))
                for half in ("A", "B")
            }
            if halves and not any(half in halves for half in ("A", "B")):
                for status in split_status.values():
                    status["reason"] = "split input has an unrecognized half label"
            a = _value(halves["A"].results.get(algorithm_id), metric.metric_id) if "A" in halves else None
            b = _value(halves["B"].results.get(algorithm_id), metric.metric_id) if "B" in halves else None
            if split_status["A"]["state"] == "ok" and split_status["B"]["state"] == "ok":
                verdict = stable(spec, _side(a, 0.5), _side(b, 0.5))
                mean = (abs(a.value) + abs(b.value)) / 2
                split = {
                    "a": a.value,
                    "b": b.value,
                    "deviation_a": split_status["A"]["deviation"],
                    "deviation_b": split_status["B"]["deviation"],
                    "unit": split_status["A"]["unit"],
                    "relative_abs_diff": (abs(a.value - b.value) / mean) if mean else 0.0,
                    "stable": verdict,
                }
            # "usable to f" (D24): every measured fraction from full depth down
            # to f is ok and usable (interpretable and stable).
            usable_to = None
            for point in sorted(points, key=lambda p: p["fraction"], reverse=True):
                if point["fraction"] >= 1.0:
                    continue
                if point["state"] != "ok_usable":
                    break
                usable_to = point["fraction"]
            if usable_to is not None:
                verdict_text = f"usable to {usable_to:g}"
            elif not downs:
                verdict_text = "not measured below full depth"
            elif any(point["state"] == "ok_unstable" for point in points if point["fraction"] < 1.0):
                verdict_text = "unstable below full depth"
            else:
                verdict_text = "no usable measurement below full depth"
            panels.append(
                {
                    "algorithm_id": algorithm_id,
                    "metric_id": metric.metric_id,
                    "label": entry["label"] if entry else metric.metric_id,
                    "headline": bool(entry and entry.headline),
                    "full_value": metric.value,
                    "points": points,
                    "split": split,
                    "split_status": split_status,
                    "verdict": verdict_text,
                }
            )
    panels.sort(key=lambda panel: (not panel["headline"], order.index(panel["algorithm_id"])))
    return panels


def repeatability(panels: list[dict[str, Any]]) -> dict[str, Any] | None:
    """REPEATABILITY block: median relative split-half |A−B| and the three worst."""

    rows = [
        (panel["split"]["relative_abs_diff"], panel["label"], panel["split"]["stable"])
        for panel in panels
        if panel.get("split") is not None
    ]
    if not rows:
        return None
    values = sorted(row[0] for row in rows)
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    worst = sorted(rows, key=lambda row: row[0], reverse=True)[:3]
    return {
        "median_relative_abs_diff": median,
        "metrics": len(rows),
        "verdict_metrics": sum(1 for row in rows if row[2] is not None),
        "no_verdict_metrics": sum(1 for row in rows if row[2] is None),
        "unstable": sum(1 for row in rows if row[2] is False),
        "incomparable_metrics": sum(
            1 for panel in panels if panel.get("split") is None
            and any(status.get("state") != "not_recorded" for status in panel.get("split_status", {}).values())
        ),
        "worst": [{"label": label, "relative_abs_diff": diff, "stable": ok} for diff, label, ok in worst],
    }


def synthetic(sample: SampleView) -> list[dict[str, Any]]:
    """SYNTHETIC block: expected vs recovered TF per CNA spike (D25 a)."""

    from .controls import DETECTED_MIN, TF_TOLERANCE

    rows = []
    for view in sorted(sample.by_transform("cna"), key=lambda v: float(v.parameters.get("tf", 0))):
        envelope = view.results.get("copy-number")
        expected = float(view.parameters["tf"])
        recovered = None
        status = "not run" if envelope is None else envelope.status
        if envelope is not None and envelope.status == "ok":
            metric = _value(envelope, "tumor_fraction")
            recovered = metric.value if metric else None
        rows.append(
            {
                "tf_expected": expected,
                "tf_recovered": recovered,
                "status": status,
                "skip_reason": envelope.skip_reason if envelope is not None else None,
                "recovered": recovered is not None and recovered >= DETECTED_MIN and abs(recovered - expected) <= TF_TOLERANCE,
            }
        )
    return rows


def sample_ids(store: LabStore) -> list[str]:
    from .ingest import source_ids

    return source_ids(store)


__all__ = [
    "CELL_STATES",
    "InputView",
    "SampleView",
    "cell_state",
    "depth_curve",
    "load_sample",
    "repeatability",
    "sample_ids",
    "selected_results",
    "sex_check_view",
    "stability_panels",
    "synthetic",
]
