"""S5: D32 stability table, the default depth curve and Stability-lens data."""

from __future__ import annotations

import copy
import json
import math
from types import SimpleNamespace
from unittest.mock import patch

import pysam
import pytest

from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab import views
from traceback_lab.controls import derived_plan_items
from traceback_lab.envelope import Depth, Failure, Metric, Provenance, ResultEnvelope, provenance
from traceback_lab.interpretation.engine import Entry, NOT_INTERPRETABLE_REASONS, load_library
from traceback_lab.read_summary import ReadSummary, depth_stamp, derived_mask
from traceback_lab.registry import load_registry, plan
from traceback_lab.stability import Side, band_at, deviation, stable
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
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


def test_unvalidated_depth_dependent_fraction_has_no_stability_verdict() -> None:
    spec = {"kind": "unvalidated"}
    assert stable(spec, Side(0.2, n=1_000_000), Side(0.8, n=1_000_000)) is None
    assert deviation(spec, 0.2, 0.8, 0.5) == (None, "none")
    assert band_at(spec, Side(0.2), Side(0.8)) is None


def test_signed_arm_scalar_without_a_rule_keeps_raw_split_difference() -> None:
    def result(value):
        metric = SimpleNamespace(metric_id="chr1p_log2", value=value, n=12, ci=None, ci_level=None)
        return SimpleNamespace(status="ok", metrics=[metric], depth=None)

    full = views.InputView("full", "a" * 64, None, {}, {"arm-summary": result(0.2)})
    down = views.InputView("0.5", "b" * 64, "downsample", {"fraction": 0.5},
                           {"arm-summary": result(0.15)}, 0.5)
    a = views.InputView("split-A", "c" * 64, "split", {"half": "A"},
                        {"arm-summary": result(0.1)}, 0.5)
    b = views.InputView("split-B", "d" * 64, "split", {"half": "B"},
                        {"arm-summary": result(0.3)}, 0.5)
    sample = views.SampleView("e" * 64, "e" * 12, False, full, [down, a, b])
    panel = next(p for p in views.stability_panels(sample)
                 if p["algorithm_id"] == "arm-summary" and p["metric_id"] == "chr1p_log2")

    assert panel["points"][0]["state"] == "ok_unvalidated"
    assert panel["points"][0]["deviation"] is None
    assert panel["verdict"] == "stability not validated"
    assert panel["split_status"]["A"]["state"] == panel["split_status"]["B"]["state"] == "ok"
    assert panel["split"]["absolute_diff"] == pytest.approx(0.2)
    assert panel["split"]["stable"] is None


def test_deliberately_unpublished_value_is_not_called_usable_or_unvalidated() -> None:
    provenance = Provenance(
        tool="bam-qc", tool_version="1", parameters_sha256="a" * 64,
        source_id="b" * 64, input_id="c" * 64, reference_id="hg38",
        timestamp_utc="2026-10-17T00:00:00Z",
    )
    result = ResultEnvelope(
        algorithm_id="bam-qc", algorithm_version="1", status="ok", provenance=provenance,
        depth=Depth(eligible_reads=100, approx_depth_x=20),
        metrics=[Metric(metric_id="duplicate_fraction", value=None)],
    )
    full = views.InputView("full", "c" * 64, None, {}, {"bam-qc": result}, 1.0)
    half = views.InputView("0.5", "d" * 64, "downsample", {}, {"bam-qc": result}, 0.5)
    library = views.load_library()
    for view in (full, half):
        assert views.cell_state(view, full, "bam-qc", "duplicate_fraction", library) == {
            "state": "not_interpretable", "reason": "none_published", "value": None,
        }


