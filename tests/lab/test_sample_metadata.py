"""Local sex assertions are versioned metadata, not measurement identity."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from traceback_lab.problems import LabProblem
from traceback_lab.ingest import write_source
from traceback_lab.sample_metadata import SCHEMA, sex_assertion, sex_concordance, with_sex_assertion
from traceback_lab.store import LabStore
from traceback_lab.views import InputView, SampleView, sex_check_view


def test_versioned_assertion_is_immutable_and_refuses_conflicts() -> None:
    original = {"source_id": "a" * 64}
    recorded = with_sex_assertion(original, "M")
    assert original == {"source_id": "a" * 64}
    assert recorded["operator_metadata"]["schema"] == SCHEMA
    assert recorded["operator_metadata"]["sex"] == "M"
    assert with_sex_assertion(recorded, "M") is recorded
    assert with_sex_assertion(recorded, None) is recorded
    with pytest.raises(LabProblem, match="TBX-LAB-023"):
        with_sex_assertion(recorded, "F")
    with pytest.raises(LabProblem, match="TBX-LAB-023"):
        sex_assertion({"operator_metadata": {"sex": "M"}})


@pytest.mark.parametrize(
    ("declared", "observed", "state", "reason"),
    [
        (None, "male", "no_verdict", "no_assertion"),
        ("M", None, "no_verdict", "no_definite_call"),
        ("F", "indeterminate", "no_verdict", "no_definite_call"),
        ("M", "male", "concordant", None),
        ("F", "female", "concordant", None),
        ("M", "female", "mismatch", None),
        ("F", "male", "mismatch", None),
    ],
)
def test_read_time_concordance(declared, observed, state, reason) -> None:
    result = sex_concordance(declared, observed)
    assert result["state"] == state and result["reason"] == reason


def test_sample_view_uses_full_measured_call_without_changing_it() -> None:
    measurement = SimpleNamespace(
        status="ok", metrics=[SimpleNamespace(metric_id="call", value="female")],
    )
    full = InputView("full", "b" * 64, None, {}, {"sex-check": measurement})
    metadata = with_sex_assertion({}, "M")["operator_metadata"]
    sample = SampleView("a" * 64, "a" * 12, False, full, operator_metadata=metadata)
    assert sex_check_view(sample)["state"] == "mismatch"
    assert measurement.metrics[0].value == "female"
    measurement.status = "skipped"
    assert sex_check_view(sample)["state"] == "no_verdict"


def test_first_ingest_records_assertion_without_changing_source_or_input_id(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    source_id, input_id = "a" * 64, "b" * 64
    chunks = SimpleNamespace(chunks=[], excluded_indexes=[], basecall_model=None)
    aligned = SimpleNamespace(
        source_id=source_id, input_id=input_id,
        manifest={"kind": "prealigned", "tools": None, "reference_id": "synth"},
    )
    record = write_source(store, chunks, aligned, declared_sex="F")
    assert record["source_id"] == source_id
    assert record["inputs"][0]["input_id"] == input_id
    assert record["operator_metadata"]["sex"] == "F"
    rerun = write_source(store, chunks, aligned)
    assert rerun["operator_metadata"] == record["operator_metadata"]
