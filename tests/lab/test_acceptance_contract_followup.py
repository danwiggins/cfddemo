"""Focused browser checks for the remaining approved Lab acceptance contracts."""

from __future__ import annotations

import copy
import re
import time

from tests.lab.test_acceptance_synthetic_routes import ID12, LIBRARY, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome


@needs_chrome
def test_viral_results_show_the_required_privacy_disclosure(tmp_path) -> None:
    sample = _sample()
    viral = next(algorithm for algorithm in sample["algorithms"] if algorithm["algorithm_id"] == "viral-reads")
    assert viral["metrics"]
    for metric in viral["metrics"]:
        assert "PRIVACY" in LIBRARY.entry("viral-reads", metric["metric_id"])["flags"]
    for metric in viral["metrics"][1:]:
        metric.update(state="no_reference", reason="no_reference")
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings")
    cards = re.findall(r'<article class="finding-card">(.*?)</article>', dom, re.DOTALL)
    marker = f'id="card-viral-reads-{viral["metrics"][0]["metric_id"]}"'
    card = next(card for card in cards if marker in card)
    assert "FLAG: PRIVACY" in card and "infection-related information" in card and "FLAG: LEGAL" in card
    for metric in viral["metrics"][1:]:
        row = re.search(rf"<tr><td>{re.escape(metric['label'])}</td>.*?</tr>", dom, re.DOTALL)
        assert row is not None
        assert "FLAG: PRIVACY" in row.group() and "infection-related information" in row.group()


@needs_chrome
def test_findings_show_every_strip_variant_and_missing_library_entry(tmp_path) -> None:
    sample = _sample()
    metrics = [(algorithm["algorithm_id"], metric)
               for algorithm in sample["algorithms"] for metric in algorithm["metrics"]]
    assert len(metrics) >= 5
    variants = (
        ("published", {"reference_kind": "published_range", "interval": [0.1, 0.2],
                       "exploratory_n": None, "percentile": None, "value": 1.0}, "strip-break"),
        ("exploratory", {"reference_kind": "internal_cohort", "interval": [0.1, 0.2],
                          "exploratory_n": 15, "percentile": None, "cohort_values": [0.12, 0.18]}, "strip-member"),
        ("percentile", {"reference_kind": "internal_cohort", "interval": None,
                         "exploratory_n": None, "percentile": 97.3}, "strip-tick"),
        ("none", {"reference_kind": "none", "interval": None,
                  "exploratory_n": None, "percentile": None}, 'class="help"'),
    )
    for (_algorithm_id, metric), (name, fields, _marker) in zip(metrics, variants, strict=False):
        metric.update(fields, state="above_typical", caption=f"{name} strip fixture")
    metrics[4][1].update(state="no_reference", reason="no_library_entry", entry=None,
                         label="Missing library entry fixture", caption="No library entry fixture")
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings")
    cards = re.findall(r'<article class="finding-card">(.*?)</article>', dom, re.DOTALL)
    for (algorithm_id, metric), (name, _fields, marker) in zip(metrics, variants, strict=False):
        card_id = f'id="card-{algorithm_id}-{metric["metric_id"]}"'
        card = next(card for card in cards if card_id in card)
        assert f"{name} strip fixture" in card and marker in card
    assert re.search(r"<tr>[^<]*<td>Missing library entry fixture</td>.*?no library entry.*?</tr>",
                     dom, re.DOTALL)


@needs_chrome
def test_sample_switcher_round_trips_through_home_deep_link(tmp_path) -> None:
    second_id = "aaaaaaaaaaaa"
    first = _sample()
    second = copy.deepcopy(first)
    second["id12"] = second_id
    second["source_id"] = "a" * 64
    samples = {ID12: first, second_id: second}
    observed = {}
    with _server(tmp_path, first) as server:
        server.app.api.samples = lambda: {"samples": [
            {"id12": id12, "run_utc": sample["run_utc"], "depth_x": 1.0, "example": False}
            for id12, sample in samples.items()
        ], "root_has_runs": True}
        server.app.api.sample = lambda id12: copy.deepcopy(samples[id12]) if id12 in samples else {"found": False}
        profile = tmp_path / "switcher"

        def switch(_command, evaluate) -> None:
            evaluate(f"document.querySelector('#sample-switcher').value = '{second_id}'")
            evaluate("document.querySelector('#sample-switcher').dispatchEvent(new Event('change'))")
            for _ in range(100):
                if evaluate("document.querySelector('#main h1')?.textContent.trim()") == f"Traceback Lab — {second_id}":
                    break
                time.sleep(0.05)
            observed["hash"] = evaluate("location.hash")
            observed["selected"] = evaluate("document.querySelector('#sample-switcher').value")
            observed["heading"] = evaluate("document.querySelector('#main h1')?.textContent.trim()")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + "&route=/"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and f"Traceback Lab — {ID12}" in dom,
            interact=switch,
        )
        deep_link = _dom(server, tmp_path / "deep-link", route=f"/?sample={second_id}",
                         want=lambda dom: f"Traceback Lab — {second_id}" in dom)
    assert observed == {"hash": f"#/?sample={second_id}", "selected": second_id,
                        "heading": f"Traceback Lab — {second_id}"}
    assert f"Traceback Lab — {second_id}" in deep_link
    assert re.search(rf'<option[^>]*value="{second_id}"[^>]*selected', deep_link)
