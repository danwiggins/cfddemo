"""Operator recovery and record-publication regressions with synthetic data only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
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
    assert "unexpired" in result["summary"]
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
