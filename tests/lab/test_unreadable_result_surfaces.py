"""Malformed selected results remain visible as faults without exposing stale results."""

from __future__ import annotations

import json

import pysam
import pytest

from tests.lab.test_cohort_store import _sample
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_lab.views import load_sample
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer

SOURCE = "b" * 64
INPUT = "c" * 64


def _aligned_ont_input(store: LabStore) -> None:
    input_dir = store.inputs_dir / INPUT
    input_dir.mkdir(parents=True)
    (input_dir / "manifest.json").write_text('{"kind":"aligned","platform":"ont"}')
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
              "RG": [{"ID": "synthetic", "PL": "ONT"}]}
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header=header):
        pass


def _result(identity: str, value: int, input_id: str = INPUT) -> ResultEnvelope:
    return ResultEnvelope.model_validate({
        "algorithm_id": "fragment-size", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(
            tool="synthetic-fixture", parameters_sha256=identity,
            source_id=SOURCE, input_id=input_id, reference_id="synthetic",
            timestamp_utc="2026-10-16T00:00:00Z",
        ),
        "metrics": [{"metric_id": "mode_bp", "value": value}],
    })


def _malformed_store(tmp_path) -> LabStore:
    store = LabStore(tmp_path)
    store.ensure()
    _aligned_ont_input(store)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{SOURCE}.json").write_text(json.dumps({
        "source_id": SOURCE, "example": False, "excluded": [], "derived": [],
        "inputs": [{"input_id": INPUT, "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    store.publish_result(_result("a" * 64, 167), input_id=INPUT)
    store.publish_result(_result("d" * 64, 168), input_id=INPUT)
    directory = store.result_dir(INPUT, "fragment-size", "d" * 64)
    attempt = json.loads((directory / "selected.json").read_bytes())["attempt_id"]
    (directory / "attempts" / attempt / "result.json").write_bytes(b"{ malformed")
    return store


def _ambiguous_store(tmp_path) -> LabStore:
    store = LabStore(tmp_path)
    store.ensure()
    _aligned_ont_input(store)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{SOURCE}.json").write_text(json.dumps({
        "source_id": SOURCE, "example": False, "excluded": [], "derived": [],
        "inputs": [{"input_id": INPUT, "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    for identity, value in (("a" * 64, 167), ("d" * 64, 168)):
        store.publish_result(_result(identity, value), input_id=INPUT)
    for identity in ("a" * 64, "d" * 64):
        selector = store.result_dir(INPUT, "fragment-size", identity) / "selected.json"
        selection = json.loads(selector.read_bytes())
        selection.pop("publication_order")
        selector.write_text(json.dumps(selection))
    return store


def _older_malformed_store(tmp_path, *, legacy_order: bool = False) -> LabStore:
    store = LabStore(tmp_path)
    store.ensure()
    _aligned_ont_input(store)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{SOURCE}.json").write_text(json.dumps({
        "source_id": SOURCE, "example": False, "excluded": [], "derived": [],
        "inputs": [{"input_id": INPUT, "used_utc": "2026-10-16T00:00:00Z"}],
    }))
    store.publish_result(_result("a" * 64, 167), input_id=INPUT)
    store.publish_result(_result("d" * 64, 168), input_id=INPUT)
    selected = store.result_dir(INPUT, "fragment-size", "a" * 64)
    selector = selected / "selected.json"
    choice = json.loads(selector.read_bytes())
    if legacy_order:
        choice.pop("publication_order")
        selector.write_text(json.dumps(choice))
    (selected / "attempts" / choice["attempt_id"] / "result.json").write_bytes(b"{ malformed")
    return store


def test_corrupt_older_identity_does_not_hide_newer_valid_result(tmp_path) -> None:
    sample = LabApi(_older_malformed_store(tmp_path)).sample(SOURCE[:12])
    assert sample["unreadable_results"] == []
    result = next(row for row in sample["algorithms"] if row["algorithm_id"] == "fragment-size")
    assert result["status"] == "ok"
    assert next(metric for metric in result["metrics"] if metric["metric_id"] == "mode_bp")["value"] == 168


def test_corrupt_older_identity_with_unknown_order_withholds_result(tmp_path) -> None:
    sample = LabApi(_older_malformed_store(tmp_path, legacy_order=True)).sample(SOURCE[:12])
    assert sample["unreadable_results"] == ["fragment-size"]
    assert all(row["algorithm_id"] != "fragment-size" for row in sample["algorithms"])


def test_malformed_newest_result_is_disclosed_without_stale_fallback(tmp_path) -> None:
    store = _malformed_store(tmp_path)

    api = LabApi(store)
    listing = api.samples()["samples"]
    assert len(listing) == 1 and listing[0]["unreadable_results"] == ["fragment-size"]
    id12 = listing[0]["id12"]
    sample = api.sample(id12)
    assert sample["found"] and sample["unreadable_results"] == ["fragment-size"]
    assert all(row["algorithm_id"] != "fragment-size" for row in sample["algorithms"])
    assert next(row for row in sample["catalog"] if row["algorithm_id"] == "fragment-size")["status"] == "unreadable"
    assert sample["defended"]["rows"] == []


def test_unregistered_result_directory_is_reported_without_exposing_its_name(tmp_path) -> None:
    store = _malformed_store(tmp_path)
    unknown = store.results_dir / INPUT / "private.sentinel" / ("e" * 64)
    unknown.mkdir(parents=True)
    (unknown / "selected.json").write_text('{"attempt_id":"invalid"}')

    api = LabApi(store)
    sample = api.sample(SOURCE[:12])
    row = api.samples()["samples"][0]
    assert sample["malformed_result_selections"] and row["malformed_result_selections"]
    assert sample["unreadable_results"] == row["unreadable_results"] == ["fragment-size"]
    assert "private.sentinel" not in json.dumps(sample)
    assert "private.sentinel" not in json.dumps(row)


@needs_chrome
@pytest.mark.parametrize("route", ("findings", "trust-qc"))
def test_unregistered_result_directory_has_generic_browser_notice(route, tmp_path) -> None:
    store = _malformed_store(tmp_path / "store")
    unknown = store.results_dir / INPUT / "private.sentinel" / ("e" * 64)
    unknown.mkdir(parents=True)
    (unknown / "selected.json").write_text('{"attempt_id":"invalid"}')
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{SOURCE[:12]}/{route}")
    assert "malformed result selection" in dom.lower()
    assert "private.sentinel" not in dom


def test_ambiguous_legacy_results_are_disclosed_without_guessing(tmp_path) -> None:
    store = _ambiguous_store(tmp_path)
    api = LabApi(store)
    listing = api.samples()["samples"]
    assert len(listing) == 1 and listing[0]["ambiguous_results"] == ["fragment-size"]
    sample = api.sample(listing[0]["id12"])
    assert sample["ambiguous_results"] == ["fragment-size"]
    assert all(row["algorithm_id"] != "fragment-size" for row in sample["algorithms"])
    assert next(row for row in sample["catalog"] if row["algorithm_id"] == "fragment-size")["status"] == "ambiguous"
    assert sample["defended"]["rows"] == []


def test_unreadable_derived_result_is_named_in_stability(tmp_path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    derived_input = "d" * 64
    _aligned_ont_input(store)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{SOURCE}.json").write_text(json.dumps({
        "source_id": SOURCE, "example": False, "excluded": [],
        "inputs": [{"input_id": INPUT, "used_utc": "2026-10-16T00:00:00Z"}],
        "derived": [{"input_id": derived_input, "parent_input_id": INPUT, "label": "0.5",
                     "transform": "downsample", "parameters": {"fraction": 0.5}}],
    }))
    identity = "a" * 64
    store.publish_result(_result(identity, 167), input_id=INPUT)
    store.publish_result(_result(identity, 170, derived_input), input_id=derived_input)
    selected = store.result_dir(derived_input, "fragment-size", identity)
    attempt = json.loads((selected / "selected.json").read_bytes())["attempt_id"]
    (selected / "attempts" / attempt / "result.json").write_bytes(b"{ malformed")

    sample = LabApi(store).sample(SOURCE[:12])
    panel = next(panel for panel in sample["stability"] if panel["algorithm_id"] == "fragment-size"
                 and panel["metric_id"] == "mode_bp")
    point = next(point for point in panel["points"] if point["fraction"] == 0.5)
    assert point["state"] == "unavailable" and point["reason"] == "result unreadable"
    row = next(row for row in sample["curve"]["rows"] if row["algorithm_id"] == "fragment-size")
    assert row["cells"][0]["reason"] == "result unreadable"


