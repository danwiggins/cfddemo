"""Malformed selected results remain visible as faults without exposing stale results."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
from traceback_lab.web.server import RunningLabServer


SOURCE = "b" * 64
INPUT = "c" * 64


def _result(identity: str, value: int) -> ResultEnvelope:
    return ResultEnvelope.model_validate({
        "algorithm_id": "fragment-size", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(
            tool="synthetic-fixture", parameters_sha256=identity,
            source_id=SOURCE, input_id=INPUT, reference_id="synthetic",
            timestamp_utc="2026-10-16T00:00:00Z",
        ),
        "metrics": [{"metric_id": "mode_bp", "value": value}],
    })


def _malformed_store(tmp_path) -> LabStore:
    store = LabStore(tmp_path)
    store.ensure()
    manifest = store.inputs_dir / INPUT / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind":"aligned"}')
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
    manifest = store.inputs_dir / INPUT / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind":"aligned"}')
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


@needs_chrome
@pytest.mark.parametrize("route,copy_text", [
    ("home", "result unreadable"),
    ("findings", "fragment-size: result unreadable"),
])
def test_malformed_stored_result_is_visible_without_alignment(route, copy_text, tmp_path) -> None:
    store = _malformed_store(tmp_path)
    id12 = LabApi(store).samples()["samples"][0]["id12"]
    path = None if route == "home" else f"/sample/{id12}/{route}"
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
    path = None if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(store) as server:
        dom = _dom(server, tmp_path / "browser", route=path)
    assert copy_text in dom
