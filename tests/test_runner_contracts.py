"""Tests for the synthetic Traceback runner foundation."""

from __future__ import annotations

import json
from pathlib import Path

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
from traceback_runner import (
    CompatibilityItem,
    CompletionState,
    ExclusionCounts,
    FragmentMeasurement,
    HistogramCount,
    SyntheticBamKind,
    canonical_json_bytes,
    canonical_model_from_bytes,
    create_synthetic_bam,
    create_synthetic_minknow_run,
    synthetic_fragment_policy,
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
        artifact_token="synthetic-artifact",
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


def test_nested_contracts_are_immutable_and_nonfinite_is_rejected() -> None:
    request = _request(execution_options={"threads": 2, "offline": True})
    with pytest.raises(ValidationError, match="frozen"):
        request.execution_options.threads = 3  # type: ignore[misc]
    with pytest.raises(ValidationError, match="finite_number"):
        _request(execution_options={"threads": float("nan"), "offline": True})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"nested": [float("inf")]})


def test_canonical_round_trip_requires_exact_bytes_and_schema() -> None:
    request = _request(execution_options={"threads": 2, "offline": True})
    encoded = canonical_json_bytes(request)
    assert canonical_model_from_bytes(JobRequest, encoded) == request
    with pytest.raises(ValueError, match="not canonical"):
        canonical_model_from_bytes(JobRequest, json.dumps(request.model_dump(mode="json"), indent=2).encode())
    with pytest.raises(ValidationError, match="schema_version"):
        JobRequest.model_validate({**request.model_dump(mode="json"), "schema_version": "traceback.job-request.v2"})


def test_locked_measurement_contract_reconciles_and_has_unbounded_final_bin() -> None:
    policy = synthetic_fragment_policy()
    assert policy.consumed_cigar_operations == ("M", "D", "N", "=", "X")
    assert policy.bins[-1].upper_exclusive is None
    exclusions = ExclusionCounts(
        unmapped=1, secondary=1, supplementary=1, qc_failure=1,
        duplicate=1, low_mapping_quality=1, unregistered_contig=1,
        no_reference_span=0,
    )
    histogram = tuple(HistogramCount(bin=item, count=1 if index == 1 else 0) for index, item in enumerate(policy.bins))
    measurement = FragmentMeasurement(
        definition_id=policy.definition_id,
        reference_id=policy.reference_id,
        completion=CompletionState.COMPLETE,
        records_scanned=8,
        eligible_alignments=1,
        exclusions=exclusions,
        histogram=histogram,
    )
    assert measurement.records_scanned == 8
    encoded = canonical_json_bytes(measurement)
    assert canonical_model_from_bytes(FragmentMeasurement, encoded) == measurement
    with pytest.raises(ValidationError, match="cannot construct"):
        measurement.model_copy(update={"completion": CompletionState.CAPPED}).__class__.model_validate(
            {**measurement.model_dump(mode="json"), "completion": "capped"}
        )


@pytest.mark.parametrize(
    "bounds",
    [
        ((50, 100), (75, None)),
        ((0, 50), (75, None)),
        ((50, 100), (0, None)),
        ((0, 50), (50, 100)),
    ],
    ids=("overlap", "gap", "out-of-order", "finite-final"),
)
def test_measurement_rejects_noncanonical_histogram_structure(
    bounds: tuple[tuple[int, int | None], ...],
) -> None:
    histogram = tuple(
        HistogramCount(
            bin={"lower_inclusive": lower, "upper_exclusive": upper},
            count=1,
        )
        for lower, upper in bounds
    )
    exclusions = ExclusionCounts(
        unmapped=0,
        secondary=0,
        supplementary=0,
        qc_failure=0,
        duplicate=0,
        low_mapping_quality=0,
        unregistered_contig=0,
        no_reference_span=0,
    )
    with pytest.raises(ValidationError, match="histogram bin"):
        FragmentMeasurement(
            definition_id="aligned-reference-span.synthetic.v1",
            reference_id="synthetic-reference.v1",
            completion=CompletionState.COMPLETE,
            records_scanned=len(bounds),
            eligible_alignments=len(bounds),
            exclusions=exclusions,
            histogram=histogram,
        )


@pytest.mark.parametrize("kind", list(SyntheticBamKind))
def test_synthetic_bam_fixtures_are_runtime_only_and_exact(tmp_path: Path, kind: SyntheticBamKind) -> None:
    fixture = create_synthetic_bam(tmp_path / kind.value, kind)
    assert fixture.bam_path.is_file()
    assert fixture.registered_reference.reference_id == fixture.reference_id
    if kind == SyntheticBamKind.CORRUPT:
        assert fixture.index_path is None
    else:
        assert fixture.index_path is not None and fixture.index_path.is_file()


def test_synthetic_minknow_fixture_contains_metadata_not_signal(tmp_path: Path) -> None:
    root = create_synthetic_minknow_run(tmp_path)
    assert (root / "sample_sheet.json").is_file()
    assert list((root / "pod5").glob("*.pod5")) == []


def test_unapproved_wet_lab_instruction_fails_closed() -> None:
    values = {
        "category": "collection",
        "item_id": "synthetic-step",
        "display_name": "Synthetic withheld step",
        "description": "Not an instruction for real work.",
        "status": "required",
        "instruction_kind": "wet_lab_instruction",
        "rendering": "withhold",
        "protocol_version": "synthetic-protocol.v1",
        "owner": "synthetic scientific owner",
        "source": "synthetic fixture source",
        "source_version": "fixture.v1",
        "last_reviewed": "2026-09-26",
    }
    assert CompatibilityItem.model_validate(values).rendering == "withhold"
    with pytest.raises(ValidationError, match="must be withheld"):
        CompatibilityItem.model_validate({**values, "rendering": "display"})
