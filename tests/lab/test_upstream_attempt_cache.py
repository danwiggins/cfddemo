"""D19 identity stays fixed while selected upstream attempts can change."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceback_lab.algorithms._base import Base
from traceback_lab.algorithms.instability import INSTABILITY
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.registry import AlgorithmOutput, InputContext, PlanItem, Ready, Skip
from traceback_lab.runner import RunTarget, StepResult, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, MAX_UPSTREAM_SELECTIONS_BYTES, new_run_id
from traceback_runner.serialization import canonical_json_bytes


@dataclass
class SyntheticCN(Base):
    upstream: tuple[str, ...] = ()
    shifted: bool = False
    fail: bool = False
    skip: bool = False

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        return Skip("depth", "synthetic local CN skip") if self.skip else Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        if self.fail:
            raise RuntimeError("synthetic CN failure")
        values = [-1.0, 1.0] if self.shifted else [0.0, 0.0]
        segments = [{"median_log2": value, "n_bins": 2} for value in values]
        return AlgorithmOutput(artifacts={"cn_segments.json": json.dumps({"segments": segments}).encode()})


FLAT_CN = SyntheticCN("copy-number", family="copy-number")
SHIFTED_CN = SyntheticCN("copy-number", family="copy-number", shifted=True)
FAILED_CN = SyntheticCN("copy-number", family="copy-number", fail=True)
SKIPPED_CN = SyntheticCN("copy-number", family="copy-number", skip=True)
CHANGED_PARAMS_CN = SyntheticCN("copy-number", family="copy-number", parameters={"variant": 2})


@dataclass
class SyntheticSummary(Base):
    upstream: tuple[str, ...] = ()
    value: int = 1

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        return AlgorithmOutput(
            metrics=[{"metric_id": "eligible_reads", "value": 2_000_000},
                     {"metric_id": "approx_depth_x", "value": 1.0}],
            artifacts={"value.txt": str(self.value).encode()},
        )


@dataclass
class SyntheticDependent(Base):
    upstream: tuple[str, ...] = ("read-summary",)

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        source = Path(ctx.extras["upstream"]["read-summary"]) / "value.txt"
        return AlgorithmOutput(metrics=[{"metric_id": "value", "value": int(source.read_text())}])


SUMMARY_ONE = SyntheticSummary("read-summary")
SUMMARY_TWO = SyntheticSummary("read-summary", value=2)
DEPENDENT = SyntheticDependent("synthetic-dependent")


def _target(store: LabStore, input_id: str, *, derived: bool = False) -> RunTarget:
    return RunTarget(
        input_id=input_id, source_id="b" * 64, input_dir=store.root,
        reference_id="hg38", reference_sha256="c" * 64, eligible_reads=2_000_000,
        derived={"transform": "downsample"} if derived else None,
    )


def _run(
    store: LabStore, target: RunTarget, items: list[PlanItem], entries: dict[str, str],
    *, force: bool = False, upstream_results: dict[str, StepResult] | None = None,
) -> list[StepResult]:
    run_id = new_run_id()
    return run_plan(
        store, None, store.run_log(run_id), target, items, entries,
        run_id=run_id, force=force, scrubber=Scrubber(root=store.root),
        upstream_results=upstream_results,
    )


def _tmad(store: LabStore, target: RunTarget, result: StepResult) -> float | None:
    envelope = store.read_result(target.input_id, "instability", result.parameters_sha256)
    assert envelope is not None
    return next(metric.value for metric in envelope.metrics if metric.metric_id == "tmad")


def test_upstream_attempt_metadata_write_matches_read_limit(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    input_id = "a" * 64
    parameters_sha256 = "b" * 64
    envelope = ResultEnvelope.model_validate({
        "algorithm_id": "synthetic-dependent", "algorithm_version": "1", "status": "ok",
        "provenance": provenance(
            tool="traceback_lab.test", parameters_sha256=parameters_sha256,
            source_id="c" * 64, input_id=input_id, reference_id="synthetic",
        ),
        "metrics": [],
    })

    def selections(width: int) -> list[dict[str, str]]:
        return [{
            "algorithm_id": "x" * (width - 2) + f"{index:02d}",
            "input_id": input_id, "parameters_sha256": parameters_sha256,
            "attempt_id": "a-20261016T010203040506-12345678",
        } for index in range(64)]

    at_limit = selections(22)
    encoded = canonical_json_bytes({
        "schema": "traceback.lab.upstream-selections.v1", "selections": at_limit,
    })
    assert len(encoded) == MAX_UPSTREAM_SELECTIONS_BYTES
    store.publish_result(envelope, input_id=input_id, upstream_selections=at_limit)
    assert store.selected_upstream_selections(input_id, envelope.algorithm_id, parameters_sha256) == tuple(at_limit)

    over_limit = selections(23)
    with pytest.raises(ValueError, match="readable metadata limit"):
        store.publish_result(envelope, input_id=input_id, upstream_selections=over_limit)
    assert store.selected_upstream_selections(input_id, envelope.algorithm_id, parameters_sha256) == tuple(at_limit)


def test_forced_same_identity_cn_attempt_invalidates_dependent_cache(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    target = _target(store, "a" * 64)
    entries = {"copy-number": "tests.lab.test_upstream_attempt_cache:FLAT_CN",
               "instability": "instability:INSTABILITY"}
    first = _run(store, target, [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert [result.status for result in first] == ["ok", "ok"]
    assert _tmad(store, target, first[1]) == 0.0
    unchanged = _run(store, target, [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert [result.cached for result in unchanged] == [True, True]

    entries["copy-number"] = "tests.lab.test_upstream_attempt_cache:SHIFTED_CN"
    forced = _run(store, target, [PlanItem(SHIFTED_CN, True)], entries, force=True)
    assert forced[0].status == "ok"
    assert forced[0].parameters_sha256 == first[0].parameters_sha256
    assert forced[0].attempt_id != first[0].attempt_id
    changed = _run(store, target, [PlanItem(SHIFTED_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert changed[0].cached and not changed[1].cached
    assert changed[1].parameters_sha256 == first[1].parameters_sha256
    assert _tmad(store, target, changed[1]) == 1.0
    assert store.selected_upstream_selections(target.input_id, "instability", changed[1].parameters_sha256) == ({
        "algorithm_id": "copy-number", "input_id": target.input_id,
        "parameters_sha256": forced[0].parameters_sha256, "attempt_id": forced[0].attempt_id,
    },)


def test_legacy_dependent_without_attempt_record_recomputes(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    target = _target(store, "a" * 64)
    items = [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)]
    entries = {"copy-number": "tests.lab.test_upstream_attempt_cache:FLAT_CN",
               "instability": "instability:INSTABILITY"}
    first = _run(store, target, items, entries)
    attempt = store.selected_attempt(target.input_id, "instability", first[1].parameters_sha256)
    assert attempt is not None
    (attempt / "upstream_selections.json").unlink()
    second = _run(store, target, items, entries)
    assert second[0].cached and not second[1].cached
    assert second[1].attempt_id != first[1].attempt_id


def test_changed_upstream_identity_still_rekeys_dependent(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    target = _target(store, "a" * 64)
    entries = {"copy-number": "tests.lab.test_upstream_attempt_cache:FLAT_CN",
               "instability": "instability:INSTABILITY"}
    first = _run(store, target, [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)], entries)
    entries["copy-number"] = "tests.lab.test_upstream_attempt_cache:CHANGED_PARAMS_CN"
    second = _run(store, target, [PlanItem(CHANGED_PARAMS_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert first[0].parameters_sha256 != second[0].parameters_sha256
    assert first[1].parameters_sha256 != second[1].parameters_sha256
    assert not second[1].cached


def test_failed_current_upstream_does_not_reuse_old_dependent_success(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    target = _target(store, "a" * 64)
    entries = {"copy-number": "tests.lab.test_upstream_attempt_cache:FLAT_CN",
               "instability": "instability:INSTABILITY"}
    first = _run(store, target, [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert first[1].status == "ok"
    entries["copy-number"] = "tests.lab.test_upstream_attempt_cache:FAILED_CN"
    _run(store, target, [PlanItem(FAILED_CN, True)], entries, force=True)
    second = _run(store, target, [PlanItem(FAILED_CN, True), PlanItem(INSTABILITY, False)], entries)
    assert second[0].status == "failed"
    assert second[1].status == "skipped" and not second[1].cached


def test_derived_control_records_parent_attempt_alias(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    parent = _target(store, "a" * 64)
    derived = _target(store, "d" * 64, derived=True)
    entries = {"read-summary": "tests.lab.test_upstream_attempt_cache:SUMMARY_ONE",
               "synthetic-dependent": "tests.lab.test_upstream_attempt_cache:DEPENDENT"}
    summary = _run(store, parent, [PlanItem(SUMMARY_ONE, True)], entries)[0]
    first = _run(store, derived, [PlanItem(DEPENDENT, False)], entries,
                 upstream_results={"read-summary": summary})[0]
    assert first.status == "ok"
    assert store.selected_upstream_selections(derived.input_id, "synthetic-dependent", first.parameters_sha256) == ({
        "algorithm_id": "read-summary", "input_id": parent.input_id,
        "parameters_sha256": summary.parameters_sha256, "attempt_id": summary.attempt_id,
    },)
    again = _run(store, derived, [PlanItem(DEPENDENT, False)], entries,
                 upstream_results={"read-summary": summary})[0]
    assert again.cached
    entries["read-summary"] = "tests.lab.test_upstream_attempt_cache:SUMMARY_TWO"
    newer = _run(store, parent, [PlanItem(SUMMARY_TWO, True)], entries, force=True)[0]
    assert newer.parameters_sha256 == summary.parameters_sha256
    changed = _run(store, derived, [PlanItem(DEPENDENT, False)], entries,
                   upstream_results={"read-summary": newer})[0]
    assert not changed.cached
    envelope = store.read_result(derived.input_id, "synthetic-dependent", changed.parameters_sha256)
    assert envelope is not None and envelope.metrics[0].value == 2


def test_local_skipped_cn_overrides_seeded_parent_and_old_dependent_cache(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    parent = _target(store, "a" * 64)
    child = _target(store, "d" * 64, derived=True)
    entries = {"copy-number": "tests.lab.test_upstream_attempt_cache:FLAT_CN",
               "instability": "instability:INSTABILITY"}
    parent_cn = _run(store, parent, [PlanItem(FLAT_CN, True)], entries)[0]
    items = [PlanItem(FLAT_CN, True), PlanItem(INSTABILITY, False)]
    first = _run(store, child, items, entries, upstream_results={"copy-number": parent_cn})
    assert [result.status for result in first] == ["ok", "ok"]
    entries["copy-number"] = "tests.lab.test_upstream_attempt_cache:SKIPPED_CN"
    _run(store, child, [PlanItem(SKIPPED_CN, True)], entries, force=True,
         upstream_results={"copy-number": parent_cn})
    second = _run(store, child, [PlanItem(SKIPPED_CN, True), PlanItem(INSTABILITY, False)], entries,
                  upstream_results={"copy-number": parent_cn})
    assert [result.status for result in second] == ["skipped", "skipped"]
    assert not second[1].cached
