"""Adversarial E14 tests over real E04 imports and canonical E06 replay."""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import evidence_inspector.result_catalog as catalog_module
import traceback_runner.web.explorer as explorer_module
import traceback_runner.web.server as server_module
from evidence_inspector.compatibility import (
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
)
from evidence_inspector.result_catalog import (
    CatalogFilesystemError,
    CatalogQuery,
    CatalogVerificationContext,
    ResultCatalog,
)
from evidence_inspector.result_view import build_result_view
from tests.web.test_loopback_server import _exchange, _request
from traceback_runner.bundles import verify_bundle
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.store import JobStore
from traceback_runner.web.explorer import (
    CanonicalExplorerArtifactRepository,
    CatalogAuthorityBinding,
    CatalogAuthorityIndex,
    ExplorerArtifactRecord,
    IntegratedExplorerSource,
    _fragment_matches_selected_documents,
    explorer_eligibility,
)
from traceback_runner.web.server import RunningLocalWebService


def _load(name: str, path: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _installed(tmp_path: Path):
    catalog_fixtures = _load("e14_catalog_fixtures", "tests/test_result_catalog.py")
    view_fixtures = _load("e14_view_fixtures", "tests/test_result_view.py")
    catalog, _, _ = catalog_fixtures._catalog(tmp_path)
    ref = catalog_fixtures._import(catalog)
    registry, definition, _, _, head, head_sha256, capability = (
        catalog_fixtures._authority()
    )
    context = CatalogVerificationContext(
        registry=registry,
        authority_head=head,
        expected_authority_head_sha256=head_sha256,
        capability=capability,
    )
    base = view_fixtures._record("a" * 40, "1")
    asset = definition.assets[0]
    compatibility_key = MeasurementCompatibilityKey(
        measurement_family=definition.family,
        quantity_id=definition.quantity_id,
        unit=definition.unit,
        result_schema=ResultSchemaReference(
            schema_id="schema_fragment_measurement", version="1.0.0"
        ),
        reference_asset=asset,
        grid_asset=asset,
        atlas_asset=asset,
        panel_asset=asset,
        normalization_semantics_id="sem_normalization_exact",
        coordinate_semantics_id="sem_coordinate_exact",
        denominator_semantics_id="sem_denominator_exact",
        registered_policy=base.compatibility_key.registered_policy,
    )
    record = VerifiedMeasurementRecord(
        result_id=ref.result_id,
        result_sha256=ref.bundle_manifest_sha256,
        bundle_id=f"bundle_{ref.result_id.removeprefix('result_')}",
        bundle_sha256=ref.bundle_sha256,
        method=definition,
        method_definition_sha256=ref.method_definition_sha256,
        current_capability=capability,
        execution_state=ExecutionState.COMPLETE,
        information_state=InformationState.SUFFICIENT,
        trust_state=TrustState.VERIFIED,
        compatibility_key=compatibility_key,
    )
    anchor = record.model_copy(
        update={
            "result_id": f"result_{'f' * 40}",
            "result_sha256": "e" * 64,
            "bundle_id": f"bundle_{'f' * 40}",
            "bundle_sha256": "d" * 64,
        }
    )
    source = view_fixtures._source(anchor, record)
    request = view_fixtures._request((source,))
    view = build_result_view(request)
    artifact = ExplorerArtifactRecord(
        result_id=ref.result_id,
        result_view_request=request,
        result_view=view,
    )
    explorer = IntegratedExplorerSource(
        catalog=catalog,
        authority=CatalogAuthorityIndex(
            (CatalogAuthorityBinding(result_id=ref.result_id, context=context),)
        ),
        artifacts=CanonicalExplorerArtifactRepository((artifact,)),
    )
    return catalog, ref, context, artifact, explorer


def test_release_eligibility_has_no_forgeable_decision_input(tmp_path: Path) -> None:
    catalog, ref, _, _, _ = _installed(tmp_path)
    try:
        assert tuple(inspect.signature(explorer_eligibility).parameters) == ("ref",)
        eligibility = explorer_eligibility(ref)
        assert eligibility.research_inspection_allowed
        assert not eligibility.release_explorer_allowed
        assert not eligibility.release_export_allowed
        assert eligibility.release_gate_decision_sha256 is None
    finally:
        catalog.close()


def test_detail_reloads_real_e04_authority_and_rejects_fabrication(
    tmp_path: Path,
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    try:
        document = explorer.get(ref.result_id)
        assert document.models.catalog_ref == ref
        assert document.models.result_view.visible_count == 1
        with pytest.raises(KeyError, match="authority is unavailable"):
            explorer.get(f"result_{'0' * 40}")
    finally:
        catalog.close()


def test_detail_rejects_closed_shadowed_or_substituted_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    catalog.close()
    catalog.get_verified = lambda result_id, context: ref  # type: ignore[method-assign]
    with pytest.raises(CatalogFilesystemError):
        explorer.get(ref.result_id)

    catalog, ref, _, _, explorer = _installed(tmp_path / "instance-shadow")
    try:
        catalog._connect = lambda: None  # type: ignore[method-assign]
        with pytest.raises(CatalogFilesystemError, match="method is shadowed"):
            explorer.get(ref.result_id)
    finally:
        catalog.close()

    class SubstituteCatalog(ResultCatalog):
        pass

    with pytest.raises(TypeError, match="exact ResultCatalog"):
        IntegratedExplorerSource(
            catalog=object.__new__(SubstituteCatalog),
            authority=explorer._authority,
            artifacts=explorer._artifacts,
        )

    catalog, ref, _, _, explorer = _installed(tmp_path / "class-shadow")
    try:
        monkeypatch.setattr(
            ResultCatalog,
            "get_verified",
            lambda self, result_id, context: ref,
        )
        with pytest.raises(CatalogFilesystemError, match="class changed"):
            explorer.get(ref.result_id)
    finally:
        catalog.close()


def test_explorer_rejects_post_construction_verifier_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    try:
        with pytest.raises(TypeError, match="sealed"):
            explorer._get_verified = lambda result_id, context: ref  # type: ignore[method-assign]
        with pytest.raises(TypeError, match="sealed"):
            explorer._reader._connection = None
        with pytest.raises(TypeError, match="class is sealed"):
            IntegratedExplorerSource.get = lambda self, result_id: ref  # type: ignore[method-assign]
        monkeypatch.setattr(
            explorer_module,
            "_reverify_document",
            lambda get_verified, authority, document: None,
        )
        with pytest.raises(TypeError, match="reader chain changed"):
            explorer.get(ref.result_id)
        monkeypatch.undo()
        catalog.close()
        with pytest.raises(CatalogFilesystemError):
            explorer.get(ref.result_id)
    finally:
        catalog.close()


def test_explorer_rejects_preconstruction_reader_factory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)

    class FakeReader:
        get_verified = staticmethod(lambda result_id, context: ref)
        query = staticmethod(lambda query: None)

    monkeypatch.setattr(
        explorer_module,
        "bind_catalog_live_reader",
        lambda candidate: FakeReader(),
    )
    try:
        with pytest.raises(TypeError, match="package-owned reader factory"):
            IntegratedExplorerSource(
                catalog=catalog,
                authority=explorer._authority,
                artifacts=explorer._artifacts,
            )
    finally:
        catalog.close()


def test_explorer_pins_bundle_verifier_against_module_global_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    object_path = catalog.objects / ref.bundle_sha256
    verified = verify_bundle(object_path, catalog.trust_store)
    report = object_path / "report.html"
    original = report.read_bytes()
    replacement = bytes((original[0] ^ 1,)) + original[1:]
    assert len(replacement) == len(original)
    report.chmod(0o600)
    report.write_bytes(replacement)
    try:
        with pytest.raises(ValueError, match="checksum mismatch"):
            explorer.get(ref.result_id)
        monkeypatch.setattr(
            catalog_module,
            "_VERIFY_CATALOG_BUNDLE",
            lambda path, trust_store: verified,
        )
        with pytest.raises(CatalogFilesystemError, match="reader binding changed"):
            explorer.get(ref.result_id)
    finally:
        catalog.close()


def test_explorer_pins_descriptor_resolver_against_alternate_object_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    object_path = catalog.objects / ref.bundle_sha256
    alternate_objects = tmp_path / "alternate-objects"
    alternate_objects.mkdir()
    shutil.copytree(object_path, alternate_objects / ref.bundle_sha256)
    report = object_path / "report.html"
    original = report.read_bytes()
    report.chmod(0o600)
    report.write_bytes(bytes((original[0] ^ 1,)) + original[1:])
    try:
        with pytest.raises(ValueError, match="checksum mismatch"):
            explorer.get(ref.result_id)
        monkeypatch.setattr(
            catalog_module,
            "_descriptor_path",
            lambda descriptor: alternate_objects,
        )
        with pytest.raises(CatalogFilesystemError, match="reader binding changed"):
            explorer.get(ref.result_id)
    finally:
        catalog.close()


def test_model_copy_poison_is_rejected_before_repository_sink(tmp_path: Path) -> None:
    catalog, _, _, artifact, _ = _installed(tmp_path)
    try:
        poisoned_view = artifact.result_view.model_copy(
            update={"filters_sha256": "0" * 64}
        )
        poisoned = artifact.model_copy(update={"result_view": poisoned_view})
        with pytest.raises((ValidationError, ValueError)):
            CanonicalExplorerArtifactRepository((poisoned,))
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "private_text",
    (
        "source%2Fprivate%2Fcase.tsv",
        "%252FUsers%252Fcase%252Finput.tsv",
        "C:%5CUsers%5CCase%5Cinput.tsv",
        "%5C%5Cserver%5Cshare%5Ccase.tsv",
        "file%3A%2F%2F%2Ftmp%2Fcase.tsv",
        "source%E2%88%95private%E2%88%95case.tsv",
        "SoUrCe%2fPrIvAtE%2fcase.tsv",
        "source∖private∖case.tsv",
        "source⧵private⧵case.tsv",
        "source╲private╲case.tsv",
        "Patient Identifier 42",
        "ACGTRYSWKMBDHVNACGTRYSWKMBDHVN",
        "AUGCRYSWKMBDHVNAUGCRYSWKMBDHVN",
        "ACGTURYSWKMBDHVNACGTURYSWKMBDHVN",
    ),
)
def test_nested_public_projection_rejects_encoded_private_text(
    tmp_path: Path, private_text: str
) -> None:
    catalog, _, _, artifact, _ = _installed(tmp_path)
    try:
        source = artifact.result_view_request.sources[0].model_copy(
            update={"accessible_label": private_text}
        )
        request = artifact.result_view_request.model_copy(update={"sources": (source,)})
        with pytest.raises((ValidationError, ValueError)):
            view = build_result_view(request)
            poisoned = artifact.model_copy(
                update={"result_view_request": request, "result_view": view}
            )
            CanonicalExplorerArtifactRepository((poisoned,))
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "private_label",
    (
        "AUGCRYSWKMBDHVNAUGCRYSWKMBDHVN",
        "ACGTURYSWKMBDHVNACGTURYSWKMBDHVN",
        "source∖private∖case.tsv",
        "source⧵private⧵case.tsv",
        "source╲private╲case.tsv",
    ),
)
def test_private_label_never_reaches_live_api_or_accessible_dom_sink(
    tmp_path: Path, private_label: str
) -> None:
    catalog, ref, _, artifact, explorer = _installed(tmp_path)
    source = artifact.result_view_request.sources[0].model_copy(
        update={"accessible_label": private_label}
    )
    request = artifact.result_view_request.model_copy(update={"sources": (source,)})
    view = build_result_view(request)
    poisoned = artifact.model_copy(
        update={"result_view_request": request, "result_view": view}
    )
    explorer._artifacts._records[ref.result_id] = canonical_json_bytes(poisoned)
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        with pytest.raises(ValueError, match="path|raw nucleotide sequence"):
            explorer.get(ref.result_id)
        with RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state",
            explorer=explorer,
        ) as service:
            cookie, _ = _exchange(service)
            status, _, body = _request(
                service,
                "GET",
                f"/api/v1/explorer/results/{ref.result_id}",
                headers={"Cookie": cookie},
            )
            assert status == 400
            assert private_label.encode() not in body
            assert b"accessible_label" not in body
        script = Path("traceback_runner/web/static/app.js").read_text()
        assert "addCell(tableRow, row.accessible_label)" in script
        assert "cell.textContent = value" in script
    finally:
        catalog.close()


