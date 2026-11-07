"""The local site's cell-origin record view (signal CO5).

Python: the view body is a pure function of the verified measurement (ranking,
the top-12 bar plus one combined row, "<0.1%", intervals only where
available, the residual line).  End to end: a planted cell-origin run is
imported and served, and its record route returns the registered view.  DOM:
the site draws min(12, n) + 1 bars, no whisker, no ``<pre>``, and a full table.
All values are synthetic.
"""

from __future__ import annotations

import json
import subprocess
import typing
from pathlib import Path
from typing import Any

import pytest

from tests.test_cell_origin_stage import MEASUREMENT, Setup, _json, _row
from tests.web.longitudinal_env import NODE, needs_node
from tests.web.test_loopback_server import _exchange
from tests.web.test_records import CHROME, _attr, _listing, _nodes, _ok, _run, _tag, _text
from tests.web.test_serve import _get, _serving
from traceback_runner.cell_origin import (
    CellOriginMeasurementV1,
    ContributorEstimate,
    FractionInterval,
    ModbaseModel,
)
from traceback_runner.cell_origin_view import (
    KEY_COUNT_UNIT,
    TOP_ROWS,
    VIEW_SCHEMA_VERSION,
    build_body,
    percent_text,
)
from traceback_runner.web.records import ANALYSIS_BANNER
from traceback_runner.web.state_copy import ENUM_SOURCES, STATE_COPY


def _planted(tmp_path: Path, capsys, monkeypatch) -> tuple[Setup, Path, dict]:
    setup = Setup(tmp_path, capsys, monkeypatch)
    root = setup.root()
    code, payload = setup.run(root, "--modbase-model", "model-x")
    assert code == 0, payload
    return setup, root, payload


def _measurement(root: Path, payload: dict) -> CellOriginMeasurementV1:
    record = root / "records" / _row(payload)["record_id"]
    return CellOriginMeasurementV1.model_validate_json((record / MEASUREMENT).read_bytes())


def _estimate(name: str, fraction: float, *, interval: bool = False) -> ContributorEstimate:
    return ContributorEstimate(
        contributor_id=name,
        fraction=fraction,
        raw_nnls_weight=fraction,
        interval=(
            FractionInterval(low=max(0.0, fraction - 0.01), high=min(1.0, fraction + 0.01))
            if interval
            else None
        ),
        interval_state="available" if interval else "insufficient_information",
    )


def _many(base: CellOriginMeasurementV1, n: int, zeros: int) -> CellOriginMeasurementV1:
    """``n`` contributors, the last ``zeros`` at exactly 0; fractions sum to 1."""

    weights = [float(n - index) for index in range(n - zeros)]
    total = sum(weights)
    estimates = [
        _estimate(f"Type{index:02d}", weight / total, interval=index % 2 == 0)
        for index, weight in enumerate(weights)
    ]
    estimates += [_estimate(f"Zero{index:02d}", 0.0) for index in range(zeros)]
    # Unsorted input: the body ranks by fraction, then ID.
    return base.model_copy(update={"estimates": tuple(reversed(estimates))})


@pytest.fixture
def planted(tmp_path: Path, capsys, monkeypatch) -> tuple[Setup, Path, dict]:
    return _planted(tmp_path, capsys, monkeypatch)


# --------------------------------------------------------------------------
# The body (pure)
# --------------------------------------------------------------------------


def test_percent_text() -> None:
    assert percent_text(0.0) == "0%"
    assert percent_text(0.0004) == "<0.1%"
    assert percent_text(0.0006) == "<0.1%"  # 0.06% never reads as 0.1%
    assert percent_text(0.000999) == "<0.1%"
    assert percent_text(0.001) == "0.1%"
    assert percent_text(0.00105) == "0.1%"
    assert percent_text(0.1234) == "12.3%"
    assert percent_text(1.0) == "100.0%"


def test_top_rows_then_one_combined_row(planted) -> None:
    _, root, payload = planted
    body = build_body(_many(_measurement(root, payload), n=30, zeros=11))
    assert len(body.top) == TOP_ROWS == 12
    assert [row.rank for row in body.top] == list(range(1, 13))
    fractions = [row.fraction for row in body.contributors]
    assert fractions == sorted(fractions, reverse=True)
    assert len(body.contributors) == 30
    assert body.other.contributors == 18 and body.other.at_zero == 11
    assert body.other.label == "18 other contributors combined (11 at 0%)"
    assert body.other.fraction == pytest.approx(sum(row.fraction for row in body.contributors[12:]))
    assert body.whiskers is False
    # Ties at zero are ordered by ID, after every non-zero value.
    zero_ids = [row.contributor_id for row in body.contributors if row.fraction == 0]
    assert zero_ids == sorted(zero_ids) and body.contributors[-1].contributor_id == "Zero10"


