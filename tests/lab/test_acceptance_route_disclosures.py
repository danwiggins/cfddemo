"""Synthetic AC7/11 contracts for registry disclosures on Lab routes."""

from __future__ import annotations

import copy
import json
import time

import pytest

from tests.lab.test_acceptance_synthetic_routes import ID12, LICENSE_IDS, _license_nodes, _sample, _server
from tests.lab.test_acceptance_accessibility import _key
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


@needs_chrome
def test_recorded_readiness_distinguishes_reasons_without_leaking_messages(tmp_path) -> None:
    sample = _sample()
    cases = {
        "copy-number": ("skipped", "missing_asset", "Registered asset missing"),
        "meth-tf": ("skipped", "invalid_asset", "Registered asset malformed"),
        "short-read-fragment-size": ("skipped", "platform", "Input platform mismatch"),
        "griffin": ("skipped", "depth", "Insufficient recorded depth"),
        "global-5hmc": ("skipped", "missing_channel", "Modification channel unavailable"),
        "instability": ("skipped", "upstream", "Upstream result unavailable"),
        "viral-reads": ("failed", None, "Recorded step failed"),
        "identity": ("not run", None, "Method not run"),
    }
    for row in sample["catalog"]:
        if row["algorithm_id"] in cases:
            row["status"], row["skip_reason"], _ = cases[row["algorithm_id"]]
    for row in sample["algorithms"]:
        if row["algorithm_id"] in cases:
            row["status"], row["skip_reason"], _ = cases[row["algorithm_id"]]
            row["skip_message"] = "PRIVATE_PATH_SENTINEL must never reach the view"
            if row["algorithm_id"] == "griffin":
                row["input_gate"] = "at least 5,000,000 eligible autosomal pairs"
    sample["cohort_state"] = "cohort_unavailable"
    sample["cohort_problem"] = {"code": "TBX-LAB-022", "cause": "PRIVATE_PATH_SENTINEL"}
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            labels = evaluate("[...document.querySelectorAll('#recorded-readiness .readiness-issues strong')].map(node => node.textContent)")
            for _, _, expected in cases.values():
                assert any(expected in label for label in labels), (expected, labels)
            text = evaluate("document.getElementById('recorded-readiness').textContent")
            assert "Current environment checked at: unknown" in text
            assert "5,000,000 eligible autosomal pairs" in text
            assert "Cohort reference: unavailable" in text
            assert "PRIVATE_PATH_SENTINEL" not in text and "negative biological finding" in text
            assert evaluate("[...document.querySelectorAll('#recorded-readiness a')].every(a => a.href.includes('/docs/LAB-GUIDE.md#'))")
            assert evaluate("[...document.querySelectorAll('.readiness-command code')].map(node => node.textContent)") == [
                "traceback lab doctor --root <ROOT>", "traceback lab run <FILE> --root <ROOT>",
            ]

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=1280,800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="recorded-readiness"' in dom,
            interact=inspect,
        )


@needs_chrome
def test_readiness_refresh_failure_retains_projection_and_navigation_discards_late_response(tmp_path) -> None:
    sample = _sample()
    late = copy.deepcopy(sample)
    late["full_input_id"] = "changed"
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            evaluate("window.fetch = () => Promise.reject(new Error('offline')); document.getElementById('readiness-refresh').click()")
            assert evaluate("document.querySelector('#recorded-readiness [role=status]').textContent.includes('projection below is stale')")
            assert evaluate("window.TracebackLab.state.sample.catalog[0].status === 'ok'")
            assert evaluate("document.getElementById('recorded-readiness').textContent.includes('Current environment checked at: unknown')")
            assert evaluate("document.querySelector('#lenses a[href$=\"/findings\"]').getAttribute('aria-disabled') === null")
            evaluate("""window.fetch = () => new Promise(resolve => { window.finishReadinessFetch = resolve; });
              document.getElementById('readiness-refresh').click();""")
            evaluate(f"location.hash = '#/sample/{ID12}/findings'")
            evaluate("window.finishReadinessFetch(new Response(JSON.stringify(__PAYLOAD__), {status:200}))".replace(
                "__PAYLOAD__", json.dumps(late)))
            for _ in range(40):
                if evaluate("document.querySelector('#main h1')?.textContent === 'Findings'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("navigation did not reach Findings")
            time.sleep(0.1)
            assert evaluate("location.hash.endsWith('/findings')")
            assert evaluate("window.TracebackLab.state.sample.full_input_id !== 'changed'")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=1280,800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="recorded-readiness"' in dom,
            interact=inspect,
        )


