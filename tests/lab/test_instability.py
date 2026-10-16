"""Instability measurement and exact derived copy-number dependency."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceback_lab import controls
from traceback_lab.algorithms.instability import INSTABILITY, segment_tmad
from traceback_lab.algorithms._base import Base
from traceback_lab.controls import derived_plan_items
from traceback_lab.derived import DerivedInput, cna, downsample, split
from traceback_lab.registry import AlgorithmOutput, InputContext, PlanItem, execution_identity, intermediate_ids, load_registry, plan
from traceback_lab.run_command import _parent_intermediates
from traceback_lab.runner import RunTarget, StepResult, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore


def _segment(value: float | None, bins: int) -> dict[str, float | int | None]:
    return {"median_log2": value, "n_bins": bins}


@dataclass
class FakeCopyNumber(Base):
    consumes: str = "counting_bam"
    upstream: tuple[str, ...] = ()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        # The parent is flat; every derived input differs. The real worker and
        # store must hand instability the result selected for that input.
        values = [0.0, 0.0] if ctx.derived is None else [-1.0, 1.0]
        return AlgorithmOutput(artifacts={
            "cn_segments.json": json.dumps({"segments": [_segment(value, 2) for value in values]}).encode()
        })


FAKE_CN = FakeCopyNumber("copy-number")


def test_tmad_weights_each_segment_by_supporting_bins() -> None:
    # Expanded bins are [0,0,0,0,1,2,2,2]. Their median is 0.5 and MAD 0.5.
    value, n = segment_tmad([_segment(0, 4), _segment(1, 1), _segment(2, 3)])
    assert (value, n) == (0.5, 8)
    assert segment_tmad([_segment(None, 8), _segment(1, 0)]) == (None, 0)
    assert segment_tmad([_segment(0.2, 5)]) == (0.0, 5)


def test_instability_reads_only_the_selected_copy_number_artifact(tmp_path: Path) -> None:
    selected = tmp_path / "derived" / "artifacts"
    selected.mkdir(parents=True)
    (selected / "cn_segments.json").write_text(
        json.dumps({"segments": [_segment(-1, 2), _segment(1, 2)]}), encoding="utf-8"
    )
    ctx = InputContext(
        root=tmp_path, source_id="a" * 64, input_id="b" * 64,
        reference_id="hg38", reference_sha256="c" * 64,
        input_dir=tmp_path, eligible_reads=2_000_000,
        extras={"upstream": {"copy-number": str(selected)}},
    )
    output = INSTABILITY.run(ctx, tmp_path)
    assert output.metrics == [{"metric_id": "tmad", "value": 1.0, "unit": "log2", "n": 4}]
    with pytest.raises(RuntimeError, match="selected copy-number"):
        INSTABILITY.run(InputContext(**{**ctx.__dict__, "extras": {}}), tmp_path)


def test_full_and_derived_plans_keep_copy_number_per_input() -> None:
    algorithms = load_registry()
    items = plan(algorithms, only=["instability"])
    assert [item.algorithm.id for item in items] == ["read-summary", "copy-number", "instability"]
    assert intermediate_ids([item.algorithm for item in items]) == frozenset({"read-summary"})
    for transform in ("downsample", "split", "cna"):
        assert [item.algorithm.id for item in derived_plan_items(items, transform)] == ["copy-number", "instability"]

    # A full-input copy-number result must never seed a derived plan.
    full = StepResult("copy-number", "ok", True, "1" * 64, input_label="full")
    assert _parent_intermediates(items, [full]) == {}
    summary = StepResult("read-summary", "ok", True, "2" * 64, input_label="full")
    assert _parent_intermediates(items, [summary, full]) == {"read-summary": summary}
    selected = {"read-summary", "meth-calls"}
    assert intermediate_ids(algorithms) == selected


def test_upstream_copy_number_identity_is_exact() -> None:
    reference = "c" * 64
    full = execution_identity(
        INSTABILITY, reference_sha256=reference,
        upstream=[{"algorithm_id": "copy-number", "parameters_sha256": "a" * 64}],
    )
    derived = execution_identity(
        INSTABILITY, reference_sha256=reference,
        upstream=[{"algorithm_id": "copy-number", "parameters_sha256": "b" * 64}],
    )
    assert full != derived


def test_derived_runs_use_their_own_copy_number_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LabStore(tmp_path / "store")
    store.ensure()
    log = store.run_log("r-20261016-1200-aaaa")
    parent = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64,
        eligible_reads=2_000_000,
    )
    items = [PlanItem(FAKE_CN, True), PlanItem(INSTABILITY, False)]
    entries = {"copy-number": "tests.lab.test_instability:FAKE_CN", "instability": "instability:INSTABILITY"}
    scrubber = Scrubber(root=store.root)
    full = run_plan(store, None, log, parent, items, entries, run_id=log.run_id, scrubber=scrubber)
    assert [result.status for result in full] == ["ok", "ok"]
    full_value = store.read_result(parent.input_id, "instability", full[1].parameters_sha256)
    assert full_value is not None and full_value.metrics[0].value == 0.0

    class Summary:
        pass

    monkeypatch.setattr(controls.ReadSummary, "open", lambda *_: Summary())
    monkeypatch.setattr(controls, "derived_mask", lambda *_: None)
    monkeypatch.setattr(controls, "depth_stamp", lambda *_: {
        "eligible_reads": 2_000_000, "mean_span_bp": 150.0, "approx_depth_x": 0.1,
    })
    monkeypatch.setattr(controls, "record_derived", lambda *_: None)

    def fake_materialize(store: LabStore, parent_id: str, parent_bam: Path, specs, **kwargs):
        made = {}
        for spec in specs:
            directory = store.inputs_dir / spec.input_id(parent_id)
            counting = directory / "counting"
            counting.mkdir(parents=True)
            (counting / "counting.json").write_text(
                json.dumps({"counted_reads": 2_000_000, "records_scanned": 2_000_000}), encoding="utf-8"
            )
            made[spec.label()] = DerivedInput(spec, spec.input_id(parent_id), parent_id, directory)
        return made

    monkeypatch.setattr(controls, "materialize", fake_materialize)
    specs = [downsample(0.5), split("A"), cna(0.1, [])]
    summary = StepResult("read-summary", "ok", True, "d" * 64, artifacts_dir=str(tmp_path))
    derived = controls.run_derived(
        store, None, log, parent, specs, items, entries, run_id=log.run_id,
        read_summary=summary, timeout_s=30, scrubber=scrubber,
        parent_upstream={"read-summary": summary, "copy-number": full[0]},
    )
    assert [run.derived.spec.transform for run in derived] == ["downsample", "split", "cna"]
    for run in derived:
        assert [result.status for result in run.results] == ["ok", "ok"]
        result = store.read_result(run.derived.input_id, "instability", run.results[1].parameters_sha256)
        assert result is not None and result.metrics[0].value == 1.0
        assert run.results[0].artifacts_dir != full[0].artifacts_dir
