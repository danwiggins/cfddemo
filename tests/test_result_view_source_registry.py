"""Protected E06 result-view-source registry: live D06/E04 re-verification."""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

import evidence_inspector.compatibility as compatibility_module
import evidence_inspector.result_view_source_registry as registry_module
from evidence_inspector.cohort_import import (
    CohortManifestRecordStatus,
    CohortRecordAvailability,
    CohortRecordCatalog,
)
from evidence_inspector.cohort_manifest import (
    MeasurementAnchor,
    MemberLineageRole,
    UnitOfAnalysis,
    build_cohort_member,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityPolicyReference,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    MeasurementCompatibilityPolicy,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage_store import (
    ProviderLinkageStore,
    ProviderLinkageStoreUnsafe,
)
from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    ResultCatalog,
)
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from evidence_inspector.result_view import ResultViewSource
from evidence_inspector.result_view_source_registry import (
    CALLER_ASSERTED_FIELDS,
    RegisteredResultViewSource,
    RegisteredResultViewSourceObject,
    ResultViewSourceRegistry,
    ResultViewSourceRegistryConflict,
    ResultViewSourceRegistryStale,
    ResultViewSourceRegistryUnsafe,
    SourceAuthorityState,
    registered_source_object_bytes,
    result_view_source_backup_from_bytes,
)
from tests.test_bundles import _measurement, _provenance
from tests.test_cohort_import import _authority
from tests.test_cohort_manifest import (
    TIME_AXIS,
    _collection_event,
    _known_run_revision,
    _manifest,
    _store,
)
from tests.test_cohort_manifest import _authority as _linkage_authority
from tests.test_cohort_summary import _ledger
from tests.test_provider_linkage import _consume, _create_approval
from tests.test_provider_linkage_store import _pins
from tests.test_result_catalog_trust_registry import RegistryTrust, public_result_key
from traceback_runner.bundles import build_result_bundle
from traceback_runner.signing import (
    KeyPurpose,
    TrustStore,
    generate_development_keypair,
)
from tests import registry_storage_checks as storage_checks


def _revision(digit: str):
    if digit == "c":
        return _known_run_revision()
    return _known_run_revision(
        linkage_id="linkage_" + digit * 32,
        subject="subject_" + digit * 32,
        collection="collection_" + digit * 32,
        specimen="specimen_" + digit * 32,
        analysis="analysis_" + digit * 32,
        measurement="measurement_" + digit * 32,
        source="projection_" + digit * 32,
        run_digit=digit,
    )


def _commit(store: ProviderLinkageStore, digit: str) -> None:
    revision = _revision(digit)
    record, _ = _consume(revision, (_create_approval(revision, digit),))
    store.commit_authorized_revision(record)


@dataclass
class Live:
    root: Path
    store: ProviderLinkageStore
    results: ResultCatalog
    cohorts: CohortRecordCatalog
    cohort_registry: CohortRegistry
    # A caller-held TrustStore, or the result-trust registry behind the same
    # mutation surface (``revoke``/``add_signing_key``).
    trust: TrustStore | RegistryTrust
    key: object
    selector_id: str
    cohort_version: int
    bindings: tuple
    records: tuple[VerifiedMeasurementRecord, ...]


def _record(binding, definition, capability, **updates) -> VerifiedMeasurementRecord:
    ref = binding.result
    asset = definition.assets[0]
    values = {
        "result_id": ref.result_id,
        "result_sha256": hashlib.sha256(ref.result_id.encode()).hexdigest(),
        "bundle_id": "bundle_" + ref.bundle_sha256[:16],
        "bundle_sha256": ref.bundle_sha256,
        "method": definition,
        "method_definition_sha256": ref.method_definition_sha256,
        "current_capability": capability,
        "execution_state": ExecutionState.COMPLETE,
        "information_state": InformationState.SUFFICIENT,
        "trust_state": TrustState.VERIFIED,
        "compatibility_key": MeasurementCompatibilityKey(
            measurement_family=definition.family,
            quantity_id=definition.quantity_id,
            unit=definition.unit,
            result_schema=ResultSchemaReference(
                schema_id="schema_fragment_alpha", version="1.0.0"
            ),
            reference_asset=asset,
            grid_asset=asset,
            atlas_asset=asset,
            panel_asset=asset,
            normalization_semantics_id="sem_normalization_alpha",
            coordinate_semantics_id="sem_coordinate_alpha",
            denominator_semantics_id="sem_denominator_alpha",
            registered_policy=CompatibilityPolicyReference(
                policy_id="policy_fragment_alpha", version="1.0.0"
            ),
        ),
    }
    values.update(updates)
    return VerifiedMeasurementRecord(**values)


