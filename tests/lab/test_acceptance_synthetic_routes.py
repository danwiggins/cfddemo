"""AC7/11 browser contracts backed by synthetic API responses, without alignment."""

from __future__ import annotations

import copy
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome
from traceback_lab.interpretation.engine import classify, load_library
from traceback_lab.registry import load_registry
from traceback_lab.store import LabStore
from traceback_lab.web.api import _catalog
from traceback_lab.web.server import RunningLabServer

ID12 = "3f9a1c2b7e04"
LIBRARY = load_library()
ALGORITHM_VERSIONS = {item.id: item.version for item in load_registry()}
REGISTRY = [item.id for item in load_registry() if item.id not in LIBRARY.hidden_algorithms]
LICENSE_IDS = {
    license_id
    for (algorithm_id, _), entry in LIBRARY.entries.items()
    if algorithm_id in REGISTRY
    for license_id in entry.get("license_ids") or []
}
DISCLOSURE_ROUTES = ("home", "findings", "stability", "evidence", "methods")
CONTEXT_LICENSE_ROUTES = (
    ("signals", {"gpl-copyleft"}),
    ("body-map", {"loyfer-atlas-terms-unknown", "yissum-uxm"}),
    ("agreement", {"gpl-copyleft"}),
)
SAMPLE_ROUTES = (
    "findings", "trust-qc", "stability", "signals", "compare",
    "body-map", "agreement", "evidence", "methods",
)


class _LicenseNodes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.callouts: list[str] = []
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        props = dict(attrs)
        classes = set((props.get("class") or "").split())
        if "license-callout" in classes:
            identity = props.get("id") or ""
            assert identity.startswith("license-"), identity
            self.callouts.append(identity.removeprefix("license-"))
        if "license-pill" in classes:
            target = props.get("aria-describedby") or ""
            assert target.startswith("license-"), target
            self.refs.append(target.removeprefix("license-"))


def _license_nodes(dom: str) -> _LicenseNodes:
    parsed = _LicenseNodes()
    parsed.feed(dom)
    return parsed


def test_license_parser_accepts_digit_ids_and_any_attribute_order() -> None:
    parsed = _license_nodes(
        '<div id="license-gpl-3" data-x="1" class="panel license-callout"></div>'
        '<button aria-describedby="license-gpl-3" class="license-pill control"></button>'
    )
    assert parsed.callouts == ["gpl-3"] and parsed.refs == ["gpl-3"]


def _sample() -> dict:
    """Show every registered metric and license, including ones a short run skips."""
    algorithms = []
    for algorithm_id in REGISTRY:
        headline = LIBRARY.headline(algorithm_id)
        assert headline is not None
        metrics = []
        for (alg, metric_id), entry in LIBRARY.entries.items():
            if alg != algorithm_id:
                continue
            metrics.append({
                "metric_id": metric_id,
                "label": entry["label"],
                "value": 1.0,
                "unit": entry.get("unit"),
                "state": "above_typical",
                "reason": None,
                "caption": "Synthetic range fixture",
                "reference_kind": "published_range",
                "interval": [0.1, 0.2],
                "exploratory_n": None,
                "percentile": None,
                "severity": 1,
                "tier": entry.tier,
                "entry": dict(entry.raw),
                "ci": None,
                "ci_level": None,
            })
        algorithms.append({
            "algorithm_id": algorithm_id,
            "algorithm_version": ALGORITHM_VERSIONS[algorithm_id],
            "hidden": False,
            "status": "ok",
            "skip_reason": None,
            "skip_message": None,
            "failure": None,
            "duration_s": 0.0,
            "tier": headline.tier,
            "min_depth_x": headline.get("min_depth_x"),
            "headline_metric": headline.metric_id,
            "panels": [],
            "metrics": metrics,
            "provenance": {
                "tool": "synthetic-fixture", "tool_version": "1", "parameters_sha256": "a" * 64,
                "input_id": "b" * 64, "source_id": "c" * 64,
                "attempt_id": "a-20261016T000000000000-abcd1234",
                "reference_id": "synthetic", "timestamp_utc": "2026-10-16T00:00:00Z",
            },
        })
    catalog = _catalog(LIBRARY, REGISTRY, algorithms)
    licensed = [row for row in catalog if row["license_ids"]]
    defended_rows = [
        {"label": row["label"], "value": 1.0, "unit": "", "state": "above_typical",
         "tier": row["tier"], "evidence": "Synthetic fixture", "license_ids": row["license_ids"]}
        for row in licensed
    ]
    return {
        "found": True, "id12": ID12, "source_id": "c" * 64,
        "full_input_id": "b" * 64, "example": False,
        "run_utc": "2026-10-16T00:00:00Z", "depth": {"approx_depth_x": 1.0},
        "excluded_chunks": [], "interpretation_version": LIBRARY.version,
        "cohort_version": None, "cohort_n": 0,
        "algorithms": algorithms, "catalog": catalog, "unreadable_results": [],
        "licenses": [{"license_id": key, **LIBRARY.licenses[key]} for key in sorted(LICENSE_IDS)],
        "curve": {"downsampled": True, "run_depth_x": 1.0, "fractions": [1.0], "rows": []},
        "stability": [
            {"algorithm_id": row["algorithm_id"], "metric_id": LIBRARY.headline(row["algorithm_id"]).metric_id,
             "label": row["label"], "verdict": "synthetic", "points": [], "split": None}
            for row in catalog if row["license_ids"]
        ],
        "repeatability": None, "synthetic": [],
        "defended": {"rows": defended_rows, "none_reasons": {
            "usable_but_speculative": 0, "unstable_at_half": 0,
            "citation_unverified": 0, "not_interpretable": 0,
        }},
        "headline": {"sentence": "Synthetic disclosure fixture.", "unlock": None},
        "excluded": [],
        "library_health": {"version": LIBRARY.version, "no_library_entry": 0,
                           "unverified_citations": 0, "error": None, "loaded_at": "13:40"},
    }


