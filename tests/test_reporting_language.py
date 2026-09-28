"""Regression guards for development-only result language."""

from __future__ import annotations

import re
from pathlib import Path


def _app_copy() -> str:
    source = Path("app.py").read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", source)


def test_personalized_results_do_not_make_negative_or_contamination_claims() -> None:
    copy = _app_copy()
    forbidden = (
        "My cfDNA is clean",
        "no short-fragment cancer signal",
        "without a short-fragment shift",
        "consistent with low high-molecular-weight gDNA contamination",
        "No shift toward the short",
        "No broad copy-number cancer signal detected",
        "research-screening scope",
        "tests for broad gains or losses",
    )

    for phrase in forbidden:
        assert phrase not in copy


def test_reference_cohort_and_copy_number_language_are_descriptive() -> None:
    copy = _app_copy()

    assert "Sample versus healthy plasma donors" not in copy
    assert "Healthy minimum" not in copy
    assert "Healthy maximum" not in copy
    assert "Which tissues contributed this cell-free DNA?" not in copy
    assert "Development estimate versus observed 23-donor reference cohort" in copy
    assert "descriptive, method-specific context" in copy
    assert "Estimated cfDNA source composition" in copy
    assert "Exploratory whole-chromosome relative dosage QC" in copy
    assert "Chromosomes outside visualization boundary" in copy


def test_each_result_renderer_uses_the_development_safety_banner() -> None:
    source = Path("app.py").read_text(encoding="utf-8")

    assert (
        "Development sample · Research use only (RUO) · Unqualified · "
        '"\n        "Not a diagnostic result"'
    ) in source
    assert source.count("_render_development_result_banner(st)") == 3
