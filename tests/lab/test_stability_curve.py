"""S5: D32 stability table, the default depth curve and Stability-lens data."""

from __future__ import annotations

import json

import pytest

from traceback_lab import views
from traceback_lab.stability import Side, band, deviation, stable
from traceback_lab.store import LabStore
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
    assert band(A, 167) == (3.0, "unit")
    assert band(R, 100.0) == (10.0, "percent")


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
    assert curve["rows"][-1]["algorithm_id"] == "copy-number"  # skipped rows sort last
    depths = [cell["depth_x"] for cell in rows["depth"]["cells"]]
    assert depths == sorted(depths)
    panels = views.stability_panels(sample)
    assert panels[0]["headline"]
    assert all(panel["split"] is not None for panel in panels if panel["algorithm_id"] != "copy-number")
    block = views.repeatability(panels)
    assert block["metrics"] == len([p for p in panels if p["split"]])


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