def test_few_contributors_still_end_with_the_combined_row(planted) -> None:
    _, root, payload = planted
    measurement = _measurement(root, payload)
    body = build_body(measurement)
    assert len(body.top) == len(measurement.estimates) <= TOP_ROWS
    assert body.other.contributors == 0
    assert body.other.label == "No other contributors"


def test_intervals_only_where_available(planted) -> None:
    _, root, payload = planted
    body = build_body(_many(_measurement(root, payload), n=6, zeros=0))
    for row in body.contributors:
        if row.interval_state == "available":
            assert row.interval is not None
            assert row.interval.text == (
                f"{percent_text(row.interval.low)} to {percent_text(row.interval.high)}"
            )
        else:
            assert row.interval is None
            assert row.interval_label == STATE_COPY["interval"]["insufficient_information"][0]
    assert {row.interval_state for row in body.contributors} == {
        "available", "insufficient_information"
    }


def test_basis_model_residual_and_denominators(planted) -> None:
    _, root, payload = planted
    measurement = _measurement(root, payload)
    body = build_body(measurement)
    d = measurement.denominators
    assert body.basis == (
        f"Based on {d.classified_fragments:,} fragments at {d.observed_markers:,} of "
        f"{d.registered_markers:,} markers; model declared by operator"
    )
    assert body.model.id == "model-x" and body.model.source == "operator_declared"
    assert [(row.axis, row.token) for row in body.states] == [
        ("cell_origin", "ready"), ("modbase", "operator_declared")
    ]
    assert body.denominator_lines[1] == f"{d.mixed_fragments:,} mixed fragments not used"
    assert body.denominator_lines[2] == f"{d.eligible_alignments:,} eligible alignments"
    assert "no threshold" in body.residual_text
    assert "L2 norm" in body.residual_text
    header = measurement.model_copy(
        update={"modbase_model": ModbaseModel(id="model-h", source="header")}
    )
    assert build_body(header).basis.endswith("model read from the BAM header")
    assert build_body(header).states[1].token == "header"


def test_ids_the_public_boundary_refuses_are_named_not_shown(planted) -> None:
    """A valid model or contributor ID that reads as a path never 503s the record."""

    from traceback_runner.web.contracts import validate_public_projection

    _, root, payload = planted
    measurement = _measurement(root, payload)
    odd = measurement.model_copy(update={
        "modbase_model": ModbaseModel(id="model..v1", source="operator_declared"),
        "estimates": (_estimate("Type..A", 0.7), _estimate("TypeB", 0.3)),
    })
    body = build_body(odd)
    validate_public_projection(json.loads(body.model_dump_json()))
    assert body.model.id == "(ID in the signed measurement file)"
    assert body.contributors[0].contributor_id == "Contributor 1 (ID in the signed measurement file)"
    assert body.contributors[1].contributor_id == "TypeB"


def test_state_copy_tokens_match_the_models() -> None:
    assert set(ENUM_SOURCES["modbase"]) == set(
        typing.get_args(ModbaseModel.model_fields["source"].annotation)
    )
    assert set(ENUM_SOURCES["interval"]) == set(
        typing.get_args(ContributorEstimate.model_fields["interval_state"].annotation)
    )


# --------------------------------------------------------------------------
# End to end: the served record route returns the registered view
# --------------------------------------------------------------------------


def _served_view(setup: Setup, root: Path, payload: dict) -> tuple[dict, dict]:
    record_id = _row(payload)["record_id"]
    code, imported = _json(
        setup.capsys, "catalog", "import", root / "records" / record_id, "--root", root
    )
    assert code == 0, imported
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, view = _get(service, f"/api/v1/records/{record_id}", cookie)
        assert status == 200, view
        status, listing = _get(service, "/api/v1/records", cookie)
        assert status == 200, listing
    return view, listing


