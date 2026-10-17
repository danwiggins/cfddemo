"""Cross-sectional A/B Compare pins current compatible results without donor assumptions."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortMember, CohortResult
from traceback_lab import cohorts
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.identity import parameters_sha256
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api


SOURCE_A, SOURCE_B = "1" * 64, "2" * 64
INPUT_A, INPUT_B = f"{101:064x}", f"{102:064x}"
PARAM_A, PARAM_B = "a" * 64, "b" * 64
COMPAT = "c" * 64


def world(tmp_path, monkeypatch, *, compat_b=COMPAT, unit_b="bp", value_b=170.0):
    sources = (SOURCE_A, SOURCE_B)
    inputs = (INPUT_A, INPUT_B)
    values = (160.0, value_b)
    params = (PARAM_A, PARAM_B)
    samples = {}
    selected = {}
    for index, source in enumerate(sources):
        metric = Metric(metric_id="mode_bp", value=values[index], unit="bp" if index == 0 else unit_b)
        envelope = ResultEnvelope(
            algorithm_id="fragment-size", algorithm_version="1", status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=params[index], source_id=source,
                                  input_id=inputs[index], reference_id="synthetic"),
            metrics=[metric],
        )
        samples[source] = SimpleNamespace(example=False, full=SimpleNamespace(
            input_id=inputs[index], results={"fragment-size": envelope}, depth_x=lambda: 1.0,
        ))
        selected[source] = CohortMember(
            source_id=source, input_id=inputs[index], aligner_identity="fixture aligner",
            results=(CohortResult(
                algorithm_id="fragment-size", parameters_sha256=params[index],
                cohort_compat_key=COMPAT if index == 0 else compat_b,
                metrics={"mode_bp": values[index]},
            ),),
        )
    monkeypatch.setattr(web_api.views, "load_sample", lambda store, source: samples[source])
    monkeypatch.setattr(web_api, "member_from_store", lambda store, source: selected[source])
    api = web_api.LabApi(LabStore(tmp_path))
    monkeypatch.setattr(api, "_id12s", lambda: {source: source[:12] for source in sources})
    return api, samples, selected


def test_sample_pair_reports_exact_b_minus_a_and_distinct_sources(tmp_path, monkeypatch) -> None:
    api, _, _ = world(tmp_path, monkeypatch)
    payload = api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])
    assert payload["state"] == "ready"
    assert payload["source_id_a"] != payload["source_id_b"]
    assert payload["input_id_a"] == INPUT_A and payload["input_id_b"] == INPUT_B
    assert payload["unscored"] == []
    row = payload["metrics"][0]
    assert (row["value_a"], row["value_b"], row["delta_b_minus_a"], row["unit"]) == (160.0, 170.0, 10.0, "bp")
    assert row["parameters_sha256_a"] == PARAM_A and row["parameters_sha256_b"] == PARAM_B
    assert api.compare_samples(SOURCE_A[:12], SOURCE_A[:12])["state"] == "same_sample"


def test_store_selected_results_keep_hidden_and_null_metrics(tmp_path, monkeypatch) -> None:
    from tests.lab.test_cohort_store import _sample as stored_sample

    store = LabStore(tmp_path)
    registry = tuple(SimpleNamespace(id=name, version="1", parameters={}, tools=[], upstream=())
                     for name in ("fragment-size", "read-summary"))
    monkeypatch.setattr(cohorts, "load_registry", lambda: registry)
    first = stored_sample(store, 1, value=160.0)
    second = stored_sample(store, 2, value=170.0)
    hidden_params = parameters_sha256(
        algorithm_id="read-summary", algorithm_version="1", parameters={},
        tools=[], reference_sha256=f"{900:064x}",
    )
    for source, input_id in ((first, f"{101:064x}"), (second, f"{102:064x}")):
        store.publish_result(ResultEnvelope(
            algorithm_id="read-summary", algorithm_version="1", status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=hidden_params,
                                  source_id=source, input_id=input_id, reference_id="synthetic"),
            metrics=[Metric(metric_id="optional_count", value=None)],
        ), input_id=input_id)

    api = web_api.LabApi(store)
    selected = cohorts.member_from_store(store, first)
    assert {row.algorithm_id for row in selected.results} == {"fragment-size", "read-summary"}
    assert next(row for row in selected.results if row.algorithm_id == "read-summary").metrics == {"optional_count": None}
    payload = api.compare_samples(first, second)
    assert payload["state"] == "ready"
    assert [(row["algorithm_id"], row["metric_id"], row["delta_b_minus_a"]) for row in payload["metrics"]] == [
        ("fragment-size", "mode_bp", 10.0),
    ]
    assert payload["unscored"] == []


def test_sample_pair_rejects_protocol_or_unit_mismatch(tmp_path, monkeypatch) -> None:
    api, _, _ = world(tmp_path, monkeypatch, compat_b="d" * 64)
    payload = api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])
    assert payload["metrics"] == []
    assert payload["unscored"] == [{"algorithm_id": "fragment-size", "metric_id": "*", "reason": "incompatible_protocol"}]

    api, _, _ = world(tmp_path, monkeypatch, unit_b="cm")
    payload = api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])
    assert payload["metrics"] == []
    assert payload["unscored"] == [{"algorithm_id": "fragment-size", "metric_id": "mode_bp", "reason": "unit_mismatch"}]


@pytest.mark.parametrize("change", ["input", "parameters", "value"])
def test_sample_pair_refuses_changed_selected_result(tmp_path, monkeypatch, change) -> None:
    api, samples, selected = world(tmp_path, monkeypatch)
    if change == "input":
        samples[SOURCE_B].full.input_id = f"{999:064x}"
    elif change == "parameters":
        envelope = samples[SOURCE_B].full.results["fragment-size"]
        samples[SOURCE_B].full.results["fragment-size"] = envelope.model_copy(update={
            "provenance": envelope.provenance.model_copy(update={"parameters_sha256": "e" * 64}),
        })
    else:
        selected[SOURCE_B] = selected[SOURCE_B].model_copy(update={
            "results": (selected[SOURCE_B].results[0].model_copy(update={"metrics": {"mode_bp": 171.0}}),),
        })
    payload = api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])
    assert payload["state"] == "stale_sample" and payload["which"] == "b"
    assert payload["metrics"] == []


def test_sample_pair_rejects_example_and_unknown_peer(tmp_path, monkeypatch) -> None:
    api, samples, _ = world(tmp_path, monkeypatch)
    samples[SOURCE_B].example = True
    assert api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])["state"] == "example_unscorable"
    assert api.compare_samples(SOURCE_A[:12], f"{3:012x}")["found"] is False


def test_sample_pair_marks_unavailable_peer_without_substituting_zero(tmp_path, monkeypatch) -> None:
    api, _, selected = world(tmp_path, monkeypatch)
    def current(store, source):
        if source == SOURCE_B:
            raise ValueError("admitted source has no full aligned input")
        return selected[source]
    monkeypatch.setattr(web_api, "member_from_store", current)
    payload = api.compare_samples(SOURCE_A[:12], SOURCE_B[:12])
    assert payload["state"] == "sample_unavailable" and payload["which"] == "b"
    assert payload["metrics"] == [] and payload["unscored"] == []


def test_sample_pair_route_requires_session_and_passes_both_ids(tmp_path, monkeypatch) -> None:
    from tests.lab.test_lab_server import _get, _request, _session
    from traceback_lab.web.server import RunningLabServer

    seen = []
    monkeypatch.setattr(web_api.LabApi, "compare_samples", lambda self, a, b: (
        seen.append((a, b)), {"found": True, "state": "same_sample"}
    )[1])
    with RunningLabServer(LabStore(tmp_path)) as server:
        path = f"/api/samples/{SOURCE_A[:12]}/compare/{SOURCE_B[:12]}"
        response, _ = _request(server, "GET", path)
        assert response.status == 401
        response, body = _get(server, path, _session(server))
    assert response.status == 200
    assert json.loads(body)["state"] == "same_sample"
    assert seen == [(SOURCE_A[:12], SOURCE_B[:12])]


@pytest.mark.skipif(__import__("tests.lab.test_lab_server", fromlist=["_chrome"])._chrome() is None,
                    reason="no Chrome/Chromium for sample Compare DOM check")
def test_sample_pair_selector_overlay_and_table_fallback(tmp_path, monkeypatch) -> None:
    from tests.lab.test_lab_server import _chrome, _dom_cdp
    from traceback_lab.web.server import RunningLabServer

    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [
        {"id12": SOURCE_A[:12], "run_utc": "", "depth_x": 1.0, "example": False},
        {"id12": SOURCE_B[:12], "run_utc": "", "depth_x": 1.0, "example": False},
    ], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, id12: {
        "found": True, "id12": id12, "algorithms": [], "catalog": [], "licenses": [],
        "cohort_n": 0, "cohort_version": None, "interpretation_version": "fixture",
        "excluded_chunks": [],
    })
    monkeypatch.setattr(web_api.LabApi, "cohorts", lambda self: {"cohorts": [], "unavailable": []})
    monkeypatch.setattr(web_api.LabApi, "compare_samples", lambda self, a, b: {
        "found": True, "state": "ready", "id12_a": a, "id12_b": b,
        "aligner_a": "fixture aligner", "aligner_b": "fixture aligner",
        "metrics": [{"algorithm_id": "fragment-size", "metric_id": "mode_bp", "label": "Size mode",
                     "unit": "bp", "value_a": 160.0, "value_b": 170.0, "delta_b_minus_a": 10.0}],
        "unscored": [{"algorithm_id": "copy-number", "metric_id": "*", "reason": "incompatible_protocol"}],
    })
    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "sample-pair-chrome"
        html = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--window-size=360,740",
            server.issue_link() + f"&route=/sample/{SOURCE_A[:12]}/compare",
        ], profile, ok=lambda page: 'data-ready="1"' in page and "Unpaired metrics and reasons" in page,
            timeout=90)
    assert 'for="compare-other-sample"' in html and 'id="compare-other-sample"' in html
    assert 'aria-label="Sample A and B comparison"' in html
    assert "Sample A" in html and "Sample B" in html and "B−A" in html
    assert re.search(
        r'<td class="num">160 bp</td><td class="num">170 bp</td><td class="num">10 bp</td>',
        html,
    )
    assert "incompatible protocol" in html
    assert "Source IDs do not establish a shared donor or timepoint" in html
    assert 'data-csp-violations="0"' in html
