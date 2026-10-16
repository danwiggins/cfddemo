"""Instability measurement and exact derived copy-number dependency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceback_lab.algorithms.instability import INSTABILITY, segment_tmad
from traceback_lab.controls import derived_plan_items
from traceback_lab.registry import InputContext, execution_identity, intermediate_ids, load_registry, plan
from traceback_lab.run_command import _parent_intermediates
from traceback_lab.runner import StepResult


def _segment(value: float | None, bins: int) -> dict[str, float | int | None]:
    return {"median_log2": value, "n_bins": bins}


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
