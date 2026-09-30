"""Offline deterministic and adversarial tests for the E13 portable view."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

import evidence_inspector.portable_view as portable
from evidence_inspector.cell_origin_explorer import build_cell_origin_explorer_artifact
from evidence_inspector.cna_explorer import CnaSource
from evidence_inspector.compatibility import CompatibilityOutcome, TrustState
from evidence_inspector.fragment_explorer import build_fragment_explorer_view
from evidence_inspector.portable_view import (
    AccessibilityMetadata,
    ExactCell,
    ExactValueState,
    MeasurementKind,
    PortableSourceIdentity,
    PortableSurfaceState,
    PortableTrustContext,
    PortableVersions,
    PortableViewBuildRequest,
    PortableViewConflictError,
    PortableViewPermissionError,
    PortableViewStorageError,
    PortableViewTamperError,
    build_portable_view,
    publish_portable_view,
    replay_portable_view,
    verify_portable_view,
)
from evidence_inspector.provenance_drawer import ProvenanceDrawer
from evidence_inspector.result_view import (
    ResultViewFixture,
    SurfaceErrorCode,
    ViewSurfaceState,
    build_result_view,
    normalize_result_filters,
)
from tests.test_cell_origin_explorer import _request as cell_request
from tests.test_cna_explorer import _snapshot
from tests.test_fragment_explorer import _request as fragment_request
from tests.test_fragment_explorer import _source
from tests.test_provenance_drawer import _build_drawer
from tests.test_provenance_drawer import _request as drawer_request
from traceback_runner.contracts import (
    BundleContent,
    BundleMethodIdentity,
    ResultBundleManifestV2,
)
from traceback_runner.serialization import canonical_json_bytes


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest(
    suffix: str, method_id: str, version: str, method_sha256: str
) -> ResultBundleManifestV2:
    return ResultBundleManifestV2(
        record_id=f"record_{suffix}",
        workflow_release_id="release_synthetic",
        measurement_schema_versions=("schema_v1",),
        method=BundleMethodIdentity(
            method_id=method_id,
            version=version,
            method_definition_sha256=method_sha256,
        ),
        contents=(
            BundleContent(
                relative_path="result.json",
                sha256=hashlib.sha256(suffix.encode()).hexdigest(),
                size_bytes=10,
            ),
        ),
        signing_key_id="dev_signing_key",
    )


def _record_identity(
    *,
    kind: MeasurementKind,
    source: object,
    manifest: ResultBundleManifestV2,
    source_sha256: str,
    decision_sha256: str,
    policy_sha256: str,
    authority_sha256: str,
    outcome: CompatibilityOutcome,
) -> PortableSourceIdentity:
    record = source.record  # type: ignore[attr-defined]
    return PortableSourceIdentity(
        measurement_kind=kind,
        source_id=f"source_{record.result_id}",
        bundle_id=record.bundle_id,
        bundle_sha256=record.bundle_sha256,
        bundle_manifest_sha256=_digest(manifest),
        result_id=record.result_id,
        result_sha256=record.result_sha256,
        method_id=record.method.method_id,
        method_version=record.method.version,
        method_definition_sha256=record.method_definition_sha256,
        asset_sha256s=tuple(
            sorted(item.content_sha256 for item in record.method.assets)
        ),
        capability_sha256=_digest(record.current_capability),
        compatibility_decision_sha256=decision_sha256,
        compatibility_policy_sha256=policy_sha256,
        compatibility_authority_head_sha256=authority_sha256,
        compatibility_outcome=outcome,
        trust_state=record.trust_state,
        source_contract_sha256=source_sha256,
    )


def _cna_identity(
    *,
    kind: MeasurementKind,
    snapshot: object,
    result_sha256: str,
    drawer: ProvenanceDrawer,
    digit: str,
) -> tuple[PortableSourceIdentity, ResultBundleManifestV2]:
    method_id = {
        MeasurementKind.CNA_DOSAGE: "sample-internal-whole-chromosome-dosage-qc",
        MeasurementKind.CNA_SEGMENTED: "ichor-development-adapter",
    }[kind]
    method_sha256 = digit * 64
    manifest = _manifest(kind.value, method_id, "1.0.0", method_sha256)
    compatibility = drawer.replay_request.compatibility_request
    source = {
        MeasurementKind.CNA_DOSAGE: CnaSource.DOSAGE_QC,
        MeasurementKind.CNA_SEGMENTED: CnaSource.SEGMENTED_CNA,
    }[kind]
    asset_sha256s = tuple(
        sorted(
            item.content_sha256
            for item in snapshot.layers.assets  # type: ignore[attr-defined,union-attr]
            if item.source == source
        )
    )
    return (
        PortableSourceIdentity(
            measurement_kind=kind,
            source_id=f"source_{kind.value}",
            bundle_id=f"bundle_{kind.value}",
            bundle_sha256=("c" if digit == "7" else "d") * 64,
            bundle_manifest_sha256=_digest(manifest),
            result_id=f"result_{kind.value}",
            result_sha256=result_sha256,
            method_id=method_id,
            method_version="1.0.0",
            method_definition_sha256=method_sha256,
            asset_sha256s=asset_sha256s,
            capability_sha256=("1" if digit == "7" else "2") * 64,
            compatibility_decision_sha256=drawer.compatibility_decision_sha256,
            compatibility_policy_sha256=compatibility.trusted_policy_sha256,
            compatibility_authority_head_sha256=(
                compatibility.trusted_authority_head_sha256
            ),
            compatibility_outcome=drawer.compatibility_outcome,
            trust_state=TrustState.VERIFIED,
            source_contract_sha256=_digest(snapshot),
        ),
        manifest,
    )


@pytest.fixture
def integrated_request(tmp_path: Path) -> PortableViewBuildRequest:
    left, right = _source("alpha"), _source("beta")
    fragment = build_fragment_explorer_view(fragment_request(left, right))
    cell_input = cell_request()
    cell = build_cell_origin_explorer_artifact(cell_input)
    _, _, cna = _snapshot(tmp_path / "cna")
    drawer = _build_drawer(drawer_request())

    fragment_manifests = [
        _manifest(
            source.record.result_id,
            source.record.method.method_id,
            source.record.method.version,
            source.record.method_definition_sha256,
        )
        for source in fragment.request.sources
    ]
    identities = [
        _record_identity(
            kind=MeasurementKind.FRAGMENT,
            source=source,
            manifest=manifest,
            source_sha256=_digest(fragment),
            decision_sha256=fragment.compatibility.decision_sha256,
            policy_sha256=fragment.request.trusted_policy_sha256,
            authority_sha256=fragment.request.trusted_authority_head_sha256,
            outcome=fragment.compatibility.outcome,
        )
        for source, manifest in zip(fragment.request.sources, fragment_manifests)
    ]
    cell_source = cell_input.result_view_request.sources[0]
    cell_manifest = _manifest(
        "cell_origin",
        cell_source.record.method.method_id,
        cell_source.record.method.version,
        cell_source.record.method_definition_sha256,
    )
    identities.append(
        _record_identity(
            kind=MeasurementKind.CELL_ORIGIN,
            source=cell_source,
            manifest=cell_manifest,
            source_sha256=_digest(cell),
            decision_sha256=cell_source.compatibility_identity.decision_sha256,
            policy_sha256=cell_source.compatibility_identity.policy_sha256,
            authority_sha256=(cell_source.compatibility_identity.authority_head_sha256),
            outcome=cell_source.compatibility_decision.outcome,
        )
    )
    result_bindings = {
        item.source: item.result_sha256 for item in cna.provenance.input_bindings
    }
    dosage, dosage_manifest = _cna_identity(
        kind=MeasurementKind.CNA_DOSAGE,
        snapshot=cna,
        result_sha256=result_bindings[CnaSource.DOSAGE_QC],
        drawer=drawer,
        digit="7",
    )
    segmented, segmented_manifest = _cna_identity(
        kind=MeasurementKind.CNA_SEGMENTED,
        snapshot=cna,
        result_sha256=result_bindings[CnaSource.SEGMENTED_CNA],
        drawer=drawer,
        digit="8",
    )
    identities.extend((dosage, segmented))
    manifests = fragment_manifests + [
        cell_manifest,
        dosage_manifest,
        segmented_manifest,
    ]
    result_view = build_result_view(cell_input.result_view_request)
    fixture = ResultViewFixture(
        fixture_id="fixture_portable_ready",
        surface_state=ViewSurfaceState.READY,
        accessible_label="Portable result view ready",
        view=result_view,
    )
    return PortableViewBuildRequest(
        view_id="portable_view_alpha",
        surface_fixture=fixture,
        filters=cell_input.result_view_request.filters,
        fragment_view=fragment,
        cell_origin_artifact=cell,
        cna_snapshot=cna,
        provenance_drawer=drawer,
        source_identities=tuple(sorted(identities, key=lambda item: item.sort_key)),
        bundle_manifests=tuple(sorted(manifests, key=_digest)),
    )


@pytest.fixture
def trust_context(integrated_request: PortableViewBuildRequest) -> PortableTrustContext:
    return PortableTrustContext(
        expected_source_identities=integrated_request.source_identities,
        expected_bundle_manifest_sha256s=tuple(
            sorted(_digest(item) for item in integrated_request.bundle_manifests)
        ),
    )


def test_integrated_view_is_exact_accessible_and_byte_deterministic(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
) -> None:
    first, first_table = build_portable_view(
        integrated_request, trust_context=trust_context
    )
    second, second_table = build_portable_view(
        integrated_request, trust_context=trust_context
    )

    assert first == second
    assert first_table == second_table
    assert first.surface_state == PortableSurfaceState.SUCCESS
    assert {table.measurement_kind for table in first.tables} == set(MeasurementKind)
    assert first.accessibility.zoom_percent_supported == 200
    assert first.accessibility.keyboard_navigation == "native_table_navigation"
    assert first.accessibility.focus_order == (
        "status",
        "filters",
        "tables",
        "provenance",
    )
    assert first.compatibility.delta_state == "not_allowed_incompatible"
    assert first.synthetic_local_only and not first.product_release_authorized
    assert first_table.startswith(b"table_schema\tmeasurement_kind")
    assert b"Presentation label" not in first_table
    tables = {item.table_id: item for item in first.tables}
    assert {
        "table_cna_bins_dosage_qc",
        "table_cna_bins_segmented_cna",
        "table_cna_masks",
        "table_cna_insufficiency_dosage_qc",
        "table_cna_insufficiency_segmented_cna",
        "table_cna_coordinate_grid_dosage_qc",
        "table_cna_coordinate_grid_segmented_cna",
        "table_cna_assets_dosage_qc",
        "table_cna_assets_segmented_cna",
        "table_cna_method_dosage_qc",
        "table_cna_method_segmented_cna",
    } <= set(tables)
    assert {cell.column_id for cell in tables["table_cna_segments"].rows[0].cells} >= {
        "native_span_bins",
        "segment_index",
        "source",
    }
    assert {
        cell.column_id for cell in tables["table_cna_model_candidates"].rows[0].cells
    } >= {
        "candidate_index",
        "initial_ploidy",
        "estimated_normal_fraction",
        "fraction_genome_subclonal",
        "fraction_cna_subclonal",
        "bic",
    }
    replay_portable_view(
        integrated_request, first, first_table, trust_context=trust_context
    )


@pytest.mark.parametrize(
    ("surface_state", "error_code", "expected"),
    [
        (ViewSurfaceState.LOADING, None, PortableSurfaceState.LOADING),
        (
            ViewSurfaceState.ERROR,
            SurfaceErrorCode.LOAD_FAILED,
            PortableSurfaceState.ERROR,
        ),
    ],
)
def test_non_ready_states_withhold_all_exact_rows(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    surface_state: ViewSurfaceState,
    error_code: SurfaceErrorCode | None,
    expected: PortableSurfaceState,
) -> None:
    fixture = ResultViewFixture(
        fixture_id=f"fixture_{surface_state.value}",
        surface_state=surface_state,
        accessible_label=f"Portable {surface_state.value}",
        error_code=error_code,
        error_message=("Unable to load portable view" if error_code else None),
    )
    request = integrated_request.model_copy(update={"surface_fixture": fixture})
    view, table = build_portable_view(request, trust_context=trust_context)
    assert view.surface_state == expected
    assert not view.tables
    assert table.count(b"\n") == 1


def test_empty_state_withholds_all_exact_rows(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
) -> None:
    source_request = cell_request().result_view_request
    filters = normalize_result_filters(trust_states=(TrustState.REVOKED,))
    empty_result = build_result_view(
        source_request.model_copy(update={"filters": filters})
    )
    fixture = ResultViewFixture(
        fixture_id="fixture_empty",
        surface_state=ViewSurfaceState.EMPTY,
        accessible_label="Portable result view empty",
        view=empty_result,
    )
    request = integrated_request.model_copy(
        update={"surface_fixture": fixture, "filters": filters}
    )
    view, table = build_portable_view(request, trust_context=trust_context)
    assert view.surface_state == PortableSurfaceState.EMPTY
    assert not view.tables
    assert table.count(b"\n") == 1


def test_partial_stale_revoked_and_incompatible_states_are_explicit(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
) -> None:
    partial, _ = build_portable_view(
        integrated_request.model_copy(update={"cna_snapshot": None}),
        trust_context=trust_context,
    )
    assert partial.surface_state == PortableSurfaceState.PARTIAL

    identities = list(integrated_request.source_identities)
    index = next(
        i
        for i, item in enumerate(identities)
        if item.measurement_kind == MeasurementKind.CNA_DOSAGE
    )
    identities[index] = identities[index].model_copy(
        update={"trust_state": TrustState.UNVERIFIED}
    )
    stale, _ = build_portable_view(
        integrated_request.model_copy(update={"source_identities": tuple(identities)}),
        trust_context=PortableTrustContext(
            expected_source_identities=tuple(identities),
            expected_bundle_manifest_sha256s=trust_context.expected_bundle_manifest_sha256s,
        ),
    )
    assert stale.surface_state == PortableSurfaceState.STALE
    assert not stale.tables

    identities[index] = identities[index].model_copy(
        update={
            "trust_state": TrustState.REVOKED,
            "compatibility_outcome": CompatibilityOutcome.INCOMPATIBLE,
        }
    )
    revoked, _ = build_portable_view(
        integrated_request.model_copy(update={"source_identities": tuple(identities)}),
        trust_context=PortableTrustContext(
            expected_source_identities=tuple(identities),
            expected_bundle_manifest_sha256s=trust_context.expected_bundle_manifest_sha256s,
        ),
    )
    assert revoked.surface_state == PortableSurfaceState.REVOKED
    assert revoked.compatibility.delta_state == "not_allowed_incompatible"
    assert not revoked.tables


def test_publication_is_atomic_no_overwrite_and_verifiable(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    destination = tmp_path / "portable-alpha"
    publish_portable_view(
        destination,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    verified = verify_portable_view(destination, trust_context=trust_context)
    assert verified.view == view
    assert verified.accessible_table_bytes == table
    assert {item.name for item in destination.iterdir()} == {
        "manifest.json",
        "view.json",
        "accessible-table.tsv",
    }
    with pytest.raises(PortableViewConflictError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: view.source_identities,
        )


def test_source_change_and_destination_race_fail_closed_without_stage_leaks(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    parent = tmp_path / "publish"
    parent.mkdir()
    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            parent / "source-change",
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: (),
        )

    destination = parent / "race"

    def race() -> tuple[PortableSourceIdentity, ...]:
        destination.mkdir()
        return view.source_identities

    with pytest.raises(PortableViewConflictError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=race,
        )
    assert not any(item.name.startswith(".race.") for item in parent.iterdir())
    assert not any(item.name.startswith(".source-change.") for item in parent.iterdir())


def test_root_swap_and_symlink_tamper_fail_closed(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "moved"

    def swap_root() -> tuple[PortableSourceIdentity, ...]:
        parent.rename(moved)
        parent.mkdir()
        return view.source_identities

    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            parent / "artifact",
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=swap_root,
        )

    published = tmp_path / "published"
    publish_portable_view(
        published,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    table_path = published / "accessible-table.tsv"
    published.chmod(0o700)
    table_path.unlink()
    table_path.symlink_to(tmp_path / "outside.tsv")
    published.chmod(0o500)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(published, trust_context=trust_context)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PermissionError(errno.EACCES, "denied"), PortableViewPermissionError),
        (OSError(errno.ENOSPC, "full"), PortableViewStorageError),
    ],
)
def test_typed_publish_errors_and_cleanup(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    expected: type[Exception],
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)

    def fail_rename(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(portable, "rename_directory_exclusive_at", fail_rename)
    destination = tmp_path / "typed-error"
    with pytest.raises(expected):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: view.source_identities,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".typed-error.*"))


def test_mutation_bounds_and_privacy_fail_closed(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    with pytest.raises(PortableViewTamperError):
        replay_portable_view(
            integrated_request,
            view,
            table + b"tamper",
            trust_context=trust_context,
        )
    with pytest.raises(ValidationError):
        PortableSourceIdentity.model_validate(
            {
                **view.source_identities[0].model_dump(mode="json"),
                "source_id": "sample_id:secret",
            }
        )
    with pytest.raises(ValidationError):
        PortableSourceIdentity.model_validate(
            {
                **view.source_identities[0].model_dump(mode="json"),
                "asset_sha256s": tuple(f"{index:064x}" for index in range(257)),
            }
        )


def test_encoded_private_identifiers_are_rejected_but_typed_digests_are_allowed(
    integrated_request: PortableViewBuildRequest,
) -> None:
    identity = integrated_request.source_identities[0]
    encoded = (
        base64.urlsafe_b64encode(b"sample_id:synthetic-secret").decode().rstrip("=")
    )
    nested = base64.urlsafe_b64encode(encoded.encode()).decode().rstrip("=")
    for private_value in (encoded, nested):
        with pytest.raises(ValidationError):
            PortableSourceIdentity.model_validate(
                {**identity.model_dump(mode="json"), "source_id": private_value}
            )
    cell = ExactCell(
        column_id="artifact_sha256",
        value_state=ExactValueState.OBSERVED,
        sha256_value="a" * 64,
        unit_id="sha256",
    )
    assert cell.sha256_value == "a" * 64


def test_self_consistent_rewrite_is_rejected_by_external_trust(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
) -> None:
    identities = list(integrated_request.source_identities)
    index = next(
        index
        for index, item in enumerate(identities)
        if item.measurement_kind == MeasurementKind.CNA_DOSAGE
    )
    identities[index] = identities[index].model_copy(update={"result_sha256": "9" * 64})
    rewritten = integrated_request.model_copy(
        update={"source_identities": tuple(identities)}
    )
    with pytest.raises(PortableViewTamperError):
        build_portable_view(rewritten, trust_context=trust_context)


def test_accessibility_and_versions_are_exact_fixed_sets() -> None:
    with pytest.raises(ValidationError):
        AccessibilityMetadata(focus_order=("status", "tables", "filters", "provenance"))
    with pytest.raises(ValidationError):
        PortableVersions(plot_data_schemas=("traceback.fragment-explorer-view.v1",))
    with pytest.raises(ValidationError):
        PortableVersions(plot_spec_schemas=("traceback.fragment-explorer-spec.v1",))


@pytest.mark.parametrize("mutation", ["rewrite", "hardlink", "symlink", "fifo"])
def test_callback_staged_file_mutation_fails_and_cleans_up(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    mutation: str,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    parent = tmp_path / mutation
    parent.mkdir()
    destination = parent / "artifact"
    outside = tmp_path / f"outside-{mutation}"
    outside.write_bytes(b"outside")

    def mutate() -> tuple[PortableSourceIdentity, ...]:
        stage = next(
            item
            for item in parent.iterdir()
            if item.is_dir() and item.name.startswith(".artifact.")
        )
        target = stage / "view.json"
        if mutation == "rewrite":
            target.write_bytes(b"{}")
        else:
            target.unlink()
            if mutation == "hardlink":
                os.link(outside, target)
            elif mutation == "symlink":
                target.symlink_to(outside)
            else:
                os.mkfifo(target)
        return view.source_identities

    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=mutate,
        )
    assert not destination.exists()
    assert not any(item.name.startswith(".artifact.") for item in parent.iterdir())


def test_rename_boundary_mutation_is_detected_and_quarantined(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    destination = tmp_path / "artifact"
    original_rename = portable.rename_directory_exclusive_at

    def mutate_after_rename(parent_fd: int, source: str, target: str) -> None:
        original_rename(parent_fd, source, target)
        if target == destination.name:
            installed_fd = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=parent_fd,
            )
            try:
                os.chmod("accessible-table.tsv", 0o600, dir_fd=installed_fd)
                table_fd = os.open(
                    "accessible-table.tsv",
                    os.O_WRONLY | os.O_TRUNC,
                    dir_fd=installed_fd,
                )
                try:
                    os.write(table_fd, b"corrupt-at-rename-boundary")
                finally:
                    os.close(table_fd)
            finally:
                os.close(installed_fd)

    monkeypatch.setattr(portable, "rename_directory_exclusive_at", mutate_after_rename)
    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: view.source_identities,
        )
    assert not destination.exists()
    assert not any(
        item.name.startswith(".artifact.invalid.") for item in tmp_path.iterdir()
    )


def test_failed_post_publish_validation_does_not_delete_a_replacement_winner(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    destination = tmp_path / "artifact"
    winner = tmp_path / "winner"
    winner.mkdir()
    (winner / "winner-marker").write_text("preserve", encoding="utf-8")
    displaced = tmp_path / "displaced-artifact"
    original_rename = portable.rename_directory_exclusive_at

    def replace_after_rename(parent_fd: int, source: str, target: str) -> None:
        original_rename(parent_fd, source, target)
        if target == destination.name:
            os.rename(
                destination.name,
                displaced.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.rename(
                winner.name,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )

    monkeypatch.setattr(portable, "rename_directory_exclusive_at", replace_after_rename)
    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: view.source_identities,
        )
    assert (destination / "winner-marker").read_text(encoding="utf-8") == "preserve"


def test_publish_final_vector_catches_in_place_manifest_mutation_at_table_open(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    destination = tmp_path / "artifact"
    original_open = portable.os.open
    original_rename = portable.rename_directory_exclusive_at
    installed = False
    mutated = False

    def mark_installed(parent_fd: int, source: str, target: str) -> None:
        nonlocal installed
        original_rename(parent_fd, source, target)
        if target == destination.name:
            installed = True

    def mutate_manifest_when_table_opens(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal mutated
        if installed and path == "accessible-table.tsv" and not mutated:
            mutated = True
            manifest = destination / "manifest.json"
            manifest.chmod(0o600)
            payload = bytearray(manifest.read_bytes())
            payload[-2] = ord("0") if payload[-2] != ord("0") else ord("1")
            manifest.write_bytes(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(portable, "rename_directory_exclusive_at", mark_installed)
    monkeypatch.setattr(portable.os, "open", mutate_manifest_when_table_opens)
    with pytest.raises(PortableViewTamperError):
        publish_portable_view(
            destination,
            view=view,
            accessible_table=table,
            trust_context=trust_context,
            source_identity_verifier=lambda: view.source_identities,
        )
    assert mutated
    assert not destination.exists()


def test_verify_rechecks_named_root_and_rejects_swap(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    root = tmp_path / "root"
    publish_portable_view(
        root,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    moved = tmp_path / "moved-root"
    original = portable._parse_canonical
    swapped = False

    def swap_then_parse(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(moved)
            root.mkdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(portable, "_parse_canonical", swap_then_parse)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(root, trust_context=trust_context)


def test_verify_rejects_manifest_replacement_while_opening_second_file(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    root = tmp_path / "root"
    publish_portable_view(
        root,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    replacement = tmp_path / "replacement-manifest.json"
    replacement.write_bytes((root / "manifest.json").read_bytes())
    original_open = portable.os.open
    replaced = False

    def racing_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "accessible-table.tsv" and dir_fd is not None and not replaced:
            replaced = True
            root.chmod(0o700)
            os.replace(replacement, root / "manifest.json")
            root.chmod(0o500)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(portable.os, "open", racing_open)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(root, trust_context=trust_context)
    assert replaced


def test_verify_final_vector_catches_in_place_manifest_mutation_at_table_open(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    root = tmp_path / "root"
    publish_portable_view(
        root,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    original_open = portable.os.open
    table_open_count = 0
    mutated = False

    def mutate_manifest_on_final_table_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal table_open_count, mutated
        if path == "accessible-table.tsv" and dir_fd is not None:
            table_open_count += 1
            if table_open_count == 2:
                mutated = True
                manifest = root / "manifest.json"
                manifest.chmod(0o600)
                payload = bytearray(manifest.read_bytes())
                payload[-2] = ord("0") if payload[-2] != ord("0") else ord("1")
                manifest.write_bytes(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(portable.os, "open", mutate_manifest_on_final_table_open)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(root, trust_context=trust_context)
    assert mutated


@pytest.mark.parametrize(
    "target", ["root", "manifest.json", "accessible-table.tsv", "view.json"]
)
def test_verify_rejects_write_bit_mode_changes(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    target: str,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    root = tmp_path / "root"
    publish_portable_view(
        root,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o500
    for filename in ("manifest.json", "accessible-table.tsv", "view.json"):
        assert stat.S_IMODE((root / filename).stat().st_mode) == 0o400
    changed = root if target == "root" else root / target
    changed.chmod(0o700 if target == "root" else 0o600)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(root, trust_context=trust_context)


@pytest.mark.parametrize(
    "filename", ["manifest.json", "accessible-table.tsv", "view.json"]
)
def test_verify_rejects_final_name_inode_swap_for_every_file(
    integrated_request: PortableViewBuildRequest,
    trust_context: PortableTrustContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    view, table = build_portable_view(integrated_request, trust_context=trust_context)
    root = tmp_path / "root"
    publish_portable_view(
        root,
        view=view,
        accessible_table=table,
        trust_context=trust_context,
        source_identity_verifier=lambda: view.source_identities,
    )
    replacement = tmp_path / f"replacement-{filename}"
    replacement.write_bytes((root / filename).read_bytes())
    original_parse = portable._parse_canonical
    replaced = False

    def replace_then_parse(*args: object, **kwargs: object) -> object:
        nonlocal replaced
        if not replaced:
            replaced = True
            root.chmod(0o700)
            os.replace(replacement, root / filename)
            root.chmod(0o500)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(portable, "_parse_canonical", replace_then_parse)
    with pytest.raises(PortableViewTamperError):
        verify_portable_view(root, trust_context=trust_context)