def _server(tmp_path, sample):
    server = RunningLabServer(LabStore(tmp_path / "root"))
    server.app.api.status = lambda: {"generation": 1, "active": None, "interrupted": None,
                                     "library": sample["library_health"]}
    server.app.api.samples = lambda: {"samples": [{
        "id12": ID12, "run_utc": sample["run_utc"], "depth_x": 1.0,
        "example": sample["example"], "counts": {"ok": len(REGISTRY), "skipped": 0, "failed": 0},
        "outside_reference": len(REGISTRY), "controls": {"spike": None, "split": False},
        "unreadable_results": sample["unreadable_results"],
    }], "root_has_runs": True}
    server.app.api.sample = lambda id12: copy.deepcopy(sample) if id12 == ID12 else {"found": False, "candidates": []}
    server.app.api.cohorts = lambda: {"cohorts": [], "unavailable": []}
    return server


@needs_chrome
def test_landing_defaults_to_completed_run_with_newer_active_sample(tmp_path) -> None:
    """The active sample stays selectable without displacing a completed run."""

    completed = _sample()
    active_id12 = "bbbbbbbbbbbb"
    active = copy.deepcopy(completed)
    active["id12"] = active_id12
    active["source_id"] = "d" * 64
    active["headline"] = {"sentence": "Active fixture has no completed results.", "unlock": None}
    active["algorithms"] = []
    active["catalog"] = []
    active["defended"]["rows"] = []
    active["curve"]["rows"] = []
    active["stability"] = []
    with _server(tmp_path, completed) as server:
        completed_row = server.app.api.samples()["samples"][0]
        active_row = {**completed_row, "id12": active_id12, "run_utc": "2026-10-18T00:00:00Z",
                      "counts": {"ok": 0, "skipped": 0, "failed": 0}, "outside_reference": 0}
        server.app.api.sample = lambda id12: copy.deepcopy({ID12: completed, active_id12: active}.get(
            id12, {"found": False, "candidates": []}
        ))
        server.app.api.status = lambda: {
            "generation": 2,
            "active": {"run_id": "r-20261018-0000-abcd", "command": "run", "step": "bam-qc",
                       "started_utc": "2026-10-18T00:00:00Z", "source_id": active["source_id"],
                       "input_id": "e" * 64},
            "interrupted": None,
            "library": completed["library_health"],
        }
        for label, default_id12, route, expected, unavailable in (
            ("completed", ID12, "/", ID12, False),
            ("explicit-active", ID12, f"/?sample={active_id12}", active_id12, False),
            ("no-completed", None, "/", None, False),
            ("unavailable", None, "/", None, True),
        ):
            server.app.api.samples = lambda default_id12=default_id12: {
                "samples": [active_row, completed_row], "default_sample_id12": default_id12,
                "completed_input_unavailable": unavailable,
                "root_has_runs": True,
            }
            dom = _dom(server, tmp_path / label, route=route,
                       want=lambda page: ('Traceback Lab —' in page or 'No completed Lab run yet' in page
                                          or 'Latest completed input unavailable' in page))
            heading = re.search(r'<h1 class="visually-hidden"[^>]*>Traceback Lab — ([0-9a-f]+)</h1>', dom)
            if expected is None:
                assert heading is None and ("Latest completed input unavailable" if unavailable
                                            else "No completed Lab run yet") in dom
                assert f'href="#/?sample={active_id12}"' in dom
                assert "Select a sample" in dom
            else:
                assert heading is not None and heading.group(1) == expected, label
                assert f'href="#/sample/{active_id12}/findings"' in dom
            if label == "explicit-active":
                assert "selected sample view" in dom and "A Lab run is in progress" in dom


