"""Read-side paired-signal projection with PoN and cohort gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from traceback_lab.cohort_contracts import CohortGeneration, CohortMember, CohortResult
from traceback_lab.tf_concordance import CAPTION, report_tf_concordance


def _envelope(algorithm_id: str, metrics: dict[str, object]):
    return SimpleNamespace(
        algorithm_id=algorithm_id, status="ok",
        provenance=SimpleNamespace(parameters_sha256="b" * 64),
        metrics=[SimpleNamespace(metric_id=name, value=value) for name, value in metrics.items()],
    )


def _member(index: int) -> CohortMember:
    return CohortMember(
        source_id=f"{index:064x}", input_id=f"{index + 100:064x}",
        aligner_identity="synthetic-matched",
        results=(
            CohortResult(
                algorithm_id="fragment-size", parameters_sha256="b" * 64,
                cohort_compat_key="c" * 64, metrics={"mode_bp": 120 if index == 1 else 99 + index},
            ),
            CohortResult(
                algorithm_id="mtdna", parameters_sha256="b" * 64,
                cohort_compat_key="d" * 64, metrics={"chrm_fraction": 0.02 if index == 1 else 0.01},
            ),
            CohortResult(
                algorithm_id="copy-number", parameters_sha256="b" * 64,
                cohort_compat_key="e" * 64, metrics={"tumor_fraction": 0.2},
            ),
        ),
    )


def _sample(target: CohortMember, *, pon_policy: str = "lab_loo_v1"):
    return SimpleNamespace(full=SimpleNamespace(
        input_id=target.input_id,
        results={
            "copy-number": _envelope("copy-number", {
                "pon_policy": pon_policy, "identifiable": True, "tumor_fraction": 0.2,
            }),
            "meth-tf": _envelope("meth-tf", {
                "identifiable": True, "tumor_fraction": 0.25,
                "markers_covered": 2, "cpg_calls_used": 16,
                "reference_condition_number": 2.0, "fit_rms": None,
            }),
            "fragment-size": _envelope("fragment-size", {"mode_bp": 120}),
            "mtdna": _envelope("mtdna", {"chrm_fraction": 0.02}),
        },
    ))


def test_paired_signals_use_ten_source_excluded_compatible_members() -> None:
    members = tuple(_member(index) for index in range(1, 12))
    generation = CohortGeneration(cohort_id="synthetic", version=1, members=members)
    report = report_tf_concordance(_sample(members[0]), generation, members[0])
    assert report["caption"] == CAPTION
    assert "whole-genome doubling" in report["wgd_caveat"]
    assert "score" not in report
    pairs = {pair["id"]: pair for pair in report["pairs"]}
    assert pairs["cn-meth"]["state"] == "ok"
    assert pairs["cn-meth"]["unavailable_reasons"] == []
    assert pairs["cn-meth"]["x_label"] == "tumour share of fitted methylation reference weights"
    assert (pairs["cn-meth"]["x"], pairs["cn-meth"]["y"]) == (0.25, 0.2)
    assert pairs["cn-meth"]["support"] == {
        "markers_covered": 2, "cpg_calls_used": 16,
        "reference_condition_number": 2.0, "fit_rms": None,
    }
    assert pairs["cn-size"]["state"] == "ok"
    assert pairs["cn-size"]["reference_n"] == 10
    assert pairs["cn-size"]["baseline_median"] == 105.5
    assert pairs["cn-size"]["x"] == pytest.approx(14.5)
    assert pairs["cn-mtdna"]["x"] == pytest.approx(0.01)


def test_no_pon_and_nine_controls_refuse_unqualified_pairs() -> None:
    members = tuple(_member(index) for index in range(1, 11))
    generation = CohortGeneration(cohort_id="synthetic", version=1, members=members)
    no_pon = report_tf_concordance(_sample(members[0], pon_policy="none"), generation, members[0])
    assert all(pair["state"] == "no_pon" and pair["x"] is None for pair in no_pon["pairs"])
    assert no_pon["pairs"][1]["unavailable_reasons"] == ["no_pon", "no_size_baseline"]
    qualified = report_tf_concordance(_sample(members[0]), generation, members[0])
    assert qualified["pairs"][0]["state"] == "ok"
    assert qualified["pairs"][1]["state"] == "no_size_baseline"
    assert qualified["pairs"][1]["reference_n"] == 9
    assert qualified["pairs"][2]["state"] == "no_mtdna_baseline"


@pytest.mark.parametrize("policy", ["none", "pending", "unverified", "", "lab_full_v1"])
def test_unrecognized_pon_policy_never_claims_qualified_copy_number(policy: str) -> None:
    report = report_tf_concordance(_sample(_member(1), pon_policy=policy))
    assert all(pair["state"] == "no_pon" and pair["y"] is None for pair in report["pairs"])


def test_missing_methylation_and_stale_target_are_named_unavailable() -> None:
    members = tuple(_member(index) for index in range(1, 12))
    generation = CohortGeneration(cohort_id="synthetic", version=1, members=members)
    sample = _sample(members[0])
    sample.full.results["meth-tf"] = _envelope("meth-tf", {
        "identifiable": False, "tumor_fraction": None,
    })
    report = report_tf_concordance(sample, generation, members[0])
    assert report["pairs"][0]["state"] == "unidentifiable_meth_tf"
    stale = _member(99)
    report = report_tf_concordance(sample, generation, stale)
    assert report["pairs"][0]["state"] == "stale_cn_binding"
    assert report["pairs"][1]["unavailable_reasons"] == ["stale_cn_binding", "no_size_baseline"]
    assert report["pairs"][2]["unavailable_reasons"] == ["stale_cn_binding", "no_mtdna_baseline"]


def test_stale_copy_number_panel_is_not_paired_with_new_generation_baselines() -> None:
    members = tuple(_member(index) for index in range(1, 12))
    target = members[0].model_copy(update={"results": tuple(
        result.model_copy(update={"parameters_sha256": "f" * 64})
        if result.algorithm_id == "copy-number" else result
        for result in members[0].results
    )})
    generation = CohortGeneration(cohort_id="synthetic", version=2, members=(target, *members[1:]))
    report = report_tf_concordance(_sample(target), generation, target)

    pairs = {pair["id"]: pair for pair in report["pairs"]}
    assert all(pair["state"] == "stale_cn_binding" and pair["x"] is None and pair["y"] is None
               for pair in pairs.values())
    assert pairs["cn-size"]["baseline_median"] == 105.5
    assert pairs["cn-size"]["unavailable_reasons"] == ["stale_cn_binding"]


def test_selected_generation_without_a_target_cannot_qualify_copy_number() -> None:
    generation = CohortGeneration(cohort_id="synthetic", version=2, members=(_member(1),))
    report = report_tf_concordance(_sample(_member(1)), generation, None)
    assert all(pair["state"] == "cn_binding_unavailable" and pair["y"] is None
               for pair in report["pairs"])


def test_pon_qualified_copy_number_can_pair_with_methylation_without_a_cohort() -> None:
    report = report_tf_concordance(_sample(_member(1)))
    meth_pair = next(pair for pair in report["pairs"] if pair["id"] == "cn-meth")
    assert (meth_pair["state"], meth_pair["x"], meth_pair["y"]) == ("ok", 0.25, 0.2)


def test_same_input_parameter_drift_refuses_only_its_baseline() -> None:
    members = tuple(_member(index) for index in range(1, 12))
    generation = CohortGeneration(cohort_id="synthetic", version=1, members=members)
    sample = _sample(members[0])
    sample.full.results["fragment-size"].provenance.parameters_sha256 = "e" * 64
    report = report_tf_concordance(sample, generation, members[0])
    pairs = {pair["id"]: pair for pair in report["pairs"]}
    assert pairs["cn-size"]["state"] == "no_size_baseline"
    assert pairs["cn-size"]["unavailable_reasons"] == ["no_size_baseline"]
    assert pairs["cn-size"]["reference_n"] == 0
    assert pairs["cn-size"]["baseline_median"] is None
    assert pairs["cn-size"]["x"] is None and pairs["cn-size"]["y"] is None
    assert pairs["cn-mtdna"]["state"] == "ok" and pairs["cn-mtdna"]["reference_n"] == 10


def test_both_axes_report_their_unavailable_reasons() -> None:
    target = _member(1)
    sample = _sample(target)
    del sample.full.results["copy-number"]
    del sample.full.results["meth-tf"]
    del sample.full.results["fragment-size"]
    report = report_tf_concordance(sample)
    assert report["pairs"][0]["state"] == "missing_cn"
    assert report["pairs"][0]["unavailable_reasons"] == ["missing_cn", "missing_meth_tf"]
    assert report["pairs"][1]["unavailable_reasons"] == [
        "missing_cn", "missing_size", "no_size_baseline",
    ]
