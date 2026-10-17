"""Sample lenses use the same eligible, leave-one-out cohort projection as Compare."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.envelope import Depth, Metric, ResultEnvelope, provenance
from traceback_lab.interpretation import engine as interpretation_engine
from traceback_lab.interpretation.engine import Cohort, classify, load_library
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore
from traceback_lab.views import InputView, SampleView
from traceback_lab.web import api as web_api


SOURCE = f"{1:064x}"
INPUT = f"{101:064x}"
PARAMETERS = "b" * 64
COMPAT = "a" * 64


def test_state_only_interpretation_matches_full_state_without_bootstrap(monkeypatch) -> None:
    entry = load_library().entry("fragment-size", "short_fraction_lt150")
    assert entry is not None
    cohort = Cohort(tuple(index / 100 for index in range(30)))
    full = classify(0.95, entry, depth_x=1.0, cohort=cohort)
    assert full.interval is not None
    monkeypatch.setattr(interpretation_engine, "bootstrap_p95_interval", lambda values: pytest.fail(
        "state-only interpretation must not bootstrap",
    ))
    state_only = classify(0.95, entry, depth_x=1.0, cohort=cohort, include_interval=False)
    assert (state_only.state, state_only.reason, state_only.percentile, state_only.severity) == (
        full.state, full.reason, full.percentile, full.severity,
    )
    assert state_only.interval is None


def member(index: int, *, compat: str = COMPAT, value: float | None = None) -> CohortMember:
    source = f"{index:064x}"
    return CohortMember(
        source_id=source, input_id=f"{index + 100:064x}", aligner_identity="fixture aligner",
        results=(CohortResult(
            algorithm_id="fragment-size", parameters_sha256=PARAMETERS,
            cohort_compat_key=compat, metrics={"short_fraction_lt150": index / 100 if value is None else value},
        ),),
    )


@pytest.mark.parametrize("member_n,kind,expected_state,eligible_n,is_member,target_value,outside_count", [
    (10, "internal_cohort", "no_reference", 9, True, 0.95, 0),
    (10, "internal_cohort", "above_typical", 10, False, 0.95, 1),
    (10, "internal_cohort", "in_range", 10, False, 0.05, 0),
    (11, "internal_cohort", "above_typical", 10, True, 0.95, 1),
    (11, "provisional_external", "above_typical", 10, True, 0.95, 1),
    (31, "internal_cohort", "markedly_above", 30, True, 0.95, 1),
])
def test_sample_lenses_pin_eligible_loo_reference(
    tmp_path, monkeypatch, member_n: int, kind: str, expected_state: str, eligible_n: int,
    is_member: bool, target_value: float, outside_count: int,
) -> None:
    members = tuple(member(index, value=target_value if index == 1 and is_member else None)
                    for index in range(1 if is_member else 2, member_n + (1 if is_member else 2)))
    generation = CohortGeneration(cohort_id="healthy-v1", version=7, kind=kind, members=members)
    reads: list[str] = []
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: (reads.append(cohort_id), generation)[1])
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: member(1, value=target_value))
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=PARAMETERS,
                              source_id=SOURCE, input_id=INPUT, reference_id="synthetic"),
        depth=Depth(eligible_reads=1_000_000, approx_depth_x=1.0),
        metrics=[Metric(metric_id="short_fraction_lt150", value=target_value)],
    )
    sample = SampleView(
        source_id=SOURCE, id12=SOURCE[:12], example=False,
        full=InputView("full", INPUT, None, {}, {"fragment-size": envelope}),
    )
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
    assert payload["cohort_kind"] == kind
    assert payload["cohort_n"] == member_n
    assert payload["cohort_state"] == "ready"
    assert payload["size_density_band"]["state"] == "no_usable_pinned_vectors"
    assert [row["state"] for row in payload["agreement_pairs"]] == ["target_metric_unavailable"] * 2
    assert metric["reference_kind"] == kind
    assert metric["eligible_n"] == eligible_n
    assert len(metric["cohort_values"]) == eligible_n
    assert metric["cohort_mean"] == (sum(metric["cohort_values"]) / eligible_n if eligible_n >= 10 else None)
    assert metric["cohort_excluded"] == ({"self": 1} if is_member else {})
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
    assert summary["outside_reference"] == outside_count
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
    pinned, target, state, problem = api._sample_cohort(SOURCE, sample)
    assert pinned is generation and target is not None and state == "ready" and problem is None

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
    pinned, target, state, problem = api._sample_cohort(SOURCE, sample)
    assert pinned is generation and target is None and state == "stale_target"
    assert problem is not None and problem["code"] == "TBX-LAB-017"
    rows, projections = api._interpret_sample_envelope(
        envelope, api.libraries.get(), 1.0, SOURCE, generation, target,
    )
    assert rows[0].interpretation.reason == "cohort_too_small"
    assert projections == {}


def test_sample_list_pins_one_generation_for_all_rows(tmp_path, monkeypatch) -> None:
    sources = (SOURCE, f"{2:064x}")
    generations = [
        CohortGeneration(cohort_id="healthy-v1", version=7, members=tuple(member(i) for i in range(1, 32))),
        CohortGeneration(cohort_id="healthy-v1", version=8, members=tuple(member(i) for i in range(1, 42))),
    ]
    reads: list[str] = []
    def current(store, cohort_id):
        reads.append(cohort_id)
        return generations[len(reads) - 1]
    monkeypatch.setattr(web_api, "read_current", current)
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: member(int(source_id, 16)))
    samples = {}
    for index, source in enumerate(sources, start=1):
        input_id = f"{index + 100:064x}"
        envelope = ResultEnvelope(
            algorithm_id="fragment-size", algorithm_version="1", status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=PARAMETERS,
                                  source_id=source, input_id=input_id, reference_id="synthetic"),
            metrics=[Metric(metric_id="short_fraction_lt150", value=0.95)],
        )
        samples[source] = SimpleNamespace(example=False, full=SimpleNamespace(
            input_id=input_id, results={"fragment-size": envelope}, depth_x=lambda: 1.0,
        ), by_transform=lambda transform: [])
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: samples[source_id])
    monkeypatch.setattr(web_api.views, "synthetic", lambda sample: [])
    monkeypatch.setattr(web_api, "source_record", lambda store, source_id: {})
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_id12s", lambda: {source: source[:12] for source in sources})
    monkeypatch.setattr(interpretation_engine, "bootstrap_p95_interval", lambda values: pytest.fail(
        "sample list must classify without computing bootstrap intervals",
    ))

    rows = api.samples()["samples"]
    assert reads == ["healthy-v1"]
    assert {row["cohort_version"] for row in rows} == {"healthy-v1:7"}
    assert {row["cohort_kind"] for row in rows} == {"internal_cohort"}
    assert all(row["cohort_n"] == 31 and row["cohort_state"] == "ready" for row in rows)
    assert all(row["outside_reference"] == 1 for row in rows)


def test_unavailable_cohort_is_explicit_not_an_empty_healthy_reference(tmp_path, monkeypatch) -> None:
    api = web_api.LabApi(LabStore(tmp_path))
    envelope = ResultEnvelope(
        algorithm_id="fragment-size", algorithm_version="1", status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=PARAMETERS,
                              source_id=SOURCE, input_id=INPUT, reference_id="synthetic"),
        metrics=[Metric(metric_id="short_fraction_lt150", value=0.95)],
    )
    sample = SimpleNamespace(example=False, full=SimpleNamespace(
        input_id=INPUT, results={"fragment-size": envelope}, depth_x=lambda: 1.0,
    ), by_transform=lambda transform: [])
    problem = LabProblem(
        "TBX-LAB-022", cause="stored cohort generation is invalid",
        fix="Restore the generation or rebuild the cohort",
    )
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: (_ for _ in ()).throw(problem))

    generation, target, state, detail = api._sample_cohort(SOURCE, sample)
    assert generation is None and target is None and state == "cohort_unavailable"
    assert detail is not None and detail["code"] == "TBX-LAB-022"
    assert detail["fix"] == "Restore the generation or rebuild the cohort"
    monkeypatch.setattr(api, "_id12s", lambda: {SOURCE: SOURCE[:12]})
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source_id: sample)
    monkeypatch.setattr(web_api.views, "synthetic", lambda sample: [])
    monkeypatch.setattr(web_api, "source_record", lambda store, source_id: {})
    row = api.samples()["samples"][0]
    assert row["cohort_state"] == "cohort_unavailable"
    assert row["cohort_problem"]["code"] == "TBX-LAB-022"
    assert row["cohort_version"] is None
    assert row["outside_reference"] == 0


@pytest.mark.parametrize("failure", [
    LabProblem("TBX-LAB-017", cause="source result unreadable", fix="Restore the sample result"),
    ValueError("admitted source has no full aligned input"),
])
def test_target_unavailable_preserves_recovery_state(tmp_path, monkeypatch, failure) -> None:
    api = web_api.LabApi(LabStore(tmp_path))
    generation = CohortGeneration(cohort_id="healthy-v1", version=2, members=())
    monkeypatch.setattr(web_api, "read_current", lambda store, cohort_id: generation)
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source_id: (_ for _ in ()).throw(failure))
    sample = SimpleNamespace(example=False, full=SimpleNamespace(input_id=INPUT))

    pinned, target, state, detail = api._sample_cohort(SOURCE, sample)
    assert pinned is generation and target is None and state == "target_unavailable"
    assert detail is not None and detail["code"] == "TBX-LAB-017"
    assert detail["fix"]
