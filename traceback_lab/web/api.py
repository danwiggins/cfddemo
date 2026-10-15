"""JSON views the Lab server returns (read-only; nothing is written).

Every id in a request is matched against a regex by the server and then
resolved through the store index; no request text becomes a path (D34).
``data_ref`` files are served only when their realpath lies inside the
result's own attempt directory and no component is a symlink.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from ..identity import resolve_prefix, shortest_unique_prefixes
from ..ingest import source_ids, source_record
from ..interpretation.engine import (
    LIBRARY_FILE,
    LICENSES_FILE,
    Library,
    LibraryError,
    interpret_result,
    parse_library,
    qc_band,
)
from ..store import LabStore, summarize_run
from .. import views


class LibraryHolder:
    """The last good library; a broken edit on disk never blanks the page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mtimes: tuple[int, int] | None = None
        self.library: Library | None = None
        self.loaded_at: str | None = None
        self.error: str | None = None

    def get(self) -> Library:
        with self._lock:
            try:
                mtimes = (LIBRARY_FILE.stat().st_mtime_ns, LICENSES_FILE.stat().st_mtime_ns)
            except OSError:
                mtimes = self._mtimes or (0, 0)
            if mtimes != self._mtimes:
                self._mtimes = mtimes
                try:
                    self.library = parse_library(LIBRARY_FILE.read_text(), LICENSES_FILE.read_text())
                    self.loaded_at = time.strftime("%H:%M", time.localtime())
                    self.error = None
                except (LibraryError, OSError) as exc:
                    self.error = str(exc)[:300]
                    if self.library is None:
                        raise
            assert self.library is not None
            return self.library

    def health(self) -> dict[str, Any]:
        return {
            "version": self.library.version if self.library else None,
            "loaded_at": self.loaded_at,
            "error": self.error,
        }


def _contained(attempt: Path, data_ref: str) -> Path | None:
    reference = PurePosixPath(data_ref)
    if reference.is_absolute() or ".." in reference.parts or not reference.parts:
        return None
    current = attempt
    for part in reference.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        real = Path(os.path.realpath(current))
        base = Path(os.path.realpath(attempt))
    except OSError:
        return None
    if base not in real.parents or not real.is_file():
        return None
    return real


