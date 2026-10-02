"""E04 result catalog wired to the protected result-trust registry."""

from __future__ import annotations

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evidence_inspector.fault_controller import (
    DeterministicFaultController,
    FaultAction,
)
from evidence_inspector.result_catalog import (
    CATALOG_AUTHORITY_SCHEMA_V1,
    CATALOG_AUTHORITY_SCHEMA_V2,
    CatalogAliases,
    CatalogConflict,
    CatalogError,
    CatalogQuery,
    ResultCatalog,
    bind_catalog_live_reader,
    bound_catalog_authority,
    catalog_authority_sha256,
    registry_trust_snapshot_sha256,
)
from evidence_inspector.result_trust_registry import (
    ResultTrustRegistry,
    ResultTrustRegistryUnsafe,
)
from tests.test_bundles import _bundle
from tests.test_result_catalog import (
    _authority,
    _bundle_method,
    _import,
    _prepare,
    _verification_context,
)
from traceback_runner.signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    PublicTrustedKey,
    RevokedKeyError,
    TrustStore,
    UnknownKeyError,
    generate_development_keypair,
)


def public_result_key(key: DevelopmentSigningKey) -> PublicTrustedKey:
    return PublicTrustedKey(
        key_id=key.key_id,
        purpose=key.purpose,
        public_key_base64=base64.b64encode(key.public_key_bytes()).decode("ascii"),
    )


class RegistryTrust:
    """Test adapter: the TrustStore mutation surface, applied to a registry."""

    def __init__(self, registry: ResultTrustRegistry) -> None:
        self.registry = registry

    def revoke(self, key_id: str) -> None:
        self.registry.revoke_key(key_id)

    def add_signing_key(self, key: DevelopmentSigningKey) -> None:
        self.registry.add_key(public_result_key(key))


def _registry_catalog(tmp_path: Path, fault_controller=None):
    import_root = tmp_path / "imports"
    import_root.mkdir(parents=True)
    *_, capability = _authority()
    bundle_path, key, _ = _bundle(
        import_root / "incoming", method=_bundle_method(capability)
    )
    trust = ResultTrustRegistry(tmp_path / "trust")
    trust.add_key(public_result_key(key))
    catalog = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        result_trust_registry=trust,
        **({"fault_controller": fault_controller} if fault_controller else {}),
    )
    return catalog, trust, key, import_root


