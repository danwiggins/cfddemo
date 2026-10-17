"""AC23: plotted values and their adjacent data tables agree in a real browser."""

from __future__ import annotations

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
        {"tf_expected": 0.1, "tf_recovered": 0.12, "recovered": True, "status": "recovered"},
        {"tf_expected": 0.3, "tf_recovered": 0.29, "recovered": True, "status": "recovered"},
    ]
    values = {("copy-number", "tumor_fraction"): 0.17,
              ("fragment-size", "mode_bp"): 167.0,
              ("mtdna", "chrm_fraction"): 0.08}
    for algorithm in sample["algorithms"]:
        for metric in algorithm["metrics"]:
            key = (algorithm["algorithm_id"], metric["metric_id"])
            if key in values:
                metric["value"] = values[key]

    observed = []
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .chart-wrap')].map((wrap) => {
              const chart = wrap.querySelector('.echart');
              const table = wrap.querySelector(':scope > details.chart-table table');
              const option = window.echarts.getInstanceByDom(chart)?.getOption();
              return {
                label: chart?.getAttribute('aria-label'),
                adjacent: wrap.children[0] === chart && wrap.children[1]?.contains(table),
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
        assert chart["label"] and chart["adjacent"] and chart["animation"] is False
        assert len(chart["points"]) == len(chart["rows"])
        # Home's first two table columns are the pair; Agreement has a sample-ID column.
        table_pairs = [row[:2] if route == "/" else row[1:3] for row in chart["rows"]]
        assert [[float(value) for value in pair] for pair in table_pairs] == chart["points"]