def test_source_class_replacement_fails_direct_and_at_http_boundary(
    tmp_path: Path,
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    cached = explorer.get(ref.result_id)
    original_get = IntegratedExplorerSource.__dict__["get"]
    type.__setattr__(
        IntegratedExplorerSource,
        "get",
        lambda self, result_id: cached,
    )
    catalog.close()
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        with pytest.raises(TypeError, match="source class changed"):
            explorer.get(ref.result_id)
        with RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state",
            explorer=explorer,
        ) as service:
            assert not hasattr(service.server, "_server")
            assert not hasattr(service.server, "RequestHandlerClass")
            with pytest.raises((AttributeError, TypeError)):
                service.server.RequestHandlerClass = object  # type: ignore[attr-defined,misc]
            cookie, _ = _exchange(service)
            status, _, _ = _request(
                service,
                "GET",
                f"/api/v1/explorer/results/{ref.result_id}",
                headers={"Cookie": cookie},
            )
            assert status == 400
    finally:
        type.__setattr__(IntegratedExplorerSource, "get", original_get)
        catalog.close()


def test_fragment_pair_cross_binding_rejects_stale_same_id_peer(
    tmp_path: Path,
) -> None:
    catalog, ref, _, artifact, _ = _installed(tmp_path)
    try:
        record = artifact.result_view_request.sources[0].record
        manifest = verify_bundle(
            catalog.objects / ref.bundle_sha256, catalog.trust_store
        ).manifest
        second_manifest = manifest.model_copy(
            update={"record_id": "record-pair-second"}
        )
        second_ref = ref.model_copy(
            update={
                "result_id": f"result_{'2' * 40}",
                "bundle_record_id": second_manifest.record_id,
                "bundle_manifest_sha256": sha256_bytes(
                    canonical_json_bytes(second_manifest)
                ),
                "bundle_sha256": "3" * 64,
            }
        )
        second_record = record.model_copy(
            update={
                "result_id": second_ref.result_id,
                "result_sha256": "4" * 64,
                "bundle_id": "bundle_pair_second",
                "bundle_sha256": second_ref.bundle_sha256,
            }
        )
        left = SimpleNamespace(
            models=SimpleNamespace(
                catalog_ref=ref,
                result_view_request=SimpleNamespace(
                    sources=(SimpleNamespace(record=record),)
                ),
            )
        )
        right = SimpleNamespace(
            models=SimpleNamespace(
                catalog_ref=second_ref,
                result_view_request=SimpleNamespace(
                    sources=(SimpleNamespace(record=second_record),)
                ),
            )
        )
        fragment = SimpleNamespace(
            state=SimpleNamespace(
                left=SimpleNamespace(
                    result_id=ref.result_id, method_ref=ref.method_ref
                ),
                right=SimpleNamespace(
                    result_id=second_ref.result_id, method_ref=second_ref.method_ref
                ),
            ),
            request=SimpleNamespace(
                sources=(
                    SimpleNamespace(record=record, manifest=manifest),
                    SimpleNamespace(record=second_record, manifest=second_manifest),
                )
            ),
        )
        assert _fragment_matches_selected_documents(fragment, left, right)
        stale = second_record.model_copy(update={"result_sha256": "5" * 64})
        stale_fragment = SimpleNamespace(
            state=fragment.state,
            request=SimpleNamespace(
                sources=(
                    SimpleNamespace(record=record, manifest=manifest),
                    SimpleNamespace(record=stale, manifest=second_manifest),
                )
            ),
        )
        assert not _fragment_matches_selected_documents(stale_fragment, left, right)
    finally:
        catalog.close()


