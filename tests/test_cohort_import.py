"""D06 verified import, D05 binding, bounds, and adversarial trust tests."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

import evidence_inspector.cohort_import as cohort_import_module
from evidence_inspector.cohort_import import (
    CohortImportConflict,
    CohortImportError,
    CohortImportFilesystemError,
    CohortRecordCatalog,
)
from evidence_inspector.cohort_manifest import MeasurementAnchor
from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    CatalogError,
    CatalogQuery,
    CatalogUnsupportedSchema,
    ResultBundleReader,
    ResultBundleReaderRegistry,
    ResultCatalog,
)
from tests.test_bundles import _bundle, _downgrade_to_v1
from tests.test_cohort_manifest import _known_run_revision, _manifest
from tests.test_method_registry import (
    T0,
    T1,
    _definition,
    _head,
    _qualification,
    _registry,
    _role,
)
from tests.test_provider_linkage import _consume, _create_approval
from tests.test_provider_linkage_store import _pins
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import RevokedKeyError

pytest_plugins = ("tests.test_cohort_manifest",)


def _authority():
    definition = _definition(method_id="mth_fragment_aligned_reference_span")
    qualification = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_primary",
        state="qualified",
        effective_at=T0,
    )
    role = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role="provider_primary",
        effective_at=T0,
    )
    registry = _registry(
        definition,
        qualifications=(qualification,),
        roles=(role,),
    )
    head = _head(registry)
    from evidence_inspector.method_registry import (
        authority_head_sha256,
        resolve_current_capability,
    )

    head_sha256 = authority_head_sha256(head)
    capability = resolve_current_capability(
        registry,
        head,
        head_sha256,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    return registry, head, head_sha256, capability


def _setup(tmp_path: Path, live):
    store, _, authority, member = live
    registry, head, head_sha256, capability = _authority()
    import_root = tmp_path / "imports"
    import_root.mkdir(parents=True)
    bundle, key, trust = _bundle(
        import_root / "incoming",
        method={
            "method_id": capability.method_ref.method_id,
            "version": capability.method_ref.version,
            "method_definition_sha256": capability.method_definition_sha256,
        },
    )
    anchor = MeasurementAnchor(
        measurement_definition_sha256=capability.method_definition_sha256,
        anchor_definition_sha256="9" * 64,
        authority_sha256="a" * 64,
    )
    manifest = _manifest(authority, (member,), measurement_anchor=anchor)
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": import_root},
        trust_store=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    cohorts = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    return (
        cohorts,
        results,
        manifest,
        member,
        bundle,
        key,
        trust,
        registry,
        head,
        head_sha256,
        capability,
    )


def _import(values, **updates):
    (
        cohorts,
        _,
        manifest,
        member,
        _,
        _,
        _,
        registry,
        head,
        head_sha256,
        capability,
    ) = values
    request = {
        "manifest_history": (manifest,),
        "provider_namespace": member.provider_namespace,
        "analysis_record_id": member.analysis_record_id,
        "root_id": "root_primary",
        "relative_path": "incoming/record",
        "registry": registry,
        "authority_head": head,
        "expected_authority_head_sha256": head_sha256,
        "capability": capability,
    }
    request.update(updates)
    return cohorts.import_bundle(**request)


def _binding_path(root: Path, binding) -> Path:
    return root / (f"{binding.cohort_manifest_sha256}.{binding.binding_id}.json")


def test_verified_bundle_is_idempotently_bound_to_exact_live_member(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    binding = _import(values)
    assert _import(values) == binding
    assert values[0].bindings_for_manifest((values[2],)) == (binding,)
    assert values[1].query(CatalogQuery()).results == (binding.result,)
    assert binding.analysis_record_id == values[3].analysis_record_id
    assert binding.member_sha256
    assert binding.reader_id == "reader_result_bundle_v2"
    assert binding.reader_minimum_version == binding.reader_maximum_version == 2
    assert binding.synthetic_only and not binding.clinical_use_authorized
    serialized = binding.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "subject_" not in serialized


def test_same_verified_record_can_bind_to_a_new_manifest_version(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    first = _import(values)
    previous = values[2]
    second = _manifest(
        previous.provider_authorities[0],
        previous.members,
        cohort_id=previous.cohort_id,
        version=2,
        previous_manifest_sha256=first.cohort_manifest_sha256,
        created_at=previous.created_at + timedelta(seconds=1),
        measurement_anchor=previous.measurement_anchor,
        policies=previous.policies.model_copy(update={"missingness_sha256": "b" * 64}),
    )
    second_binding = _import(values, manifest_history=(previous, second))
    assert second_binding.result == first.result
    assert second_binding.binding_id != first.binding_id
    assert values[0].bindings_for_manifest((previous, second)) == (second_binding,)


def test_binding_count_limit_rejects_without_replacing_existing_record(
    tmp_path: Path, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _setup(tmp_path, live)
    first = _import(values)
    previous = values[2]
    second = _manifest(
        previous.provider_authorities[0],
        previous.members,
        cohort_id=previous.cohort_id,
        version=2,
        previous_manifest_sha256=first.cohort_manifest_sha256,
        created_at=previous.created_at + timedelta(seconds=1),
        measurement_anchor=previous.measurement_anchor,
        policies=previous.policies.model_copy(update={"missingness_sha256": "b" * 64}),
    )
    monkeypatch.setattr(cohort_import_module, "MAX_BINDINGS", 1)
    with pytest.raises(CohortImportFilesystemError, match="bound"):
        _import(values, manifest_history=(previous, second))
    assert values[0].bindings_for_manifest((previous,)) == (first,)


def test_import_has_no_network_path(
    tmp_path: Path, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    values = _setup(tmp_path, live)

    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert _import(values)


def test_tampered_bundle_is_rejected_before_either_index(tmp_path: Path, live) -> None:
    values = _setup(tmp_path, live)
    measurement = values[4] / "measurements/fragment-length.v1.json"
    measurement.write_bytes(measurement.read_bytes() + b" ")
    with pytest.raises(Exception, match="checksum mismatch|invalid measurement"):
        _import(values)
    assert values[1].query(CatalogQuery()).empty
    assert list((tmp_path / "cohort-records").iterdir()) == []


def test_revoked_and_wrong_purpose_signatures_never_index(tmp_path: Path, live) -> None:
    revoked = _setup(tmp_path / "revoked", live)
    revoked[6].revoke(revoked[5].key_id)
    with pytest.raises(RevokedKeyError):
        _import(revoked)
    assert revoked[1].query(CatalogQuery()).empty

    wrong = _setup(tmp_path / "wrong", live)
    signature_path = wrong[4] / "bundle.sig"
    signature = json.loads(signature_path.read_bytes())
    signature["purpose"] = "release"
    signature_path.write_bytes(canonical_json_bytes(signature))
    with pytest.raises(Exception, match="signature|purpose|invalid"):
        _import(wrong)
    assert wrong[1].query(CatalogQuery()).empty


def test_unsupported_manifest_schema_is_rejected_before_index(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    _downgrade_to_v1(values[4], values[5])
    with pytest.raises(CatalogUnsupportedSchema, match="unsupported"):
        _import(values)
    assert values[1].query(CatalogQuery()).empty


def test_malformed_extra_symlink_and_oversize_inventory_reject(
    tmp_path: Path, live
) -> None:
    extra = _setup(tmp_path / "extra", live)
    (extra[4] / "unexpected.json").write_text("{}")
    with pytest.raises(Exception, match="inventory|unexpected|import failed"):
        _import(extra)
    assert extra[1].query(CatalogQuery()).empty

    symlinked = _setup(tmp_path / "symlink", live)
    source = symlinked[4]
    source.rename(source.parent / "real-record")
    source.symlink_to(source.parent / "real-record", target_is_directory=True)
    with pytest.raises(Exception, match="import path|import failed"):
        _import(symlinked)
    assert symlinked[1].query(CatalogQuery()).empty

    oversize = _setup(tmp_path / "oversize", live)
    report = oversize[4] / "report.html"
    report.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(Exception, match="byte bound|import failed"):
        _import(oversize)
    assert oversize[1].query(CatalogQuery()).empty


def test_nonmember_and_wrong_measurement_anchor_reject_before_result_index(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    with pytest.raises(CohortImportError, match="exact cohort member"):
        _import(values, analysis_record_id="analysis_" + "f" * 32)
    assert values[1].query(CatalogQuery()).empty

    wrong_anchor = values[2].model_copy(
        update={
            "measurement_anchor": values[2].measurement_anchor.model_copy(
                update={"measurement_definition_sha256": "f" * 64}
            )
        }
    )
    with pytest.raises(CohortImportError, match="measurement anchor"):
        _import(values, manifest_history=(wrong_anchor,))
    assert values[1].query(CatalogQuery()).empty


def test_revocation_after_import_withholds_binding_on_read(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    _import(values)
    values[6].revoke(values[5].key_id)
    with pytest.raises(RevokedKeyError):
        values[0].bindings_for_manifest((values[2],))


def test_linkage_store_advance_withholds_stale_manifest_binding(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    _import(values)
    revision = _known_run_revision(
        linkage_id="linkage_" + "d" * 32,
        subject="subject_" + "d" * 32,
        collection="collection_" + "d" * 32,
        specimen="specimen_" + "d" * 32,
        analysis="analysis_" + "d" * 32,
        measurement="measurement_" + "d" * 32,
        source="projection_" + "d" * 32,
        run_digit="d",
    )
    authorized, _ = _consume(revision, (_create_approval(revision, "d"),))
    live[0].commit_authorized_revision(authorized)
    with pytest.raises(CohortImportError, match="current and trusted"):
        values[0].bindings_for_manifest((values[2],))


def test_binding_file_tamper_and_symlink_fail_closed(tmp_path: Path, live) -> None:
    tampered = _setup(tmp_path / "tampered", live)
    binding = _import(tampered)
    path = _binding_path(tmp_path / "tampered/cohort-records", binding)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(CohortImportFilesystemError, match="invalid"):
        tampered[0].bindings_for_manifest((tampered[2],))

    symlinked = _setup(tmp_path / "index-symlink", live)
    binding = _import(symlinked)
    path = _binding_path(tmp_path / "index-symlink/cohort-records", binding)
    target = path.with_suffix(".saved")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(CohortImportFilesystemError, match="invalid"):
        symlinked[0].bindings_for_manifest((symlinked[2],))

    semantic = _setup(tmp_path / "semantic", live)
    binding = _import(semantic)
    path = _binding_path(tmp_path / "semantic/cohort-records", binding)
    payload = binding.model_dump(mode="json")
    payload["denominator_contribution"] = not binding.denominator_contribution
    path.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(CohortImportConflict, match="manifest"):
        semantic[0].bindings_for_manifest((semantic[2],))

    exposed = _setup(tmp_path / "exposed", live)
    binding = _import(exposed)
    path = _binding_path(tmp_path / "exposed/cohort-records", binding)
    path.chmod(0o644)
    with pytest.raises(CohortImportFilesystemError, match="invalid"):
        exposed[0].bindings_for_manifest((exposed[2],))

    linked = _setup(tmp_path / "hardlink", live)
    binding = _import(linked)
    path = _binding_path(tmp_path / "hardlink/cohort-records", binding)
    os.link(path, tmp_path / "hardlink-copy")
    with pytest.raises(CohortImportFilesystemError, match="invalid"):
        linked[0].bindings_for_manifest((linked[2],))


def test_index_root_replacement_and_foreign_inventory_fail_closed(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    root = tmp_path / "cohort-records"
    root.rename(tmp_path / "displaced")
    root.mkdir()
    with pytest.raises(CohortImportFilesystemError, match="root changed"):
        _import(values)

    foreign = _setup(tmp_path / "foreign", live)
    (tmp_path / "foreign/cohort-records/note.txt").write_text("unsafe")
    with pytest.raises(CohortImportFilesystemError, match="inventory"):
        _import(foreign)
    assert foreign[1].query(CatalogQuery()).empty


def test_conflicting_second_bundle_for_same_member_is_rejected(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    first = _import(values)
    second_path, second_key, _ = _bundle(
        (tmp_path / "imports/second"),
        method={
            "method_id": values[10].method_ref.method_id,
            "version": values[10].method_ref.version,
            "method_definition_sha256": values[10].method_definition_sha256,
        },
    )
    values[6].add_signing_key(second_key)
    assert second_path.exists()
    with pytest.raises(Exception, match="identity conflict|binding conflicts"):
        _import(values, relative_path="second/record")
    assert values[0].bindings_for_manifest((values[2],)) == (first,)


def test_reader_registry_range_is_explicit_and_cannot_be_substituted(
    tmp_path: Path, live
) -> None:
    unsupported = ResultBundleReaderRegistry(
        readers=(
            ResultBundleReader(
                reader_id="reader_result_bundle_v3",
                minimum_version=3,
                maximum_version=3,
                measurement_schema_versions=("traceback.fragment-measurement.v1",),
            ),
        )
    )
    values = _setup(tmp_path, live)
    with pytest.raises(CohortImportError, match="does not match"):
        CohortRecordCatalog(
            tmp_path / "other-index",
            result_catalog=values[1],
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            reader_registry=unsupported,
        )

    class FakeRegistry(ResultBundleReaderRegistry):
        pass

    fake = FakeRegistry.model_validate(
        DEFAULT_RESULT_BUNDLE_READER_REGISTRY.model_dump(mode="python")
    )
    with pytest.raises(TypeError, match="exact reader"):
        CohortRecordCatalog(
            tmp_path / "fake-index",
            result_catalog=values[1],
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            reader_registry=fake,
        )


def test_unsupported_reader_range_rejects_before_result_index(
    tmp_path: Path, live
) -> None:
    store, _, authority, member = live
    registry, head, head_sha256, capability = _authority()
    import_root = tmp_path / "imports"
    import_root.mkdir()
    _, _, trust = _bundle(
        import_root / "incoming",
        method={
            "method_id": capability.method_ref.method_id,
            "version": capability.method_ref.version,
            "method_definition_sha256": capability.method_definition_sha256,
        },
    )
    unsupported = ResultBundleReaderRegistry(
        readers=(
            ResultBundleReader(
                reader_id="reader_result_bundle_v3",
                minimum_version=3,
                maximum_version=3,
                measurement_schema_versions=("traceback.fragment-measurement.v1",),
            ),
        )
    )
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": import_root},
        trust_store=trust,
        reader_registry=unsupported,
    )
    anchor = MeasurementAnchor(
        measurement_definition_sha256=capability.method_definition_sha256,
        anchor_definition_sha256="9" * 64,
        authority_sha256="a" * 64,
    )
    manifest = _manifest(authority, (member,), measurement_anchor=anchor)
    cohorts = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=unsupported,
    )
    with pytest.raises(CatalogUnsupportedSchema, match="unsupported"):
        cohorts.import_bundle(
            manifest_history=(manifest,),
            provider_namespace=member.provider_namespace,
            analysis_record_id=member.analysis_record_id,
            root_id="root_primary",
            relative_path="incoming/record",
            registry=registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
        )
    assert results.query(CatalogQuery()).empty


def test_model_copy_authority_and_reference_corruption_fail_closed(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    malformed = values[10].model_copy(update={"method_ref": None})
    with pytest.raises(CohortImportError, match="authority contract"):
        _import(values, capability=malformed)
    assert values[1].query(CatalogQuery()).empty

    binding = _import(values)
    corrupted = binding.result.model_copy(update={"method_ref": None})
    with pytest.raises(CohortImportFilesystemError, match="invalid"):
        path = _binding_path(tmp_path / "cohort-records", binding)
        payload = binding.model_dump(mode="json")
        payload["result"] = corrupted.model_dump(mode="json")
        path.write_bytes(canonical_json_bytes(payload))
        values[0].bindings_for_manifest((values[2],))


def test_result_catalog_rejects_reader_and_trust_authority_substitution(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)

    class FakeResultCatalog(ResultCatalog):
        pass

    fake = object.__new__(FakeResultCatalog)
    with pytest.raises(TypeError, match="exact result"):
        CohortRecordCatalog(
            tmp_path / "fake-result-index",
            result_catalog=fake,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
        )
    values[1].prepare_bundle_import = lambda **_: None  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="shadowed"):
        CohortRecordCatalog(
            tmp_path / "shadow-index",
            result_catalog=values[1],
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
        )


def test_live_trust_and_reader_method_shadowing_fail_before_verification(
    tmp_path: Path, live
) -> None:
    trust_shadow = _setup(tmp_path / "trust-shadow", live)
    trust_shadow[6].resolve = lambda _: object()  # type: ignore[method-assign]
    with pytest.raises(CatalogError, match="trust store"):
        _import(trust_shadow)
    assert trust_shadow[1].query(CatalogQuery()).empty

    reader_shadow = _setup(tmp_path / "reader-shadow", live)
    object.__setattr__(
        reader_shadow[1].reader_registry,
        "select",
        lambda _: DEFAULT_RESULT_BUNDLE_READER_REGISTRY.readers[0],
    )
    with pytest.raises(CatalogUnsupportedSchema, match="reader registry"):
        _import(reader_shadow)
    assert reader_shadow[1].query(CatalogQuery()).empty

    content_shadow = _setup(tmp_path / "reader-content-shadow", live)
    object.__setattr__(
        content_shadow[1].reader_registry,
        "readers",
        (
            ResultBundleReader(
                reader_id="reader_result_bundle_v3",
                minimum_version=3,
                maximum_version=3,
                measurement_schema_versions=("traceback.fragment-measurement.v1",),
            ),
        ),
    )
    with pytest.raises(CatalogUnsupportedSchema, match="reader registry changed"):
        _import(content_shadow)
    assert content_shadow[1].query(CatalogQuery()).empty

    cohort_shadow = _setup(tmp_path / "cohort-reader-shadow", live)
    object.__setattr__(cohort_shadow[0]._reader_registry, "readers", ())
    with pytest.raises(CohortImportError, match="reader registry"):
        _import(cohort_shadow)
    assert cohort_shadow[1].query(CatalogQuery()).empty


def test_binding_bytes_are_canonical_and_permissions_private(
    tmp_path: Path, live
) -> None:
    values = _setup(tmp_path, live)
    binding = _import(values)
    path = _binding_path(tmp_path / "cohort-records", binding)
    assert path.read_bytes() == canonical_json_bytes(binding)
    assert stat_mode(path) == 0o600
    assert stat_mode(path.parent) == 0o700


@pytest.mark.parametrize(
    "point",
    (
        "after_preflight",
        "after_result_stage",
        "before_binding_publish",
        "after_binding_publish",
        "before_visibility",
        "after_visibility_staged",
        "after_visibility_commit",
    ),
)
def test_authority_change_at_every_publication_window_compensates(
    tmp_path: Path, live, point: str
) -> None:
    values = _setup(tmp_path / point, live)
    fired = False

    def fault(observed: str) -> None:
        nonlocal fired
        if observed == point and not fired:
            fired = True
            values[6].revoke(values[5].key_id)

    values[0]._fault_injector = fault
    with pytest.raises(Exception, match="authority changed|revoked"):
        _import(values)
    assert fired
    assert values[1].query(CatalogQuery()).empty
    inventory = tuple((tmp_path / point / "cohort-records").iterdir())
    assert inventory == ()
    assert tuple((tmp_path / point / "results/objects").iterdir())


@pytest.mark.parametrize("point", ("before_binding_publish", "after_binding_publish"))
def test_root_replacement_during_publication_compensates(
    tmp_path: Path, live, point: str
) -> None:
    values = _setup(tmp_path / point, live)
    root = tmp_path / point / "cohort-records"
    displaced = tmp_path / point / "displaced"
    fired = False

    def fault(observed: str) -> None:
        nonlocal fired
        if observed == point and not fired:
            fired = True
            root.rename(displaced)
            root.mkdir(mode=0o700)

    values[0]._fault_injector = fault
    with pytest.raises(CohortImportFilesystemError, match="root changed"):
        _import(values)
    assert fired
    assert values[1].query(CatalogQuery()).empty
    assert tuple(displaced.iterdir()) == ()
    assert tuple(root.iterdir()) == ()


def test_result_catalog_object_substitution_is_rejected_before_import(
    tmp_path: Path, live
) -> None:
    victim = _setup(tmp_path / "victim", live)
    attacker = _setup(tmp_path / "attacker", live)
    object.__setattr__(victim[0], "_result_catalog", attacker[1])
    with pytest.raises(CohortImportError, match="authority changed"):
        _import(victim)
    assert victim[1].query(CatalogQuery()).empty
    assert attacker[1].query(CatalogQuery()).empty


def test_linkage_advance_before_visibility_compensates(tmp_path: Path, live) -> None:
    values = _setup(tmp_path, live)
    revision = _known_run_revision(
        linkage_id="linkage_" + "e" * 32,
        subject="subject_" + "e" * 32,
        collection="collection_" + "e" * 32,
        specimen="specimen_" + "e" * 32,
        analysis="analysis_" + "e" * 32,
        measurement="measurement_" + "e" * 32,
        source="projection_" + "e" * 32,
        run_digit="e",
    )
    authorized, _ = _consume(revision, (_create_approval(revision, "e"),))
    fired = False

    def fault(point: str) -> None:
        nonlocal fired
        if point == "before_visibility" and not fired:
            fired = True
            live[0].commit_authorized_revision(authorized)

    values[0]._fault_injector = fault
    with pytest.raises(CohortImportError, match="changed during import"):
        _import(values)
    assert fired
    assert values[1].query(CatalogQuery()).empty
    assert tuple((tmp_path / "cohort-records").iterdir()) == ()


@pytest.mark.parametrize(
    ("point", "visible"),
    (("after_result_stage", False), ("after_visibility_commit", True)),
)
def test_crash_recovery_reconciles_journal_and_catalog_publication(
    tmp_path: Path, live, point: str, visible: bool
) -> None:
    values = _setup(tmp_path, live)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - abrupt crash path cannot report assertions
        values[0]._fault_injector = lambda observed: (
            os._exit(71) if observed == point else None
        )
        _import(values)
        os._exit(72)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 71
    values[0].close()
    values[1].close()
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": tmp_path / "imports"},
        trust_store=values[6],
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    cohorts = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        page = results.query(CatalogQuery())
        bindings = cohorts.bindings_for_manifest((values[2],))
        assert bool(page.results) is visible
        assert bool(bindings) is visible
        assert not tuple((tmp_path / "cohort-records").glob(".pending.*"))
    finally:
        cohorts.close()
        results.close()


def test_crash_recovery_removes_partial_pre_stage_journal(tmp_path: Path, live) -> None:
    values = _setup(tmp_path, live)
    values[0].close()
    partial = (
        tmp_path / "cohort-records" / (".pending.publication_" + "f" * 64 + ".json")
    )
    partial.write_bytes(b'{"partial":')
    partial.chmod(0o600)
    recovered = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=values[1],
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        assert not partial.exists()
        assert values[1].query(CatalogQuery()).empty
    finally:
        recovered.close()


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
