"""Interpretation library v0: loader rules and the D7/D8 state machine."""

from __future__ import annotations

import json
import re

import numpy as np
import pytest

from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.interpretation.engine import (
    LIBRARY_FILE,
    LICENSES_FILE,
    Cohort,
    LibraryError,
    bootstrap_p95_interval,
    classify,
    findings_sort_key,
    interpret_result,
    license_callouts,
    load_library,
    parse_library,
    percentile_of,
    qc_band,
)
from traceback_lab.registry import load_registry

LIB = load_library()
LICENSES = LICENSES_FILE.read_text()


def _entry(**reference):
    text = LIBRARY_FILE.read_text()
    library = parse_library(text, LICENSES)
    base = dict(library.entry("bam-qc", "mapped_fraction").raw)
    base["reference"] = reference
    base["min_depth_x"] = 0.5
    from traceback_lab.interpretation.engine import Entry

    return Entry("bam-qc", "mapped_fraction", base)


RANGE = _entry(kind="published_range", lo=0.10, hi=0.20, unit="", source="S", n=48)
COHORT = _entry(kind="internal_cohort")
PERCENTILE_COHORT = Cohort(values=list(np.arange(101.0)))  # percentile_of(v) == v

CASES = [
    # (value, entry, cohort, depth_x, expected state, reason, exploratory_n)
    (0.15, RANGE, None, 1.0, "in_range", None, None),
    (0.10, RANGE, None, 1.0, "in_range", None, None),
    (0.20, RANGE, None, 1.0, "in_range", None, None),
    (0.2001, RANGE, None, 1.0, "above_typical", None, None),
    (0.0999, RANGE, None, 1.0, "below_typical", None, None),
    (5.0, RANGE, None, 1.0, "above_typical", None, None),  # never "markedly" on a published range
    (0.15, RANGE, None, 0.1, "not_interpretable", "depth", None),
    (None, RANGE, None, 1.0, "not_interpretable", "depth", None),
    (0.15, None, None, 1.0, "no_reference", "no_library_entry", None),
    (0.15, COHORT, None, 1.0, "no_reference", "cohort_too_small", None),
    (0.15, COHORT, Cohort(values=[0.1] * 9), 1.0, "no_reference", "cohort_too_small", None),
    (0.15, COHORT, Cohort(values=list(np.linspace(0.1, 0.2, 10))), 1.0, "in_range", None, 10),
    (0.25, COHORT, Cohort(values=list(np.linspace(0.1, 0.2, 10))), 1.0, "above_typical", None, 10),
    (0.05, COHORT, Cohort(values=list(np.linspace(0.1, 0.2, 29))), 1.0, "below_typical", None, 29),
    (99.9, COHORT, Cohort(values=list(np.linspace(0.1, 0.2, 29))), 1.0, "above_typical", None, 29),
    (50.0, COHORT, Cohort(values=list(np.arange(30.0))), 1.0, "markedly_above", None, None),
    (0.5, COHORT, PERCENTILE_COHORT, 1.0, "markedly_below", None, None),
    (1.0, COHORT, PERCENTILE_COHORT, 1.0, "below_typical", None, None),
    (4.99, COHORT, PERCENTILE_COHORT, 1.0, "below_typical", None, None),
    (5.0, COHORT, PERCENTILE_COHORT, 1.0, "in_range", None, None),
    (50.0, COHORT, PERCENTILE_COHORT, 1.0, "in_range", None, None),
    (95.0, COHORT, PERCENTILE_COHORT, 1.0, "in_range", None, None),
    (95.01, COHORT, PERCENTILE_COHORT, 1.0, "above_typical", None, None),
    (99.0, COHORT, PERCENTILE_COHORT, 1.0, "above_typical", None, None),
    (99.01, COHORT, PERCENTILE_COHORT, 1.0, "markedly_above", None, None),
]


@pytest.mark.parametrize("value,entry,cohort,depth,state,reason,exploratory", CASES)
def test_d8_state_table(value, entry, cohort, depth, state, reason, exploratory) -> None:
    result = classify(value, entry, depth_x=depth, cohort=cohort)
    assert (result.state, result.reason, result.exploratory_n) == (state, reason, exploratory)


