"""LABUI: the all-methods overview and per-method result pages (synthetic API responses)."""

from __future__ import annotations

import re
import time

from tests.lab.test_acceptance_synthetic_routes import ID12, LIBRARY, REGISTRY, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.web.api import _catalog

SKIPPED, FAILED, UNDERPOWERED = "copy-number", "griffin", "end-motifs"


def _mixed_sample() -> dict:
    sample = _sample()
    for algorithm in sample["algorithms"]:
        if algorithm["algorithm_id"] == SKIPPED:
            algorithm.update(status="skipped", skip_reason="missing_asset",
                             skip_message="needs a registered synthetic asset", metrics=[])
        elif algorithm["algorithm_id"] == FAILED:
            algorithm.update(status="failed", metrics=[], failure={
                "first_line": "synthetic failure line", "stderr_tail": ["synthetic tail"],
                "exit_code": 2, "timed_out": False, "reason": None})
        elif algorithm["algorithm_id"] == UNDERPOWERED:
            algorithm["underpowered"] = {"floor": 500000, "observed": 1000, "kind": "eligible_reads"}
            for metric in algorithm["metrics"]:
                metric.update(state="not_interpretable", reason="depth", value=0.123456)
    sample["catalog"] = _catalog(LIBRARY, REGISTRY, sample["algorithms"])
    return sample


def _main(dom: str) -> str:
    return dom.split("<main", 1)[1].split("</main>", 1)[0]


@needs_chrome
def test_overview_groups_every_method_with_text_status(tmp_path) -> None:
    sample = _mixed_sample()
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/overview",
                   want=lambda d: 'class="ov-groups"' in d and "ov-tile" in d)
    main = _main(dom)
    assert 'aria-current="page">All methods' in dom
    assert "Research use only. Not a diagnosis." in main
    tiles = re.findall(r'data-algorithm-id="([a-z0-9-]+)"', main)
    assert sorted(tiles) == sorted(REGISTRY)  # every visible method exactly once
    statuses = {algorithm: status for status, algorithm in
                re.findall(r'data-status="([a-z-]+)" data-algorithm-id="([a-z0-9-]+)"', main)}
    assert statuses[SKIPPED] == "skipped" and statuses[FAILED] == "failed"
    assert statuses[UNDERPOWERED] == "underpowered"
    # An underpowered card never shows a value: it reads as "not a call".
    card = re.search(rf'data-algorithm-id="{UNDERPOWERED}">(.*?)</li>', main, re.DOTALL).group(1)
    assert "Underpowered · not a call" in card and "tile-value" not in card and "12.3" not in card
    skipped = re.search(rf'data-algorithm-id="{SKIPPED}">(.*?)</li>', main, re.DOTALL).group(1)
    assert "Registered asset missing" in skipped
    for group in ("Quality and identity", "Copy number", "Methylation", "Tissue and cell origin"):
        assert group in main, group
    assert f'href="#/sample/{ID12}/result?algorithm={SKIPPED}"' in main


@needs_chrome
def test_status_filter_hides_other_methods(tmp_path) -> None:
    observed = {}
    with _server(tmp_path, _mixed_sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            evaluate("document.querySelector('.filter-bar [data-filter=\"failed\"]').click()")
            time.sleep(0.1)
            observed["shown"] = evaluate("[...document.querySelectorAll('.ov-item')].filter(i => !i.hidden).map(i => i.dataset.algorithmId)")
            observed["groups"] = evaluate("[...document.querySelectorAll('.ov-group')].filter(g => !g.hidden).length")

        _dom_cdp([_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                  server.issue_link() + f"&route=/sample/{ID12}/overview"],
                 profile, ok=lambda d: 'data-ready="1"' in d and "ov-tile" in d, interact=inspect)
    assert observed == {"shown": [FAILED], "groups": 1}


@needs_chrome
def test_result_page_explains_skip_and_fix(tmp_path) -> None:
    with _server(tmp_path, _mixed_sample()) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/result?algorithm={SKIPPED}",
                   want=lambda d: "result-status" in d)
    main = _main(dom)
    assert "Copy-number profile" in main and "Research use only. Not a diagnosis." in main
    assert "Registered asset missing" in main and "needs a registered synthetic asset" in main
    assert "How to fix:" in main and "A skipped method is not a negative finding." in main
    assert 'aria-current="page">All methods' in dom  # result pages live under the overview tab


def _underpowered_with_inputs() -> dict:
    sample = _mixed_sample()
    metric = next(a for a in sample["algorithms"] if a["algorithm_id"] == UNDERPOWERED)["headline_metric"]
    sample["curve"]["rows"] = [{
        "algorithm_id": UNDERPOWERED, "metric_id": metric, "label": "synthetic", "tier": None,
        "cells": [{"label": "0.5", "fraction": 0.5, "depth_x": 0.5, "state": "ok_usable", "value": 0.123456},
                  {"label": "full", "fraction": 1.0, "depth_x": 1.0, "state": "not_interpretable", "value": 0.123456}],
    }]
    sample["stability"].append({"algorithm_id": UNDERPOWERED, "metric_id": metric, "label": "synthetic",
                                "verdict": "synthetic", "points": [], "split": None,
                                "split_status": {"A": {"state": "ok", "value": 0.123456},
                                                 "B": {"state": "ok", "value": 0.123456}}})
    return sample


