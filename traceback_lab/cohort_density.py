"""Immutable size-density snapshots and descriptive, leave-one-out cohort bands."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from .cohort_contracts import CohortGeneration, CohortMember, CohortResult, SizeDensityVector
from .problems import LabProblem
from .store import LabStore

_HISTOGRAM = "artifacts/size_hist.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _invalid_density(source_id: str, detail: str) -> LabProblem:
    return LabProblem(
        "TBX-LAB-020", cause=f"fragment-size density for {source_id[:12]} is {detail}",
        fix="Restore the selected fragment-size result or rerun it before publishing this cohort",
        data={"source_id": source_id, "field": "size-density"},
    )


def _read_attempt_file(base: Path, parts: tuple[str, ...], source_id: str) -> bytes:
    """Read below results_dir without traversing symlinks, with a 1 MiB cap."""

    if any(part in ("", ".", "..") or "/" in part or "\\" in part for part in parts):
        raise _invalid_density(source_id, "outside its selected attempt")
    try:
        fd = os.open(base, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise _invalid_density(source_id, "not a bounded regular artifact")
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = -1
                return stream.read(1024 * 1024 + 1)
        finally:
            if file_fd != -1:
                os.close(file_fd)
    except OSError as exc:
        raise _invalid_density(source_id, "unreadable or unsafe") from exc


def _snapshot_vector(store: LabStore, member: CohortMember, result: CohortResult) -> SizeDensityVector | None:
    envelope = store.read_result(member.input_id, result.algorithm_id, result.parameters_sha256)
    if envelope is None or envelope.status != "ok":
        raise _invalid_density(member.source_id, "missing its selected histogram")
    panels = [panel for panel in envelope.panels if panel.panel_id == "size-density"]
    if not panels:
        return None  # A valid scalar-only legacy result remains publishable.
    if len(panels) != 1 or panels[0].data_ref != _HISTOGRAM:
        raise _invalid_density(member.source_id, "pointing outside its expected histogram")
    selected = store.selected_attempt(member.input_id, result.algorithm_id, result.parameters_sha256)
    if selected is None:
        raise _invalid_density(member.source_id, "missing its selected histogram")
    try:
        parts = (*selected.relative_to(store.results_dir).parts, "artifacts", "size_hist.json")
    except ValueError as exc:
        raise _invalid_density(member.source_id, "outside the results store") from exc
    content = _read_attempt_file(store.results_dir, parts, member.source_id)
    try:
        data = json.loads(content)
        if (not isinstance(data, dict) or data.get("span_bp") != list(range(1, 1001)) or
                not isinstance(data.get("count"), list) or len(data["count"]) != 1000 or
                any(type(value) is not int or value < 0 for value in data["count"])):
            raise ValueError("histogram dimensions or counts invalid")
        return SizeDensityVector(
            attempt_id=selected.name, artifact_sha256=hashlib.sha256(content).hexdigest(),
            count=tuple(data["count"]),
        )
    except (ValueError, TypeError) as exc:
        raise _invalid_density(member.source_id, "malformed or without eligible 50–1000 bp counts") from exc


def capture_density_vectors(store: LabStore, members: tuple[CohortMember, ...]) -> tuple[CohortMember, ...]:
    """Copy selected histograms into the generation before its pointer is published."""

    captured = []
    for member in members:
        results = []
        for result in member.results:
            vector = _snapshot_vector(store, member, result) if result.algorithm_id == "fragment-size" else None
            results.append(result.model_copy(update={"size_density": vector}) if vector is not None else result)
        captured.append(member.model_copy(update={"results": tuple(results)}))
    return tuple(captured)


def project_density_band(
    generation: CohortGeneration | None, target: CohortMember | None, source_id: str,
    selected_parameters_sha256: str | None, cohort_state: str,
) -> dict[str, Any]:
    """Observed per-bin min–max; no threshold, percentile or diagnosis is derived."""

    row: dict[str, Any] = {
        "state": cohort_state if cohort_state != "ready" else "target_unavailable",
        "cohort_version": generation.version_id if generation else None,
        "eligible_n": 0, "excluded": {}, "span_bp": [], "low": [], "high": [],
    }
    if generation is None or target is None or cohort_state != "ready":
        return row
    target_result = next((result for result in target.results if result.algorithm_id == "fragment-size"), None)
    if target_result is None or target_result.parameters_sha256 != selected_parameters_sha256:
        return row
    vectors: list[tuple[float, ...]] = []
    excluded: Counter[str] = Counter()
    for member in generation.members:
        if member.source_id == source_id:
            excluded["self"] += 1
            continue
        result = next((item for item in member.results if item.algorithm_id == "fragment-size"), None)
        if result is None:
            excluded["missing_result"] += 1
        elif result.cohort_compat_key != target_result.cohort_compat_key:
            excluded["incompatible"] += 1
        elif result.size_density is None:
            excluded["missing_pinned_vector"] += 1
        else:
            vector = result.size_density
            vectors.append(tuple(count / vector.denominator for count in vector.count[49:700]))
    row["eligible_n"] = len(vectors)
    row["excluded"] = dict(sorted(excluded.items()))
    if not vectors:
        row["state"] = "old_generation_no_vectors" if excluded["missing_pinned_vector"] else "no_eligible_vectors"
    elif len(vectors) < 10:
        row["state"] = "cohort_too_small"
    else:
        row.update({
            "state": "ready", "span_bp": list(range(50, 701)),
            "low": [min(values) for values in zip(*vectors, strict=True)],
            "high": [max(values) for values in zip(*vectors, strict=True)],
        })
    return row


__all__ = ["capture_density_vectors", "project_density_band"]