def test_exploratory_band_never_says_markedly_and_shows_min_max() -> None:
    cohort = Cohort(values=list(np.linspace(1, 2, 20)))
    result = classify(1000.0, COHORT, depth_x=1.0, cohort=cohort)
    assert result.state == "above_typical" and result.interval == (1.0, 2.0)
    assert "exploratory" in result.caption and "n = 20" in result.caption


def test_percentile_state_carries_n_and_bootstrap_interval() -> None:
    result = classify(97.0, COHORT, depth_x=1.0, cohort=PERCENTILE_COHORT)
    assert result.percentile == pytest.approx(97.0)
    assert result.interval == bootstrap_p95_interval(PERCENTILE_COHORT.values)
    assert result.interval[0] < 95 < result.interval[1]
    assert "n = 101" in result.caption
    assert result.severity == pytest.approx(47.0)


def test_provisional_external_is_labelled() -> None:
    result = classify(0.15, COHORT, depth_x=1.0, cohort=Cohort(values=[0.1] * 12, kind="provisional_external"))
    assert "Protocol-mismatched external reference" in result.caption


def test_explicit_not_interpretable_reason_wins() -> None:
    assert classify(0.15, RANGE, depth_x=1.0, not_interpretable="no_pon").reason == "no_pon"
    with pytest.raises(ValueError):
        classify(0.15, RANGE, depth_x=1.0, not_interpretable="vibes")


def test_hepatocyte_style_depth_gate_keeps_the_estimate_visible() -> None:
    result = classify(0.031, RANGE, depth_x=0.2)
    assert result.state == "not_interpretable" and "estimate stays visible" in result.caption


def test_percentile_of_matches_numpy_inverse() -> None:
    values = [1.0, 2.0, 4.0, 8.0]
    for p in (0, 10, 33.3, 50, 90, 100):
        assert percentile_of(float(np.percentile(values, p)), values) == pytest.approx(p, abs=1e-9)


def test_findings_sort_by_state_then_tier_then_severity() -> None:
    from traceback_lab.interpretation.engine import Interpretation

    rows = [
        ("SPECULATIVE", Interpretation("markedly_above", severity=49)),
        ("PROVEN", Interpretation("above_typical", severity=1)),
        ("PROVEN", Interpretation("above_typical", severity=3)),
        ("NEWER", Interpretation("markedly_above", severity=45)),
    ]
    ordered = sorted(rows, key=lambda row: findings_sort_key(*row))
    assert [(t, i.state, i.severity) for t, i in ordered] == [
        ("NEWER", "markedly_above", 45),
        ("SPECULATIVE", "markedly_above", 49),
        ("PROVEN", "above_typical", 3),
        ("PROVEN", "above_typical", 1),
    ]


def test_qc_bands() -> None:
    entry = LIB.entry("bam-qc", "mapped_fraction")
    assert [qc_band(v, entry) for v in (0.95, 0.90, 0.80, 0.74)] == ["pass", "pass", "warn", "fail"]
    assert qc_band(0.5, LIB.entry("depth", "approx_depth_x")) is None


# ------------------------------------------------------------- loader rules
def _mutate(mutator) -> str:
    import yaml

    data = yaml.safe_load(LIBRARY_FILE.read_text())
    mutator(data)
    return yaml.safe_dump(data)


def test_shipped_library_is_valid_and_covers_every_registered_metric_family() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", LIB.version)
    registered = {algorithm.id for algorithm in load_registry()}
    for algorithm_id in LIB.algorithms():
        assert algorithm_id in registered
        assert LIB.headline(algorithm_id) is not None
    assert "read-summary" in LIB.hidden_algorithms


@pytest.mark.parametrize(
    "mutator,message",
    [
        (lambda d: d["entries"][0].update(license_ids=["nope"]), "unknown license_id"),
        (lambda d: d["entries"][0].update(headline=True), "exactly one headline"),
        (lambda d: d["entries"][1].update(headline=False), "exactly one headline"),
        (lambda d: d["entries"][0].update(tier="MAYBE"), "tier"),
        (lambda d: d.update(version="1"), "semver"),
        (lambda d: d["entries"][6].pop("stability"), "explicit stability"),
        (lambda d: d["entries"][0].update(reference={"kind": "published_range", "lo": 2, "hi": 1}), "lo <= hi"),
        (lambda d: d["entries"][0].update(performance=None, performance_missing_reason=None), "performance"),
        (lambda d: d["entries"].append(dict(d["entries"][0])), "duplicate"),
    ],
)
def test_loader_refusals(mutator, message) -> None:
    with pytest.raises(LibraryError, match=message):
        parse_library(_mutate(mutator), LICENSES)


