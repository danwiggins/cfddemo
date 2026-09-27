"""Operator-state and diagnostic privacy tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from traceback_runner.contracts import JobState
from traceback_runner.operator import (
    OperatorBlocker,
    RecordAvailability,
    build_job_view,
    support_payload,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_complete_is_not_ready_until_signature_is_independently_verified() -> None:
    unverified = build_job_view(
        job_id="job.synthetic",
        state=JobState.COMPLETE,
        observed_at=NOW,
        now=NOW,
    )
    verified = build_job_view(
        job_id="job.synthetic",
        state=JobState.COMPLETE,
        observed_at=NOW,
        now=NOW,
        signature_verified=True,
    )

    assert unverified.record_availability == RecordAvailability.VERIFYING
    assert unverified.headline != "Signed local record ready"
    assert verified.record_availability == RecordAvailability.READY
    assert verified.headline == "Signed local record ready"


def test_stale_status_is_distinct_from_job_failure() -> None:
    view = build_job_view(
        job_id="job.synthetic",
        state=JobState.RUNNING,
        observed_at=NOW - timedelta(seconds=46),
        now=NOW,
    )

    assert view.stale is True
    assert view.headline == "Runner status is stale"
    assert view.blocker is None


def test_actionable_blocker_controls_next_action() -> None:
    blocker = OperatorBlocker(
        code="TBX-SYS-001",
        problem="Synthetic workspace is full",
        likely_cause="The configured test capacity is too small",
        exact_fix="Free space in the configured synthetic workspace",
        owner="Local operator",
        retryable=True,
        docs_path="docs/OPERATOR-GUIDE.md",
    )
    view = build_job_view(
        job_id="job.synthetic",
        state=JobState.RETRYABLE_FAILURE,
        observed_at=NOW,
        now=NOW,
        blocker=blocker,
    )

    assert view.next_action == blocker.exact_fix
    assert view.blocker.code == "TBX-SYS-001"


def test_support_payload_redacts_forbidden_fields_and_absolute_paths() -> None:
    forbidden = (
        "/private/clinical/sample-a.bam",
        "sample-a",
        "read-1234",
        "ACGTACGT",
        "secret-value",
        "a" * 64,
    )
    payload = support_payload(
        job_state=JobState.RETRYABLE_FAILURE,
        error_codes=["TBX-JOB-001"],
        runner_version="0.1.0",
        events=[
            {
                "message": "failed at /private/clinical/sample-a.bam",
                "detail": "sequence ACGTACGT from read-1234",
                "sample_id": forbidden[1],
                "read_id": forbidden[2],
                "sequence": forbidden[3],
                "secret": forbidden[4],
                "input_sha256": forbidden[5],
            }
        ],
    )
    serialized = json.dumps(payload, sort_keys=True)

    for value in forbidden:
        assert value not in serialized
    assert "TBX-JOB-001" in serialized
    assert payload["events"] == [{}]


def test_operator_state_fixture_contains_expected_release_one_states() -> None:
    fixture = Path(__file__).parent / "fixtures" / "operator_states.json"
    states = json.loads(fixture.read_text(encoding="utf-8"))

    assert {
        "empty",
        "queued",
        "processing",
        "stale",
        "paused",
        "retryable_failure",
        "complete_unverified",
        "complete_verified",
    } <= set(states)
    assert states["complete_verified"]["headline"] == "Signed local record ready"
    assert states["complete_unverified"]["record_availability"] == "verifying"
