"""Tests for the synthetic-only executor comparison harness."""

from __future__ import annotations

import json

import pytest

from tests.executor.conformance import (
    Candidate,
    EvidenceKind,
    ExecutorRequestFixture,
    MountFixture,
    RuntimeState,
    inventory_runtimes,
    main,
    request_fixture,
    run_comparison,
)


def test_all_candidates_run_identical_nonqualifying_fixture_traces() -> None:
    report = run_comparison()

    assert report.synthetic_only is True
    assert report.real_data_authorized is False
    assert len(report.traces) == len(Candidate) * 3
    assert {trace.scenario for trace in report.traces} == {
        "complete",
        "changed_input_rejected",
        "offline_restart_after_publication",
    }
    assert all(trace.passed for trace in report.traces)
    assert all(trace.evidence == EvidenceKind.OBSERVED for trace in report.traces)
    assert all(trace.executor_exercised is False for trace in report.traces)
    assert all(trace.qualified is False for trace in report.traces)


def test_fixture_request_cannot_authorize_real_execution() -> None:
    fixture = request_fixture(Candidate.DIRECT_OCI, "a" * 64)

    assert fixture.synthetic_only is True
    assert fixture.real_data_authorized is False
    assert fixture.network == "none"
    assert sum(not mount.read_only for mount in fixture.mounts) == 1


def test_fixture_request_rejects_second_writable_mount() -> None:
    fixture = request_fixture(Candidate.DIRECT_OCI, "a" * 64)

    with pytest.raises(ValueError, match="exactly one writable attempt"):
        ExecutorRequestFixture(
            **{
                **fixture.__dict__,
                "mounts": (
                    *fixture.mounts,
                    MountFixture("scratch", "/scratch", False),
                ),
            }
        )


def test_runtime_inventory_is_observation_not_qualification() -> None:
    observations = inventory_runtimes()

    assert {item.candidate for item in observations} == set(Candidate)
    assert all(item.evidence == EvidenceKind.OBSERVED for item in observations)
    assert all(item.state in set(RuntimeState) for item in observations)
    assert all(item.reason for item in observations)


def test_json_cli_is_canonical_and_explicitly_nonqualifying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json"]) == 0
    raw = capsys.readouterr().out.strip()
    payload = json.loads(raw)

    assert raw == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert payload["synthetic_only"] is True
    assert payload["real_data_authorized"] is False
    assert not any(trace["executor_exercised"] for trace in payload["traces"])
    assert not any(trace["qualified"] for trace in payload["traces"])
