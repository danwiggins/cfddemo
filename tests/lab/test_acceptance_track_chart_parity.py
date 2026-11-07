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
    assert ratio_rows[1][4:] == ["unavailable", "unavailable"]
    assert [None if row[4] == "unavailable" else float(row[4]) for row in ratio_rows] == [
        row["ratio"] for row in bins]
    assert [None if row[5] == "unavailable" else float(row[5]) for row in ratio_rows] == [
        row["centred"] for row in bins]
    assert ratio_chart["categories"] == [
        f'{row["contig"]}:{row["start"] // 1_000_000} Mb' for row in bins]
    assert ratio_chart["plotted"] == [row["centred"] for row in bins]


@pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for S5 chart parity")
def test_griffin_and_aligned_end_plots_equal_adjacent_tables(tmp_path, monkeypatch) -> None:
    source_id = "a" * 64
    id12 = source_id[:12]
    sample = {
        "found": True, "id12": id12, "source_id": source_id,
        "algorithms": [
            {"algorithm_id": "fragment-size", "status": "skipped", "skip_reason": "depth", "metrics": []},
            {"algorithm_id": "griffin", "status": "ok", "hidden": False, "provenance": {}, "metrics": []},
            {"algorithm_id": "aligned-end-structure", "status": "ok", "hidden": False,
             "provenance": {}, "metrics": []},
        ],
        "catalog": [], "licenses": [], "cohort_n": 0, "cohort_version": None,
        "excluded_chunks": [], "interpretation_version": "fixture",
    }
    starts = [-30, -15, 0, 15, 30]
    coverage = [0.123456789, 0.25, 0.987654321, 0.5, 0.333333333]
    griffin = {"primary_site": "CTCF", "profiles": [{
        "site_name": "CTCF", "site_count": 17, "bin_start_bp": starts,
        "gc_corrected_coverage": coverage, "central_coverage": 0.987654321,
        "amplitude": 0.864197532, "smoothing": True, "cna_normalization": False,
    }]}
    offsets = list(range(-100, 101))
    u_counts = [abs(bp) % 7 for bp in offsets]
    d_counts = [(bp + 100) % 11 for bp in offsets]
    aligned = {
        "schema": "traceback.lab.aligned-end-structure.v1", "reference_id": "hg38",
        "reference_sha256": "c" * 64, "catalogue_identity": "d" * 64,
        "offset_bp": offsets, "u_count": u_counts, "d_count": d_counts,
        "source_label": "synthetic catalogue", "centers_source": "centers-v1",
        "preferred_source": "sites-v1", "terms": "unknown", "observed_depth_x": 10.0,
        "valid_molecules": 100, "eligible_centers": 2, "catalogue_centers": 2,
        "eligible_preferred_sites": 3, "catalogue_preferred_sites": 3,
        "ocf_like_left": 1, "ocf_like_right": 2, "ocf_like_left_ends": 3,
        "ocf_like_right_ends": 4, "preferred_u_hits": 5, "preferred_d_hits": 6,
        "valid_ends": 200, "left_d_peak_offset_bp": -60,
        "right_u_peak_offset_bp": 60,
        "aligned_span_length_bins_bp": {"lt100": 0, "100_200": 100, "201_500": 0,
                                        "501_2000": 0, "gt2000": 0},
    }
    artifacts = {"griffin": griffin, "aligned-end-structure": aligned}
    monkeypatch.setattr(web_api.LabApi, "samples", lambda self: {"samples": [
        {"id12": id12, "run_utc": "", "depth_x": 10.0, "example": False}],
        "root_has_runs": True})
    monkeypatch.setattr(web_api.LabApi, "sample", lambda self, value: sample)
    monkeypatch.setattr(web_api.LabApi, "artifact", lambda self, _id, _label, algorithm_id, _panel:
                        json.dumps(artifacts[algorithm_id]).encode())

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
                  xUnit: option.xAxis[0].name, yUnit: option.yAxis[0].name,
                  series: option.series.map((series) => ({name: series.name, data: series.data})),
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
    griffin_chart = next(item for item in observed if "Griffin" in item["label"])
    assert len(griffin_chart["series"]) == 1
    assert (griffin_chart["xUnit"], griffin_chart["yUnit"]) == ("bp from site", "relative coverage")
    assert griffin_chart["series"][0]["data"] == [
        [start + 7.5, value] for start, value in zip(starts, coverage, strict=True)]
    assert [[int(row[0]), float(row[1]), float(row[2])] for row in griffin_chart["rows"]] == [
        [start, start + 7.5, value] for start, value in zip(starts, coverage, strict=True)]

    aligned_chart = next(item for item in observed if "Upstream and downstream" in item["label"])
    assert (aligned_chart["xUnit"], aligned_chart["yUnit"]) == ("bp from center", "raw ends (not depth-normalized)")
    assert {series["name"] for series in aligned_chart["series"]} == {"U", "D"}
    assert {series["name"]: series["data"] for series in aligned_chart["series"]} == {
        "U": [[offset, count] for offset, count in zip(offsets, u_counts, strict=True)],
        "D": [[offset, count] for offset, count in zip(offsets, d_counts, strict=True)],
    }
    assert [[int(value) for value in row] for row in aligned_chart["rows"]] == [
        [offset, u, d] for offset, u, d in zip(offsets, u_counts, d_counts, strict=True)]