@needs_chrome
def test_result_page_marks_underpowered_as_not_a_call(tmp_path) -> None:
    with _server(tmp_path, _underpowered_with_inputs()) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/result?algorithm={UNDERPOWERED}",
                   want=lambda d: "result-inputs" in d)
    main = _main(dom)
    assert "Underpowered — below the method" in main and "Not a call." in main
    assert "not interpretable (depth)" in main
    assert "finding-card" not in main  # no headline card that could read as a call
    # No value is shown anywhere for an underpowered method: not in the metric
    # table (no raw-value disclosure either) and not on any derived input.
    metrics = re.search(r'class="panel result-metrics".*?</section>', main, re.DOTALL).group(0)
    inputs = re.search(r'class="panel result-inputs".*?</section>', main, re.DOTALL).group(0)
    assert "Withheld (underpowered)" in metrics and "raw-value" not in metrics
    for block in (metrics, inputs):
        cells = re.findall(r"<td>(.*?)</td>", block, re.DOTALL)
        values = [re.sub(r"<[^>]+>", "", cell) for cell in cells]
        assert not any(re.search(r"0\.12|12\.3", value) for value in values), values
    assert "0.123" not in main and "12.3" not in main


@needs_chrome
def test_across_inputs_shows_values_only_for_usable_cells(tmp_path) -> None:
    sample = _underpowered_with_inputs()
    for algorithm in sample["algorithms"]:
        if algorithm["algorithm_id"] == UNDERPOWERED:
            algorithm["underpowered"] = None
            for metric in algorithm["metrics"]:
                metric.update(state="in_range", reason=None)
    sample["catalog"] = _catalog(LIBRARY, REGISTRY, sample["algorithms"])
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/result?algorithm={UNDERPOWERED}",
                   want=lambda d: "result-inputs" in d)
    inputs = re.search(r'class="panel result-inputs".*?</section>', _main(dom), re.DOTALL).group(0)
    rows = {re.sub(r"<[^>]+>", "", row[0]): [re.sub(r"<[^>]+>", "", c) for c in row[1:]]
            for row in re.findall(r'<tr><th scope="row">(.*?)</th><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>', inputs)}
    assert rows["Full input"][1] == "—", rows  # not_interpretable cell: no value
    assert rows["0.5× downsample"][1] != "—", rows  # usable cell keeps its value


@needs_chrome
def test_empty_root_landing_renders_onboarding_without_glance(tmp_path) -> None:
    from traceback_lab.store import LabStore
    from traceback_lab.web.server import RunningLabServer

    with RunningLabServer(LabStore(tmp_path / "root")) as server:
        dom = _dom(server, tmp_path / "browser", route="/", want=lambda d: "onboarding-guide" in d)
    assert "Start with a synthetic example" in dom and "ov-glance" not in dom
    assert 'data-csp-violations="0"' in dom


@needs_chrome
def test_theme_toggle_has_a_fixed_name_and_pressed_state(tmp_path) -> None:
    observed = {}
    with _server(tmp_path, _mixed_sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            state = "(() => { const b = document.getElementById('theme-toggle'); return [b.getAttribute('aria-label'), b.getAttribute('aria-pressed'), document.documentElement.dataset.resolvedTheme]; })()"
            observed["before"] = evaluate(state)
            evaluate("document.getElementById('theme-toggle').click()")
            observed["after"] = evaluate(state)
            evaluate("localStorage.removeItem('traceback-lab-theme')")

        _dom_cdp([_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                  server.issue_link() + f"&route=/sample/{ID12}/overview"],
                 profile, ok=lambda d: 'data-ready="1"' in d, interact=inspect)
    # The harness pins the light system scheme; the toggle overrides it.
    assert observed == {"before": ["Dark theme", "false", "light"], "after": ["Dark theme", "true", "dark"]}


@needs_chrome
def test_result_page_shows_failure_and_unknown_method(tmp_path) -> None:
    with _server(tmp_path, _mixed_sample()) as server:
        failed = _dom(server, tmp_path / "failed", route=f"/sample/{ID12}/result?algorithm={FAILED}",
                      want=lambda d: "result-status" in d)
        unknown = _dom(server, tmp_path / "unknown", route=f"/sample/{ID12}/result?algorithm=no-such-method",
                       want=lambda d: "Method not found" in d)
    assert "synthetic failure line" in _main(failed) and "exit code 2" in _main(failed)
    assert "No visible method no-such-method" in _main(unknown)


@needs_chrome
def test_landing_has_methods_at_a_glance(tmp_path) -> None:
    with _server(tmp_path, _mixed_sample()) as server:
        dom = _dom(server, tmp_path / "browser", route="/", want=lambda d: "ov-glance" in d)
    main = _main(dom)
    assert main.index('class="landing-band"') < main.index("ov-glance") < main.index('class="clia-panel"')
    assert len(re.findall(r'class="glance-dot ', main)) == len(REGISTRY)
    assert f'href="#/sample/{ID12}/overview"' in main
