"""The result envelope: strict shape, canonical bytes, committed JSON schema."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from traceback_lab.envelope import (
    SCHEMA_FILE,
    ResultEnvelope,
    json_schema,
    provenance,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64


def _stamp():
    return provenance(
        tool="traceback_lab.fragment_size",
        parameters_sha256=A,
        source_id=B,
        input_id=C,
        reference_id="hg38-test",
        timestamp_utc="2026-10-14T21:00:00Z",
    )


def _ok(**overrides):
    base = dict(
        algorithm_id="fragment-size",
        algorithm_version="1",
        status="ok",
        provenance=_stamp(),
        depth={"eligible_reads": 10, "approx_depth_x": 0.01},
        metrics=[{"metric_id": "mode_bp", "value": 166, "unit": "bp", "n": 10}],
        panels=[{"panel_id": "size-density", "kind": "density", "data_ref": "artifacts/size_hist.json"}],
    )
    base.update(overrides)
    return ResultEnvelope.model_validate(base)


def test_round_trip_is_byte_stable() -> None:
    envelope = _ok()
    content = envelope.to_bytes()
    assert json.loads(content)["schema"] == "traceback.lab.result.v1"
    assert ResultEnvelope.from_bytes(content).to_bytes() == content


def test_committed_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA_FILE.read_text())
    assert committed == json_schema()


def test_failed_needs_failure_and_no_metrics() -> None:
    with pytest.raises(ValidationError):
        _ok(status="failed")
    with pytest.raises(ValidationError):
        _ok(status="failed", failure={"first_line": "x", "stderr_tail": ["x"]})
    failed = _ok(
        status="failed",
        metrics=[],
        panels=[],
        failure={"first_line": "readCounter exited 1", "stderr_tail": ["a"], "exit_code": 1},
    )
    assert failed.failure is not None


def test_skipped_needs_a_known_reason() -> None:
    with pytest.raises(ValidationError):
        _ok(status="skipped", metrics=[], panels=[])
    with pytest.raises(ValidationError):
        _ok(status="skipped", skip_reason="bored", metrics=[], panels=[])
    skipped = _ok(status="skipped", skip_reason="depth", metrics=[], panels=[])
    assert skipped.skip_reason == "depth"
    with pytest.raises(ValidationError):
        _ok(skip_reason="depth")


@pytest.mark.parametrize("ref", ["../x.json", "/etc/passwd", "a/../../b", "a//b", ""])
def test_data_ref_must_be_contained(ref: str) -> None:
    with pytest.raises(ValidationError):
        _ok(panels=[{"panel_id": "p", "kind": "density", "data_ref": ref}])


def test_non_finite_values_are_refused() -> None:
    with pytest.raises(ValidationError):
        _ok(metrics=[{"metric_id": "x", "value": float("nan")}])


def test_duplicate_metric_ids_are_refused() -> None:
    with pytest.raises(ValidationError):
        _ok(metrics=[{"metric_id": "x", "value": 1}, {"metric_id": "x", "value": 2}])


def test_extra_fields_are_refused() -> None:
    data = json.loads(_ok().to_bytes())
    data["interpretation_version"] = "0.1.0"
    with pytest.raises(ValidationError):
        ResultEnvelope.model_validate(data)


def test_provenance_timestamp_format() -> None:
    with pytest.raises(ValidationError):
        provenance(
            tool="t",
            parameters_sha256=A,
            source_id=B,
            input_id=C,
            reference_id="r",
            timestamp_utc="2026-10-14 21:00",
        )


@pytest.mark.parametrize("field,value", [("tool", "/Users/x/tool"), ("reference_id", "../ref"), ("reference_id", "a b")])
def test_provenance_fields_are_identifiers_not_paths(field, value) -> None:
    kwargs = dict(tool="t", parameters_sha256=A, source_id=B, input_id=C, reference_id="ref", timestamp_utc="2026-10-14T21:00:00Z")
    kwargs[field] = value
    with pytest.raises(ValidationError):
        provenance(**kwargs)
