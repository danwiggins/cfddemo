"""Synthetic AC7/11 contracts for registry disclosures on Lab routes."""

from __future__ import annotations

import re

import pytest

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
    ("trust-qc", "FLAG: FDA"),
    ("body-map", "FLAG: LEGAL"),
    ("methods", "FLAG: PII"),
])
@needs_chrome
def test_route_flags_are_visible(route, expected, route_page_root, tmp_path) -> None:
    id12 = _id12(route_page_root)
    path = None if route == "home" else f"/sample/{id12}/{route}"
    with RunningLabServer(LabStore(route_page_root)) as server:
        dom = _dom(server, tmp_path / route, route=path)
    assert expected in dom
