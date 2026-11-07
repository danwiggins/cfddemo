"""AC23: every SVG chart has an adjacent table of its plotted data."""

import json
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import needs_chrome


def _observe(server, tmp_path, route, wait_for=""):
    profile = Path(tmp_path / "browser-profile")
    observed = []

    def inspect(_command, evaluate):
        observed.extend(evaluate("""(() => [...document.querySelectorAll('#main svg[role="img"]')].map((svg) => {
          const details = svg.nextElementSibling;
          if (details?.matches('details.chart-table')) details.open = true;
          const table = details?.querySelector('table');
          return {
            chartClass: svg.getAttribute('class') || '',
            label: svg.getAttribute('aria-label'),
            adjacent: !!table && table.getClientRects().length > 0,
            headers: table ? [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim()) : [],
            rows: table ? [...table.querySelectorAll('tbody tr')].map((tr) =>
              [...tr.querySelectorAll('td')].map((td) =>
                (td.querySelector('.display-value') || td).textContent.trim())).filter((row) => row.length) : [],
            rawRows: table ? [...table.querySelectorAll('tbody tr')].map((tr) =>
              [...tr.querySelectorAll('td')].map((td) =>
                td.querySelector('details.raw-value code')?.textContent.trim() || null)).filter((row) => row.length) : [],
            rawLabels: table ? [...table.querySelectorAll('details.raw-value summary')].map((summary) =>
              summary.getAttribute('aria-label')) : [],
            followingRows: details?.nextElementSibling?.matches('details.chart-table')
              ? [...details.nextElementSibling.querySelectorAll('tbody tr')].map((tr) =>
                [...tr.querySelectorAll('td')].map((td) =>
                  (td.querySelector('.display-value') || td).textContent.trim())) : [],
            members: [...svg.querySelectorAll('circle.strip-member')].length,
            bands: [...svg.querySelectorAll('rect.band')].map((rect) =>
              ({y: Number(rect.getAttribute('y')), height: Number(rect.getAttribute('height'))})),
            points: [...svg.querySelectorAll('text.pt')].map((point) =>
              ({y: Number(point.getAttribute('y')), glyph: point.textContent.trim()})),
            marks: [...svg.querySelectorAll('text.strip-mark')].map((mark) => mark.textContent.trim()),
          };
        }))()"""))

    _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={profile}", server.issue_link() + f"&route={route}"],
        profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="main"' in dom and wait_for in dom,
        interact=inspect,
    )
    return observed


@needs_chrome
def test_reference_strip_families_have_formatted_adjacent_values(tmp_path) -> None:
    sample = _sample()
    published, exploratory, percentile = [algorithm["metrics"][0] for algorithm in sample["algorithms"][:3]]
    published.update(value=0.123456789, interval=[0.01, 0.2])
    exploratory.update(reference_kind="internal_cohort", value=0.345678901,
                       interval=[0.1, 0.5], exploratory_n=3,
                       cohort_values=[0.111111111, 0.222222222, 0.444444444])
    percentile.update(reference_kind="internal_cohort", value=0.789123456,
                      interval=[0.5, 0.7], percentile=98.7654321)
    with _server(tmp_path, sample) as server:
        charts = _observe(server, tmp_path, f"/sample/{ID12}/findings")

    assert charts and all(chart["label"] and chart["adjacent"] for chart in charts)
    published_chart = next(chart for chart in charts if [row[1] for row in chart["rows"]][-1] == "0.123 (unit not declared)")
    exploratory_chart = next(chart for chart in charts if chart["members"] == 3)
    percentile_chart = next(chart for chart in charts if chart["headers"][1] == "Percentile (%)")
    cutoff_chart = next(chart for chart in charts if "cutoff-whisker" in chart["chartClass"])
    assert ["0.01 (unit not declared)", "0.2 (unit not declared)", "0.123 (unit not declared)"] == [row[1] for row in published_chart["rows"]]
    assert [row[1] for row in published_chart["rawRows"]] == [
        "0.01 unit not declared", "0.2 unit not declared", "0.123456789 unit not declared"]
    assert all("published reference strip" in label and "Value" in label
               for label in published_chart["rawLabels"])
    assert [row[1] for row in exploratory_chart["rows"]] == [
        "0.1 (unit not declared)", "0.5 (unit not declared)", "0.111 (unit not declared)",
        "0.222 (unit not declared)", "0.444 (unit not declared)", "0.346 (unit not declared)"]
    assert [row[1] for row in percentile_chart["rows"]] == ["5", "95", "1", "5", "95", "99", "98.7654321"]
    assert cutoff_chart["marks"] == ["▶"]
    assert cutoff_chart["rows"] == [
        ["Cutoff interval low", "0.5 (unit not declared)", "On scale"],
        ["Cutoff interval high", "0.7 (unit not declared)", "On scale"],
        ["This sample", "0.789 (unit not declared)", "Off scale right"]]


