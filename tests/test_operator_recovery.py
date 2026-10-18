"""Operator recovery and record-publication regressions with synthetic data only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from traceback_runner import cli
from traceback_runner.bundles import verify_bundle
from traceback_runner.contracts import ExecutionOptions, InputKind, JobRequest, JobState
from traceback_runner.runner import InjectedCrash, Runner
from traceback_runner.signing import (
    DevelopmentTrustDocument, KeyPurpose, development_trust_bytes,
    development_trust_document_bytes, generate_development_keypair,
    load_development_trust,
)
from traceback_runner.snapshots import input_tree_sha256


def invoke(capsys, *args):
    code = cli.main([*map(str, args), "--json"])
    return code, json.loads(capsys.readouterr().out)


def demo_job(root):
    source, files = cli._ensure_synthetic_input(root)
    request = JobRequest(
        sample_token="synthetic-sample-token", input_kind=InputKind.MODBAM,
        input_tree_sha256_local=input_tree_sha256(source, files),
        workflow_release_sha256=hashlib.sha256(cli._WORKFLOW_ID.encode()).hexdigest(),
    )
    runner = Runner(root / "runner", synthetic_enabled=True)
    return runner, runner.submit(request, source, files)


def test_fresh_status_of_old_complete_and_paused_jobs(tmp_path, capsys):
    code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    job_id = result["data"]["job_id"]
    with sqlite3.connect(tmp_path / "runner/runner.sqlite3") as db:
        db.execute("UPDATE jobs SET updated_at=?", (time.time() - 600,))
    for _ in range(2):
        code, status = invoke(capsys, "status", job_id, "--root", tmp_path)
        view = status["data"]["operator_state"]
        assert code == 0 and not view["stale"]
        assert view["headline"] == "Signed local record ready"
    with sqlite3.connect(tmp_path / "runner/runner.sqlite3") as db:
        db.execute("UPDATE jobs SET state='paused'")
    _, status = invoke(capsys, "status", job_id, "--root", tmp_path)
    assert status["data"]["operator_state"]["headline"] == "Job paused"


def test_resume_rejects_live_worker_then_recovers_expired_crash(tmp_path, capsys):
    runner, job = demo_job(tmp_path)
    key = generate_development_keypair(KeyPurpose.RESULT)
    cli._append_development_trust(tmp_path / cli._TRUST_RELATIVE, development_trust_bytes(key))
    def crash(point):
        if point == "after_publication":
            raise InjectedCrash()
    runner.fault_injector = crash
    with pytest.raises(InjectedCrash):
        runner.execute(job.job_id, cli._demo_stages(key), worker_id="crashed")
    before = (tmp_path / cli._TRUST_RELATIVE).read_bytes()
    code, result = invoke(capsys, "resume", job.job_id, "--root", tmp_path)
    assert code == cli.ExitCode.BLOCKED
    assert result["data"]["code"] == "TBX-JOB-002"
    assert result["data"]["job_id"] == job.job_id
    assert "unexpired" in result["data"]["cause"]
    assert (tmp_path / cli._TRUST_RELATIVE).read_bytes() == before
    with sqlite3.connect(runner.store.path) as db:
        db.execute("UPDATE jobs SET lease_expires_at=?", (time.time() - 1,))
    code, result = invoke(capsys, "resume", job.job_id, "--root", tmp_path)
    assert code == 0 and result["data"]["state"] == "complete"
    trust = load_development_trust((tmp_path / cli._TRUST_RELATIVE).read_bytes())
    verify_bundle(tmp_path / result["data"]["bundle"], trust)


def test_retry_identifies_resume_then_resume_completes(tmp_path, capsys):
    runner, job = demo_job(tmp_path)
    runner.store.transition(job.job_id, JobState.RUNNING, "synthetic start")
    runner.store.transition(job.job_id, JobState.RETRYABLE_FAILURE, "synthetic failure")
    code, result = invoke(capsys, "retry", job.job_id, "--root", tmp_path)
    assert code == 0 and "resume" in result["data"]["next_action"]
    _, status = invoke(capsys, "status", job.job_id, "--root", tmp_path)
    assert "resume" in status["data"]["operator_state"]["next_action"]
    code, result = invoke(capsys, "resume", job.job_id, "--root", tmp_path)
    assert code == 0 and result["data"]["state"] == "complete"


def test_second_job_preserves_first_records_trust(tmp_path, capsys):
    code, first = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    runner = Runner(tmp_path / "runner", synthetic_enabled=True)
    request = runner.store.request(first["data"]["job_id"]).model_copy(
        update={"execution_options": ExecutionOptions(threads=2)}
    )
    source, files = cli._ensure_synthetic_input(tmp_path)
    second = runner.submit(request, source, files)
    runner.store.transition(second.job_id, JobState.RUNNING, "synthetic start")
    runner.store.transition(second.job_id, JobState.RETRYABLE_FAILURE, "synthetic retry")
    code, result = invoke(capsys, "resume", second.job_id, "--root", tmp_path)
    assert code == 0
    trust_path = tmp_path / cli._TRUST_RELATIVE
    trust = load_development_trust(trust_path.read_bytes())
    verify_bundle(tmp_path / first["data"]["bundle"], trust)
    verify_bundle(tmp_path / result["data"]["bundle"], trust)
    assert len(DevelopmentTrustDocument.model_validate_json(trust_path.read_bytes()).keys) == 2


def test_append_trust_retains_revocation_and_rejects_bad_existing_bytes(tmp_path):
    path = tmp_path / "trust.json"
    old, new = (generate_development_keypair(KeyPurpose.RESULT) for _ in range(2))
    document = DevelopmentTrustDocument.model_validate_json(development_trust_bytes(old))
    document = document.model_copy(update={"keys": (document.keys[0].model_copy(update={"revoked": True}),)})
    path.write_bytes(development_trust_document_bytes(document))
    cli._append_development_trust(path, development_trust_bytes(new))
    actual = DevelopmentTrustDocument.model_validate_json(path.read_bytes())
    assert next(key for key in actual.keys if key.key_id == old.key_id).revoked
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        cli._append_development_trust(path, development_trust_bytes(new))
    assert path.read_bytes() == b"invalid"


def test_interrupted_copy_exposes_no_partial_final_then_retry_recovers(tmp_path, capsys):
    def fail_copy(source, destination, **kwargs):
        (destination / "partial").write_text("partial")
        raise OSError("simulated storage failure")
    with patch.object(cli.shutil, "copytree", fail_copy):
        code, _ = invoke(capsys, "demo", "--root", tmp_path)
    assert code == cli.ExitCode.RETRYABLE_FAILURE
    assert list((tmp_path / "records").iterdir()) == []
    code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    assert len(list((tmp_path / "records").iterdir())) == 1


def test_published_record_keeps_signed_bytes_and_sealed_descendants(tmp_path, capsys):
    code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    published = tmp_path / result["data"]["bundle"]
    runner = Runner(tmp_path / "runner", synthetic_enabled=True)
    source = cli._signed_bundle_from_outputs(runner, result["data"]["job_id"])
    assert stat.S_IMODE(published.lstat().st_mode) == 0o555
    for member in source.rglob("*"):
        relative = member.relative_to(source)
        copy = published / relative
        expected = 0o555 if member.is_dir() else 0o444
        assert stat.S_IMODE(copy.lstat().st_mode) == expected
        if member.is_file():
            assert copy.read_bytes() == member.read_bytes()
    trust = load_development_trust((tmp_path / cli._TRUST_RELATIVE).read_bytes())
    verify_bundle(published, trust)


@pytest.mark.parametrize("crash_point", ["after_rename", "before_parent_fsync"])
def test_process_crash_during_record_publication_reuses_and_syncs(
    tmp_path, capsys, crash_point,
):
    script = """
