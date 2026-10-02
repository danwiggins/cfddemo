"""E12 read-only routes and the packaged view over one shared world.

Bare B01 sessions, injected roles and wrong scopes get the bounded denial
before any read; selector, diff, workspace and source-detail routes render only
public projections; the packaged view's nine states, separate SVG series,
unequal x spacing, responsive drawer and focus containment run under an
offline DOM harness (plan items 12 and 19).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.web.longitudinal_env import (
    DENIED,
    PREFIX,
    Env,
    _assert_denied,
    _assert_no_protected,
    _call_route,
    _cohort_query,
    _comparison_row,
    _controller_layout,
    _every_route,
    _http,
    _journey_responses,
    _journey_steps,
    _json,
    _record_reads,
    _run_harness,
    _segment,
    _variant,
    _workspace,
    longitudinal_action,
    needs_node,
    shared_env,
)
from traceback_runner.web.longitudinal import validate_longitudinal_public


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory):
    yield from shared_env(tmp_path_factory)


def test_bare_b01_session_gets_the_same_bounded_denial_before_any_read(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_reads(monkeypatch)
    cookie, csrf = env.unbound()
    malformed = [
        ("GET", "selectors", "cohort_selector_id=bad"),
        ("GET", "selectors", "role=longitudinal_reader"),
        ("GET", "diff", "nonsense=1"),
        ("POST", "workspace", {"request": {"role": "longitudinal_reader"}}),
        ("POST", "workspace", {"request": env.request_json, "grant": "x"}),
        ("POST", "source", {"request": env.request_json, "row_ordinal": 999_999}),
        ("POST", "reopen", {"saved_selector_id": "x"}),
    ]
    for method, route, value in [*_every_route(env), *malformed]:
        status, content = _call_route(
            env, method, route, value, cookie=cookie, csrf=csrf
        )
        _assert_denied(status, content)
    # Only the lock-free identity binding the gate needs was read; no selector,
    # registry, saved object or workspace read happened.
    assert set(calls) <= {"ResultViewSourceRegistry.registry_identity"}


def test_b01_transport_checks_still_run_first(env: Env) -> None:
    # No session at all: B01's own errors, not the reader shell.
    status, content = _http(env.service, "GET", PREFIX + "selectors", {})
    assert status in {401, 403} and b"TBX-AUTH" in content
    # POST without Origin/CSRF is refused by B01 before the reader gate.
    status, content = _http(
        env.service,
        "POST",
        PREFIX + "workspace",
        {"Cookie": env.cookie},
        {"request": env.request_json},
    )
    assert status == 403 and b"TBX-AUTH" in content


def test_caller_cannot_inject_role_scope_or_grant(env: Env) -> None:
    request = env.request_json
    for payload in (
        {"request": request, "role": "longitudinal_reader"},
        {"request": {**request, "grant_sha256": "0" * 64}},
        {"request": {**request, "scope": ["anything"]}},
        {"request": request, "release": True},
    ):
        status, content = env.post("workspace", payload)
        assert status == 400, content
        assert _json(content)["error"]["code"] == "invalid_request"
    status, content = _http(
        env.service,
        "GET",
        PREFIX + "selectors?" + _cohort_query(env, role="longitudinal_reader"),
        {"Cookie": env.cookie, "X-Traceback-Role": "longitudinal_reader"},
    )
    assert status == 400


def test_wrong_scope_and_wrong_measurement_are_denied(env: Env) -> None:
    request = env.request_json
    request["measurement"]["quantity_id"] = "qty_fragment_other"
    for route, payload in (
        ("workspace", {"request": request}),
        ("source", {"request": request, "row_ordinal": 1}),
        ("save", {"request": request}),
    ):
        _assert_denied(*env.post(route, payload))
    _assert_denied(
        *env.get("diff", _cohort_query(env, quantity_id="qty_fragment_other"))
    )
    _assert_denied(
        *env.get("selectors", _cohort_query(env, quantity_id="qty_fragment_other"))
    )


def test_selector_journey_is_bounded_and_opaque(env: Env) -> None:
    status, content = env.get("selectors")
    assert status == 200
    first = _json(content)
    assert first["step"] == "cohorts"
    assert first["free_form_identity_accepted"] is False
    assert [item["cohort_selector_id"] for item in first["cohorts"]] == [
        env.world.request.cohort_selector_id
    ]
    assert first["save"]["state"] == "available"
    status, content = env.get(
        "selectors",
        _cohort_query(
            env,
            anchor_policy_selector_id=env.world.request.anchor_policy_selector_id,
            anchor_policy_version="1",
        ),
    )
    assert status == 200, content
    step = _json(content)
    request = env.world.request
    assert step["step"] == "anchor_candidates"
    [measurement] = step["measurement_options"]
    assert measurement["measurement"] == json.loads(
        request.measurement.model_dump_json()
    )
    assert (
        measurement["projection_policy_selector_id"]
        == request.projection_policy_selector_id
    )
    page = step["anchor_candidates"]
    assert page["explicit_selection_required"] is True
    assert page["candidate_page_sha256"] == request.anchor_candidate_page_sha256
    assert [item["anchor_selector_id"] for item in page["candidates"]] == [
        request.anchor_selector_id
    ]
    assert [item["d09_policy_selector_id"] for item in step["d09_policies"]] == [
        request.d09_policy_selector_id
    ]
    # Free-form or foreign identities are rejected, never resolved.
    for query in (
        _cohort_query(env, cohort_selector_id="cohort_selector_" + "f" * 40),
        _cohort_query(env, cohort_version="7"),
        _cohort_query(
            env,
            anchor_policy_selector_id="anchor_policy_" + "f" * 40,
            anchor_policy_version="1",
        ),
        "cohort_selector_id=" + request.cohort_selector_id,
        "limit=1000",
    ):
        status, content = env.get("selectors", query)
        assert status in {400, 409}, (query, content)
        assert b"cohort_selector_" + b"f" * 40 not in content


def test_version_diff_precedes_results_and_carries_counts_only(env: Env) -> None:
    status, content = env.get("diff", _cohort_query(env))
    assert status == 200
    payload = _json(content)
    diff = payload["diff"]
    assert payload["shown_before_results"] is True
    assert diff["kind"] == "initial_version" and diff["selected_version"] == 1
    assert diff["silent_upgrade"] is False
    assert diff["added_member_count"] == len(env.world.manifest.members)
    _assert_no_protected(env, content)


def test_workspace_renders_only_the_public_projection(env: Env) -> None:
    payload = _workspace(env)
    workspace = payload["workspace"]
    assert payload["release_control"] == "disabled"
    assert payload["export_control"] == "disabled"
    assert workspace["schema_version"] == (
        "traceback.e12-longitudinal-workspace-projection.v1"
    )
    for name in (
        "protected_rows",
        "reader_authorization",
        "dependency_heads",
        "visible_row_ordinals",
    ):
        assert name not in workspace
    assert workspace["product_release_authorized"] is False
    assert workspace["release_export_authorized"] is False
    assert workspace["diagnostic_interpretation_allowed"] is False
    assert all(
        item["slot"] != "reader_authorization"
        for item in workspace["authority"]["heads"]
    )
    validate_longitudinal_public(payload)


def test_source_detail_reresolves_one_row(env: Env) -> None:
    status, content = env.post(
        "source", {"request": env.request_json, "row_ordinal": 3}
    )
    assert status == 200, content
    detail = _json(content)
    workspace = _workspace(env)["workspace"]
    assert detail["row"] == next(r for r in workspace["rows"] if r["row_ordinal"] == 3)
    assert [s["to_row_ordinal"] for s in detail["segments"]] == [3]
    assert detail["replay_sha256"] == workspace["replay_sha256"]
    assert detail["denominator_ledger_label"] == "operator-entered, unverified"
    status, content = env.post(
        "source", {"request": env.request_json, "row_ordinal": 99}
    )
    assert status == 400


def test_no_protected_identifier_reaches_any_route(env: Env) -> None:
    for method, route, value in _every_route(env):
        if route in {"save"}:
            continue
        status, content = _call_route(env, method, route, value)
        assert status in {200, 403, 409}, (route, content)
        _assert_no_protected(env, content)
    # The packaged page and assets carry no protected identifier either.
    for asset in (
        "/",
        "/assets/app.js",
        "/assets/longitudinal.js",
        "/assets/styles.css",
    ):
        status, content = _http(env.service, "GET", asset)
        assert status == 200
        _assert_no_protected(env, content)


def test_no_route_accepts_an_action_or_decision(env: Env) -> None:
    request = env.request_json
    for payload in (
        {"request": request, "action": "review_registered_bridge"},
        {"request": request, "next_action": "request_reanalysis"},
        {"request": {**request, "compatibility_outcome": "equivalent"}},
        {"request": {**request, "segments": []}},
    ):
        status, content = env.post("workspace", payload)
        assert status == 400, content
    rows = _workspace(env)["workspace"]["rows"]
    assert {row["bridge_execution_state"] for row in rows} == {"not_executed"}


def test_assets_are_served_with_the_page(env: Env) -> None:
    status, content = _http(env.service, "GET", "/assets/longitudinal.js")
    assert status == 200 and b"TracebackLongitudinal" in content
    status, content = _http(env.service, "GET", "/")
    assert b'src="/assets/longitudinal.js"' in content


@needs_node
def test_view_states_series_spacing_drawer_and_focus(env: Env, tmp_path: Path) -> None:
    real = _workspace(env)["workspace"]
    success = _workspace(env, record_availability=["available"])["workspace"]
    empty = _workspace(env, lineage_roles=["technical_replicate"])["workspace"]
    anchor = next(
        r for r in real["rows"] if r["comparison_state"] == "anchor_reference"
    )
    member = next(r for r in real["rows"] if r["comparison_state"] == "available")
    # Unequal intervals: offsets 0, 1 day and 3 days.
    third = _comparison_row(member, 5, 3, 3 * 86_400, 0.6)
    chained = _variant(
        real,
        [anchor, member, third],
        [_segment(anchor, member), _segment(member, third)],
    )
    broken = _variant(real, [anchor, member, third], [_segment(anchor, member)])
    fourth = _comparison_row(member, 6, 4, 5 * 86_400, 0.7)
    disjoint = _variant(
        real,
        [anchor, member, third, fourth],
        [_segment(anchor, member), _segment(third, fourth)],
    )
    no_segments = _variant(real, [anchor, member], [])
    revoked = _variant(
        real,
        [
            anchor,
            member,
            {
                **next(
                    r for r in real["rows"] if r["record_availability"] == "missing"
                ),
                "record_availability": "withheld",
                "withheld_reason": "result_key_revoked",
            },
        ],
        real["segments"],
    )
    outcomes = [
        "equivalent",
        "qualified_compatible",
        "requires_reanalysis",
        "registered_bridge",
        "incompatible",
        "unknown",
    ]
    six = _variant(
        real,
        [anchor]
        + [
            {
                **member,
                "row_ordinal": index + 2,
                "compatibility_state": outcome,
                "next_action": longitudinal_action(outcome),
                "comparison_state": "suppressed",
                "comparison": None,
                "suppression_reasons": ["d03_not_comparable"],
            }
            for index, outcome in enumerate(outcomes)
        ],
        [],
    )
    workspaces = [
        real,
        success,
        empty,
        chained,
        broken,
        disjoint,
        no_segments,
        revoked,
        six,
    ]
    report = _run_harness(tmp_path, {"pure": {"workspaces": workspaces}})["pure"]
    assert report["states"] == [
        "loading",
        "empty",
        "error",
        "success",
        "partial",
        "stale",
        "revoked",
        "permission-denied",
        "slow-stage",
    ]
    assert report["actions"] == {o: longitudinal_action(o) for o in outcomes}
    assert report["classify"][:3] == ["partial", "success", "empty"]
    assert report["classify"][7] == "revoked"
    charts = report["charts"]
    # Separate SVG paths per authorized series; lines only from segments.
    assert [len(c["series"]) for c in charts[3:7]] == [1, 1, 2, 0]
    assert charts[4]["unconnected"] == [5]
    assert charts[6]["unconnected"] == [anchor["row_ordinal"], member["row_ordinal"]]
    xs = {p["row_ordinal"]: p["x"] for p in charts[3]["points"]}
    first_gap = xs[member["row_ordinal"]] - xs[anchor["row_ordinal"]]
    second_gap = xs[5] - xs[member["row_ordinal"]]
    assert second_gap == pytest.approx(2 * first_gap)
    renders = report["renders"]
    paths = lambda render: (
        json.dumps(render["chart"]).count('"tag": "PATH"')
        + json.dumps(render["chart"]).count('"tag":"PATH"')
    )
    assert [paths(r) for r in renders[3:7]] == [1, 1, 2, 0]
    # Native table semantics and controlled state text.
    rows_text = renders[0]["rowsText"]
    assert "anchor reference" in rows_text and "not a denominator unit" in rows_text
    assert "suppressed:" in rows_text and "biological unit" in rows_text
    assert '"scope": "row"' in json.dumps(renders[0]["rows"])
    six_text = renders[8]["rowsText"] + renders[8]["outcomeText"]
    for outcome in outcomes:
        assert longitudinal_action(outcome).replace("_", " ") in six_text
    assert "bridge" in six_text and "executed" not in renders[8]["outcomeText"].replace(
        "never executed", ""
    )
    assert "operator-entered, unverified" in renders[0]["covariatesText"]
    assert "not a comparison gate" in renders[0]["stripText"]
    assert "not authorized" in renders[0]["identityText"]
    assert report["drawer"] == [
        "beside",
        "beside",
        "overlay",
        "overlay",
        "overlay",
        "sheet",
        "sheet",
    ]
    assert report["focus"] == {
        "count": 3,
        "tabFromLast": "first",
        "shiftTabFromFirst": "last",
        "tabFromMiddle": None,
        "fromOutside": "first",
    }
    problems = {p["code"]: p for p in report["problems"]}
    assert problems["permission_denied"]["remediation"].startswith("Ask the operator")
    assert "1,000-object bound" in problems["save_unavailable"]["remediation"]


@needs_node
@pytest.mark.parametrize("width", [1440, 800, 375])
def test_controller_journey_diff_first_and_responsive_drawer(
    env: Env, tmp_path: Path, width: int
) -> None:
    report = _run_harness(
        tmp_path,
        {
            "controller": {
                "layout": _controller_layout(),
                "responses": _journey_responses(env),
                "steps": _journey_steps(env, width),
            }
        },
    )
    snaps = {s["label"]: s for s in report["snapshots"]}
    routes = [f["url"].split("?")[0] for f in report["fetches"]]
    # Results are never requested before the version diff.
    assert routes.index("/api/v1/longitudinal/diff") < routes.index(
        "/api/v1/longitudinal/workspace"
    )
    assert snaps["submit-before-diff"]["showResultsDisabled"] is True
    assert snaps["submit-before-diff"]["resultsHidden"] is True
    assert snaps["selectors"]["options"]["lg-anchor"][0] == ""  # explicit choice
    results = snaps["results"]
    assert results["state"] == "partial" and results["resultsHidden"] is False
    assert results["rows"] == len(_workspace(env)["workspace"]["rows"])
    assert results["paths"] == 1
    assert results["releaseDisabled"] is True and results["exportDisabled"] is True
    assert results["saveDisabled"] is False
    assert "operator-entered, unverified" in results["resultsText"]
    for fetch in report["fetches"]:
        if fetch["method"] == "POST":
            assert fetch["csrf"] == "csrf-token"
            assert set(fetch["body"]) <= {"request", "row_ordinal"}
    drawer = snaps["drawer"]
    mode = {1440: "beside", 800: "overlay", 375: "sheet"}[width]
    assert drawer["drawerHidden"] is False and drawer["drawerMode"] == mode
    assert drawer["drawerModal"] == ("true" if mode == "sheet" else "false")
    assert drawer["active"] == "lg-drawer-close"
    if mode == "sheet":
        # Focus is contained: Tab from the only control stays inside the sheet.
        assert snaps["tab"]["active"] == "lg-drawer-close"
    closed = snaps["closed"]
    assert closed["drawerHidden"] is True
    assert closed["active"].startswith("Details for row")  # focus restored


@needs_node
def test_controller_loading_slow_error_denied_and_stale_states(
    env: Env, tmp_path: Path
) -> None:
    step1 = _json(env.get("selectors")[1])
    layout = _controller_layout()
    report = _run_harness(
        tmp_path,
        {
            "controller": {
                "layout": layout,
                "responses": {
                    "/api/v1/longitudinal/selectors": [{"pending": True}],
                    "/api/v1/longitudinal/saved": [{"pending": True}],
                },
                "steps": [
                    {"do": "bind", "snapshot": "loading"},
                    {"do": "tick", "ms": 1000, "snapshot": "loading-1s"},
                    {"do": "tick", "ms": 2000, "snapshot": "slow"},
                    {"do": "hide", "snapshot": "away"},
                    {
                        "do": "respond",
                        "route": "/api/v1/longitudinal/selectors",
                        "response": {"status": 403, "payload": DENIED},
                    },
                    {"do": "bind", "snapshot": "denied"},
                    {
                        "do": "respond",
                        "route": "/api/v1/longitudinal/selectors",
                        "response": {
                            "status": 409,
                            "payload": {
                                "error": {
                                    "code": "authority_stale",
                                    "remediation": "retry_read",
                                }
                            },
                        },
                    },
                    {"do": "bind", "snapshot": "error"},
                    {
                        "do": "respond",
                        "route": "/api/v1/longitudinal/selectors",
                        "response": {"status": 200, "payload": step1},
                    },
                    {"do": "click", "id": "lg-retry", "snapshot": "retried"},
                ],
            }
        },
    )
    snaps = {s["label"]: s for s in report["snapshots"]}
    assert snaps["loading"]["state"] == "loading" and snaps["loading"]["busy"] == "true"
    assert snaps["loading"]["status"] == "Loading: cohort selectors (0 s elapsed)"
    assert snaps["loading-1s"]["status"] == "Loading: cohort selectors (1 s elapsed)"
    assert snaps["slow"]["state"] == "slow-stage"
    assert (
        "cohort selectors" in snaps["slow"]["status"]
        and "3 s elapsed" in snaps["slow"]["status"]
    )
    assert "%" not in snaps["slow"]["status"]
    assert (
        snaps["slow"]["resultsHidden"] is True
    )  # no stale chart relabelled as current
    denied = snaps["denied"]
    assert denied["state"] == "permission-denied" and denied["problemHidden"] is False
    assert denied["retryHidden"] is True
    assert not re.search(r"\d", denied["status"] + denied["problem"] + denied["fix"])
    error = snaps["error"]
    assert error["state"] == "error" and error["problem"].startswith("authority_stale:")
    assert error["fix"] == "Retry the read." and error["retryHidden"] is False
    assert snaps["retried"]["state"] == "empty"
    assert snaps["retried"]["options"]["lg-cohort"][1].startswith(
        env.world.request.cohort_selector_id
    )