def test_unreadable_selected_copy_number_blocks_its_pon_consumer(tmp_path) -> None:
    store = LabStore(tmp_path)
    source_id = _sample(store, 1)
    input_id = f"{101:064x}"

    def result(algorithm_id: str, version: str, identity: str) -> ResultEnvelope:
        return ResultEnvelope(
            algorithm_id=algorithm_id, algorithm_version=version, status="ok",
            provenance=provenance(
                tool="fixture", parameters_sha256=identity,
                source_id=source_id, input_id=input_id, reference_id="hg38",
            ),
        )

    store.publish_result(result("copy-number", "1", "8" * 64), input_id=input_id)
    current_attempt = store.publish_result(result("copy-number", "2", "9" * 64), input_id=input_id)
    store.publish_result(
        result("instability", "1", "a" * 64), input_id=input_id,
        upstream_selections=[{
            "algorithm_id": "copy-number", "input_id": input_id,
            "parameters_sha256": "9" * 64, "attempt_id": current_attempt,
        }],
    )
    current = store.result_dir(input_id, "copy-number", "9" * 64)
    (current / "attempts" / current_attempt / "result.json").write_bytes(b"{ malformed")

    view = load_sample(store, source_id)
    assert view is not None
    assert "copy-number" in view.full.unreadable
    assert "copy-number" not in view.full.results
    assert view.full.results["instability"].skip_reason == "superseded_method"