@needs_chrome
def test_landing_actions_keep_synthetic_status_on_each_destination(tmp_path) -> None:
    sample = _sample()
    sample["example"] = True
    observed = []
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "scientist-landing"

        def visit(command, evaluate) -> None:
            assert evaluate("document.getElementById('sample-context').closest('header') !== null")
            assert evaluate("document.getElementById('sample-context').textContent").count("EXAMPLE (synthetic)") == 1
            assert evaluate("document.querySelector('.landing-intro').textContent").count("EXAMPLE (synthetic)") == 1
            actions = (
                ("Explore signals", "signals", "Signals"),
                ("Check reliability", "trust-qc", "Trust/QC"),
                ("Method readiness", "methods", "Methods"),
            )
            for label, lens, heading in actions:
                href = evaluate(f"[...document.querySelectorAll('.landing-actions a')].find(a => a.textContent === {json.dumps(label)})?.getAttribute('href')")
                assert href == f"#/sample/{ID12}/{lens}"
                evaluate(f"[...document.querySelectorAll('.landing-actions a')].find(a => a.textContent === {json.dumps(label)}).click()")
                for _ in range(100):
                    if evaluate("location.hash") == href and evaluate("document.querySelector('#main h1')?.textContent.trim()") == heading:
                        break
                    time.sleep(0.05)
                observed.append((label, evaluate("location.hash"), evaluate("document.getElementById('sample-context').textContent")))
                evaluate("location.hash = '#/'")
                for _ in range(100):
                    if evaluate("document.querySelector('.landing-actions') !== null"):
                        break
                    time.sleep(0.05)
            for lens in SAMPLE_ROUTES:
                route = f"#/sample/{ID12}/{lens}"
                evaluate(f"location.hash = {json.dumps(route)}")
                for _ in range(100):
                    if evaluate("window.TracebackLab.state.renderedHash") == route:
                        break
                    time.sleep(0.05)
                assert evaluate("window.TracebackLab.state.renderedHash") == route
                assert "EXAMPLE (synthetic)" in evaluate("document.getElementById('sample-context').textContent")
            command("Emulation.setEmulatedMedia", {"media": "print"})
            assert evaluate("getComputedStyle(document.getElementById('sample-context')).display") != "none"
            assert "EXAMPLE (synthetic)" in evaluate("document.getElementById('sample-context').textContent")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + "&route=/"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="landing-actions"' in dom,
            interact=visit,
        )
    assert [route for _, route, _ in observed] == [f"#/sample/{ID12}/{lens}" for lens in ("signals", "trust-qc", "methods")]
    assert all("EXAMPLE (synthetic)" in context for _, _, context in observed)


@needs_chrome
def test_landing_names_result_selection_problems_beside_recorded_counts(tmp_path) -> None:
    sample = _sample()
    sample["unreadable_results"] = ["bam-qc"]
    sample["ambiguous_results"] = ["fragment-size"]
    sample["malformed_result_selections"] = True
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route="/",
                   want=lambda page: "Result selection problems" in page)
    assert f"{len(sample['algorithms'])} visible entries" in dom
    assert "1 unreadable" in dom and "1 ambiguous" in dom
    assert "malformed selection present" in dom


