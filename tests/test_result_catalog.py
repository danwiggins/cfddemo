"""Adversarial tests for the immutable local result catalog."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import evidence_inspector.result_catalog as catalog_module
from evidence_inspector.method_registry import (
    DisplayRole,
    QualificationState,
    RegistryIdentityError,
    authority_head_sha256,
    registry_sha256,
    resolve_current_capability,
)
from evidence_inspector.result_catalog import (
    CatalogAliases,
    CatalogConflict,
    CatalogEmptyReason,
    CatalogError,
    CatalogFilesystemError,
    CatalogOrder,
    CatalogQualificationState,
    CatalogQuery,
    CatalogResultRef,
    CatalogUnsupportedSchema,
    ExecutionState,
    InformationState,
    ResultCatalog,
    TrustState,
    _copy_exact_bundle,
)
from tests.test_bundles import _bundle, _downgrade_to_v1
from tests.test_method_registry import (
    T0,
    T1,
    T2,
    _definition,
    _head,
    _qualification,
    _registry,
    _revoke_qualification,
    _role,
)
from traceback_runner.bundles import verify_bundle

ALIASES = CatalogAliases(
    display_alias="dsp_aaaaaaaa",
    run_alias="rnx_bbbbbbbb",
    timepoint_alias="tpt_cccccccc",
)


def _authority():
    definition = _definition(method_id="mth_fragment_aligned_reference_span")
    qualification = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_primary",
        state=QualificationState.QUALIFIED,
        effective_at=T0,
    )
    role = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T0,
    )
    registry = _registry(
        definition,
        qualifications=(qualification,),
        roles=(role,),
    )
    head = _head(registry)
    head_sha256 = authority_head_sha256(head)
    capability = resolve_current_capability(
        registry,
        head,
        head_sha256,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    return registry, definition, qualification, role, head, head_sha256, capability


def _bundle_method(capability) -> dict[str, str]:
    return {
        "method_id": capability.method_ref.method_id,
        "version": capability.method_ref.version,
        "method_definition_sha256": capability.method_definition_sha256,
    }


def _catalog(tmp_path: Path, *, fault=None):
    import_root = tmp_path / "imports"
    import_root.mkdir(parents=True)
    *_, capability = _authority()
    bundle_path, _, trust_store = _bundle(
        import_root / "incoming", method=_bundle_method(capability)
    )
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=trust_store,
        fault_injector=fault,
    )
    return catalog, bundle_path, import_root


def _import(catalog: ResultCatalog, **updates):
    registry, _, _, _, head, head_sha256, capability = _authority()
    values = {
        "root_id": "root_primary",
        "relative_path": "incoming/record",
        "registry": registry,
        "authority_head": head,
        "expected_authority_head_sha256": head_sha256,
        "capability": capability,
        "aliases": ALIASES,
    }
    values.update(updates)
    return catalog.import_bundle(**values)


def _prepare(catalog: ResultCatalog):
    registry, _, _, _, head, head_sha256, capability = _authority()
    return catalog.prepare_bundle_import(
        root_id="root_primary",
        relative_path="incoming/record",
        registry=registry,
        authority_head=head,
        expected_authority_head_sha256=head_sha256,
        capability=capability,
        aliases=ALIASES,
    )


def test_prepared_publication_is_hidden_until_atomic_adoption(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _catalog(tmp_path)
    prepared = _prepare(catalog)
    catalog.stage_prepared_import(prepared)
    assert catalog.query(CatalogQuery()).empty
    with pytest.raises(CatalogConflict, match="not indexed"):
        catalog.verify_reference(prepared.reference)
    windows: list[str] = []
    catalog.adopt_prepared_import(prepared, revalidate=windows.append)
    assert windows == ["before_visibility", "after_visibility_staged"]
    assert catalog.query(CatalogQuery()).results == (prepared.reference,)
    catalog.finish_prepared_import(prepared)


def test_prepared_publication_compensation_retains_shared_object(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _catalog(tmp_path)
    prepared = _prepare(catalog)
    object_path = tmp_path / "catalog/objects" / prepared.reference.bundle_sha256
    catalog.stage_prepared_import(prepared)
    catalog.compensate_prepared_import(prepared)
    assert catalog.query(CatalogQuery()).empty
    assert object_path.is_dir()


def test_import_is_idempotent_private_and_selectable(tmp_path: Path) -> None:
    catalog, _, import_root = _catalog(tmp_path)
    first = _import(catalog)
    assert _import(catalog) == first

    query = CatalogQuery(
        method_refs=(first.method_ref, first.method_ref),
        execution_states=(ExecutionState.COMPLETE,),
        information_states=(InformationState.AVAILABLE,),
        trust_states=(TrustState.DEVELOPMENT_SIGNATURE_VERIFIED,),
        qualification_states=(CatalogQualificationState.QUALIFIED,),
        display_alias=ALIASES.display_alias,
        run_alias=ALIASES.run_alias,
        timepoint_alias=ALIASES.timepoint_alias,
    )
    page = catalog.query(query)
    assert page.results == (first,)
    assert not page.empty
    assert page.empty_reason is None
    assert len(query.method_refs) == 1

    public = page.model_dump_json()
    for secret in (
        str(import_root),
        ALIASES.display_alias,
        ALIASES.run_alias,
        ALIASES.timepoint_alias,
        "synthetic.run.v1",
    ):
        assert secret not in first.model_dump_json()
        assert secret not in public


def test_empty_states_pagination_and_order_are_explicit(tmp_path: Path) -> None:
    catalog, _, _ = _catalog(tmp_path)
    empty = catalog.query(CatalogQuery())
    assert empty.empty
    assert empty.empty_reason == CatalogEmptyReason.NO_IMPORTED_RESULTS

    result = _import(catalog)
    no_match = catalog.query(
        CatalogQuery(cursor=result.result_id, order=CatalogOrder.RESULT_ID_ASC)
    )
    assert no_match.empty
    assert no_match.empty_reason == CatalogEmptyReason.NO_MATCHES

    with pytest.raises(ValueError, match="value bound"):
        CatalogQuery(method_refs=(result.method_ref,) * 33)


@pytest.mark.parametrize(
    "relative",
    ("../incoming/record", "/incoming/record", "a/b/c/d/e", "incoming//record"),
)
def test_import_rejects_traversal_and_unbounded_paths(
    tmp_path: Path, relative: str
) -> None:
    catalog, _, _ = _catalog(tmp_path)
    with pytest.raises(CatalogFilesystemError, match="invalid"):
        _import(catalog, relative_path=relative)


def test_import_rejects_symlinks_extra_special_and_oversize(tmp_path: Path) -> None:
    catalog, bundle, import_root = _catalog(tmp_path)
    os.symlink(bundle, import_root / "linked")
    with pytest.raises(CatalogFilesystemError) as error:
        _import(catalog, relative_path="linked")
    assert str(import_root) not in str(error.value)

    extra = bundle / "extra"
    extra.write_text("not allowed", encoding="utf-8")
    with pytest.raises(CatalogFilesystemError, match="inventory"):
        _import(catalog)
    extra.unlink()

    fifo = bundle / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(CatalogFilesystemError, match="inventory"):
            _import(catalog)
    finally:
        fifo.unlink()

    report = bundle / "report.html"
    report.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(CatalogFilesystemError, match="byte bound"):
        _import(catalog)


def test_import_rejects_source_mutation_and_root_swap(tmp_path: Path) -> None:
    mutation_root = tmp_path / "mutation"
    mutation_root.mkdir()
    bundle, _, trust = _bundle(mutation_root / "incoming")

    def mutate(point: str) -> None:
        if point == "after_bundle_snapshot":
            (bundle / "report.html").write_bytes(b"changed")

    catalog = ResultCatalog(
        tmp_path / "mutation-catalog",
        import_roots={"root_primary": mutation_root},
        trust_store=trust,
        fault_injector=mutate,
    )
    with pytest.raises(CatalogFilesystemError, match="changed"):
        _import(catalog)

    path_root = tmp_path / "path-swap"
    path_root.mkdir()
    path_bundle, _, path_trust = _bundle(path_root / "incoming")

    def swap_path(point: str) -> None:
        if point == "after_bundle_snapshot":
            path_bundle.rename(path_bundle.with_name("old-record"))
            path_bundle.mkdir()

    path_swapped = ResultCatalog(
        tmp_path / "path-swap-catalog",
        import_roots={"root_primary": path_root},
        trust_store=path_trust,
        fault_injector=swap_path,
    )
    with pytest.raises(CatalogFilesystemError, match="path changed"):
        _import(path_swapped)

    swap_root = tmp_path / "swap"
    swap_root.mkdir()
    _, _, swap_trust = _bundle(swap_root / "incoming")

    def swap(point: str) -> None:
        if point == "after_bundle_snapshot":
            swap_root.rename(tmp_path / "old-swap")
            swap_root.mkdir()

    swapped = ResultCatalog(
        tmp_path / "swap-catalog",
        import_roots={"root_primary": swap_root},
        trust_store=swap_trust,
        fault_injector=swap,
    )
    with pytest.raises(CatalogFilesystemError, match="root changed"):
        _import(swapped)


def test_conflict_and_crash_leave_no_partial_catalog_state(tmp_path: Path) -> None:
    catalog, _, import_root = _catalog(tmp_path)
    first = _import(catalog)
    conflicting_aliases = ALIASES.model_copy(update={"run_alias": "rnx_dddddddd"})
    with pytest.raises(CatalogConflict, match="identity conflict") as error:
        _import(
            catalog,
            aliases=conflicting_aliases,
        )
    assert conflicting_aliases.run_alias not in str(error.value)
    assert catalog.query(CatalogQuery()).results == (first,)

    _, mismatched_key, _ = _bundle(import_root / "mismatched")
    catalog.trust_store.add_signing_key(mismatched_key)
    with pytest.raises(CatalogConflict, match="method identity"):
        _import(
            catalog,
            relative_path="mismatched/record",
            aliases=CatalogAliases(
                display_alias="dsp_11111111",
                run_alias="rnx_22222222",
                timepoint_alias="tpt_33333333",
            ),
        )

    *_, capability = _authority()
    _, second_key, _ = _bundle(
        import_root / "second", method=_bundle_method(capability)
    )
    catalog.trust_store.add_signing_key(second_key)
    with pytest.raises(CatalogConflict, match="identity conflict"):
        _import(
            catalog,
            relative_path="second/record",
            aliases=CatalogAliases(
                display_alias="dsp_dddddddd",
                run_alias="rnx_eeeeeeee",
                timepoint_alias="tpt_ffffffff",
            ),
        )
    assert catalog.query(CatalogQuery()).results == (first,)

    def fail(point: str) -> None:
        if point == "before_catalog_commit":
            raise OSError("synthetic crash")

    crash_root = tmp_path / "crash"
    crash_root.mkdir()
    *_, capability = _authority()
    _, _, trust = _bundle(crash_root / "incoming", method=_bundle_method(capability))
    crashing = ResultCatalog(
        tmp_path / "crash-catalog",
        import_roots={"root_primary": crash_root},
        trust_store=trust,
        fault_injector=fail,
    )
    with pytest.raises(OSError, match="synthetic crash"):
        _import(crashing)
    reopened = ResultCatalog(
        tmp_path / "crash-catalog",
        import_roots={"root_primary": crash_root},
        trust_store=trust,
    )
    assert reopened.query(CatalogQuery()).empty
    assert _import(reopened)


def test_failed_publisher_never_unlinks_an_object_adopted_concurrently(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    *_, capability = _authority()
    _, _, trust = _bundle(import_root / "incoming", method=_bundle_method(capability))
    published = threading.Event()
    adopted = threading.Event()

    def pause_then_fail(point: str) -> None:
        if point == "after_object_publish":
            published.set()
            assert adopted.wait(timeout=10)
            raise OSError("synthetic losing publisher")

    first = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=trust,
        fault_injector=pause_then_fail,
    )
    second = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=trust,
    )
    errors: list[BaseException] = []

    def losing_import() -> None:
        try:
            _import(first)
        except BaseException as error:  # noqa: BLE001 - collect thread outcome
            errors.append(error)

    worker = threading.Thread(target=losing_import)
    worker.start()
    assert published.wait(timeout=10)
    committed = _import(second)
    adopted.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    object_path = second._bound_objects / committed.bundle_sha256
    assert object_path.is_dir()
    assert (
        verify_bundle(object_path, trust).manifest.record_id
        == committed.bundle_record_id
    )
    assert second.query(CatalogQuery()).results == (committed,)


def test_destination_and_database_replacement_fail_closed(tmp_path: Path) -> None:
    catalog, _, _ = _catalog(tmp_path)
    original_root = catalog.root
    displaced_root = tmp_path / "displaced-catalog"

    def replace_destination(point: str) -> None:
        if point == "after_bundle_snapshot":
            original_root.rename(displaced_root)
            original_root.mkdir()

    catalog.fault_injector = replace_destination
    with pytest.raises(CatalogFilesystemError, match="catalog root changed"):
        _import(catalog)
    assert not list((displaced_root / "objects").glob("[0-9a-f]" * 64))
    catalog.close()

    database_catalog, _, _ = _catalog(tmp_path / "database-swap")
    database_catalog.database.rename(database_catalog.root / "old-catalog.sqlite3")
    database_catalog.database.touch()
    with pytest.raises(CatalogFilesystemError, match="database changed"):
        database_catalog.query(CatalogQuery())


def test_transient_connect_window_root_swap_cannot_open_attacker_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _, import_root = _catalog(tmp_path)
    original_root = original.root
    original.close()
    attacker_root = tmp_path / "attacker-catalog"
    shutil.copytree(original_root, attacker_root)
    attacker = ResultCatalog(
        attacker_root,
        import_roots={"root_primary": import_root},
        trust_store=original.trust_store,
    )
    attacker_ref = _synthetic_ref(999)
    with attacker._connect() as connection:
        connection.execute(
            "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                attacker_ref.result_id,
                attacker_ref.bundle_sha256,
                attacker_ref.bundle_record_id,
                attacker_ref.method_ref.method_id,
                attacker_ref.method_ref.version,
                attacker_ref.execution_state.value,
                attacker_ref.information_state.value,
                attacker_ref.trust_state.value,
                attacker_ref.qualification_state.value,
                attacker_ref.model_dump_json().encode(),
            ),
        )
    attacker.close()

    real_connect = catalog_module.sqlite3.connect
    displaced_root = tmp_path / "displaced-original"

    def swap_during_connect(*args, **kwargs):
        original_root.rename(displaced_root)
        attacker_root.rename(original_root)
        try:
            return real_connect(*args, **kwargs)
        finally:
            original_root.rename(attacker_root)
            displaced_root.rename(original_root)

    monkeypatch.setattr(catalog_module.sqlite3, "connect", swap_during_connect)
    with pytest.raises(CatalogFilesystemError, match="identity is unproven"):
        ResultCatalog(
            original_root,
            import_roots={"root_primary": import_root},
            trust_store=original.trust_store,
        )

    monkeypatch.setattr(catalog_module.sqlite3, "connect", real_connect)
    reopened = ResultCatalog(
        original_root,
        import_roots={"root_primary": import_root},
        trust_store=original.trust_store,
    )
    assert reopened.query(CatalogQuery()).empty


def test_sqlite_descriptor_proof_accepts_reused_fd_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _, import_root = _catalog(tmp_path)
    original_root = original.root
    trust_store = original.trust_store
    original.close()
    real_snapshot = catalog_module._open_descriptor_identities
    calls = 0
    reused_descriptor: int | None = None
    prior_identity: tuple[int, int, int] | None = None

    def force_reuse() -> dict[int, tuple[int, int, int]]:
        nonlocal calls, reused_descriptor, prior_identity
        calls += 1
        if calls == 1:
            reused_descriptor = os.open(
                tmp_path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            snapshot = real_snapshot()
            prior_identity = snapshot[reused_descriptor]
            os.close(reused_descriptor)
            return snapshot
        snapshot = real_snapshot()
        assert reused_descriptor is not None
        assert prior_identity is not None
        assert snapshot[reused_descriptor] != prior_identity
        return snapshot

    monkeypatch.setattr(catalog_module, "_open_descriptor_identities", force_reuse)
    reopened = ResultCatalog(
        original_root,
        import_roots={"root_primary": import_root},
        trust_store=trust_store,
    )

    assert calls >= 2
    assert reopened.query(CatalogQuery()).empty
    reopened.close()


def test_simultaneous_fresh_catalog_constructors_are_atomic_and_usable(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    *_, capability = _authority()
    _, _, trust = _bundle(import_root / "incoming", method=_bundle_method(capability))
    catalog_root = tmp_path / "catalog"
    barrier = threading.Barrier(2)
    catalogs: list[ResultCatalog] = []
    errors: list[BaseException] = []

    def construct() -> None:
        try:
            barrier.wait(timeout=10)
            catalogs.append(
                ResultCatalog(
                    catalog_root,
                    import_roots={"root_primary": import_root},
                    trust_store=trust,
                )
            )
        except BaseException as error:  # noqa: BLE001 - collect thread outcome
            errors.append(error)

    workers = [threading.Thread(target=construct) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(catalogs) == 2
    assert all(catalog.query(CatalogQuery()).empty for catalog in catalogs)
    imported = _import(catalogs[0])
    assert catalogs[1].query(CatalogQuery()).results == (imported,)


def test_published_object_digest_is_the_accepted_digest(tmp_path: Path) -> None:
    catalog, _, _ = _catalog(tmp_path)
    reference = _import(catalog)
    object_path = catalog._bound_objects / reference.bundle_sha256
    descriptor = os.open(
        object_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        observed, _, _ = _copy_exact_bundle(descriptor, None)
    finally:
        os.close(descriptor)
    assert observed == reference.bundle_sha256
    assert verify_bundle(object_path, catalog.trust_store).manifest.record_id == (
        reference.bundle_record_id
    )


def _write_preexisting_catalog_schema(
    database: Path, *, dropped_constraints: bool, wrong_indexes: bool
) -> None:
    if dropped_constraints:
        tables = """
            CREATE TABLE metadata(key TEXT, value TEXT);
            CREATE TABLE results(
                result_id TEXT,
                bundle_sha256 TEXT NOT NULL,
                bundle_record_id TEXT NOT NULL,
                method_id TEXT NOT NULL,
                method_version TEXT NOT NULL,
                execution_state TEXT NOT NULL,
                information_state TEXT NOT NULL,
                trust_state TEXT NOT NULL,
                qualification_state TEXT NOT NULL,
                ref_json BLOB NOT NULL
            );
            CREATE TABLE opaque_aliases(
                result_id TEXT,
                display_alias TEXT NOT NULL,
                run_alias TEXT NOT NULL,
                timepoint_alias TEXT NOT NULL
            );
        """
    else:
        tables = """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE results(
                result_id TEXT PRIMARY KEY,
                bundle_sha256 TEXT NOT NULL UNIQUE,
                bundle_record_id TEXT NOT NULL UNIQUE,
                method_id TEXT NOT NULL,
                method_version TEXT NOT NULL,
                execution_state TEXT NOT NULL,
                information_state TEXT NOT NULL,
                trust_state TEXT NOT NULL,
                qualification_state TEXT NOT NULL,
                ref_json BLOB NOT NULL
            );
            CREATE TABLE opaque_aliases(
                result_id TEXT PRIMARY KEY REFERENCES results(result_id),
                display_alias TEXT NOT NULL UNIQUE,
                run_alias TEXT NOT NULL,
                timepoint_alias TEXT NOT NULL
            );
        """
    results_method = (
        "CREATE UNIQUE INDEX results_method "
        "ON results(method_version, method_id, result_id);"
        if wrong_indexes
        else "CREATE INDEX results_method "
        "ON results(method_id, method_version, result_id);"
    )
    indexes = f"""
        {results_method}
        CREATE INDEX results_states ON results(
            execution_state,
            information_state,
            trust_state,
            qualification_state,
            result_id
        );
        CREATE INDEX aliases_run ON opaque_aliases(run_alias, result_id);
        CREATE INDEX aliases_timepoint ON opaque_aliases(timepoint_alias, result_id);
        INSERT INTO metadata VALUES('schema_version', '1');
    """
    with sqlite3.connect(database) as connection:
        connection.executescript(tables + indexes)


@pytest.mark.parametrize(
    ("dropped_constraints", "wrong_indexes"),
    ((True, False), (False, True)),
)
def test_same_inventory_malformed_schema_fails_closed(
    tmp_path: Path, dropped_constraints: bool, wrong_indexes: bool
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    _, _, trust = _bundle(import_root / "incoming")
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    _write_preexisting_catalog_schema(
        catalog_root / "catalog.sqlite3",
        dropped_constraints=dropped_constraints,
        wrong_indexes=wrong_indexes,
    )

    with pytest.raises(CatalogUnsupportedSchema, match="unsupported"):
        ResultCatalog(
            catalog_root,
            import_roots={"root_primary": import_root},
            trust_store=trust,
        )


def test_unsupported_schema_and_stale_revoked_capability_fail_closed(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _catalog(tmp_path)
    with catalog._connect() as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(CatalogUnsupportedSchema, match="unsupported"):
        ResultCatalog(
            catalog.root,
            import_roots=catalog.import_roots,
            trust_store=catalog.trust_store,
        )

    registry, definition, qualification, role, _, _, stale = _authority()
    revoked = _registry(
        definition,
        qualifications=(qualification,),
        roles=(role,),
        revocations=(
            _revoke_qualification(
                qualification.record_ref,
                revocation_ref="revoke_fragment_primary",
                effective_at=T2,
            ),
        ),
        version=2,
        published_at=T2,
        previous=registry_sha256(registry),
    )
    revoked_head = _head(revoked, T2)
    with pytest.raises(RegistryIdentityError):
        _import(
            catalog,
            registry=revoked,
            authority_head=revoked_head,
            expected_authority_head_sha256=authority_head_sha256(revoked_head),
            capability=stale,
        )
    revoked_capability = resolve_current_capability(
        revoked,
        revoked_head,
        authority_head_sha256(revoked_head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T2,
    )
    with pytest.raises(CatalogError, match="revoked method authority"):
        _import(
            catalog,
            registry=revoked,
            authority_head=revoked_head,
            expected_authority_head_sha256=authority_head_sha256(revoked_head),
            capability=revoked_capability,
        )


def test_catalog_rejects_verified_v1_bundle_without_method_binding(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    *_, capability = _authority()
    bundle, key, trust = _bundle(
        import_root / "incoming", method=_bundle_method(capability)
    )
    _downgrade_to_v1(bundle, key)
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        trust_store=trust,
    )

    with pytest.raises(CatalogUnsupportedSchema, match="bundle schema is unsupported"):
        _import(catalog)


def _synthetic_ref(index: int) -> CatalogResultRef:
    bundle_sha = f"{index + 1:064x}"
    method_sha = "c" * 64
    identity = hashlib.sha256(
        b'{"bundle_sha256":"'
        + bundle_sha.encode()
        + b'","method_definition_sha256":"'
        + method_sha.encode()
        + b'"}'
    ).hexdigest()
    return CatalogResultRef(
        result_id=f"result_{identity[:40]}",
        bundle_sha256=bundle_sha,
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


def test_ten_thousand_result_query_p95_is_below_250ms(tmp_path: Path) -> None:
    catalog, _, _ = _catalog(tmp_path)
    refs = [_synthetic_ref(index) for index in range(10_000)]
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
        for ref in refs
    ]
    with catalog._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        connection.commit()

    query = CatalogQuery(
        method_refs=(refs[0].method_ref,),
        qualification_states=(CatalogQualificationState.QUALIFIED,),
        limit=100,
    )
    durations = []
    for _ in range(25):
        started = time.perf_counter()
        page = catalog.query(query)
        durations.append(time.perf_counter() - started)
        assert len(page.results) == 100
        assert page.next_cursor is not None
    p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
    assert p95 <= 0.250