def test_loader_reports_the_yaml_line_and_refuses_python_tags() -> None:
    with pytest.raises(LibraryError, match="line"):
        parse_library("version: 0.1.0\nentries: [\n", LICENSES)
    with pytest.raises(LibraryError):
        parse_library("!!python/object/apply:os.system ['true']\n", LICENSES)


def test_default_stability_by_value_kind() -> None:
    entry = LIB.entry("bam-qc", "mapped_fraction")
    assert entry.stability == {"kind": "proportion"}
    library = parse_library(_mutate(lambda d: d["entries"][4].pop("stability")), LICENSES)
    assert library.entry("bam-qc", "mean_read_q").stability == {"kind": "relative", "tolerance": 0.10}


def _envelope(metrics):
    return ResultEnvelope.model_validate(
        {
            "algorithm_id": "fragment-size",
            "algorithm_version": "1",
            "status": "ok",
            "provenance": provenance(
                tool="t", parameters_sha256="a" * 64, source_id="b" * 64, input_id="c" * 64,
                reference_id="r", timestamp_utc="2026-10-15T00:00:00Z",
            ),
            "metrics": metrics,
        }
    )


def test_missing_entry_is_kept_and_failed_algorithms_show_nothing() -> None:
    envelope = _envelope([{"metric_id": "mode_bp", "value": 168}, {"metric_id": "brand_new", "value": 3}])
    rows = interpret_result(envelope, LIB, depth_x=0.26)
    assert [(r.metric_id, r.interpretation.state, r.interpretation.reason) for r in rows] == [
        ("mode_bp", "no_reference", "none_published"),
        ("brand_new", "no_reference", "no_library_entry"),
    ]
    data = json.loads(envelope.to_bytes())
    data.update(status="failed", metrics=[], failure={"first_line": "x", "stderr_tail": []})
    assert interpret_result(ResultEnvelope.model_validate(data), LIB) == []


def test_editing_a_footnote_changes_no_result_byte() -> None:
    envelope = _envelope([{"metric_id": "mode_bp", "value": 168}])
    before = envelope.to_bytes()
    edited = parse_library(
        _mutate(lambda d: d["entries"][12]["performance"].update(value="edited footnote")), LICENSES
    )
    interpret_result(envelope, edited, depth_x=0.26)
    assert envelope.to_bytes() == before
    assert edited.entry("fragment-size", "mode_bp")["performance"]["value"] == "edited footnote"


def test_license_callouts_once_each_in_first_use_order() -> None:
    library = parse_library(
        _mutate(
            lambda d: (
                d["entries"][0].update(license_ids=["method-patents", "gpl-copyleft"]),
                d["entries"][1].update(license_ids=["gpl-copyleft"]),
            )
        ),
        LICENSES,
    )
    entries = [library.entry("bam-qc", "total_reads"), library.entry("bam-qc", "mapped_fraction")]
    assert [c["license_id"] for c in license_callouts(entries, library)] == ["method-patents", "gpl-copyleft"]
    assert all(c["headline"] for c in license_callouts(entries, library))


def test_d7_precedence_and_null_causes() -> None:
    assert classify(0.1, None, not_interpretable="qc").state == "not_interpretable"
    dup = LIB.entry("bam-qc", "duplicate_fraction")
    result = classify(None, dup, depth_x=1.0)
    assert (result.state, result.caption) == ("no_reference", "not marked (native ONT)")
    assert classify(None, LIB.entry("bam-qc", "mapped_fraction"), depth_x=1.0).reason == "depth"


def test_ties_take_the_mid_rank() -> None:
    assert percentile_of(7.0, [7.0] * 30) == pytest.approx(50.0)
    assert classify(7.0, COHORT, depth_x=1.0, cohort=Cohort(values=[7.0] * 30)).state == "in_range"


def test_headline_must_be_a_boolean() -> None:
    with pytest.raises(LibraryError, match="true or false"):
        parse_library(_mutate(lambda d: d["entries"][0].update(headline="false")), LICENSES)


def test_published_mode_caption_states_the_statistic() -> None:
    result = classify(168, LIB.entry("fragment-size", "mode_bp"), depth_x=0.26)
    assert result.caption.startswith("Published mode, approximately 167 bp")
    assert "n = " not in result.caption