def test_the_served_record_carries_the_cell_origin_view(planted) -> None:
    setup, root, payload = planted
    view, listing = _served_view(setup, root, payload)
    measurement = _measurement(root, payload)
    assert view["schema_version"] == VIEW_SCHEMA_VERSION
    assert view["analysis"] == "cell_origin"
    assert view["banner"] == ANALYSIS_BANNER
    assert view["key_count"] == measurement.denominators.classified_fragments
    assert view["key_count_unit"] == KEY_COUNT_UNIT
    assert view["body"] == json.loads(build_body(measurement).model_dump_json())
    # The catalog row never carries a fraction (gate G1 screenshot risk).
    (row,) = [item for item in listing["records"] if item["record_id"] == view["record_id"]]
    fractions = {repr(item.fraction) for item in measurement.estimates if item.fraction}
    assert not any(value in json.dumps(row) for value in fractions)
    assert not any(row_text["fraction_text"] in json.dumps(row) for row_text in view["body"]["top"]
                   if row_text["fraction_text"] not in {"0%"})


# --------------------------------------------------------------------------
# DOM harness
# --------------------------------------------------------------------------


def _summary(view: dict) -> dict:
    return {
        "record_id": view["record_id"],
        "short_id": view["short_id"],
        "status": "verified",
        "status_label": "Verified",
        "label": None,
        "analysis": "cell_origin",
        "analysis_label": "Cell origin",
        "input_digest": view["input_digest"],
        "key_count": view["key_count"],
        "key_count_unit": view["key_count_unit"],
        "method_version_state": "current_method_version",
        "reference_id": view["reference_id"],
        "policy_label": None,
        "eligible_alignments": None,
        "records_scanned": None,
        "preflight": view["preflight"]["outcome"],
        "preflight_label": "Preflight passed",
        "preflight_warnings": 0,
        "warning_texts": [],
        "method_version": view["method_version"],
        "imported_at": view["imported_at"],
    }


def _dom(tmp_path: Path, view: dict, *, compact: bool = False) -> dict:
    record_id = view["record_id"]
    scenario: dict[str, Any] = {
        "responses": {"/api/v1/records": _ok(_listing(_summary(view))),
                      f"/api/v1/records/{record_id}": _ok(view)},
        "steps": [{"hash": f"#/records/{record_id}"}],
    }
    if compact:
        scenario["compact"] = True
    return _run(tmp_path, scenario)[1]


@pytest.fixture
def served(planted) -> tuple[dict, CellOriginMeasurementV1]:
    setup, root, payload = planted
    view, _ = _served_view(setup, root, payload)
    return view, _measurement(root, payload)


def _with_body(view: dict, measurement: CellOriginMeasurementV1) -> dict:
    return {**view, "body": json.loads(build_body(measurement).model_dump_json())}


@needs_node
@pytest.mark.parametrize(
    "n, zeros, compact", [(30, 11, False), (30, 11, True), (5, 0, False), (12, 3, False), (13, 1, True)]
)
def test_dom_bars_are_min_12_n_plus_one_other(
    served, tmp_path: Path, n: int, zeros: int, compact: bool
) -> None:
    view, measurement = served
    report = _dom(tmp_path, _with_body(view, _many(measurement, n=n, zeros=zeros)), compact=compact)
    # The planted BAM's header lacks digests: a preflight warning makes it "partial".
    assert report["dataset"]["view"] == "record"
    assert report["dataset"]["state"] in {"success", "partial"}
    tree = report["view"]
    bars = _nodes(tree, lambda node: node["tag"] == "rect" and "bar" in node["attrs"].get("class", "").split())
    assert len(bars) == min(12, n) + 1
    assert bars[-1]["attrs"]["data-rank"] == "other"
    assert "bar-other" in bars[-1]["attrs"]["class"]
    assert [bar["attrs"]["data-rank"] for bar in bars[:-1]] == [str(i) for i in range(1, min(12, n) + 1)]
    # No whisker of any kind: intervals live in the table only.
    assert not _nodes(tree, lambda node: "whisker" in node["attrs"].get("class", ""))
    chart = _nodes(tree, _attr("id", "co-chart"))[0]
    assert not _nodes(chart, lambda node: node["tag"] == "line" and "interval" in json.dumps(node["attrs"]))
    assert not _nodes(tree, _tag("pre"))
    rows = _nodes(_nodes(tree, _attr("id", "co-table"))[0], _attr("data-interval"))
    assert len(rows) == n