@needs_chrome
def test_raw_details_distinguish_a_boundary_hidden_by_display_rounding(tmp_path) -> None:
    sample = _sample()
    metric = sample["algorithms"][0]["metrics"][0]
    metric.update(value=0.30004, interval=[0.1, 0.3], state="above_typical")
    with _server(tmp_path, sample) as server:
        charts = _observe(server, tmp_path, f"/sample/{ID12}/findings")
    strip = next(chart for chart in charts if chart["rows"] and
                 chart["rows"][-1][:1] == ["This sample"] and chart["rows"][-1][1] == "0.3 (unit not declared)")
    assert strip["rows"][-2][1] == strip["rows"][-1][1]
    assert strip["rawRows"][-2][1] == "0.3 unit not declared"
    assert strip["rawRows"][-1][1] == "0.30004 unit not declared"
    assert float(strip["rawRows"][-1][1].split()[0]) > float(strip["rawRows"][-2][1].split()[0])


@needs_chrome
def test_stability_svg_table_exposes_band_endpoints_and_split_values(tmp_path) -> None:
    sample = _sample()
    panel = sample["stability"][0]
    panel["points"] = [
        {"fraction": 0.25, "state": "ok_usable", "reason": None, "value": 0.123456789,
         "deviation": -12.3456789, "unit": "percent",
         "band": {"lo": -17.1234567, "hi": -8.23456789, "unit": "percent"}},
        {"fraction": 1.0, "state": "ok_usable", "reason": None, "value": 0.2,
         "deviation": 0.0, "unit": "percent", "band": None},
    ]
    panel["split_status"] = {
        "A": {"state": "ok", "reason": None, "value": 0.156789123, "deviation": 2.3456789, "unit": "percent"},
        "B": {"state": "ok", "reason": None, "value": 0.167891234, "deviation": 3.4567891, "unit": "percent"},
    }
    with _server(tmp_path, sample) as server:
        charts = _observe(server, tmp_path, f"/sample/{ID12}/stability")

    assert charts and all(chart["label"] and chart["adjacent"] for chart in charts)
    first = charts[0]
    assert first["headers"][1] == "Input fraction (%)"
    assert first["headers"][3] == "Relative deviation (%)"
    assert first["headers"][4:6] == ["Relative band low (%)", "Relative band high (%)"]
    assert first["bands"] and first["bands"][0]["height"] > 0
    def y(value):
        return 70 - (value / 50) * 58

    assert first["bands"][0]["y"] == pytest.approx(y(-8.23456789))
    assert first["bands"][0]["height"] == pytest.approx(y(-17.1234567) - y(-8.23456789))
    assert first["points"][0] == {"y": pytest.approx(y(-12.3456789) + 4), "glyph": "●"}
    assert [row[:6] for row in first["rows"]] == [
        ["Measured fraction", "25", "0.123 (unit not declared)", "-12.3%", "-17.1%", "-8.23%"],
        ["Measured fraction", "100", "0.2 (unit not declared)", "0%", "unavailable", "unavailable"],
        ["Split A", "50", "0.157 (unit not declared)", "2.35%", "unavailable", "unavailable"],
        ["Split B", "50", "0.168 (unit not declared)", "3.46%", "unavailable", "unavailable"],
    ]
    assert first["rawRows"][0][2:6] == [
        "0.123456789 unit not declared", "-12.3456789 %", "-17.1234567 %", "-8.23456789 %"]