import os
import sys
from pathlib import Path
from traceback_runner import cli

root, point = Path(sys.argv[1]), sys.argv[2]
if point == "after_rename":
    real_rename = cli._rename_directory_exclusive
    def crash(source, destination):
        real_rename(source, destination)
        os._exit(81)
    cli._rename_directory_exclusive = crash
else:
    real_fsync = cli._fsync_directory
    def crash(path):
        if path == root / "records":
            os._exit(82)
        real_fsync(path)
    cli._fsync_directory = crash
cli.main(["demo", "--root", str(root), "--json"])
raise AssertionError("crash injection did not fire")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), crash_point],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == (81 if crash_point == "after_rename" else 82), (
        completed.stdout + completed.stderr
    )
    records = tmp_path / "records"
    candidates = list(records.iterdir())
    assert len(candidates) == 1
    published = candidates[0]
    assert stat.S_IMODE(published.lstat().st_mode) == (
        0o700 if crash_point == "after_rename" else 0o555
    )
    signed_bytes = {
        path.relative_to(published): path.read_bytes()
        for path in published.rglob("*") if path.is_file()
    }
    real_fsync = cli._fsync_directory
    synced = []
    def observe(path):
        synced.append(path)
        real_fsync(path)
    with patch.object(cli, "_fsync_directory", observe):
        code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    assert tmp_path / result["data"]["bundle"] == published
    assert list(records.iterdir()) == [published]
    assert stat.S_IMODE(published.lstat().st_mode) == 0o555
    assert {
        path.relative_to(published): path.read_bytes()
        for path in published.rglob("*") if path.is_file()
    } == signed_bytes
    assert published in synced and records in synced
    trust = load_development_trust((tmp_path / cli._TRUST_RELATIVE).read_bytes())
    verify_bundle(published, trust)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_writable_descendant_is_preserved_and_recovered_sibling_is_sealed(
    tmp_path, capsys, kind,
):
    _, first = invoke(capsys, "demo", "--root", tmp_path)
    original = tmp_path / first["data"]["bundle"]
    member = next(path for path in original.rglob("*") if (
        path.is_file() if kind == "file" else path.is_dir()
    ))
    writable_mode = 0o644 if kind == "file" else 0o755
    member.chmod(writable_mode)
    code, second = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    recovered = tmp_path / second["data"]["bundle"]
    assert recovered != original
    assert stat.S_IMODE(member.lstat().st_mode) == writable_mode
    assert stat.S_IMODE(recovered.lstat().st_mode) == 0o555
    assert stat.S_IMODE((recovered / member.relative_to(original)).lstat().st_mode) == (
        0o444 if kind == "file" else 0o555
    )


