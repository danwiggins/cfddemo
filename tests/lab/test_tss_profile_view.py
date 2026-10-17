"""The Signals lens renders the measured TSS profile and its denominators."""

from __future__ import annotations

import json
import math
import re

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for TSS DOM check")
@pytest.mark.parametrize(("ratio", "rendered"), [(0.75, "0.750"), (None, "—")])
def test_tss_profile_chart_and_table(tmp_path, monkeypatch, ratio, rendered) -> None:
    source_id = "a" * 64
    sample = {
        "found": True, "id12": source_id[:12], "source_id": source_id,
        "cohort_version": None, "cohort_n": 0, "cohort_state": "not_built",
        "excluded_chunks": [], "interpretation_version": "fixture",
        "catalog": [{"algorithm_id": "tss-profile", "tier": "NEWER", "origin_tier": "NEWER",
                     "footnote": "No ONT calibration established", "citation_checked": True,
                     "license_ids": ["non-commercial-not-adopted"]}],
        "licenses": [{"license_id": "non-commercial-not-adopted",
                      "headline": "NON-COMMERCIAL: Zhao-lab code (not adopted)",
                      "body": "This TSS profile is an in-house implementation."}],
        "algorithms": [{
            "algorithm_id": "tss-profile", "status": "ok", "hidden": False,
            "metrics": [{"metric_id": "central_flank_ratio", "label": "TSS central-to-flank coverage ratio",
                         "value": ratio, "unit": "ratio", "state": "no_reference",
                         "caption": "No reference interval is published.", "entry": {"flags": ["LEGAL"]}}],
            "provenance": {},
        }],
    }
    panel = {
        "offset_bp": list(range(-1000, 1000, 50)),
        "coverage_x": [0.123456789 + index * 0.001 for index in range(40)],
        "gene_n": 19_130, "eligible_autosomal_reads": 5_000_000,
        "annotation_sha256": "d" * 64,
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [
        {"id12": source_id[:12], "run_utc": "", "depth_x": 1.0, "example": False},
    ], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, *args: json.dumps(panel).encode())

    observed = {}
    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "tss-chrome"
        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => {
              const wrap = [...document.querySelectorAll('#main .chart-wrap')].find((item) =>
                item.querySelector('.echart[aria-label*="Mean aligned-fragment coverage"]'));
              const chart = wrap.querySelector('.echart');
              const table = wrap.querySelector(':scope > details.chart-table table');
              const option = window.echarts.getInstanceByDom(chart).getOption();
              return {
                adjacent: wrap.children[0] === chart && wrap.children[1].contains(table),
                animation: option.animation,
                points: option.series.filter((series) => series.type === 'line').flatMap((series) => series.data),
                rows: [...table.querySelectorAll('tbody tr')].map((row) =>
                  [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())),
              };
            })()"""))
        html = _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--window-size=1280,800",
            server.issue_link() + f"&route=/sample/{source_id[:12]}/signals",
        ], profile, ok=lambda page: 'data-ready="1"' in page and 'data-rendered="1"' in page
            and "19,130 complete protein-coding gene windows" in page,
            timeout=60, interact=inspect)
    assert "TSS coverage profile" in html
    assert f"Central/flank ratio: {rendered}" in html
    assert "5,000,000 eligible autosomal reads" in html
    assert "TSS coverage in 50 bp bins" in html and "[-1000, -950)" in html
    assert "plotted at bin centres" in html
    assert "FLAG: LEGAL" in html
    assert "NON-COMMERCIAL: Zhao-lab code (not adopted)" in html
    assert 'data-csp-violations="0"' in html
    assert observed["adjacent"] and observed["animation"] is False
    assert len(observed["points"]) == len(observed["rows"]) == 40
    for point, (interval, displayed) in zip(observed["points"], observed["rows"], strict=True):
        match = re.fullmatch(r"\[(-?\d+), (-?\d+)\)", interval)
        assert match is not None
        assert point[0] == (int(match[1]) + int(match[2])) / 2
        assert math.isclose(float(displayed), point[1], rel_tol=0.05, abs_tol=1e-10)
