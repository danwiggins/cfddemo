"""Synthetic AC7/11 contracts for registry disclosures on Lab routes."""

from __future__ import annotations

import re

import pytest

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
                                            "license_ids": ["headline-rights"]},
            ),
            ("cell-origin", "secondary"): Entry(
                "cell-origin", "secondary", {"headline": False, "license_ids": ["secondary-rights"]},
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


@pytest.mark.parametrize("route", ROUTES)
@needs_chrome
def test_route_license_references_resolve_to_one_full_callout(route, route_page_root, tmp_path) -> None:
    """Every visible pill on a synthetic route must reach one full callout."""
    # Reuse the synthetic landing fixture; the browser is run only in the DOM suite.
    id12 = _id12(route_page_root)
    path = None if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(LabStore(route_page_root)) as server:
        dom = _dom(server, tmp_path / route, route=path)
    callouts = re.findall(r'class="license-callout" id="license-([a-z-]+)"', dom)
    refs = re.findall(r'class="license-pill" aria-describedby="license-([a-z-]+)"', dom)
    assert len(callouts) == len(set(callouts)), route
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