def test_disclosure_fixture_is_nonvacuous_and_registry_complete() -> None:
    sample = _sample()
    assert {a["algorithm_id"] for a in sample["algorithms"]} == set(REGISTRY)
    assert LICENSE_IDS == {lic["license_id"] for lic in sample["licenses"]}
    assert LICENSE_IDS == {license_id for row in sample["catalog"] for license_id in row["license_ids"]}
    assert LICENSE_IDS == {license_id for row in sample["defended"]["rows"] for license_id in row["license_ids"]}


def _low_depth_liver_sample() -> dict:
    sample = _sample()
    sample["depth"]["approx_depth_x"] = 0.2
    liver = next(metric for algorithm in sample["algorithms"] if algorithm["algorithm_id"] == "cell-origin"
                 for metric in algorithm["metrics"] if metric["metric_id"] == "fraction_liver_hep")
    result = classify(0.031, LIBRARY.entry("cell-origin", "fraction_liver_hep"), depth_x=0.2)
    liver.update(value=0.031, state=result.state, reason=result.reason, caption=result.caption,
                 reference_kind=result.reference_kind, interval=result.interval)
    return sample


def test_actual_liver_metric_is_depth_gated_at_point_two_x_with_estimate_retained() -> None:
    sample = _low_depth_liver_sample()
    liver = next(metric for algorithm in sample["algorithms"] if algorithm["algorithm_id"] == "cell-origin"
                 for metric in algorithm["metrics"] if metric["metric_id"] == "fraction_liver_hep")
    assert (liver["state"], liver["reason"], liver["value"]) == ("not_interpretable", "depth", 0.031)
    assert "Needs 5x; this input is 0.2x. The estimate stays visible." == liver["caption"]


@needs_chrome
def test_low_depth_liver_metric_keeps_numeric_estimate_in_findings(tmp_path) -> None:
    with _server(tmp_path, _low_depth_liver_sample()) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings",
                   want=lambda d: 'data-ready="1"' in d and "Not interpretable" in d)
    row = re.search(r"<tr><td>Liver-Hep share of cfDNA.*?</tr>", dom, re.DOTALL)
    assert row is not None, "the liver metric must appear in the not-interpretable table"
    assert "0.031" in row.group() and "depth" in row.group() and "5x" in row.group()


@needs_chrome
@pytest.mark.parametrize("route", DISCLOSURE_ROUTES)
def test_every_registry_license_has_one_callout_on_disclosure_route(route, tmp_path) -> None:
    sample = _sample()
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda d: "Synthetic disclosure fixture." in d if route == "home" else "data-ready" in d)
    parsed = _license_nodes(dom)
    callouts, refs = parsed.callouts, parsed.refs
    assert set(callouts) == LICENSE_IDS, (route, set(callouts), LICENSE_IDS)
    assert len(callouts) == len(LICENSE_IDS), route
    assert set(refs) <= LICENSE_IDS, route
    if route != "stability":
        assert refs, route
    if route == "home":
        assert dom.index("License callouts") < dom.index('id="license-')
    if route == "findings":
        cards = re.findall(r'<article class="finding-card">(.*?)</article>', dom, re.DOTALL)
        assert len(cards) == sum(len(a["metrics"]) for a in sample["algorithms"])
        for card in cards:
            assert card.count('class="tier ') == 1
            assert "Performance:" in card
            assert '<details class="provenance">' in card  # collapsed by default
        for tier in ("PROVEN", "NEWER", "SPECULATIVE"):
            assert f">{tier}<" in dom


@needs_chrome
@pytest.mark.parametrize("route,expected", CONTEXT_LICENSE_ROUTES)
def test_context_routes_show_full_license_callouts(route, expected, tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        if route == "signals":
            kind, source = server.app.assets["/assets/lab.js"]
            server.app.assets["/assets/lab.js"] = (kind, source + b"""
              (() => {
                const timer = setInterval(() => {
                  const tab = [...document.querySelectorAll('.subtab')]
                    .find((item) => item.textContent === 'Copy number');
                  if (tab) { tab.click(); clearInterval(timer); }
                }, 25);
              })();
            """)
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/{route}",
                   want=lambda d: all(f'id="license-{identity}"' in d for identity in expected))
    parsed = _license_nodes(dom)
    assert expected <= set(parsed.callouts), (route, parsed.callouts)
    assert len(parsed.callouts) == len(set(parsed.callouts)), route
    assert set(parsed.refs) <= set(parsed.callouts), (route, parsed.refs)