@needs_node
def test_dom_evidence_first_and_partial_interval_text(served, tmp_path: Path) -> None:
    view, measurement = served
    report = _dom(tmp_path, _with_body(view, _many(measurement, n=6, zeros=0)))
    tree = report["view"]
    order = json.dumps(tree)
    assert order.index("analysis-banner") < order.index("co-basis") < order.index("co-chart")
    assert order.index("co-chart") < order.index("co-table")
    assert _text(_nodes(tree, _attr("id", "co-basis"))[0]).startswith("Based on ")
    assert "declared by the operator" in _text(_nodes(tree, _attr("id", "co-model"))[0])
    assert "no threshold" in _text(_nodes(tree, _attr("id", "co-residual"))[0])
    assert "no 'unassigned' share" in _text(_nodes(tree, _attr("id", "co-sum-note"))[0])
    rows = _nodes(_nodes(tree, _attr("id", "co-table"))[0], _attr("data-interval"))
    partial = [row for row in rows if row["attrs"]["data-interval"] == "insufficient_information"]
    available = [row for row in rows if row["attrs"]["data-interval"] == "available"]
    assert partial and available
    assert all("No interval" in _text(row) for row in partial)
    assert all(" to " in _text(row) for row in available)
    assert _text(_nodes(tree, _attr("id", "analysis-body-title"))[0]) == (
        "Estimated cell-type mixture (Loyfer atlas)"
    )


@needs_node
def test_dom_without_the_renderer_draws_no_estimate(served, tmp_path: Path) -> None:
    """A view schema with no renderer still names the signed report only."""

    view, _ = served
    other = {**view, "schema_version": "traceback.local-cell-origin-view.v9"}
    tree = _dom(tmp_path, other)["view"]
    assert not _nodes(tree, _attr("id", "co-chart"))
    assert "report.html is the record of truth" in _text(_nodes(tree, _attr("id", "analysis-body"))[0])


# --------------------------------------------------------------------------
# Real browser: no horizontal scroll at 390 px, CSS applied under the CSP
# --------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(900)
@needs_node
@pytest.mark.skipif(CHROME is None, reason="chrome-headless-shell is not cached")
@pytest.mark.parametrize("width", [390, 1280])
def test_real_browser_cell_origin_record_has_no_horizontal_scroll(
    planted, width: int
) -> None:
    setup, root, payload = planted
    record_id = _row(payload)["record_id"]
    code, imported = _json(
        setup.capsys, "catalog", "import", root / "records" / record_id, "--root", root
    )
    assert code == 0, imported
    with _serving(root) as (service, _):
        result = subprocess.run(
            [NODE, "tests/web/site_chrome_check.js", str(CHROME), service.launch_url, str(width),
             f"#/records/{record_id}", "#/"],
            capture_output=True, text=True, timeout=600, check=False,
        )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    states = [item["state"] for item in report["results"]]
    # The launch page first, then each route.
    assert states[0] == states[2] == "catalog:success", states
    assert states[1] in {"record:success", "record:partial"}, states
    for item in report["results"]:
        assert item["scrollWidth"] <= item["innerWidth"], item
        assert item["bannerBorder"] == "solid", item
    assert not [
        m for m in report["messages"] if "Content Security Policy" in m or m == "exception"
    ], report


def test_long_contributor_ids_wrap_instead_of_widening_the_table() -> None:
    """codex round 2: ``break-word`` keeps a long ID's min-content width at 390 px."""

    css = (Path("traceback_runner/web/static/styles.css")).read_text()
    assert ".co-table th, .co-table td { overflow-wrap: anywhere; }" in css
    site = Path("traceback_runner/web/static/site.js").read_text()
    assert 'class: "data-table co-table"' in site


@needs_node
@pytest.mark.parametrize("compact, limit", [(False, 60), (True, 30)])
def test_dom_bar_labels_start_at_the_left_edge_and_are_shortened(
    served, tmp_path: Path, compact: bool, limit: int
) -> None:
    """codex round 3: a right-aligned label ran off the start of the SVG."""

    view, measurement = served
    many = _many(measurement, n=30, zeros=11)
    long_id = "Type" + "W" * 40  # codex round 4: 44 wide glyphs overflowed 360 units
    first = many.estimates[-1].model_copy(update={"contributor_id": long_id})
    many = many.model_copy(update={"estimates": (*many.estimates[:-1], first)})
    tree = _dom(tmp_path, _with_body(view, many), compact=compact)["view"]
    labels = _nodes(_nodes(tree, _attr("id", "co-chart"))[0], _attr("class", "label-text"))
    assert len(labels) == 13
    for label in labels:
        assert label["attrs"]["x"] == "0" and "text-anchor" not in label["attrs"]
        shown = "".join(child for child in label["children"] if isinstance(child, str))
        assert len(shown) <= limit
        assert shown == label["attrs"]["data-full"] or shown.endswith("…")
    assert labels[-1]["attrs"]["data-full"] == "18 other contributors combined (11 at 0%)"
    assert any(label["attrs"]["data-full"] == long_id for label in labels)
