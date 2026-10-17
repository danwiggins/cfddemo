"""S5: D32 stability table, the default depth curve and Stability-lens data."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab import views
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.stability import Side, band_at, deviation, stable
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer
from traceback_runner import cli as signed_cli

P = {"kind": "proportion"}
R = {"kind": "relative", "tolerance": 0.10}
A = {"kind": "absolute", "tolerance": 3}
S = {"kind": "scales_with_depth", "tolerance": 0.10}

TABLE = [
    # spec, a, b, expected
    (P, Side(0.30, n=1000), Side(0.30, n=1000), True),
    (P, Side(0.30, n=10_000), Side(0.36, n=10_000), False),
    (P, Side(0.30, n=20), Side(0.36, n=20), True),  # small n: wide z bound
    (P, Side(0.300), Side(0.309), True),  # no n: the 0.01 floor
    (P, Side(0.300), Side(0.312), False),
    (P, Side(0.0, n=1000), Side(0.0, n=1000), True),
    (R, Side(100.0), Side(109.0), True),
    (R, Side(100.0), Side(112.0), False),
    (R, Side(0.0), Side(0.0), True),  # both zero
    (R, Side(0.05), Side(-0.05), False),  # mean |.| 0.05: relative diff 2.0
    ({"kind": "relative", "tolerance": 0.2}, Side(0.0), Side(0.1), False),  # one zero: relative diff 2.0
    (A, Side(167), Side(170), True),
    (A, Side(167), Side(171), False),
    (S, Side(500, fraction=0.5), Side(1000, fraction=1.0), True),
    (S, Side(400, fraction=0.5), Side(1000, fraction=1.0), False),
    (P, Side(None), Side(0.3), None),
    (P, Side(0.3), Side(None), None),
    (R, Side([1, 2]), Side([1, 2]), None),  # vectors: no verdict
    # The old failure: 0.50 with CI [0.45, 0.55] against itself is stable.
    (P, Side(0.50, ci=(0.45, 0.55), ci_level=0.9), Side(0.50, ci=(0.45, 0.55), ci_level=0.9), True),
    (P, Side(0.50, ci=(0.45, 0.55), ci_level=0.9), Side(0.58, ci=(0.53, 0.63), ci_level=0.9), False),
    (P, Side(0.50, ci=(0.45, 0.55), ci_level=0.9), Side(0.56, ci=(0.51, 0.61), ci_level=0.9), True),
]


@pytest.mark.parametrize("spec,a,b,expected", TABLE)
def test_d32_table(spec, a, b, expected) -> None:
    assert stable(spec, a, b) is expected


def test_absolute_needs_a_tolerance() -> None:
    with pytest.raises(ValueError):
        stable({"kind": "absolute"}, Side(1.0), Side(1.0))


def test_deviation_and_band_units() -> None:
    assert deviation(R, 110.0, 100.0) == (10.0, "percent")
    assert deviation(R, 10**400, 100.0) == (None, "none")
    assert deviation(A, 170, 167) == (3, "unit")
    assert deviation(R, 0.1, 0.0) == (0.1, "unit")  # full value 0: relative undefined
    assert deviation(S, 500, 1000, 0.5) == (0.0, "percent")
    assert band_at(A, Side(170), Side(167)) == (-3.0, 3.0, "unit")
    lo, hi, unit = band_at(R, Side(110.0), Side(100.0))
    assert unit == "percent" and lo < 0 < hi
    # the band matches the D32 verdict at its edges
    assert stable(R, Side(100.0 * (1 + hi / 100) - 1e-9), Side(100.0)) is True
    assert stable(R, Side(100.0 * (1 + hi / 100) + 1e-6), Side(100.0)) is False
    small = band_at(P, Side(0.36, n=20), Side(0.30, n=10))
    assert small[1] > 20  # small counts: a wide proportion band, as the verdict


# ----------------------------------------------------------- default lab run
@pytest.fixture
def default_run(lab_world, capsys, shared_mmi):
    root = lab_world["root"]
    code = signed_cli.main(["lab", "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    return root, payload


def test_default_run_builds_the_curve_and_controls(default_run) -> None:
    root, payload = default_run
    controls = payload["data"]["controls"]
    assert controls["downsample"] == [0.5, 0.25, 0.1] and controls["split"] is True
    assert controls["cna_skipped"] in {"missing_toolchain", "missing_asset"}
    store = LabStore(root)
    sample = views.load_sample(store, payload["data"]["sample"]["source_id"])
    curve = views.depth_curve(sample)
    assert curve["fractions"] == [0.1, 0.25, 0.5, 1.0]
    rows = {row["algorithm_id"]: row for row in curve["rows"]}
    assert "read-summary" not in rows
    assert rows["fragment-size"]["metric_id"] == "mode_bp"
    assert [cell["label"] for cell in rows["bam-qc"]["cells"]] == ["0.1", "0.25", "0.5", "full"]
    assert rows["bam-qc"]["cells"][-1]["state"] == "ok_usable"
    assert rows["copy-number"]["full_state"] == "skipped"
    assert curve["rows"][-1]["full_state"] == "skipped"  # skipped rows sort last
    depths = [cell["depth_x"] for cell in rows["depth"]["cells"]]
    assert depths == sorted(depths)
    panels = views.stability_panels(sample)
    assert panels[0]["headline"]
    with_split = [panel for panel in panels if panel["split"] is not None]
    assert {panel["algorithm_id"] for panel in with_split} >= {"bam-qc", "depth", "fragment-size"}
    assert all(panel["split_status"][half]["state"] == "ok" for panel in with_split for half in ("A", "B"))
    block = views.repeatability(panels)
    assert block["metrics"] == len([p for p in panels if p["split"]])

    with patch.object(views, "stable", return_value=None), patch.object(views, "deviation", return_value=(None, "none")):
        no_verdict = next(p for p in views.stability_panels(sample)
                          if p["algorithm_id"] == "fragment-size" and p["metric_id"] == "mode_bp")
    assert no_verdict["split"]["stable"] is None
    assert no_verdict["split"]["deviation_a"] is None
    assert no_verdict["verdict"] == "no usable measurement below full depth"
    assert no_verdict["split"]["relative_abs_diff"] >= 0
    assert views.repeatability([no_verdict])["no_verdict_metrics"] == 1
    assert all(no_verdict["split_status"][half]["state"] == "ok" for half in ("A", "B"))
    assert all(no_verdict["split_status"][half]["value"] is not None for half in ("A", "B"))

    halves = {view.parameters["half"]: view for view in sample.by_transform("split")}
    selected = "fragment-size"
    original_b = halves["B"].results[selected]
    original_a = halves["A"].results.pop(selected)
    try:
        def fragment_panel():
            return next(p for p in views.stability_panels(sample)
                        if p["algorithm_id"] == selected and p["metric_id"] == "mode_bp")

        missing = fragment_panel()
        assert missing["split"] is None
        assert missing["split_status"]["A"]["state"] == "not_recorded"
        assert missing["split_status"]["A"]["reason"] == "no selected result recorded"
        halves["A"].results[selected] = original_a

        halves["B"].results[selected] = original_b.model_copy(
            update={"status": "skipped", "skip_reason": "depth", "metrics": []})
        skipped = fragment_panel()
        assert skipped["split"] is None
        assert skipped["split_status"]["A"]["state"] == "ok"
        assert skipped["split_status"]["B"]["state"] == "skipped"
        assert skipped["split_status"]["B"]["reason"] == "depth"

        halves["B"].results[selected] = original_b.model_copy(
            update={"status": "failed", "metrics": []})
        assert fragment_panel()["split_status"]["B"]["state"] == "failed"

        null_metrics = [m.model_copy(update={"value": None}) if m.metric_id == "mode_bp" else m
                        for m in original_b.metrics]
        halves["B"].results[selected] = original_b.model_copy(update={"metrics": null_metrics})
        null = fragment_panel()
        assert null["split"] is None
        assert null["split_status"]["A"]["state"] == "ok"
        assert null["split_status"]["B"]["state"] == "not_interpretable"
    finally:
        halves["A"].results[selected] = original_a
        halves["B"].results[selected] = original_b


def test_rerun_is_cached_and_no_downsample_leaves_only_full(default_run, lab_world, capsys, shared_mmi) -> None:
    root, payload = default_run
    code = signed_cli.main(["lab", "run", "latest", "--root", str(root), "--json"])
    again = json.loads(capsys.readouterr().out)
    assert code == 0
    assert all(step["cached"] for step in again["data"]["steps"] if step["status"] == "ok")
    fresh = lab_world["tmp"] / "root-nd"
    from traceback_runner.references import register_reference

    register_reference(fresh, lab_world["fasta"], "synth")
    code = signed_cli.main(["lab", "run", str(lab_world["chunks"]), "--root", str(fresh), "--mmi", str(shared_mmi), "--no-downsample", "--json"])
    payload = json.loads(capsys.readouterr().out)
    sample = views.load_sample(LabStore(fresh), payload["data"]["sample"]["source_id"])
    curve = views.depth_curve(sample)
    assert curve["fractions"] == [1.0] and curve["downsampled"] is False


@pytest.mark.parametrize("value", ["0,1", "1.5", "abc"])
def test_bad_downsample_is_tbx_lab_002(lab_world, capsys, value) -> None:
    code = signed_cli.main(["lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--downsample", value, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["data"]["code"] == "TBX-LAB-002"
    assert not (lab_world["root"] / "lab").exists()


def test_odd_fractions_work(lab_world, capsys, shared_mmi) -> None:
    code = signed_cli.main(
        ["lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi),
         "--downsample", "0.05,0.125", "--no-controls", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    labels = {step["input"] for step in payload["data"]["steps"]}
    assert {"0.05", "0.125", "full"} <= labels


def test_stale_derived_results_are_not_compared(default_run) -> None:
    root, payload = default_run
    store = LabStore(root)
    sample = views.load_sample(store, payload["data"]["sample"]["source_id"])
    full_identity = sample.full.results["fragment-size"].provenance.parameters_sha256
    for view in sample.derived:
        if "fragment-size" in view.results:
            assert view.results["fragment-size"].provenance.parameters_sha256 == full_identity
    rows = views.selected_results(store, sample.derived[0].input_id, {"fragment-size": "0" * 64})
    assert rows == {}


def _published_split_store(tmp_path):
    store = LabStore(tmp_path)
    store.ensure()
    source_id, full_id, split_id, identity = (character * 64 for character in "abcd")
    manifest = store.inputs_dir / full_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind":"prealigned"}')
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id,
        "inputs": [{"input_id": full_id, "used_utc": "2026-10-16T00:00:00Z"}],
        "derived": [{"input_id": split_id, "parent_input_id": full_id,
                     "transform": "split", "label": "split-A", "parameters": {"half": "A"}}],
    }))
    for input_id in (full_id, split_id):
        envelope = ResultEnvelope.model_validate({
            "algorithm_id": "fragment-size", "algorithm_version": "1", "status": "ok",
            "provenance": provenance(
                tool="test", parameters_sha256=identity, source_id=source_id,
                input_id=input_id, reference_id="ref", timestamp_utc="2026-10-16T00:00:00Z",
            ),
            "metrics": [{"metric_id": "mode_bp", "value": 167.0}],
        })
        store.publish_result(envelope, input_id=input_id)
    return store, source_id, full_id, split_id, identity


def test_damaged_split_selection_is_explained_by_loaded_sample(tmp_path) -> None:
    store, source_id, _, split_id, identity = _published_split_store(tmp_path)
    sample = views.load_sample(store, source_id)
    assert sample is not None
    split = next(item for item in sample.derived if item.transform == "split")
    selector = store.result_dir(split.input_id, "fragment-size", identity) / "selected.json"
    selection = json.loads(selector.read_bytes())
    selection["attempt_id"] = "../../invalid"
    selector.write_text(json.dumps(selection))

    reloaded = views.load_sample(store, source_id)
    assert reloaded is not None
    damaged = next(item for item in reloaded.derived if item.input_id == split.input_id)
    assert "fragment-size" in damaged.unreadable
    status = views._split_half_status(damaged, "fragment-size", "mode_bp", 167.0,
                                      {"kind": "relative", "tolerance": 0.1})
    assert status["state"] == "unavailable"
    assert status["reason"] == "result unreadable"


def _damage_full_selection(store: LabStore, full_id: str, identity: str) -> None:
    selector = store.result_dir(full_id, "fragment-size", identity) / "selected.json"
    selection = json.loads(selector.read_bytes())
    selection["attempt_id"] = "../../invalid"
    selector.write_text(json.dumps(selection))


def test_damaged_full_selection_is_disclosed_without_a_metric_panel(tmp_path) -> None:
    store, source_id, full_id, _, identity = _published_split_store(tmp_path)
    _damage_full_selection(store, full_id, identity)
    sample = views.load_sample(store, source_id)
    assert sample is not None and sample.full.unreadable == {"fragment-size"}
    assert "fragment-size" not in sample.full.results
    assert all(panel["algorithm_id"] != "fragment-size" for panel in views.stability_panels(sample))

    payload = web_api.LabApi(store).sample(sample.id12)
    assert payload["stability_unavailable"] == [
        {"algorithm_id": "fragment-size", "reason": "full-depth result unreadable"},
    ]
    assert all(panel["algorithm_id"] != "fragment-size" for panel in payload["stability"])


@needs_chrome
def test_damaged_full_selection_is_named_on_stability_page(tmp_path) -> None:
    store, source_id, full_id, _, identity = _published_split_store(tmp_path)
    _damage_full_selection(store, full_id, identity)
    with RunningLabServer(store) as server:
        id12 = web_api.LabApi(store)._id12s()[source_id]
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{id12}/stability",
                   want=lambda page: "full-depth result unreadable" in page)
        home = _dom(server, tmp_path / "browser-home", route="/",
                    want=lambda page: "full-depth algorithm result unavailable" in page)
    assert "fragment-size: full-depth result unreadable; no metric-level Stability panel can be shown." in dom
    assert "1 full-depth algorithm result unavailable" in home


def test_usable_to_agrees_with_the_curve(default_run) -> None:
    root, payload = default_run
    sample = views.load_sample(LabStore(root), payload["data"]["sample"]["source_id"])
    curve = {row["algorithm_id"]: row for row in views.depth_curve(sample)["rows"]}
    for panel in views.stability_panels(sample):
        if not panel["headline"]:
            continue
        cells = [c for c in curve[panel["algorithm_id"]]["cells"] if c["fraction"] < 1.0]
        if panel["verdict"].startswith("usable to "):
            floor = float(panel["verdict"].split()[-1])
            assert all(c["state"] == "ok_usable" for c in cells if c["fraction"] >= floor)


def test_null_split_values_never_break_panels() -> None:
    from traceback_lab.views import _numeric

    assert not _numeric(None) and not _numeric(True) and _numeric(3)


def test_split_half_missingness_keeps_each_status_visible() -> None:
    spec = {"kind": "relative", "tolerance": 0.1}
    def metric(value):
        return SimpleNamespace(metric_id="mode_bp", value=value)
    view = views.InputView("split-A", "a" * 64, "split", {"half": "A"}, {}, 0.5)
    status = views._split_half_status

    assert status(None, "fragment-size", "mode_bp", 100, spec)["state"] == "not_recorded"
    assert status(None, "fragment-size", "mode_bp", 100, spec,
                  other_half_recorded=True, half_label="B")["reason"] == "no input labelled B recorded"
    assert status(view, "fragment-size", "mode_bp", 100, spec) == {
        "state": "not_recorded", "reason": "no selected result recorded", "value": None, "deviation": None,
    }
    view.unreadable = {"fragment-size"}
    assert status(view, "fragment-size", "mode_bp", 100, spec)["reason"] == "result unreadable"
    view.unreadable = set()
    view.ambiguous = {"fragment-size"}
    assert status(view, "fragment-size", "mode_bp", 100, spec)["reason"] == "result selection ambiguous"
    view.ambiguous = set()

    view.results["fragment-size"] = SimpleNamespace(status="skipped", skip_reason="depth")
    assert status(view, "fragment-size", "mode_bp", 100, spec) == {
        "state": "skipped", "reason": "depth", "value": None, "deviation": None,
    }
    view.results["fragment-size"] = SimpleNamespace(status="failed")
    assert status(view, "fragment-size", "mode_bp", 100, spec)["state"] == "failed"


    view.results["fragment-size"] = SimpleNamespace(status="ok", metrics=[metric(None)])
    assert status(view, "fragment-size", "mode_bp", 100, spec)["state"] == "not_interpretable"
    view.results["fragment-size"] = SimpleNamespace(status="ok", metrics=[metric(float("nan"))])
    assert status(view, "fragment-size", "mode_bp", 100, spec)["state"] == "not_interpretable"
    view.results["fragment-size"] = SimpleNamespace(status="ok", metrics=[metric(105)])
    assert status(view, "fragment-size", "mode_bp", 100, spec) == {
        "state": "ok", "reason": None, "value": 105, "deviation": 5.0, "unit": "percent",
    }
    assert status(view, "fragment-size", "mode_bp", float("nan"), spec) == {
        "state": "ok", "reason": None, "value": 105, "deviation": None, "unit": "none",
    }
    view.results["fragment-size"] = SimpleNamespace(status="ok", metrics=[metric(10**400)])
    assert status(view, "fragment-size", "mode_bp", 100, spec)["state"] == "not_interpretable"


def test_missing_downsample_does_not_claim_instability() -> None:
    metric = SimpleNamespace(metric_id="mode_bp", value=167.0, n=None, ci=None, ci_level=None)
    full = views.InputView("full", "a" * 64, None, {}, {
        "fragment-size": SimpleNamespace(status="ok", metrics=[metric], depth=None),
    })
    down = views.InputView("0.5", "b" * 64, "downsample", {"fraction": 0.5}, {}, 0.5)
    sample = views.SampleView("c" * 64, "c" * 12, False, full, [down])
    down.unreadable = {"fragment-size"}
    assert views.cell_state(down, full, "fragment-size", "mode_bp", views.load_library()) == {
        "state": "unavailable", "reason": "result unreadable",
    }
    curve_row = next(row for row in views.depth_curve(sample)["rows"] if row["algorithm_id"] == "fragment-size")
    assert curve_row["cells"][0]["reason"] == "result unreadable"
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert panel["verdict"] == "no usable measurement below full depth"
    assert panel["points"][0]["reason"] == "result unreadable"
    down.unreadable = set()
    sample.derived.append(views.InputView("split-X", "d" * 64, "split", {"half": "X"}, {}, 0.5))
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert {half: item["state"] for half, item in panel["split_status"].items()} == {
        "A": "unavailable", "B": "unavailable",
    }
    assert panel["split_status"]["A"]["reason"] == (
        "no input labelled A recorded; an unrecognized split label is present")
    assert panel["split_status"]["B"]["reason"] == (
        "no input labelled B recorded; an unrecognized split label is present")
    sample.derived.append(views.InputView("split-A", "f" * 64, "split", {"half": "A"}, {
        "fragment-size": SimpleNamespace(status="ok", metrics=[metric], depth=None),
    }, 0.5))
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert panel["split_status"]["A"]["state"] == "ok"
    assert panel["split_status"]["B"]["reason"] == (
        "no input labelled B recorded; an unrecognized split label is present")
    sample.derived.append(views.InputView("split-B", "9" * 64, "split", {"half": "B"}, {
        "fragment-size": SimpleNamespace(status="ok", metrics=[metric], depth=None),
    }, 0.5))
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert panel["split"] is not None
    assert panel["split_warning"] == "an unrecognized split label is present"
    down.results["fragment-size"] = SimpleNamespace(
        status="ok", metrics=[SimpleNamespace(metric_id="mode_bp", value=190.0,
                                               n=None, ci=None, ci_level=None)],
        depth=SimpleNamespace(approx_depth_x=0.5),
    )
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert panel["points"][0]["state"] == "ok_unstable"
    assert panel["verdict"] == "unstable below full depth"
    down.results.clear()
    lower = views.InputView("0.25", "e" * 64, "downsample", {"fraction": 0.25}, {
        "fragment-size": SimpleNamespace(status="ok", metrics=[metric], depth=None),
    }, 0.25)
    sample.derived.append(lower)
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert [point["state"] for point in panel["points"]] == ["ok_usable", None, "ok_usable"]
    assert panel["verdict"] == "usable only at isolated lower depths"


def test_repeatability_preserves_raw_splits_without_inventing_a_verdict() -> None:
    no_verdict = {"label": "WPS mean", "split": {
        "a": 100, "b": 105, "relative_abs_diff": 5 / 102.5, "stable": None,
    }}
    validated = {"label": "Fragment mode", "split": {
        "a": 100, "b": 110, "relative_abs_diff": 10 / 105, "stable": True,
    }}
    raw_only = views.repeatability([no_verdict])
    assert raw_only is not None
    assert raw_only["metrics"] == 1
    assert raw_only["verdict_metrics"] == 0
    assert raw_only["no_verdict_metrics"] == 1
    assert raw_only["incomparable_metrics"] == 0
    assert raw_only["worst"][0]["stable"] is None
    report = views.repeatability([no_verdict, validated])
    assert report is not None
    assert report["metrics"] == 2
    assert report["verdict_metrics"] == 1
    assert report["no_verdict_metrics"] == 1
    assert report["incomparable_metrics"] == 0
    assert report["median_relative_abs_diff"] == ((5 / 102.5) + (10 / 105)) / 2
    assert report["unstable"] == 0
    missing = {"label": "Copy number", "split": None, "split_status": {
        "A": {"state": "ok"}, "B": {"state": "unavailable"},
    }}
    unrecorded = {"label": "Unrun algorithm", "split": None, "split_status": {
        "A": {"state": "not_recorded"}, "B": {"state": "not_recorded"},
    }}
    assert views.repeatability([no_verdict, validated, missing, unrecorded])["incomparable_metrics"] == 1
