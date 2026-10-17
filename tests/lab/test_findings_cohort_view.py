"""Findings counts must include reference-backed metrics in the In range group."""

from __future__ import annotations

import re

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for Findings DOM check")
def test_cohort_in_range_count_matches_collapsed_group(tmp_path, monkeypatch) -> None:
    source_id = f"{1:064x}"
    id12 = source_id[:12]
    metrics = [
        {"metric_id": "short_fraction_lt150", "label": "Short fragment share", "value": 0.12,
         "unit": "fraction", "state": "in_range", "reason": None, "reference_kind": "internal_cohort",
         "eligible_n": 10, "exploratory_n": 10, "interval": [0.08, 0.18],
         "cohort_values": [0.08 + i * 0.01 for i in range(10)], "caption": "Observed cohort min–max"},
        {"metric_id": "mode_bp", "label": "Size mode", "value": 167, "unit": "bp",
         "state": "no_reference", "reason": "none_published", "reference_kind": "none",
         "caption": "No reference"},
    ]
    sample = {
        "found": True, "id12": id12, "source_id": source_id,
        "algorithms": [{"algorithm_id": "fragment-size", "status": "ok", "hidden": False,
                        "metrics": metrics, "provenance": {}}],
        "catalog": [], "licenses": [], "cohort_n": 10, "cohort_min": 10,
        "cohort_state": "ready", "cohort_version": "healthy-v1:2",
        "interpretation_version": "1.0.0", "excluded_chunks": [],
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [{
        "id12": id12, "run_utc": "", "depth_x": 1.0, "example": False,
    }], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)

    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "chrome-profile"
        dom = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--window-size=1280,800",
            server.issue_link() + f"&route=/sample/{id12}/findings",
        ], profile, ok=lambda page: 'data-ready="1"' in page and "In range (1)" in page, timeout=60)

    summary = re.search(r'class="findings-summary">([^<]*)<', dom).group(1)
    assert "0 outside a reference" in summary
    assert "1 without a reference" in summary
    assert "1 in range" in summary
    assert "eligible cohort n = 10" in summary
    assert "No metric is outside a reference. 1 metric has a reference" in dom
    assert "1 metric has no reference" in dom
    assert "<summary>In range (1)</summary>" in dom
    assert "Short fragment share" in dom and "Observed cohort min–max" in dom
    assert 'data-csp-violations="0"' in dom
