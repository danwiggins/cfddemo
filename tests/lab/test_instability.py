"""Instability measurement and exact derived copy-number dependency."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceback_lab import controls
from traceback_lab.algorithms.instability import INSTABILITY, segment_tmad
from traceback_lab.algorithms._base import Base
from traceback_lab.controls import derived_plan_items
from traceback_lab.derived import DerivedInput, cna, downsample, split
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import Cohort, interpret_result, load_library
from traceback_lab.registry import AlgorithmOutput, InputContext, PlanItem, Skip, execution_identity, intermediate_ids, load_registry, plan
from traceback_lab.run_command import _parent_intermediates
from traceback_lab.runner import RunTarget, StepResult, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id


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
    result = segment_tmad([_segment(0, 4), _segment(1, 1), _segment(2, 3)])
    assert (result.value, result.bins_used, result.segments_used) == (0.5, 8, 3)
    assert (result.bins_missing_estimate, result.segments_missing_estimate) == (0, 0)
    missing = segment_tmad([_segment(None, 8), _segment(0.2, 5)])
    assert (missing.value, missing.bins_used, missing.segments_used) == (0.0, 5, 1)
    assert (missing.bins_missing_estimate, missing.segments_missing_estimate) == (8, 1)
    assert segment_tmad([]).value is None


def test_tmad_uses_only_a_compatible_internal_cohort_reference() -> None:
    envelope = ResultEnvelope(
        algorithm_id="instability", algorithm_version="1", status="ok",
        provenance=provenance(
            tool="fixture", parameters_sha256="a" * 64, source_id="b" * 64,
            input_id="c" * 64, reference_id="hg38",
        ),
        metrics=[{"metric_id": "tmad", "value": 0.2, "unit": "log2", "n": 10}],
    )
    library = load_library()
    missing = interpret_result(envelope, library)[0].interpretation
    assert (missing.state, missing.reason, missing.reference_kind) == (
        "no_reference", "cohort_too_small", "internal_cohort",
    )
    cohort = Cohort(values=[0.1 + index * 0.02 for index in range(10)], version="healthy-v1:1")
    supplied = interpret_result(envelope, library, cohorts={"tmad": cohort})[0].interpretation
    assert (supplied.state, supplied.reference_kind, supplied.exploratory_n) == (
        "in_range", "internal_cohort", 10,
    )


@pytest.mark.parametrize("segment", [
    _segment(1, 0), _segment(1, -1), _segment(1, 1.5), _segment(1, "2"),
    _segment(math.nan, 2), _segment(math.inf, 2), _segment("0.2", 2),
])
def test_malformed_segment_is_not_silently_excluded(segment) -> None:
    with pytest.raises(ValueError, match="invalid"):
        segment_tmad([segment])


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
    assert output.metrics == [
        {"metric_id": "tmad", "value": 1.0, "unit": "log2", "n": 4},
        {"metric_id": "segments_used", "value": 2, "unit": "segments", "n": None},
        {"metric_id": "segments_missing_estimate", "value": 0, "unit": "segments", "n": None},
        {"metric_id": "bins_missing_estimate", "value": 0, "unit": "bins", "n": None},
    ]
    with pytest.raises(RuntimeError, match="selected copy-number"):
        INSTABILITY.run(InputContext(**{**ctx.__dict__, "extras": {}}), tmp_path)
    (selected / "cn_segments.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no segment list"):
        INSTABILITY.run(ctx, tmp_path)
    assert INSTABILITY.preflight(InputContext(**{**ctx.__dict__, "eligible_reads": 1_999_999})).reason_code == "depth"


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
        # The parameter hash may legitimately match the parent's; input ID
        # separates the store entries and their selected artifact directories.
        assert run.results[0].parameters_sha256 == full[0].parameters_sha256
        assert run.results[1].parameters_sha256 == full[1].parameters_sha256
        assert run.results[0].artifacts_dir != full[0].artifacts_dir


def test_algorithm_run_records_resource_measurements(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "store")
    store.ensure()
    log = store.run_log("r-20261016-1200-aaab")
    target = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64, eligible_reads=2_000_000,
    )
    result = run_plan(
        store, None, log, target, [PlanItem(FAKE_CN, False)],
        {"copy-number": "tests.lab.test_instability:FAKE_CN"},
        run_id=log.run_id, scrubber=Scrubber(root=store.root),
    )[0]
    assert result.status == "ok"
    assert result.peak_rss_bytes is not None and result.peak_rss_bytes > 0
    assert result.peak_tree_rss_bytes is None or result.peak_tree_rss_bytes > 0
    assert result.peak_disk_bytes is not None and result.peak_disk_bytes > 0
    assert result.public()["peak_disk_bytes"] == result.peak_disk_bytes
    step = next(event for event in log.events() if event["event"] == "step_end")
    assert step["peak_rss_bytes"] == result.peak_rss_bytes
    assert step["peak_tree_rss_bytes"] == result.peak_tree_rss_bytes
    assert step["peak_disk_bytes"] == result.peak_disk_bytes

    cached_run_id = new_run_id()
    cached_log = store.run_log(cached_run_id)
    cached = run_plan(
        store, None, cached_log, target, [PlanItem(FAKE_CN, False)],
        {"copy-number": "tests.lab.test_instability:FAKE_CN"},
        run_id=cached_run_id, scrubber=Scrubber(root=store.root),
    )[0]
    assert cached.status == "ok" and cached.cached
    assert cached.parameters_sha256 == result.parameters_sha256
    assert cached.attempt_id == result.attempt_id
    assert (cached.peak_rss_bytes, cached.peak_tree_rss_bytes, cached.peak_disk_bytes) == (None, None, None)
    assert cached.public()["cached"] is True


def test_disk_sampling_only_walks_the_current_step_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_lab import pipeline

    store = LabStore(tmp_path / "store")
    store.ensure()
    stale = store.lab / "tmp" / "microbial-stale"
    stale.mkdir(parents=True)
    (stale / "leftover").write_bytes(b"x" * 2_000_000)
    log = store.run_log(new_run_id())
    expected = store.lab / "tmp" / log.run_id / "copy-number-full"
    walked: list[Path] = []
    directory_bytes = pipeline.directory_bytes

    def record_walk(path: Path) -> int:
        walked.append(path)
        return directory_bytes(path)

    monkeypatch.setattr(pipeline, "directory_bytes", record_walk)
    target = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64, eligible_reads=2_000_000,
    )
    result = run_plan(
        store, None, log, target, [PlanItem(FAKE_CN, False)],
        {"copy-number": "tests.lab.test_instability:FAKE_CN"},
        run_id=log.run_id, scrubber=Scrubber(root=store.root),
    )[0]
    assert result.status == "ok" and result.peak_disk_bytes is not None
    assert walked and all(path == expected for path in walked)


def test_non_ok_copy_number_propagates_as_upstream_skip(tmp_path: Path) -> None:
    @dataclass
    class SkippingCopyNumber(FakeCopyNumber):
        def preflight(self, ctx: InputContext) -> Skip:
            return Skip("missing_asset", "synthetic copy-number unavailable")

    store = LabStore(tmp_path / "store")
    store.ensure()
    target = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64, eligible_reads=2_000_000,
    )
    results = run_plan(
        store, None, store.run_log("r-20261016-1201-aaab"), target,
        [PlanItem(SkippingCopyNumber("copy-number"), True), PlanItem(INSTABILITY, False)],
        {"copy-number": "tests.lab.test_instability:FAKE_CN", "instability": "instability:INSTABILITY"},
        run_id="r-20261016-1201-aaab", scrubber=Scrubber(root=store.root),
    )
    assert [(result.status, result.skip_reason) for result in results] == [
        ("skipped", "missing_asset"), ("skipped", "upstream"),
    ]