@needs_chrome
def test_every_registered_metric_flag_is_on_its_finding_card(tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/findings")
    cards = re.findall(r'<article class="finding-card">(.*?)</article>', dom, re.DOTALL)
    assert len(cards) == sum(len(a["metrics"]) for a in sample["algorithms"])
    for algorithm in sample["algorithms"]:
        for metric in algorithm["metrics"]:
            marker = f'id="card-{algorithm["algorithm_id"]}-{metric["metric_id"]}"'
            card = next((text for text in cards if marker in text), None)
            assert card is not None, marker
            for flag in metric["entry"].get("flags") or []:
                assert f"FLAG: {flag}" in card, (marker, flag)


FLAG_SURFACES = [
    ("home", "PII", None),
    ("findings", "CLIA", None),
    ("signals", "LEGAL", None),
    ("signals", "FDA", "copy-number"),
    ("body-map", "LEGAL", None),
    ("compare", "CLIA", None),
    ("methods", "PII", None),
]


@needs_chrome
@pytest.mark.parametrize("route,flag,only_algorithm", FLAG_SURFACES)
def test_required_flag_surface_has_visible_pill(route, flag, only_algorithm, tmp_path) -> None:
    sample = _sample()
    if only_algorithm:
        sample["algorithms"] = [a for a in sample["algorithms"] if a["algorithm_id"] == only_algorithm]
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda d: f"FLAG: {flag}" in d)
    assert re.search(rf'<[^>]*class="flag-pill"[^>]*>FLAG: {flag}', dom)


