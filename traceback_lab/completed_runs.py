"""Validated primary inputs of completed Lab runs, without taking the writer lock."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .file_access import open_regular_no_follow
from .store import LabStore, SelectionSnapshotChanged, canonical_json_bytes, write_atomic

_RUN = re.compile(r"r-\d{8}-\d{4}-[0-9a-f]{4,8}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")


def _read_json(path: Path, limit: int) -> Any:
    try:
        fd = open_regular_no_follow(path)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                return None
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                return None
        finally:
            os.close(fd)
        return json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None


def _completed(store: LabStore, run_id: str) -> dict[str, Any] | None:
    if not _RUN.fullmatch(run_id):
        return None
    path = store.runs_dir / f"{run_id}.jsonl"
    first = last = sample = None
    try:
        with os.fdopen(open_regular_no_follow(path), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            # Valid runs can exceed the status endpoint's 1 MiB / 4096-line
            # display cap. Validate every event while holding only one line.
            while line := stream.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
                    return None
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("run_id") != run_id:
                    return None
                if first is None:
                    first = event
                if event.get("event") == "sample":
                    sample = event
                last = event
            after = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                return None
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if first is None or first.get("event") != "run_start" or first.get("command") != "run":
        return None
    end = last
    if end.get("event") != "run_end" or (end.get("status"), end.get("exit_code")) not in (
        ("ok", 0), ("partial_failure", 8)
    ):
        return None
    utc = end.get("utc")
    if not isinstance(utc, str) or not _UTC.fullmatch(utc):
        return None
    if sample is None:
        return None
    source_id, input_id = sample.get("source_id"), sample.get("input_id")
    if not isinstance(source_id, str) or not _HASH.fullmatch(source_id):
        return None
    if not isinstance(input_id, str) or not _HASH.fullmatch(input_id):
        return None
    return {"run_id": run_id, "source_id": source_id, "input_id": input_id,
            "completed_utc": utc, "completed_mtime_ns": info.st_mtime_ns}


def _order(candidate: dict[str, Any]) -> tuple[str, int, str]:
    return candidate["completed_utc"], candidate["completed_mtime_ns"], candidate["run_id"]


def _advisory_pointer(value: Any) -> dict[str, Any] | None:
    """A recorded claim can signal uncertainty, never authorize a selection."""

    if not isinstance(value, dict):
        return None
    run_id, source_id, input_id, utc, mtime = (
        value.get(key) for key in (
            "run_id", "source_id", "input_id", "completed_utc", "completed_mtime_ns",
        )
    )
    if (not isinstance(run_id, str) or not _RUN.fullmatch(run_id)
            or not isinstance(source_id, str) or not _HASH.fullmatch(source_id)
            or not isinstance(input_id, str) or not _HASH.fullmatch(input_id)
            or not isinstance(utc, str) or not _UTC.fullmatch(utc)
            or not isinstance(mtime, int) or isinstance(mtime, bool) or mtime < 0):
        return None
    return {"run_id": run_id, "source_id": source_id, "input_id": input_id,
            "completed_utc": utc, "completed_mtime_ns": mtime}


def _unverified_claim(store: LabStore, claim: dict[str, Any],
                      selected: dict[str, Any] | None) -> bool:
    if selected is not None and (
        (claim["run_id"], claim["source_id"]) == (selected["run_id"], selected["source_id"])
        or _order(claim) <= _order(selected)
    ):
        return False
    # The writer may have published this terminal log after our scan. A direct
    # validation is evidence to retry the whole read, not to trust the pointer.
    validated = _completed(store, claim["run_id"])
    if validated is not None and all(validated[key] == claim[key] for key in (
        "run_id", "source_id", "input_id", "completed_utc",
    )):
        if selected is None or _order(validated) > _order(selected):
            raise SelectionSnapshotChanged("completed run appeared during read")
        return False  # the log is valid; only the pointer's file timestamp is stale
    return True


def completion_history_unavailable(
    store: LabStore, source_id: str, record: dict[str, Any],
    selected: dict[str, Any] | None,
) -> bool:
    """Fail closed if a newer recorded completion has no validated terminal log."""

    claims = (record.get("last_completed_run"), _read_json(store.lab / "last_completed_run.json", 4096))
    for raw in claims:
        claim = _advisory_pointer(raw)
        if claim is None or claim["source_id"] != source_id:
            continue
        if _unverified_claim(store, claim, selected):
            return True
    return False


def global_completion_history_unavailable(store: LabStore, selected: dict[str, Any] | None) -> bool:
    claim = _advisory_pointer(_read_json(store.lab / "last_completed_run.json", 4096))
    return claim is not None and _unverified_claim(store, claim, selected)


def completion_snapshot(store: LabStore) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """One bounded-per-log scan shared by all rows in a sample listing."""
    by_source: dict[str, dict[str, Any]] = {}
    latest = None
    try:
        with os.scandir(store.runs_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl") or not _RUN.fullmatch(entry.name[:-6]):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                candidate = _completed(store, entry.name[:-6])
                if candidate is None:
                    continue
                prior = by_source.get(candidate["source_id"])
                if prior is None or _order(candidate) > _order(prior):
                    by_source[candidate["source_id"]] = candidate
                if latest is None or _order(candidate) > _order(latest):
                    latest = candidate
    except OSError:
        pass
    return by_source, latest


def _scan(store: LabStore, source_id: str | None = None,
          baseline: dict[str, Any] | None = None) -> dict[str, Any] | None:
    by_source, latest = completion_snapshot(store)
    found = by_source.get(source_id) if source_id is not None else latest
    if found is None:
        return baseline
    return found if baseline is None or _order(found) >= _order(baseline) else baseline


def latest_completed(store: LabStore) -> dict[str, Any] | None:
    pointer = _read_json(store.lab / "last_completed_run.json", 4096)
    selected = None
    if isinstance(pointer, dict) and isinstance(pointer.get("run_id"), str):
        selected = _completed(store, pointer["run_id"])
        if selected is not None and any(pointer.get(key) != selected[key] for key in selected):
            selected = None
    # A writer can crash after run_end and before the pointer update. Reconcile
    # the append-only logs so a valid but stale pointer never hides that run.
    return _scan(store, baseline=selected)


def completed_for_source(store: LabStore, source_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    pointer = record.get("last_completed_run")
    selected = None
    if isinstance(pointer, dict) and isinstance(pointer.get("run_id"), str):
        selected = _completed(store, pointer["run_id"])
        if (selected is not None and (selected["source_id"] != source_id
                or any(pointer.get(key) != selected[key] for key in selected))):
            selected = None
    return _scan(store, source_id, selected)


def record_completion(store: LabStore, run_id: str, source_id: str, input_id: str) -> None:
    """Publish small completion indexes after run_end; logs remain the authority."""
    completed = _completed(store, run_id)
    if completed is None or completed["source_id"] != source_id or completed["input_id"] != input_id:
        return
    path = store.sources_dir / f"{source_id}.json"
    record = _read_json(path, 1024 * 1024)
    if isinstance(record, dict) and record.get("source_id") == source_id:
        record["last_completed_run"] = completed
        write_atomic(path, canonical_json_bytes(record))
    write_atomic(store.lab / "last_completed_run.json", canonical_json_bytes(completed))
    store.bump_generation()
