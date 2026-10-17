"""Arm aggregation and read-side z scores on pinned synthetic cohort values."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab import arm_zscores
from traceback_lab.algorithms import arm_summary
from traceback_lab.arm_zscores import report_arm_zscores
from traceback_lab.cli import main as lab_main
from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.interpretation.engine import classify, load_library
from traceback_lab.problems import LabProblem
from traceback_lab.registry import InputContext, Skip, load_registry
from traceback_lab.store import LabStore


def _source(index: int) -> str:
    return f"{index:064x}"


def test_registered_arm_summary_and_whole_bin_mask_policy() -> None:
    assert "arm-summary" in [algorithm.id for algorithm in load_registry()]
    centromeres = {f"chr{index}": (4, 6) for index in range(1, 23)}
    lengths = {f"chr{index}": 10 for index in range(1, 23)}
    intervals = arm_summary.arm_intervals(centromeres, lengths)
    assert len(intervals) == 44
    assert intervals["chr1p"] == ("chr1", 0, 4)
    assert intervals["chr1q"] == ("chr1", 6, 10)
    bins = [
        {"chr": "chr1", "start": 0, "end": 1, "log2": -1.0, "mask": None},
        {"chr": "chr1", "start": 1, "end": 2, "log2": 0.0, "mask": None},
        {"chr": "chr1", "start": 2, "end": 4, "log2": 1.0, "mask": None},
        {"chr": "chr1", "start": 3, "end": 5, "log2": 500.0, "mask": None},
        {"chr": "chr1", "start": 6, "end": 8, "log2": 2.0, "mask": None},
        {"chr": "chr1", "start": 8, "end": 10, "log2": 500.0, "mask": "low-map"},
    ]
    result = arm_summary.summarize_arms(bins, intervals)
    assert result["chr1p"] == (0.0, 3)
    assert result["chr1q"] == (2.0, 1)
    assert result["chr2p"] == (None, 0)
    bare_intervals = arm_summary.arm_intervals(
        centromeres, {str(index): 10 for index in range(1, 23)},
    )
    assert bare_intervals["chr1p"] == ("1", 0, 4)
    assert arm_summary.summarize_arms(
        [dict(item, chr="1") for item in bins], bare_intervals,
    )["chr1p"] == (0.0, 3)
    with pytest.raises(ValueError, match="duplicate"):
        arm_summary.summarize_arms(bins + bins[:1], intervals)
    with pytest.raises(ValueError, match="invalid log2"):
        arm_summary.summarize_arms([dict(bins[0], log2=float("nan"))], intervals)


def test_arm_summary_preflight_fails_closed_without_selected_pon(tmp_path: Path) -> None:
    ctx = InputContext(
        root=tmp_path, source_id=_source(100), input_id=_source(101),
        reference_id="synthetic", reference_sha256=_source(102), input_dir=tmp_path,
        eligible_reads=2_000_000,
    )
    decision = arm_summary.ARM_SUMMARY.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "no_pon"


def test_arm_summary_run_uses_pinned_centromere_and_selected_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    centromere = tmp_path / "centromere.tsv"
    centromere.write_text("synthetic arm definition")
    cn = tmp_path / "copy-number"
    cn.mkdir()
    (cn / "cn_bins.json").write_text(json.dumps({"bins": [
        {"chr": "chr1", "start": 0, "end": 1, "log2": -1.0, "mask": None},
        {"chr": "chr1", "start": 1, "end": 2, "log2": 0.0, "mask": None},
        {"chr": "chr1", "start": 2, "end": 4, "log2": 1.0, "mask": None},
    ]}))
    digest = hashlib.sha256(centromere.read_bytes()).hexdigest()
    monkeypatch.setattr(arm_summary, "_registered_centromere", lambda root: (centromere, digest))
    monkeypatch.setattr(arm_summary, "centromeres_from_table", lambda path: {
        f"chr{index}": (4, 6) for index in range(1, 23)
    })
    monkeypatch.setattr(arm_summary.ReadSummary, "open", lambda path: SimpleNamespace(
        contigs=[f"chr{index}" for index in range(1, 23)], lengths=[10] * 22,
    ))
    ctx = InputContext(
        root=tmp_path, source_id=_source(100), input_id=_source(101),
        reference_id="synthetic", reference_sha256=_source(102), input_dir=tmp_path,
        eligible_reads=2_000_000,
        extras={"pon_selection": {"pon_sha256": "b" * 64},
                "planned_arm_centromere_sha256": digest,
                "upstream": {"read-summary": str(tmp_path), "copy-number": str(cn)}},
    )
    result = arm_summary.ARM_SUMMARY.run(ctx, tmp_path)
    metrics = {item["metric_id"]: item["value"] for item in result.metrics}
    assert metrics["chr1p_log2"] == 0.0
    assert metrics["chr1q_log2"] is None
    assert metrics["arms_with_value"] == 1
    artifact = json.loads(result.artifacts["arm_summaries.json"])
    assert artifact["arm_asset_sha256"] == digest
    assert artifact["policy"] == arm_summary.POLICY
    assert (tmp_path / "arm-centromere.tsv").read_bytes() == centromere.read_bytes()

    centromere.write_text("changed after planning")
    with pytest.raises(ValueError, match="before worker consumption"):
        arm_summary.ARM_SUMMARY.run(ctx, tmp_path)


def test_unbuilt_cohort_is_a_cli_refusal_not_a_depth_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    store = LabStore(tmp_path)
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{_source(1)}.json").write_text("{}")
    with pytest.raises(LabProblem) as caught:
        report_arm_zscores(store, "unbuilt", _source(1))
    assert caught.value.code == "TBX-LAB-017"
    assert "no published generation" in caught.value.cause

    code = lab_main(["arm-zscores", _source(1)[:12], "--cohort", "unbuilt",
                     "--root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["status"] == "not_found"
    assert payload["data"]["code"] == "TBX-LAB-017"


def _member(index: int, value: float, *, protocol: str = "a" * 64) -> CohortMember:
    return CohortMember(
        source_id=_source(index), input_id=_source(index + 1000),
        aligner_identity="synthetic-matched", pon_asset_sha256=_source(index + 199),
        results=(
            CohortResult(
                algorithm_id="read-summary", parameters_sha256="b" * 64,
                cohort_compat_key="c" * 64, metrics={"eligible_reads": 2_000_000},
            ),
            CohortResult(
                algorithm_id="arm-summary", parameters_sha256="d" * 64,
                cohort_compat_key=protocol, metrics={"chr1p_log2": value},
            ),
        ),
    )


def test_each_arm_value_has_an_exploratory_library_label() -> None:
    library = load_library()
    arm_entries = [entry for (algorithm_id, metric_id), entry in library.entries.items()
                   if algorithm_id == "arm-summary" and metric_id.endswith("_log2")]
    assert len(arm_entries) == 44
    for entry in arm_entries:
        assert entry["tier"] == "SPECULATIVE"
        assert entry.stability is None
        assert entry["headline"] is False
        result = classify(0.01, entry)
        assert result.state == "no_reference"
        assert result.reason == "none_published"


def test_read_side_arm_z_requires_ten_controls_after_self_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    members = tuple(_member(index, float(index)) for index in range(1, 12))
    target = members[0]

    def generation_for(selected: tuple[CohortMember, ...]) -> CohortGeneration:
        return CohortGeneration(
            cohort_id="synthetic", version=1, members=selected, pon_n=len(selected),
            pon_asset_sha256="e" * 64,
            pon_build={
                "source_ids": tuple(sorted(member.source_id for member in selected)),
                "loo_asset_sha256s": tuple((member.source_id, member.pon_asset_sha256)
                                              for member in selected),
                "reference_sha256": "f" * 64, "aligner_identity": "synthetic-matched",
                "grid_sha256": "1" * 64, "counting_parameters_sha256": "2" * 64,
                "toolchain_lock_sha256": "3" * 64, "builder_source_sha256": "4" * 64,
                "builder_sha256": "5" * 64, "validator_sha256": "6" * 64,
                "gc_sha256": "7" * 64, "map_sha256": "8" * 64,
                "centromere_sha256": "9" * 64,
            },
        )

    generation = generation_for(members)
    monkeypatch.setattr(arm_zscores, "read_current", lambda *_: generation)
    monkeypatch.setattr(arm_zscores, "member_for_generation", lambda *_: target)
    monkeypatch.setattr(arm_zscores.ARM_SUMMARY, "asset_sha256s", lambda *_: ["9" * 64])
    report = report_arm_zscores(store, "synthetic", target.source_id)
    assert report.public()["target_panel_sha256"] == target.pon_asset_sha256
    assert report.public()["target_panel_n"] == 10
    assert report.public()["target_excluded"] is True
    score = report.scores["chr1p"]
    assert score.state == "ok" and score.reference_n == 10
    assert score.reference_mean == 6.5
    assert score.z == pytest.approx((1 - 6.5) / math.sqrt(8.25))

    external = target.model_copy(update={"source_id": _source(99), "pon_asset_sha256": None})
    monkeypatch.setattr(arm_zscores, "member_for_generation", lambda *_: external)
    full_report = report_arm_zscores(store, "synthetic", external.source_id)
    assert full_report.public()["target_panel_sha256"] == generation.pon_asset_sha256
    assert full_report.public()["target_panel_n"] == 11
    assert full_report.public()["target_excluded"] is False
    monkeypatch.setattr(arm_zscores, "member_for_generation", lambda *_: target)

    ten_member_generation = generation_for(members[:10])
    assert CohortGeneration.model_validate(ten_member_generation.model_dump()) == ten_member_generation
    monkeypatch.setattr(arm_zscores, "read_current", lambda *_: ten_member_generation)
    score = report_arm_zscores(store, "synthetic", target.source_id).scores["chr1p"]
    assert score.state == "no_reference" and score.reference_n == 9