def test_wps_control_values_are_measured_but_not_declared_usable() -> None:
    provenance = Provenance(
        tool="wps", tool_version="1", parameters_sha256="a" * 64,
        source_id="b" * 64, input_id="c" * 64, reference_id="hg38",
        timestamp_utc="2026-10-17T00:00:00Z",
    )

    def view(label: str, fraction: float, value: float, transform: str | None = None) -> views.InputView:
        result = ResultEnvelope(
            algorithm_id="wps", algorithm_version="1", status="ok", provenance=provenance,
            depth=Depth(eligible_reads=1_000_000, approx_depth_x=20 * fraction),
            metrics=[Metric(metric_id="qualifying_fragment_depth_x", value=value)],
        )
        return views.InputView(label, "c" * 64, transform, {"half": label[-1]} if transform == "split" else {},
                               {"wps": result}, fraction)

    full = view("full", 1.0, 0.7)
    half = view("0.5", 0.5, 0.4, "downsample")
    sample = views.SampleView("b" * 64, "b" * 12, False, full,
                              [half, view("split-A", 0.5, 0.2, "split"),
                               view("split-B", 0.5, 0.6, "split")])
    library = views.load_library()
    cell = views.cell_state(half, full, "wps", "qualifying_fragment_depth_x", library)
    assert cell == {"state": "ok_unvalidated", "value": 0.4, "verdict": None}
    panel = next(p for p in views.stability_panels(sample, library)
                 if p["metric_id"] == "qualifying_fragment_depth_x")
    assert panel["verdict"] == "stability not validated"
    assert panel["points"][0]["value"] == 0.4
    assert panel["points"][0]["deviation"] is None
    assert panel["split"]["stable"] is None
    repeatability = views.repeatability(views.stability_panels(sample, library))
    assert repeatability["metrics"] == 1
    assert repeatability["no_verdict"] == 1
    assert repeatability["unstable"] == 0
    assert all(row["stable"] is None for row in repeatability["worst"])


def test_all_skipped_wps_controls_are_not_called_unstable() -> None:
    provenance = Provenance(
        tool="wps", tool_version="1", parameters_sha256="a" * 64,
        source_id="b" * 64, input_id="c" * 64, reference_id="hg38",
        timestamp_utc="2026-10-17T00:00:00Z",
    )
    full_result = ResultEnvelope(
        algorithm_id="wps", algorithm_version="1", status="ok", provenance=provenance,
        depth=Depth(eligible_reads=1_000_000, approx_depth_x=12),
        metrics=[Metric(metric_id="qualifying_fragment_depth_x", value=0.7)],
    )
    skipped_result = full_result.model_copy(update={"status": "skipped", "skip_reason": "depth", "metrics": []})
    full = views.InputView("full", "c" * 64, None, {}, {"wps": full_result}, 1.0)
    half = views.InputView("0.5", "d" * 64, "downsample", {}, {"wps": skipped_result}, 0.5)
    sample = views.SampleView("b" * 64, "b" * 12, False, full, [half])

    panel = next(p for p in views.stability_panels(sample)
                 if p["algorithm_id"] == "wps" and p["metric_id"] == "qualifying_fragment_depth_x")
    assert panel["points"][0]["state"] == "skipped"
    assert panel["verdict"] == "not measured below full depth"
def test_downsample_without_a_stability_rule_has_no_verdict() -> None:
    metric = SimpleNamespace(metric_id="unlisted_metric", value=3, n=None, ci=None, ci_level=None)
    result = SimpleNamespace(status="ok", metrics=[metric], depth=None)
    full = views.InputView("full", "a" * 64, None, {}, {"unknown-algorithm": result})
    half = views.InputView("0.5", "b" * 64, "downsample", {},
                           {"unknown-algorithm": result}, fraction=0.5)
    assert views.cell_state(half, full, "unknown-algorithm", "unlisted_metric", load_library()) == {
        "state": "not_interpretable", "reason": "no_stability_verdict", "value": 3,
    }
    assert "no_stability_verdict" in NOT_INTERPRETABLE_REASONS


