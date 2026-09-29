"""Adversarial tests for the immutable local result catalog."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evidence_inspector.method_registry import (
    RegistryIdentityError,
    authority_head_sha256,
    registry_sha256,
    resolve_current_capability,
)
from evidence_inspector.result_catalog import (
    CatalogAliases,
    CatalogConflict,
    CatalogEmptyReason,
    CatalogFilesystemError,
    CatalogOrder,
    CatalogQuery,
    CatalogQualificationState,
    CatalogResultRef,
    ExecutionState,
    InformationState,
    ResultCatalog,
    TrustState,
)
from tests.test_bundles import _bundle
from tests.test_method_registry import (
    T0,
    T1,
    T2,
    _active_provider_registry,
    _head,
    _registry,
    _revoke_qualification,
)


ALIASES = CatalogAliases(
    display_alias="dsp_aaaaaaaa",
    run_alias="rnx_bbbbbbbb",
    timepoint_alias="tpt_cccccccc",
)


def _authority():
    registry, definition, qualification, role = _active_provider_registry()
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


def _catalog(tmp_path: Path, *, fault=None):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    bundle_path, _, trust_store = _bundle(import_root / "incoming")
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
    assert str(import_root) not in public
    assert "synthetic.run.v1" not in public


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

    _, second_key, _ = _bundle(import_root / "second")
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
    _, _, trust = _bundle(crash_root / "incoming")
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


def test_unsupported_schema_and_stale_revoked_capability_fail_closed(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _catalog(tmp_path)
    with catalog._connect() as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(Exception, match="unsupported"):
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
        capability_as_of=datetime(2026, 2, 1, tzinfo=timezone.utc),
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