@needs_chrome
def test_full_callout_computed_type_and_border(tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        probe = b"""
        (() => {
          const timer = setInterval(() => {
            const block = document.querySelector('.license-callout');
            if (!block) return;
            const title = getComputedStyle(block.querySelector('h3'));
            const body = getComputedStyle(block.querySelector('p'));
            const border = getComputedStyle(block);
            document.body.dataset.licenseTitleSize = title.fontSize;
            document.body.dataset.licenseTitleWeight = title.fontWeight;
            document.body.dataset.licenseBodySize = body.fontSize;
            document.body.dataset.licenseBodyWeight = body.fontWeight;
            document.body.dataset.licenseBorder = border.borderLeftWidth;
            document.body.dataset.licenseColor = border.borderLeftColor;
            document.body.dataset.licenseToken = getComputedStyle(document.documentElement).getPropertyValue('--license').trim();
            const expectedColor = getComputedStyle(block).borderLeftColor;
            const failures = [];
            const blocks = [...document.querySelectorAll('.license-callout')];
            for (const item of blocks) {
              const heading = item.querySelector('h3');
              const paragraph = item.querySelector('p');
              if (!heading || !paragraph) { failures.push(item.id); continue; }
              const headingStyle = getComputedStyle(heading);
              const paragraphStyle = getComputedStyle(paragraph);
              const itemStyle = getComputedStyle(item);
              if (parseFloat(headingStyle.fontSize) < 20 || parseFloat(paragraphStyle.fontSize) < 20
                  || parseInt(headingStyle.fontWeight, 10) < 700 || parseInt(paragraphStyle.fontWeight, 10) < 700
                  || parseFloat(itemStyle.borderLeftWidth) < 3 || itemStyle.borderLeftColor !== expectedColor)
                failures.push(item.id);
            }
            document.body.dataset.licenseCount = String(blocks.length);
            document.body.dataset.licenseFailures = failures.join(',');
            clearInterval(timer);
          }, 20);
        })();
        """
        server.app.assets["/assets/lab.js"] = (kind, source + probe)
        dom = _dom(server, tmp_path / "browser", route="/",
                   want=lambda d: "data-license-title-size" in d)
    body = re.search(r"<body([^>]*)>", dom).group(1)
    attrs = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', body))
    assert int(attrs["license-count"]) == len(LICENSE_IDS)
    assert attrs["license-failures"] == ""
    assert float(attrs["license-title-size"].removesuffix("px")) >= 20
    assert float(attrs["license-body-size"].removesuffix("px")) >= 20
    assert int(attrs["license-title-weight"]) >= 700 and int(attrs["license-body-weight"]) >= 700
    assert float(attrs["license-border"].removesuffix("px")) >= 3
    token = attrs["license-token"].lstrip("#")
    assert len(token) == 6
    expected_color = f"rgb({int(token[:2], 16)}, {int(token[2:4], 16)}, {int(token[4:], 16)})"
    assert attrs["license-color"] == expected_color


STATE_CASES = [
    ("partial-input", "home", "Provisional: this input excludes 2 truncated chunks"),
    ("partial-input", "findings", "Provisional: this input excludes 2 truncated chunks"),
    ("partial-input", "trust-qc", "Provisional: this input excludes 2 truncated chunks"),
    ("partial-exit-8", "findings", "bam-qc failed: synthetic failure."),
    ("partial-exit-8", "trust-qc", "Last 50 stderr lines (scrubbed)"),
    ("no-downsample", "home", "Defended results need the depth curve"),
    ("no-downsample", "stability", "This run used --no-downsample"),
    ("no-simulate", "home", f"No synthetic control yet. Run traceback lab run {ID12}."),
    ("no-simulate", "stability", "Split-half markers absent"),
    ("none-defended", "home", "No result is defended at"),
    ("pre-cohort", "findings", "eligible cohort n = 0 of 10 needed"),
    ("invalid-library", "trust-qc", "Interpretation library on disk is invalid"),
    ("unknown-sample", "findings", "No Lab sample matches aaaaaaaaaaaa."),
    ("chart-failed", "signals", "Chart could not be drawn (synthetic renderer failure)"),
]


@needs_chrome
@pytest.mark.parametrize("state,route,copy_text", STATE_CASES)
def test_synthetic_state_copy_on_surface(state, route, copy_text, tmp_path) -> None:
    sample = _sample()
    if state == "partial-input":
        sample["excluded_chunks"] = [0, 1]
    elif state == "partial-exit-8":
        failed = sample["algorithms"][0]
        failed["status"] = "failed"
        failed["failure"] = {"first_line": "synthetic failure", "stderr_tail": ["synthetic failure"]}
    elif state == "no-downsample":
        sample["curve"]["downsampled"] = False
    elif state == "none-defended":
        sample["defended"]["rows"] = []
    elif state == "pre-cohort":
        sample["algorithms"][0]["metrics"][0]["state"] = "no_reference"
        sample["algorithms"][0]["metrics"][0]["reference_kind"] = "internal_cohort"
        sample["algorithms"][0]["metrics"][0]["eligible_n"] = 0
    elif state == "invalid-library":
        sample["library_health"]["error"] = "line 212"
    target = "aaaaaaaaaaaa" if state == "unknown-sample" else ID12
    path = None if route == "home" else f"/sample/{target}/{route}"
    with _server(tmp_path, sample) as server:
        if state == "chart-failed":
            counts = [0] * 1000
            for span, count in zip((49, 50, 167, 700, 701), (1, 2, 3, 4, 5), strict=True):
                counts[span - 1] = count
            density = json.dumps({"span_bp": list(range(1, 1001)), "count": counts}).encode()
            server.app.api.artifact = lambda id12, label, algorithm_id, panel_id: (
                density if (id12, label, algorithm_id, panel_id) ==
                (ID12, "full", "fragment-size", "size-density") else None
            )
            kind, _ = server.app.assets["/assets/vendor/echarts.min.js"]
            server.app.assets["/assets/vendor/echarts.min.js"] = (
                kind, b"window.echarts={init(){throw new Error('synthetic renderer failure')}};",
            )
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda d: copy_text in d if state == "chart-failed" else 'data-ready="1"' in d)
    assert copy_text in dom, (state, route)
    if state == "chart-failed":
        assert "Data table" in dom and "167" in dom
    if state == "partial-exit-8" and route == "findings":
        assert "Details in Trust/QC" in dom