def test_known_stability_rule_with_unavailable_full_value_is_not_comparable() -> None:
    half_metric = SimpleNamespace(metric_id="score", value=3, n=None, ci=None, ci_level=None)
    full_metric = SimpleNamespace(metric_id="score", value=None, n=None, ci=None, ci_level=None)
    half = views.InputView("0.5", "b" * 64, "downsample", {},
                           {"fixture": SimpleNamespace(status="ok", metrics=[half_metric], depth=None)}, 0.5)
    full = views.InputView("full", "a" * 64, None, {},
                           {"fixture": SimpleNamespace(status="ok", metrics=[full_metric], depth=None)})
    entry = Entry("fixture", "score", {"reference": {"kind": "none"}, "value_kind": "scalar", "stability": R})
    library = SimpleNamespace(entry=lambda algorithm_id, metric_id: entry)
    assert views.cell_state(half, full, "fixture", "score", library) == {
        "state": "not_interpretable", "reason": "comparison_unavailable", "value": 3,
    }
    assert "comparison_unavailable" in NOT_INTERPRETABLE_REASONS


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
    assert all(panel["split"]["absolute_diff"] == abs(panel["split"]["a"] - panel["split"]["b"])
               for panel in with_split)
    block = views.repeatability(panels)
    assert block["metrics"] == len([p for p in panels if p["split"]])
    _assert_default_scalar_selection(store, sample, payload)


