"""The landing selection follows terminal runs, not ingest timestamps."""

from __future__ import annotations

import json
import os

import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab import completed_runs
from traceback_lab.completed_runs import record_completion
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer


def _source(store: LabStore, source: str, *inputs: tuple[str, str]) -> None:
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source}.json").write_text(json.dumps({
        "source_id": source, "example": True,
        "inputs": [{"input_id": value, "used_utc": used} for value, used in inputs],
    }))
    for value, _ in inputs:
        manifest = store.inputs_dir / value / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"input_id": value}))


def _run(store: LabStore, run_id: str, source: str, input_id: str, utc: str,
         status: str | None = "ok") -> None:
    log = store.run_log(run_id)
    log.append("run_start", command="run", utc=utc)
    log.append("sample", source_id=source, input_id=input_id, utc=utc)
    if status is not None:
        log.append("run_end", status=status, exit_code={
            "ok": 0, "partial_failure": 8, "blocked": 3, "interrupted": 6,
        }[status], utc=utc)


def test_latest_completed_primary_survives_newer_active_and_blocked_runs(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source_a, source_b = "a" * 64, "b" * 64
    old_a, new_a, input_b = "1" * 64, "2" * 64, "3" * 64
    _source(store, source_a, (old_a, "2026-10-17T10:00:00Z"),
            (new_a, "2026-10-18T11:00:00Z"))
    _source(store, source_b, (input_b, "2026-10-18T12:00:00Z"))
    first = "r-20261017-1000-abcd"
    _run(store, first, source_a, old_a, "2026-10-17T10:00:00Z")
    prior_generation = store.generation()
    record_completion(store, first, source_a, old_a)
    assert store.generation() > prior_generation
    _run(store, "r-20261018-1100-abcd", source_a, new_a, "2026-10-18T11:00:00Z", None)
    _run(store, "r-20261018-1200-abcd", source_b, input_b, "2026-10-18T12:00:00Z", "blocked")

    api = LabApi(store)
    listing = api.samples()
    assert listing["samples"][0]["id12"] == source_b[:12]
    assert listing["default_sample_id12"] == source_a[:12]
    assert next(row for row in listing["samples"] if row["id12"] == source_a[:12])["run_utc"] == "2026-10-17T10:00:00Z"
    assert api.sample(source_a[:12])["full_input_id"] == old_a
    assert api.sample(source_a[:12])["run_utc"] == "2026-10-17T10:00:00Z"
    assert api.sample(source_b[:12])["full_input_id"] == input_b  # explicit active fallback

    second = "r-20261018-1300-abcd"
    _run(store, second, source_a, new_a, "2026-10-18T13:00:00Z")
    # Simulate a crash after run_end but before either completion pointer update.
    assert api.sample(source_a[:12])["full_input_id"] == new_a
    prior_generation = store.generation()
    record_completion(store, second, source_a, new_a)
    assert store.generation() > prior_generation
    assert api.sample(source_a[:12])["full_input_id"] == new_a
    assert api.samples()["default_sample_id12"] == source_a[:12]

    partial = "r-20261018-1400-abcd"
    _run(store, partial, source_b, input_b, "2026-10-18T14:00:00Z", "partial_failure")
    assert api.samples()["default_sample_id12"] == source_b[:12]
    record_completion(store, partial, source_b, input_b)
    assert api.samples()["default_sample_id12"] == source_b[:12]


def test_no_completed_run_has_no_default_but_active_sample_is_available(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, input_id = "c" * 64, "4" * 64
    _source(store, source, (input_id, "2026-10-18T10:00:00Z"))
    _run(store, "r-20261018-1000-abcd", source, input_id, "2026-10-18T10:00:00Z", "interrupted")
    api = LabApi(store)
    assert api.samples()["default_sample_id12"] is None
    assert api.sample(source[:12])["full_input_id"] == input_id


def test_legacy_completed_run_is_found_after_more_than_64_failed_runs(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, input_id = "d" * 64, "5" * 64
    _source(store, source, (input_id, "2026-10-17T10:00:00Z"))
    _run(store, "r-20261017-1000-abcd", source, input_id, "2026-10-17T10:00:00Z")
    for minute in range(70):
        _run(store, f"r-20261018-{minute:04d}-abcd", source, input_id,
             f"2026-10-18T01:{minute % 60:02d}:00Z", "blocked")
    assert LabApi(store).samples()["default_sample_id12"] == source[:12]


def test_same_second_completion_uses_terminal_file_order(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source_a, source_b = "e" * 64, "f" * 64
    input_a, input_b = "6" * 64, "7" * 64
    _source(store, source_a, (input_a, "2026-10-18T10:00:00Z"))
    _source(store, source_b, (input_b, "2026-10-18T10:00:00Z"))
    first, second = "r-20261018-1000-ffff", "r-20261018-1000-aaaa"
    _run(store, first, source_a, input_a, "2026-10-18T10:00:00Z")
    _run(store, second, source_b, input_b, "2026-10-18T10:00:00Z")
    os.utime(store.runs_dir / f"{first}.jsonl", ns=(1, 1))
    os.utime(store.runs_dir / f"{second}.jsonl", ns=(2, 2))
    assert LabApi(store).samples()["default_sample_id12"] == source_b[:12]


def test_missing_completed_input_is_not_replaced_by_active_rerun(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, old, active = "a" * 64, "8" * 64, "9" * 64
    _source(store, source, (old, "2026-10-17T10:00:00Z"),
            (active, "2026-10-18T10:00:00Z"))
    _run(store, "r-20261017-1000-abcd", source, old, "2026-10-17T10:00:00Z")
    (store.inputs_dir / old / "manifest.json").unlink()
    api = LabApi(store)
    assert api.sample(source[:12])["state"] == "completed_input_unavailable"
    assert api.samples()["default_sample_id12"] is None
    assert api.samples()["completed_input_unavailable"] is True
    assert api.samples()["samples"][0]["state"] == "completed_input_unavailable"


@needs_chrome
def test_older_missing_completed_input_stays_listed_and_explicit_route_names_problem(tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    unavailable, latest = "a" * 64, "b" * 64
    old, active, current = "8" * 64, "9" * 64, "7" * 64
    _source(store, unavailable, (old, "2026-10-17T10:00:00Z"),
            (active, "2026-10-18T10:00:00Z"))
    _source(store, latest, (current, "2026-10-18T11:00:00Z"))
    _run(store, "r-20261017-1000-abcd", unavailable, old, "2026-10-17T10:00:00Z")
    _run(store, "r-20261018-1100-abcd", latest, current, "2026-10-18T11:00:00Z")
    (store.inputs_dir / old / "manifest.json").unlink()
    api = LabApi(store)
    listing = api.samples()
    assert listing["default_sample_id12"] == latest[:12]
    assert next(row for row in listing["samples"] if row["id12"] == unavailable[:12])["state"] == "completed_input_unavailable"
    assert api.sample(unavailable[:12])["state"] == "completed_input_unavailable"
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/?sample={unavailable[:12]}",
                   want=lambda page: "Completed input unavailable" in page)
    assert f"Sample {unavailable[:12]} exists" in dom
    assert "Unknown sample" not in dom


def test_failed_completion_pointer_write_does_not_promote_an_active_run(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    completed_source, active_source = "a" * 64, "b" * 64
    completed_input, active_input = "8" * 64, "9" * 64
    _source(store, completed_source, (completed_input, "2026-10-17T10:00:00Z"))
    _source(store, active_source, (active_input, "2026-10-18T10:00:00Z"))
    run_id = "r-20261017-1000-abcd"
    _run(store, run_id, completed_source, completed_input, "2026-10-17T10:00:00Z")
    real_write = completed_runs.write_atomic

    def fail_global_pointer(path, content):
        if path.name == "last_completed_run.json":
            raise OSError("simulated pointer write failure")
        real_write(path, content)

    monkeypatch.setattr(completed_runs, "write_atomic", fail_global_pointer)
    with pytest.raises(OSError, match="pointer write failure"):
        record_completion(store, run_id, completed_source, completed_input)
    _run(store, "r-20261018-1000-abcd", active_source, active_input,
         "2026-10-18T10:00:00Z", None)
    assert LabApi(store).samples()["default_sample_id12"] == completed_source[:12]


def test_supported_large_run_log_can_be_the_completed_default(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, input_id = "c" * 64, "7" * 64
    _source(store, source, (input_id, "2026-10-18T10:00:00Z"))
    run_id = "r-20261018-1000-abcd"
    common = {"run_id": run_id, "utc": "2026-10-18T10:00:00Z"}
    events = [
        {**common, "event": "run_start", "command": "run"},
        {**common, "event": "sample", "source_id": source, "input_id": input_id},
        *({**common, "event": "step_end", "step": "derived", "status": "ok", "padding": "x" * 256}
          for _ in range(4200)),
        {**common, "event": "run_end", "status": "ok", "exit_code": 0},
    ]
    raw = b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events)
    assert len(events) > 4096 and len(raw) > 1024 * 1024
    store.runs_dir.mkdir(parents=True)
    (store.runs_dir / f"{run_id}.jsonl").write_bytes(raw)
    assert LabApi(store).samples()["default_sample_id12"] == source[:12]
    record_completion(store, run_id, source, input_id)


@pytest.mark.parametrize("damage", ("corrupt_middle", "removed"))
def test_unreadable_completed_history_never_selects_new_active_input(tmp_path, damage) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, completed_input, active_input = "a" * 64, "8" * 64, "9" * 64
    _source(store, source, (completed_input, "2026-10-17T10:00:00Z"),
            (active_input, "2026-10-18T10:00:00Z"))
    run_id = "r-20261017-1000-abcd"
    _run(store, run_id, source, completed_input, "2026-10-17T10:00:00Z")
    record_completion(store, run_id, source, completed_input)
    path = store.runs_dir / f"{run_id}.jsonl"
    if damage == "corrupt_middle":
        lines = path.read_bytes().splitlines()
        path.write_bytes(b"\n".join((lines[0], lines[1], b"{malformed", lines[2])) + b"\n")
    else:
        path.unlink()

    api = LabApi(store)
    assert api.sample(source[:12])["state"] == "completion_history_unavailable"
    listing = api.samples()
    assert listing["default_sample_id12"] is None
    assert listing["completion_history_unavailable"] is True
    assert listing["samples"][0]["state"] == "completion_history_unavailable"


@needs_chrome
def test_newer_unreadable_completion_blocks_older_default_but_not_older_explicit(tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    older, newer = "a" * 64, "b" * 64
    old_input, new_input = "8" * 64, "9" * 64
    _source(store, older, (old_input, "2026-10-17T10:00:00Z"))
    _source(store, newer, (new_input, "2026-10-18T10:00:00Z"))
    old_run, new_run = "r-20261017-1000-abcd", "r-20261018-1000-abcd"
    _run(store, old_run, older, old_input, "2026-10-17T10:00:00Z")
    record_completion(store, old_run, older, old_input)
    _run(store, new_run, newer, new_input, "2026-10-18T10:00:00Z")
    record_completion(store, new_run, newer, new_input)
    (store.runs_dir / f"{new_run}.jsonl").unlink()

    api = LabApi(store)
    listing = api.samples()
    assert listing["default_sample_id12"] is None
    assert listing["completion_history_unavailable"] is True
    assert api.sample(older[:12])["full_input_id"] == old_input
    assert api.sample(newer[:12])["state"] == "completion_history_unavailable"
    with RunningLabServer(store) as server:
        home = _dom(server, tmp_path / "browser-home", route="/",
                    want=lambda page: "Completion history unavailable" in page)
        detail = _dom(server, tmp_path / "browser-detail", route=f"/?sample={newer[:12]}",
                      want=lambda page: f"Sample {newer[:12]} exists" in page)
    assert "latest recorded completion cannot be verified" in home
    assert f"Sample {newer[:12]} exists" in detail and "Unknown sample" not in detail


def test_valid_run_wins_when_saved_pointer_has_later_file_mtime(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    source, input_id = "a" * 64, "8" * 64
    _source(store, source, (input_id, "2026-10-17T10:00:00Z"))
    run_id = "r-20261017-1000-abcd"
    _run(store, run_id, source, input_id, "2026-10-17T10:00:00Z")
    record_completion(store, run_id, source, input_id)
    os.utime(store.runs_dir / f"{run_id}.jsonl", ns=(1, 1))

    api = LabApi(store)
    assert api.sample(source[:12])["full_input_id"] == input_id
    listing = api.samples()
    assert listing["default_sample_id12"] == source[:12]
    assert listing["completion_history_unavailable"] is False