@needs_chrome
@pytest.mark.parametrize("route", SAMPLE_ROUTES)
def test_unknown_sample_copy_on_every_sample_route(route, tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/aaaaaaaaaaaa/{route}")
    assert "No Lab sample matches aaaaaaaaaaaa." in dom
    assert "Back to Lab home" in dom


STATUS_CASES = [
    ("run-in-progress", "A Lab run is in progress (step: bam-qc"),
    ("interrupted", "The last run (r-20261015-0101-abcd) stopped during bam-qc."),
    ("status-unavailable", "The last run (r-20261015-0101-abcd) status is unavailable (log unreadable, unsafe, or over 1 MiB)."),
    ("run-finished", "New results are available."),
    ("results-changed", "Results changed on disk since this page loaded."),
    ("disconnected", "Lab server not responding since"),
]


def _run_marker() -> dict:
    return {"run_id": "r-20261015-0101-abcd", "command": "run", "step": "bam-qc",
            "source_id": "c" * 64, "input_id": "b" * 64}


@needs_chrome
@pytest.mark.parametrize("state,copy_text", STATUS_CASES)
def test_synthetic_status_copy_in_live_region(state, copy_text, tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        assert b"const POLL_MS = 15000;" in source
        server.app.assets["/assets/lab.js"] = (kind, source.replace(b"const POLL_MS = 15000;", b"const POLL_MS = 500;"))
        calls = 0

        def status():
            nonlocal calls
            calls += 1
            baseline = {"generation": 1, "active": None, "interrupted": None,
                        "library": sample["library_health"]}
            if state == "run-in-progress":
                baseline["active"] = {**_run_marker(), "started_utc": "2026-10-16T00:00:00Z"}
            elif state == "interrupted":
                baseline["interrupted"] = _run_marker()
            elif state == "status-unavailable":
                baseline["run_log_unavailable"] = {"run_id": _run_marker()["run_id"],
                                                   "reason": "unreadable_or_over_1mib"}
            elif state == "run-finished":
                if calls == 1:
                    baseline["active"] = {**_run_marker(), "started_utc": "2026-10-16T00:00:00Z"}
                else:
                    baseline["generation"] = 2
            elif state == "results-changed":
                baseline["generation"] = 1 if calls == 1 else 2
                if calls > 1:
                    sample["algorithms"][0]["duration_s"] = 99.9
            elif state == "disconnected" and calls > 1:
                raise OSError("synthetic poll failure")
            return baseline

        server.app.api.status = status
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/trust-qc",
                   want=lambda d: copy_text in d)
    status_region = re.search(
        r'<p(?=[^>]*\bid="status")(?=[^>]*\brole="status")(?=[^>]*\baria-live="polite")[^>]*>(.*?)</p>', dom, re.S,
    )
    assert status_region is not None and copy_text in status_region.group(1)
    if state == "run-in-progress":
        run = dom.split("<h2>Run</h2>", 1)[1].split("</table>", 1)[0]
        assert re.search(r'<th scope="row">bam-qc</th><td[^>]*>ok(?:\s|<)', run)
        assert 'latest run in progress' in run
        assert '0.0 s (selected result)' in run
    if state == "interrupted":
        run = dom.split("<h2>Run</h2>", 1)[1].split("</table>", 1)[0]
        assert re.search(r'<th scope="row">bam-qc</th><td[^>]*>ok(?:\s|<)', run)
        assert 'latest run interrupted' in run
    if state == "status-unavailable":
        run = dom.split("<h2>Run</h2>", 1)[1].split("</table>", 1)[0]
        assert re.search(r'<th scope="row">bam-qc</th><td[^>]*>ok(?:\s|<)', run)
        assert 'latest run interrupted' not in run
        assert 'stopped during bam-qc' not in dom
    if state in ("run-finished", "results-changed"):
        assert ">Reload</button>" in dom
    if state == "results-changed":
        assert "0.0 s" in dom
        assert "99.9 s" not in dom
    if state == "disconnected":
        assert 'data-disconnected="1"' in dom
        assert re.search(r'<body[^>]*class="[^"]*\bdimmed\b', dom)
        assert re.search(r'<select[^>]*id="sample-switcher"[^>]*disabled', dom)
        assert 'aria-disabled="true"' in dom


@needs_chrome
def test_loading_copy_while_synthetic_listing_is_in_flight(tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        listing = server.app.api.samples

        def delayed_listing():
            time.sleep(5)
            return listing()

        server.app.api.samples = delayed_listing
        profile = Path(tmp_path / "browser-profile")
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link()],
            profile, ok=lambda d: "Loading Lab results…" in d, timeout=12,
        )
    assert "Loading Lab results…" in dom and "Loading…" in dom


