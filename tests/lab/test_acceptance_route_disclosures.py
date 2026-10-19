"""Synthetic AC7/11 contracts for registry disclosures on Lab routes."""

from __future__ import annotations

import json

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, LICENSE_IDS, _license_nodes, _sample, _server
from tests.lab.test_lab_server import _chrome, _dom_cdp
from tests.lab.test_landing import _dom, _id12, needs_chrome, page_root as _landing_page_root
from traceback_lab.interpretation.engine import Entry, Library, load_library
from traceback_lab.registry import load_registry
from traceback_lab.store import LabStore
from traceback_lab.web.api import _catalog, _result_license_ids
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
    assert row["description"] and row["question"] and row["limitations"]
    assert row["family"] and row["platforms"]
    assert row["environment_readiness"] == "unknown" and row["readiness_checked_at"] is None


def test_method_explanations_cover_exactly_the_visible_registry() -> None:
    assert set(LIBRARY.method_catalog) == set(REGISTRY)
    assert LIBRARY.hidden_algorithms == {"read-summary", "meth-calls"}


def test_catalog_numeric_prerequisites_match_algorithm_gates() -> None:
    from traceback_lab.adapters.griffin_run import MIN_ELIGIBLE_AUTOSOMAL_PAIRS
    from traceback_lab.algorithms.aligned_end_structure import MIN_DEPTH_X as END_DEPTH
    from traceback_lab.algorithms.lionheart import RESOURCE_VERSION
    from traceback_lab.algorithms.wps import MIN_DEPTH_X as WPS_DEPTH

    methods = LIBRARY.method_catalog
    assert f"At least {END_DEPTH:g}x observed eligible depth" in methods["aligned-end-structure"]["requirements"]
    assert f"At least {WPS_DEPTH:g}x approximate autosomal depth" in methods["wps"]["requirements"]
    assert MIN_ELIGIBLE_AUTOSOMAL_PAIRS == 5_000_000
    assert "At least five million eligible autosomal pairs" in methods["griffin"]["requirements"]
    assert any(RESOURCE_VERSION in text for text in methods["lionheart"]["requirements"])


@needs_chrome
def test_methods_search_and_filters_keep_provenance_available(tmp_path) -> None:
    sample = _sample()
    sample["catalog"][0]["status"] = "skipped"
    sample["catalog"][0]["skip_reason"] = "missing_asset"
    sample["algorithms"] = [row for row in sample["algorithms"] if row["algorithm_id"] != "epi-clock"]
    next(row for row in sample["catalog"] if row["algorithm_id"] == "epi-clock")["status"] = "not run"
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def interact(_command, evaluate) -> None:
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == len(REGISTRY)
            assert evaluate("document.getElementById('method-provenance') !== null")
            assert evaluate("document.querySelector('#method-status option[value=ok]').textContent") == "ran"
            assert evaluate("document.querySelector('#main').textContent.includes('Current environment readiness is unknown')")
            assert evaluate("""(() => { const input = document.getElementById('method-search');
              input.value = 'Weidner'; input.dispatchEvent(new Event('input', {bubbles: true}));
              return [...document.querySelectorAll('#method-catalog-results article')]
                .map((row) => row.id); })()""") == ["method-epi-clock"]
            assert not evaluate("document.querySelector('#method-epi-clock button')")
            assert evaluate("document.querySelector('#method-epi-clock a[href$=\"/evidence\"]').getAttribute('aria-label').includes('Weidner')")
            assert evaluate("""(() => { const input = document.getElementById('method-search');
              input.value = ''; input.dispatchEvent(new Event('input', {bubbles: true}));
              const select = document.getElementById('method-status'); select.value = 'skipped';
              select.dispatchEvent(new Event('change', {bubbles: true}));
              return [...document.querySelectorAll('#method-catalog-results article')]
                .map((row) => row.id); })()""") == ["method-bam-qc"]
            assert evaluate("""(() => { const article = document.getElementById('method-bam-qc');
              article.querySelector('details').open = true;
              article.querySelector('button').click();
              return document.activeElement?.id === 'method-provenance'; })()""")
            assert evaluate("document.querySelector('#method-bam-qc button').getAttribute('aria-label').includes('Mapped share')")
            assert evaluate("""(() => { const select = document.getElementById('method-family');
              select.value = 'methylation'; select.dispatchEvent(new Event('change', {bubbles: true}));
              return document.querySelector('#method-catalog-results').textContent.includes('No methods match'); })()""")
            assert evaluate("document.getElementById('method-provenance').textContent.includes('Every number with its provenance')")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=1280,800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="method-catalog-results"' in dom,
            interact=interact,
        )


def test_stored_modkit_result_keeps_its_license_without_labeling_new_decoder() -> None:
    entry = LIBRARY.headline("cell-origin")
    assert entry is not None
    old = _result_license_ids(entry, "cell-origin", "1", "ok")
    new = _result_license_ids(entry, "cell-origin", "2", "ok")
    skipped = _result_license_ids(entry, "cell-origin", "1", "skipped")
    assert "modkit-ont-public" in old
    assert "modkit-ont-public" not in new
    assert "modkit-ont-public" not in _result_license_ids(entry, "cell-origin", "3", "ok")
    assert "modkit-ont-public" not in skipped
    assert "modkit-ont-public" in _result_license_ids(entry, "cell-origin", "", "ok")
    assert "modkit-ont-public" in LIBRARY.licenses
    for version, expected in (("1", old), ("2", new)):
        catalog = _catalog(
            LIBRARY, ["cell-origin"],
            [{"algorithm_id": "cell-origin", "algorithm_version": version,
              "status": "ok", "skip_reason": None}],
        )
        assert catalog[0]["license_ids"] == expected


def test_catalog_keeps_nonheadline_metric_licenses_when_result_exists() -> None:
    library = Library(
        "test",
        {
            ("cell-origin", "headline"): Entry(
                "cell-origin", "headline", {"headline": True, "label": "Cell origin", "tier": "NEWER",
                                            "license_ids": ["headline-rights"], "citations": ["doi:headline"]},
            ),
            ("cell-origin", "secondary"): Entry(
                "cell-origin", "secondary", {"headline": False, "license_ids": ["secondary-rights"],
                                              "citations": ["doi:secondary", "doi:headline"]},
            ),
        },
        {"headline-rights": {}, "secondary-rights": {}, "modkit-ont-public": {}},
    )
    for version, expected in (("1", {"headline-rights", "secondary-rights", "modkit-ont-public"}),
                              ("2", {"headline-rights", "secondary-rights"})):
        catalog = _catalog(
            library, ["cell-origin"],
            [{"algorithm_id": "cell-origin", "algorithm_version": version,
              "status": "ok", "skip_reason": None}],
        )
        assert set(catalog[0]["license_ids"]) == expected
        assert catalog[0]["citations"] == ["doi:headline", "doi:secondary"]


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
