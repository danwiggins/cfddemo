"""Tests for the synthetic-only executor comparison harness."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Sequence

import pytest

from tests.executor.conformance import (
    Candidate,
    EvidenceKind,
    ExecutorRequestFixture,
    MountFixture,
    RuntimeObservation,
    RuntimeState,
    inventory_runtimes,
    main,
    request_fixture,
    run_comparison,
)


@pytest.fixture(autouse=True)
def _forbid_live_runtime_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_probe(_command: str, _arguments: Sequence[str]) -> tuple[bool, str]:
        raise AssertionError("offline tests must inject runtime observations or probes")

    monkeypatch.setattr(
        "tests.executor.conformance._command_version", forbidden_probe
    )


def _offline_runtime_observations() -> tuple[RuntimeObservation, ...]:
    return tuple(
        RuntimeObservation(
            candidate=candidate,
            state=RuntimeState.UNTESTED,
            evidence=EvidenceKind.OBSERVED,
            reason="hermetic test observation; no installed command invoked",
            commands=(),
        )
        for candidate in Candidate
    )


def test_all_candidates_run_identical_nonqualifying_fixture_traces() -> None:
    report = run_comparison(_offline_runtime_observations())

    assert report.synthetic_only is True
    assert report.real_data_authorized is False
    assert len(report.traces) == len(Candidate) * 3
    assert {trace.scenario for trace in report.traces} == {
        "complete",
        "changed_input_rejected",
        "runner_reinstantiation_after_publication",
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


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"synthetic_only": False}, "synthetic-only"),
        ({"real_data_authorized": True}, "cannot authorize real data"),
        ({"network": "default"}, "network must remain disabled"),
    ),
)
def test_fixture_request_rejects_forbidden_execution_authority(
    changes: dict[str, object], message: str
) -> None:
    fixture = request_fixture(Candidate.DIRECT_OCI, "a" * 64)

    with pytest.raises(ValueError, match=message):
        replace(fixture, **changes)


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
    calls: list[tuple[str, tuple[str, ...]]] = []

    def probe(command: str, arguments: Sequence[str]) -> tuple[bool, str]:
        argv = tuple(arguments)
        calls.append((command, argv))
        return True, f"synthetic {command} CLI"

    observations = inventory_runtimes(probe=probe, host_system="Linux")

    assert {item.candidate for item in observations} == set(Candidate)
    assert all(item.evidence == EvidenceKind.OBSERVED for item in observations)
    assert all(item.state in set(RuntimeState) for item in observations)
    assert all(item.reason for item in observations)
    direct = next(item for item in observations if item.candidate == Candidate.DIRECT_OCI)
    assert direct.state == RuntimeState.UNTESTED
    assert "CLI prerequisite detected" in direct.reason
    assert "rootless execution was not exercised" in direct.reason
    assert {command for command, _ in calls} == {
        "epi2me",
        "docker",
        "podman",
        "nextflow",
        "java",
    }


def test_json_cli_is_canonical_and_explicitly_nonqualifying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(
        ["--json"], runtime_observations=_offline_runtime_observations()
    ) == 0
    raw = capsys.readouterr().out.strip()
    payload = json.loads(raw)

    assert raw == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert payload["synthetic_only"] is True
    assert payload["real_data_authorized"] is False
    assert not any(trace["executor_exercised"] for trace in payload["traces"])
    assert not any(trace["qualified"] for trace in payload["traces"])
