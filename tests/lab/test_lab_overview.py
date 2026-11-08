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


@needs_chrome
def test_result_page_marks_underpowered_as_not_a_call(tmp_path) -> None:
    with _server(tmp_path, _mixed_sample()) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/result?algorithm={UNDERPOWERED}",
                   want=lambda d: "result-status" in d)
    main = _main(dom)
    assert "Underpowered — below the method" in main and "Not a call." in main
    assert "not interpretable (depth)" in main
    assert "finding-card" not in main  # no headline card that could read as a call


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
