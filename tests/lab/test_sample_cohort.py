"""Sample lenses use the same eligible, leave-one-out cohort projection as Compare."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api


SOURCE = f"{1:064x}"
INPUT = f"{101:064x}"
PARAMETERS = "b" * 64
COMPAT = "a" * 64


def member(index: int, *, compat: str = COMPAT) -> CohortMember:
    source = f"{index:064x}"
    return CohortMember(
        source_id=source, input_id=f"{index + 100:064x}", aligner_identity="fixture aligner",
        results=(CohortResult(
            algorithm_id="fragment-size", parameters_sha256=PARAMETERS,
            cohort_compat_key=compat, metrics={"short_fraction_lt150": index / 100},
        ),),
    )


@pytest.mark.parametrize("member_n,kind,expected_state,eligible_n", [
    (10, "internal_cohort", "no_reference", 9),
    (11, "internal_cohort", "above_typical", 10),
    (11, "provisional_external", "above_typical", 10),
    (31, "internal_cohort", "markedly_above", 30),
])
def test_sample_lenses_pin_eligible_loo_reference(
    tmp_path, monkeypatch, member_n: int, kind: str, expected_state: str, eligible_n: int,
) -> None:
    members = tuple(member(index) for index in range(1, member_n + 1))
    generation = CohortGeneration(cohort_id="healthy-v1", version=7, kind=kind, members=members)
    reads: list[str] = []
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: (reads.append(cohort_id), generation)[1])
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: members[0])
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=PARAMETERS,
                              source_id=SOURCE, input_id=INPUT, reference_id="synthetic"),
        metrics=[Metric(metric_id="short_fraction_lt150", value=0.95)],
    )
    sample = SimpleNamespace(example=False, full=SimpleNamespace(
        input_id=INPUT, results={"fragment-size": envelope}, depth_x=lambda: 1.0,
    ), derived=[], by_transform=lambda transform: [])
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: sample)
    monkeypatch.setattr(web_api.views, "stability_panels", lambda sample, library: [])
    monkeypatch.setattr(web_api.views, "depth_curve", lambda sample, library: {"rows": [], "downsampled": False})
    monkeypatch.setattr(web_api.views, "synthetic", lambda sample: [])
    monkeypatch.setattr(web_api, "source_record", lambda store, source_id: {})
    monkeypatch.setattr(web_api, "_defended", lambda *args: ({"rows": []}, {"text": "fixture"}))
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_resolve", lambda id12: (SOURCE, []))
    monkeypatch.setattr(api, "_id12s", lambda: {SOURCE: SOURCE[:12]})

    payload = api.sample(SOURCE[:12])
    metric = payload["algorithms"][0]["metrics"][0]
    assert reads == ["healthy-v1"]
    assert payload["cohort_version"] == "healthy-v1:7"
    assert payload["cohort_n"] == member_n
    assert payload["cohort_state"] == "ready"
    assert metric["reference_kind"] == kind
    assert metric["eligible_n"] == eligible_n
    assert len(metric["cohort_values"]) == eligible_n
    assert metric["cohort_excluded"] == {"self": 1}
    assert metric["state"] == expected_state
    if eligible_n < 10:
        assert metric["reason"] == "cohort_too_small"
    elif eligible_n < 30:
        assert metric["exploratory_n"] == eligible_n
        assert metric["percentile"] is None
        if kind == "provisional_external":
            assert "Protocol-mismatched external reference" in metric["caption"]
    else:
        assert metric["percentile"] is not None
        assert metric["interval"] is not None
    summary = api.samples()["samples"][0]
    assert summary["outside_reference"] == int(expected_state != "no_reference")
    assert reads == ["healthy-v1", "healthy-v1"]


def test_incompatible_or_stale_target_never_scores_against_cohort(tmp_path, monkeypatch) -> None:
    generation = CohortGeneration(
        cohort_id="healthy-v1", version=2,
        members=tuple(member(index, compat="c" * 64) for index in range(2, 13)),
    )
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    api = web_api.LabApi(LabStore(tmp_path))
    sample = SimpleNamespace(example=False, full=SimpleNamespace(input_id=INPUT))
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: member(1))
    pinned, target, state = api._sample_cohort(SOURCE, sample)
    assert pinned is generation and target is not None and state == "ready"

    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=PARAMETERS,
                              source_id=SOURCE, input_id=INPUT, reference_id="synthetic"),
        metrics=[Metric(metric_id="short_fraction_lt150", value=0.95)],
    )
    rows, projections = api._interpret_sample_envelope(
        envelope, api.libraries.get(), 1.0, SOURCE, generation, target,
    )
    assert rows[0].interpretation.reason == "cohort_too_small"
    assert projections["short_fraction_lt150"].percentile_n == 0
    assert projections["short_fraction_lt150"].exclusions == {"incompatible": 11}

    sample.full.input_id = f"{999:064x}"
    pinned, target, state = api._sample_cohort(SOURCE, sample)
    assert pinned is generation and target is None and state == "stale_target"
    rows, projections = api._interpret_sample_envelope(
        envelope, api.libraries.get(), 1.0, SOURCE, generation, target,
    )
    assert rows[0].interpretation.reason == "cohort_too_small"
    assert projections == {}
