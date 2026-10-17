"""The Signals lens renders the measured TSS profile and its denominators."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for TSS DOM check")
def test_tss_profile_chart_and_table(tmp_path, monkeypatch) -> None:
    source_id = "a" * 64
    sample = {
        "found": True, "id12": source_id[:12], "source_id": source_id,
        "cohort_version": None, "cohort_n": 0, "cohort_state": "not_built",
        "excluded_chunks": [], "catalog": [], "licenses": [], "interpretation_version": "fixture",
        "algorithms": [{
            "algorithm_id": "tss-profile", "status": "ok", "hidden": False,
            "metrics": [{"metric_id": "central_flank_ratio", "label": "TSS central-to-flank coverage ratio",
                         "value": 0.75, "unit": "ratio", "state": "no_reference",
                         "caption": "No reference interval is published."}],
            "provenance": {},
        }],
    }
    panel = {
        "offset_bp": list(range(-1000, 1000, 50)), "coverage_x": [0.75] * 40,
        "gene_n": 19_130, "eligible_autosomal_reads": 5_000_000,
        "annotation_sha256": "d" * 64,
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [
        {"id12": source_id[:12], "run_utc": "", "depth_x": 1.0, "example": False},
    ], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, *args: json.dumps(panel).encode())

    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "tss-chrome"
        html = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--window-size=1280,800",
            server.issue_link() + f"&route=/sample/{source_id[:12]}/signals",
        ], profile, ok=lambda page: 'data-ready="1"' in page and "19,130 complete protein-coding gene windows" in page,
            timeout=60)
    assert "TSS coverage profile" in html
    assert "Central/flank ratio: 0.750" in html
    assert "5,000,000 eligible autosomal reads" in html
    assert "TSS coverage in 50 bp bins" in html
    assert 'data-csp-violations="0"' in html
