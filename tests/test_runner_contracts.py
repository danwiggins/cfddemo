"""Tests for the synthetic Traceback runner foundation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evidence_inspector.models import sha256_bytes
from traceback_runner.cli import main
from traceback_runner.contracts import (
    ArtifactCommitment,
    InputKind,
    JobRequest,
    JobState,
    PreflightCheck,
    PreflightOutcome,
    PreflightReport,
    job_key,
    validate_transition,
)


def _request(**updates: object) -> JobRequest:
    values = {
        "sample_token": "sample.synthetic",
        "input_kind": InputKind.MODBAM,
        "input_tree_sha256_local": sha256_bytes(b"input"),
        "workflow_release_sha256": sha256_bytes(b"workflow"),
    }
    values.update(updates)
    return JobRequest.model_validate(values)


def test_job_key_is_canonical_and_changes_with_execution_input() -> None:
    first = _request(execution_options={"threads": 2, "offline": True})
    reordered = _request(execution_options={"offline": True, "threads": 2})
    changed = _request(execution_options={"threads": 3, "offline": True})

    assert job_key(first) == job_key(reordered)
    assert job_key(first) != job_key(changed)


def test_runner_contracts_are_closed_and_export_commitment_has_no_locator() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _request(path="/private/sample.bam")

    artifact = ArtifactCommitment(
        role="analysis_input",
        size_bytes=12,
        provider_hmac_sha256="a" * 64,
    )
    serialized = artifact.model_dump_json()
    assert "path" not in serialized
    assert "sha256_local" not in serialized


def test_preflight_report_severity_must_match_checks() -> None:
    blocked = PreflightCheck(
        code="TBX-BAM-001",
        outcome=PreflightOutcome.BLOCKED,
        problem="The index is missing.",
        remediation="Place the qualified index beside the modBAM.",
        owner="sequencing operator",
        retryable=True,
    )
    report = PreflightReport(outcome=PreflightOutcome.BLOCKED, checks=(blocked,))
    assert report.outcome == PreflightOutcome.BLOCKED

    with pytest.raises(ValidationError, match="most severe"):
        PreflightReport(outcome=PreflightOutcome.PASS, checks=(blocked,))


def test_state_machine_rejects_skipping_validation_or_reopening_history() -> None:
    validate_transition(JobState.DISCOVERED, JobState.SNAPSHOTTING)
    validate_transition(JobState.COMPLETE, JobState.SUPERSEDED)

    with pytest.raises(ValueError, match="invalid job transition"):
        validate_transition(JobState.DISCOVERED, JobState.RUNNING)
    with pytest.raises(ValueError, match="invalid job transition"):
        validate_transition(JobState.COMPLETE, JobState.RUNNING)


def test_cli_doctor_and_demo_are_explicitly_synthetic(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    assert "Real-data execution is not enabled" in capsys.readouterr().out

    assert main(["demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "contract_validated"
    assert payload["real_data_enabled"] is False
