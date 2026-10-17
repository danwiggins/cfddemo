"""AC23: fragmentomic track values equal their adjacent data tables."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from traceback_lab.store import LabStore
from traceback_lab.web import api as web_api
from traceback_lab.web.server import RunningLabServer


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for track parity")
def test_end_motif_and_delfi_plots_equal_their_adjacent_tables(tmp_path, monkeypatch) -> None:
    source_id = f"{1:064x}"
    id12 = source_id[:12]

    def algorithm(algorithm_id: str, metric_id: str) -> dict:
        return {"algorithm_id": algorithm_id, "status": "ok", "hidden": False,
                "provenance": {}, "metrics": [{"metric_id": metric_id, "label": metric_id,
                                              "value": 0.2, "unit": "fraction", "state": "no_reference"}]}

    sample = {
        "found": True, "id12": id12, "source_id": source_id,
        "algorithms": [algorithm("end-motifs", "ccca_fraction"),
                       algorithm("delfi-ratio", "ratio_sd")],
        "catalog": [], "licenses": [], "cohort_n": 0, "cohort_version": None,
        "excluded_chunks": [],
    }
    motifs = [a + b + c + d for a in "ACGT" for b in "ACGT" for c in "ACGT" for d in "ACGT"]
    counts = [0] * len(motifs)
    for rank, index in enumerate((255, 0, 65, 16, 224, 80, 33, 192,
                                  128, 48, 240, 112, 176, 144, 96, 208, 32, 64), 1):
        counts[index] = rank
    total = sum(counts)
    motif_panel = {"motif": motifs, "count": counts,
                   "freq": [count / total for count in counts]}
    bins = [
        {"contig": "chr1", "start": 0, "short": 4, "long": 2, "ratio": 2.0, "centred": 0.625},
        {"contig": "chr1", "start": 5_000_000, "short": 5, "long": 0,
         "ratio": None, "centred": None},
        {"contig": "chr2", "start": 0, "short": 3, "long": 2, "ratio": 1.5, "centred": -0.375},
    ]
    artifacts = {"end-motifs": motif_panel,
                 "delfi-ratio": {"bin_bp": 5_000_000, "median_ratio_removed": True,
                                  "bins": bins}}
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [
        {"id12": id12, "run_utc": "", "depth_x": 1.0, "example": False}],
        "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact",
                        lambda self, _id, _label, algorithm_id, _panel: json.dumps(
                            artifacts[algorithm_id]).encode())

    observed = []
    with RunningLabServer(LabStore(tmp_path)) as server:
        profile = tmp_path / "chrome-profile"

        def inspect(_command, evaluate) -> None:
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .chart-wrap')]
              .map((wrap) => {
                const chart = wrap.querySelector('.echart');
                const details = wrap.querySelector(':scope > details.chart-table');
                details.open = true;
                const table = details.querySelector('table');
                const option = window.echarts.getInstanceByDom(chart).getOption();
                return {label: chart.getAttribute('aria-label'),
                  adjacent: wrap.children[0] === chart && wrap.children[1] === details,
                  visible: table.getClientRects().length > 0,
                  animation: option.animation,
                  categories: option.xAxis[0].data,
                  plotted: option.series[0].data,
                  rows: [...table.querySelectorAll('tbody tr')].map((row) =>
                    [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim()))};
              }))()"""))

        _dom_cdp([
            _chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{id12}/signals",
        ], profile, ok=lambda page: 'data-ready="1"' in page
            and page.count('data-rendered="1"') >= 2, timeout=60, interact=inspect)

    assert len(observed) == 2
    assert all(item["adjacent"] and item["visible"] and item["animation"] is False
               for item in observed)
    motif_chart = next(item for item in observed if "4-mers" in item["label"])
    motif_rows = motif_chart["rows"]
    assert len(motif_rows) == 256
    assert [row[0] for row in motif_rows] == motifs
    assert [int(row[1]) for row in motif_rows] == counts
    assert [float(row[2]) for row in motif_rows] == pytest.approx(motif_panel["freq"])
    top = sorted(motif_rows, key=lambda row: (-float(row[2]), row[0]))[:16]
    assert motif_chart["categories"] == [row[0] for row in top]
    assert motif_chart["plotted"] == pytest.approx([float(row[2]) for row in top])

    ratio_chart = next(item for item in observed if "short/long ratio" in item["label"])
    ratio_rows = ratio_chart["rows"]
    assert len(ratio_rows) == len(bins)
    assert [[row[0], int(row[1]), int(row[2]), int(row[3])] for row in ratio_rows] == [
        [row["contig"], row["start"], row["short"], row["long"]] for row in bins]
    assert [None if row[4] == "—" else float(row[4]) for row in ratio_rows] == [
        row["ratio"] for row in bins]
    assert [None if row[5] == "—" else float(row[5]) for row in ratio_rows] == [
        row["centred"] for row in bins]
    assert ratio_chart["categories"] == [
        f'{row["contig"]}:{row["start"] // 1_000_000} Mb' for row in bins]
    assert ratio_chart["plotted"] == [row["centred"] for row in bins]