def _assert_default_scalar_selection(store, sample, payload) -> None:
    """The default selection must leave a status at every measured depth."""

    library = load_library()
    chosen = plan(load_registry())
    full_ids = {item.algorithm.id for item in chosen}
    assert {"read-summary", "meth-calls", "bam-qc", "depth", "fragment-size"} <= full_ids
    derived_ids = full_ids - {"read-summary", "meth-calls"}
    for transform in ("downsample", "split"):
        assert {item.algorithm.id for item in derived_plan_items(chosen, transform)} == derived_ids
    steps = {(step["input"], step["algorithm_id"]): step for step in payload["data"]["steps"]}
    assert len(steps) == len(payload["data"]["steps"]), "a selected step was duplicated"
    labels = {"full", "0.5", "0.25", "0.1", "split-A", "split-B"}
    assert {label for label, _ in steps} == labels
    assert set(sample.full.results) == full_ids
    for algorithm_id in ("bam-qc", "depth", "fragment-size"):
        result = sample.full.results[algorithm_id]
        assert result.status == "ok", algorithm_id
        headline = library.headline(algorithm_id)
        assert headline is not None
        assert any(metric.metric_id == headline.metric_id and metric.value is not None
                   for metric in result.metrics)

    summary_result = sample.full.results["read-summary"]
    attempt = store.selected_attempt(sample.full.input_id, "read-summary", summary_result.provenance.parameters_sha256)
    assert attempt is not None
    summary = ReadSummary.open(attempt / "artifacts" / "columns")
    all_views = [sample.full, *sample.derived]
    assert len(all_views) == len(labels)
    views_by_label = {view.label: view for view in all_views}
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
            if algorithm_id in {"bam-qc", "depth", "fragment-size"}:
                assert result.status == "ok", (label, algorithm_id, result.status)
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
    assert all(abs(count / full_count - view.fraction) < min(0.08, max(0.02, 0.15 * view.fraction))
               for count, view in zip(counts, downs))

    api = LabApi(store).sample(sample.id12)
    assert api["found"] is True and api["source_id"] == sample.source_id
    algorithm_rows = {row["algorithm_id"]: row for row in api["algorithms"]}
    assert len(algorithm_rows) == len(api["algorithms"])
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
    assert len(catalog) == len(api["catalog"])
    assert set(catalog) == full_ids - library.hidden_algorithms
    assert {"bam-qc", "depth", "fragment-size"} <= set(catalog)
    assert all(row["status"] in {"ok", "skipped", "failed"} for row in catalog.values())
    expected_curve_ids = {
        algorithm_id for algorithm_id in full_ids - library.hidden_algorithms
        if library.headline(algorithm_id) is not None
    }
    assert expected_curve_ids
    assert {"bam-qc", "depth", "fragment-size"} <= expected_curve_ids
    assert len(api["curve"]["rows"]) == len(expected_curve_ids)
    assert {row["algorithm_id"] for row in api["curve"]["rows"]} == expected_curve_ids
    before_cells = {}
    for curve_row in api["curve"]["rows"]:
        algorithm_id = curve_row["algorithm_id"]
        assert curve_row["metric_id"] == library.headline(algorithm_id).metric_id
        assert [cell["label"] for cell in curve_row["cells"]] == ["0.1", "0.25", "0.5", "full"]
        for cell in curve_row["cells"]:
            label = cell["label"]
            before_cells[algorithm_id, label] = copy.deepcopy(cell)
            view = views_by_label[label]
            result = view.results[algorithm_id]
            assert cell["state"] in views.CELL_STATES, (algorithm_id, label)
            assert cell["depth_x"] == pytest.approx(view.depth_x())
            assert result.depth is not None
            assert cell["depth_x"] == pytest.approx(result.depth.approx_depth_x)
            if result.status != "ok":
                assert cell["state"] == result.status
                assert cell.get("value") is None
                continue
            metric = next(metric for metric in result.metrics if metric.metric_id == curve_row["metric_id"])
            if isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool):
                assert cell["value"] == pytest.approx(metric.value)
            else:
                assert cell.get("value") == metric.value
            if algorithm_id in {"bam-qc", "depth", "fragment-size"}:
                assert cell["state"] in {"ok_usable", "ok_unstable"}
                assert isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool)
                assert math.isfinite(metric.value)
        if algorithm_id == "depth":
            xs = [cell["depth_x"] for cell in curve_row["cells"]]
            assert xs == sorted(set(xs)), xs
            assert xs[-1] > 0
            assert all(abs(value / xs[-1] - fraction) < min(0.08, max(0.02, 0.15 * fraction))
                       for value, fraction in zip(xs[:-1], (0.1, 0.25, 0.5)))

    # A later selected failure or null value changes the API's measured cell;
    # it must not fall back to the previous successful attempt or disappear.
    for key in (("bam-qc", "0.1"), ("fragment-size", "0.25")):
        assert before_cells[key]["state"] in {"ok_usable", "ok_unstable"}
        assert before_cells[key]["value"] is not None
    failure_view = views_by_label["0.1"]
    prior = failure_view.results["bam-qc"]
    previous_attempt = store.selected_attempt(failure_view.input_id, "bam-qc", prior.provenance.parameters_sha256)
    assert previous_attempt is not None
    failed = ResultEnvelope.model_validate({
        **prior.model_dump(), "status": "failed", "metrics": [], "panels": [],
        "failure": Failure(first_line="fixture worker failed", stderr_tail=[]).model_dump(),
    })
    store.publish_result(failed, input_id=failure_view.input_id)
    selected_attempt = store.selected_attempt(failure_view.input_id, "bam-qc", prior.provenance.parameters_sha256)
    assert selected_attempt is not None and selected_attempt != previous_attempt
    assert previous_attempt.exists(), "the earlier success must remain a real fallback candidate"
    null_view = views_by_label["0.25"]
    prior = null_view.results["fragment-size"]
    fragment_headline = library.headline("fragment-size")
    assert fragment_headline is not None
    previous_null_attempt = store.selected_attempt(
        null_view.input_id, "fragment-size", prior.provenance.parameters_sha256,
    )
    assert previous_null_attempt is not None
    null_result = ResultEnvelope.model_validate({
        **prior.model_dump(), "status": "ok",
        "metrics": [Metric(metric_id=fragment_headline.metric_id, value=None, unit="bp").model_dump()],
        "panels": [], "failure": None,
    })
    store.publish_result(null_result, input_id=null_view.input_id)
    selected_null_attempt = store.selected_attempt(
        null_view.input_id, "fragment-size", prior.provenance.parameters_sha256,
    )
    assert selected_null_attempt is not None and selected_null_attempt != previous_null_attempt
    assert previous_null_attempt.exists(), "the earlier scalar must remain a real fallback candidate"
    updated = LabApi(store).sample(sample.id12)
    updated_rows = {row["algorithm_id"]: row for row in updated["curve"]["rows"]}
    assert len(updated_rows) == len(updated["curve"]["rows"])
    assert set(updated_rows) == expected_curve_ids
    for algorithm_id, row in updated_rows.items():
        assert [cell["label"] for cell in row["cells"]] == ["0.1", "0.25", "0.5", "full"]
        for cell in row["cells"]:
            if (algorithm_id, cell["label"]) not in {("bam-qc", "0.1"), ("fragment-size", "0.25")}:
                assert cell == before_cells[algorithm_id, cell["label"]]
    failed_cells = {cell["label"]: cell for cell in updated_rows["bam-qc"]["cells"]}
    null_cells = {cell["label"]: cell for cell in updated_rows["fragment-size"]["cells"]}
    assert set(failed_cells) == set(null_cells) == {"0.1", "0.25", "0.5", "full"}
    assert failed_cells["0.1"]["state"] == "failed"
    assert failed_cells["0.1"]["reason"] == "fixture worker failed"
    assert failed_cells["0.1"].get("value") is None
    assert null_cells["0.25"]["state"] == "not_interpretable"
    assert null_cells["0.25"]["value"] is None

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
    issues = {}
    rows = views.selected_results(store, sample.derived[0].input_id, {"fragment-size": "0" * 64}, issues=issues)
    assert rows == {}
    assert "fragment-size" in issues["stale"]


