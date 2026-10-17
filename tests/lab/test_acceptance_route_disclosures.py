"""Synthetic AC7/11 contracts for registry disclosures on Lab routes."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, LICENSE_IDS, _license_nodes, _sample, _server
from tests.lab.test_landing import _dom, _id12, needs_chrome, page_root as _landing_page_root
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import load_registry
from traceback_lab.store import LabStore
from traceback_lab.web.api import _catalog
from traceback_lab.web.server import RunningLabServer


LIBRARY = load_library()
REGISTRY = [item.id for item in load_registry() if item.id not in LIBRARY.hidden_algorithms]
ROUTES = [
    "home", "findings", "trust-qc", "stability", "signals", "compare",
    "body-map", "agreement", "evidence", "methods",
]


@pytest.fixture(scope="module")
def route_page_root(tmp_path_factory, synthetic_world):
    return _landing_page_root.__wrapped__(tmp_path_factory, synthetic_world)


@pytest.mark.parametrize("algorithm_id", REGISTRY)
def test_every_registered_algorithm_has_catalog_disclosures(algorithm_id: str) -> None:
    row = next(row for row in _catalog(LIBRARY, REGISTRY, []) if row["algorithm_id"] == algorithm_id)
    headline = LIBRARY.headline(algorithm_id)
    assert headline is not None
    assert row["tier"] in {"PROVEN", "NEWER", "SPECULATIVE"}
    assert row["footnote"]  # A published result or a structured missing reason.
    expected = {
        license_id
        for (alg, _), entry in LIBRARY.entries.items()
        if alg == algorithm_id
        for license_id in entry.get("license_ids") or []
    }
    assert set(row["license_ids"]) == expected
    assert expected <= LIBRARY.licenses.keys()


@pytest.mark.parametrize("route,subtab", [(route, None) for route in ROUTES] + [
    ("signals", "Fragmentomics"), ("signals", "Methylation"),
])
@needs_chrome
def test_route_license_references_resolve_to_one_full_callout(route, subtab, tmp_path) -> None:
    """The expected library licenses are visible, complete, and referenced once per route."""
    sample = _sample()
    path = None if route == "home" else f"/sample/{ID12}/{route}"
    signal_algorithms = {
        None: ("copy-number",),
        "Fragmentomics": ("fragment-size", "end-motifs", "delfi-ratio", "long-tail", "mtdna",
                           "tss-profile", "breakpoint-motifs", "lionheart"),
        "Methylation": ("frag-x-meth", "global-5mc", "global-5hmc", "pmd-score",
                        "hypometh-reads", "modbase-qc"),
    }
    context_algorithms = signal_algorithms[subtab] if route == "signals" else {
        "body-map": ("cell-origin",), "agreement": ("copy-number",),
    }.get(route, ())
    expected = set(LICENSE_IDS) if route in {"home", "findings", "stability", "evidence", "methods"} else {
        license_id for row in sample["catalog"] if row["algorithm_id"] in context_algorithms
        for license_id in row["license_ids"]
    }
    if route not in {"trust-qc", "compare"}:
        assert expected, route
    licenses = {row["license_id"]: {"headline": row["headline"], "body": row["body"]}
                for row in sample["licenses"]}
    with _server(tmp_path, sample) as server:
        kind, source = server.app.assets["/assets/lab.js"]
        probe = b"""
          (() => {
            const timer = setInterval(() => {
              if (document.body.dataset.ready !== '1') return;
              const tabName = __TAB__;
              if (tabName) {
                const tab = [...document.querySelectorAll('.subtab')]
                  .find((item) => item.textContent === tabName);
                if (!tab) return;
                tab.click();
              }
              const expected = __LICENSES__;
              const blocks = [...document.querySelectorAll('.license-callout')];
              const bad = blocks.filter((block) => {
                const entry = expected[block.id.replace(/^license-/, '')];
                const heading = block.querySelector('h3');
                const body = block.querySelector('p');
                if (!entry || !heading || !body || !block.getClientRects().length ||
                    !heading.getClientRects().length || !body.getClientRects().length ||
                    heading.textContent.trim() !== entry.headline || body.textContent.trim() !== entry.body ||
                    !entry.headline || !entry.body ||
                    parseFloat(getComputedStyle(block).borderLeftWidth) < 3) return true;
                return [heading, body].some((node) => {
                  const style = getComputedStyle(node);
                  return parseFloat(style.fontSize) < 20 || parseInt(style.fontWeight, 10) < 700;
                });
              });
              document.body.dataset.routeLicenseCount = String(blocks.length);
              document.body.dataset.routeLicenseFailures = bad.map((block) => block.id).join(',');
              clearInterval(timer);
            }, 25);
          })();
        """.replace(b"__TAB__", json.dumps(subtab or ("Copy number" if route == "signals" else None)).encode()).replace(
            b"__LICENSES__", json.dumps(licenses).encode(),
        )
        server.app.assets["/assets/lab.js"] = (kind, source + probe)
        dom = _dom(server, tmp_path / route, route=path,
                   want=lambda page: 'data-route-license-count' in page)
    parsed = _license_nodes(dom)
    callouts, refs = parsed.callouts, parsed.refs
    assert len(callouts) == len(set(callouts)), route
    assert set(callouts) == expected, (route, set(callouts), expected)
    assert f'data-route-license-count="{len(callouts)}"' in dom
    assert 'data-route-license-failures=""' in dom
    assert set(refs) <= set(callouts), (route, set(refs) - set(callouts))
    if route == "home":
        assert "License callouts" in dom
    for license_id in callouts:
        assert license_id in LIBRARY.licenses


@pytest.mark.parametrize("route,expected", [
    ("home", "FLAG: PII"),
    ("findings", "FLAG: CLIA"),
    ("signals", "FLAG: FDA"),
    ("body-map", "FLAG: LEGAL"),
    ("methods", "FLAG: PII"),
])
@needs_chrome
def test_route_flags_are_visible(route, expected, route_page_root, tmp_path) -> None:
    id12 = _id12(route_page_root)
    path = None if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(LabStore(route_page_root)) as server:
        if route == "signals":
            # The FDA marker belongs to the Copy number panel, not the initial
            # Fragmentomics subtab. Open that panel through the actual control.
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
        dom = _dom(server, tmp_path / route, route=path, want=lambda d: expected in d)
    assert expected in dom
