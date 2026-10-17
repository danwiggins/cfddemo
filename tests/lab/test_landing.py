"""S6c: defended-result rule, headline sentence, and the landing / Findings / Stability DOM."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.lab.test_lab_server import _chrome
from traceback_lab.defended import Candidate, defended, exclusion, headline
from traceback_lab.store import LabStore
from traceback_lab.web.server import RunningLabServer, clia_copy
from traceback_runner import cli as signed_cli


def _c(algorithm_id="bam-qc", **overrides) -> Candidate:
    base = dict(
        algorithm_id=algorithm_id, label=algorithm_id, value=1.0, unit=None, tier="NEWER", status="ok",
        state="no_reference", usable_at_half=True, split_stable=True, split_relative_diff=0.01,
        citation_verified=True, control_recovered=None, order=0,
    )
    base.update(overrides)
    return Candidate(**base)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({}, None),
        ({"state": "in_range"}, None),
        ({"status": "failed"}, "not_ok"),
        ({"state": "not_interpretable"}, "not_interpretable"),
        ({"usable_at_half": False}, "unstable_at_half"),
        ({"split_stable": False}, "split_unstable"),
        ({"split_stable": None}, "split_unstable"),
        ({"tier": "SPECULATIVE"}, "speculative"),
        ({"citation_verified": False}, "citation_unverified"),
    ],
)
def test_exclusion_rules(overrides, reason) -> None:
    assert exclusion(_c(**overrides)) == reason


def test_control_rule_applies_to_copy_number_only() -> None:
    assert exclusion(_c("copy-number", control_recovered=False)) == "control_not_recovered"
    assert exclusion(_c("copy-number", control_recovered=None)) == "control_not_recovered"
    assert exclusion(_c("copy-number", control_recovered=True)) is None
    assert exclusion(_c("bam-qc", control_recovered=None)) is None


def test_ranking_and_the_cap_of_five() -> None:
    candidates = [
        _c("a", tier="NEWER", state="in_range", split_relative_diff=0.01, order=0),
        _c("b", tier="PROVEN", state="no_reference", split_relative_diff=0.05, order=1),
        _c("c", tier="PROVEN", state="in_range", split_relative_diff=0.09, order=2),
        _c("d", tier="PROVEN", state="in_range", split_relative_diff=0.02, order=3),
        _c("e", tier="NEWER", state="no_reference", split_relative_diff=0.00, order=4),
        _c("f", tier="NEWER", state="in_range", split_relative_diff=0.01, order=5),
        _c("g", tier="SPECULATIVE", order=6),
    ]
    block = defended(candidates)
    assert [row["algorithm_id"] for row in block["rows"]] == ["d", "c", "b", "a", "f"]
    assert block["qualified"] == 6 and len(block["rows"]) == 5


def test_none_defended_counts_reasons() -> None:
    block = defended([
        _c("a", tier="SPECULATIVE"),
        _c("b", usable_at_half=False),
        _c("c", citation_verified=False),
        _c("d", state="not_interpretable"),
    ])
    assert block["rows"] == []
    assert block["none_reasons"] == {
        "usable_but_speculative": 1, "unstable_at_half": 1, "citation_unverified": 1,
        "not_interpretable": 1, "control_not_recovered": 0,
    }


def test_headline_sentence_and_unlock_clause() -> None:
    result = headline(0.23, 3, 16, 2, {"x": 0.5, "y": 0.8, "z": 1.0, "w": 5.0, "v": None})
    assert result["sentence"] == "At 0.23x, 3 of 16 algorithms are usable and 2 results are defended."
    assert result["unlock"]["text"] == "Literature minimums suggest 0.5x would add 1 and 1x would add 2 more."
    assert result["unlock"]["note"] == "literature, not measured"
    assert headline(0.23, 3, 16, 0, {})["sentence"].endswith("and no result is defended at this depth.")
    assert headline(0.23, 3, 16, 0, {}, downsampled=False)["sentence"] == "At 0.23x, 3 of 16 algorithms ran; stability not measured."
    assert "(1 algorithm failed: see Trust/QC)" in headline(0.23, 3, 16, 1, {}, failed=1)["sentence"]


def test_clia_copy_is_one_source_with_both_verify_flags() -> None:
    copy = clia_copy()
    words = len(re.sub(r"\[\[.*?\]\]", "", copy["short"]).split())
    assert words <= 150
    assert copy["short"].count("FLAG: LEGAL — verify") == 2 and copy["full"].count("FLAG: LEGAL — verify") == 2
    assert "New York" in copy["short"] and "31 March 2025" in copy["short"]
    assert "42 CFR 493.3(b)(2)" in copy["full"]


# ------------------------------------------------------------------------ DOM (headless)
@pytest.fixture(scope="module")
def page_root(tmp_path_factory, synthetic_world) -> Path:
    from traceback_runner.references import register_reference

    root = tmp_path_factory.mktemp("landing") / "root"
    register_reference(root, synthetic_world["fasta"], "synth")
    assert signed_cli.main(["lab", "run", str(synthetic_world["chunks"]), "--root", str(root), "--json"]) == 0
    return root


def _dom(server, tmp_path, *, size="1280,800", route=None, want=None) -> str:
    """Inspect one page with a fresh link and bounded readiness wait."""

    from tests.lab.test_lab_server import _dom_cdp

    if want is None:
        want = (lambda d: True) if route else (lambda d: "data-measured" in d)
    good = lambda d: 'data-ready="1"' in d and want(d)  # noqa: E731
    link = server.issue_link() + (f"&route={route}" if route else "")
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "profile"
    return _dom_cdp(
        [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
         f"--user-data-dir={profile}", f"--window-size={size}", link],
        profile, ok=good, timeout=90,
    )



def _body(dom: str) -> dict[str, str]:
    tag = re.search(r"<body([^>]*)>", dom).group(1)
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', tag))


needs_chrome = pytest.mark.skipif(_chrome() is None, reason="no Chrome/Chromium for the headless DOM checks")


@needs_chrome
def test_landing_pixel_budget_at_1280x800(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path)
    body = _body(dom)
    assert body["csp-violations"] == "0"
    assert int(body["banner-height"]) <= 40
    assert int(body["header-height"]) <= 56
    assert int(body["clia-height"]) <= 320 and body["clia-clipped"] == "false"
    assert body["headline-visible"] == "true"
    assert int(body["curve-rows-visible"]) >= 4, body
    # >= 3 defended rows (or every row when fewer qualify) or the none state, above the fold.
    total = int(body["defended-total"])
    if body["none-defended"] == "true":
        assert int(body["defended-visible"]) >= 1
    else:
        assert int(body["defended-visible"]) >= min(3, total) and total >= 1
    assert body["clia-fully-visible"] == "true"
    assert body["page-scroll-x"] == "false"
    for text in (
        "What CLIA means for this demo", "FLAG: LEGAL — verify", "Defended results", "Depth → utility",
        "Positive controls", "SYNTHETIC", "REPEATABILITY", "KNOWN-STATUS", "FLAG: LEGAL — public data-use terms",
        "Samples", "License callouts", "literature, not measured",
    ):
        assert text in dom, text


@needs_chrome
@pytest.mark.parametrize("size", ["1024,768", "768,1024", "360,740"])
def test_no_horizontal_page_scroll(page_root, tmp_path, size) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        body = _body(_dom(server, tmp_path, size=size))
    assert body["page-scroll-x"] == "false"
    if size == "1024,768":
        assert int(body["clia-height"]) <= 320


@needs_chrome
def test_navigation_order_with_compare(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/stability")
    nav = re.search(r'<nav aria-label="Lenses"[^>]*>(.*?)</nav>', dom, re.S).group(1)
    labels = re.findall(r">([A-Za-z/ ]+?)(?: \(\d+[^)]*\))?</a>", nav)
    assert labels == ["Findings", "Trust/QC", "Stability", "Signals", "Compare", "Body map", "Agreement", "Evidence", "Methods"]
    assert 'aria-current="page">Stability' in nav
    options = re.findall(r"<option[^>]*>([^<]*)</option>", dom)
    assert len(options) == 1 and "@" not in options[0]  # derived inputs are never samples
    assert "usable to" in dom or "unstable below full depth" in dom
    assert "Stability: every point" in dom


@needs_chrome
def test_stability_names_both_halves_without_a_numeric_verdict(page_root, tmp_path, monkeypatch) -> None:
    from traceback_lab import views

    original = views.stability_panels

    def panels_with_missing_halves(sample, library=None):
        panels = original(sample, library)
        states = [
            (("skipped", "depth"), ("failed", "algorithm failed")),
            (("unavailable", "selected result unavailable"), ("not_interpretable", "metric value unavailable")),
        ]
        for panel, (a, b) in zip(panels, states):
            panel["split"] = None
            panel["split_status"] = {
                half: {"state": state, "reason": reason, "value": None, "deviation": None}
                for half, (state, reason) in zip(("A", "B"), (a, b))
            }
        assert len(panels) >= 3
        panels[2]["split"] = {"a": 100, "b": 105, "deviation_a": None, "deviation_b": None,
                               "relative_abs_diff": 5 / 102.5, "stable": None}
        panels[2]["split_status"] = {
            half: {"state": "ok", "reason": None, "value": value, "deviation": None}
            for half, value in (("A", 100), ("B", 105))
        }
        for point in panels[2]["points"]:
            point.update({"state": "not_interpretable", "unit": "none", "deviation": None, "band": None})
        return panels

    monkeypatch.setattr(views, "stability_panels", panels_with_missing_halves)
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/stability",
                   want=lambda page: "Split halves: split A skipped" in page)
    assert "split A skipped (depth); split B failed (algorithm failed)" in dom
    assert "split A unavailable (selected result unavailable); split B not interpretable (metric value unavailable)" in dom
    assert "No split-half stability verdict." in dom
    assert "100 / 105" in dom and "4.9% |A−B|" in dom
    assert "No validated stability deviation scale" in dom


@needs_chrome
def test_findings_grouping_and_flags(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/findings")
    assert "FLAG: CLIA — health-assessment labels on an individual sample" in dom
    summary = re.search(r'class="findings-summary">([^<]*)<', dom).group(1)
    labels = ["outside a reference", "not interpretable", "without a reference", "in range"]
    counts = []
    for label in labels:
        matches = re.findall(rf"(\d+) {label}", summary)
        assert len(matches) == 1, summary
        counts.append(int(matches[0]))
    groups = {name: int(count) for name, count in re.findall(r"<summary>(Not interpretable|No reference|In range) \((\d+)\)", dom)}
    assert list(groups) == ["Not interpretable", "No reference", "In range"]
    assert counts[0] == dom.count('class="finding-card"')
    assert counts[1:] == [groups["Not interpretable"], groups["No reference"], groups["In range"]]
    from traceback_lab.web.api import LabApi

    payload = LabApi(LabStore(page_root)).sample(_id12(page_root))
    visible_metrics = sum(
        1 for algorithm in payload["algorithms"] if not algorithm["hidden"] and algorithm["status"] == "ok"
        for metric in algorithm["metrics"] if metric["value"] is None or not isinstance(metric["value"], (dict, list))
    )
    assert sum(counts) == visible_metrics
    assert "No metric is outside a reference." in dom  # pre-cohort: no card groups
    assert "Cell-type composition (cell-origin)" in dom and "no composition at this depth" in dom


@needs_chrome
def test_about_clia_renders_the_full_copy(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route="/about/clia",
                   want=lambda d: "What CLIA is." in d and d.count("FLAG: LEGAL — verify") >= 2)
    assert "What CLIA is." in dom and "42 CFR 493.3(b)(2)" in dom
    assert dom.count("FLAG: LEGAL — verify") >= 2
    assert "Back to Lab home" in dom


@needs_chrome
def test_unknown_sample_state(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route="/sample/aaaaaaaaaaaa/findings")
    assert "No Lab sample matches aaaaaaaaaaaa." in dom


def _id12(root: Path) -> str:
    from traceback_lab.web.api import LabApi

    return LabApi(LabStore(root)).samples()["samples"][0]["id12"]


@needs_chrome
def test_evidence_lists_every_algorithm_by_tier_with_exclusions(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/evidence")
    from traceback_lab.interpretation.engine import load_library
    from traceback_lab.registry import load_registry

    hidden = load_library().hidden_algorithms  # intermediates (read-summary, meth-calls)
    for algorithm in load_registry():
        if algorithm.id not in hidden:
            assert f">{algorithm.id}<" in dom, algorithm.id
    assert "jagged-ends" in dom and "low-vaf-snv" in dom
    callouts = re.findall(r'class="license-callout" id="license-([a-z-]+)"', dom)
    assert len(callouts) == len(set(callouts)) and callouts  # one full block per license


@needs_chrome
def test_methods_shows_provenance_for_every_number(page_root, tmp_path) -> None:
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/methods")
    assert "Every number with its provenance" in dom
    assert "Interpretation library v" in dom and "provenance stamp only" in dom
    assert "FLAG: PII" in dom
    assert "/Users/" not in dom and "/home/" not in dom
    assert "source_id" in dom  # the full stamp sits in each row's disclosure
    callouts = re.findall(r'class="license-callout" id="license-([a-z-]+)"', dom)
    assert len(callouts) == len(set(callouts))


@needs_chrome
def test_deep_link_waits_for_deferred_lens_script(page_root, tmp_path, monkeypatch) -> None:
    """Fast API responses must not outrun the later deferred views script."""

    import time
    from traceback_lab.web.server import _Handler

    original = _Handler.do_GET
    delayed = []

    def delayed_views(handler):
        if handler.path.split("?", 1)[0] == "/assets/lab-views.js":
            delayed.append(handler.path)
            time.sleep(3)
        original(handler)

    monkeypatch.setattr(_Handler, "do_GET", delayed_views)
    with RunningLabServer(LabStore(page_root)) as server:
        dom = _dom(server, tmp_path, route=f"/sample/{_id12(page_root)}/methods")
    assert delayed, "the regression must actually delay the deferred lens script"
    assert "Every number with its provenance" in dom
    assert "Unknown sample" not in dom


@needs_chrome
def test_signals_body_map_and_agreement(page_root, tmp_path) -> None:
    id12 = _id12(page_root)
    with RunningLabServer(LabStore(page_root)) as server:
        signals = _dom(server, tmp_path / "s", route=f"/sample/{id12}/signals", want=lambda d: 'data-rendered="1"' in d)
        body = _dom(server, tmp_path / "b", route=f"/sample/{id12}/body-map")
        agreement = _dom(server, tmp_path / "a", route=f"/sample/{id12}/agreement")
    assert 'data-rendered="1"' in signals and "Size density" in signals and "Data table" in signals
    assert 'aria-pressed="true"' in signals
    assert "FLAG: LEGAL" in body and "cell-origin" in body
    assert "These signals share inputs and are not independent confirmation; agreement is not validation." in agreement
    assert "score" not in agreement.lower().split("agreement is not validation")[1][:2000]
    for dom in (signals, body, agreement):
        assert _body(dom)["csp-violations"] == "0"