def _published_split_store(tmp_path, *, split_identity=None):
    store = LabStore(tmp_path)
    store.ensure()
    source_id, full_id, split_id, identity = (character * 64 for character in "abcd")
    manifest = store.inputs_dir / full_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind":"prealigned","platform":"ont"}')
    with pysam.AlignmentFile(
        str(manifest.parent / "aligned.bam"), "wb",
        header={"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
                "RG": [{"ID": "ont", "PL": "ONT"}]},
    ):
        pass
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
                tool="test", source_id=source_id,
                parameters_sha256=split_identity if input_id == split_id and split_identity else identity,
                input_id=input_id, reference_id="ref", timestamp_utc="2026-10-16T00:00:00Z",
            ),
            "metrics": [{"metric_id": "mode_bp", "value": 167.0}],
        })
        store.publish_result(envelope, input_id=input_id)
    return store, source_id, full_id, split_id, identity


def test_stale_split_selection_is_visible_but_not_compared(tmp_path) -> None:
    store, source_id, _, _, _ = _published_split_store(tmp_path, split_identity="e" * 64)
    sample = views.load_sample(store, source_id)
    assert sample is not None
    half = sample.by_transform("split")[0]
    assert half.results == {} and half.stale == {"fragment-size"}
    panel = next(panel for panel in views.stability_panels(sample)
                 if panel["algorithm_id"] == "fragment-size" and panel["metric_id"] == "mode_bp")
    assert panel["split"] is None
    assert panel["split_status"]["A"] == {
        "state": "unavailable", "reason": "result parameters differ from full depth",
        "value": None, "deviation": None,
    }
    assert views.repeatability([panel]) is None


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
        home = _dom(server, tmp_path / "browser-home", route=f"/?sample={id12}",
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
    view.stale = {"fragment-size"}
    assert status(view, "fragment-size", "mode_bp", 100, spec) == {
        "state": "unavailable", "reason": "result parameters differ from full depth",
        "value": None, "deviation": None,
    }
    view.stale = set()

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