def test_recovered_record_seals_copied_descendants_from_writable_source(tmp_path, capsys):
    _, first = invoke(capsys, "demo", "--root", tmp_path)
    original = tmp_path / first["data"]["bundle"]
    runner = Runner(tmp_path / "runner", synthetic_enabled=True)
    source = cli._signed_bundle_from_outputs(runner, first["data"]["job_id"])
    source_file = next(path for path in source.rglob("*") if path.is_file())
    source_file.chmod(0o644)
    original_file = original / source_file.relative_to(source)
    original_file.chmod(0o644)

    code, second = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    recovered = tmp_path / second["data"]["bundle"]
    assert recovered != original
    assert stat.S_IMODE(source_file.lstat().st_mode) == 0o644
    assert stat.S_IMODE(original_file.lstat().st_mode) == 0o644
    assert stat.S_IMODE((recovered / source_file.relative_to(source)).lstat().st_mode) == 0o444
    trust = load_development_trust((tmp_path / cli._TRUST_RELATIVE).read_bytes())
    verify_bundle(recovered, trust)

    code, third = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0 and third["data"]["bundle"] == second["data"]["bundle"]
    assert len(list((tmp_path / "records").iterdir())) == 2


def test_matching_candidate_fsync_failure_propagates_without_sibling(tmp_path, capsys):
    _, first = invoke(capsys, "demo", "--root", tmp_path)
    published = tmp_path / first["data"]["bundle"]
    with patch.object(cli, "_fsync_directory", side_effect=OSError("sync failed")):
        code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == cli.ExitCode.RETRYABLE_FAILURE
    assert result["data"]["code"] == "TBX-JOB-001"
    assert result["data"]["retryable"]
    assert list((tmp_path / "records").iterdir()) == [published]


