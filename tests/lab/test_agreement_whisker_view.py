"""Agreement shows paired cohort points; percentile cutoff CI uses its own value scale."""

from __future__ import annotations

import re

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for Agreement DOM check")
def test_agreement_points_and_findings_cutoff_whisker(tmp_path, monkeypatch) -> None:
    source_id = "a" * 64
    id12 = source_id[:12]

    def algorithm(algorithm_id: str, metric: dict) -> dict:
        return {"algorithm_id": algorithm_id, "status": "ok", "hidden": False,
                "metrics": [metric], "provenance": {}}

    mode = {
        "metric_id": "mode_bp", "label": "Fragment mode", "value": 167, "unit": "bp",
        "state": "in_range", "reason": None, "reference_kind": "internal_cohort",
        "percentile": 50, "interval": [190, 200], "eligible_n": 30,
        "cohort_values": list(range(150, 150150)),
        "caption": "p = 50.0 against n = 30. Whisker: 90% bootstrap interval of the 95th-percentile cut-off (1000 resamples).",
    }
    sample = {
        "found": True, "id12": id12, "source_id": source_id, "cohort_version": "healthy-v1:8",
        "cohort_n": 31, "cohort_min": 10, "cohort_state": "ready", "excluded_chunks": [],
        "algorithms": [
            algorithm("fragment-size", mode),
            algorithm("copy-number", {"metric_id": "tumor_fraction", "label": "Tumour fraction",
                                      "value": 0.04, "unit": "fraction", "state": "no_reference"}),
            algorithm("mtdna", {"metric_id": "chrm_fraction", "label": "chrM share",
                                "value": 0.003, "unit": "fraction", "state": "no_reference"}),
        ],
        "agreement_pairs": [
            {"pair_id": "size-cn", "state": "ready", "cohort_version": "healthy-v1:8", "eligible_n": 2,
             "points": [{"source_id": f"{2:064x}", "display_id": f"{2:064x}", "x": 160, "y": 0.03},
                        {"source_id": f"{4:064x}", "display_id": f"{4:064x}", "x": 175, "y": 0.05}]},
            {"pair_id": "mtdna-cn", "state": "ready", "cohort_version": "healthy-v1:8", "eligible_n": 1,
             "points": [{"source_id": f"{2:064x}", "display_id": f"{2:064x}", "x": 0.002, "y": 0.03}]},
        ],
        "catalog": [], "licenses": [], "interpretation_version": "1.0.0",
    }
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [{
        "id12": id12, "run_utc": "", "depth_x": 1.0, "example": False,
    }], "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)

    with RunningLabServer(LabStore(tmp_path)) as server:
        def dom(route: str) -> str:
            profile = tmp_path / f"chrome-{route}"
            return _dom_cdp([
                _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--window-size=1280,800",
                server.issue_link() + f"&route=/sample/{id12}/{route}",
            ], profile, ok=lambda page: 'data-ready="1"' in page and
                    ("compatible paired cohort sources" in page and page.count('data-rendered="1"') >= 2
                     if route == "agreement" else "cutoff-whisker" in page), timeout=60)

        agreement = dom("agreement")
        findings = dom("findings")

    assert "2 compatible paired cohort sources from healthy-v1:8" in agreement
    assert "1 compatible paired cohort source from healthy-v1:8" in agreement
    assert f"{2:064x}" in agreement and f"{4:064x}" in agreement
    assert "agreement is not validation" in agreement
    assert agreement.count('data-rendered="1"') >= 2
    assert 'class="strip-svg cutoff-whisker"' in findings
    assert "90% bootstrap interval 190 bp to 200 bp; separate value scale" in findings
    assert "Cutoff CI: 190 bp–200 bp on a separate value scale" in findings
    whisker = findings.split('class="strip-svg cutoff-whisker"', 1)[1].split("</svg>", 1)[0]
    segment = re.search(r'<line x1="([0-9.]+)" x2="([0-9.]+)" y1="14" y2="14"', whisker)
    assert segment is not None and float(segment.group(2)) - float(segment.group(1)) > 50
    assert 'data-csp-violations="0"' in agreement and 'data-csp-violations="0"' in findings
