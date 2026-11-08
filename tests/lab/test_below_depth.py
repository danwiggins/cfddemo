"""``lab run --below-depth``: exploratory runs below method depth floors, marked underpowered."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms.aligned_end_structure import ALIGNED_END_STRUCTURE
from traceback_lab.algorithms.griffin import GRIFFIN
from traceback_lab.algorithms.tss_profile import TSS_PROFILE
from traceback_lab.algorithms.wps import WPS
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import UNDERPOWERED_CAPTION, interpret_result, load_library
from traceback_lab.read_summary import COLUMNS, SCHEMA
from traceback_lab.registry import InputContext, Ready, Skip, registry_entries
from traceback_lab.report_command import (
    UNDERPOWERED_BANNER, UNDERPOWERED_NOTE, project_report, render_html, report_scrubber,
)
from traceback_lab.store import LabStore
from traceback_lab.web.api import LabApi
from traceback_runner import cli as signed_cli
from traceback_runner.references import register_reference

ONLY = "sex-check,end-motifs,fetal-fraction,short-read-fragment-size"
HG38_CHR1 = 248_956_422


def _run(capsys, root: Path, chunks: Path, *extra: str) -> tuple[int, dict, str]:
    code = signed_cli.main(["lab", "run", str(chunks), "--root", str(root), "--only", ONLY,
                            "--no-controls", "--downsample", "0.5", "--json", *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def _full(payload: dict) -> dict[str, dict]:
    return {step["algorithm_id"]: step for step in payload["data"]["steps"] if step["input"] == "full"}


def _run_start(root: Path, run_id: str) -> dict:
    events = [json.loads(line) for line in (root / "lab" / "runs" / f"{run_id}.jsonl").read_text().splitlines()]
    return next(event for event in events if event.get("event") == "run_start")


@pytest.fixture(scope="module")
def runs(tmp_path_factory, synthetic_world):
    """One store run three times: default, --below-depth, default again (one capsys each)."""

    root = tmp_path_factory.mktemp("below-depth") / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    return root, synthetic_world["chunks"]


def test_default_skips_below_depth_runs_and_marks_and_default_skips_again(runs, capsys) -> None:
    root, chunks = runs
    # (a) without the flag the depth floors skip, as before
    code, default, _ = _run(capsys, root, chunks)
    assert code == 0
    steps = _full(default)
    eligible = None
    for algorithm_id in ("sex-check", "end-motifs", "fetal-fraction"):
        assert steps[algorithm_id]["status"] == "skipped"
        assert steps[algorithm_id]["skip_reason"] == "depth"
        assert steps[algorithm_id]["underpowered"] is None
    assert default["data"]["below_depth"] is False and default["data"]["underpowered"] == []
    assert _run_start(root, default["data"]["run_id"])["flags"]["below_depth"] is False

    # (b) --below-depth runs them and marks every such result underpowered
    code, below, err = _run(capsys, root, chunks, "--below-depth")
    assert code == 0
    steps = _full(below)
    for algorithm_id, floor in (("sex-check", 500_000), ("end-motifs", 500_000)):
        step = steps[algorithm_id]
        assert step["status"] == "ok", step
        mark = step["underpowered"]
        assert mark["floor"] == floor and mark["kind"] == "eligible_reads"
        eligible = mark["observed"]
        assert 0 < eligible < floor
        assert f"ok (underpowered: {eligible:,} / {floor:,} reads;" in err
    derived = [step for step in below["data"]["steps"]
               if step["input"] != "full" and step["algorithm_id"] == "sex-check"]
    assert derived and all(step["status"] == "ok" and step["underpowered"]["observed"] < eligible
                           for step in derived)
    assert below["data"]["below_depth"] is True
    assert {(row["algorithm_id"], row["input"]) for row in below["data"]["underpowered"]} >= {
        ("sex-check", "full"), ("end-motifs", "full")}
    assert _run_start(root, below["data"]["run_id"])["flags"]["below_depth"] is True
    # (c) non-depth skips and assay-contract floors still skip
    assert steps["short-read-fragment-size"]["skip_reason"] == "platform"
    assert steps["fetal-fraction"]["status"] == "skipped"
    assert steps["fetal-fraction"]["skip_reason"] == "depth"
    assert steps["fetal-fraction"]["underpowered"] is None
    # read-summary and other at-floor results are not marked
    assert steps["read-summary"]["underpowered"] is None

    # the stored result JSON carries the mark; unmarked results keep their bytes
    store = LabStore(root)
    stored = {}
    for path in (root / "lab" / "results").rglob("result.json"):
        envelope = ResultEnvelope.from_bytes(path.read_bytes())
        if envelope.provenance.input_id == below["data"]["sample"]["input_id"]:
            stored.setdefault(envelope.algorithm_id, []).append((envelope, path.read_bytes()))
    marked = [envelope for envelope, _ in stored["sex-check"] if envelope.underpowered is not None]
    assert marked and marked[0].underpowered.floor == 500_000
    assert all(b'"underpowered"' not in raw for envelope, raw in stored["read-summary"])

    # the Lab web payload shows the mark, and no call is made from it
    id12 = LabApi(store).samples()["default_sample_id12"]
    sample = LabApi(store).sample(id12)
    algorithm = next(row for row in sample["algorithms"] if row["algorithm_id"] == "sex-check")
    assert algorithm["underpowered"]["kind"] == "eligible_reads"
    assert algorithm["metrics"]
    assert all(metric["state"] == "not_interpretable" and metric["reason"] == "depth"
               for metric in algorithm["metrics"])

    # lab report --html: banner plus a limitation, in step with the browser projection
    report = project_report(sample, "2026-11-07T12:00:00Z", report_scrubber(Path("/nonexistent")))
    assert f"Method sex-check: {UNDERPOWERED_NOTE}" in report["limitations"]
    page = render_html(report, "CLIA copy.")
    assert 'id="underpowered"' in page and UNDERPOWERED_BANNER.replace("'", "&#x27;") in page

    # a later default run does not reuse the underpowered result: it skips again
    code, again, _ = _run(capsys, root, chunks)
    assert code == 0
    steps = _full(again)
    assert steps["sex-check"]["status"] == "skipped" and steps["sex-check"]["skip_reason"] == "depth"
    assert steps["sex-check"]["underpowered"] is None
    rerun = LabApi(store).sample(id12)
    assert next(row for row in rerun["algorithms"] if row["algorithm_id"] == "sex-check")["underpowered"] is None


def test_dry_run_names_the_underpowered_plan(runs, capsys) -> None:
    root, chunks = runs
    code = signed_cli.main(["lab", "run", "latest", "--root", str(root), "--only", "sex-check",
                            "--dry-run", "--below-depth", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = next(row for row in payload["data"]["plan"] if row["algorithm_id"] == "sex-check")
    assert row["decision"] == "runs underpowered below depth minimum (--below-depth)"


def _ctx(tmp_path: Path, **updates: object) -> InputContext:
    data = dict(
        root=tmp_path / "root", source_id="1" * 64, input_id="2" * 64, reference_id="hg38",
        reference_sha256="3" * 64, input_dir=tmp_path / "input", eligible_reads=1_000, approx_depth_x=0.4,
        extras={},
    )
    data.update(updates)
    return InputContext(**data)


def _below(ctx: InputContext) -> InputContext:
    return replace(ctx, extras={**ctx.extras, "below_depth": True})


@pytest.mark.parametrize("name", [
    name for name, algorithm in registry_entries() if algorithm.requires.min_eligible_reads
])
def test_every_eligible_read_floor_is_bypassed_unless_it_is_an_assay_contract(tmp_path, name) -> None:
    from traceback_lab.algorithms._base import Base

    algorithm = dict(registry_entries())[name]
    ctx = _ctx(tmp_path)
    floor = Base.preflight(algorithm, ctx)
    assert isinstance(floor, Skip) and floor.reason_code == "depth"
    assert (floor.floor, floor.observed, floor.floor_kind) == (
        algorithm.requires.min_eligible_reads, 1_000, "eligible_reads")
    bypassed = Base.preflight(algorithm, _below(ctx))
    if algorithm.id in ("fetal-fraction", "fetal-fraction-chry", "methylbert"):
        assert bypassed == floor
    else:
        assert isinstance(bypassed, Ready)


def test_wps_and_griffin_ont_floors_bypass_to_their_next_gate(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    wps = WPS.preflight(ctx)
    assert wps.reason_code == "depth" and (wps.floor, wps.floor_kind) == (10.0, "depth_x")
    assert WPS.preflight(_below(ctx)).reason_code == "upstream"  # no read summary here
    griffin = GRIFFIN.preflight(ctx)
    assert griffin.reason_code == "depth" and griffin.floor == 5_000_000
    assert GRIFFIN.preflight(_below(ctx)).reason_code == "reference"  # no registered FASTA here
    # a missing measurement is not a floor: never bypassed
    unknown = replace(_below(ctx), eligible_reads=None, approx_depth_x=None)
    assert WPS.preflight(unknown).reason_code == "depth"
    assert GRIFFIN.preflight(unknown).reason_code == "depth"


def _summary(parent: Path, rows: int, *, length: int = 1000) -> None:
    columns = parent / "columns"
    columns.mkdir(parents=True)
    values = {"contig": [0] * rows, "start": [440] * rows, "end": [561] * rows,
              "flag": [0] * rows, "mapq": [60] * rows,
              "h_ds": [0, 2**62, 2**62, 3 * 2**62] * (rows // 4), "h_split": [0, 2**63] * (rows // 2)}
    for name, dtype in COLUMNS.items():
        np.asarray(values.get(name, [0] * rows), dtype=dtype).tofile(columns / f"{name}.bin")
    (columns / "meta.json").write_text(json.dumps({
        "schema": SCHEMA, "rows": rows, "columns": COLUMNS, "contigs": ["chr1"],
        "lengths": [length], "index_mapped_upper_bound": rows,
    }), encoding="utf-8")


def test_tss_profile_floor_bypasses_to_its_asset_gate(tmp_path) -> None:
    parent = tmp_path / "read-summary"
    _summary(parent, 8, length=HG38_CHR1)
    ctx = _ctx(tmp_path, extras={"upstream": {"read-summary": str(parent)}})
    floor = TSS_PROFILE.preflight(ctx)
    assert floor.reason_code == "depth" and (floor.floor, floor.observed) == (5_000_000, 8)
    assert TSS_PROFILE.preflight(_below(ctx)).reason_code == "missing_asset"


def test_aligned_end_structure_runs_below_10x_and_derived_does_not_crash(tmp_path) -> None:
    from traceback_lab.end_catalog_assets import register_end_catalogues

    root = tmp_path / "root"
    (tmp_path / "centers.bed").write_text("chr1\t490\t510\n", encoding="ascii")
    (tmp_path / "preferred.bed").write_text("chr1\t440\t441\tU\nchr1\t560\t561\tD\n", encoding="ascii")
    register_end_catalogues(root, centers=tmp_path / "centers.bed", preferred=tmp_path / "preferred.bed",
                            source_label="synthetic hg38 end catalogues", centers_source="synthetic-centers-v1",
                            preferred_source="synthetic-sites-v1", terms="unknown", license_id=None)
    (root / "input").mkdir()
    (root / "input" / "manifest.json").write_text(json.dumps({"kind": "aligned"}), encoding="utf-8")
    parent = tmp_path / "read-summary"
    _summary(parent, 8)
    ctx = _ctx(tmp_path, root=root, input_dir=root / "input", eligible_reads=8, approx_depth_x=0.97,
               extras={"upstream": {"read-summary": str(parent)}})
    floor = ALIGNED_END_STRUCTURE.preflight(ctx)
    assert floor.reason_code == "depth" and floor.floor_kind == "depth_x"
    with pytest.raises(ValueError, match="at least 10x"):
        ALIGNED_END_STRUCTURE.run(ctx, tmp_path / "out")
    assert isinstance(ALIGNED_END_STRUCTURE.preflight(_below(ctx)), Ready)
    output = ALIGNED_END_STRUCTURE.run(_below(ctx), tmp_path / "out")
    assert json.loads(output.artifacts["aligned_end_structure.json"])["valid_molecules"] == 8
    derived = replace(_below(ctx), derived={"transform": "split", "half": "A"})
    assert isinstance(ALIGNED_END_STRUCTURE.preflight(derived), Ready)
    output = ALIGNED_END_STRUCTURE.run(derived, tmp_path / "out")
    assert json.loads(output.artifacts["aligned_end_structure.json"])["valid_molecules"] == 4


def _envelope(**updates: object) -> ResultEnvelope:
    data = {
        "algorithm_id": "sex-check", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(tool="traceback_lab.sex_check", parameters_sha256="a" * 64,
                                 source_id="b" * 64, input_id="c" * 64, reference_id="hg38"),
        "metrics": [{"metric_id": "chry_fraction", "value": 0.001}],
    }
    data.update(updates)
    return ResultEnvelope.model_validate(data)


def test_envelope_mark_shape_and_interpretation() -> None:
    mark = {"floor": 500_000, "observed": 1_000, "kind": "eligible_reads"}
    envelope = _envelope(underpowered=mark)
    assert json.loads(envelope.to_bytes())["underpowered"] == mark
    assert "underpowered" not in json.loads(_envelope().to_bytes())
    with pytest.raises(ValueError, match="underpowered"):
        _envelope(status="skipped", skip_reason="depth", metrics=[], underpowered=mark)
    rows = interpret_result(envelope, load_library())
    assert rows and all(row.interpretation.state == "not_interpretable"
                        and row.interpretation.reason == "depth"
                        and row.interpretation.caption == UNDERPOWERED_CAPTION for row in rows)
