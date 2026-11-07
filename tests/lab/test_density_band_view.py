"""Signals labels observed density envelopes without turning them into references."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for density DOM check")
def test_normalized_density_band_and_fresh_unusable_vectors(tmp_path, monkeypatch) -> None:
    source_id = "a" * 64
    id12 = source_id[:12]
    counts = [0] * 1000
    counts[99], counts[166] = 30, 20
    panel = {"span_bp": list(range(1, 1001)), "count": counts}
    low = [0.0] * 651
    high = [0.0] * 651
    low[50], high[50] = 0.2, 0.5
    sample = {
        "found": True, "id12": id12, "source_id": source_id,
        "algorithms": [{"algorithm_id": "fragment-size", "status": "ok", "hidden": False,
                        "metrics": [{"metric_id": "short_fraction_lt150", "label": "Short share",
                                     "value": 0.6, "unit": "fraction", "state": "no_reference"}],
                        "provenance": {"tool": "fixture", "tool_version": "1", "parameters_sha256": "b" * 64,
                                       "input_id": "c" * 64, "source_id": source_id,
                                       "reference_id": "synthetic", "timestamp_utc": "2026-10-16T00:00:00Z"}}],
        "size_density_band": {"state": "ready", "cohort_version": "healthy-v1:8", "eligible_n": 10,
                              "excluded": {"self": 1, "incompatible": 2},
                              "span_bp": list(range(50, 701)), "low": low, "high": high},
        "catalog": [], "licenses": [], "cohort_n": 11, "cohort_version": "healthy-v1:8",
        "cohort_state": "ready", "excluded_chunks": [], "interpretation_version": "1.0.0",
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [{
        "id12": id12, "run_utc": "", "depth_x": 1.0, "example": False,
    }], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, *args: json.dumps(panel).encode())

    with RunningLabServer(LabStore(tmp_path)) as server:
        def dom(profile_name: str, marker: str) -> str:
            profile = tmp_path / profile_name
            return _dom_cdp([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--window-size=1280,800",
                server.issue_link() + f"&route=/sample/{id12}/signals",
            ], profile, ok=lambda page: 'data-ready="1"' in page and marker in page and
                "Normalized aligned-span density" in page and 'data-rendered="1"' in page, timeout=60)

        ready = dom("ready-profile", "Observed cohort density min–max (n=10)")
        # A newly built generation can also have no vectors when every member
        # has a scalar-only result or zero eligible 50–1000 bp molecules.
        sample["size_density_band"] = {"state": "no_usable_pinned_vectors", "eligible_n": 0,
                                       "excluded": {"missing_pinned_vector": 10},
                                       "span_bp": [], "low": [], "high": []}
        unavailable = dom("fresh-unusable-profile", "no usable pinned size histograms")

    assert "descriptive, not a reference interval" in ready
    assert "Exclusions: self 1 · incompatible 2" in ready or "Exclusions: incompatible 2 · self 1" in ready
    assert "% of 50–1000 bp molecules" in ready and '>60%</span>' in ready
    assert "Raw value" in ready and "0.6 fraction" in ready
    assert "167 bp marked" in ready
    assert "zero eligible 50–1000 bp molecules" in unavailable and "missing pinned vector 10" in unavailable
    assert "predates pinned" not in unavailable and "rebuild the cohort" not in unavailable
    assert "Normalized aligned-span density" in unavailable
    assert 'data-csp-violations="0"' in ready and 'data-csp-violations="0"' in unavailable