def _policy(record: VerifiedMeasurementRecord) -> CompatibilityPolicy:
    capability = record.current_capability
    return CompatibilityPolicy(
        policy_id="policy_fragment_alpha",
        version="1.0.0",
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        measurement_policies=(
            MeasurementCompatibilityPolicy(
                measurement_family=record.method.family,
                quantity_id=record.method.quantity_id,
                unit=record.method.unit,
                allowed_method_definitions=(
                    AllowedMethodDefinition(
                        method_ref=record.method.method_ref,
                        method_definition_sha256=record.method_definition_sha256,
                    ),
                ),
                allowed_result_schemas=(record.compatibility_key.result_schema,),
                delta_allowed_when_comparable=True,
                shared_axis_allowed_when_comparable=True,
            ),
        ),
    )


# Parametrize ``live`` indirectly with "registry" to back E04 by the
# protected result-trust registry; the default is a caller-held TrustStore.
TRUST_MODES = ("store", "registry")


@pytest.fixture
def live(tmp_path: Path, request: pytest.FixtureRequest):
    trust_mode = getattr(request, "param", "store")
    store = _store(tmp_path / "protected")
    for digit in ("c", "d"):
        _commit(store, digit)
    snapshot = store.active_snapshot()
    members = tuple(
        build_cohort_member(
            revision=revision,
            receipt=snapshot.receipts[index],
            collection_event=_collection_event(
                provider_namespace=revision.provider_namespace,
                subject_token=revision.biological.subject_token,
                collection_token=revision.biological.collection_token,
            ),
            time_axis=TIME_AXIS,
            lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
            denominator_contribution=True,
            unit_of_analysis=UnitOfAnalysis.COLLECTION,
        )
        for index, revision in enumerate(snapshot.revisions)
    )
    method_registry, head, head_sha256, capability = _authority()
    manifest = _manifest(
        _linkage_authority(snapshot),
        members,
        measurement_anchor=MeasurementAnchor(
            measurement_definition_sha256=capability.method_definition_sha256,
            anchor_definition_sha256="9" * 64,
            authority_sha256="a" * 64,
        ),
    )
    key = generate_development_keypair(KeyPurpose.RESULT)
    trust = TrustStore()
    trust.add_signing_key(key)
    imports = tmp_path / "imports"
    imports.mkdir()
    method = {
        "method_id": capability.method_ref.method_id,
        "version": capability.method_ref.version,
        "method_definition_sha256": capability.method_definition_sha256,
    }
    for name in ("a", "b"):
        build_result_bundle(
            imports / name,
            measurement=_measurement(),
            provenance=_provenance(run_token=f"synthetic.run.{name}"),
            method=method,
            signing_key=key,
        )
    trust_registry = None
    if trust_mode == "registry":
        trust_registry = ResultTrustRegistry(tmp_path / "result-trust")
        trust_registry.add_key(public_result_key(key))
        trust = RegistryTrust(trust_registry)
        trust_authority = {"result_trust_registry": trust_registry}
    else:
        trust_authority = {"trust_store": trust}
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": imports},
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
        **trust_authority,
    )
    cohort_registry = CohortRegistry(
        tmp_path / "cohort-registry",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    cohorts = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=store,
        cohort_registry=cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    cohort_registry.register(manifest)
    selector = cohort_registry.list_selectors().records[0]
    bindings = tuple(
        cohorts.import_bundle(
            selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            provider_namespace=member.provider_namespace,
            analysis_record_id=member.analysis_record_id,
            root_id="root_primary",
            relative_path=name,
            registry=method_registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
        )
        for member, name in zip(members, ("a", "b"), strict=True)
    )
    definition = next(
        item
        for item in method_registry.method_definitions
        if item.method_ref == capability.method_ref
    )
    records = tuple(_record(item, definition, capability) for item in bindings)
    try:
        yield Live(
            root=tmp_path,
            store=store,
            results=results,
            cohorts=cohorts,
            cohort_registry=cohort_registry,
            trust=trust,
            key=key,
            selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            bindings=bindings,
            records=records,
        )
    finally:
        cohorts.close()
        results.close()
        store.close()
        if trust_registry is not None:
            trust_registry.close()


@pytest.fixture
def registry(live: Live):
    value = ResultViewSourceRegistry(live.root / "e06", record_catalog=live.cohorts)
    try:
        yield value
    finally:
        value.close()


def _register(registry: ResultViewSourceRegistry, live: Live, **updates):
    record = updates.pop("record", live.records[0])
    policy = updates.pop("policy", _policy(record))
    values = {
        "cohort_selector_id": live.selector_id,
        "cohort_version": live.cohort_version,
        "record": record,
        "counterpart_record": live.records[1],
        "policy": policy,
        "expected_policy_sha256": compatibility_policy_sha256(policy),
        "denominator": _ledger(),
        "accessible_label": "Research aggregate",
        "qc_label": "Qualified research result",
    }
    values.update(updates)
    return registry.register_source(**values)


def _resolve(registry, live: Live, receipt, *, index: int = 0):
    return registry.resolve(
        receipt.selector_id,
        receipt.source_version,
        expected_member_sha256=live.bindings[index].member_sha256,
        expected_result_id=live.bindings[index].result.result_id,
    )


def test_registration_derives_source_and_resolve_reverifies_it(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    resolved = _resolve(registry, live, receipt)

    assert type(resolved) is RegisteredResultViewSource
    assert resolved.source.record == live.records[0]
    assert resolved.source.compatibility_decision.outcome is (
        CompatibilityOutcome.COMPARABLE
    )
    assert resolved.member_sha256 == live.bindings[0].member_sha256
    assert resolved.counterpart_member_sha256 == live.bindings[1].member_sha256
    assert resolved.binding_sha256 == hashlib.sha256(
        canonical_contract_bytes(live.bindings[0])
    ).hexdigest()
    assert resolved.source_sha256 == receipt.source_sha256
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert resolved.caller_asserted_fields == CALLER_ASSERTED_FIELDS
    assert "denominator" in resolved.caller_asserted_fields
    assert resolved.denominator_verified is False
    assert resolved.method_authority_head_current_verified is False
    assert resolved.replayed_against_live_authority is True
    assert receipt.selector_id == registry.selector_for_member(
        live.selector_id, live.cohort_version, live.bindings[0].member_sha256
    )
    # The decision's authority-head pin comes from the D06/E04 binding.
    assert (
        resolved.source.compatibility_decision.binding.authority_head_sha256
        == live.bindings[0].result.authority_head_sha256
    )


def test_exact_retry_is_idempotent_and_new_inputs_append_a_version(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    first = _register(registry, live)
    assert _register(registry, live) == first
    second = _register(registry, live, accessible_label="Research aggregate two")
    assert second.selector_id == first.selector_id
    assert (first.source_version, second.source_version) == (1, 2)
    assert second.state_version == 2
    assert _resolve(registry, live, first).source.accessible_label == (
        "Research aggregate"
    )
    assert _resolve(registry, live, second).source.accessible_label == (
        "Research aggregate two"
    )
    other = _register(
        registry,
        live,
        record=live.records[1],
        counterpart_record=live.records[0],
    )
    assert other.selector_id != first.selector_id
    assert other.source_version == 1
    assert _resolve(registry, live, other, index=1).member_sha256 == (
        live.bindings[1].member_sha256
    )


def test_registration_accepts_no_caller_source_decision_or_member() -> None:
    import inspect

    parameters = inspect.signature(ResultViewSourceRegistry.register_source).parameters
    for forbidden in (
        "source",
        "compatibility_decision",
        "decision",
        "member_sha256",
        "trusted_authority_head_sha256",
        "sources",
    ):
        assert forbidden not in parameters


@pytest.mark.parametrize(
    "updates",
    (
        {"bundle_sha256": "f" * 64},
        {"trust_state": TrustState.UNVERIFIED},
        {"trust_state": TrustState.REVOKED},
        {"execution_state": ExecutionState.FAILED},
    ),
)
def test_record_that_disagrees_with_live_catalog_is_rejected(
    registry: ResultViewSourceRegistry, live: Live, updates
) -> None:
    record = live.records[0].model_copy(update=updates)
    with pytest.raises(ResultViewSourceRegistryConflict):
        _register(registry, live, record=record)
    assert registry.list_selectors(live.selector_id, live.cohort_version).records == ()


def test_capability_that_disagrees_with_catalog_binding_is_rejected(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    capability = live.records[0].current_capability
    for update in (
        {"authority_revision": capability.authority_revision + 1},
        {"authority_head_sha256": "e" * 64},
        {"research_inspectable": not capability.research_inspectable},
    ):
        changed = capability.model_copy(update=update)
        record = live.records[0].model_copy(update={"current_capability": changed})
        with pytest.raises(ResultViewSourceRegistryStale, match="live catalog"):
            _register(registry, live, record=record)


def test_result_outside_the_cohort_and_self_counterpart_are_rejected(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    stranger = live.records[0].model_copy(
        update={"result_id": "result_" + "0" * 40}
    )
    with pytest.raises(ResultViewSourceRegistryStale, match="exactly one live member"):
        _register(registry, live, record=stranger)
    with pytest.raises(ResultViewSourceRegistryConflict, match="distinct"):
        _register(registry, live, counterpart_record=live.records[0])


def test_hostile_caller_contracts_are_rejected_before_authority(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    class Subclass(VerifiedMeasurementRecord):
        pass

    subclass = Subclass(**live.records[0].model_dump())
    with pytest.raises(ResultViewSourceRegistryConflict, match="exact canonical"):
        _register(registry, live, record=subclass)
    poisoned = live.records[0].model_copy()
    object.__setattr__(poisoned, "__pydantic_private__", {"hidden": "subject"})
    with pytest.raises(ResultViewSourceRegistryConflict, match="exact canonical"):
        _register(registry, live, record=poisoned)
    with pytest.raises(ResultViewSourceRegistryConflict, match="exact canonical"):
        _register(registry, live, denominator=list(_ledger()))
    for bad in (
        {"cohort_selector_id": "cohort_selector_x"},
        {"cohort_version": True},
        {"expected_policy_sha256": "x"},
        {"accessible_label": "a" * 500},
    ):
        with pytest.raises(ResultViewSourceRegistryConflict, match="input is invalid"):
            _register(registry, live, **bad)


def test_resolve_requires_exact_selector_version_and_commitments(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    with pytest.raises(ResultViewSourceRegistryConflict, match="unavailable"):
        registry.resolve(
            receipt.selector_id,
            2,
            expected_member_sha256=live.bindings[0].member_sha256,
            expected_result_id=live.bindings[0].result.result_id,
        )
    with pytest.raises(ResultViewSourceRegistryConflict, match="unavailable"):
        registry.resolve(
            "e06_source_" + "0" * 40,
            1,
            expected_member_sha256=live.bindings[0].member_sha256,
            expected_result_id=live.bindings[0].result.result_id,
        )
    for member, result in (
        (live.bindings[1].member_sha256, live.bindings[0].result.result_id),
        (live.bindings[0].member_sha256, live.bindings[1].result.result_id),
    ):
        with pytest.raises(ResultViewSourceRegistryConflict, match="expected result"):
            registry.resolve(
                receipt.selector_id,
                1,
                expected_member_sha256=member,
                expected_result_id=result,
            )
    for selector, version in (("d03_series_" + "0" * 40, 1), (receipt.selector_id, 0)):
        with pytest.raises(ResultViewSourceRegistryConflict, match="invalid"):
            registry.resolve(
                selector,
                version,
                expected_member_sha256=live.bindings[0].member_sha256,
                expected_result_id=live.bindings[0].result.result_id,
            )


@pytest.mark.parametrize("live", TRUST_MODES, indirect=True)
def test_result_key_revocation_makes_source_stale(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    live.trust.revoke(live.key.key_id)
    with pytest.raises(ResultViewSourceRegistryStale):
        _resolve(registry, live, receipt)
    page = registry.list_selectors(live.selector_id, live.cohort_version)
    assert [row.authority_state for row in page.records] == [
        SourceAuthorityState.STALE
    ]
    with pytest.raises(ResultViewSourceRegistryStale):
        _register(registry, live)


@pytest.mark.parametrize("live", TRUST_MODES, indirect=True)
def test_trust_store_mutation_makes_source_stale(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    live.trust.add_signing_key(generate_development_keypair(KeyPurpose.RESULT))
    with pytest.raises(ResultViewSourceRegistryStale):
        _resolve(registry, live, receipt)


def test_linkage_advance_makes_source_stale(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    _commit(live.store, "e")
    with pytest.raises(ResultViewSourceRegistryStale):
        _resolve(registry, live, receipt)
    with pytest.raises(ResultViewSourceRegistryStale):
        registry.list_selectors(live.selector_id, live.cohort_version)


@pytest.mark.parametrize("live", TRUST_MODES, indirect=True)
def test_resolve_holds_d06_fence_through_exact_return(
    registry: ResultViewSourceRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(registry, live)
    original_sha256 = hashlib.sha256
    started = threading.Event()
    finished = threading.Event()
    workers: list[threading.Thread] = []

    def sha256(content=b"", *args, **kwargs):
        # The replay digest is computed while the result is constructed, after
        # live re-verification and before return.
        if bytes(content).startswith(b"traceback-e06-source-replay-v2") and not workers:

            def revoke() -> None:
                started.set()
                live.trust.revoke(live.key.key_id)
                finished.set()

            worker = threading.Thread(target=revoke)
            workers.append(worker)
            worker.start()
            assert started.wait(timeout=1)
            assert not finished.wait(timeout=0.05)
        return original_sha256(content, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", sha256)
    resolved = _resolve(registry, live, receipt)
    assert resolved.object_sha256 == receipt.object_sha256
    assert workers
    workers[0].join(timeout=5)
    assert finished.is_set()
    monkeypatch.undo()
    with pytest.raises(ResultViewSourceRegistryStale):
        _resolve(registry, live, receipt)


def test_d01_fence_is_not_reentrant_inside_the_d06_fence(live: Live) -> None:
    with CohortRecordCatalog.record_status_authority_fence(
        live.cohorts, live.selector_id, live.cohort_version
    ):
        with pytest.raises(ProviderLinkageStoreUnsafe, match="idle connection"):
            with ProviderLinkageStore.authority_read_fence(live.store):
                pass


def test_one_result_cannot_represent_two_members(live: Live) -> None:
    status = live.cohorts.record_status_for_manifest(
        live.selector_id, live.cohort_version
    )
    assert registry_module._member_for_result(
        status, live.bindings[0].result.result_id
    ) == live.bindings[0]
    duplicated = status.members[1].model_copy(
        update={"binding": status.members[0].binding}
    )
    forged = CohortManifestRecordStatus.model_construct(
        **{**dict(status), "members": (status.members[0], duplicated)}
    )
    with pytest.raises(ResultViewSourceRegistryStale, match="exactly one"):
        registry_module._member_for_result(forged, live.bindings[0].result.result_id)
    withheld = status.members[0].model_copy(
        update={"availability": CohortRecordAvailability.WITHHELD}
    )
    forged = CohortManifestRecordStatus.model_construct(
        **{**dict(status), "members": (withheld, status.members[1])}
    )
    with pytest.raises(ResultViewSourceRegistryStale, match="not available"):
        registry_module._member_for_result(forged, live.bindings[0].result.result_id)


def test_journal_rejects_one_result_for_two_members(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    first = _register(registry, live)
    second = _register(registry, live, record=live.records[1],
                       counterpart_record=live.records[0])
    loaded, _ = registry_module._SR_LOAD_STATE(registry)
    values = {digest: value for digest, (value, _, _) in loaded.items()}
    journal = registry_module._SR_LOAD_JOURNAL(registry)
    epoch = registry._metadata.registry_epoch_sha256
    registry_module._validate_journal_semantics(epoch, journal, values)
    moved = values[second.object_sha256].model_copy(
        update={"source": values[first.object_sha256].source}
    )
    values[second.object_sha256] = moved
    with pytest.raises(ValueError, match="two members"):
        registry_module._validate_journal_semantics(epoch, journal, values)


def test_stored_object_cannot_pair_a_source_with_other_inputs(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    value = registry_module.registered_source_object_from_bytes(content)
    values = value.model_dump(mode="python")
    swapped = dict(values)
    swapped["compatibility_request"] = {
        **values["compatibility_request"],
        "left": values["compatibility_request"]["right"],
        "right": values["compatibility_request"]["left"],
    }
    with pytest.raises(ValidationError, match="does not match its request"):
        RegisteredResultViewSourceObject.model_validate(swapped)
    forged_pin = dict(values)
    forged_pin["compatibility_request"] = {
        **values["compatibility_request"],
        "trusted_authority_head_sha256": "e" * 64,
    }
    with pytest.raises(ValidationError, match="binding head"):
        RegisteredResultViewSourceObject.model_validate(forged_pin)


def test_registered_result_rejects_altered_identity_or_ledger(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    resolved = _resolve(registry, live, _register(registry, live))
    values = resolved.model_dump(mode="python")
    other_ledger = _ledger().model_copy(
        update={"input_records": _ledger().input_records.model_copy(
            update={"accessible_label": "Other input records"}
        )}
    )
    for update, match in (
        ({"member_sha256": live.bindings[1].member_sha256}, "selector"),
        ({"denominator_ledger_sha256": "0" * 64}, "ledger"),
        ({"source": {**values["source"], "denominator": other_ledger}}, "digest"),
        ({"record_status_sha256": "0" * 64}, "replay digest"),
        ({"caller_asserted_fields": CALLER_ASSERTED_FIELDS[1:]}, "unverified"),
        ({"denominator_verified": True}, "denominator_verified"),
    ):
        with pytest.raises(ValidationError, match=match):
            RegisteredResultViewSource.model_validate({**values, **update})


def test_selector_page_is_private_counted_and_paginated(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipts = (
        _register(registry, live),
        _register(registry, live, accessible_label="Second aggregate"),
        _register(
            registry, live, record=live.records[1], counterpart_record=live.records[0]
        ),
    )
    page = registry.list_selectors(live.selector_id, live.cohort_version, limit=2)
    assert page.state_version == 3
    assert len(page.records) == 2
    assert page.next_after_selector_id == page.records[-1].selector_id
    rest = registry.list_selectors(
        live.selector_id,
        live.cohort_version,
        after_selector_id=page.next_after_selector_id,
        after_source_version=page.next_after_source_version,
    )
    rows = page.records + rest.records
    assert rest.next_after_selector_id is None
    assert sorted((row.selector_id, row.source_version) for row in rows) == sorted(
        (item.selector_id, item.source_version) for item in receipts
    )
    assert all(row.authority_state is SourceAuthorityState.CURRENT for row in rows)
    public = canonical_contract_bytes(page).decode() + canonical_contract_bytes(
        rest
    ).decode()
    for forbidden in (
        live.bindings[0].result.result_id,
        live.bindings[1].result.result_id,
        live.bindings[0].member_sha256,
        live.bindings[0].analysis_record_id,
        live.bindings[0].provider_namespace,
        live.bindings[0].result.bundle_sha256,
        live.selector_id,
        "Research aggregate",
        "Second aggregate",
        "Qualified research result",
        "subject_",
    ):
        assert forbidden not in public
    for bad in ({"limit": 0}, {"limit": 101}, {"after_selector_id": "x"}):
        with pytest.raises(ResultViewSourceRegistryConflict):
            registry.list_selectors(live.selector_id, live.cohort_version, **bad)


def test_object_tamper_and_extra_entries_fail_closed(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(original.replace(b"Research aggregate", b"Research aggregatX"))
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="digest"):
        _resolve(registry, live, receipt)
    path.write_bytes(original)
    assert _resolve(registry, live, receipt).object_sha256 == receipt.object_sha256
    (registry.root / "objects" / "notes.txt").write_bytes(b"x")
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="invalid object"):
        _resolve(registry, live, receipt)


def test_journal_rollback_and_deletion_fail_closed(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    first = _register(registry, live)
    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    second = _register(registry, live, accessible_label="Second aggregate")
    after = journal.read_bytes()
    with open(journal, "r+b") as handle:
        handle.truncate(len(before))
    (registry.root / "objects" / f"{second.object_sha256}.json").unlink()
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="rollback"):
        _resolve(registry, live, first)
    with open(journal, "r+b") as handle:
        handle.write(after)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="inconsistent"):
        _resolve(registry, live, first)


def test_reopen_requires_exact_retained_identity_and_head(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    root = registry.root
    registry.close()
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="required"):
        ResultViewSourceRegistry(root, record_catalog=live.cohorts)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="identity or head"):
        ResultViewSourceRegistry(
            root,
            record_catalog=live.cohorts,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256="0" * 64,
        )
    reopened = ResultViewSourceRegistry(
        root,
        record_catalog=live.cohorts,
        expected_registry_id=receipt.registry_id,
        expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
        expected_state_head_sha256=receipt.state_head_sha256,
    )
    try:
        assert _resolve(reopened, live, receipt).object_sha256 == receipt.object_sha256
    finally:
        reopened.close()


def test_registry_cannot_be_rebound_to_another_d06_catalog(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    root = registry.root
    registry.close()
    other = CohortRecordCatalog(
        live.root / "other-records",
        result_catalog=live.results,
        linkage_store=live.store,
        cohort_registry=live.cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        with pytest.raises(ResultViewSourceRegistryUnsafe, match="authority changed"):
            ResultViewSourceRegistry(
                root,
                record_catalog=other,
                expected_registry_id=receipt.registry_id,
                expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
                expected_state_head_sha256=receipt.state_head_sha256,
            )
    finally:
        other.close()
    with pytest.raises(TypeError, match="exact D06"):
        ResultViewSourceRegistry(live.root / "type", record_catalog=object())


def test_backup_restore_preserves_identity_and_reverifies(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    assert result_view_source_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    values = {
        "record_catalog": live.cohorts,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    with pytest.raises(ResultViewSourceRegistryConflict, match="expected head"):
        ResultViewSourceRegistry.restore(
            live.root / "bad", backup, **{**values, "expected_state_head_sha256": "0" * 64}
        )
    with pytest.raises(ResultViewSourceRegistryConflict, match="backup is invalid"):
        ResultViewSourceRegistry.restore(live.root / "trunc", backup[:-10], **values)
    assert not (live.root / "bad").exists()
    assert not (live.root / "trunc").exists()
    restored = ResultViewSourceRegistry.restore(live.root / "restored", backup, **values)
    try:
        assert _resolve(restored, live, receipt).object_sha256 == receipt.object_sha256
    finally:
        restored.close()
    with pytest.raises(ResultViewSourceRegistryConflict, match="already exists"):
        ResultViewSourceRegistry.restore(live.root / "restored", backup, **values)


def test_failed_restore_removes_its_partial_target_and_can_retry(
    registry: ResultViewSourceRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = live.root / "partial-restore"
    values = {
        "record_catalog": live.cohorts,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="restore failed"):
        ResultViewSourceRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()
    restored = ResultViewSourceRegistry.restore(target, backup, **values)
    restored.close()


def test_torn_journal_append_is_truncated_and_registration_retries(
    registry: ResultViewSourceRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"e06-source-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="append failed"):
        _register(registry, live)
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""
    receipt = _register(registry, live)
    assert receipt.state_version == 1
    assert _resolve(registry, live, receipt).object_sha256 == receipt.object_sha256


def test_instance_and_class_callable_shadows_are_rejected(
    registry: ResultViewSourceRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    object.__setattr__(registry, "resolve", lambda *args, **kwargs: None)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="callable changed"):
        registry.resolve(receipt.selector_id, 1, expected_member_sha256="0" * 64,
                         expected_result_id="result_" + "0" * 40)
    del registry.__dict__["resolve"]
    monkeypatch.setattr(
        ResultViewSourceRegistry, "_replay_in_fence", lambda *args: None
    )
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="callable changed"):
        _resolve(registry, live, receipt)


def test_pinned_authority_and_constructor_replacement_is_rejected(
    registry: ResultViewSourceRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    monkeypatch.setattr(
        compatibility_module, "decide_compatibility", lambda request: None
    )
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="authority callable"):
        _resolve(registry, live, receipt)
    monkeypatch.undo()
    monkeypatch.setattr(
        registry_module, "_SR_RESOLVED", RegisteredResultViewSource.model_construct
    )
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="authority callable"):
        _resolve(registry, live, receipt)
    monkeypatch.undo()
    monkeypatch.setattr(
        CohortRecordCatalog,
        "record_status_authority_fence",
        CohortRecordCatalog.record_status_for_manifest,
    )
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="authority callable"):
        _resolve(registry, live, receipt)
    monkeypatch.undo()
    assert _resolve(registry, live, receipt).object_sha256 == receipt.object_sha256


def test_instance_authority_state_replacement_is_rejected(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    original = registry.__dict__["_trusted_head_sha256"]
    registry.__dict__["_trusted_head_sha256"] = "0" * 64
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="authority state"):
        _resolve(registry, live, receipt)
    registry.__dict__["_trusted_head_sha256"] = original
    assert _resolve(registry, live, receipt).object_sha256 == receipt.object_sha256


@pytest.mark.parametrize("live", TRUST_MODES, indirect=True)
def test_concurrent_resolve_and_revoke_terminate_with_exact_outcomes(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def reader() -> None:
        for _ in range(4):
            try:
                _resolve(registry, live, receipt)
                outcomes.append("current")
            except ResultViewSourceRegistryStale:
                outcomes.append("stale")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    live.trust.revoke(live.key.key_id)
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(outcomes) == 8
    with pytest.raises(ResultViewSourceRegistryStale):
        _resolve(registry, live, receipt)


def test_interpreter_warning_registry_does_not_disable_the_registry(
    registry: ResultViewSourceRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert _resolve(registry, live, receipt).object_sha256 == receipt.object_sha256


def test_stored_source_is_the_exact_e06_contract(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    value = registry_module.registered_source_object_from_bytes(content)
    assert registered_source_object_bytes(value) == content
    assert type(value.source) is ResultViewSource
    assert len(content) < registry_module.MAX_OBJECT_BYTES // 4


def test_counterpart_fields_are_declared_unverified() -> None:
    for name in (
        "counterpart.information_state",
        "counterpart.compatibility_key.result_schema",
        "record.compatibility_key.semantics",
        "counterpart.result_sha256",
        "counterpart.bundle_id",
        "counterpart.current_capability.effective_approval_ref",
    ):
        assert name in CALLER_ASSERTED_FIELDS


def test_concurrent_restores_to_one_target_leave_exactly_one_registry(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    target = live.root / "raced-restore"
    values = {
        "record_catalog": live.cohorts,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def restore() -> None:
        barrier.wait()
        try:
            outcomes.append(ResultViewSourceRegistry.restore(target, backup, **values))
        except (ResultViewSourceRegistryConflict, ResultViewSourceRegistryUnsafe) as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=restore) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    winners = [item for item in outcomes if isinstance(item, ResultViewSourceRegistry)]
    assert len(outcomes) == 2 and len(winners) == 1
    try:
        assert _resolve(winners[0], live, receipt).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        winners[0].close()
    assert not [
        item for item in live.root.iterdir() if item.name.startswith(".raced-restore.")
    ]


def test_a_root_without_metadata_never_rebootstraps(live: Live) -> None:
    root = live.root / "metadata-deleted"
    created = ResultViewSourceRegistry(root, record_catalog=live.cohorts)
    created.close()
    (root / "registry-metadata.json").unlink()
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="metadata is missing"):
        ResultViewSourceRegistry(root, record_catalog=live.cohorts)
    assert not (root / "registry-metadata.json").exists()


def test_registered_result_binds_every_returned_commitment(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    resolved = _resolve(registry, live, _register(registry, live))
    values = resolved.model_dump(mode="python")
    for name in (
        "catalog_result_sha256",
        "cohort_manifest_sha256",
        "state_head_sha256",
        "binding_sha256",
        "catalog_authority_sha256",
        "object_sha256",
    ):
        with pytest.raises(ValidationError, match="replay digest"):
            RegisteredResultViewSource.model_validate({**values, name: "0" * 64})
    for name, value in (
        ("registry_id", "e06_registry_" + "0" * 32),
        ("state_version", resolved.state_version + 1),
        ("source_version", resolved.source_version + 1),
    ):
        with pytest.raises(ValidationError, match="replay digest"):
            RegisteredResultViewSource.model_validate({**values, name: value})
    with pytest.raises(ValidationError):
        RegisteredResultViewSource.model_validate(
            {**values, "counterpart_member_sha256": "0" * 64}
        )


def test_replay_rejects_any_stored_field_that_live_derivation_does_not_reproduce(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    loaded, _ = registry_module._SR_LOAD_STATE(registry)
    value = loaded[receipt.object_sha256][0]
    with CohortRecordCatalog.record_status_authority_fence(
        live.cohorts, live.selector_id, live.cohort_version
    ) as (_, status):
        registry_module._SR_REPLAY_IN_FENCE(registry, value, status)
        for update in (
            {"binding_sha256": "0" * 64},
            {"counterpart_binding_sha256": "0" * 64},
            {"counterpart_member_sha256": "0" * 64},
            {"cohort_registry_epoch_sha256": "0" * 64},
        ):
            with pytest.raises(ResultViewSourceRegistryStale):
                registry_module._SR_REPLAY_IN_FENCE(
                    registry, value.model_copy(update=update), status
                )


def test_peer_rejects_rollback_to_its_own_preappend_head(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    first = _register(registry, live)
    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    peer = ResultViewSourceRegistry(
        registry.root,
        record_catalog=live.cohorts,
        expected_registry_id=first.registry_id,
        expected_registry_epoch_sha256=first.registry_epoch_sha256,
        expected_state_head_sha256=first.state_head_sha256,
    )
    try:
        second = _register(registry, live, accessible_label="Second aggregate")
        with open(journal, "r+b") as handle:
            handle.truncate(len(before))
        (registry.root / "objects" / f"{second.object_sha256}.json").unlink()
        # The peer's own trusted head is still in the truncated chain; only the
        # process-wide head fence detects the rollback.
        with pytest.raises(ResultViewSourceRegistryUnsafe, match="rollback"):
            _resolve(peer, live, first)
    finally:
        peer.close()


def test_failed_restore_reopen_removes_the_target_and_can_retry(
    registry: ResultViewSourceRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    target = live.root / "reopen-restore"
    values = {
        "record_catalog": live.cohorts,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    original_flock = fcntl.flock
    restored_lock = target / ".registry.lock"

    def failing_flock(descriptor, operation):
        # Only the restored registry's own lock fails, during its final reopen.
        if restored_lock.exists():
            lock = restored_lock.stat()
            bound = os.fstat(descriptor)
            if (bound.st_dev, bound.st_ino) == (lock.st_dev, lock.st_ino):
                raise OSError("lock unavailable")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    with pytest.raises(ResultViewSourceRegistryUnsafe, match="restore failed"):
        ResultViewSourceRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()
    restored = ResultViewSourceRegistry.restore(target, backup, **values)
    try:
        assert _resolve(restored, live, receipt).object_sha256 == receipt.object_sha256
    finally:
        restored.close()


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    registry: ResultViewSourceRegistry, live: Live
) -> None:
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: _register(registry, live),
        lambda values: ResultViewSourceRegistry(
            registry.root,
            record_catalog=live.cohorts,
            **storage_checks.expected(values),
        ),
        ResultViewSourceRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    registry: ResultViewSourceRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_append_interrupt_truncates(
        registry, registry_module, lambda: _register(registry, live), monkeypatch
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    registry: ResultViewSourceRegistry, live: Live, tmp_path: Path
) -> None:
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, ResultViewSourceRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    registry: ResultViewSourceRegistry,
    live: Live,
) -> None:
    _register(registry, live)
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: ResultViewSourceRegistry(
            registry.root,
            record_catalog=live.cohorts,
            **storage_checks.expected(values),
        ),
        ResultViewSourceRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    registry: ResultViewSourceRegistry,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_checks.check_interrupted_creation(
        lambda root: ResultViewSourceRegistry(root, record_catalog=live.cohorts),
        lambda root, values: ResultViewSourceRegistry(
            root, record_catalog=live.cohorts, **storage_checks.expected(values)
        ),
        tmp_path / "created-by-storage-check",
        registry_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    registry: ResultViewSourceRegistry,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(registry, live)
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: ResultViewSourceRegistry.restore(
            target,
            backup,
            record_catalog=live.cohorts,
            **storage_checks.expected(values),
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )


def test_storage_creation_under_a_symlinked_parent(
    registry: ResultViewSourceRegistry, live: Live, tmp_path: Path
) -> None:
    storage_checks.check_creation_under_symlinked_parent(
        lambda root: ResultViewSourceRegistry(root, record_catalog=live.cohorts),
        lambda root, values: ResultViewSourceRegistry(
            root, record_catalog=live.cohorts, **storage_checks.expected(values)
        ),
        tmp_path,
    )