@needs_chrome
def test_composition_plotted_subset_has_adjacent_table_and_full_source_remains_available(tmp_path) -> None:
    sample = _sample()
    contributors = [
        {"contributor_id": "A", "fraction": 0.123456789, "interval": [0.1, 0.2], "interval_state": "ok"},
        {"contributor_id": "B", "fraction": 0, "interval": [0, 0], "interval_state": "ok"},
    ]
    with _server(tmp_path, sample) as server:
        server.app.api.artifact = lambda id12, label, algorithm_id, panel_id: (
            json.dumps({"confidence_level": 0.9, "contributors": contributors}).encode()
            if (id12, label, algorithm_id, panel_id) ==
            (ID12, "full", "cell-origin", "composition") else None)
        charts = _observe(server, tmp_path, f"/sample/{ID12}/findings", "composition-svg")

    composition = next(chart for chart in charts if chart["chartClass"] == "composition-svg")
    assert composition["adjacent"]
    assert composition["rows"] == [["A", "12.3%", "10%", "20%", "ok"]]
    assert composition["rawRows"][0][1:4] == [
        "0.123456789 fraction", "0.1 fraction", "0.2 fraction"]
    assert composition["followingRows"] == [
        ["A", "12.3%", "10%", "20%", "ok"], ["B", "0%", "0%", "0%", "ok"]]


@needs_chrome
def test_sample_comparison_overlay_has_formatted_adjacent_values(tmp_path) -> None:
    sample = _sample()
    other = "a1b2c3d4e5f6"
    with _server(tmp_path, sample) as server:
        listing = server.app.api.samples()["samples"]
        server.app.api.samples = lambda: {"samples": [*listing, {**listing[0], "id12": other}],
                                          "root_has_runs": True}
        server.app.api.compare_samples = lambda a, b: {
            "found": True, "state": "ready", "id12_a": ID12, "id12_b": other,
            "source_id_a": sample["source_id"], "source_id_b": other + "d" * 52,
            "input_id_a": sample["full_input_id"], "input_id_b": "c" * 64,
            "aligner_a": "bwa", "aligner_b": "bwa", "unscored": [],
            "metrics": [{"algorithm_id": "copy-number", "metric_id": "tumor_fraction",
                         "label": "Tumour fraction", "unit": "fraction",
                         "value_a": 0.123456789, "value_b": 0.234567891,
                         "delta_b_minus_a": 0.111111102}],
        }
        charts = _observe(server, tmp_path, f"/sample/{ID12}/compare", "sample-overlay")

    overlay = next(chart for chart in charts if chart["chartClass"] == "")
    assert overlay["adjacent"] and "Tumour fraction" in overlay["label"]
    assert overlay["rows"] == [["Sample A", "12.3%"], ["Sample B", "23.5%"],
                                ["B−A", "11.1 percentage points"]]
    assert [row[1] for row in overlay["rawRows"]] == [
        "0.123456789 fraction", "0.234567891 fraction", "0.111111102 fraction"]


@needs_chrome
def test_body_map_svg_groups_keep_their_existing_adjacent_tables(tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        charts = _observe(server, tmp_path, f"/sample/{ID12}/body-map")
    assert charts and all(chart["chartClass"] == "composition-svg" and chart["adjacent"]
                          and chart["rows"] for chart in charts)
