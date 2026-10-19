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
        kind, source = server.app.assets["/assets/lab.js"]
        server.app.assets["/assets/lab.js"] = (kind, source + b"""
          window.addEventListener('load', () => {
            const timer = setInterval(() => {
              if (document.body.dataset.ready !== '1') return;
              const group = [...document.querySelectorAll('#main details.finding-group')]
                .find((item) => item.querySelector('summary')?.textContent.startsWith('No reference'));
              if (!group || !group.querySelector('tr .flag-pill')) return;
              group.open = true;
              const wrap = group.querySelector('.table-wrap');
              const pill = group.querySelector('tr .flag-pill');
              document.body.dataset.viralRows = String(group.querySelectorAll('tbody tr').length);
              document.body.dataset.viralPageOverflow = String(
                document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
              document.body.dataset.viralWrapContained = String(
                wrap.getBoundingClientRect().right <= window.innerWidth + 1);
              document.body.dataset.viralWrapOverflow = getComputedStyle(wrap).overflowX;
              document.body.dataset.viralPillWrap = getComputedStyle(pill).whiteSpace;
              clearInterval(timer);
            }, 20);
          });
        """)
        dom = _dom(server, tmp_path / "browser", size="360,740",
                   route=f"/sample/{ID12}/findings", want=lambda page: 'data-viral-rows' in page)
    assert 'data-viral-rows="' in dom and 'data-viral-rows="0"' not in dom
    assert 'data-viral-page-overflow="false"' in dom
    assert 'data-viral-wrap-contained="true"' in dom
    assert 'data-viral-wrap-overflow="auto"' in dom
    assert 'data-viral-pill-wrap="nowrap"' not in dom
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
    for (_algorithm_id, metric), (name, fields, _marker) in zip(metrics[:4], variants, strict=True):
        metric.update(fields, state="above_typical", caption=f"{name} strip fixture")
    metrics[4][1].update(state="no_reference", reason="no_library_entry", entry=None,
                         label="Missing library entry fixture", caption="No library entry fixture")
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings")
    cards = re.findall(r'<article class="finding-card">(.*?)</article>', dom, re.DOTALL)
    for (algorithm_id, metric), (name, _fields, marker) in zip(metrics[:4], variants, strict=True):
        card_id = f'id="card-{algorithm_id}-{metric["metric_id"]}"'
        card = next(card for card in cards if card_id in card)
        assert f"{name} strip fixture" in card and marker in card
    assert re.search(r"<tr>[^<]*<td>Missing library entry fixture</td>.*?no library entry.*?</tr>",
                     dom, re.DOTALL)


@needs_chrome
def test_findings_details_break_down_each_reason_count(tmp_path) -> None:
    sample = _sample()
    metrics = [metric for algorithm in sample["algorithms"] for metric in algorithm["metrics"]]
    assert len(metrics) >= 6
    for metric, state, reason in zip(
        metrics[:6],
        ("not_interpretable", "not_interpretable", "not_interpretable",
         "no_reference", "no_reference", "in_range"),
        ("depth", "depth", "qc", "cohort_too_small", "no_library_entry", None),
        strict=True,
    ):
        metric.update(state=state, reason=reason)
    metrics[4].update(entry=None, label="Missing library entry fixture")

    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings")

    summaries = re.findall(r'<details class="finding-group"><summary>(.*?)</summary>', dom, re.DOTALL)
    assert len(summaries) == 3
    summary_text = [" ".join(re.sub(r"<[^>]+>", "", item).split()) for item in summaries]
    assert summary_text == [
        "Not interpretable (3) depth 2 · qc 1",
        "No reference (2) cohort too small 1 · no library entry 1",
        "In range (1)",
    ]
    assert "Missing library entry fixture" in dom


@needs_chrome
def test_sample_switcher_round_trips_through_home_deep_link(tmp_path) -> None:
    second_id = "aaaaaaaaaaaa"
    first = _sample()
    second = copy.deepcopy(first)
    second["id12"] = second_id
    second["source_id"] = second_id + "a" * 52
    samples = {ID12: first, second_id: second}
    observed = {}
    with _server(tmp_path, first) as server:
        listing = server.app.api.samples()
        second_row = copy.deepcopy(listing["samples"][0])
        second_row["id12"] = second_id
        server.app.api.samples = lambda: {**listing, "samples": [listing["samples"][0], second_row]}
        server.app.api.sample = lambda id12: copy.deepcopy(samples[id12]) if id12 in samples else {"found": False}
        profile = tmp_path / "switcher"

        def switch(_command, evaluate) -> None:
            assert evaluate("document.querySelector('#main h1')?.textContent.trim()") == f"Traceback Lab — {ID12}"
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
            profile, ok=lambda dom: 'data-ready="1"' in dom,
            interact=switch,
        )
        deep_link = _dom(server, tmp_path / "deep-link", route=f"/?sample={second_id}",
                         want=lambda dom: f"Traceback Lab — {second_id}" in dom)
    assert observed == {"hash": f"#/?sample={second_id}", "selected": second_id,
                        "heading": f"Traceback Lab — {second_id}"}
    assert f"Traceback Lab — {second_id}" in deep_link
    option = re.search(rf'<option[^>]*value="{second_id}"[^>]*>', deep_link)
    assert option is not None and "selected" in option.group()


@needs_chrome
def test_loading_status_clears_when_the_first_status_poll_fails(tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        def unavailable_status():
            raise OSError("status temporarily unavailable")

        server.app.api.status = unavailable_status
        dom = _dom(server, tmp_path / "browser", route="/",
                   want=lambda page: f"Traceback Lab — {ID12}" in page)
    status = re.search(r'<p(?=[^>]*id="status")[^>]*>(.*?)</p>', dom, re.DOTALL)
    assert status is not None and re.sub(r"<[^>]+>", "", status.group(1)).strip() == ""
    assert "Loading Lab results…" not in dom