def test_new_publication_fsync_failure_reuses_sealed_record(tmp_path, capsys):
    real_fsync = cli._fsync_directory
    records = tmp_path / "records"
    def fail_destination_sync(path):
        if path.parent == records and not path.name.startswith(".record-"):
            raise OSError("destination sync failed")
        real_fsync(path)
    with patch.object(cli, "_fsync_directory", fail_destination_sync):
        code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == cli.ExitCode.RETRYABLE_FAILURE
    assert result["data"]["retryable"]
    published = next(records.iterdir())
    assert stat.S_IMODE(published.lstat().st_mode) == 0o555
    code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    assert tmp_path / result["data"]["bundle"] == published
    assert list(records.iterdir()) == [published]


def test_publication_collision_preserves_winner_and_cleans_staging(tmp_path, capsys):
    real_rename = cli._rename_directory_exclusive
    def collide(source, destination):
        destination.mkdir()
        real_rename(source, destination)
    with patch.object(cli, "_rename_directory_exclusive", collide):
        code, _ = invoke(capsys, "demo", "--root", tmp_path)
    assert code == cli.ExitCode.RETRYABLE_FAILURE
    records = tmp_path / "records"
    assert len(list(records.iterdir())) == 1
    assert list(next(records.iterdir()).iterdir()) == []
    assert not list(records.glob(".record-*"))
    code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0
    assert len(list(records.iterdir())) == 2
    assert "-recovered-" in result["data"]["bundle"]


def test_invalid_existing_record_preserved_and_valid_sibling_reused(tmp_path, capsys):
    _, first = invoke(capsys, "demo", "--root", tmp_path)
    original = tmp_path / first["data"]["bundle"]
    # Select a real existing member; content must remain untouched by recovery.
    victim = next(path for path in original.iterdir() if path.is_file())
    victim.chmod(0o644)
    victim.write_bytes(b"corrupted user-visible output")
    code, second = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0 and second["data"]["bundle"] != first["data"]["bundle"]
    assert victim.read_bytes() == b"corrupted user-visible output"
    code, third = invoke(capsys, "demo", "--root", tmp_path)
    assert code == 0 and third["data"]["bundle"] == second["data"]["bundle"]


def test_exclusive_publication_never_replaces_existing_empty_directory(tmp_path):
    source, destination = tmp_path / "private", tmp_path / "final"
    source.mkdir()
    destination.mkdir()
    (source / "record").write_text("complete")
    with pytest.raises(FileExistsError):
        cli._rename_directory_exclusive(source, destination)
    assert (source / "record").read_text() == "complete"
    assert list(destination.iterdir()) == []


def test_workspace_lock_blocks_another_cli_mutation(tmp_path, capsys):
    with cli._operator_lock(tmp_path):
        code, result = invoke(capsys, "demo", "--root", tmp_path)
    assert code == cli.ExitCode.BLOCKED
    assert not (tmp_path / "runner").exists()


def test_resume_rejects_non_demo_workflow(tmp_path, capsys):
    runner, job = demo_job(tmp_path)
    with sqlite3.connect(runner.store.path) as db:
        request = runner.store.request(job.job_id).model_copy(update={"workflow_release_sha256": "b" * 64})
        db.execute("UPDATE jobs SET request_json=?,state='queued'", (request.model_dump_json(),))
    code, result = invoke(capsys, "resume", job.job_id, "--root", tmp_path)
    assert code == cli.ExitCode.BLOCKED
    assert "registered synthetic" in result["summary"]
    assert not (tmp_path / cli._TRUST_RELATIVE).exists()
