"""JSON views the Lab server returns (read-only; nothing is written).

Every id in a request is matched against a regex by the server and then
resolved through the store index; no request text becomes a path (D34).
``data_ref`` files are served only when their realpath lies inside the
result's own attempt directory and no component is a symlink.
"""

from __future__ import annotations

import os
import math
import shlex
import stat
import threading
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .. import views
from ..cohort_contracts import (
    CohortGeneration,
    CohortMember,
    MetricProjection,
    project_metric,
)
from ..cohorts import list_cohorts, member_from_store, read_current
from ..identity import resolve_prefix, shortest_unique_prefixes
from ..ingest import source_ids, source_record
from ..identity_pair import pairs_for_sample
from ..algorithms.identity import somalier_tool
from ..interpretation.engine import (
    COHORT_MIN,
    LIBRARY_FILE,
    LICENSES_FILE,
    Entry,
    Library,
    LibraryError,
    interpret_result,
    parse_library,
    qc_band,
)
from ..problems import LabProblem
from ..store import LabStore, summarize_run
from ..tf_concordance import report_tf_concordance


def _recovery_problem(problem: LabProblem) -> dict[str, str]:
    """Only operator-safe fields cross the local Compare API boundary."""

    return {"code": problem.code, "summary": problem.summary,
            "cause": problem.cause, "fix": problem.fix, "docs": problem.docs}


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

    def _published_sample_cohort(self) -> tuple[CohortGeneration | None, dict[str, str] | None]:
        """Read one immutable generation pointer for an entire API response."""

        try:
            return read_current(self.store, "healthy-v1"), None
        except LabProblem as problem:
            return None, _recovery_problem(problem)

    def _sample_cohort(
        self, source_id: str, sample: Any,
        snapshot: tuple[CohortGeneration | None, dict[str, str] | None] | None = None,
    ) -> tuple[CohortGeneration | None, CohortMember | None, str, dict[str, str] | None]:
        """Pin the default kit cohort and the same full input shown by the page.

        A broken cohort or stale target must not turn an unrelated published
        reference into a cohort result.  The metric then remains unreferenced.
        """

        if sample.example:
            return None, None, "example", None
        generation, problem = snapshot if snapshot is not None else self._published_sample_cohort()
        if problem is not None:
            return None, None, "cohort_unavailable", problem
        if generation is None:
            return None, None, "not_built", None
        try:
            target = member_from_store(self.store, source_id)
        except LabProblem as error:
            return generation, None, "target_unavailable", _recovery_problem(error)
        except ValueError:
            error = LabProblem("TBX-LAB-017", cause="sample has no readable current full input",
                               fix="Inspect Lab runs, restore the input, then reload the sample")
            return generation, None, "target_unavailable", _recovery_problem(error)
        if target.source_id != source_id or target.input_id != sample.full.input_id:
            error = LabProblem("TBX-LAB-017", cause="sample results changed while its cohort reference was loading",
                               fix="Reload the sample to pin its current full input")
            return generation, None, "stale_target", _recovery_problem(error)
        return generation, target, "ready", None

    @staticmethod
    def _interpret_sample_envelope(
        envelope: Any, library: Library, depth_x: float | None, source_id: str,
        generation: CohortGeneration | None, target: CohortMember | None,
        *, include_interval: bool = True,
    ) -> tuple[list[Any], dict[str, MetricProjection]]:
        """Apply D8 only to library-declared cohort metrics, using D31 keys."""

        if envelope.status != "ok" or generation is None or target is None:
            return interpret_result(envelope, library, depth_x=depth_x,
                                    include_interval=include_interval), {}
        target_result = next((row for row in target.results if row.algorithm_id == envelope.algorithm_id), None)
        if target_result is None or target_result.parameters_sha256 != envelope.provenance.parameters_sha256:
            return interpret_result(envelope, library, depth_x=depth_x,
                                    include_interval=include_interval), {}
        projections: dict[str, MetricProjection] = {}
        entries: dict[tuple[str, str], Entry] | None = None
        for metric in envelope.metrics:
            entry = library.entry(envelope.algorithm_id, metric.metric_id)
            if entry is None or entry["reference"]["kind"] not in ("internal_cohort", "provisional_external"):
                continue
            if entry["value_kind"] in ("vector", "categorical"):
                continue
            projection = project_metric(
                generation, algorithm_id=envelope.algorithm_id, metric_id=metric.metric_id,
                cohort_compat_key=target_result.cohort_compat_key, target_source_id=source_id,
            )
            projections[metric.metric_id] = projection
            if entry["reference"]["kind"] != generation.kind:
                if entries is None:
                    entries = dict(library.entries)
                entries[(envelope.algorithm_id, metric.metric_id)] = Entry(
                    envelope.algorithm_id, metric.metric_id,
                    {**entry.raw, "reference": {"kind": generation.kind}},
                )
        interpreted = interpret_result(
            envelope,
            library if entries is None else Library(
                version=library.version, entries=entries, licenses=library.licenses,
                hidden_algorithms=library.hidden_algorithms,
            ),
            depth_x=depth_x,
            cohorts={key: projection.interpretation_cohort() for key, projection in projections.items()},
            include_interval=include_interval,
        )
        return interpreted, projections

    def samples(self) -> dict[str, Any]:
        library = self.libraries.get()
        cohort_snapshot = self._published_sample_cohort()
        rows = []
        for source_id, id12 in self._id12s().items():
            record = source_record(self.store, source_id) or {}
            sample = views.load_sample(self.store, source_id)
            if sample is None:
                continue
            generation, target, cohort_state, cohort_problem = self._sample_cohort(
                source_id, sample, cohort_snapshot,
            )
            used = max((entry.get("used_utc", "") for entry in record.get("inputs", [])), default="")
            counts = {"ok": 0, "skipped": 0, "failed": 0}
            outside = 0
            for envelope in sample.full.results.values():
                if envelope.algorithm_id in library.hidden_algorithms:
                    continue
                counts[envelope.status] = counts.get(envelope.status, 0) + 1
                interpreted, _ = self._interpret_sample_envelope(
                    envelope, library, sample.full.depth_x(), source_id, generation, target,
                    include_interval=False,
                )
                for row in interpreted:
                    outside += int(row.interpretation.out_of_range)
            spikes = views.synthetic(sample)
            rows.append(
                {
                    "id12": id12,
                    "run_utc": used,
                    "depth_x": sample.full.depth_x(),
                    "example": sample.example,
                    "cohort_version": generation.version_id if generation else None,
                    "cohort_kind": generation.kind if generation else None,
                    "cohort_n": generation.percentile_n if generation else 0,
                    "cohort_min": COHORT_MIN,
                    "cohort_state": cohort_state,
                    "cohort_problem": cohort_problem,
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
        generation, target, cohort_state, cohort_problem = self._sample_cohort(source_id, sample)
        algorithms = []
        no_entry = 0
        unverified: set[tuple[str, str]] = set()
        used_licenses: list[str] = []
        for algorithm_id, envelope in sample.full.results.items():
            metrics = []
            cis = {m.metric_id: (m.ci, m.ci_level) for m in envelope.metrics}
            interpreted, projections = self._interpret_sample_envelope(
                envelope, library, depth_x, source_id, generation, target,
            )
            for row in interpreted:
                entry = row.entry
                if entry is None and algorithm_id not in library.hidden_algorithms:
                    no_entry += 1
                elif entry is not None and not entry.citation_verified and entry.get("citations"):
                    unverified.add((row.algorithm_id, row.metric_id))
                for license_id in (entry.get("license_ids") or []) if entry else []:
                    if license_id not in used_licenses:
                        used_licenses.append(license_id)
                payload = _metric_payload(row, entry, qc_band(row.value, entry))
                projection = projections.get(row.metric_id)
                payload["eligible_n"] = projection.percentile_n if projection else None
                payload["cohort_values"] = list(projection.values) if projection else []
                payload["cohort_excluded"] = dict(projection.exclusions) if projection else {}
                payload["cohort_mean"] = (
                    sum(projection.values) / projection.percentile_n
                    if projection and projection.percentile_n >= COHORT_MIN else None
                )
                stored = cis.get(row.metric_id)
                payload["ci"] = list(stored[0]) if stored and stored[0] else None
                payload["ci_level"] = stored[1] if stored else None
                metrics.append(payload)
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
        identity_tool_unavailable = ("identity" in sample.full.results and somalier_tool() is None)
        identity_pairs = [] if identity_tool_unavailable else pairs_for_sample(self.store, source_id)
        return {
            "found": True,
            "id12": self._id12s()[source_id],
            "source_id": source_id,
            "example": sample.example,
            "run_utc": max((entry.get("used_utc", "") for entry in record.get("inputs", [])), default=""),
            "depth": next((env.depth.model_dump() for env in sample.full.results.values() if env.depth), None),
            "excluded_chunks": record.get("excluded", []),
            "preanalytic_sex": views.sex_check_view(sample),
            "interpretation_version": library.version,
            "cohort_version": generation.version_id if generation else None,
            "cohort_kind": generation.kind if generation else None,
            "cohort_n": generation.percentile_n if generation else 0,
            "cohort_min": COHORT_MIN,
            "cohort_id": generation.cohort_id if generation else "healthy-v1",
            "cohort_state": cohort_state,
            "cohort_problem": cohort_problem,
            "algorithms": algorithms,
            "identity_pairs": identity_pairs,
            "identity_pairs_reason": (
                "Somalier is unavailable; reinstall the pinned tool and rerun identity before comparing."
                if identity_tool_unavailable else None
            ),
            "curve": curve,
            "stability": panels,
            "repeatability": views.repeatability(panels),
            "synthetic": synthetic,
            "concordance": report_tf_concordance(sample, generation, target),
            "defended": defended_block,
            "headline": headline_block,
            "derived": [{"label": view.label, "transform": view.transform} for view in sample.derived],
            "licenses": [{"license_id": key, **library.licenses[key]} for key in _all_used(used_licenses, library, order)],
            "catalog": _catalog(library, order, algorithms),
            "excluded": EXCLUDED,
            "library_health": {
                **self.libraries.health(),
                "no_library_entry": no_entry,
                "unverified_citations": len(unverified),
            },
        }

    # -- Compare (L1.6): one pinned immutable cohort generation ----------------------
    def cohorts(self) -> dict[str, Any]:
        rows = []
        unavailable = []
        try:
            listings = list_cohorts(self.store)
        except LabProblem as problem:
            return {"cohorts": [], "unavailable": [], "problem": _recovery_problem(problem)}
        for listing in listings:
            if listing.problem is not None:
                unavailable.append({"cohort_id": listing.cohort_id,
                                    "problem": _recovery_problem(listing.problem)})
                continue
            generation = listing.generation
            if generation is not None:
                rows.append({
                    "cohort_id": listing.cohort_id,
                    "cohort_version": generation.version_id,
                    "kind": generation.kind,
                    "member_n": generation.percentile_n,
                    "pon_n": generation.pon_n,
                })
        return {"cohorts": rows, "unavailable": unavailable}

    def compare(self, id12: str, cohort_id: str) -> dict[str, Any]:
        source_id, candidates = self._resolve(id12)
        if source_id is None:
            return {"found": False, "candidates": candidates}
        sample = views.load_sample(self.store, source_id)
        if sample is None:
            problem = LabProblem(
                "TBX-LAB-017", cause=f"sample {id12} has no readable current full input",
                fix="Inspect `traceback lab runs`, restore the source record if needed, then rerun the sample",
            )
            return {"found": True, "id12": id12, "cohort_id": cohort_id,
                    "state": "target_unavailable", "metrics": [], "unscored": [],
                    "problem": _recovery_problem(problem)}
        try:
            generation = read_current(self.store, cohort_id)
        except LabProblem as problem:
            return {"found": True, "id12": id12, "cohort_id": cohort_id,
                    "state": "cohort_unavailable", "metrics": [], "unscored": [],
                    "problem": _recovery_problem(problem)}
        if generation is None:
            problem = LabProblem(
                "TBX-LAB-017", cause=f"cohort {cohort_id} has no published generation",
                fix="Run `" + shlex.join(["traceback", "lab", "cohort", "build", "--cohort", cohort_id,
                                          "--root", str(self.store.root)]) + "`, then reload Compare",
            )
            return {"found": True, "id12": id12, "cohort_id": cohort_id,
                    "state": "cohort_unavailable", "metrics": [], "unscored": [],
                    "problem": _recovery_problem(problem)}
        base = {
            "found": True,
            "id12": self._id12s()[source_id],
            "cohort_id": cohort_id,
            "cohort_version": generation.version_id,
            "kind": generation.kind,
            "member_n": generation.percentile_n,
            "pon_n": generation.pon_n,
            "store_generation": self.store.generation(),
        }
        # EXAMPLE cannot enter the real kit cohort, and its invented biological
        # values must not be presented as a patient-vs-cohort comparison.
        if sample.example:
            return {**base, "state": "example_unscorable", "metrics": [], "unscored": []}
        try:
            target = member_from_store(self.store, source_id)
        except LabProblem as problem:
            if problem.code == "TBX-LAB-021":
                return {**base, "state": "example_unscorable", "metrics": [], "unscored": []}
            return {**base, "state": "target_unavailable", "metrics": [], "unscored": [],
                    "problem": _recovery_problem(problem)}
        except ValueError as exc:
            if str(exc) != "admitted source has no full aligned input":
                raise
            problem = LabProblem(
                "TBX-LAB-017", cause=f"sample {id12} has no stored full aligned input",
                fix="Inspect `traceback lab runs`, restore the sample's full input if needed, "
                    "then rerun from its original input and reload Compare",
            )
            return {**base, "state": "target_unavailable", "metrics": [], "unscored": [],
                    "problem": _recovery_problem(problem)}
        # Both reads must describe the same full input. A newer run may become
        # current between load_sample and member_from_store while retaining the
        # same algorithm parameter hash; comparing its compatibility key with
        # the older envelope would mix two sample snapshots.
        if target.source_id != source_id or target.input_id != sample.full.input_id:
            return {**base, "state": "stale_target", "metrics": [], "unscored": []}
        current = {result.algorithm_id: result for result in target.results}
        library = self.libraries.get()
        rows = []
        unscored = []
        for algorithm_id, envelope in sample.full.results.items():
            if algorithm_id in library.hidden_algorithms:
                continue
            if envelope.status != "ok":
                unscored.append({"algorithm_id": algorithm_id, "status": envelope.status,
                                 "reason": envelope.skip_reason or "failed"})
                continue
            scalar_metrics = []
            for metric in envelope.metrics:
                entry = library.entry(algorithm_id, metric.metric_id)
                if entry is not None and entry["value_kind"] in ("vector", "categorical"):
                    unscored.append({"algorithm_id": algorithm_id, "status": "non_scalar",
                                     "reason": metric.metric_id})
                    continue
                if metric.value is None or (isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool)):
                    scalar_metrics.append(metric)
            if not scalar_metrics:
                continue
            target_result = current.get(algorithm_id)
            if target_result is None or target_result.parameters_sha256 != envelope.provenance.parameters_sha256:
                unscored.append({"algorithm_id": algorithm_id, "status": "incompatible",
                                 "reason": "current target result unavailable"})
                continue
            projections = {}
            comparison_entries = dict(library.entries)
            for metric in scalar_metrics:
                entry = library.entry(algorithm_id, metric.metric_id)
                if entry is None or entry["value_kind"] in ("vector", "categorical"):
                    continue
                projection = project_metric(
                    generation, algorithm_id=algorithm_id, metric_id=metric.metric_id,
                    cohort_compat_key=target_result.cohort_compat_key, target_source_id=source_id,
                )
                projections[metric.metric_id] = projection
                # D8 still classifies the value. This copy applies only to the
                # selected Compare response; the library and normal sample
                # interpretation retain their original reference policy.
                comparison_entries[(algorithm_id, metric.metric_id)] = Entry(
                    algorithm_id, metric.metric_id,
                    {**entry.raw, "reference": {"kind": generation.kind}},
                )
            comparison_library = Library(
                version=library.version, entries=comparison_entries,
                licenses=library.licenses, hidden_algorithms=library.hidden_algorithms,
            )
            interpreted = interpret_result(
                envelope, comparison_library, depth_x=sample.full.depth_x(),
                cohorts={metric_id: projection.interpretation_cohort()
                         for metric_id, projection in projections.items()},
            )
            for row in interpreted:
                if row.metric_id not in {metric.metric_id for metric in scalar_metrics}:
                    continue
                projection = projections.get(row.metric_id)
                original_entry = library.entry(algorithm_id, row.metric_id)
                payload = _metric_payload(row, original_entry, qc_band(row.value, original_entry))
                payload.update({
                    "algorithm_id": algorithm_id,
                    "eligible_n": projection.percentile_n if projection else None,
                    "cohort_values": list(projection.values) if projection else [],
                    "excluded": dict(projection.exclusions) if projection else {},
                    "aligner_counts": dict(projection.aligner_counts) if projection else {},
                    "original_reference": original_entry.get("reference") if original_entry else None,
                    "comparison_context": "selected_cohort" if projection else "no_library_entry",
                })
                rows.append(payload)
        aligners: dict[str, int] = {}
        for member in generation.members:
            aligners[member.aligner_identity] = aligners.get(member.aligner_identity, 0) + 1
        return {**base, "state": "ready", "target_aligner": target.aligner_identity,
                "aligner_counts": dict(sorted(aligners.items())),
                "metrics": rows, "unscored": unscored}

    def compare_samples(self, id12_a: str, id12_b: str) -> dict[str, Any]:
        """Compare two distinct current full-input snapshots, B minus A.

        This is cross-sectional: source IDs identify samples, not visits or
        donors.  Each metric needs both exact selected executions and the same
        D31 compatibility key; no numeric threshold is inferred.
        """

        mapping = self._id12s()
        a_match = resolve_prefix(id12_a, mapping)
        b_match = resolve_prefix(id12_b, mapping)
        if a_match.state != "found" or b_match.state != "found":
            return {"found": False, "which": "a" if a_match.state != "found" else "b",
                    "candidates": [mapping[key] for key in (a_match.candidates if a_match.state != "found" else b_match.candidates)]}
        source_a, source_b = a_match.value, b_match.value
        base = {"found": True, "id12_a": mapping[source_a], "id12_b": mapping[source_b],
                "source_id_a": source_a, "source_id_b": source_b}
        if source_a == source_b:
            return {**base, "state": "same_sample", "metrics": [], "unscored": []}
        samples = [views.load_sample(self.store, source) for source in (source_a, source_b)]
        if any(sample is None for sample in samples):
            return {**base, "state": "sample_unavailable", "which": "a" if samples[0] is None else "b",
                    "metrics": [], "unscored": []}
        sample_a, sample_b = samples
        if sample_a.example or sample_b.example:
            return {**base, "state": "example_unscorable", "metrics": [], "unscored": []}
        try:
            selected_a = member_from_store(self.store, source_a)
        except (LabProblem, ValueError):
            return {**base, "state": "sample_unavailable", "which": "a", "metrics": [], "unscored": []}
        try:
            selected_b = member_from_store(self.store, source_b)
        except (LabProblem, ValueError):
            return {**base, "state": "sample_unavailable", "which": "b", "metrics": [], "unscored": []}
        for source, sample, selected, which in (
            (source_a, sample_a, selected_a, "a"), (source_b, sample_b, selected_b, "b"),
        ):
            if selected.source_id != source or selected.input_id != sample.full.input_id:
                return {**base, "state": "stale_sample", "which": which, "metrics": [], "unscored": []}
            current = {result.algorithm_id: result for result in selected.results}
            for algorithm_id, envelope in sample.full.results.items():
                if envelope.status != "ok":
                    continue
                pinned = current.get(algorithm_id)
                if pinned is None or pinned.parameters_sha256 != envelope.provenance.parameters_sha256:
                    return {**base, "state": "stale_sample", "which": which, "metrics": [], "unscored": []}
                for metric in envelope.metrics:
                    value = metric.value
                    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
                        if metric.metric_id not in pinned.metrics or pinned.metrics[metric.metric_id] != value:
                            return {**base, "state": "stale_sample", "which": which, "metrics": [], "unscored": []}
        library = self.libraries.get()
        selected_results_a = {result.algorithm_id: result for result in selected_a.results}
        selected_results_b = {result.algorithm_id: result for result in selected_b.results}
        rows: list[dict[str, Any]] = []
        unscored: list[dict[str, str]] = []
        for algorithm_id, envelope_a in sample_a.full.results.items():
            if algorithm_id in library.hidden_algorithms:
                continue
            envelope_b = sample_b.full.results.get(algorithm_id)
            if envelope_b is None or envelope_a.status != "ok" or envelope_b.status != "ok":
                unscored.append({"algorithm_id": algorithm_id, "metric_id": "*", "reason": "missing_or_not_ok"})
                continue
            result_a, result_b = selected_results_a[algorithm_id], selected_results_b[algorithm_id]
            if result_a.cohort_compat_key != result_b.cohort_compat_key:
                unscored.append({"algorithm_id": algorithm_id, "metric_id": "*", "reason": "incompatible_protocol"})
                continue
            metrics_a = {metric.metric_id: metric for metric in envelope_a.metrics}
            metrics_b = {metric.metric_id: metric for metric in envelope_b.metrics}
            interpreted_a = {row.metric_id: row for row in interpret_result(envelope_a, library, depth_x=sample_a.full.depth_x(), include_interval=False)}
            interpreted_b = {row.metric_id: row for row in interpret_result(envelope_b, library, depth_x=sample_b.full.depth_x(), include_interval=False)}
            for metric_id in sorted(metrics_a.keys() | metrics_b.keys()):
                metric_a, metric_b = metrics_a.get(metric_id), metrics_b.get(metric_id)
                entry = library.entry(algorithm_id, metric_id)
                if metric_a is None or metric_b is None:
                    reason = "missing_metric"
                elif entry is not None and entry["value_kind"] in ("vector", "categorical"):
                    reason = "non_scalar"
                elif any(not isinstance(metric.value, (int, float)) or isinstance(metric.value, bool)
                         or not math.isfinite(metric.value) for metric in (metric_a, metric_b)):
                    reason = "missing_or_non_numeric"
                elif metric_a.unit != metric_b.unit:
                    reason = "unit_mismatch"
                elif interpreted_a[metric_id].interpretation.state == "not_interpretable" or interpreted_b[metric_id].interpretation.state == "not_interpretable":
                    reason = "not_interpretable"
                else:
                    reason = None
                if reason is not None:
                    unscored.append({"algorithm_id": algorithm_id, "metric_id": metric_id, "reason": reason})
                    continue
                rows.append({
                    "algorithm_id": algorithm_id, "metric_id": metric_id,
                    "label": entry["label"] if entry is not None else metric_id,
                    "unit": metric_a.unit, "value_a": metric_a.value, "value_b": metric_b.value,
                    "delta_b_minus_a": metric_b.value - metric_a.value,
                    "parameters_sha256_a": result_a.parameters_sha256,
                    "parameters_sha256_b": result_b.parameters_sha256,
                })
        for algorithm_id in sample_b.full.results.keys() - sample_a.full.results.keys():
            if algorithm_id not in library.hidden_algorithms:
                unscored.append({"algorithm_id": algorithm_id, "metric_id": "*", "reason": "missing_or_not_ok"})
        return {**base, "state": "ready", "input_id_a": selected_a.input_id,
                "input_id_b": selected_b.input_id, "aligner_a": selected_a.aligner_identity,
                "aligner_b": selected_b.aligner_identity, "metrics": rows, "unscored": unscored}

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


EXCLUDED = [
    {"id": "jagged-ends", "reason": "needs Jag-seq library preparation; not computable from a standard ONT BAM"},
    {"id": "low-vaf-snv", "reason": "needs a matched tumour sample; not computable from one plasma BAM"},
]


def _all_used(used: list[str], library: Library, order: list[str]) -> list[str]:
    """Licenses of every registered algorithm's entries (Evidence lists them all)."""

    out = list(used)
    for (algorithm_id, _), entry in library.entries.items():
        if algorithm_id in order:
            for license_id in entry.get("license_ids") or []:
                if license_id not in out:
                    out.append(license_id)
    return out


def _catalog(library: Library, order: list[str], algorithms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every registered (non-hidden) algorithm with its tier, footnote and status here."""

    by_id = {row["algorithm_id"]: row for row in algorithms}
    rows = []
    for algorithm_id in order:
        if algorithm_id in library.hidden_algorithms:
            continue
        entry = library.headline(algorithm_id)
        licenses: list[str] = []
        for (alg, _), item in library.entries.items():
            if alg == algorithm_id:
                for license_id in item.get("license_ids") or []:
                    if license_id not in licenses:
                        licenses.append(license_id)
        performance = entry.get("performance") if entry else None
        result = by_id.get(algorithm_id)
        rows.append(
            {
                "algorithm_id": algorithm_id,
                "label": entry["label"] if entry else algorithm_id,
                "tier": entry.tier if entry else None,
                "origin_tier": entry.get("origin_tier") if entry else None,
                "origin_platform": entry.get("origin_platform") if entry else None,
                "footnote": (performance or {}).get("value") if performance else (entry.get("performance_missing_reason") if entry else None),
                "footnote_source": (performance or {}).get("source") if performance else None,
                "min_depth_x": entry.get("min_depth_x") if entry else None,
                "license_ids": licenses,
                "citation_checked": bool(entry and entry.citation_verified),
                "status": result["status"] if result else "not run",
                "skip_reason": result["skip_reason"] if result else None,
            }
        )
    return rows


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
