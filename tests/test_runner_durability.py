"""Fault-injected tests for the synthetic durable runner."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Event

import pytest

from evidence_inspector.models import sha256_bytes
from traceback_runner.contracts import InputKind, JobRequest, JobState, StageReceipt
from traceback_runner.receipts import OutputCorrupt, load_receipt
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
from traceback_runner.store import JobStore, StaleLease, StoreError, UnsupportedSchema


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
        execution_options={"offline": True, "threads": 1},
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


def test_store_permissions_and_identity_are_revalidated(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    path = tmp_path / "private-state" / "jobs.sqlite3"
    store = JobStore(path)
    record = store.submit(_request(source, files))

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    os.chmod(path, 0o644)
    with pytest.raises(StoreError, match="database identity changed"):
        store.get(record.job_id)


def test_projection_state_and_revision_share_one_sqlite_snapshot(
    tmp_path: Path,
) -> None:
    source, files = _source(tmp_path)
    store = JobStore(tmp_path / "state" / "jobs.sqlite3")
    record = store.submit(_request(source, files))
    before = store.get_projection_snapshot(record.job_id)
    started = Event()

    def transition() -> None:
        started.set()
        store.transition(
            record.job_id,
            JobState.WAITING_FOR_FINALIZATION,
            "input remains open",
        )

    observed: set[tuple[JobState, int]] = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(transition)
        assert started.wait(2)
        for _ in range(100):
            snapshot = store.get_projection_snapshot(record.job_id)
            observed.add((snapshot.record.state, snapshot.revision))
            listed = store.list_projection_snapshots()
            assert (listed[0].record.state, listed[0].revision) in {
                (before.record.state, before.revision),
                (JobState.WAITING_FOR_FINALIZATION, before.revision + 1),
            }
        future.result()
    after = store.get_projection_snapshot(record.job_id)
    observed.add((after.record.state, after.revision))
    assert observed <= {
        (before.record.state, before.revision),
        (JobState.WAITING_FOR_FINALIZATION, before.revision + 1),
    }


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


def test_stage_output_cannot_escape_attempt_through_symlink(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text("{}")
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)

    def callback(context: object) -> StageResult:
        (context.attempt_dir / "link").symlink_to(outside)  # type: ignore[attr-defined]
        return StageResult({"result": "link/result.json"})

    with pytest.raises(OutputCorrupt, match="escapes attempt directory"):
        runner.execute(
            job.job_id,
            [StageSpec("measure", "v1", callback)],
            worker_id="worker",
        )
    assert runner.status(job.job_id).state == JobState.RETRYABLE_FAILURE


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


def test_recovery_does_not_quarantine_active_unexpired_attempt(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    started = Event()
    release = Event()

    def callback(context: object) -> StageResult:
        started.set()
        assert release.wait(5)
        (context.attempt_dir / "result.json").write_text("{}")  # type: ignore[attr-defined]
        return StageResult({"result": "result.json"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.execute,
            job.job_id,
            [StageSpec("measure", "v1", callback)],
            worker_id="live-worker",
        )
        assert started.wait(5)
        report = runner.recover(job.job_id)
        assert report.quarantined == ()
        release.set()
        assert future.result().state == JobState.COMPLETE


def test_publication_crash_is_adopted_exactly_once(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    fired = False
    clock = FakeClock()

    def crash(point: str) -> None:
        nonlocal fired
        if point == "after_publication" and not fired:
            fired = True
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(state, clock=clock, synthetic_enabled=True, fault_injector=crash)
    job = runner.submit(_request(source, files), source, files)
    with pytest.raises(InjectedCrash):
        runner.execute(job.job_id, [_stage()], worker_id="worker-a")

    clock.now += 31
    recovered = Runner(state, clock=clock, synthetic_enabled=True)
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


def test_db_commit_crash_reuses_receipt_without_rerunning_callback(
    tmp_path: Path,
) -> None:
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
    recovered = Runner(state, synthetic_enabled=True)
    assert (
        recovered.execute(job.job_id, [stage], worker_id="worker-b").state
        == JobState.COMPLETE
    )
    assert calls == 1


def test_changed_stage_version_cannot_reuse_previous_receipt(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    calls: list[str] = []

    def callback(version: str):
        def run(context: object) -> StageResult:
            calls.append(version)
            (context.attempt_dir / "result.json").write_text(version)  # type: ignore[attr-defined]
            return StageResult({"result": "result.json"})

        return run

    fired = False

    def crash(point: str) -> None:
        nonlocal fired
        if point == "after_db_commit" and not fired:
            fired = True
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(state, synthetic_enabled=True, fault_injector=crash)
    job = runner.submit(_request(source, files), source, files)
    with pytest.raises(InjectedCrash):
        runner.execute(
            job.job_id,
            [StageSpec("measure", "v1", callback("v1"))],
            worker_id="worker-a",
        )

    recovered = Runner(state, synthetic_enabled=True)
    recovered.execute(
        job.job_id,
        [StageSpec("measure", "v2", callback("v2"))],
        worker_id="worker-b",
    )
    assert calls == ["v1", "v2"]
    assert recovered.outputs(job.job_id, "measure")["result"].read_text() == "v2"
    publications = (recovered.artifacts_dir / job.job_id / "measure").iterdir()
    envelope = next(
        load_receipt(path)
        for path in publications
        if load_receipt(path).contract.attempt == 2
    )
    assert isinstance(envelope.contract, StageReceipt)


def test_changed_ancestor_invalidates_unchanged_downstream_receipt(
    tmp_path: Path,
) -> None:
    source, files = _source(tmp_path)
    calls: list[str] = []
    commits = 0

    def upstream(version: str):
        def callback(context: object) -> StageResult:
            calls.append(version)
            (context.attempt_dir / "result").write_text(version)  # type: ignore[attr-defined]
            return StageResult({"result": "result"})

        return callback

    def downstream(context: object) -> StageResult:
        calls.append("downstream")
        prior = (context.prior_stage_dirs[-1] / "result").read_text()  # type: ignore[attr-defined]
        (context.attempt_dir / "result").write_text(prior)  # type: ignore[attr-defined]
        return StageResult({"result": "result"})

    def crash(point: str) -> None:
        nonlocal commits
        if point == "after_db_commit":
            commits += 1
            if commits == 2:
                raise InjectedCrash(point)

    runner = Runner(tmp_path / "state", synthetic_enabled=True, fault_injector=crash)
    job = runner.submit(_request(source, files), source, files)
    with pytest.raises(InjectedCrash):
        runner.execute(
            job.job_id,
            [
                StageSpec("validate", "v1", upstream("v1")),
                StageSpec("measure", "v1", downstream),
            ],
            worker_id="old",
        )

    runner.fault_injector = None
    runner.execute(
        job.job_id,
        [
            StageSpec("validate", "v2", upstream("v2")),
            StageSpec("measure", "v1", downstream),
        ],
        worker_id="new",
    )
    assert calls == ["v1", "downstream", "v2", "downstream"]
    assert runner.outputs(job.job_id, "measure")["result"].read_text() == "v2"


def test_snapshot_is_rehashed_before_stage_callback(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    sealed = runner.snapshots_dir / job.job_id / "input.bin"
    sealed.chmod(0o644)
    sealed.write_text("tampered")

    with pytest.raises(SnapshotViolation, match="digest changed"):
        runner.execute(job.job_id, [_stage()], worker_id="worker")
    assert runner.status(job.job_id).state == JobState.TERMINAL_FAILURE


def test_resubmit_recovers_snapshot_sealed_before_db_attach(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    fired = False

    def crash(point: str) -> None:
        nonlocal fired
        if point == "after_snapshot_seal" and not fired:
            fired = True
            raise InjectedCrash(point)

    state = tmp_path / "state"
    runner = Runner(state, synthetic_enabled=True, fault_injector=crash)
    request = _request(source, files)
    with pytest.raises(InjectedCrash):
        runner.submit(request, source, files)

    recovered = Runner(state, synthetic_enabled=True)
    job = recovered.submit(request, source, files)
    assert job.state == JobState.READY
    assert (
        recovered.execute(job.job_id, [_stage()], worker_id="worker").state
        == JobState.COMPLETE
    )


def test_recovery_preserves_verified_receipts_from_multiple_stages(
    tmp_path: Path,
) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    stages = [_stage("validate"), _stage("measure")]
    assert (
        runner.execute(job.job_id, stages, worker_id="worker").state
        == JobState.COMPLETE
    )

    report = runner.recover(job.job_id)
    assert set(report.adopted) == {"validate", "measure"}
    assert report.quarantined == ()
    assert runner.outputs(job.job_id, "validate")["result"].is_file()
    assert runner.outputs(job.job_id, "measure")["result"].is_file()


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
    assert (
        recovered.execute(job.job_id, [_stage()], worker_id="worker-b").state
        == JobState.COMPLETE
    )


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
    with pytest.raises(
        SyntheticExecutionDisabled, match="real-data execution is not enabled"
    ):
        disabled.execute(job.job_id, [_stage()], worker_id="worker")

    enabled = Runner(tmp_path / "enabled", synthetic_enabled=True)
    job = enabled.submit(request, source, files)

    def pausing(context: object) -> StageResult:
        enabled.request_pause(context.job_id)  # type: ignore[attr-defined]
        (context.attempt_dir / "paused.json").write_text("{}")  # type: ignore[attr-defined]
        return StageResult({"result": "paused.json"})

    stages = [StageSpec("measure", "v1", pausing)]
    assert (
        enabled.execute(job.job_id, stages, worker_id="worker").state == JobState.PAUSED
    )
    assert (
        enabled.resume(job.job_id, stages, worker_id="worker").state
        == JobState.COMPLETE
    )


def test_failure_can_retry_without_reusing_changed_stage_definition(
    tmp_path: Path,
) -> None:
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
        connection.execute(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
    with pytest.raises(UnsupportedSchema, match="unsupported"):
        JobStore(bad)


@pytest.mark.parametrize(
    "point",
    [
        "before_snapshot_capture",
        "after_snapshot_seal",
        "after_snapshot_attach",
        "after_snapshot_validating",
    ],
)
def test_submission_crash_boundaries_recover_on_resubmit(
    tmp_path: Path, point: str
) -> None:
    source, files = _source(tmp_path)
    request = _request(source, files)
    state = tmp_path / "state"

    def crash(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    runner = Runner(state, synthetic_enabled=True, fault_injector=crash)
    with pytest.raises(InjectedCrash):
        runner.submit(request, source, files)
    pending = runner.store.submit(request)
    if point == "before_snapshot_capture":
        incomplete = runner.snapshots_dir / f".{pending.job_id}.dead.tmp"
        incomplete.mkdir()
        (incomplete / "partial").write_bytes(b"partial")
    recovered = Runner(state, synthetic_enabled=True)
    job = recovered.submit(request, source, files)
    assert job.job_id == pending.job_id
    assert job.state == JobState.READY
    if point == "before_snapshot_capture":
        assert not incomplete.exists()
        assert any(recovered.quarantine_dir.iterdir())
    assert (
        recovered.execute(job.job_id, [_stage()], worker_id="resumed").state
        == JobState.COMPLETE
    )


@pytest.mark.parametrize(
    "point",
    ["after_snapshot_seal", "after_snapshot_attach", "after_snapshot_validating"],
)
def test_recover_finishes_sealed_submission_without_source(
    tmp_path: Path, point: str
) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)

    def crash(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    runner.fault_injector = crash
    request = _request(source, files)
    with pytest.raises(InjectedCrash):
        runner.submit(request, source, files)
    job = runner.store.submit(request)
    runner.fault_injector = None
    (source / files[0]).unlink()
    runner.recover(job.job_id)
    assert runner.status(job.job_id).state == JobState.READY


def test_recovery_quarantine_holds_lease_grant_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, files = _source(tmp_path)
    clock = FakeClock()
    runner = Runner(tmp_path / "state", clock=clock, synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    runner.store.transition(job.job_id, JobState.RUNNING, "test")
    old = runner.store.acquire_lease(job.job_id, "measure", "a" * 64, "old", 1)
    directory = runner.attempts_dir / job.job_id / f"measure-{old.token}"
    directory.mkdir(parents=True)
    clock.now += 2
    grant_started = Event()
    grant_finished = Event()
    real_quarantine = runner._quarantine
    futures = []

    def grant():
        grant_started.set()
        lease = runner.store.acquire_lease(job.job_id, "measure", "a" * 64, "new", 10)
        current = runner.attempts_dir / job.job_id / f"measure-{lease.token}"
        current.mkdir()
        grant_finished.set()
        return lease, current

    with ThreadPoolExecutor(max_workers=1) as pool:

        def quarantine(path, reason):
            futures.append(pool.submit(grant))
            assert grant_started.wait(5)
            assert not grant_finished.wait(0.1)
            return real_quarantine(path, reason)

        monkeypatch.setattr(runner, "_quarantine", quarantine)
        report = runner.recover(job.job_id)
        lease, current = futures[0].result(timeout=5)
    assert len(report.quarantined) == 1
    assert current.is_dir()
    assert lease.token > old.token
    with pytest.raises(StaleLease):
        runner.store.heartbeat(old, 10)
    assert runner.recover(job.job_id).quarantined == ()


def test_missing_attached_manifest_fails_closed_during_recovery(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    request = _request(source, files)

    def crash(point: str) -> None:
        if point == "after_snapshot_attach":
            raise InjectedCrash(point)

    runner.fault_injector = crash
    with pytest.raises(InjectedCrash):
        runner.submit(request, source, files)
    record = runner.store.submit(request)
    snapshot = runner.snapshots_dir / record.job_id
    snapshot.chmod(0o755)
    (snapshot / "input-manifest.local.json").unlink()
    runner.fault_injector = None
    with pytest.raises(SnapshotViolation, match="manifest"):
        runner.recover(record.job_id)
    assert runner.status(record.job_id).state == JobState.TERMINAL_FAILURE


def test_recovery_does_not_adopt_live_publication_before_commit(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    published = Event()
    finish = Event()

    def pause(point: str) -> None:
        if point == "after_publication":
            published.set()
            assert finish.wait(5)

    runner = Runner(tmp_path / "state", synthetic_enabled=True, fault_injector=pause)
    job = runner.submit(_request(source, files), source, files)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, job.job_id, [_stage()], worker_id="live")
        assert published.wait(5)
        report = runner.recover(job.job_id)
        assert report.adopted == ()
        assert report.quarantined == ()
        assert runner.store.get(job.job_id).lease_owner == "live"
        finish.set()
        assert future.result(timeout=5).state == JobState.COMPLETE


@pytest.mark.parametrize(
    "new_names",
    [
        ("measure", "validate"),
        ("validate", "technical_qc", "measure"),
        ("measure",),
    ],
)
def test_changed_stage_order_addition_and_removal_recompute_inputs(
    tmp_path: Path, new_names
) -> None:
    source, files = _source(tmp_path)
    calls = []
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)

    def stage(name):
        def callback(context):
            calls.append(name)
            predecessors = [p.name for p in context.prior_stage_dirs]
            (context.attempt_dir / "result.json").write_text(json.dumps(predecessors))
            return StageResult({"result": "result.json"})

        return StageSpec(name, "v1", callback)

    committed = 0

    def crash(point):
        nonlocal committed
        if point == "after_db_commit":
            committed += 1
            if committed == 2:
                raise InjectedCrash(point)

    runner.fault_injector = crash
    with pytest.raises(InjectedCrash):
        runner.execute(
            job.job_id, [stage("validate"), stage("measure")], worker_id="old"
        )
    runner.fault_injector = None
    calls.clear()
    result = runner.execute(job.job_id, [stage(n) for n in new_names], worker_id="new")
    assert result.state == JobState.COMPLETE
    expected = list(new_names[1:]) if new_names[0] == "validate" else list(new_names)
    assert calls == expected


@pytest.mark.parametrize(
    "corruption", ["extra-file", "extra-directory", "missing-manifest"]
)
def test_sealed_snapshot_inventory_changes_block_execution(
    tmp_path: Path, corruption: str
) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)
    root = runner.snapshots_dir / job.job_id
    root.chmod(0o755)
    if corruption == "extra-file":
        (root / "undeclared").write_bytes(b"unexpected")
    elif corruption == "extra-directory":
        (root / "undeclared").mkdir()
    else:
        (root / "input-manifest.local.json").unlink()
    with pytest.raises(SnapshotViolation):
        runner.execute(job.job_id, [_stage()], worker_id="worker")
    assert runner.status(job.job_id).state == JobState.TERMINAL_FAILURE


def test_callback_snapshot_mutation_cannot_publish(tmp_path: Path) -> None:
    source, files = _source(tmp_path)
    runner = Runner(tmp_path / "state", synthetic_enabled=True)
    job = runner.submit(_request(source, files), source, files)

    def mutate(context):
        input_file = context.sealed_input_dir / files[0]
        input_file.chmod(0o644)
        input_file.write_bytes(b"changed during callback")
        (context.attempt_dir / "out").write_bytes(b"invalid result")
        return StageResult({"result": "out"})

    with pytest.raises(SnapshotViolation, match="digest changed"):
        runner.execute(
            job.job_id, [StageSpec("measure", "v1", mutate)], worker_id="worker"
        )
    assert runner.status(job.job_id).state == JobState.TERMINAL_FAILURE
    assert not (runner.artifacts_dir / job.job_id).exists()


class _RenewalCounter:
    """Wrap ``JobStore.heartbeat`` to count renewals and failed renewals."""

    def __init__(self, store: JobStore) -> None:
        self.condition = Condition()
        self.renewed = 0
        self.failed = 0
        real = store.heartbeat

        def heartbeat(lease, seconds):
            try:
                result = real(lease, seconds)
            except BaseException:
                with self.condition:
                    self.failed += 1
                    self.condition.notify_all()
                raise
            with self.condition:
                self.renewed += 1
                self.condition.notify_all()
            return result

        store.heartbeat = heartbeat  # type: ignore[method-assign]

    def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        with self.condition:
            return self.condition.wait_for(predicate, timeout)


def _keeper_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "traceback-lease-keeper"]


def test_slow_snapshot_verification_keeps_the_lease_alive(tmp_path: Path) -> None:
    # The real-data failure: re-hashing a 2 GB sealed snapshot before and
    # after a stage outlasted the 30 s lease, because only stage callbacks
    # renewed it.  Here each verification spans three lease lengths on the
    # store clock and the callback never heartbeats.
    clock = FakeClock()
    lease = 5.0
    runner = Runner(
        tmp_path / "state",
        clock=clock,
        lease_seconds=lease,
        heartbeat_seconds=0.01,
        synthetic_enabled=True,
    )
    source, files = _source(tmp_path)
    job = runner.submit(_request(source, files), source, files)
    counter = _RenewalCounter(runner.store)
    real_verify = runner._verify_job_snapshot
    slow_verifications = []

    def slow_verify(job_id: str) -> None:
        real_verify(job_id)
        if runner.store.get(job_id).lease_owner is None:
            return  # execute()'s check before any lease; nothing to keep alive
        slow_verifications.append(job_id)
        for _ in range(6):
            seen = counter.renewed
            clock.now += lease / 2
            # Two renewals after the advance: the second started after it.
            assert counter.wait_for(lambda: counter.renewed >= seen + 2 or counter.failed)
            assert counter.failed == 0

    runner._verify_job_snapshot = slow_verify  # type: ignore[method-assign]

    record = runner.execute(job.job_id, [_stage()], worker_id="worker")

    assert record.state == JobState.COMPLETE
    assert len(slow_verifications) == 2  # before and after the callback
    assert counter.failed == 0
    assert not _keeper_threads()


def test_superseded_lease_still_stops_the_worker_before_publication(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    lease = 5.0
    runner = Runner(
        tmp_path / "state",
        clock=clock,
        lease_seconds=lease,
        heartbeat_seconds=0.01,
        synthetic_enabled=True,
    )
    source, files = _source(tmp_path)
    job = runner.submit(_request(source, files), source, files)
    counter = _RenewalCounter(runner.store)
    takeover = JobStore(runner.store.path, clock=clock)
    real_verify = runner._verify_job_snapshot
    verified_under_lease = []
    second = []

    def verify(job_id: str) -> None:
        if runner.store.get(job_id).lease_owner is not None:
            verified_under_lease.append(job_id)
        real_verify(job_id)

    def stalled_then_superseded(context: object) -> StageResult:
        # The worker stalls past its lease; a second runner takes the job over.
        clock.now += lease + 1
        second.append(
            takeover.acquire_lease(job.job_id, "measure", "a" * 64, "worker-b", lease)
        )
        # The keeper's next renewal meets the superseded fence and stops.
        assert counter.wait_for(lambda: counter.failed >= 1)
        (context.attempt_dir / "result.json").write_text("{}")  # type: ignore[attr-defined]
        return StageResult({"result": "result.json"})

    runner._verify_job_snapshot = verify  # type: ignore[method-assign]

    with pytest.raises(StaleLease):
        runner.execute(
            job.job_id,
            [StageSpec("measure", "v1", stalled_then_superseded)],
            worker_id="worker-a",
        )

    # The first worker stopped at the callback boundary: no post-callback
    # verification, no publication, and the second runner keeps the lease.
    assert len(verified_under_lease) == 1
    assert not (runner.artifacts_dir / job.job_id).exists()
    current = takeover.get(job.job_id)
    assert current.lease_owner == "worker-b"
    assert current.lease_token == second[0].token
    assert not _keeper_threads()


def test_heartbeat_interval_must_be_shorter_than_the_lease(tmp_path: Path) -> None:
    for name, interval in (("equal", 1), ("zero", 0)):
        with pytest.raises(ValueError, match="heartbeat_seconds"):
            Runner(
                tmp_path / name,
                lease_seconds=1,
                heartbeat_seconds=interval,
                synthetic_enabled=True,
            )
    assert Runner(tmp_path / "default", synthetic_enabled=True).heartbeat_seconds == 10.0
