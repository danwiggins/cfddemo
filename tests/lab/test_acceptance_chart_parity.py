"""AC23: plotted values and their adjacent data tables agree in a real browser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome


@needs_chrome
@pytest.mark.parametrize("route,expected", [("/", 1), (f"/sample/{ID12}/agreement", 2)])
def test_echarts_scatter_values_match_adjacent_table_without_animation(route, expected, tmp_path) -> None:
    sample = _sample()
    sample["synthetic"] = [
        {"tf_expected": 0.1, "tf_recovered": 0.123456789, "recovered": True, "status": "recovered"},
        {"tf_expected": 0.3, "tf_recovered": 0.29000000001, "recovered": True, "status": "recovered"},
    ]
    values = {("copy-number", "tumor_fraction"): 0.17,
              ("fragment-size", "mode_bp"): 167.0,
              ("mtdna", "chrm_fraction"): 0.08}
    for algorithm in sample["algorithms"]:
        for metric in algorithm["metrics"]:
            key = (algorithm["algorithm_id"], metric["metric_id"])
            if key in values:
                metric["value"] = values[key]
    sample["concordance"] = {"pairs": [
        {"id": "cn-meth", "label": "Methylation TF vs copy-number TF", "state": "ok",
         "x": 0.17, "y": 0.13, "x_label": "copy-number TF", "y_label": "methylation TF",
         "baseline_median": None, "reference_n": 0},
        {"id": "cn-size", "label": "Size shift vs copy-number TF", "state": "ok",
         "x": 0.17, "y": 12.0, "x_label": "copy-number TF", "y_label": "size shift (bp)",
         "baseline_median": 155.0, "reference_n": 12},
    ]}

    observed = []
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .chart-wrap')].map((wrap) => {
              const chart = wrap.querySelector('.echart');
              const details = wrap.querySelector(':scope > details.chart-table');
              details.open = true;
              const table = details.querySelector('table');
              const option = window.echarts.getInstanceByDom(chart)?.getOption();
              return {
                label: chart?.getAttribute('aria-label'),
                adjacent: wrap.children[0] === chart && wrap.children[1]?.contains(table),
                visible: table.getClientRects().length > 0,
                animation: option?.animation,
                points: option?.series.filter((series) => series.type === 'scatter').flatMap((series) => series.data),
                rows: [...table.querySelectorAll('tbody tr')].map((row) =>
                  [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())),
              };
            }))()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route={route}"],
            profile,
            ok=lambda dom: 'data-ready="1"' in dom and dom.count('data-rendered="1"') >= expected,
            interact=inspect,
        )

    assert len(observed) == expected
    for chart in observed:
        assert chart["label"] and chart["adjacent"] and chart["visible"] and chart["animation"] is False
        assert len(chart["points"]) == len(chart["rows"])
        # Home's first two table columns are the pair; Agreement has a sample-ID column.
        table_pairs = [row[:2] if route == "/" else row[1:3] for row in chart["rows"]]
        assert [[float(value) for value in pair] for pair in table_pairs] == chart["points"]


@needs_chrome
@pytest.mark.parametrize("route", (f"/sample/{ID12}/trust-qc", f"/sample/{ID12}/signals"))
def test_fragment_density_line_values_match_adjacent_table_without_animation(route, tmp_path) -> None:
    sample = _sample()
    data = json.dumps({"span_bp": [49, 50, 167, 700, 701], "count": [9, 2, 10, 3, 7]}).encode()
    observed = []
    with _server(tmp_path, sample) as server:
        server.app.api.artifact = lambda id12, label, algorithm_id, panel_id: (
            data if (id12, label, algorithm_id, panel_id) ==
            (ID12, "full", "fragment-size", "size-density") else None
        )
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .chart-wrap')].filter((wrap) =>
              wrap.querySelector('.echart[aria-label*="density"]')).map((wrap) => {
              const chart = wrap.querySelector('.echart');
              const details = wrap.querySelector(':scope > details.chart-table');
              details.open = true;
              const table = details.querySelector('table');
              const option = window.echarts.getInstanceByDom(chart)?.getOption();
              return {
                adjacent: wrap.children[0] === chart && wrap.children[1]?.contains(table),
                visible: table.getClientRects().length > 0,
                animation: option?.animation,
                points: option?.series.filter((series) => series.type === 'line').flatMap((series) => series.data),
                rows: [...table.querySelectorAll('tbody tr')].map((row) =>
                  [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())),
              };
            }))()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route={route}"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'data-rendered="1"' in dom,
            interact=inspect,
        )
    assert len(observed) == 1
    chart = observed[0]
    assert chart["adjacent"] and chart["visible"] and chart["animation"] is False
    assert chart["points"] == [[50, 2], [167, 10], [700, 3]]
    assert [[float(value) for value in row] for row in chart["rows"]] == chart["points"]


@needs_chrome
def test_genome_copy_number_points_match_unmasked_table_bins_in_genome_order(tmp_path) -> None:
    bins = [
        {"chr": "chr1", "start": 0, "end": 1000, "log2": 0.123456789, "mask": None},
        {"chr": "chr1", "start": 1000, "end": 2000, "log2": None, "mask": "low_mappability"},
        {"chr": "chr2", "start": 0, "end": 500, "log2": -1.23456789, "mask": None},
        {"chr": "chr2", "start": 500, "end": 800, "log2": 0.0000004321, "mask": None},
    ]
    observed = {}
    with _server(tmp_path, _sample()) as server:
        server.app.api.artifact = lambda id12, label, algorithm_id, panel_id: (
            json.dumps({"bins": bins}).encode() if (id12, label, algorithm_id, panel_id) ==
            (ID12, "full", "copy-number", "genome-track") else None
        )
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => {
              const wrap = [...document.querySelectorAll('#main .chart-wrap')].find((item) =>
                item.querySelector('.echart[aria-label*="Copy-number log2 ratio"]'));
              const chart = wrap.querySelector('.echart');
              const details = wrap.querySelector(':scope > details.chart-table');
              details.open = true;
              const table = details.querySelector('table');
              const option = window.echarts.getInstanceByDom(chart).getOption();
              return {
                adjacent: wrap.children[0] === chart && wrap.children[1].contains(table),
                visible: table.getClientRects().length > 0,
                animation: option.animation,
                points: option.series.filter((series) => series.type === 'scatter').flatMap((series) => series.data),
                rows: [...table.querySelectorAll('tbody tr')].map((row) =>
                  [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())),
              };
            })()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/signals"],
            profile,
            ok=lambda dom: 'data-ready="1"' in dom and 'Copy-number bins' in dom
            and 'aria-label="Copy-number log2 ratio' in dom and 'data-rendered="1"' in dom,
            action="""(() => {
              const tab = [...document.querySelectorAll('#main .subtab')]
                .find((item) => item.textContent === 'Copy number');
              if (!tab) return false;
              tab.click();
              return true;
            })()""",
            interact=inspect,
        )
    assert observed["adjacent"] and observed["visible"] and observed["animation"] is False
    assert len(observed["rows"]) == len(bins)
    offsets = {}
    cursor = 0
    expected = []
    for (chromosome, start, end, log2, _mask), source in zip(observed["rows"], bins, strict=True):
        beginning = int(start.replace(",", ""))
        ending = int(end.replace(",", ""))
        if chromosome not in offsets:
            offsets[chromosome] = cursor
        cursor = max(cursor, offsets[chromosome] + ending)
        if log2 != "—":
            expected.append([(offsets[chromosome] + beginning) / 1_000_000, float(log2)])
            assert float(log2) == source["log2"]
    assert observed["points"] == [
        [0.0, 0.123456789], [0.002, -1.23456789], [0.0025, 0.0000004321],
    ]
    assert observed["points"] == expected