def test_real_catalog_api_is_authorized_canonical_and_release_disabled(
    tmp_path: Path,
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
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
                f"/api/v1/explorer/results/{ref.result_id}",
                headers={"Cookie": cookie},
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["models"]["schema_version"] == (
                "traceback.integrated-explorer-models.v2"
            )
            assert payload["models"]["result_view_request"]["filter_id"]
            assert payload["eligibility"]["release_state"] == (
                "disabled_no_installed_authority"
            )
            assert canonical_json_bytes(payload) == body.rstrip(b"\n")
            compare_status, _, _ = _request(
                service,
                "GET",
                f"/api/v1/explorer/compare?left={ref.result_id}&right={ref.result_id}",
                headers={"Cookie": cookie},
            )
            assert compare_status == 400
    finally:
        catalog.close()


def test_http_routes_ignore_substituted_dispatch_globals_and_bind_result_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    cached = explorer.get(ref.result_id)
    cached_page = explorer.query(CatalogQuery(limit=1))
    monkeypatch.setattr(
        server_module,
        "_EXPLORER_DISPATCH",
        (
            lambda source, query: cached_page,
            lambda source, result_id: cached,
            lambda source, left, right: (_ for _ in ()).throw(
                AssertionError("substituted")
            ),
        ),
    )
    monkeypatch.setattr(
        server_module,
        "prepare_explorer_document_response",
        lambda source, document: {
            "models": {
                "catalog_ref": {"result_id": f"result_{'f' * 40}"},
                "private_path": "/private/subject.tsv",
            }
        },
    )
    monkeypatch.setattr(
        server_module,
        "prepare_explorer_comparison_response",
        lambda source, comparison: {
            "left_result_id": f"result_{'e' * 40}",
            "right_result_id": f"result_{'d' * 40}",
            "private_path": "/private/subject.tsv",
        },
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        with pytest.raises(TypeError):
            RunningLocalWebService.start(
                store=store,
                state_directory=tmp_path / "rejected-state",
                explorer=explorer,
                _explorer_dispatch=server_module._EXPLORER_DISPATCH,  # type: ignore[call-arg]
            )
        with RunningLocalWebService.start(
            store=store,
            state_directory=tmp_path / "state",
            explorer=explorer,
        ) as service:
            cookie, _ = _exchange(service)
            headers = {"Cookie": cookie}
            missing = f"result_{'0' * 40}"
            status, _, _ = _request(
                service,
                "GET",
                f"/api/v1/explorer/results/{missing}",
                headers=headers,
            )
            assert status == 404
            status, _, body = _request(
                service,
                "GET",
                f"/api/v1/explorer/results/{ref.result_id}",
                headers=headers,
            )
            assert status == 200
            assert b"private/subject" not in body
            status, _, body = _request(
                service,
                "GET",
                "/api/v1/explorer/catalog?limit=1&method_id=mth_nonexistent"
                "&method_version=9.9.9",
                headers=headers,
            )
            assert status == 200
            payload = json.loads(body)
            assert payload["results"] == []
            assert payload["query"]["method_refs"] == [
                {"method_id": "mth_nonexistent", "version": "9.9.9"}
            ]
            assert (
                _request(
                    service,
                    "GET",
                    f"/api/v1/explorer/compare?left={ref.result_id}&right={ref.result_id}",
                    headers=headers,
                )[0]
                == 400
            )
    finally:
        catalog.close()


def test_api_boundary_rejects_encoded_private_projection(tmp_path: Path) -> None:
    class UnsafeDocument:
        @staticmethod
        def model_dump(*, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "models": {
                    "result_view": {
                        "rows": [{"accessible_label": "source%2Fprivate%2Fcase.tsv"}]
                    }
                }
            }

    class UnsafeExplorer:
        @staticmethod
        def get(result_id: str) -> UnsafeDocument:
            assert result_id.startswith("result_")
            return UnsafeDocument()

    store = JobStore(tmp_path / "jobs.sqlite3")
    with RunningLocalWebService.start(
        store=store,
        state_directory=tmp_path / "state",
        explorer=UnsafeExplorer(),  # type: ignore[arg-type]
    ) as service:
        cookie, _ = _exchange(service)
        status, _, body = _request(
            service,
            "GET",
            f"/api/v1/explorer/results/result_{'1' * 40}",
            headers={"Cookie": cookie},
        )
        assert status == 400
        assert b"source" not in body


def test_packaged_dom_uses_exact_pair_endpoint_and_explicit_states() -> None:
    root = Path("traceback_runner/web/static")
    html = (root / "index.html").read_text()
    script = (root / "app.js").read_text()
    styles = (root / "styles.css").read_text()
    assert '<html lang="en">' in html
    assert "api/v1/explorer/compare" in script
    assert "sameMethod" not in script
    assert 'dataset.renderState = "ready"' in script
    assert "E12 longitudinal" in script
    assert "E13 sensitivity" in script
    assert "performance.now()" in script
    assert "@media (max-width:" in styles
    for content in (html, script, styles):
        lowered = content.lower()
        assert "http://" not in lowered
        assert "https://" not in lowered
        assert "//cdn" not in lowered


def test_catalog_query_projection_remains_bounded(tmp_path: Path) -> None:
    catalog, ref, _, _, explorer = _installed(tmp_path)
    try:
        page = explorer.query(CatalogQuery(limit=1))
        assert len(page.results) == 1
        assert page.results[0].ref == ref
        assert page.results[0].has_registered_view
    finally:
        catalog.close()


def test_local_browser_manifest_is_explicitly_unapproved_and_auditable() -> None:
    payload = json.loads(Path("artifacts/e14-local/browser-manifest.json").read_text())
    assert payload["approval_state"] == "local_unapproved"
    assert payload["application_render"]["render_state"] == "ready"
    assert payload["application_render"]["console_message_count"] == 0
    assert payload["memory"]["chromium_process_rss_bytes"] is None
    assert payload["screenshot"]["sha256"]
