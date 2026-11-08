"""``lab run --below-depth``: exploratory runs below method depth floors, marked underpowered."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms._base import Base, depth_floor, metric
from traceback_lab.algorithms.aligned_end_structure import ALIGNED_END_STRUCTURE
from traceback_lab.algorithms.griffin import GRIFFIN
from traceback_lab.algorithms.tss_profile import TSS_PROFILE
from traceback_lab.algorithms.wps import WPS
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import UNDERPOWERED_CAPTION, interpret_result, load_library
from traceback_lab.read_summary import COLUMNS, SCHEMA
from traceback_lab.registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, registry_entries
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

    # never a cohort value, never a stability verdict
    from traceback_lab.cohorts import member_from_store
    from traceback_lab.views import load_sample, stability_panels

    source_id = below["data"]["sample"]["source_id"]
    member = member_from_store(store, source_id)
    pooled = {result.algorithm_id for result in member.results}
    assert "read-summary" in pooled and "sex-check" not in pooled and "end-motifs" not in pooled
    panels = [panel for panel in stability_panels(load_sample(store, source_id))
              if panel["algorithm_id"] in ("sex-check", "end-motifs")]
    assert panels and all(panel["verdict"].startswith("underpowered") and panel["split"] is None
                          for panel in panels)
    assert all(point["state"] != "ok_usable" for panel in panels for point in panel["points"])

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


def _dry(capsys, root: Path, *extra: str) -> dict[str, str]:
    code = signed_cli.main(["lab", "run", "latest", "--root", str(root), "--only",
                            "sex-check,fetal-fraction,fetal-fraction-chry,methylbert", "--dry-run", "--json", *extra])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    return {row["algorithm_id"]: row["decision"] for row in payload["data"]["plan"]}


def test_dry_run_names_the_underpowered_plan(runs, capsys) -> None:
    root, chunks = runs
    assert _run(capsys, root, chunks, "--below-depth")[0] == 0  # self-contained under xdist
    below = _dry(capsys, root, "--below-depth")
    assert below["sex-check"] == "runs underpowered below depth minimum (--below-depth)"
    for contract in ("fetal-fraction", "fetal-fraction-chry", "methylbert"):
        assert below[contract] == "skip expected below depth minimum (unless cached)", contract
    # After a --below-depth run the default plan still predicts the skip (an
    # underpowered result is never reused by a default run; see the run test).
    assert _dry(capsys, root)["sex-check"] == "skip expected below depth minimum (unless cached)"


# --- runner: run-time floors, run-time skips and upstream propagation ------------------------

@dataclass
class _RunTimeFloor(Base):
    """Preflight passes; run() re-checks a different, lower count (as Griffin ONT does)."""

    upstream: tuple[str, ...] = ()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput | Skip:
        floor = depth_floor(ctx, floor=100, observed=5, kind="eligible_reads", message="needs 100 reads")
        return floor if floor is not None else AlgorithmOutput(metrics=[metric("value", 1.0)])


@dataclass
class _LowSupport(Base):
    """Below its floor with --below-depth, run() finds nothing to measure."""

    upstream: tuple[str, ...] = ()
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=100))

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput | Skip:
        return Skip("low_support", "no qualifying fragments")


@dataclass
class _Floor(Base):
    upstream: tuple[str, ...] = ()
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=100))

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        (out / "x.txt").write_text("x", encoding="ascii")
        return AlgorithmOutput(metrics=[metric("value", 2.0)])


@dataclass
class _Downstream(Base):
    upstream: tuple[str, ...] = ("fake-floor",)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        return AlgorithmOutput(metrics=[metric("value", 3.0)])


FAKE_RUNTIME_FLOOR = _RunTimeFloor("fake-runtime-floor")
FAKE_LOW_SUPPORT = _LowSupport("fake-low-support")
FAKE_FLOOR = _Floor("fake-floor")
FAKE_DOWNSTREAM = _Downstream("fake-downstream")
_FAKES = (FAKE_RUNTIME_FLOOR, FAKE_LOW_SUPPORT, FAKE_FLOOR, FAKE_DOWNSTREAM)
_ENTRIES = {
    "fake-runtime-floor": "tests.lab.test_below_depth:FAKE_RUNTIME_FLOOR",
    "fake-low-support": "tests.lab.test_below_depth:FAKE_LOW_SUPPORT",
    "fake-floor": "tests.lab.test_below_depth:FAKE_FLOOR",
    "fake-downstream": "tests.lab.test_below_depth:FAKE_DOWNSTREAM",
}


def _plan(tmp_path: Path, below: bool) -> dict:
    from traceback_lab.registry import PlanItem
    from traceback_lab.runner import RunTarget, run_plan
    from traceback_lab.scrub import Scrubber
    from traceback_lab.store import new_run_id

    root = tmp_path / "root"
    store = LabStore(root)
    store.ensure()
    (tmp_path / "input").mkdir(exist_ok=True)
    target = RunTarget(
        input_id="2" * 64, source_id="1" * 64, input_dir=tmp_path / "input", reference_id="hg38",
        reference_sha256="3" * 64, eligible_reads=5, approx_depth_x=0.1,
        extras={"below_depth": True} if below else {},
    )
    run_id = new_run_id()
    results = run_plan(store, None, store.run_log(run_id), target,
                       [PlanItem(algorithm, False) for algorithm in _FAKES], _ENTRIES,
                       run_id=run_id, scrubber=Scrubber(root=root))
    out = {}
    for result in results:
        envelope = store.read_result(target.input_id, result.algorithm_id, result.parameters_sha256)
        out[result.algorithm_id] = (result, envelope)
    return out


def test_runner_marks_run_time_floors_and_upstreams_and_keeps_skips_unmarked(tmp_path) -> None:
    below = _plan(tmp_path, True)
    # (1) a floor passed only inside run() still marks the result
    result, envelope = below["fake-runtime-floor"]
    assert result.status == "ok" and envelope.underpowered is not None
    assert (envelope.underpowered.floor, envelope.underpowered.observed) == (100, 5)
    # (2) a run-time low_support skip publishes a plain skip, not an invalid marked envelope
    result, envelope = below["fake-low-support"]
    assert result.status == "skipped" and result.skip_reason == "low_support"
    assert envelope.status == "skipped" and envelope.underpowered is None and result.underpowered is None
    # (4) a result built on an underpowered upstream is itself underpowered
    result, envelope = below["fake-floor"]
    assert result.status == "ok" and envelope.underpowered.upstream is None
    result, envelope = below["fake-downstream"]
    assert result.status == "ok" and envelope.underpowered is not None
    assert envelope.underpowered.upstream == "fake-floor" and result.underpowered["upstream"] == "fake-floor"

    # a default run reuses none of the marked results
    default = _plan(tmp_path, False)
    assert default["fake-runtime-floor"][0].status == "skipped"
    assert default["fake-runtime-floor"][0].skip_reason == "depth"
    assert default["fake-floor"][0].status == "skipped"
    assert default["fake-downstream"][0].status == "skipped"
    assert default["fake-downstream"][0].skip_reason == "upstream"


def test_spike_controls_give_no_verdict_when_underpowered() -> None:
    from types import SimpleNamespace

    from traceback_lab.controls import synthetic_summary
    from traceback_lab.runner import StepResult
    from traceback_lab.views import synthetic

    mark = {"floor": 2_000_000, "observed": 900, "kind": "eligible_reads"}
    envelope = _envelope(algorithm_id="copy-number", underpowered=mark,
                         metrics=[{"metric_id": "tumor_fraction", "value": 0.1}])
    view = SimpleNamespace(parameters={"tf": 0.1}, results={"copy-number": envelope})
    rows = synthetic(SimpleNamespace(by_transform=lambda transform: [view] if transform == "cna" else []))
    assert rows[0]["underpowered"] is True and rows[0]["recovered"] is False
    assert "underpowered" in rows[0]["status"]

    run = SimpleNamespace(
        derived=SimpleNamespace(spec=SimpleNamespace(transform="cna", parameters={"tf": 0.1}),
                                input_id="4" * 64, counting=None),
        results=[StepResult("copy-number", "ok", False, "a" * 64, underpowered=mark)],
    )
    store = SimpleNamespace(read_result=lambda *args: envelope)
    summary = synthetic_summary(store, [run])
    assert summary[0]["underpowered"] is True
    assert summary[0]["recovered_within_tolerance"] is False and summary[0]["detected"] is False


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
    assert json.loads(envelope.to_bytes())["underpowered"] == {**mark, "upstream": None}
    assert "underpowered" not in json.loads(_envelope().to_bytes())
    with pytest.raises(ValueError, match="underpowered"):
        _envelope(status="skipped", skip_reason="depth", metrics=[], underpowered=mark)
    rows = interpret_result(envelope, load_library())
    assert rows and all(row.interpretation.state == "not_interpretable"
                        and row.interpretation.reason == "depth"
                        and row.interpretation.caption == UNDERPOWERED_CAPTION for row in rows)
