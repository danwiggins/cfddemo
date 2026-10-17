"""Local sex assertions are versioned metadata, not measurement identity."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.algorithms.sex_check import call_sex
from traceback_lab.envelope import Metric, Provenance, ResultEnvelope
from traceback_lab.ingest import write_source
from traceback_lab.problems import LabProblem
from traceback_lab.sample_metadata import SCHEMA, sex_assertion, sex_concordance, with_sex_assertion
from traceback_lab.store import LabStore
from traceback_lab.views import InputView, SampleView, load_sample, sex_check_view
from traceback_lab.web.api import LabApi


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
    with pytest.raises(LabProblem, match="TBX-LAB-023"):
        with_sex_assertion({"operator_metadata": {"sex": "M"}}, "M")
    malformed = {"operator_metadata": {"sex": "M"}}
    assert with_sex_assertion(malformed, None) is malformed
    for timestamp in ("2026-02-30T00:00:00+00:00", "2026-10-16T00:00:00+01:00", "yesterday"):
        with pytest.raises(LabProblem, match="TBX-LAB-023"):
            sex_assertion({"operator_metadata": {**recorded["operator_metadata"], "recorded_utc": timestamp}})
    assert sex_assertion({"operator_metadata": {**recorded["operator_metadata"],
                                           "recorded_utc": "2026-10-16T00:00:00Z"}}) is not None


@pytest.mark.parametrize(
    ("declared", "observed", "state", "reason"),
    [
        (None, "male", "no_verdict", "no_assertion"),
        ("M", None, "no_verdict", "no_measurement"),
        ("F", "indeterminate", "no_verdict", "indeterminate_call"),
        ("M", "unexpected", "no_verdict", "invalid_call"),
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
    assert sex_check_view(sample)["reason"] == "no_measurement"


def test_real_sex_check_call_vocabulary_is_compared_without_reinterpreting_it() -> None:
    assert sex_concordance("F", call_sex(0.049))["state"] == "concordant"
    assert sex_concordance("M", call_sex(0.31))["state"] == "concordant"
    assert sex_concordance("M", call_sex(0.0))["state"] == "mismatch"
    for ratio in (None, 0.05, 0.3):
        expected = "no_measurement" if ratio is None else "indeterminate_call"
        assert sex_concordance("M", call_sex(ratio))["reason"] == expected

    measured = ResultEnvelope(
        algorithm_id="sex-check", algorithm_version="1", status="ok",
        provenance=Provenance(
            tool="test", tool_version="1", parameters_sha256="c" * 64,
            source_id="a" * 64, input_id="b" * 64, reference_id="synth",
            timestamp_utc="2026-10-16T00:00:00Z",
        ),
        metrics=[Metric(metric_id="call", value=call_sex(0.31))],
    )
    full = InputView("full", "b" * 64, None, {}, {"sex-check": measured})
    metadata = with_sex_assertion({}, "F")["operator_metadata"]
    sample = SampleView("a" * 64, "a" * 12, False, full, operator_metadata=metadata)
    assert sex_check_view(sample) == {
        "state": "mismatch", "reason": None, "declared": "F", "observed": "male",
    }


def test_malformed_optional_metadata_keeps_sample_list_and_measurements_available(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    input_id = "b" * 64
    manifest = store.inputs_dir / input_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind": "prealigned"}')
    store.sources_dir.mkdir(parents=True)
    for source_id, metadata in (("a" * 64, {"sex": "M"}), ("c" * 64, None)):
        record = {"source_id": source_id, "inputs": [{"input_id": input_id, "used_utc": "2026-10-16T00:00:00Z"}]}
        if metadata is not None:
            record["operator_metadata"] = metadata
        (store.sources_dir / f"{source_id}.json").write_text(json.dumps(record))
    api = LabApi(store)
    listing = api.samples()["samples"]
    assert {row["id12"] for row in listing} == {"a" * 12, "c" * 12}
    bad = load_sample(store, "a" * 64)
    assert bad is not None and bad.full.input_id == input_id
    check = sex_check_view(bad)
    assert check["state"] == "no_verdict" and check["reason"] == "malformed_metadata"
    assert check["problem"]["code"] == "TBX-LAB-023"
    assert "Restore" in check["problem"]["fix"]
    assert api.sample("a" * 12)["preanalytic_sex"] == check
    assert api.sample("c" * 12)["preanalytic_sex"]["reason"] == "no_assertion"


def test_first_ingest_records_assertion_without_changing_source_or_input_id(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    source_id, input_id = "a" * 64, "b" * 64
    chunks = SimpleNamespace(chunks=[], excluded_indexes=[], basecall_model=None)
    aligned = SimpleNamespace(
        source_id=source_id, input_id=input_id,
        manifest={"kind": "prealigned", "tools": None, "reference_id": "synth"},
    )
    generation = store.generation()
    record = write_source(store, chunks, aligned, declared_sex="F")
    assert record["source_id"] == source_id
    assert record["inputs"][0]["input_id"] == input_id
    assert record["operator_metadata"]["sex"] == "F"
    assert store.generation() == generation + 1
    rerun = write_source(store, chunks, aligned)
    assert rerun["operator_metadata"] == record["operator_metadata"]
    assert store.generation() == generation + 1