class LabApi:
    def __init__(self, store: LabStore) -> None:
        self.store = store
        self.libraries = LibraryHolder()

    # -- status (D22): two small reads, never the lock ---------------------------
    def status(self) -> dict[str, Any]:
        status = self.store.status()
        interrupted = None
        if status["active"] is None:
            run_ids = self.store.run_ids()
            if run_ids:
                summary = summarize_run(self.store.run_log(run_ids[0]).events(), self.store)
                if summary["status"] == "interrupted":
                    interrupted = {"run_id": summary["run_id"], "step": summary["open_step"]}
        try:
            self.libraries.get()
        except (LibraryError, OSError):
            pass
        return {**status, "interrupted": interrupted, "library": self.libraries.health()}

    # -- samples -------------------------------------------------------------------
    def _id12s(self) -> dict[str, str]:
        ids = source_ids(self.store)
        return shortest_unique_prefixes(ids) if ids else {}

    def samples(self) -> dict[str, Any]:
        library = self.libraries.get()
        rows = []
        for source_id, id12 in self._id12s().items():
            record = source_record(self.store, source_id) or {}
            sample = views.load_sample(self.store, source_id)
            if sample is None:
                continue
            used = max((entry.get("used_utc", "") for entry in record.get("inputs", [])), default="")
            counts = {"ok": 0, "skipped": 0, "failed": 0}
            outside = 0
            for envelope in sample.full.results.values():
                if envelope.algorithm_id in library.hidden_algorithms:
                    continue
                counts[envelope.status] = counts.get(envelope.status, 0) + 1
                for row in interpret_result(envelope, library, depth_x=sample.full.depth_x()):
                    outside += int(row.interpretation.out_of_range)
            spikes = views.synthetic(sample)
            rows.append(
                {
                    "id12": id12,
                    "run_utc": used,
                    "depth_x": sample.full.depth_x(),
                    "example": sample.example,
                    "counts": counts,
                    "outside_reference": outside,
                    "controls": {
                        "spike": None if not spikes else all(row["recovered"] for row in spikes if row["tf_expected"] >= 0.1),
                        "split": bool(sample.by_transform("split")),
                    },
                }
            )
        rows.sort(key=lambda row: row["run_utc"], reverse=True)
        return {"samples": rows, "root_has_runs": bool(self.store.run_ids())}

    def _resolve(self, id12: str) -> tuple[str | None, list[str]]:
        mapping = self._id12s()
        match = resolve_prefix(id12, mapping)
        if match.state == "found":
            return match.value, []
        return None, [mapping[value] for value in match.candidates]

    def sample(self, id12: str) -> dict[str, Any]:
        source_id, candidates = self._resolve(id12)
        if source_id is None:
            return {"found": False, "candidates": candidates}
        sample = views.load_sample(self.store, source_id)
        if sample is None:
            return {"found": False, "candidates": []}
        library = self.libraries.get()
        record = source_record(self.store, source_id) or {}
        depth_x = sample.full.depth_x()
        algorithms = []
        no_entry = 0
        unverified: set[tuple[str, str]] = set()
        used_licenses: list[str] = []
        for algorithm_id, envelope in sample.full.results.items():
            metrics = []
            for row in interpret_result(envelope, library, depth_x=depth_x):
                entry = row.entry
                if entry is None and algorithm_id not in library.hidden_algorithms:
                    no_entry += 1
                elif entry is not None and not entry.citation_verified and entry.get("citations"):
                    unverified.add((row.algorithm_id, row.metric_id))
                for license_id in (entry.get("license_ids") or []) if entry else []:
                    if license_id not in used_licenses:
                        used_licenses.append(license_id)
                metrics.append(_metric_payload(row, entry, qc_band(row.value, entry)))
            headline = library.headline(algorithm_id)
            algorithms.append(
                {
                    "algorithm_id": algorithm_id,
                    "hidden": algorithm_id in library.hidden_algorithms,
                    "status": envelope.status,
                    "skip_reason": envelope.skip_reason,
                    "skip_message": envelope.skip_message,
                    "failure": envelope.failure.model_dump() if envelope.failure else None,
                    "duration_s": envelope.duration_s,
                    "tier": headline.tier if headline else None,
                    "min_depth_x": headline.get("min_depth_x") if headline else None,
                    "headline_metric": headline.metric_id if headline else None,
                    "panels": [{"panel_id": panel.panel_id, "kind": panel.kind} for panel in envelope.panels],
                    "metrics": metrics,
                    "provenance": {
                        "tool": envelope.provenance.tool,
                        "tool_version": envelope.provenance.tool_version,
                        "parameters_sha256": envelope.provenance.parameters_sha256,
                        "input_id": envelope.provenance.input_id,
                        "source_id": envelope.provenance.source_id,
                        "reference_id": envelope.provenance.reference_id,
                        "timestamp_utc": envelope.provenance.timestamp_utc,
                    },
                }
            )
        order = [a.id for a in _registry()]
        algorithms.sort(key=lambda row: order.index(row["algorithm_id"]) if row["algorithm_id"] in order else 99)
        panels = views.stability_panels(sample, library)
        return {
            "found": True,
            "id12": self._id12s()[source_id],
            "source_id": source_id,
            "example": sample.example,
            "run_utc": max((entry.get("used_utc", "") for entry in record.get("inputs", [])), default=""),
            "depth": next((env.depth.model_dump() for env in sample.full.results.values() if env.depth), None),
            "excluded_chunks": record.get("excluded", []),
            "interpretation_version": library.version,
            "cohort_version": None,
            "cohort_n": 0,
            "algorithms": algorithms,
            "curve": views.depth_curve(sample, library),
            "stability": panels,
            "repeatability": views.repeatability(panels),
            "synthetic": views.synthetic(sample),
            "derived": [{"label": view.label, "transform": view.transform} for view in sample.derived],
            "licenses": [{"license_id": key, **library.licenses[key]} for key in used_licenses],
            "library_health": {
                **self.libraries.health(),
                "no_library_entry": no_entry,
                "unverified_citations": len(unverified),
            },
        }

    # -- artifacts (D34 containment) -------------------------------------------------
    def artifact(self, id12: str, label: str, algorithm_id: str, panel_id: str) -> bytes | None:
        source_id, _ = self._resolve(id12)
        if source_id is None:
            return None
        sample = views.load_sample(self.store, source_id)
        if sample is None:
            return None
        view = sample.full if label == "full" else next((v for v in sample.derived if v.label == label), None)
        if view is None:
            return None
        envelope = view.results.get(algorithm_id)
        if envelope is None or envelope.status != "ok":
            return None
        panel = next((p for p in envelope.panels if p.panel_id == panel_id), None)
        if panel is None:
            return None
        attempt = self.store.selected_attempt(view.input_id, algorithm_id, envelope.provenance.parameters_sha256)
        if attempt is None:
            return None
        path = _contained(attempt, panel.data_ref)
        if path is None:
            return None
        return path.read_bytes()


def _registry() -> Iterable[Any]:
    from ..registry import load_registry

    return load_registry()


def _metric_payload(row: Any, entry: Any, band: str | None) -> dict[str, Any]:
    interpretation = row.interpretation
    payload: dict[str, Any] = {
        "metric_id": row.metric_id,
        "label": row.label,
        "value": row.value,
        "unit": row.unit,
        "n": row.n,
        "state": interpretation.state,
        "reason": interpretation.reason,
        "caption": interpretation.caption,
        "reference_kind": interpretation.reference_kind,
        "exploratory_n": interpretation.exploratory_n,
        "percentile": interpretation.percentile,
        "interval": list(interpretation.interval) if interpretation.interval else None,
        "severity": interpretation.severity,
        "qc_band": band,
        "tier": row.tier,
        "entry": None,
    }
    if entry is not None:
        payload["entry"] = {
            key: entry.get(key)
            for key in (
                "origin_tier",
                "origin_platform",
                "min_depth_x",
                "reference",
                "performance",
                "performance_missing_reason",
                "meaning_high",
                "meaning_low",
                "confounders",
                "next_steps",
                "license_ids",
                "flags",
                "headline",
                "citations",
                "citation_checked",
            )
        }
    return payload


__all__ = ["LabApi", "LibraryHolder"]