def test_catalog_requires_exactly_one_trust_authority(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    trust = ResultTrustRegistry(tmp_path / "trust")
    with pytest.raises(CatalogError, match="exactly one"):
        ResultCatalog(tmp_path / "a", import_roots={"root_primary": import_root})
    with pytest.raises(CatalogError, match="exactly one"):
        ResultCatalog(
            tmp_path / "b",
            import_roots={"root_primary": import_root},
            trust_store=TrustStore(),
            result_trust_registry=trust,
        )
    with pytest.raises(CatalogError, match="trust registry is unsupported"):
        ResultCatalog(
            tmp_path / "c",
            import_roots={"root_primary": import_root},
            result_trust_registry=object(),  # type: ignore[arg-type]
        )


def test_revocation_reaches_the_next_verification_without_reopening(
    tmp_path: Path,
) -> None:
    catalog, trust, key, _ = _registry_catalog(tmp_path)
    reference = _import(catalog)
    reader = bind_catalog_live_reader(catalog)
    assert catalog.verify_reference(reference)[0].manifest.record_id == (
        reference.bundle_record_id
    )
    assert reader.get_verified(reference.result_id, _verification_context()) == (
        reference
    )
    before = catalog.authority_snapshot()
    assert before.schema_version == CATALOG_AUTHORITY_SCHEMA_V2
    assert before.trust_snapshot_sha256 == registry_trust_snapshot_sha256(
        trust.current_trust()
    )

    trust.revoke_key(key.key_id)

    # Same catalog instance, same reader bound before the revocation.
    with pytest.raises(RevokedKeyError):
        catalog.verify_reference(reference)
    with pytest.raises(RevokedKeyError):
        reader.get_verified(reference.result_id, _verification_context())
    with pytest.raises(RevokedKeyError):
        catalog.get_verified(reference.result_id, _verification_context())
    with pytest.raises(RevokedKeyError):
        _import(catalog)
    after = catalog.authority_snapshot()
    assert after.trust_snapshot_sha256 != before.trust_snapshot_sha256
    assert catalog_authority_sha256(after) != catalog_authority_sha256(before)
    # The index itself is unchanged; only verification is withheld.
    assert catalog.query(CatalogQuery()).results == (reference,)


def test_old_trust_cannot_come_back(tmp_path: Path) -> None:
    catalog, trust, key, import_root = _registry_catalog(tmp_path)
    reference = _import(catalog)
    old = trust.current_trust()
    old_backup = trust.backup_bytes()
    trust.revoke_key(key.key_id)
    # Re-adding the revoked key is refused by the store.
    with pytest.raises(Exception, match="cannot be re-added|revoked"):
        trust.add_key(public_result_key(key))
    # A restore of the pre-revocation backup is refused by the process head
    # fence, so no older registry copy can be handed to a catalog.
    with pytest.raises(Exception, match="rollback"):
        ResultTrustRegistry.restore(
            tmp_path / "restored",
            old_backup,
            expected_registry_id=old.registry_id,
            expected_registry_epoch_sha256=old.registry_epoch_sha256,
            expected_state_head_sha256=old.state_head_sha256,
        )
    # Reopening the same root needs the current head; the old one is refused.
    with pytest.raises(Exception, match="rollback|head"):
        ResultTrustRegistry(
            tmp_path / "trust",
            expected_registry_id=old.registry_id,
            expected_registry_epoch_sha256=old.registry_epoch_sha256,
            expected_state_head_sha256=old.state_head_sha256,
        )
    # A second catalog over the same root bound to the current registry still
    # sees the revocation.
    current = trust.current_trust()
    reopened_trust = ResultTrustRegistry(
        tmp_path / "trust",
        expected_registry_id=current.registry_id,
        expected_registry_epoch_sha256=current.registry_epoch_sha256,
        expected_state_head_sha256=current.state_head_sha256,
    )
    catalog.close()
    reopened = ResultCatalog(
        tmp_path / "catalog",
        import_roots={"root_primary": import_root},
        result_trust_registry=reopened_trust,
    )
    with pytest.raises(RevokedKeyError):
        reopened.verify_reference(reference)
    reopened.close()
    reopened_trust.close()


def test_catalog_rejects_a_trust_head_older_than_it_has_seen(tmp_path: Path) -> None:
    catalog, trust, _, _ = _registry_catalog(tmp_path)
    reference = _import(catalog)
    seen = trust.current_trust()
    # Simulate an older head reaching this catalog: its high-water mark is
    # ahead of the registry it reads.
    catalog._trust_high_water = (seen.state_version + 1, seen.state_head_sha256)
    with pytest.raises(CatalogError, match="rolled back"):
        catalog.verify_reference(reference)
    catalog._trust_high_water = (seen.state_version, "0" * 64)
    with pytest.raises(CatalogError, match="rolled back"):
        catalog.authority_snapshot()
    catalog._trust_high_water = (seen.state_version, seen.state_head_sha256)
    catalog.verify_reference(reference)


def test_an_added_key_reaches_the_next_import_without_reopening(
    tmp_path: Path,
) -> None:
    catalog, trust, _, import_root = _registry_catalog(tmp_path)
    *_, capability = _authority()
    _, other_key, _ = _bundle(import_root / "other", method=_bundle_method(capability))
    aliases = CatalogAliases(
        display_alias="dsp_11111111",
        run_alias="rnx_22222222",
        timepoint_alias="tpt_33333333",
    )
    with pytest.raises(UnknownKeyError):
        _import(catalog, relative_path="other/record", aliases=aliases)
    trust.add_key(public_result_key(other_key))
    assert _import(catalog, relative_path="other/record", aliases=aliases)


def test_revocation_between_prepare_and_adopt_is_never_published(
    tmp_path: Path,
) -> None:
    catalog, trust, key, _ = _registry_catalog(tmp_path)
    prepared = _prepare(catalog)
    catalog.stage_prepared_import(prepared)
    trust.revoke_key(key.key_id)
    with pytest.raises(CatalogConflict, match="authority changed"):
        catalog.adopt_prepared_import(prepared)
    assert catalog.query(CatalogQuery()).empty


def test_v1_and_v2_authorities_are_distinct_and_rebuildable(tmp_path: Path) -> None:
    catalog, _, _, _ = _registry_catalog(tmp_path)
    v2 = catalog.authority_snapshot()
    v1 = v2.model_copy(update={"schema_version": CATALOG_AUTHORITY_SCHEMA_V1})
    assert catalog_authority_sha256(v1) != catalog_authority_sha256(v2)
    for value in (v1, v2):
        assert (
            bound_catalog_authority(
                storage_identity_sha256=value.storage_identity_sha256,
                trust_snapshot_sha256=value.trust_snapshot_sha256,
                reader_registry_sha256=value.reader_registry_sha256,
                expected_catalog_authority_sha256=catalog_authority_sha256(value),
            )
            == value
        )
    assert (
        bound_catalog_authority(
            storage_identity_sha256=v2.storage_identity_sha256,
            trust_snapshot_sha256=v2.trust_snapshot_sha256,
            reader_registry_sha256=v2.reader_registry_sha256,
            expected_catalog_authority_sha256="0" * 64,
        )
        is None
    )


def test_trust_store_path_keeps_v1_authority(tmp_path: Path) -> None:
    from tests.test_result_catalog import _catalog

    catalog, _, _ = _catalog(tmp_path)
    assert catalog.authority_snapshot().schema_version == CATALOG_AUTHORITY_SCHEMA_V1
    assert catalog.result_trust_registry is None
    with catalog.trust_authority_fence() as snapshot:
        assert snapshot is None


def test_trust_fields_are_read_only(tmp_path: Path) -> None:
    catalog, _, _, _ = _registry_catalog(tmp_path)
    for name, value in (
        ("trust_store", TrustStore()),
        ("result_trust_registry", None),
        ("_result_trust_identity", ("a", "b")),
        ("_trust_fence_holders", {}),
    ):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(catalog, name, value)


@pytest.mark.parametrize(
    ("point", "operation"),
    (
        ("before_live_reader_return", "read"),
        ("before_catalog_commit", "import"),
    ),
)
def test_trust_fence_is_held_through_return(
    tmp_path: Path, point: str, operation: str
) -> None:
    controller = DeterministicFaultController(point, action=FaultAction.PAUSE)
    catalog, trust, key, _ = _registry_catalog(tmp_path, controller)
    if operation == "read":
        reference = _import(catalog)
        call = lambda: catalog.get_verified(  # noqa: E731
            reference.result_id, _verification_context()
        )
    else:
        call = lambda: _import(catalog)  # noqa: E731
    started = threading.Event()

    def revoke() -> None:
        started.set()
        trust.revoke_key(key.key_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        running = executor.submit(call)
        assert controller.wait_until_reached()
        mutation = executor.submit(revoke)
        assert started.wait(timeout=10)
        assert not mutation.done()
        controller.release()
        result = running.result(timeout=10)
        mutation.result(timeout=10)
    # The returned value is consistent with the head it was verified under;
    # the revocation applies to the next verification.
    assert result.result_id
    with pytest.raises(RevokedKeyError):
        catalog.verify_reference(result)


def test_composing_fence_reuses_one_head_and_blocks_trust_events(
    tmp_path: Path,
) -> None:
    catalog, trust, key, _ = _registry_catalog(tmp_path)
    reference = _import(catalog)
    started = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with catalog.trust_authority_fence() as snapshot:
            assert snapshot is not None
            authority = catalog.authority_snapshot()
            assert authority.trust_snapshot_sha256 == registry_trust_snapshot_sha256(
                snapshot
            )
            # Nested catalog verification reuses the held head.
            catalog.verify_reference(reference)
            catalog.get_verified(reference.result_id, _verification_context())

            def revoke() -> None:
                started.set()
                trust.revoke_key(key.key_id)

            mutation = executor.submit(revoke)
            assert started.wait(timeout=10)
            assert not mutation.done()
            catalog.verify_reference(reference)
            # The trust registry lock is not reentrant: a trust mutation inside
            # the fence on the same thread raises instead of deadlocking.
            with pytest.raises(ResultTrustRegistryUnsafe, match="not reentrant"):
                trust.revoke_key(key.key_id)
        mutation.result(timeout=10)
    with pytest.raises(RevokedKeyError):
        catalog.verify_reference(reference)


def test_concurrent_catalog_reads_imports_and_trust_events_terminate(
    tmp_path: Path,
) -> None:
    catalog, trust, key, import_root = _registry_catalog(tmp_path)
    reference = _import(catalog)
    errors: list[BaseException] = []
    outcomes: list[str] = []

    def reader() -> None:
        for _ in range(15):
            try:
                catalog.get_verified(reference.result_id, _verification_context())
                catalog.verify_reference(reference)
                catalog.authority_snapshot()
                outcomes.append("verified")
            except RevokedKeyError:
                outcomes.append("revoked")
            except BaseException as exc:  # noqa: BLE001 - exact thread outcome
                errors.append(exc)

    def trust_events() -> None:
        try:
            for _ in range(5):
                trust.add_key(
                    public_result_key(generate_development_keypair(KeyPurpose.RESULT))
                )
            trust.revoke_key(key.key_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads.append(threading.Thread(target=trust_events))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(outcomes) == 45
    with pytest.raises(RevokedKeyError):
        catalog.verify_reference(reference)
