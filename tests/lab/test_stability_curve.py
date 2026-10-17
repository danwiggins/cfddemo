"""S5: D32 stability table, the default depth curve and Stability-lens data."""

from __future__ import annotations

import json

import pytest

from traceback_lab import views
from traceback_lab.controls import derived_plan_items
from traceback_lab.envelope import Failure, Metric, ResultEnvelope
from traceback_lab.interpretation.engine import load_library
from traceback_lab.read_summary import ReadSummary, depth_stamp, derived_mask
from traceback_lab.registry import load_registry, plan
from traceback_lab.stability import Side, band_at, deviation, stable
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
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
    block = views.repeatability(panels)
    assert block["metrics"] == len([p for p in panels if p["split"]])
    _assert_default_scalar_selection(store, sample, payload)


def _assert_default_scalar_selection(store, sample, payload) -> None:
    """The default selection must leave a status at every measured depth."""

    library = load_library()
    chosen = plan(load_registry())
    full_ids = {item.algorithm.id for item in chosen}
    derived_ids = full_ids - {"read-summary", "meth-calls"}
    for transform in ("downsample", "split"):
        assert {item.algorithm.id for item in derived_plan_items(chosen, transform)} == derived_ids
    steps = {(step["input"], step["algorithm_id"]): step for step in payload["data"]["steps"]}
    assert len(steps) == len(payload["data"]["steps"]), "a selected step was duplicated"
    labels = {"full", "0.5", "0.25", "0.1", "split-A", "split-B"}
    assert {label for label, _ in steps} == labels
    assert set(sample.full.results) == full_ids

    summary_result = sample.full.results["read-summary"]
    attempt = store.selected_attempt(sample.full.input_id, "read-summary", summary_result.provenance.parameters_sha256)
    assert attempt is not None
    summary = ReadSummary.open(attempt / "artifacts" / "columns")
    views_by_label = {view.label: view for view in [sample.full, *sample.derived] if view.label in labels}
    assert set(views_by_label) == labels
    assert len({view.input_id for view in views_by_label.values()}) == len(labels)
    for label, view in views_by_label.items():
        expected_ids = full_ids if label == "full" else derived_ids
        assert {algorithm_id for step_label, algorithm_id in steps if step_label == label} == expected_ids
        assert set(view.results) == expected_ids, f"selected results omitted from {label}"
        mask = derived_mask(summary, {"transform": view.transform, **view.parameters}) if label != "full" else None
        stamp = depth_stamp(summary, mask)
        for algorithm_id, result in view.results.items():
            assert result.status in {"ok", "skipped", "failed"}
            assert result.status == steps[label, algorithm_id]["status"]
            assert result.provenance.input_id == view.input_id
            assert result.provenance.source_id == sample.source_id
            if algorithm_id == "read-summary":
                assert label == "full" and result.depth is None
            else:
                assert result.depth is not None and result.depth.eligible_reads == stamp["eligible_reads"]
                assert result.depth.approx_depth_x == pytest.approx(stamp["approx_depth_x"])
            if result.status == "skipped":
                assert result.skip_reason == steps[label, algorithm_id]["skip_reason"]
            elif result.status == "failed":
                assert result.failure is not None
            else:
                headline = library.headline(algorithm_id)
                if headline is not None and algorithm_id not in library.hidden_algorithms:
                    assert headline.metric_id in {metric.metric_id for metric in result.metrics}

    full_count = sample.full.results["bam-qc"].depth.eligible_reads
    halves = sample.by_transform("split")
    assert sum(view.results["bam-qc"].depth.eligible_reads for view in halves) == full_count
    downs = sorted(sample.by_transform("downsample"), key=lambda view: view.fraction)
    counts = [view.results["bam-qc"].depth.eligible_reads for view in downs]
    assert counts[0] < counts[1] < counts[2] < full_count
    assert all(abs(count / full_count - view.fraction) < 0.08 for count, view in zip(counts, downs))

    api = LabApi(store).sample(sample.id12)
    assert api["found"] is True and api["source_id"] == sample.source_id
    algorithm_rows = {row["algorithm_id"]: row for row in api["algorithms"]}
    assert set(algorithm_rows) == full_ids
    for algorithm_id, row in algorithm_rows.items():
        result = sample.full.results[algorithm_id]
        assert row["status"] == result.status
        assert row["provenance"]["parameters_sha256"] == result.provenance.parameters_sha256
        assert row["provenance"]["input_id"] == sample.full.input_id
        if result.status == "skipped":
            assert row["skip_reason"] == result.skip_reason and row["metrics"] == []
        elif result.status == "failed":
            assert row["failure"] is not None and row["metrics"] == []
        else:
            assert {metric["metric_id"] for metric in row["metrics"]} == {metric.metric_id for metric in result.metrics}
    catalog = {row["algorithm_id"]: row for row in api["catalog"]}
    assert set(catalog) == full_ids - library.hidden_algorithms
    assert all(row["status"] in {"ok", "skipped", "failed"} for row in catalog.values())
    expected_curve_ids = {
        algorithm_id for algorithm_id in full_ids - library.hidden_algorithms
        if library.headline(algorithm_id) is not None
    }
    assert {row["algorithm_id"] for row in api["curve"]["rows"]} == expected_curve_ids
    for curve_row in api["curve"]["rows"]:
        assert curve_row["metric_id"] == library.headline(curve_row["algorithm_id"]).metric_id
        assert [cell["label"] for cell in curve_row["cells"]] == ["0.1", "0.25", "0.5", "full"]
        for cell in curve_row["cells"]:
            assert cell["state"] in views.CELL_STATES, (curve_row["algorithm_id"], cell["label"])
            assert cell["depth_x"] == pytest.approx(views_by_label[cell["label"]].depth_x())

    # A later selected failure or null value changes the API's measured cell;
    # it must not fall back to the previous successful attempt or disappear.
    failure_view = views_by_label["0.1"]
    prior = failure_view.results["bam-qc"]
    failed = ResultEnvelope.model_validate({
        **prior.model_dump(), "status": "failed", "metrics": [], "panels": [],
        "failure": Failure(first_line="fixture worker failed", stderr_tail=[]).model_dump(),
    })
    store.publish_result(failed, input_id=failure_view.input_id)
    null_view = views_by_label["0.25"]
    prior = null_view.results["fragment-size"]
    fragment_headline = library.headline("fragment-size")
    assert fragment_headline is not None
    null_result = ResultEnvelope.model_validate({
        **prior.model_dump(), "status": "ok",
        "metrics": [Metric(metric_id=fragment_headline.metric_id, value=None, unit="bp").model_dump()],
        "panels": [], "failure": None,
    })
    store.publish_result(null_result, input_id=null_view.input_id)
    updated = LabApi(store).sample(sample.id12)
    updated_rows = {row["algorithm_id"]: row for row in updated["curve"]["rows"]}
    failed_cells = {cell["label"]: cell for cell in updated_rows["bam-qc"]["cells"]}
    null_cells = {cell["label"]: cell for cell in updated_rows["fragment-size"]["cells"]}
    assert set(failed_cells) == set(null_cells) == {"0.1", "0.25", "0.5", "full"}
    assert failed_cells["0.1"]["state"] == "failed"
    assert failed_cells["0.1"]["reason"] == "fixture worker failed"
    assert failed_cells["0.1"].get("value") is None
    assert null_cells["0.25"]["state"] == "not_interpretable"
    assert null_cells["0.25"]["value"] is None


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