@needs_chrome
def test_readiness_refresh_success_and_copy_feedback(tmp_path) -> None:
    sample = _sample()
    refreshed = copy.deepcopy(sample)
    refreshed["catalog"][0]["status"] = "skipped"
    refreshed["catalog"][0]["skip_reason"] = "missing_asset"
    refreshed["algorithms"][0]["status"] = "skipped"
    refreshed["algorithms"][0]["skip_reason"] = "missing_asset"
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def inspect(command, evaluate) -> None:
            payload = json.dumps(refreshed)
            evaluate("""window.refreshPaths = [];
              window.fetch = async (path, options) => {
                window.refreshPaths.push({path, credentials: options?.credentials, hasSignal: !!options?.signal});
                return new Response(JSON.stringify(__PAYLOAD__), {status: 200,
                  headers: {'Content-Type': 'application/json'}});
              };""".replace("__PAYLOAD__", payload))
            command("Page.bringToFront")
            evaluate("document.getElementById('readiness-refresh').focus()")
            _key(command, "Enter", "Enter", 13)
            for _ in range(40):
                if evaluate("window.TracebackLab.state.sample.catalog[0].status === 'skipped'"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("sample refresh did not replace the recorded projection")
            assert evaluate("window.refreshPaths.filter(row => row.path.startsWith('/api/samples/'))") == [{
                "path": f"/api/samples/{ID12}", "credentials": "same-origin", "hasSignal": True,
            }]
            assert evaluate("document.getElementById('recorded-readiness').textContent.includes('Registered asset missing')")
            assert evaluate("document.getElementById('recorded-readiness').textContent.includes('Current environment checked at: unknown')")
            assert evaluate("document.activeElement?.id === 'readiness-refresh'")
            assert evaluate("document.activeElement?.matches(':focus-visible')")
            evaluate("""Object.defineProperty(navigator, 'clipboard', {configurable:true,
              value:{writeText: async (text) => { window.copiedCommand = text; }}});
              document.querySelector('.readiness-command button').click();""")
            for _ in range(20):
                if evaluate("document.querySelector('#recorded-readiness [role=status]:last-child')?.textContent === 'Copied command'"):
                    break
                time.sleep(0.05)
            assert evaluate("window.copiedCommand") == "traceback lab doctor --root <ROOT>"
            evaluate("""Object.defineProperty(navigator, 'clipboard', {configurable:true,
              value:{writeText: async () => { throw new Error('denied'); }}});
              document.querySelector('.readiness-command button').click();""")
            for _ in range(20):
                if evaluate("document.getElementById('recorded-readiness').textContent.includes('Copy failed.')"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("clipboard rejection did not produce an error announcement")
            assert evaluate("document.querySelector('.readiness-command code').textContent") == "traceback lab doctor --root <ROOT>"

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--window-size=1280,800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="recorded-readiness"' in dom,
            interact=inspect,
        )


@needs_chrome
def test_readiness_refresh_401_expires_session_without_losing_record(tmp_path) -> None:
    """An explicit refresh 401 shares the global session-expiry transition."""

    with _server(tmp_path, _sample()) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            before = evaluate("window.TracebackLab.state.sample.id12")
            evaluate("""(() => {
              const original = window.fetch;
              window.fetch = (...args) => String(args[0]).startsWith('/api/samples/')
                ? Promise.resolve(new Response('{}', {status:401})) : original.apply(window, args);
              document.getElementById('readiness-refresh').click();
            })()""")
            for _ in range(40):
                if evaluate("window.TracebackLab.state.sessionExpired"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("readiness refresh 401 did not expire the session")
            assert evaluate("window.TracebackLab.state.sample.id12") == before
            assert evaluate("document.getElementById('freshness-state').dataset.stale === 'true'")
            assert evaluate("document.body.dataset.disconnected === '1'")
            assert evaluate("document.getElementById('status').textContent.includes('Session expired')")
            assert evaluate("!document.getElementById('recovery-action').textContent.includes('Export')")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="recorded-readiness"' in dom,
            interact=inspect,
        )


@needs_chrome
def test_readiness_refresh_does_not_duplicate_after_rerender(tmp_path) -> None:
    """The in-flight guard outlives a replacement Methods button."""

    sample = _sample()
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            evaluate("""(() => {
              const original = window.fetch;
              window.sampleFetches = 0;
              window.fetch = (...args) => String(args[0]) === '/api/samples/3f9a1c2b7e04'
                ? (window.sampleFetches++, new Promise((resolve) => { window.finishSampleRefresh = resolve; }))
                : original.apply(window, args);
              document.getElementById('readiness-refresh').click();
              window.TracebackLab.render();
              document.getElementById('readiness-refresh').click();
            })()""")
            assert evaluate("window.sampleFetches === 1")
            assert evaluate("document.getElementById('readiness-refresh').disabled")
            evaluate("window.finishSampleRefresh(new Response(__PAYLOAD__, {status:200}))".replace(
                "__PAYLOAD__", json.dumps(json.dumps(sample))))
            for _ in range(40):
                if evaluate("window.TracebackLab.state.selectedRefresh === null"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("selected refresh did not settle")
            assert evaluate("window.sampleFetches === 1")
            assert evaluate("!document.getElementById('readiness-refresh').disabled")
            assert evaluate("document.getElementById('freshness-state').dataset.stale === 'false'")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="recorded-readiness"' in dom,
            interact=inspect,
        )


@needs_chrome
@pytest.mark.parametrize("width", [375, 1280])
def test_methods_consumes_validated_route_context_and_keeps_refresh_focus(width, tmp_path) -> None:
    sample = _sample()
    metric_id = sample["algorithms"][0]["metrics"][0]["metric_id"]
    with _server(tmp_path, sample) as server:
        profile = tmp_path / "browser-profile"

        def inspect(_command, evaluate) -> None:
            def context(value: dict, *, invalid: bool = False) -> None:
                evaluate("""(() => { const lab = window.TracebackLab;
                  lab.state.navigationRevision += 1;
                  lab.state.route.context = __CONTEXT__;
                  lab.state.route.contextInvalid = __INVALID__;
                  lab.render(); })()""".replace("__CONTEXT__", json.dumps(value)).replace("__INVALID__", json.dumps(invalid)))

            def wait_for(expression: str) -> None:
                for _ in range(40):
                    if evaluate(expression):
                        return
                    time.sleep(0.05)
                pytest.fail(f"method context did not settle: {expression}")

            context({"algorithm": "epi-clock", "metric": None, "family": None})
            wait_for("document.activeElement?.id === 'method-epi-clock'")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == 1

            context({"algorithm": "depth", "metric": None, "family": None})
            wait_for("document.activeElement?.id === 'method-depth'")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == 1
            assert not evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            evaluate("""const input = document.getElementById('method-search'); input.value = '';
              input.dispatchEvent(new Event('input', {bubbles:true}));""")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == len(REGISTRY)

            context({"algorithm": "bam-qc", "metric": metric_id, "family": None})
            wait_for(f"document.activeElement?.dataset.metricId === '{metric_id}'")
            assert evaluate("document.activeElement?.dataset.algorithmId") == "bam-qc"

            context({"algorithm": None, "metric": None, "family": "methylation"})
            wait_for("document.activeElement?.id === 'method-family'")
            assert evaluate("document.getElementById('method-family').value") == "methylation"

            context({"algorithm": "not-registered", "metric": None, "family": None})
            assert evaluate("document.getElementById('method-context-feedback').textContent.includes('unavailable for this sample')")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == len(REGISTRY)

            context({"algorithm": None, "metric": None, "family": "not-a-family"})
            assert evaluate("document.getElementById('method-context-feedback').textContent.includes('family is unavailable')")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == len(REGISTRY)

            context({"algorithm": "bam-qc", "metric": "not_a_metric", "family": None})
            wait_for("document.activeElement?.id === 'method-bam-qc'")
            assert evaluate("document.getElementById('method-context-feedback').textContent.includes('metric is unavailable')")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == 1

            context({"algorithm": "bam-qc", "metric": None, "family": "methylation"})
            assert evaluate("document.getElementById('method-context-feedback').textContent.includes('outside that family')")
            assert evaluate("document.querySelectorAll('#method-catalog-results article').length") == len(REGISTRY)

            context({"algorithm": None, "metric": None, "family": None}, invalid=True)
            assert evaluate("document.getElementById('method-context-feedback').textContent.includes('invalid context')")

            context({"algorithm": "epi-clock", "metric": None, "family": None})
            wait_for("document.activeElement?.id === 'method-epi-clock'")
            evaluate("""window.fetch = async () => new Response(JSON.stringify(__PAYLOAD__),
              {status: 200, headers: {'Content-Type': 'application/json'}});""".replace(
                  "__PAYLOAD__", json.dumps(sample)))
            evaluate("document.getElementById('readiness-refresh').click()")
            wait_for("document.activeElement?.id === 'readiness-refresh'")
            time.sleep(0.05)
            assert evaluate("document.activeElement?.id === 'readiness-refresh'")

        _dom_cdp(
            [_chrome(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", f"--window-size={width},800",
             server.issue_link() + f"&route=/sample/{ID12}/methods"],
            profile, ok=lambda dom: 'data-ready="1"' in dom and 'id="method-catalog-results"' in dom,
            interact=inspect, viewport=(width, 800),
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
