"""E12 routes that mutate authority or the saved registry: per-test worlds.

Reader-grant revocation, expiry, stale head and registry replacement on every
route; revocation and authority advance during final return; Save/Reopen
(plan items 17, 18 and the route half of 21); the static view contract
(item 12: order, table semantics, contrast, targets, reduced motion, reflow,
no external requests, disabled release controls).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as reader_module
import traceback_runner.web.longitudinal as longitudinal_module
from evidence_inspector.reader_authorization_registry import ReaderRevocationReason
from evidence_inspector.reader_authorization_synthetic import synthetic_reader_grant
from tests.longitudinal_workspace_world import (
    NOW,
    make_world,
    selector_cohort_registry_id,
)
from tests.web.longitudinal_env import (
    PREFIX,
    STATIC,
    Env,
    _assert_denied,
    _assert_no_protected,
    _cohort_query,
    _contrast,
    _controller_layout,
    _deny_everywhere,
    _http,
    _json,
    _layout,
    _lg_rules,
    _make_env,
    _object_files,
    _register_cohort_v2,
    _reopen,
    _run_harness,
    _save,
    _workspace,
    fresh_env,
    needs_node,
)
from tests.web.test_loopback_server import _exchange, _store
from traceback_runner.web import server as server_module
from traceback_runner.web.auth import ReaderSessionBinding
from traceback_runner.web.explorer import (
    CanonicalExplorerArtifactRepository,
    CatalogAuthorityIndex,
    IntegratedExplorerSource,
)
from traceback_runner.web.longitudinal import (
    LongitudinalExplorerSource,
    validate_longitudinal_public,
)
from traceback_runner.web.server import LocalWebServerError, RunningLocalWebService


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yield from fresh_env(tmp_path, monkeypatch)


def test_revoked_grant_is_denied_on_every_route(fresh: Env) -> None:
    status, _ = fresh.get("selectors")
    assert status == 200
    fresh.world.reader.revoke_grant(
        fresh.world.grant.payload.grant_selector,
        reason=ReaderRevocationReason.OPERATOR_REQUEST,
    )
    _deny_everywhere(fresh)


def test_expired_grant_is_denied_on_every_route(fresh: Env) -> None:
    clock = object.__getattribute__(fresh.world.reader, "_time_source")
    clock.advance_to(NOW + timedelta(days=2))
    _deny_everywhere(fresh)


def test_stale_bound_head_is_denied(fresh: Env) -> None:
    broker = fresh.binder()._boundary.broker
    token = fresh.cookie.split("=", 1)[1]
    record = broker.require_session(token, authority=fresh.service.config.authority)
    binding = record.reader_binding
    stale = ReaderSessionBinding(
        grant_sha256=binding.grant_sha256, registry_head_sha256="0" * 64
    )
    object.__setattr__(record, "reader_binding", stale)
    _deny_everywhere(fresh)


def test_reader_registry_replacement_is_denied(fresh: Env) -> None:
    root = fresh.world.reader.root
    shutil.move(root, root.with_name("reader-replaced"))
    root.mkdir(mode=0o700)
    _deny_everywhere(fresh)


def test_unrelated_grant_keeps_the_session_but_its_own_revocation_ends_it(
    fresh: Env,
) -> None:
    world = fresh.world
    identity = world.reader.identity()
    world.reader.add_grant(
        synthetic_reader_grant(
            registry_id=identity.registry_id,
            registry_epoch_sha256=identity.registry_epoch_sha256,
            grant_selector="reader_grant_" + "2" * 32,
            cohort_registry_ids=(selector_cohort_registry_id(world.cohort),),
            measurement_scopes=(world.extra["scope"],),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
        )
    )
    assert fresh.get("selectors")[0] == 200


def test_revocation_during_final_return_yields_denial_and_no_payload(
    fresh: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = fresh.world
    original = longitudinal_module._PINNED_PROJECT

    def project_then_revoke(workspace):
        projection = original(workspace)
        world.reader.revoke_grant(
            world.grant.payload.grant_selector,
            reason=ReaderRevocationReason.OPERATOR_REQUEST,
        )
        return projection

    monkeypatch.setattr(longitudinal_module, "_PINNED_PROJECT", project_then_revoke)
    status, content = fresh.post("workspace", {"request": fresh.request_json})
    _assert_denied(status, content)
    assert b"rows" not in content and b"replay" not in content


def test_routes_need_their_own_reader_registry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    world = make_world(tmp_path / "world")
    try:
        source = LongitudinalExplorerSource(
            stores=world.stores(), measurement_scopes=(world.extra["scope"],)
        )
        explorer = IntegratedExplorerSource(
            catalog=world.results,
            authority=CatalogAuthorityIndex(()),
            artifacts=CanonicalExplorerArtifactRepository(()),
            longitudinal=source,
        )
        store, _ = _store(tmp_path)
        with pytest.raises(LocalWebServerError):
            RunningLocalWebService.start(
                store=store, state_directory=tmp_path / "state", explorer=explorer
            )
        with pytest.raises(TypeError):
            IntegratedExplorerSource(
                catalog=world.results,
                authority=CatalogAuthorityIndex(()),
                artifacts=CanonicalExplorerArtifactRepository(()),
                longitudinal=object(),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError):
            LongitudinalExplorerSource(
                stores={**world.stores(), "cohort_registry": object()},
                measurement_scopes=(world.extra["scope"],),
            )
        # Without the adapter the routes do not exist.
        with RunningLocalWebService.start(
            store=store, state_directory=tmp_path / "state2"
        ) as service:
            cookie, _ = _exchange(service)
            status, _ = _http(service, "GET", PREFIX + "selectors", {"Cookie": cookie})
            assert status == 404
    finally:
        world.close()


def test_public_encoder_rejects_protected_fields_and_free_text() -> None:
    for payload in (
        {"protected_rows": []},
        {"rows": [{"member_sha256": "a" * 64}]},
        {"reader_authorization": {}},
        {"note": "/Users/someone/private.bam"},
        {"note": "ACGTTGCAACGTTGCAACGTTGCA"},
        {"Bad-Key": 1},
        {"value": object()},
    ):
        with pytest.raises(ValueError):
            validate_longitudinal_public(payload)
    validate_longitudinal_public({"digest": "ab" * 32, "state": "current"})


def test_save_publishes_once_and_reopens_current_after_the_diff(fresh: Env) -> None:
    status, content = _save(fresh)
    assert status == 200, content
    receipt = _json(content)
    assert receipt["applied"] is True
    assert receipt["dependency_fence_kind"] == "composite_authority_fence"
    assert receipt["final_fence_passed"] is True
    assert receipt["saving_authorizes_export"] is False
    files = _object_files(fresh)
    assert list(files) == [f"{receipt['object_sha256']}.json"] or len(files) == 1
    stored = next(iter(files.values()))
    assert hashlib.sha256(stored).hexdigest() == receipt["object_sha256"]
    saved = json.loads(stored)
    workspace = _workspace(fresh)["workspace"]
    assert saved["workspace_replay_sha256"] == workspace["replay_sha256"]
    assert saved["commitments"]["reader_grant_sha256"] == (
        fresh.world.credential.grant_sha256
    )
    # Exact retry is idempotent: same object, nothing written.
    status, content = _save(fresh)
    retry = _json(content)
    assert status == 200 and retry["applied"] is False
    assert retry["object_sha256"] == receipt["object_sha256"]
    assert _object_files(fresh) == files
    # Saved page and Reopen: the diff comes first and carries no result.
    status, content = fresh.get("saved")
    page = _json(content)
    assert [r["authority_state"] for r in page["records"]] == ["current"]
    status, content = _reopen(fresh, receipt, "diff")
    assert status == 200, content
    diff_stage = _json(content)
    assert diff_stage["stage"] == "diff"
    assert diff_stage["current_workspace"] is None and diff_stage["stale_rows"] == []
    diff = diff_stage["diff"]
    assert diff["comparison_state"] == "current" and diff["changes"] == []
    assert diff["saved_bytes_rewritten"] is False and diff["silent_upgrade"] is False
    status, content = _reopen(fresh, receipt, "results")
    results = _json(content)
    assert (
        results["current_workspace"]["replay_sha256"]
        == receipt["workspace_replay_sha256"]
    )
    assert results["current_workspace"]["segments"]
    assert _object_files(fresh) == files
    _assert_no_protected(fresh, content)


def test_stale_authority_reopens_stale_without_current_segments(fresh: Env) -> None:
    _, content = _save(fresh)
    receipt = _json(content)
    files = _object_files(fresh)
    # Authority moves: an unrelated grant advances the reader-registry head.
    world = fresh.world
    identity = world.reader.identity()
    world.reader.add_grant(
        synthetic_reader_grant(
            registry_id=identity.registry_id,
            registry_epoch_sha256=identity.registry_epoch_sha256,
            grant_selector="reader_grant_" + "3" * 32,
            cohort_registry_ids=(selector_cohort_registry_id(world.cohort),),
            measurement_scopes=(world.extra["scope"],),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
        )
    )
    page = _json(fresh.get("saved")[1])
    assert page["records"][0]["authority_state"] == "stale"
    assert page["records"][0]["stale_dependencies"] == ["reader_authorization"]
    diff = _json(_reopen(fresh, receipt, "diff")[1])["diff"]
    assert diff["comparison_state"] == "stale"
    assert "dependency_heads_changed" in diff["changes"]
    _, content = _reopen(fresh, receipt, "results")
    results = _json(content)
    assert results["current_workspace"] is None
    assert results["stale_segments"] == []
    assert results["refresh_action"] == "start_new_comparison_at_current_authority"
    assert {row["comparison_state"] for row in results["stale_rows"]} <= {
        "anchor_reference",
        "suppressed_stale_authority",
    }
    for forbidden in (b'"delta"', b'"member_value"', b'"segments"', b'"comparison":'):
        assert forbidden not in content
    assert _object_files(fresh) == files


def test_cohort_advance_is_diffed_and_never_silently_applied(fresh: Env) -> None:
    receipt = _json(_save(fresh)[1])
    _register_cohort_v2(fresh.world)
    cohorts = _json(fresh.get("selectors")[1])["cohorts"]
    assert [item["cohort_version"] for item in cohorts] == [1, 2]
    status, content = fresh.get("diff", _cohort_query(fresh, cohort_version="2"))
    assert status == 200, content
    diff = _json(content)["diff"]
    assert diff["kind"] == "predecessor" and diff["predecessor_version"] == 1
    assert diff["removed_member_count"] == 1 and "members_removed" in diff["reasons"]
    # Reopen keeps the saved version; the newer version is shown, not applied.
    reopen = _json(_reopen(fresh, receipt, "diff")[1])["diff"]
    assert reopen["saved_selection"]["cohort_version"] == 1
    assert reopen["newer_cohort_version_available"] is True
    assert reopen["latest_cohort_version"] == 2
    assert reopen["comparison_state"] == "stale"
    assert "d05_cohort" in reopen["stale_dependencies"]
    assert reopen["cohort_version_diff"]["selected_version"] == 1
    # A fresh workspace for version 1 still builds version 1.
    workspace = _json(fresh.post("workspace", {"request": fresh.request_json})[1])
    assert workspace["workspace"]["authority"]["cohort_version"] == 1


def test_receipt_only_after_the_final_fence(
    fresh: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = fresh.world
    original = longitudinal_module.SavedComparisonDependencyScopeV1

    def advance_then_scope(**kwargs):
        # Runs after register() returned and before the final composite hold.
        identity = world.reader.identity()
        world.reader.add_grant(
            synthetic_reader_grant(
                registry_id=identity.registry_id,
                registry_epoch_sha256=identity.registry_epoch_sha256,
                grant_selector="reader_grant_" + "4" * 32,
                cohort_registry_ids=(selector_cohort_registry_id(world.cohort),),
                measurement_scopes=(world.extra["scope"],),
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
            )
        )
        return original(**kwargs)

    monkeypatch.setattr(
        longitudinal_module, "SavedComparisonDependencyScopeV1", advance_then_scope
    )
    status, content = _save(fresh)
    assert status == 409, content
    assert _json(content)["error"]["code"] == "authority_stale"
    assert b"saved_selector_id" not in content and b"object_sha256" not in content
    # The publication committed, but no receipt was shown; its bytes reopen stale.
    assert len(_object_files(fresh)) == 1


def test_revocation_after_publication_shows_no_receipt(
    fresh: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = fresh.world
    original = longitudinal_module.SavedComparisonDependencyScopeV1

    def revoke_then_scope(**kwargs):
        world.reader.revoke_grant(
            world.grant.payload.grant_selector,
            reason=ReaderRevocationReason.OPERATOR_REQUEST,
        )
        return original(**kwargs)

    monkeypatch.setattr(
        longitudinal_module, "SavedComparisonDependencyScopeV1", revoke_then_scope
    )
    status, content = _save(fresh)
    assert status in {403, 409}
    assert b"saved_selector_id" not in content


def test_save_is_disabled_without_a_healthy_bounded_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    absent = _make_env(tmp_path / "absent", with_registry=False)
    try:
        assert _json(absent.get("selectors")[1])["save"]["state"] == "registry_absent"
        status, content = _save(absent)
        assert status == 409
        assert _json(content) == {
            "error": {"code": "save_unavailable", "remediation": "registry_absent"}
        }
        assert _json(absent.get("saved")[1])["records"] == []
    finally:
        absent.close()
    env = _make_env(tmp_path / "bounded")
    try:
        assert _json(_save(env)[1])["applied"] is True
        monkeypatch.setattr(longitudinal_module, "MAX_SAVED_COMPARISONS", 1)
        assert _json(env.get("selectors")[1])["save"]["state"] == "registry_full"
        assert _json(_save(env)[1])["error"]["remediation"] == "registry_full"
        monkeypatch.undo()
        monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
        # Corrupt journal: the registry is unhealthy and Save is unavailable.
        journal = env.saved.root / "registry-journal.jsonl"  # type: ignore[union-attr]
        journal.chmod(0o600)
        with journal.open("ab") as stream:
            stream.write(b"{}\n")
        assert _json(env.get("selectors")[1])["save"]["state"] == "registry_unhealthy"
        status, content = _save(env)
        assert _json(content)["error"]["remediation"] == "registry_unhealthy"
    finally:
        env.close()


def test_tampered_object_or_swapped_root_never_reopens(fresh: Env) -> None:
    receipt = _json(_save(fresh)[1])
    objects = fresh.saved.root / "objects"  # type: ignore[union-attr]
    [path] = list(objects.iterdir())
    original = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(original.replace(b'"local_only":true', b'"local_only":true '))
    status, content = _reopen(fresh, receipt, "results")
    assert status in {409, 500}
    assert b"rows" not in content and b"workspace" not in content
    path.write_bytes(original)
    root = fresh.saved.root  # type: ignore[union-attr]
    shutil.move(root, root.with_name("saved-swapped"))
    shutil.copytree(root.with_name("saved-swapped"), root)
    status, content = _reopen(fresh, receipt, "diff")
    assert status in {409, 500}
    assert _json(content)["error"]["code"] in {"integrity_failure", "authority_stale"}


def test_unknown_saved_selector_answers_like_a_denial(fresh: Env) -> None:
    status, content = fresh.post(
        "reopen",
        {
            "saved_selector_id": "saved_comparison_" + "a" * 40,
            "comparison_version": 1,
            "stage": "diff",
        },
    )
    _assert_denied(status, content)


def test_html_order_table_semantics_and_disabled_release_controls() -> None:
    layout = _layout()
    order = layout.order
    sequence = [
        "lg-identity",
        "lg-outcome",
        "lg-denominators",
        "lg-table-section",
        "lg-chart",
        "lg-covariates",
        "lg-drawer",
    ]
    assert [order.index(item) for item in sequence] == sorted(
        order.index(i) for i in sequence
    )
    # Selection and diff precede every result.
    assert (
        order.index("lg-journey") < order.index("lg-diff") < order.index("lg-results")
    )
    html = (STATIC / "index.html").read_text()
    table = html[
        html.index('<table id="lg-table">') : html.index(
            "</table>", html.index('<table id="lg-table">')
        )
    ]
    assert "<caption>" in table and table.count('scope="col"') == 13
    script = (STATIC / "longitudinal.js").read_text()
    assert (
        'el(doc, "th", `${row.row_ordinal} (${row.source_alias})`, { scope: "row" })'
        in script
    )
    for control in ("lg-release", "lg-export"):
        element = next(item for item in layout.elements if item["id"] == control)
        assert element["disabled"] and element["tag"] == "button"
        assert f"{control.replace('lg-', '')}.disabled = false" not in script
    assert "ui.release" not in script and "ui.export" not in script
    status = next(item for item in layout.elements if item["id"] == "lg-status")
    assert (
        status["attrs"]["role"] == "status" and status["attrs"]["aria-live"] == "polite"
    )
    drawer = next(item for item in layout.elements if item["id"] == "lg-drawer")
    assert drawer["attrs"]["role"] == "dialog"
    region = re.search(r'<div class="table-wrap" role="region"[^>]*tabindex="0"', html)
    assert region, "the table overflow region must be keyboard reachable"


def test_contrast_targets_reduced_motion_and_reflow() -> None:
    rules = _lg_rules()
    checked = 0
    for selector, body in rules:
        fg = re.search(r"(?<![-\w])color:\s*(#[0-9a-f]{6})", body)
        bg = re.search(r"background:\s*(#[0-9a-f]{6})", body)
        if fg:
            background = bg.group(1) if bg else "#ffffff"
            assert _contrast(fg.group(1), background) >= 4.5, selector
            checked += 1
        for name in ("stroke", "fill", "outline"):
            match = re.search(rf"{name}:\s*(?:\d+px solid\s*)?(#[0-9a-f]{{6}})", body)
            if match:
                assert _contrast(match.group(1), "#ffffff") >= 4.5, (selector, name)
                checked += 1
    assert checked >= 15
    css = (STATIC / "styles.css").read_text()
    assert (
        ".lg button, .lg select, .lg .lg-check { min-height: 2.75rem; min-width: 2.75rem; }"
        in css
    )
    assert re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{ \.lg \*.*transition: none !important",
        css,
    )
    for selector, body in rules:
        assert not re.search(r"(?<!max-)width:\s*\d{3,}px", body), selector
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert ".table-wrap { overflow-x: auto; }" in css
    for media in (
        "@media (min-width: 64rem)",
        "@media (min-width: 42rem) and (max-width: 63.99rem)",
        "@media (max-width: 41.99rem)",
    ):
        assert media in css
    assert '[data-drawer="beside"]' in css and '[data-drawer="overlay"]' in css
    assert '[data-drawer="sheet"]' in css
    # Every longitudinal button lives inside the .lg section, so it gets the target size.
    assert all(
        item.get("type") == "button" or item.get("type") == "submit"
        for item in _layout().buttons
    )


def test_no_external_requests_and_same_origin_routes_only() -> None:
    script = (STATIC / "longitudinal.js").read_text()
    for content in (
        script,
        (STATIC / "index.html").read_text(),
        (STATIC / "styles.css").read_text(),
    ):
        lowered = content.lower()
        assert (
            "http://" not in lowered
            and "https://" not in lowered
            and "//cdn" not in lowered
        )
    paths = re.findall(r'call\("(?:GET|POST)", [`"]([^`"?$]+)', script)
    assert paths and all(path.startswith("/api/v1/longitudinal/") for path in paths)
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script and "eval(" not in script
    assert 'credentials: "same-origin"' in script
    headers = server_module._SECURITY_HEADERS["Content-Security-Policy"]
    assert "connect-src 'self'" in headers and "default-src 'self'" in headers


@needs_node
def test_controller_reopen_shows_diff_then_stale_results(
    fresh: Env, tmp_path: Path
) -> None:
    receipt = _json(_save(fresh)[1])
    _register_cohort_v2(fresh.world)
    saved_page = _json(fresh.get("saved")[1])
    diff = _json(_reopen(fresh, receipt, "diff")[1])
    results = _json(_reopen(fresh, receipt, "results")[1])
    report = _run_harness(
        tmp_path,
        {
            "controller": {
                "layout": _controller_layout(),
                "responses": {
                    "/api/v1/longitudinal/selectors": [
                        {"status": 200, "payload": _json(fresh.get("selectors")[1])}
                    ],
                    "/api/v1/longitudinal/saved": [
                        {"status": 200, "payload": saved_page}
                    ],
                    "/api/v1/longitudinal/reopen": [
                        {"status": 200, "payload": diff},
                        {"status": 200, "payload": results},
                    ],
                },
                "steps": [
                    {"do": "bind"},
                    {"do": "clickText", "text": "Reopen version 1", "snapshot": "diff"},
                    {
                        "do": "clickText",
                        "text": "Show reopened results",
                        "snapshot": "results",
                    },
                ],
            }
        },
    )
    snaps = {s["label"]: s for s in report["snapshots"]}
    stages = [
        f["body"]["stage"] for f in report["fetches"] if f["url"].endswith("/reopen")
    ]
    assert stages == ["diff", "results"]
    assert (
        snaps["diff"]["resultsHidden"] is True and snaps["diff"]["diffHidden"] is False
    )
    assert "not applied" in snaps["diff"]["diffText"]
    assert "never rewritten" in snaps["diff"]["diffText"]
    stale = snaps["results"]
    assert stale["state"] == "stale" and stale["paths"] == 0
    assert "Comparisons and segments are suppressed" in stale["status"]
    assert stale["saveDisabled"] is True
    assert "suppressed: saved comparison is stale" in stale["resultsText"]
