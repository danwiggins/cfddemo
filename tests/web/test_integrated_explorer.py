"""Integrated E14 catalog/API/DOM and release-eligibility regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
    ResultCatalog,
)
from evidence_inspector.result_view import build_result_view
from tests.web.test_loopback_server import _exchange, _request
from traceback_runner.product_gates import (
    AccessibilityAuditArtifact,
    BrowserCapture,
    BrowserCaptureArtifact,
    FiveProviderStudyArtifact,
    GateId,
    ProviderTaskOutcome,
    ReleaseGateDecision,
)
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import TrustStore
from traceback_runner.store import JobStore
from traceback_runner.web.explorer import (
    ExplorerReadModels,
    IntegratedExplorerSource,
    explorer_eligibility,
)
from traceback_runner.web.server import RunningLocalWebService


def _ref(index: int = 0) -> CatalogResultRef:
    bundle = f"{index + 1:064x}"
    method_sha = "c" * 64
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": bundle,
                "method_definition_sha256": method_sha,
            }
        )
    ).hexdigest()
    return CatalogResultRef(
        result_id=f"result_{identity[:40]}",
        bundle_sha256=bundle,
        bundle_record_id=f"record-{index:024x}",
        bundle_manifest_sha256=f"{index + 2:064x}",
        workflow_release_id="synthetic-workflow.v1",
        method_ref={
            "method_id": "mth_fragment_raw_query_length",
            "version": "1.0.0",
        },
        method_definition_sha256=method_sha,
        registry_sha256="d" * 64,
        registry_version=1,
        authority_head_sha256="e" * 64,
        authority_revision=2,
        authority_scope="scope_provider_west",
        capability_as_of=datetime(2026, 2, 1, tzinfo=UTC),
        qualification_state=CatalogQualificationState.QUALIFIED,
        display_role="provider_primary",
        research_inspectable=True,
        current_provider_eligible=True,
    )


def _catalog(
    tmp_path: Path, refs: tuple[CatalogResultRef, ...] | None = None
) -> tuple[ResultCatalog, CatalogResultRef]:
    imports = tmp_path / "imports"
    imports.mkdir()
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_synthetic": imports},
        trust_store=TrustStore(),
    )
    selected = refs or (_ref(),)
    rows = [
        (
            ref.result_id,
            ref.bundle_sha256,
            ref.bundle_record_id,
            ref.method_ref.method_id,
            ref.method_ref.version,
            ref.execution_state.value,
            ref.information_state.value,
            ref.trust_state.value,
            ref.qualification_state.value,
            ref.model_dump_json().encode(),
        )
        for ref in selected
    ]
    with catalog._connect() as connection:
        connection.executemany("INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    return catalog, selected[0]


def _result_view_test_module():
    name = "e14_result_view_fixture"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path("tests/test_result_view.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _integrated_ref(record, index: int) -> CatalogResultRef:
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": record.bundle_sha256,
                "method_definition_sha256": record.method_definition_sha256,
            }
        )
    ).hexdigest()
    assert record.result_id == f"result_{identity[:40]}"
    capability = record.current_capability
    return CatalogResultRef(
        result_id=record.result_id,
        bundle_sha256=record.bundle_sha256,
        bundle_record_id=f"record-integrated-{index}",
        bundle_manifest_sha256=f"{index + 8:064x}",
        workflow_release_id="synthetic-workflow.v1",
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        authority_scope=capability.authority_scope,
        capability_as_of=capability.as_of,
        qualification_state=capability.qualification_state,
        display_role=capability.display_role,
        research_inspectable=capability.research_inspectable,
        current_provider_eligible=capability.current_provider_eligible,
    )


def _integrated_models() -> tuple[ExplorerReadModels, ExplorerReadModels]:
    fixtures = _result_view_test_module()
    method = fixtures._method()
    method_sha = fixtures.method_definition_sha256(method)
    records = []
    for digit in ("1", "2", "3"):
        bundle_sha = hex((int(digit, 16) + 1) % 16)[2:] * 64
        identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "bundle_sha256": bundle_sha,
                    "method_definition_sha256": method_sha,
                }
            )
        ).hexdigest()
        records.append(fixtures._record(identity[:40], digit))
    anchor = records[0]
    display_records = records[1:]
    sources = tuple(fixtures._source(anchor, record) for record in display_records)
    view = build_result_view(fixtures._request(sources))
    refs = tuple(
        _integrated_ref(record, index) for index, record in enumerate(display_records)
    )
    return tuple(ExplorerReadModels(catalog_ref=ref, result_view=view) for ref in refs)


def _decision(enabled: bool) -> ReleaseGateDecision:
    return ReleaseGateDecision(
        report_sha256="a" * 64,
        authority_policy_sha256="b" * 64 if enabled else None,
        trusted_external_evidence_sha256=(
            ("c" * 64, "d" * 64, "e" * 64, "f" * 64) if enabled else ()
        ),
        capability_enabled=enabled,
        unmet_gates=() if enabled else (GateId.ACCESSIBILITY,),
    )


def test_release_gate_never_blocks_research_inspection() -> None:
    ref = _ref()
    disabled = explorer_eligibility(ref, _decision(False))
    assert disabled.research_inspection_allowed
    assert not disabled.release_explorer_allowed
    assert not disabled.release_export_allowed

    enabled = explorer_eligibility(ref, _decision(True))
    assert enabled.research_inspection_allowed
    assert enabled.release_explorer_allowed
    assert enabled.release_export_allowed
    assert enabled.release_gate_decision_sha256 is not None


def test_real_catalog_api_is_bounded_authorized_and_private(tmp_path: Path) -> None:
    catalog, ref = _catalog(tmp_path)
    explorer = IntegratedExplorerSource(
        catalog=catalog,
        release_gate_decision=_decision(False),
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        with RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state",
            explorer=explorer,
        ) as service:
            assert (
                _request(service, "GET", "/api/v1/explorer/catalog?limit=1")[0] == 401
            )
            cookie, _ = _exchange(service)
            status, _, body = _request(
                service,
                "GET",
                "/api/v1/explorer/catalog?limit=1",
                headers={"Cookie": cookie},
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["results"][0]["ref"]["result_id"] == ref.result_id
            assert payload["results"][0]["has_registered_view"] is False
            assert payload["results"][0]["eligibility"] == {
                "release_export_allowed": False,
                "release_explorer_allowed": False,
                "release_gate_decision_sha256": explorer_eligibility(
                    ref, _decision(False)
                ).release_gate_decision_sha256,
                "research_inspection_allowed": True,
            }
            serialized = body.decode()
            assert str(catalog.root) not in serialized
            assert "root_synthetic" not in serialized
            assert (
                _request(
                    service,
                    "GET",
                    f"/api/v1/explorer/results/{ref.result_id}",
                    headers={"Cookie": cookie},
                )[0]
                == 404
            )
            assert (
                _request(
                    service,
                    "GET",
                    "/api/v1/explorer/catalog?unexpected=value",
                    headers={"Cookie": cookie},
                )[0]
                == 400
            )
            assert (
                _request(
                    service,
                    "GET",
                    "/api/v1/explorer/catalog?limit=1&limit=2",
                    headers={"Cookie": cookie},
                )[0]
                == 400
            )
    finally:
        catalog.close()


def test_registered_e06_models_flow_through_real_api(tmp_path: Path) -> None:
    models = _integrated_models()
    catalog, _ = _catalog(tmp_path, tuple(item.catalog_ref for item in models))
    explorer = IntegratedExplorerSource(
        catalog=catalog,
        read_models={item.catalog_ref.result_id: item for item in models},
        release_gate_decision=_decision(False),
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        with RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state",
            explorer=explorer,
        ) as service:
            cookie, _ = _exchange(service)
            status, _, body = _request(
                service,
                "GET",
                f"/api/v1/explorer/results/{models[0].catalog_ref.result_id}",
                headers={"Cookie": cookie},
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["models"]["schema_version"] == (
                "traceback.integrated-explorer-models.v1"
            )
            assert payload["models"]["result_view"]["visible_count"] == 2
            ledger = payload["models"]["result_view"]["rows"][0]["denominator"]
            assert ledger["displayed_records"] == {
                "accessible_label": "Displayed records",
                "state": "observed",
                "value": 75,
            }
            assert payload["eligibility"]["research_inspection_allowed"] is True
            assert payload["eligibility"]["release_export_allowed"] is False
    finally:
        catalog.close()


def test_packaged_dom_is_semantic_responsive_and_offline() -> None:
    root = Path("traceback_runner/web/static")
    html = (root / "index.html").read_text()
    script = (root / "app.js").read_text()
    styles = (root / "styles.css").read_text()
    assert '<html lang="en">' in html
    assert 'class="skip-link"' in html
    assert "<caption>" in html
    assert html.count('scope="col"') == 6
    assert 'aria-live="polite"' in html
    assert 'count.state !== "observed"' in script
    assert "Research inspection only" in script
    assert "@media (max-width:" in styles
    for content in (html, script, styles):
        lowered = content.lower()
        assert "http://" not in lowered
        assert "https://" not in lowered
        assert "//cdn" not in lowered


def test_external_evidence_contracts_are_parsed_and_content_addressed() -> None:
    captured_at = datetime(2026, 9, 29, tzinfo=UTC)
    capture = BrowserCaptureArtifact(
        captured_at=captured_at,
        browser_name="chromium",
        browser_version="version_140",
        captures=(
            BrowserCapture(
                capture_id="capture_ready_mobile",
                fixture_id="fixture_ready",
                surface_state="ready",
                viewport_width_px=390,
                zoom_percent=100,
                image_sha256="1" * 64,
                dom_sha256="2" * 64,
                filters_sha256="3" * 64,
            ),
        ),
    )
    capture_sha = hashlib.sha256(canonical_json_bytes(capture)).hexdigest()
    audit = AccessibilityAuditArtifact(
        audited_at=captured_at,
        auditor_id="auditor_external",
        browser_capture_artifact_sha256=capture_sha,
        keyboard_audit_passed=False,
        screen_reader_audit_passed=False,
        zoom_200_audit_passed=False,
        findings=("External audit remains required",),
    )
    outcomes = tuple(
        ProviderTaskOutcome(
            participant_id=f"participant_{index}",
            task_id="task_compare_methods",
            completed=False,
            duration_seconds=0,
            error_count=0,
        )
        for index in range(1, 6)
    )
    study = FiveProviderStudyArtifact(
        conducted_at=captured_at,
        protocol_sha256="4" * 64,
        browser_capture_artifact_sha256=capture_sha,
        outcomes=outcomes,
    )
    assert audit.browser_capture_artifact_sha256 == capture_sha
    assert len({item.participant_id for item in study.outcomes}) == 5
