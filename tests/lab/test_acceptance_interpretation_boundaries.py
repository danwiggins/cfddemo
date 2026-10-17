"""Synthetic AC9 boundary cases kept separate from the library loader tests."""

from __future__ import annotations

import numpy as np
import pytest

from traceback_lab.interpretation.engine import Cohort, Entry, classify, load_library


_ENTRY = Entry("bam-qc", "mapped_fraction", {
    **load_library().entry("bam-qc", "mapped_fraction").raw,
    "reference": {"kind": "internal_cohort"},
    "min_depth_x": 0.5,
})
_VALUES = list(np.arange(101.0))


@pytest.mark.parametrize(
    "n,value,state,exploratory",
    [
        (9, 1000.0, "no_reference", None),
        (10, -1.0, "below_typical", 10),
        (10, 1000.0, "above_typical", 10),
        (29, -1.0, "below_typical", 29),
        (29, 1000.0, "above_typical", 29),
        (30, -1.0, "markedly_below", None),
        (30, 1000.0, "markedly_above", None),
    ],
)
def test_cohort_size_transition(n: int, value: float, state: str, exploratory: int | None) -> None:
    result = classify(value, _ENTRY, depth_x=1.0, cohort=Cohort(values=_VALUES[:n]))
    assert (result.state, result.exploratory_n) == (state, exploratory)
    if 10 <= n < 30:
        assert result.interval == (0.0, float(n - 1))
        assert f"n = {n}" in result.caption and "exploratory" in result.caption
    if n >= 30:
        assert result.percentile is not None and result.interval is not None
        assert "n = 30" in result.caption


@pytest.mark.parametrize("p,state", [
    (0.99, "markedly_below"),
    (1.0, "below_typical"),
    (4.99, "below_typical"),
    (5.0, "in_range"),
    (95.0, "in_range"),
    (95.01, "above_typical"),
    (99.0, "above_typical"),
    (99.01, "markedly_above"),
])
def test_percentile_boundary_state(p: float, state: str) -> None:
    result = classify(p, _ENTRY, depth_x=1.0, cohort=Cohort(values=_VALUES))
    assert result.state == state
    assert result.percentile == pytest.approx(p)


@pytest.mark.parametrize("value,state", [
    (0.0999, "below_typical"),
    (0.1, "in_range"),
    (0.2, "in_range"),
    (0.2001, "above_typical"),
    (1000.0, "above_typical"),
])
def test_published_range_edges_without_cohort(value: float, state: str) -> None:
    entry = Entry("bam-qc", "mapped_fraction", {
        **_ENTRY.raw,
        "reference": {"kind": "published_range", "lo": 0.1, "hi": 0.2, "unit": "", "source": "Synthetic"},
    })
    result = classify(value, entry, depth_x=1.0)
    assert result.state == state
    assert result.exploratory_n is None and result.percentile is None
