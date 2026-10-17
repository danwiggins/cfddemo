"""Findings counts must include reference-backed metrics in the In range group."""

from __future__ import annotations

import re

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for Findings DOM check")
@pytest.mark.parametrize("outside", [False, True])
def test_cohort_findings_counts_and_readiness_are_explicit(tmp_path, monkeypatch, outside: bool) -> None:
    source_id = f"{1:064x}"
    id12 = source_id[:12]
    metrics = [
        {"metric_id": "short_fraction_lt150", "label": "Short fragment share", "value": 0.12,
         "unit": "fraction", "state": "in_range", "reason": None, "reference_kind": "internal_cohort",
         "eligible_n": 10, "exploratory_n": 10, "interval": [0.08, 0.18],
         "cohort_values": [0.08 + i * 0.01 for i in range(10)], "caption": "Observed cohort min–max",
         "cohort_excluded": {"self": 1, "incompatible": 3}},
        {"metric_id": "mode_bp", "label": "Size mode", "value": 167, "unit": "bp",
         "state": "no_reference", "reason": "none_published", "reference_kind": "none",
         "caption": "No reference"},
    ]
    if outside:
        metrics.append({"metric_id": "peak_mono_bp", "label": "High mono peak", "value": 210,
                        "unit": "bp", "state": "above_typical", "reason": None,
                        "reference_kind": "internal_cohort", "eligible_n": 10,
                        "tier": "NEWER", "severity": 20, "caption": "Above cohort range", "entry": {}})
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
        def view(name: str) -> str:
            profile = tmp_path / name
            return _dom_cdp([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--window-size=1280,800",
                server.issue_link() + f"&route=/sample/{id12}/findings",
            ], profile, ok=lambda page: 'data-ready="1"' in page and "In range (1)" in page, timeout=60)

        dom = view("normal-profile")
        if not outside:
            metrics[0]["eligible_n"] = None
            metrics[0]["cohort_target_state"] = "stale_parameters"
            stale = view("stale-profile")
            sample["example"], sample["cohort_state"] = True, "example"
            example = view("example-profile")

    summary = re.search(r'class="findings-summary">([^<]*)<', dom).group(1)
    assert f"{int(outside)} outside a reference" in summary
    assert "1 without a reference" in summary
    assert "1 in range" in summary
    assert "eligible cohort n = 10" in summary
    assert ("protocol incompatible up to 3 per metric" if outside else "protocol incompatible 3") in summary
    assert "recompute members with matching reference, aligner and counting protocol" in summary
    assert re.findall(r"<summary>(Not interpretable|No reference|In range) \((\d+)\)", dom) == [
        ("Not interpretable", "0"), ("No reference", "1"), ("In range", "1")]
    assert dom.count('class="finding-card"') == int(outside)
    assert "1 metric has no reference" in dom
    if outside:
        assert "Above typical (1)" in dom and "High mono peak" in dom
    else:
        assert "No metric is outside a reference. 1 metric has a reference" in dom
        assert "selected result identity changed" in stale
        assert "reload or recompute this sample" in stale
        assert "EXAMPLE sample; no kit cohort reference" in example
    assert "Short fragment share" in dom and "Observed cohort min–max" in dom
    assert 'data-csp-violations="0"' in dom
