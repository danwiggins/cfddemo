"""Usability C0/C1/C3/C5: the local record routes and the records site.

Route tests run ``traceback serve`` over one generated ROOT (one run, imported)
and check the per-request ``LocalRecordView`` against the signed measurement,
the fail-closed 503 for a tampered bundle or a stale authority, and the
operator-only gate.  DOM tests drive ``chart.js`` + ``site.js`` in the offline
harness (``site_dom_harness.js``) through every state of the C0 table.
Everything here is generated, unqualified, local and not for clinical use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web.longitudinal_env import NODE, _contrast, needs_node
from tests.web.test_loopback_server import _exchange, _request
from tests.web.test_serve import _get, _main_json, _reader_cookie, _serving
from traceback_runner.contracts import parse_fragment_measurement
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.web.contracts import validate_public_text
from traceback_runner.web.records import read_record_label, valid_label
from traceback_runner.web.state_copy import ENUM_SOURCES, RECORD_AXES, STATE_COPY

STATIC = Path("traceback_runner/web/static")
HARNESS = Path("tests/web/site_dom_harness.js")
WEB_503 = {"error": {"code": "TBX-WEB-503"}}
AUTH_007 = {"error": {"code": "TBX-AUTH-007"}}


# --- C3: plain-language copy -----------------------------------------------------------


def test_every_enum_member_the_site_shows_has_plain_copy() -> None:
    for axis, tokens in ENUM_SOURCES.items():
        assert tokens, axis
        for token in tokens:
            label, meaning = STATE_COPY[axis][token]
            assert label and meaning.endswith("."), (axis, token)
    assert set(RECORD_AXES) <= set(STATE_COPY)


def test_state_copy_passes_the_public_text_boundary_and_stays_descriptive() -> None:
    forbidden = re.compile(r"\b(normal|abnormal|healthy|diagnos\w*|validated|cancer)\b", re.I)
    for axis, rows in STATE_COPY.items():
        for token, (label, meaning) in rows.items():
            for text in (label, meaning):
                validate_public_text(text)
                # A diagnosis is mentioned only to be denied, never claimed.
                assert not forbidden.search(text.replace("diagnosis or a", "")), (axis, token)


# --- labels (read side of A4b) ----------------------------------------------------------


def test_label_grammar_and_unsafe_label_files(tmp_path: Path) -> None:
    assert valid_label("plasma batch 2") == "plasma batch 2"
    assert valid_label("x" * 80) == "x" * 80
    for bad in ("", "x" * 81, " lead", "a/b", "a\\b", "tab\there", "..", "patient id 7", 7, None):
        assert valid_label(bad) is None, bad
    record_id = "record-" + "a" * 24
    labels = tmp_path / "labels"
    labels.mkdir()
    target = labels / f"{record_id}.json"
    assert read_record_label(tmp_path, record_id) is None
    target.write_text(json.dumps({"label": "run A", "set_at": "2026-10-04T00:00:00Z"}))
    assert read_record_label(tmp_path, record_id) == "run A"
    target.write_text("not json")
    assert read_record_label(tmp_path, record_id) is None
    target.unlink()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"label": "followed"}))
    target.symlink_to(elsewhere)
    assert read_record_label(tmp_path, record_id) is None
    assert read_record_label(tmp_path, "../escape") is None


# --- C1/C5 routes over a real served ROOT ---------------------------------------------


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("records-base")
    inputs = create_local_golden_path_inputs(work / "inputs")
    root = work / "root"
    code, payload = _main_json(
        "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    code, payload = _main_json("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == 0, payload
    record_id = payload["data"]["record_id"]
    code, payload = _main_json("catalog", "import", root / "records" / record_id, "--root", root)
    assert code == 0, payload
    return root, record_id, payload["data"]["result_id"]


@pytest.fixture
def world(base, tmp_path: Path):
    root, record_id, result_id = base
    copy_root = tmp_path / "root"
    shutil.copytree(root, copy_root, symlinks=True)
    return copy_root, record_id, result_id


def _signed_measurement(root: Path, record_id: str):
    path = root / "records" / record_id / "measurements" / "fragment-length.v1.json"
    return parse_fragment_measurement(path.read_bytes())


def test_record_view_counts_equal_the_signed_measurement(world) -> None:
    root, record_id, result_id = world
    measurement = _signed_measurement(root, record_id)
    (root / "labels").mkdir(mode=0o700)
    (root / "labels" / f"{record_id}.json").write_text(
        json.dumps({"label": "generated fixture", "set_at": "2026-10-04T00:00:00Z"})
    )
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        status, view = _get(service, f"/api/v1/records/{record_id}", cookie)
        assert status == 200, view
        assert view["record_id"] == record_id and view["result_id"] == result_id
        assert view["label"] == "generated fixture"
        counts = [row["count"] for row in view["histogram"]]
        assert counts == [item.count for item in measurement.histogram]
        assert sum(counts) == view["eligible_alignments"] == measurement.eligible_alignments
        assert view["records_scanned"] == measurement.records_scanned
        assert view["eligible_alignments"] + sum(
            row["count"] for row in view["exclusions"]
        ) == view["records_scanned"]
        assert view["policy"]["builtin"] is True and view["policy"]["min_mapq"] == 20
        assert [row["axis"] for row in view["states"]] == list(RECORD_AXES)
        assert {row["axis"]: row["token"] for row in view["states"]}["qualification"] == (
            "development_unqualified"
        )
        assert view["preflight"]["origin"] == "job_store"
        status, listing = _get(service, "/api/v1/records", cookie)
        assert status == 200
        (row,) = listing["records"]
        assert row["status"] == "verified" and row["label"] == "generated fixture"
        assert row["eligible_alignments"] == measurement.eligible_alignments
        assert {item["token"] for item in listing["job_states"]} >= {"complete", "running"}
        # No path or absolute location reaches the payload.
        assert str(root) not in json.dumps(view) + json.dumps(listing)


def _make_writable(path: Path) -> None:
    subprocess.run(["chmod", "-R", "u+w", str(path)], check=True)


def test_tampered_bundle_returns_503_and_no_counts(world) -> None:
    root, record_id, _ = world
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        assert _get(service, f"/api/v1/records/{record_id}", cookie)[0] == 200
        objects = root / "catalog" / "objects"
        _make_writable(objects)
        (measurement,) = objects.glob("*/measurements/fragment-length.v1.json")
        content = measurement.read_bytes()
        first = re.search(rb'"count":(\d+)', content)
        edited = content.replace(first.group(0), b'"count":' + str(int(first.group(1)) + 1).encode(), 1)
        measurement.write_bytes(edited)
        status, _, body = _request(
            service, "GET", f"/api/v1/records/{record_id}", headers={"Cookie": cookie}
        )
        assert (status, json.loads(body)) == (503, WEB_503)
        assert b"histogram" not in body
        status, listing = _get(service, "/api/v1/records", cookie)
        assert status == 200
        (row,) = listing["records"]
        assert row["status"] == "failed_verification"
        assert row["eligible_alignments"] is None and row["records_scanned"] is None


def test_stale_authority_returns_503(world) -> None:
    root, record_id, _ = world
    with _serving(root) as (service, _):
        cookie, _ = _exchange(service)
        assert _get(service, f"/api/v1/records/{record_id}", cookie)[0] == 200
        pins = root / "authority" / "ref" / "pins.json"
        os.chmod(pins, 0o600)
        pins.write_bytes(pins.read_bytes().replace(b'"', b"'", 1))
        status, _, body = _request(
            service, "GET", f"/api/v1/records/{record_id}", headers={"Cookie": cookie}
        )
        assert (status, json.loads(body)) == (503, WEB_503)


def test_reader_sessions_unknown_and_malformed_ids(world) -> None:
    root, record_id, _ = world
    with _serving(root) as (service, _):
        reader = _reader_cookie(service)
        for path in ("/api/v1/records", f"/api/v1/records/{record_id}"):
            assert _get(service, path, reader) == (403, AUTH_007)
        cookie, _ = _exchange(service, service.issue_bootstrap())
        status, problem = _get(service, "/api/v1/records/record-" + "0" * 24, cookie)
        assert status == 404 and problem["code"] == "TBX-WEB-404"
        for bad in (record_id.upper(), record_id + "0", "result_" + "0" * 40, "../x"):
            assert _get(service, f"/api/v1/records/{bad}", cookie)[0] == 404
        # No session at all: 401 before any read.
        status, _, _ = _request(service, "GET", "/api/v1/records")
        assert status == 401


# --- DOM harness: every C0 state ------------------------------------------------------

RID = ["record-" + str(digit) * 24 for digit in range(1, 5)]
EDGES = (0, 100, 150, 200, 300, 500, 1000)


def _view(record_id: str = RID[0], *, label=None, counts=(5, 10, 60, 10, 10, 4, 1),
          edges=EDGES, warnings=True, origin="job_store") -> dict:
    histogram = [
        {"lower": low, "upper": high, "count": count}
        for low, high, count in zip(edges, (*edges[1:], None), counts, strict=True)
    ]
    eligible = sum(counts)
    checks = (
        [{"code": "TBX-BAM-002", "outcome": "warn", "summary": "BAM header lacks M5, AS."}]
        if warnings and origin == "job_store"
        else []
    )
    preflight_token = ("warn" if checks else "pass") if origin == "job_store" else "not_available"
    states = [
        ("qualification", "development_unqualified", "Not qualified"),
        ("trust", "development_signature_verified", "Signature verified (development key)"),
        ("display_role", "research_baseline", "Research baseline"),
        ("reference_match", "name_and_length_only" if warnings else "registered_digests",
         "Matched by contig name and length only" if warnings else "Matched by contig checksums"),
        ("preflight", preflight_token, STATE_COPY["preflight"][preflight_token][0]),
        ("comparison", "not_compared", "Shown on its own"),
    ]
    return {
        "schema_version": "traceback.local-record-view.v1",
        "record_id": record_id,
        "short_id": record_id[7:19],
        "result_id": "result_" + "a" * 40,
        "label": label,
        "reference_id": "ref",
        "policy": {"id": "built-in", "builtin": True, "min_mapq": 20,
                   "bins": [{"lower": row["lower"], "upper": row["upper"]} for row in histogram]},
        "method_version": "1.0.0-local-ref",
        "records_scanned": eligible + 7,
        "eligible_alignments": eligible,
        "exclusions": [
            {"reason": "unmapped", "stage": "acceptance", "label": "Unmapped records", "count": 3},
            {"reason": "low_mapping_quality", "stage": "eligibility", "label": "Below MAPQ 20", "count": 4},
        ],
        "histogram": histogram,
        "measurement_sha256": "b" * 64,
        "preflight": {"outcome": preflight_token, "origin": origin, "checks": checks},
        "warnings": len(checks) if origin == "job_store" else int(warnings),
        "states": [
            {"axis": axis, "token": token, "label": text, "meaning": STATE_COPY[axis][token][1]}
            for axis, token, text in states
        ],
        "imported_at": "2026-10-04T02:48:54Z",
    }


def _summary(record_id: str, *, status="verified", label=None, minute=0) -> dict:
    verified = status == "verified"
    return {
        "record_id": record_id,
        "short_id": record_id[7:19],
        "status": status,
        "status_label": STATE_COPY["record_status"][status][0],
        "label": label,
        "reference_id": "ref" if verified else None,
        "policy_label": "built-in" if verified else None,
        "eligible_alignments": 100 if verified else None,
        "records_scanned": 107 if verified else None,
        "preflight": "warn" if verified else None,
        "preflight_label": "Preflight passed with warnings" if verified else None,
        "preflight_warnings": 1 if verified else None,
        "warning_texts": ["TBX-BAM-002: BAM header lacks M5, AS."] if verified else [],
        "method_version": "1.0.0-local-ref" if verified else None,
        "imported_at": f"2026-10-04T02:{minute:02d}:00Z" if verified else None,
    }


def _listing(*rows: dict) -> dict:
    return {
        "schema_version": "traceback.local-record-list.v1",
        "records": list(rows),
        "truncated": False,
        "job_states": [
            {"token": token, "label": label, "meaning": meaning}
            for token, (label, meaning) in STATE_COPY["job"].items()
        ],
    }


def _ok(payload) -> list[dict]:
    return [{"status": 200, "payload": payload}]


def _run(tmp_path: Path, scenario: dict) -> list[dict]:
    path = tmp_path / "site-scenario.json"
    path.write_text(json.dumps({"static": str(STATIC.absolute()), **scenario}))
    result = subprocess.run(
        [NODE, str(HARNESS), str(path)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["errors"] == []
    return report["reports"]


def _nodes(tree, predicate) -> list[dict]:
    found = []

    def visit(node) -> None:
        if isinstance(node, dict):
            if predicate(node):
                found.append(node)
            for child in node["children"]:
                visit(child)

    visit(tree)
    return found


def _text(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node["children"])


def _visible_text(node) -> str:
    """Text outside the collapsed exact-values disclosure."""

    if isinstance(node, str):
        return node
    if node["attrs"].get("id") == "exact-values":
        return ""
    return " ".join(_visible_text(child) for child in node["children"])


def _tag(name: str):
    return lambda node: node["tag"] == name


def _attr(name: str, value: str | None = None):
    return lambda node: name in node["attrs"] and (value is None or node["attrs"][name] == value)


@needs_node
def test_catalog_states_empty_error_success_partial_and_session(tmp_path: Path) -> None:
    empty = _run(tmp_path, {"responses": {"/api/v1/records": _ok(_listing())}})[0]
    assert empty["dataset"]["state"] == "empty" and not empty["siteHidden"]
    assert "No records yet. Run: traceback run BAM --reference ID --label NAME --import" in _text(empty["view"])
    error = _run(tmp_path, {"responses": {"/api/v1/records": [{"status": 500, "payload": {"error": {"code": "TBX-INTERNAL"}}}]}})[0]
    assert error["dataset"]["state"] == "error"
    assert "Could not read the catalog (TBX-INTERNAL).Run traceback doctor." in _text(error["view"])
    assert _nodes(error["view"], lambda n: n["tag"] == "button" and _text(n) == "Retry")
    rows = _listing(_summary(RID[0], minute=1), _summary(RID[1], status="failed_verification"),
                    _summary(RID[2], minute=3))
    partial = _run(tmp_path, {"responses": {"/api/v1/records": _ok(rows)}})[0]
    assert partial["dataset"]["state"] == "partial"
    failed = _nodes(partial["view"], _attr("data-status", "failed_verification"))[0]
    assert "Failed verification" in _text(failed) and "traceback verify ROOT/records/" in _text(failed)
    assert len(_nodes(partial["view"], _attr("data-status", "verified"))) == 2
    good = _run(tmp_path, {"responses": {"/api/v1/records": _ok(_listing(_summary(RID[0])))}})[0]
    assert good["dataset"]["state"] == "success"
    ended = _run(tmp_path, {"responses": {"/api/v1/records": [{"status": 401, "payload": {}}]},
                            "storage": {"traceback.compare-selection": json.dumps([RID[0]])}})[0]
    assert "Session ended. Run traceback serve again" in _text(ended["view"])
    assert ended["storage"] == {}


@needs_node
def test_checkbox_compare_enables_only_for_exactly_two(tmp_path: Path) -> None:
    rows = _listing(*(_summary(rid, minute=index) for index, rid in enumerate(RID[:3])))
    steps = [{"check": RID[0]}, {"check": RID[1]}, {"check": RID[2]}, {"check": RID[2], "value": False},
             {"click": {"id": "compare"}}]
    reports = _run(tmp_path, {"responses": {"/api/v1/records": _ok(rows)}, "steps": steps})

    def button(report):
        return _nodes(report["view"], _attr("id", "compare"))[0]

    def reason(report):
        return _text(_nodes(report["view"], _attr("id", "compare-reason"))[0])

    assert [button(item).get("disabled", False) for item in reports[:5]] == [True, True, False, True, False]
    assert reason(reports[0]) == "Select exactly 2 records (0 selected)"
    assert reason(reports[3]) == "Select exactly 2 records (3 selected)"
    assert button(reports[0])["attrs"]["aria-describedby"] == "compare-reason"
    boxes = _nodes(reports[0]["view"], _tag("input"))
    assert [box["attrs"]["aria-label"] for box in boxes] == [f"Compare {rid[7:19]}" for rid in RID[:3]]
    compare = reports[5]
    assert compare["dataset"]["view"] == "compare"
    assert f"#/records/{RID[0]}" in json.dumps(compare["view"])  # A = earlier import
    assert "not built yet" in _text(compare["view"])
    assert json.loads(compare["storage"]["traceback.compare-selection"]) == RID[:2]


@needs_node
def test_selection_survives_back_and_failed_rows_cannot_be_selected(tmp_path: Path) -> None:
    rows = _listing(_summary(RID[0], minute=1), _summary(RID[1], status="failed_verification"))
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(rows), f"/api/v1/records/{RID[0]}": _ok(_view())},
        "steps": [{"check": RID[0]}, {"hash": f"#/records/{RID[0]}"}, {"hash": "#/"}],
    })
    boxes = _nodes(reports[-1]["view"], _tag("input"))
    assert boxes[0].get("checked") is True
    assert boxes[1].get("disabled") is True and not boxes[1].get("checked")


@needs_node
def test_eighty_character_label_and_no_free_text_inputs(tmp_path: Path) -> None:
    label = "L" * 80
    report = _run(tmp_path, {"responses": {"/api/v1/records": _ok(_listing(_summary(RID[0], label=label)))}})[0]
    link = _nodes(report["view"], _attr("href", f"#/records/{RID[0]}"))[0]
    assert _text(link) == label
    assert RID[0][7:19] in _text(report["view"])  # the short ID stays beside the label
    inputs = _nodes(report["view"], _tag("input"))
    assert all(node["attrs"]["type"] == "checkbox" for node in inputs)
    selects = _nodes(report["view"], _tag("select"))
    assert [node["attrs"]["id"] for node in selects] == ["filter-method", "filter-policy"]


@needs_node
def test_record_view_histogram_svg_table_footnote_and_focus(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      f"/api/v1/records/{RID[0]}": _ok(_view(label="batch two"))},
        "steps": [{"hash": f"#/records/{RID[0]}"}],
    })
    record = reports[1]
    assert record["dataset"] == {"view": "record", "state": "partial"}
    assert record["focused"] == "view-title"
    view = record["view"]
    assert _text(_nodes(view, _tag("h1"))[0]) == "batch two"
    (svg,) = _nodes(view, _attr("class", "chart-svg"))
    assert svg["attrs"]["role"] == "img" and svg["attrs"]["aria-describedby"] == "hist-table"
    bars = _nodes(svg, lambda n: n["tag"] == "rect" and "bar" in n["attrs"].get("class", "").split())
    assert len(bars) == len(EDGES)
    assert bars[-1]["attrs"]["class"] == "bar bar-open" and "hatch" in bars[-1]["attrs"]["fill"]
    assert [b["attrs"]["data-most-common"] for b in bars].count("true") == 1
    assert bars[2]["attrs"]["data-most-common"] == "true"
    labels = _nodes(svg, _attr("class", "bar-label"))
    assert len(labels) == len(EDGES) and "most common bin" in _text(labels[2])
    table = _nodes(view, _attr("id", "hist-table"))[0]
    assert len(_nodes(table, _attr("data-bin-row"))) == len(EDGES)
    assert "open bin, width not defined; share is exact" in _text(table)
    assert _nodes(view, _attr("data-footnote", "open-bin"))
    text = _text(view)
    assert "Aligned reference span (bp)" in text and "Share of eligible alignments per bp" in text
    assert "n = 100 eligible alignments; policy built-in (MAPQ ≥ 20)." in text
    # Warnings are listed above the chart (partial preflight).
    order = json.dumps(view)
    assert order.index("record-warnings") < order.index("chart-svg")
    assert "60.0% of eligible alignments have an aligned reference span of 150 to 199 bp" in text


@needs_node
def test_compact_width_drops_bar_labels_and_thins_ticks(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "compact": True,
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      f"/api/v1/records/{RID[0]}": _ok(_view())},
        "steps": [{"hash": f"#/records/{RID[0]}"}, {"compact": False}],
    })
    compact, wide = reports[1]["view"], reports[2]["view"]
    assert not _nodes(compact, _attr("class", "bar-label"))
    ticks = [_text(n) for n in _nodes(compact, lambda n: n["tag"] == "text" and "tick-label" in n["attrs"].get("class", "") and n["attrs"].get("text-anchor") == "middle")]
    assert ticks == ["0", "200", "500", "1,000", "1,000+ (open)"]
    assert len(_nodes(wide, _attr("class", "bar-label"))) == len(EDGES)


@needs_node
def test_thousand_bin_policy_draws_one_line_and_a_grouped_table(tmp_path: Path) -> None:
    edges = tuple(range(0, 1001))
    counts = tuple((index % 7) + 1 for index in range(len(edges)))
    view = _view(edges=edges, counts=counts, warnings=False)
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      f"/api/v1/records/{RID[0]}": _ok(view)},
        "steps": [{"hash": f"#/records/{RID[0]}"}, {"click": {"tag": "button", "attr": ["aria-controls", "histogram-table"]}}],
    })
    record = reports[1]["view"]
    (svg,) = _nodes(record, _attr("class", "chart-svg"))
    assert len(_nodes(svg, _tag("path"))) == 1
    assert not _nodes(svg, lambda n: n["tag"] == "rect" and "bar" in n["attrs"].get("class", ""))
    assert not _nodes(svg, _attr("class", "bar-label"))
    table = _nodes(record, _attr("id", "hist-table"))[0]
    assert len(_nodes(table, _attr("data-bin-row"))) == 101
    every = _nodes(reports[2]["view"], _attr("id", "hist-table"))[0]
    assert len(_nodes(every, _attr("data-bin-row"))) == 1001


@needs_node
def test_record_error_and_empty_states(tmp_path: Path) -> None:
    missing = "record-" + "9" * 24
    empty_view = _view(RID[2], counts=(0, 0, 0, 0, 0, 0, 0), warnings=False)
    reports = _run(tmp_path, {
        "responses": {
            "/api/v1/records": _ok(_listing(_summary(RID[0]))),
            f"/api/v1/records/{missing}": [{"status": 404, "payload": {"code": "TBX-WEB-404"}}],
            f"/api/v1/records/{RID[1]}": [{"status": 503, "payload": WEB_503}],
            f"/api/v1/records/{RID[2]}": _ok(empty_view),
        },
        "steps": [{"hash": f"#/records/{missing}"}, {"hash": f"#/records/{RID[1]}"}, {"hash": f"#/records/{RID[2]}"}],
    })
    not_found, failed, empty = reports[1], reports[2], reports[3]
    assert not_found["dataset"]["state"] == "error"
    assert "No record with this ID" in _text(not_found["view"])
    assert _nodes(not_found["view"], _attr("href", "#/"))
    assert failed["dataset"]["state"] == "error"
    assert "This record failed verification. Nothing is shown." in _text(failed["view"])
    assert "traceback verify" in _text(failed["view"])
    assert not _nodes(failed["view"], _tag("svg"))
    assert empty["dataset"]["state"] == "empty"
    assert "No eligible alignments; see the denominator strip." in _text(empty["view"])


@needs_node
def test_denominator_strip_reconciles_and_tokens_stay_in_exact_values(tmp_path: Path) -> None:
    view = _view()
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      f"/api/v1/records/{RID[0]}": _ok(view)},
        "steps": [{"hash": f"#/records/{RID[0]}"}],
    })
    record = reports[1]["view"]
    items = _nodes(record, _attr("data-strip"))
    by_kind = {}
    for item in items:
        by_kind.setdefault(item["attrs"]["data-strip"], []).append(int(item["attrs"]["data-count"]))
    assert by_kind["scanned"][0] == sum(by_kind["excluded"]) + by_kind["eligible"][0]
    assert "100 of 107 (93.5%)" in _text(record)
    visible = _visible_text(record)
    for axis in RECORD_AXES:
        token = next(row["token"] for row in view["states"] if row["axis"] == axis)
        if axis == "comparison" or "_" in token:
            assert token not in visible, token
    exact = _nodes(record, _attr("id", "exact-values"))[0]
    assert "development_unqualified" in _text(exact)
    rows = _nodes(_nodes(record, _attr("id", "states"))[0], _attr("data-axis"))
    assert [row["attrs"]["data-axis"] for row in rows] == list(RECORD_AXES)
    assert "How to read it (descriptive, not diagnostic)" in _text(record)


@needs_node
def test_a_slow_earlier_route_never_overwrites_the_current_one(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "responses": {
            "/api/v1/records": _ok(_listing(_summary(RID[0]), _summary(RID[1]))),
            f"/api/v1/records/{RID[0]}": [{"status": 200, "payload": _view(RID[0], label="slow A"), "delayMs": 300}],
            f"/api/v1/records/{RID[1]}": _ok(_view(RID[1], label="fast B")),
        },
        "steps": [{"hashes": [f"#/records/{RID[0]}", f"#/records/{RID[1]}"]}],
    })
    final = reports[1]
    assert _text(_nodes(final["view"], _tag("h1"))[0]) == "fast B"
    assert "slow A" not in _text(final["view"])


@needs_node
def test_row_warnings_expand_and_compare_ignores_rows_that_failed_later(tmp_path: Path) -> None:
    first = _listing(*(_summary(rid, minute=i) for i, rid in enumerate(RID[:3])))
    later = _listing(_summary(RID[0], minute=0), _summary(RID[1], minute=1),
                     _summary(RID[2], status="failed_verification"))
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": [{"status": 200, "payload": first}, {"status": 200, "payload": later}]},
        "steps": [{"check": RID[0]}, {"check": RID[1]}, {"check": RID[2]},
                  {"click": {"id": "refresh"}}, {"click": {"id": "compare"}}],
    })
    details = _nodes(reports[0]["view"], _attr("class", "row-warnings"))
    assert len(details) == 3 and "TBX-BAM-002" in _text(details[0])
    after_refresh = reports[4]
    assert not _nodes(after_refresh["view"], _attr("id", "compare"))[0].get("disabled")
    assert json.loads(after_refresh["storage"]["traceback.compare-selection"]) == RID[:2]
    assert reports[5]["dataset"]["view"] == "compare"


@needs_node
def test_compare_hook_states(tmp_path: Path) -> None:
    rows = _listing(_summary(RID[0], minute=1), _summary(RID[1], status="failed_verification"))
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(rows)},
        "steps": [{"hash": "#/compare?a=" + RID[0]}, {"hash": f"#/compare?a={RID[0]}&b={RID[1]}"}],
    })
    assert reports[1]["dataset"]["state"] == "empty"
    assert "Select exactly 2 different records" in _text(reports[1]["view"])
    assert reports[2]["dataset"]["state"] == "partial"
    assert "Failed verification" in _text(reports[2]["view"])


@needs_node
def test_jobs_disclosure_words(tmp_path: Path) -> None:
    def jobs(*items):
        return {"jobs": [
            {"job_id": "job_" + "0" * 32, "state": state, "stage_label": stage, "stale": stale,
             "updated_at": "2026-10-04T02:00:00Z", "headline": "Runner status is stale"}
            for state, stage, stale in items
        ]}

    listing = {"/api/v1/records": _ok(_listing())}
    cases = [
        (jobs(), "No job has run on this ROOT"),
        (jobs(("complete", "sign", True)), "Last job finished (signed record written); no job is running"),
        (jobs(("terminal_failure", "measure", True)), "Last job: Failed; no job is running"),
        (None, "Job status unavailable"),
    ]
    for payload, expected in cases:
        report = _run(tmp_path, {"responses": listing, "jobs": payload})[0]
        assert report["jobsSummary"] == expected
        assert "stale" not in " ".join(report["jobs"]) and "out of date" not in " ".join(report["jobs"])
    running = _run(tmp_path, {"responses": listing, "jobs": jobs(("measuring", "measure", True))})[0]
    assert running["jobsSummary"].startswith("Running: Measuring, stage measure (last update ")
    assert running["jobsSummary"].endswith("status may be out of date")


@needs_node
def test_keyboard_journey_uses_native_controls_and_moves_focus(tmp_path: Path) -> None:
    rows = _listing(_summary(RID[0], minute=1), _summary(RID[1], minute=2))
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(rows),
                      f"/api/v1/records/{RID[0]}": _ok(_view())},
        "steps": [{"hash": f"#/records/{RID[0]}"}, {"hash": "#/"}, {"check": RID[0]}, {"check": RID[1]},
                  {"click": {"id": "compare"}}, {"hash": "#/"}],
    })
    # Every interactive element is a native, keyboard-operable control.
    for report in reports:
        for node in _nodes(report["view"], lambda n: n["tag"] in {"div", "span", "td", "tr", "li"}):
            assert "onclick" not in node["attrs"] and node["attrs"].get("role") != "button"
    assert [r["focused"] for r in reports[1:3]] == ["view-title", "view-title"]
    assert reports[5]["dataset"]["view"] == "compare" and reports[5]["focused"] == "view-title"
    assert reports[6]["dataset"]["view"] == "catalog"


@needs_node
def test_window_focus_refreshes_without_moving_focus(tmp_path: Path) -> None:
    reports = _run(tmp_path, {
        "responses": {"/api/v1/records": _ok(_listing(_summary(RID[0]))),
                      "/api/v1/jobs": _ok({"jobs": []})},
        "steps": [{"windowFocus": True}],
    })
    paths = [item["path"] for item in reports[1]["requests"]]
    assert set(paths) == {"/api/v1/records", "/api/v1/jobs"}
    assert reports[1]["focused"] is None


# --- real browser: CSP-styled, no horizontal scroll at 390 px -----------------------

CHROME = next(
    iter(
        sorted(
            Path.home().glob(
                "Library/Caches/ms-playwright/chromium_headless_shell-*/"
                "chrome-headless-shell-*/chrome-headless-shell"
            )
        )
    ),
    None,
)


@needs_node
@pytest.mark.skipif(CHROME is None, reason="chrome-headless-shell is not cached")
@pytest.mark.parametrize("width", [390, 1280])
def test_real_browser_has_no_horizontal_scroll_and_packaged_css_applies(world, width: int) -> None:
    root, record_id, _ = world
    with _serving(root) as (service, _):
        result = subprocess.run(
            [NODE, "tests/web/site_chrome_check.js", str(CHROME), service.launch_url, str(width),
             f"#/records/{record_id}", f"#/compare?a={record_id}&b=record-{'0' * 24}",
             f"#/records/record-{'0' * 24}", "#/"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    states = [item["state"] for item in report["results"]]
    assert states[0] == states[4] == "catalog:success", states
    assert states[1] in {"record:success", "record:partial"}, states
    assert states[2:4] == ["compare:partial", "record:error"], states
    for item in report["results"]:
        assert item["scrollWidth"] <= item["innerWidth"], item
        assert item["bannerBorder"] == "solid", item  # styles.css applied under the CSP
    assert not [m for m in report["messages"] if "Content Security Policy" in m or m == "exception"], report


# --- static assets and tokens --------------------------------------------------------


def test_packaged_site_assets_are_offline_and_free_of_inline_style() -> None:
    for name in ("site.js", "chart.js", "index.html", "styles.css"):
        content = (STATIC / name).read_text()
        lowered = content.lower()
        assert "http://" not in lowered and "https://" not in lowered and "//cdn" not in lowered
        assert "style=" not in content and ".style." not in content and "innerHTML" not in content
    html = (STATIC / "index.html").read_text()
    assert "Development records · unqualified · not for clinical use" in html
    assert html.index("/assets/site.js") < html.index("/assets/app.js")


def test_design_tokens_meet_contrast() -> None:
    css = (STATIC / "styles.css").read_text()
    for token in ("--chart-a", "--chart-b", "--chart-hatch", "--neutral", "--focus"):
        value = re.search(rf"{token}:\s*(#[0-9a-f]{{6}})", css).group(1)
        assert _contrast(value, "#ffffff") >= 4.5, token
    assert "#d88400" not in css


def test_streamlit_demo_no_longer_says_validated() -> None:
    lines = [
        number
        for number, line in enumerate(Path("app.py").read_text().splitlines(), start=1)
        if "Validated" in line
    ]
    # Only the "Not built" disclaimer remains.
    assert len(lines) == 1
    assert "Validated tumor-fraction calling" in Path("app.py").read_text().splitlines()[lines[0] - 1]

