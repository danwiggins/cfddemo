"""Missing AC21 state/surface pairs from the approved front-end matrix."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests.lab.test_acceptance_synthetic_routes import (
    ID12,
    SAMPLE_ROUTES,
    _run_marker,
    _sample,
    _server,
)
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, needs_chrome


@needs_chrome
@pytest.mark.parametrize("route", ("home", *SAMPLE_ROUTES))
def test_every_sample_surface_discloses_partial_input_and_all_excluded_indexes(route, tmp_path) -> None:
    sample = _sample()
    sample["excluded_chunks"] = [2, 5]
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda page: "Provisional: this input excludes 2 truncated chunks" in page)
    assert dom.count("Provisional: this input excludes 2 truncated chunks") == 1
    assert "Excluded chunk indexes: 2, 5" in dom


@needs_chrome
@pytest.mark.parametrize("route", ("home", *SAMPLE_ROUTES))
def test_every_sample_surface_discloses_interrupted_run(route, tmp_path) -> None:
    sample = _sample()
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        server.app.api.status = lambda: {
            "generation": 1, "active": None, "interrupted": _run_marker(),
            "library": sample["library_health"],
        }
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda page: "stopped during bam-qc" in page)
    assert "Results shown are from completed steps" in dom


@needs_chrome
@pytest.mark.parametrize("route", ("home", *SAMPLE_ROUTES))
@pytest.mark.parametrize("state,copy_text", (
    ("active", "A Lab run is in progress (step: bam-qc"),
    ("status-unavailable", "status is unavailable (log unreadable, unsafe, or over 1 MiB)"),
    ("invalid-library", "Interpretation library on disk is invalid (line 212)"),
))
def test_every_sample_surface_discloses_global_run_or_library_problem(state, copy_text, route, tmp_path) -> None:
    sample = _sample()
    if state == "invalid-library":
        sample["library_health"]["error"] = "line 212"
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    with _server(tmp_path, sample) as server:
        status = {"generation": 1, "active": None, "interrupted": None,
                  "library": sample["library_health"]}
        if state == "active":
            status["active"] = {**_run_marker(), "started_utc": "2026-10-16T00:00:00Z"}
        elif state == "status-unavailable":
            status["run_log_unavailable"] = {"run_id": _run_marker()["run_id"],
                                             "reason": "unreadable_or_over_1mib"}
        server.app.api.status = lambda: status
        dom = _dom(server, tmp_path / "browser", route=path,
                   want=lambda page: copy_text in page)
    assert copy_text in dom


@needs_chrome
@pytest.mark.parametrize("route", ("home", *SAMPLE_ROUTES))
def test_every_sample_surface_has_loading_copy_while_listing_is_pending(route, tmp_path) -> None:
    observed = {}
    with _server(tmp_path, _sample()) as server:
        listing = server.app.api.samples
        release_listing = threading.Event()

        def delayed_listing():
            assert release_listing.wait(timeout=120), "browser did not release pending listing"
            return listing()

        server.app.api.samples = delayed_listing
        profile = tmp_path / "browser-profile"
        path = "/" if route == "home" else f"/sample/{ID12}/{route}"
        placeholder = "loading-clia" if route == "home" else "loading-lens"

        def inspect(_command, evaluate) -> None:
            observed.update(evaluate("""(() => ({
              status: document.querySelector('#status').textContent.trim(),
              sections: [...document.querySelectorAll('#main section')].map((section) => ({
                loading: section.textContent.includes('Loading…'),
                height: section.getBoundingClientRect().height,
                classes: section.className,
              })),
            }))()"""))

        try:
            dom = _dom_cdp(
                [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--window-size=1280,800",
                 f"--user-data-dir={profile}", server.issue_link() + f"&route={path}"],
                profile, ok=lambda page: "Loading Lab results…" in page and placeholder in page,
                interact=inspect, timeout=60,
            )
        finally:
            release_listing.set()
    assert "Loading Lab results…" in dom
    assert observed["status"] == "Loading Lab results…"
    assert all(section["loading"] for section in observed["sections"])
    if route == "home":
        assert len(observed["sections"]) == 4
        assert observed["sections"][0]["height"] == 320
        assert all(section["height"] >= 160 for section in observed["sections"][1:])
    else:
        assert len(observed["sections"]) == 1
        assert observed["sections"][0]["height"] >= 160


@needs_chrome
def test_partial_input_trust_qc_names_excluded_chunk_indexes(tmp_path) -> None:
    sample = _sample()
    sample["excluded_chunks"] = [2, 5]
    with _server(tmp_path, sample) as server:
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/trust-qc",
                   want=lambda page: "Provisional: this input excludes 2 truncated chunks" in page)
    assert "Excluded chunk indexes: 2, 5" in dom


@needs_chrome
def test_every_finding_state_has_visible_text_in_its_expanded_view(tmp_path) -> None:
    labels = {
        "above_typical": "above typical",
        "markedly_above": "markedly above",
        "below_typical": "below typical",
        "markedly_below": "markedly below",
        "not_interpretable": "not interpretable",
        "no_reference": "no reference",
        "in_range": "in range",
    }
    sample = _sample()
    metrics = [metric for algorithm in sample["algorithms"] for metric in algorithm["metrics"]]
    assert len(metrics) >= len(labels)
    for metric, state in zip(metrics, labels):
        metric["state"] = state
    observed = []
    groups = []
    with _server(tmp_path, sample) as server:
        profile = Path(tmp_path / "browser-profile")

        def inspect(_command, evaluate) -> None:
            evaluate("document.querySelectorAll('#main .finding-group').forEach((group) => { group.open = true; })")
            observed.extend(evaluate("""(() => [...document.querySelectorAll('#main .state-chip')].map((chip) => {
              const textOnly = chip.cloneNode(true);
              textOnly.querySelectorAll('[aria-hidden="true"]').forEach((node) => node.remove());
              return textOnly.textContent.trim();
            }))()"""))
            groups.extend(evaluate("""(() => [...document.querySelectorAll('#main .finding-group')].map((group) => ({
              title: group.querySelector('summary').textContent.trim(),
              rows: [...group.querySelectorAll('tbody tr')].map((row) => row.textContent.trim()),
              visible: group.open && getComputedStyle(group).display !== 'none'
            })))()"""))

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/findings"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'class="finding-card"' in dom,
            interact=inspect,
        )
    assert set(list(labels.values())[:4]) <= set(observed)
    for title, metric in zip(("Not interpretable (1)", "No reference (1)", "In range (1)"), metrics[4:]):
        group = next(group for group in groups if title in group["title"])
        assert group["visible"] and any(metric["label"] in row for row in group["rows"])
