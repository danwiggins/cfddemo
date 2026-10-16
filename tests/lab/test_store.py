"""The Lab store: atomic publish, selected attempts, writer lock, heartbeat, run log."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from traceback_lab import STORE_VERSION
from traceback_lab import store as store_module
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore, ResultPublishUncertain, new_run_id, summarize_run

A = "a" * 64
B = "b" * 64
C = "c" * 64
REPO = Path(__file__).resolve().parents[2]


def _envelope(value: int, *, input_id: str = C) -> ResultEnvelope:
    return ResultEnvelope.model_validate(
        {
            "algorithm_id": "fragment-size",
            "algorithm_version": "1",
            "status": "ok",
            "provenance": provenance(
                tool="traceback_lab.test",
                parameters_sha256=A,
                source_id=B,
                input_id=input_id,
                reference_id="ref",
                timestamp_utc="2026-10-14T21:00:00Z",
            ),
            "metrics": [{"metric_id": "mode_bp", "value": value}],
        }
    )


def test_store_version_written_once_and_newer_refused(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    assert store.version_file.read_text().strip() == str(STORE_VERSION)
    store.version_file.write_text(f"{STORE_VERSION + 1}\n")
    with pytest.raises(LabProblem) as caught:
        store.check_version()
    assert caught.value.code == "TBX-LAB-018"
    assert caught.value.data == {"store_version": STORE_VERSION + 1, "code_version": STORE_VERSION}
    store.version_file.write_text("garbage\n")
    with pytest.raises(LabProblem) as caught:
        store.check_version()
    assert caught.value.code == "TBX-LAB-018"


def test_v1_fixture_store_reads(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.lab.mkdir(parents=True)
    store.version_file.write_text("1\n")
    store.check_version()
    with store.writer("run", new_run_id()):
        store.publish_result(_envelope(166), input_id=C)
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 166


def test_publish_selects_newest_and_bumps_generation(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    assert store.generation() == 0
    first = store.publish_result(_envelope(1), input_id=C, artifacts={"h/x.json": b"{}"})
    second = store.publish_result(_envelope(2), input_id=C)
    assert first != second
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 2
    assert store.generation() == 2
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None and attempt.name == second
    old = store.result_dir(C, "fragment-size", A) / "attempts" / first
    assert (old / "artifacts" / "h" / "x.json").read_bytes() == b"{}"


def _failed(n: int) -> ResultEnvelope:
    data = json.loads(_envelope(n).to_bytes())
    data.update(status="failed", metrics=[], failure={"first_line": f"boom {n}", "stderr_tail": []})
    return ResultEnvelope.model_validate(data)


def test_pruning_keeps_selected_previous_and_last_three_failures(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    failures = [store.publish_result(_failed(n), input_id=C) for n in range(4)]
    oks = [store.publish_result(_envelope(n), input_id=C) for n in range(4)]
    attempts = sorted(path.name for path in (store.result_dir(C, "fragment-size", A) / "attempts").iterdir())
    # selected (oks[-1]) + the one it replaced (oks[-2]) + the newest 3 failures
    assert attempts == sorted([*failures[-3:], oks[-2], oks[-1]])


def test_interrupted_publish_leaves_old_selection_and_no_staging(tmp_path: Path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    seen: list[int] = []

    def crash(parent_fd: int, source: str, destination: str) -> None:
        # A reader during the publish sees the old, complete attempt.
        seen.append(store.read_result(C, "fragment-size", A).metrics[0].value)
        raise OSError(5, "simulated crash between staging and rename")

    monkeypatch.setattr(store_module, "rename_directory_exclusive_at", crash)
    with pytest.raises(OSError):
        store.publish_result(_envelope(2), input_id=C)
    assert seen == [1]
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 1
    assert store.orphaned_staging() == []
    assert store.generation() == 1


def test_failed_retry_selects_failure_but_retains_prior_success(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    success = store.publish_result(_envelope(1), input_id=C, artifacts={"h/x.json": b"ok"})
    failures = [store.publish_result(_failed(n), input_id=C) for n in range(5)]

    selected = store.selected_attempt(C, "fragment-size", A)
    assert selected is not None and selected.name == failures[-1]
    assert store.read_result(C, "fragment-size", A).status == "failed"
    attempts = store.result_dir(C, "fragment-size", A) / "attempts"
    assert (attempts / success / "artifacts" / "h" / "x.json").read_bytes() == b"ok"
    assert sorted(path.name for path in attempts.iterdir()) == sorted([success, *failures[-3:]])


def test_reversed_attempt_timestamps_keep_publication_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = iter(
        f"a-20261016T205100{n:06d}-{n:08x}"
        for n in range(7, 0, -1)
    )
    monkeypatch.setattr(store_module, "new_attempt_id", lambda: next(ids))
    store = LabStore(tmp_path)
    store.ensure()
    older = store.publish_result(_envelope(1), input_id=C)
    latest_success = store.publish_result(_envelope(2), input_id=C)
    failures = [store.publish_result(_failed(n), input_id=C) for n in range(5)]

    directory = store.result_dir(C, "fragment-size", A)
    selected = store.selected_attempt(C, "fragment-size", A)
    assert selected is not None and selected.name == failures[-1]
    assert store.read_result(C, "fragment-size", A).status == "failed"
    selector = json.loads((directory / "selected.json").read_bytes())
    assert selector["last_success_attempt_id"] == latest_success
    attempts = directory / "attempts"
    assert not (attempts / older).exists()
    assert sorted(path.name for path in attempts.iterdir()) == sorted([latest_success, *failures[-3:]])

def test_ambiguous_legacy_successes_are_not_pruned(tmp_path: Path, monkeypatch) -> None:
    ids = iter(
        f"a-20261016T205200{n:06d}-{n:08x}"
        for n in range(6, 0, -1)
    )
    monkeypatch.setattr(store_module, "new_attempt_id", lambda: next(ids))
    store = LabStore(tmp_path)
    store.ensure()
    older = store.publish_result(_envelope(1), input_id=C)
    newer = store.publish_result(_envelope(2), input_id=C)
    attempts = store.result_dir(C, "fragment-size", A) / "attempts"
    for name in (older, newer):
        (attempts / name / "artifacts.json").unlink()

    for n in range(4):
        store.publish_result(_failed(n), input_id=C)
    assert (attempts / older).is_dir()
    assert (attempts / newer).is_dir()
    assert store.read_result(C, "fragment-size", A).status == "failed"

def test_unreadable_ordinal_aborts_publication_without_reusing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    latest = store.publish_result(_envelope(2), input_id=C)
    directory = store.result_dir(C, "fragment-size", A)
    attempts = directory / "attempts"
    before_attempts = {path.name for path in attempts.iterdir()}
    before_selector = (directory / "selected.json").read_bytes()
    before_generation = store.generation()
    target = attempts / latest / "artifacts.json"
    real_read = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == target:
            raise OSError(5, "synthetic EIO")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert store.read_result(C, "fragment-size", A) is None
    with pytest.raises(store_module.ResultOrderUnavailable, match="unreadable"):
        store.publish_result(_failed(3), input_id=C)
    assert (directory / "selected.json").read_bytes() == before_selector
    assert {path.name for path in attempts.iterdir()} == before_attempts
    assert store.generation() == before_generation

    monkeypatch.setattr(Path, "read_bytes", real_read)
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 2
    retry = store.publish_result(_failed(3), input_id=C)
    index = json.loads((attempts / retry / "artifacts.json").read_bytes())
    assert index["ordinal"] == 3


def test_unreadable_order_during_prune_keeps_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    first = store.publish_result(_envelope(1), input_id=C)
    second = store.publish_result(_envelope(2), input_id=C)
    real_order = LabStore._attempt_order
    calls = 0

    def intermittent(path: Path) -> int:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise store_module.ResultOrderUnavailable("synthetic pruning EIO")
        return real_order(path)

    monkeypatch.setattr(LabStore, "_attempt_order", staticmethod(intermittent))
    failed = store.publish_result(_failed(3), input_id=C)
    directory = store.result_dir(C, "fragment-size", A)
    assert store.selected_attempt(C, "fragment-size", A).name == failed
    assert {path.name for path in (directory / "attempts").iterdir()} == {first, second, failed}

def test_invalid_modern_ordinal_is_not_treated_as_legacy(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    attempt = store.publish_result(_envelope(1), input_id=C)
    directory = store.result_dir(C, "fragment-size", A)
    index_path = directory / "attempts" / attempt / "artifacts.json"
    index = json.loads(index_path.read_bytes())
    index["ordinal"] = 0
    index_path.write_text(json.dumps(index))
    before_selector = (directory / "selected.json").read_bytes()

    assert store.read_result(C, "fragment-size", A) is None
    with pytest.raises(store_module.ResultOrderUnavailable, match="ordinal"):
        store.publish_result(_envelope(2), input_id=C)
    assert (directory / "selected.json").read_bytes() == before_selector
    assert [path.name for path in (directory / "attempts").iterdir()] == [attempt]

def test_selection_write_failure_keeps_prior_success(tmp_path: Path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    first = store.publish_result(_envelope(1), input_id=C)
    real_write = store_module.write_atomic

    def fail_selection(path: Path, content: bytes) -> None:
        if path.name == "selected.json":
            raise OSError(5, "simulated selection write failure")
        real_write(path, content)

    monkeypatch.setattr(store_module, "write_atomic", fail_selection)
    with pytest.raises(OSError, match="selection write failure"):
        store.publish_result(_envelope(2), input_id=C)
    assert store.selected_attempt(C, "fragment-size", A).name == first
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 1
    assert store.generation() == 1
    monkeypatch.setattr(store_module, "write_atomic", real_write)
    for n in range(4):
        store.publish_result(_failed(n), input_id=C)
    directory = store.result_dir(C, "fragment-size", A)
    selected = json.loads((directory / "selected.json").read_bytes())
    assert selected["last_success_attempt_id"] == first
    assert (directory / "attempts" / first).is_dir()


@pytest.mark.parametrize("fail_generation", [False, True])
def test_selection_directory_sync_failure_keeps_every_attempt(
    tmp_path: Path, monkeypatch, fail_generation: bool
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    first = store.publish_result(_envelope(1), input_id=C)
    second = store.publish_result(_envelope(2), input_id=C)
    directory = store.result_dir(C, "fragment-size", A)
    real_fsync_dir = store_module._fsync_dir

    def fail_selection_sync(path: Path) -> None:
        if path == directory:
            raise OSError(5, "simulated selector directory sync failure")
        real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", fail_selection_sync)
    if fail_generation:
        def fail_bump() -> int:
            raise OSError(28, "simulated generation write failure")

        monkeypatch.setattr(store, "bump_generation", fail_bump)
    with pytest.raises(ResultPublishUncertain) as caught:
        store.publish_result(_envelope(3), input_id=C)

    assert caught.value.code == "TBX-LAB-020"
    assert isinstance(caught.value.__cause__, OSError)
    assert "selector directory sync failure" in str(caught.value.__cause__)
    assert "visible" in caught.value.cause
    assert "before retrying" in caught.value.fix
    assert caught.value.data["generation_notified"] is not fail_generation
    if fail_generation:
        assert "Manually reload" in caught.value.fix
        assert "generation notification could not be confirmed" in caught.value.cause
    else:
        assert "generation counter advanced" in caught.value.cause
    selected = json.loads((directory / "selected.json").read_bytes())["attempt_id"]
    assert caught.value.data == {
        "input_id": C,
        "algorithm_id": "fragment-size",
        "parameters_sha256": A,
        "attempt_id": selected,
        "generation_notified": not fail_generation,
    }
    assert selected not in {first, second}
    assert store.selected_attempt(C, "fragment-size", A).name == selected
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 3
    assert {path.name for path in (directory / "attempts").iterdir()} == {first, second, selected}
    assert store.generation() == (2 if fail_generation else 3)

    monkeypatch.undo()
    fourth = store.publish_result(_envelope(4), input_id=C)
    assert {path.name for path in (directory / "attempts").iterdir()} == {selected, fourth}
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 4


def test_publish_directory_refuses_existing_destination(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    parent = store.inputs_dir
    store.publish_directory(parent, "x", lambda staging: (staging / "f").write_bytes(b"1"))
    with pytest.raises(FileExistsError):
        store.publish_directory(parent, "x", lambda staging: (staging / "f").write_bytes(b"2"))
    assert (parent / "x" / "f").read_bytes() == b"1"
    assert store.orphaned_staging() == []


def test_result_must_be_stored_under_the_consumed_input(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    with pytest.raises(ValueError):
        store.publish_result(_envelope(1, input_id=C), input_id=B)


@pytest.mark.parametrize("name", ["../x", "/abs", ""])
def test_artifact_paths_are_contained(tmp_path: Path, name: str) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    with pytest.raises(ValueError):
        store.publish_result(_envelope(1), input_id=C, artifacts={name: b"x"})


def test_selected_json_with_a_bad_attempt_id_reads_as_missing(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    (store.result_dir(C, "fragment-size", A) / "selected.json").write_text('{"attempt_id":"../../x"}')
    assert store.read_result(C, "fragment-size", A) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("algorithm_id", "copy-number"),
        ("parameters_sha256", B),
        ("input_id", B),
    ],
)
def test_selected_result_with_wrong_identity_is_unavailable(
    tmp_path: Path, field: str, value: str,
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None
    data = json.loads((attempt / "result.json").read_bytes())
    if field in ("parameters_sha256", "input_id"):
        data["provenance"][field] = value
    else:
        data[field] = value
    (attempt / "result.json").write_text(json.dumps(data))
    assert store.selected_attempt(C, "fragment-size", A) is None
    assert store.read_result(C, "fragment-size", A) is None


def test_malformed_selected_result_is_unavailable(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None
    (attempt / "result.json").write_bytes(b"{broken")
    assert store.selected_attempt(C, "fragment-size", A) is None
    assert store.read_result(C, "fragment-size", A) is None


def test_incomplete_new_panel_attempt_does_not_replace_success(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    first = store.publish_result(_envelope(1), input_id=C)
    data = json.loads(_envelope(2).to_bytes())
    data["panels"] = [{"panel_id": "histogram", "kind": "chart", "data_ref": "artifacts/h/x.json"}]
    with pytest.raises(ValueError, match="panel data_ref"):
        store.publish_result(ResultEnvelope.model_validate(data), input_id=C)
    assert store.selected_attempt(C, "fragment-size", A).name == first
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 1
    assert store.orphaned_staging() == []

def test_missing_selected_panel_artifact_is_unavailable(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    data = json.loads(_envelope(1).to_bytes())
    data["panels"] = [{"panel_id": "histogram", "kind": "chart", "data_ref": "artifacts/h/x.json"}]
    envelope = ResultEnvelope.model_validate(data)
    store.publish_result(envelope, input_id=C, artifacts={"h/x.json": b"{}"})
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None
    (attempt / "artifacts" / "h" / "x.json").unlink()
    assert store.selected_attempt(C, "fragment-size", A) is None
    assert store.read_result(C, "fragment-size", A) is None

@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_incomplete_upstream_artifact_is_not_reused(tmp_path: Path, damage: str) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(
        _envelope(1), input_id=C,
        artifacts={"columns/meta.json": b"meta", "columns/flag.bin": b"1234"},
    )
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None
    artifact = attempt / "artifacts" / "columns" / "flag.bin"
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"12")
    assert store.read_result(C, "fragment-size", A) is None
    assert store.selected_artifacts(C, "fragment-size", A) is None

def test_pre_index_scalar_result_remains_readable(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    store.publish_result(_envelope(1), input_id=C)
    attempt = store.selected_attempt(C, "fragment-size", A)
    assert attempt is not None
    (attempt / "artifacts.json").unlink()
    assert store.read_result(C, "fragment-size", A).metrics[0].value == 1


@pytest.mark.parametrize("algorithm_id", ["read-summary", "meth-calls"])
def test_pre_index_intermediate_requires_complete_layout(
    tmp_path: Path, algorithm_id: str,
) -> None:
    from traceback_lab import meth_calls, read_summary

    store = LabStore(tmp_path)
    store.ensure()
    data = json.loads(_envelope(1).to_bytes())
    data["algorithm_id"] = algorithm_id
    envelope = ResultEnvelope.model_validate(data)
    if algorithm_id == "read-summary":
        meta = {"schema": read_summary.SCHEMA, "rows": 1, "columns": read_summary.COLUMNS}
        artifacts = {
            **{
                f"columns/{name}.bin": bytes(np.dtype(dtype).itemsize)
                for name, dtype in read_summary.COLUMNS.items()
            },
            "columns/meta.json": json.dumps(meta).encode(),
        }
        damaged = "columns/flag.bin"
    else:
        meta = {
            "schema": meth_calls.SCHEMA, "dtype": [list(field) for field in meth_calls.DTYPE.descr], "chunks": [1], "invalid_mm": 1,
        }
        artifacts = {
            "calls/meta.json": json.dumps(meta).encode(),
            "calls/calls-00000.bin": bytes(meth_calls.DTYPE.itemsize),
            "calls/invalid_rows.bin": bytes(4),
        }
        damaged = "calls/calls-00000.bin"
    store.publish_result(envelope, input_id=C, artifacts=artifacts)
    attempt = store.selected_attempt(C, algorithm_id, A)
    assert attempt is not None
    (attempt / "artifacts.json").unlink()
    assert store.read_result(C, algorithm_id, A) is not None
    (attempt / "artifacts" / damaged).write_bytes(b"")
    assert store.read_result(C, algorithm_id, A) is None

@pytest.mark.parametrize("algorithm_id", ["read-summary", "meth-calls"])
@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_pre_index_intermediate_non_ok_result_remains_visible(
    tmp_path: Path, algorithm_id: str, status: str,
) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    data = json.loads(_envelope(1).to_bytes())
    data["algorithm_id"] = algorithm_id
    data["status"] = status
    data["metrics"] = []
    if status == "failed":
        data["failure"] = {"first_line": "synthetic failure", "stderr_tail": []}
    else:
        data["skip_reason"] = "depth"
        data["skip_message"] = "synthetic preflight skip"
    store.publish_result(ResultEnvelope.model_validate(data), input_id=C)
    attempt = store.selected_attempt(C, algorithm_id, A)
    assert attempt is not None
    (attempt / "artifacts.json").unlink()

    selected = store.read_result(C, algorithm_id, A)
    assert selected is not None and selected.status == status
    assert store.selected_attempt(C, algorithm_id, A) == attempt
    assert store.selected_artifacts(C, algorithm_id, A) is None

def test_writer_lock_heartbeat_and_cleanup(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    run_id = new_run_id()
    with store.writer("run", run_id, step="ingest") as lock:
        active = store.read_active()
        assert active is not None and active.active
        assert (active.run_id, active.pid, active.command, active.step) == (run_id, os.getpid(), "run", "ingest")
        lock.set_step("align")
        assert store.read_active().step == "align"
        assert store.status()["active"]["step"] == "align"
        # A second writer in the same process uses a new open file description
        # and is refused like any other process.
        with pytest.raises(LabProblem) as caught:
            with store.writer("simulate", new_run_id()):
                pass
        problem = caught.value
        assert problem.code == "TBX-LAB-012"
        assert problem.exit_code == 3 and problem.retryable
        holder = problem.data["holder"]
        assert holder["command"] == "run" and holder["run_id"] == run_id
        assert holder["pid"] == os.getpid() and holder["step"] == "align"
        assert f"pid {os.getpid()}" in problem.cause and "step align" in problem.cause
    assert store.read_active() is None
    assert store.status() == {"generation": 0, "active": None}


def test_heartbeat_thread_refreshes(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    with store.writer("run", new_run_id(), heartbeat_s=0.05):
        first = store.active_file.read_bytes()
        deadline = time.time() + 3
        while time.time() < deadline and store.active_file.read_bytes() == first:
            time.sleep(0.05)
        # heartbeat_utc has 1 s resolution; the file is rewritten regardless
        assert store.read_active() is not None


def test_lock_held_by_another_process(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from traceback_lab.store import LabStore
        store = LabStore(Path({str(tmp_path)!r}))
        with store.writer("cohort build", "r-20261014-2104-7f3a", step="pon"):
            print("held", flush=True)
            time.sleep(30)
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script], cwd=REPO, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        store = LabStore(tmp_path)
        with pytest.raises(LabProblem) as caught:
            with store.writer("run", new_run_id()):
                pass
        assert caught.value.data["holder"]["command"] == "cohort build"
        assert caught.value.data["holder"]["pid"] == holder.pid
        # The server's status read never touches the lock.
        for _ in range(100):
            assert store.status()["active"]["run_id"] == "r-20261014-2104-7f3a"
    finally:
        holder.kill()
        holder.wait()
    # The kernel released the lock when the holder died; a stale heartbeat stays.
    store = LabStore(tmp_path)
    active = store.read_active()
    assert active is not None and not active.active
    with store.writer("run", new_run_id()):
        pass


def test_status_polling_never_blocks_a_starting_writer(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    stop = threading.Event()
    polls = 0

    def poll() -> None:
        nonlocal polls
        while not stop.is_set():
            LabStore(tmp_path).status()
            polls += 1

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        for _ in range(50):
            with store.writer("run", new_run_id()):
                pass
    finally:
        stop.set()
        thread.join()
    assert polls > 0


def test_stale_heartbeat_reads_as_interrupted(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    run_id = "r-20261014-2104-7f3a"
    log = store.run_log(run_id)
    log.append("run_start", command="run", sample="3f9a1c2b7e04")
    log.append("step_start", step="align")
    store.active_file.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": 999999,
                "command": "run",
                "step": "align",
                "started_utc": "2026-10-14T21:00:00Z",
                "heartbeat_utc": "2026-10-14T21:00:00Z",
            }
        )
    )
    assert store.status()["active"] is None
    summary = summarize_run(log.events(), store)
    assert summary["status"] == "interrupted"
    assert summary["open_step"] == "align"


def test_run_log_tolerates_a_torn_last_line(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    run_id = new_run_id()
    log = store.run_log(run_id)
    log.append("run_start", command="run")
    log.append("step_start", step="ingest")
    log.append("step_end", step="ingest", status="ok", duration_s=1.5)
    log.append("run_end", status="ok", exit_code=0)
    with log.path.open("ab") as stream:
        stream.write(b'{"event":"step_')
    events = log.events()
    assert [event["event"] for event in events] == ["run_start", "step_start", "step_end", "run_end"]
    summary = summarize_run(events, store)
    assert summary["status"] == "ok" and summary["steps"] == {"ok": 1}
    assert store.run_ids() == [run_id]


def test_run_ids_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LabStore(tmp_path).run_log("../../etc/passwd")


def test_hash_cache_round_trip(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    chunk = tmp_path / "chunk_0.bam"
    chunk.write_bytes(b"x")
    key = store.hash_cache_key(chunk)
    store.save_hash_cache({key: {"sha256": A, "records": 3}})
    assert store.load_hash_cache() == {key: {"sha256": A, "records": 3}}
    os.utime(chunk, ns=(1, 1))
    assert store.hash_cache_key(chunk) != key


def test_orphaned_staging_is_reported(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    orphan = store.inputs_dir / ".staging-deadbeef"
    orphan.mkdir(parents=True)
    assert store.orphaned_staging() == [orphan]
