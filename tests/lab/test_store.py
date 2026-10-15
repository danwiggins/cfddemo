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

import pytest

from traceback_lab import STORE_VERSION
from traceback_lab import store as store_module
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore, new_run_id, summarize_run

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


def test_unselected_attempts_are_pruned_to_three(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    ids = [store.publish_result(_envelope(n), input_id=C) for n in range(6)]
    attempts = sorted(path.name for path in (store.result_dir(C, "fragment-size", A) / "attempts").iterdir())
    assert attempts == sorted(ids[-4:])


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
