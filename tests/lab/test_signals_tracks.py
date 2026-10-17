"""Signals displays the published fragmentomics artifacts, including null bins."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for Signals DOM check")
def test_end_motif_and_delfi_tracks_have_charts_and_complete_tables(tmp_path, monkeypatch) -> None:
    source_id = f"{1:064x}"
    id12 = source_id[:12]

    def algorithm(algorithm_id: str, metric_id: str) -> dict:
        return {
            "algorithm_id": algorithm_id, "status": "ok", "hidden": False,
            "provenance": {"tool": "fixture", "tool_version": "1", "parameters_sha256": "a" * 64,
                           "input_id": "b" * 64, "source_id": source_id,
                           "reference_id": "synthetic", "timestamp_utc": "2026-10-16T00:00:00Z"},
            "metrics": [{
                "metric_id": metric_id, "label": metric_id, "value": 0.2,
                "unit": "fraction", "state": "no_reference", "reason": "none_published",
            }],
        }

    sample = {
        "found": True, "id12": id12, "source_id": source_id,
        "algorithms": [algorithm("end-motifs", "ccca_fraction"), algorithm("delfi-ratio", "ratio_sd")],
        "catalog": [{"algorithm_id": algorithm_id, "tier": "NEWER", "license_ids": ["shared"]}
                    for algorithm_id in ("end-motifs", "delfi-ratio")],
        "licenses": [{"license_id": "shared", "headline": "Research-only method", "body": "Synthetic fixture license."}],
        "cohort_n": 0, "cohort_version": None,
        "excluded_chunks": [],
    }
    motifs = [a + b + c + d for a in "ACGT" for b in "ACGT" for c in "ACGT" for d in "ACGT"]
    artifacts = {
        "end-motifs": {
            "motif": motifs, "count": [2] + [0] * 255, "freq": [1.0] + [0.0] * 255,
        },
        "delfi-ratio": {
            "bin_bp": 5_000_000, "median_ratio_removed": True,
            "bins": [
                {"contig": "chr1", "start": 0, "short": 4, "long": 2, "ratio": 2.0, "centred": 0.5},
                {"contig": "chr1", "start": 5_000_000, "short": 5, "long": 0, "ratio": None, "centred": None},
                {"contig": "chr2", "start": 0, "short": 2, "long": 1, "ratio": 2.0, "centred": 0.5},
            ],
        },
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [{
        "id12": id12, "run_utc": "", "depth_x": 1.0, "example": False,
    }], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, id12, label, algorithm_id, panel_id: (
        json.dumps(artifacts[algorithm_id]).encode()
    ))

    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "chrome-profile"
        dom = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--window-size=1280,800",
            server.issue_link() + f"&route=/sample/{id12}/signals",
        ], profile, ok=lambda page: 'data-ready="1"' in page and
            "All 256 aligned-end 4-mers" in page and "DELFI-style 5 Mb bins" in page and
            page.count('data-rendered="1"') >= 2, timeout=60)
        artifacts["end-motifs"]["freq"][0] = None
        artifacts["delfi-ratio"]["bins"][0]["centred"] = "invalid"
        invalid_profile = tmp_path / "chrome-invalid-profile"
        invalid_dom = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={invalid_profile}", "--window-size=1280,800",
            server.issue_link() + f"&route=/sample/{id12}/signals",
        ], invalid_profile, ok=lambda page: 'data-ready="1"' in page and
            "End-motif chart unavailable (motif panel values invalid)" in page and
            "Short/long ratio track unavailable (ratio track bin values invalid)" in page, timeout=60)

    assert "All 256 aligned-end 4-mers" in dom, dom[-3000:]
    assert "DELFI-style 5 Mb bins" in dom, dom[-3000:]
    assert "Sixteen most frequent aligned-end 4-mers" in dom
    assert "AAAA" in dom and "TTTT" in dom
    assert "Median-centred short/long ratio" in dom
    assert "No GC correction or classifier" in dom
    assert "chr1" in dom and "chr2" in dom
    assert dom.count('data-rendered="1"') >= 2
    assert dom.count('id="license-shared"') == 1
    assert "data-csp-violations=\"0\"" in dom
    assert "End-motif chart unavailable (motif panel values invalid)" in invalid_dom
    assert "Short/long ratio track unavailable (ratio track bin values invalid)" in invalid_dom
    assert "data-csp-violations=\"0\"" in invalid_dom
