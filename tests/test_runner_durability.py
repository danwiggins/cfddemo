"""Fault-injected tests for the synthetic durable runner."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evidence_inspector.models import sha256_bytes
from traceback_runner.contracts import InputKind, JobRequest, JobState
from traceback_runner.runner import (
    InjectedCrash,
    OrphanQuarantined,
    Runner,
    StageResult,
    StageSpec,
    SyntheticExecutionDisabled,
)
from traceback_runner.snapshots import (
    SnapshotViolation,
    capture_snapshot,
    input_tree_sha256,
)
from traceback_runner.store import JobStore, StaleLease, UnsupportedSchema


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _request(source: Path, files: list[str]) -> JobRequest:
    return JobRequest(
        sample_token="sample.synthetic",
        input_kind=InputKind.MODBAM,
        input_tree_sha256_local=input_tree_sha256(source, files),
        workflow_release_sha256=sha256_bytes(b"synthetic-workflow-v1"),
        execution_options={"synthetic": True},
    )


def _source(tmp_path: Path) -> tuple[Path, list[str]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.bin").write_bytes(b"synthetic-only-input")
    return source, ["input.bin"]


def _stage(name: str = "measure", version: str = "v1") -> StageSpec:
    def callback(context: object) -> StageResult:
        attempt_dir = context.attempt_dir  # type: ignore[attr-defined]
        (attempt_dir / "result.json").write_text('{"synthetic":true}')
        return StageResult(
            outputs={"result": "result.json"},
            metadata={"scope": "synthetic"},
            postconditions={"json_valid": True, "synthetic_only": True},
        )

    return StageSpec(name, version, callback, {"mode": "synthetic"})


def test_duplicate_and_concurrent_submit_creates_one_job(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    request = _request(source, files)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(
                lambda _: runner.submit(
                    request, source, files, idempotency_key="same-request"
                ),
                range(8),
            )
        )

    assert len({record.job_id for record in records}) == 1
    job_id = records[0].job_id
    assert runner.status(job_id).state == JobState.READY
    with sqlite3.connect(runner.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    summary = runner.snapshot(job_id)
    assert summary["content_sha256"] == request.input_tree_sha256_local
    assert str(source) not in json.dumps(summary)


def test_submit_rejects_caller_digest_that_does_not_match_sealed_bytes(
    tmp_path: Path,
) -> None:
    source, files = _source(tmp_path)
    request = _request(source, files).model_copy(
        update={"input_tree_sha256_local": "0" * 64}
    )
    runner = Runner(tmp_path / "state")

    with pytest.raises(SnapshotViolation, match="does not match"):
        runner.submit(request, source, files)

    jobs = runner.store.audit(runner.store.submit(request).job_id)
    assert jobs[-1]["next_state"] == JobState.TERMINAL_FAILURE.value


def test_snapshot_rejects_mutation_and_symlink(tmp_path: Path) -> None:
    source, files = _source(tmp_path)

    def mutate(path: Path) -> None:
        path.write_bytes(b"changed")

    with pytest.raises(SnapshotViolation, match="changed during capture"):
        capture_snapshot(source, files, tmp_path / "snapshots", after_copy=mutate)

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (source / "escape.bin").symlink_to(outside)
    with pytest.raises(SnapshotViolation, match="symlinks are not accepted"):
        capture_snapshot(source, ["escape.bin"], tmp_path / "snapshots2")


def test_lease_expiry_fences_stale_worker_commit(tmp_path: Path) -> None:
    clock = FakeClock()
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", clock=clock, synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    runner.store.transition(job.job_id, JobState.RUNNING, "test worker started")
    first = runner.store.acquire_lease(job.job_id, "measure", "a" * 64, "worker-a", 5)
    clock.now += 6
    second = runner.store.acquire_lease(job.job_id, "measure", "a" * 64, "worker-b", 5)

    assert second.token > first.token
    with pytest.raises(StaleLease, match="stale worker"):
        runner.store.commit_attempt(first, "old/receipt.json", "b" * 64)


def test_publication_crash_is_adopted_exactly_once(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    fired = False

    def crash(point: str) -> None:
        nonlocal fired
        if point == "after_publication" and not fired:
            fired = True
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(state, synthetic_enabled=True, fault_injector=crash)
    job = runner.submit(_request(source, files), source, files)
    with pytest.raises(InjectedCrash):
        runner.execute(job.job_id, [_stage()], worker_id="worker-a")

    recovered = Runner(state, synthetic_enabled=True)
    report = recovered.recover(job.job_id)
    assert report.adopted == ("measure",)
    assert recovered.recover(job.job_id).adopted == ("measure",)
    result = recovered.execute(job.job_id, [_stage()], worker_id="worker-b")
    assert result.state == JobState.COMPLETE
    outputs = recovered.outputs(job.job_id, "measure")
    assert outputs["result"].read_text() == '{"synthetic":true}'
    with sqlite3.connect(recovered.store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE status='committed'"
            ).fetchone()[0]
            == 1
        )


def test_db_commit_crash_reuses_receipt_without_rerunning_callback(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    calls = 0

    def callback(context: object) -> StageResult:
        nonlocal calls
        calls += 1
        (context.attempt_dir / "result.json").write_text("{}")  # type: ignore[attr-defined]
        return StageResult({"result": "result.json"})

    def crash(point: str) -> None:
        if point == "after_db_commit":
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(state, synthetic_enabled=True, fault_injector=crash)
    job = runner.submit(_request(source, files), source, files)
    stage = StageSpec("measure", "v1", callback)
    with pytest.raises(InjectedCrash):
        runner.execute(job.job_id, [stage], worker_id="worker-a")
    assert calls == 1


def test_recovery_preserves_verified_receipts_from_multiple_stages(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    stages = [_stage("prepare"), _stage("measure")]
    assert runner.execute(job.job_id, stages, worker_id="worker").state == JobState.COMPLETE

    report = runner.recover(job.job_id)
    assert set(report.adopted) == {"prepare", "measure"}
    assert report.quarantined == ()
    assert runner.outputs(job.job_id, "prepare")["result"].is_file()
    assert runner.outputs(job.job_id, "measure")["result"].is_file()

    recovered = Runner(state, synthetic_enabled=True)
    assert recovered.execute(job.job_id, [stage], worker_id="worker-b").state == JobState.COMPLETE
    assert calls == 1


def test_private_crash_is_quarantined_and_retried_after_lease_expiry(
    tmp_path: Path,
) -> None:
    source, files = _source(tmp_path)
    clock = FakeClock()

    def crash(point: str) -> None:
        if point == "after_receipt":
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(
        state,
        clock=clock,
        lease_seconds=5,
        synthetic_enabled=True,
        fault_injector=crash,
    )
    job = runner.submit(_request(source, files), source, files)
    with pytest.raises(InjectedCrash):
        runner.execute(job.job_id, [_stage()], worker_id="worker-a")

    clock.now += 6
    recovered = Runner(state, clock=clock, lease_seconds=5, synthetic_enabled=True)
    report = recovered.recover(job.job_id)
    assert len(report.quarantined) == 1
    assert recovered.execute(job.job_id, [_stage()], worker_id="worker-b").state == JobState.COMPLETE


@pytest.mark.parametrize("target", ["receipt", "output"])
def test_corrupt_publication_is_quarantined(tmp_path: Path, target: str) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    runner.execute(job.job_id, [_stage()], worker_id="worker")
    publication = next((runner.artifacts_dir / job.job_id / "measure").iterdir())
    victim = publication / ("receipt.json" if target == "receipt" else "result.json")
    victim.chmod(0o644)
    victim.write_bytes(b"corrupt")

    with pytest.raises(OrphanQuarantined):
        runner.recover(job.job_id)
    assert any(runner.quarantine_dir.iterdir())


def test_pause_retry_and_explicit_synthetic_gate(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    request = _request(source, files)
    disabled = Runner(tmp_path / "disabled")
    job = disabled.submit(request, source, files)
    with pytest.raises(SyntheticExecutionDisabled, match="real-data execution is not enabled"):
        disabled.execute(job.job_id, [_stage()], worker_id="worker")

    enabled = Runner(tmp_path / "enabled", synthetic_enabled=True)
    job = enabled.submit(request, source, files)

    def pausing(context: object) -> StageResult:
        enabled.request_pause(context.job_id)  # type: ignore[attr-defined]
        (context.attempt_dir / "paused.json").write_text("{}")  # type: ignore[attr-defined]
        return StageResult({"result": "paused.json"})

    stages = [StageSpec("measure", "v1", pausing)]
    assert enabled.execute(job.job_id, stages, worker_id="worker").state == JobState.PAUSED
    assert enabled.resume(job.job_id, stages, worker_id="worker").state == JobState.COMPLETE


def test_failure_can_retry_without_reusing_changed_stage_definition(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)

    def fail(_: object) -> StageResult:
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        runner.execute(job.job_id, [StageSpec("measure", "v1", fail)], worker_id="a")
    assert runner.status(job.job_id).state == JobState.RETRYABLE_FAILURE
    result = runner.resume(job.job_id, [_stage(version="v2")], worker_id="b")
    assert result.state == JobState.COMPLETE


def test_backup_and_unsupported_schema_fail_closed(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "runner.sqlite3")
    backup = store.backup(tmp_path / "backup.sqlite3")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    bad = tmp_path / "future.sqlite3"
    with sqlite3.connect(bad) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
    with pytest.raises(UnsupportedSchema, match="unsupported"):
        JobStore(bad)