@needs_chrome
def test_empty_copy_and_complete_example_commands(tmp_path) -> None:
    with _server(tmp_path, _sample()) as server:
        server.app.api.samples = lambda: {"samples": [], "root_has_runs": False}
        server.app.api.sample = lambda _id12: {"found": False, "candidates": []}
        dom = _dom(server, tmp_path / "home", route="/",
                   want=lambda page: page.count("FLAG: LEGAL — verify") == 4)
        unknown = _dom(server, tmp_path / "unknown", route=f"/sample/{ID12}/findings",
                       want=lambda page: "Unknown sample" in page)
        about = _dom(server, tmp_path / "clia", route="/about/clia",
                     want=lambda page: "What CLIA is." in page)
    assert '<section class="clia-panel"' in dom
    short = re.search(r'<div class="clia-short">(.*?)</div>', dom, re.DOTALL)
    full = re.search(r'<div class="clia-full"[^>]*>(.*?)</div>', dom, re.DOTALL)
    assert short is not None and full is not None
    assert short.group(1).count("FLAG: LEGAL — verify") == 2
    assert full.group(1).count("FLAG: LEGAL — verify") == 2
    assert "Loading…" not in short.group(1) and "Loading…" not in full.group(1)
    assert "What CLIA means for this demo" in dom
    assert "No Lab runs under this server's ROOT" in dom
    assert "traceback lab doctor" in dom and "traceback lab run --example" in dom
    assert "traceback lab run &lt;chunk dir&gt;" in dom
    assert f"No Lab sample matches {ID12}" in unknown and "No Lab runs under" not in unknown
    assert "What CLIA is." in about and "42 CFR 493.3(b)(2)" in about
    assert "No Lab runs under" not in about


@needs_chrome
@pytest.mark.parametrize("route,copy_text", [
    ("home", f"result unreadable (sample {ID12})"),
    ("findings", f"bam-qc: result unreadable (sample {ID12})"),
    ("trust-qc", "result unreadable"),
])
def test_unreadable_result_copy_on_each_required_surface(route, copy_text, tmp_path) -> None:
    sample = _sample()
    sample["unreadable_results"] = ["bam-qc"]
    sample["algorithms"] = [a for a in sample["algorithms"] if a["algorithm_id"] != "bam-qc"]
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        if route == "trust-qc":
            server.app.api.status = lambda: {
                "generation": 1, "active": {
                    "run_id": "r-20261015-0101-abcd", "command": "run", "step": "bam-qc",
                    "source_id": sample["source_id"], "input_id": sample["full_input_id"],
                    "started_utc": "2026-10-16T00:00:00Z",
                }, "interrupted": None, "run_log_unavailable": None,
                "library": sample["library_health"],
            }
        dom = _dom(server, tmp_path / "browser", route=path)
    assert copy_text in dom
    if route == "trust-qc":
        assert "no result was published for this step" not in dom


@needs_chrome
def test_stale_run_log_link_refuses_to_mix_new_attempt_with_loaded_results(tmp_path) -> None:
    sample = _sample()
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"
        dom = _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/trust-qc"],
            profile, ok=lambda d: 'data-ready="1"' in d and "Run log line →" in d,
            action="""(() => {
              const state = window.TracebackLab.state;
              state.generationAtLoad = 1;
              state.lastStatus ||= {generation: 1};
              state.lastStatus.generation = 2;
              const button = [...document.querySelectorAll('main button')]
                .find((item) => item.textContent.includes('Run log line →'));
              if (!button) return false;
              button.click();
              return true;
            })()""",
        )
    assert "Results changed on disk; reload before viewing this run log line." in dom


@needs_chrome
@pytest.mark.parametrize("scope", ("matching", "other-sample", "unscoped"))
def test_interrupted_note_is_scoped_and_preserves_selected_result(tmp_path, scope) -> None:
    sample = _sample()
    marker = _run_marker()
    if scope == "other-sample":
        marker["source_id"] = "d" * 64
    elif scope == "unscoped":
        marker.pop("source_id")
        marker.pop("input_id")
    with _server(tmp_path, sample) as server:
        server.app.api.status = lambda: {
            "generation": 1, "active": None,
            "interrupted": marker,
            "library": sample["library_health"],
        }
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/trust-qc")
    run = dom.split("<h2>Run</h2>", 1)[1].split("</table>", 1)[0]
    assert re.search(r'<th scope="row">bam-qc</th><td[^>]*>ok(?:\s|<)', run)
    assert ("latest run interrupted" in run) == (scope == "matching")
