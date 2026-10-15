"""JSON views the Lab server returns (read-only; nothing is written).

Every id in a request is matched against a regex by the server and then
resolved through the store index; no request text becomes a path (D34).
``data_ref`` files are served only when their realpath lies inside the
result's own attempt directory and no component is a symlink.
"""

from __future__ import annotations

import os
import stat
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
                stats = (LIBRARY_FILE.stat(), LICENSES_FILE.stat())
                mtimes = tuple(
                    (st.st_mtime_ns, st.st_size, st.st_ino) for st in stats
                )
            except OSError:
                mtimes = ("unreadable",)
            if mtimes != self._mtimes:
                self._mtimes = mtimes
                try:
                    self.library = parse_library(LIBRARY_FILE.read_text(), LICENSES_FILE.read_text())
                    self.loaded_at = time.strftime("%H:%M", time.localtime())
                    self.error = None
                except LibraryError as exc:
                    self.error = str(exc)[:300]  # names the entry or YAML line, never a path
                    if self.library is None:
                        raise
                except OSError:
                    self.error = "the library file is unreadable"
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _read_contained(base: Path, parts: list[str], data_ref: str, *, limit: int = 64 * 1024 * 1024) -> bytes | None:
    """Read ``base/parts…/data_ref`` without following any symlink (D34).

    Every component below ``base`` (the store's results directory) is opened
    with ``O_NOFOLLOW`` relative to its parent's descriptor, so a symlinked
    attempt, artifact directory or file, or a swap after a check, cannot
    redirect the read.  The file must be a regular file with one link.
    """

    reference = PurePosixPath(data_ref)
    if reference.is_absolute() or ".." in reference.parts or not reference.parts:
        return None
    components = [*parts, *reference.parts]
    if any(part in ("", ".", "..") or "/" in part for part in components):
        return None
    try:
        fd = os.open(base, os.O_RDONLY | _DIRECTORY)
    except OSError:
        return None
    try:
        for part in components[:-1]:
            next_fd = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(components[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
    except OSError:
        os.close(fd)
        return None
    os.close(fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            return None
        chunks = []
        while chunk := os.read(file_fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _contained(attempt: Path, data_ref: str) -> Path | None:
    """Compatibility check used by tests: is ``data_ref`` readable without symlinks?"""

    return attempt / data_ref if _read_contained(attempt, [], data_ref) is not None else None


class LabApi:
    def __init__(self, store: LabStore) -> None:
        self.store = store
        self.libraries = LibraryHolder()

    # -- status (D22): two small reads, never the lock ---------------------------
    def status(self) -> dict[str, Any]:
        status = self.store.status()
        interrupted = None
        if status["active"] is None:
            # Bounded (D22): one pointer file and that one run's log, never a scan.
            last = self.store.last_run_id()
            if last is not None:
                summary = summarize_run(self.store.run_log(last).events(), self.store)
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
        curve = views.depth_curve(sample, library)
        synthetic = views.synthetic(sample)
        defended_block, headline_block = _defended(algorithms, curve, panels, synthetic, library, order, depth_x)
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
            "curve": curve,
            "stability": panels,
            "repeatability": views.repeatability(panels),
            "synthetic": synthetic,
            "defended": defended_block,
            "headline": headline_block,
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
        parts = [view.input_id, algorithm_id, envelope.provenance.parameters_sha256, "attempts", attempt.name]
        return _read_contained(self.store.results_dir, parts, panel.data_ref)


def _defended(
    algorithms: list[dict[str, Any]],
    curve: dict[str, Any],
    panels: list[dict[str, Any]],
    synthetic: list[dict[str, Any]],
    library: Library,
    order: list[str],
    depth_x: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from ..defended import Candidate, defended, headline

    rows = {row["algorithm_id"]: row for row in curve["rows"]}
    by_metric = {(panel["algorithm_id"], panel["metric_id"]): panel for panel in panels}
    spike = next((row for row in synthetic if abs(row["tf_expected"] - 0.10) < 1e-9), None)
    candidates = []
    usable = total = failed = 0
    min_depths: dict[str, float | None] = {}
    for algorithm in algorithms:
        if algorithm["hidden"]:
            continue
        total += 1
        failed += int(algorithm["status"] == "failed")
        entry = library.headline(algorithm["algorithm_id"])
        min_depths[algorithm["algorithm_id"]] = entry.get("min_depth_x") if entry else None
        row = rows.get(algorithm["algorithm_id"])
        if row and row["full_state"] == "ok_usable":
            usable += 1
        if entry is None:
            continue
        metric = next((m for m in algorithm["metrics"] if m["metric_id"] == entry.metric_id), None)
        half = next((c for c in (row or {}).get("cells", []) if c["fraction"] == 0.5), None)
        panel = by_metric.get((algorithm["algorithm_id"], entry.metric_id))
        split = panel.get("split") if panel else None
        candidates.append(
            Candidate(
                algorithm_id=algorithm["algorithm_id"],
                label=entry["label"],
                value=metric["value"] if metric else None,
                unit=metric["unit"] if metric else None,
                tier=entry.tier,
                status=algorithm["status"],
                state=metric["state"] if metric else None,
                usable_at_half=bool(half and half.get("state") == "ok_usable"),
                split_stable=split.get("stable") if split else None,
                split_relative_diff=split.get("relative_abs_diff") if split else None,
                citation_verified=entry.citation_verified,
                control_recovered=(spike["recovered"] if spike else False) if algorithm["algorithm_id"] == "copy-number" else None,
                order=order.index(algorithm["algorithm_id"]) if algorithm["algorithm_id"] in order else 99,
                license_ids=list(entry.get("license_ids") or []),
            )
        )
    block = defended(candidates)
    sentence = headline(
        depth_x, usable, total, len(block["rows"]), min_depths, downsampled=curve["downsampled"], failed=failed
    )
    return block, sentence


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