@needs_chrome
@pytest.mark.parametrize("route", ("home", "stability"))
@pytest.mark.parametrize("reason", ("result unreadable", "result selection ambiguous"))
def test_unavailable_result_keeps_state_and_reason_in_browser(route, reason, tmp_path) -> None:
    # The API case above owns the malformed-store setup; this checks the
    # renderer's explicit state copy with a lightweight synthetic response.
    from tests.lab.test_acceptance_synthetic_routes import _sample, _server

    sample = _sample()
    sample["curve"]["rows"] = [{
        "algorithm_id": "fragment-size", "metric_id": "mode_bp", "label": "Fragment mode",
        "tier": "NEWER", "min_depth_x": None, "full_state": "unavailable",
        "cells": [{"fraction": 1.0, "label": "full", "state": "unavailable",
                   "reason": reason, "depth_x": 1.0}],
    }]
    sample["stability"][0]["points"] = [{"fraction": 0.5, "label": "0.5", "state": "unavailable",
                                           "reason": reason, "value": None, "deviation": None,
                                           "unit": "percent", "band": None}]
    path = "/" if route == "home" else f"/sample/{sample['id12']}/stability"
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=path)
    assert f"unavailable ({reason})" in dom
    if route == "stability":
        assert ("unreadable" if reason == "result unreadable" else "ambiguous") + "</text>" in dom


@needs_chrome
@pytest.mark.parametrize("route", ("home", "stability"))
def test_failure_detail_stays_out_of_curve_and_stability_labels(route, tmp_path) -> None:
    from tests.lab.test_acceptance_synthetic_routes import _sample, _server

    sample = _sample()
    sample["curve"]["rows"] = [{
        "algorithm_id": "fragment-size", "metric_id": "mode_bp", "label": "Fragment mode",
        "tier": "NEWER", "min_depth_x": None, "full_state": "failed",
        "cells": [{"fraction": 1.0, "label": "full", "state": "failed",
                   "reason": "PRIVATE_FAILURE_DETAIL", "depth_x": 1.0}],
    }]
    sample["stability"][0]["points"] = [{
        "fraction": 0.5, "label": "0.5", "state": "failed",
        "reason": "PRIVATE_FAILURE_DETAIL", "value": None, "deviation": None,
        "unit": "percent", "band": None,
    }]
    path = "/" if route == "home" else f"/sample/{sample['id12']}/stability"
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=path)
    assert "PRIVATE_FAILURE_DETAIL" not in dom
    assert "failed" in dom


@needs_chrome
@pytest.mark.parametrize("route,copy_text", [
    ("home", "result unreadable"),
    ("findings", "fragment-size: result unreadable"),
])
def test_malformed_stored_result_is_visible_without_alignment(route, copy_text, tmp_path) -> None:
    store = _malformed_store(tmp_path)
    id12 = LabApi(store).samples()["samples"][0]["id12"]
    path = f"/?sample={id12}" if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=path)
    assert f"{copy_text} (sample {id12})" in dom


@needs_chrome
@pytest.mark.parametrize("route,copy_text", [
    ("home", "result selection ambiguous"),
    ("findings", "fragment-size: result selection ambiguous"),
    ("evidence", "ambiguous"),
])
def test_ambiguous_legacy_result_is_visible_across_surfaces(route, copy_text, tmp_path) -> None:
    store = _ambiguous_store(tmp_path)
    id12 = LabApi(store).samples()["samples"][0]["id12"]
    path = f"/?sample={id12}" if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=path)
    assert copy_text in dom
