"""E12 family-source artifact discovery: derived E07 artifacts, live replay."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import evidence_inspector.fragment_explorer as e07_module
import evidence_inspector.measurement_source_artifact_registry as registry_module
import tests.test_cohort_import as cohort_import_tests
import tests.test_result_view_source_registry as e06_tests
from evidence_inspector.cohort_import import CohortRecordCatalog
from evidence_inspector.compatibility import (
    CompatibilityOutcome,
    InformationState,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
)
from evidence_inspector.fragment_explorer import (
    ExplorerControls,
    ExplorerMethodSelection,
    FragmentExplorerRequest,
    FragmentQuantity,
    PanelId,
    build_fragment_explorer_state,
    build_fragment_explorer_view,
    fragment_source_from_verified_bundle,
)
from evidence_inspector.measurement_source_artifact_registry import (
    ArtifactAuthorityState,
    MeasurementSourceArtifactNotApplicable,
    MeasurementSourceArtifactRegistry,
    MeasurementSourceArtifactRegistryConflict,
    MeasurementSourceArtifactRegistryStale,
    MeasurementSourceArtifactRegistryUnsafe,
    RegisteredFragmentSourceArtifact,
    RegisteredFragmentSourceArtifactObject,
    SourceArtifactFamily,
    SourceArtifactSelectorRecord,
    measurement_source_artifact_backup_from_bytes,
    registered_artifact_object_bytes,
    registered_artifact_object_from_bytes,
)
from evidence_inspector.method_registry import MethodDefinition
from evidence_inspector.provider_linkage_store import (
    ProviderLinkageStore,
    ProviderLinkageStoreUnsafe,
)
from evidence_inspector.result_view_source_registry import (
    CALLER_ASSERTED_FIELDS,
    ResultViewSourceRegistry,
    ResultViewSourceRegistryStale,
)
from tests.test_method_registry import _definition
from tests.test_result_view_source_registry import Live, _policy
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import KeyPurpose, generate_development_keypair
from tests import registry_storage_checks as storage_checks


def _fragment_authority():
    """D06 authority whose E01 method is an exact E07 fragment quantity."""

    base = _definition(method_id="mth_fragment_aligned_reference_span")
    definition = MethodDefinition.model_validate(
        {
            **base.model_dump(mode="python"),
            "quantity_id": "qty_fragment_aligned_reference_span",
            "unit": "unit_bp",
        }
    )
    original = cohort_import_tests._definition
    cohort_import_tests._definition = lambda **_: definition
    try:
        return cohort_import_tests._authority()
    finally:
        cohort_import_tests._definition = original


def _measurement_digest(live: Live, index: int) -> str:
    bundle, _ = live.results.verify_reference(live.bindings[index].result)
    return hashlib.sha256(canonical_json_bytes(bundle.measurement)).hexdigest()


def _with(record: VerifiedMeasurementRecord, **updates) -> VerifiedMeasurementRecord:
    return VerifiedMeasurementRecord.model_validate(
        {**record.model_dump(mode="python"), **updates}
    )


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(e06_tests, "_authority", _fragment_authority)
    # The E06 fixture is parametrized over trust modes; use its default store mode.
    fixture = e06_tests.live.__wrapped__(tmp_path, SimpleNamespace())
    value = next(fixture)
    monkeypatch.undo()
    # E06 records whose caller-asserted result digest is the E04 measurement.
    value.records = tuple(
        _with(record, result_sha256=_measurement_digest(value, index))
        for index, record in enumerate(value.records)
    )
    try:
        yield value
    finally:
        fixture.close()


@pytest.fixture
def e06(live: Live):
    value = ResultViewSourceRegistry(live.root / "e06", record_catalog=live.cohorts)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def registry(live: Live, e06: ResultViewSourceRegistry):
    value = MeasurementSourceArtifactRegistry(
        live.root / "family", result_view_source_registry=e06
    )
    try:
        yield value
    finally:
        value.close()


def _e06_register(e06: ResultViewSourceRegistry, live: Live, index: int = 0, **updates):
    other = 1 - index
    values = {
        "record": live.records[index],
        "counterpart_record": live.records[other],
    }
    values.update(updates)
    return e06_tests._register(e06, live, **values)


def _register(registry, live: Live, e06_receipt, index: int = 0, **updates):
    values = {
        "e06_selector_id": e06_receipt.selector_id,
        "e06_source_version": e06_receipt.source_version,
        "expected_member_sha256": live.bindings[index].member_sha256,
        "expected_result_id": live.bindings[index].result.result_id,
        "counterpart_record": live.records[1 - index],
        "policy": _policy(live.records[index]),
    }
    values.update(updates)
    return registry.register_fragment_artifact(**values)


def _resolve(registry, live: Live, receipt, e06_receipt, index: int = 0):
    return registry.resolve(
        receipt.selector_id,
        expected_e06_selector_id=e06_receipt.selector_id,
        expected_e06_source_version=e06_receipt.source_version,
        expected_member_sha256=live.bindings[index].member_sha256,
        expected_result_id=live.bindings[index].result.result_id,
    )


def _registered(registry, e06, live: Live, index: int = 0):
    e06_receipt = _e06_register(e06, live, index)
    return _register(registry, live, e06_receipt, index), e06_receipt


def _independent_view(live: Live, index: int = 0):
    """Build the expected E07 view from the public E07 API and live E04 bundles."""

    sources = []
    for position in (index, 1 - index):
        bundle, _ = live.results.verify_reference(live.bindings[position].result)
        record = _with(
            live.records[position],
            bundle_sha256=live.bindings[position].result.bundle_manifest_sha256,
        )
        sources.append(
            fragment_source_from_verified_bundle(
                bundle, record=record, quantity=FragmentQuantity.ALIGNED_REFERENCE_SPAN
            )
        )
    left, right = sources
    policy = _policy(live.records[index])
    state = build_fragment_explorer_state(
        left=ExplorerMethodSelection(
            result_id=left.record.result_id, method_ref=left.record.method.method_ref
        ),
        right=ExplorerMethodSelection(
            result_id=right.record.result_id, method_ref=right.record.method.method_ref
        ),
        filters_linked=False,
        left_controls=ExplorerControls(
            bin_start_inclusive=0, bin_end_exclusive=len(left.chart.rows)
        ),
        right_controls=ExplorerControls(
            bin_start_inclusive=0, bin_end_exclusive=len(right.chart.rows)
        ),
    )
    return build_fragment_explorer_view(
        FragmentExplorerRequest(
            sources=tuple(sorted(sources, key=lambda item: item.record.result_id)),
            policy=policy,
            trusted_policy_sha256=compatibility_policy_sha256(policy),
            trusted_authority_head_sha256=(
                live.bindings[index].result.authority_head_sha256
            ),
            state=state,
        )
    )


def test_registration_derives_the_e07_artifact_and_resolve_replays_it(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    resolved = _resolve(registry, live, receipt, e06_receipt)

    assert type(resolved) is RegisteredFragmentSourceArtifact
    assert resolved.family is SourceArtifactFamily.FRAGMENT
    assert resolved.artifact == _independent_view(live)
    assert resolved.subject_panel is PanelId.A
    assert resolved.artifact.left.selection.result_id == (
        live.bindings[0].result.result_id
    )
    assert len(resolved.artifact.left.rows) == len(
        resolved.artifact.request.sources[0].chart.rows
    )
    assert resolved.artifact.compatibility.outcome is CompatibilityOutcome.COMPARABLE
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.artifact_sha256 == receipt.artifact_sha256
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert resolved.e06_selector_id == e06_receipt.selector_id
    assert resolved.e06_object_sha256 == e06_receipt.object_sha256
    assert resolved.member_sha256 == live.bindings[0].member_sha256
    assert resolved.counterpart_member_sha256 == live.bindings[1].member_sha256
    assert resolved.caller_asserted_fields == CALLER_ASSERTED_FIELDS
    assert resolved.replayed_against_live_authority is True
    assert resolved.method_authority_head_current_verified is False
    assert receipt.selector_id == registry.selector_for_e06_source(
        e06_receipt.selector_id, e06_receipt.source_version
    )
    # Only the bundle digest differs between the E06 and the E07 record.
    e07_record = next(
        item.record
        for item in resolved.artifact.request.sources
        if item.record.result_id == live.bindings[0].result.result_id
    )
    assert e07_record == _with(
        live.records[0],
        bundle_sha256=live.bindings[0].result.bundle_manifest_sha256,
    )


def test_registration_accepts_no_caller_artifact_or_presentation_state() -> None:
    parameters = inspect.signature(
        MeasurementSourceArtifactRegistry.register_fragment_artifact
    ).parameters
    for forbidden in (
        "artifact",
        "view",
        "request",
        "sources",
        "state",
        "controls",
        "decision",
        "bundle",
        "member_sha256",
    ):
        assert forbidden not in parameters


def test_artifact_derivation_is_deterministic_across_registries(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    other = MeasurementSourceArtifactRegistry(
        live.root / "family-two", result_view_source_registry=e06
    )
    try:
        second = _register(other, live, e06_receipt)
    finally:
        other.close()
    assert second.artifact_sha256 == receipt.artifact_sha256
    assert second.selector_id != receipt.selector_id


def test_exact_retry_is_idempotent_and_each_e06_source_has_its_own_selector(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    assert _register(registry, live, e06_receipt) == receipt
    newer = _e06_register(e06, live, accessible_label="Research aggregate two")
    assert newer.selector_id == e06_receipt.selector_id
    second = _register(registry, live, newer)
    assert second.selector_id != receipt.selector_id
    assert second.artifact_sha256 == receipt.artifact_sha256
    counterpart_receipt, counterpart_e06 = _registered(registry, e06, live, index=1)
    resolved = _resolve(registry, live, counterpart_receipt, counterpart_e06, index=1)
    assert resolved.artifact.state.left.result_id == live.bindings[1].result.result_id
    assert counterpart_receipt.state_version == 3


@pytest.mark.parametrize(
    "mutation",
    (
        "counterpart_bundle_id",
        "counterpart_is_subject",
        "counterpart_information",
        "counterpart_trust",
        "policy_flag",
        "policy_pin",
    ),
)
def test_inputs_that_do_not_reproduce_the_e06_decision_are_rejected(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live, mutation: str
) -> None:
    e06_receipt = _e06_register(e06, live)
    policy = _policy(live.records[0])
    updates: dict[str, object] = {}
    if mutation == "counterpart_bundle_id":
        updates["counterpart_record"] = _with(live.records[1], bundle_id="bundle_other")
    elif mutation == "counterpart_is_subject":
        updates["counterpart_record"] = live.records[0]
    elif mutation == "counterpart_information":
        updates["counterpart_record"] = _with(
            live.records[1], information_state=InformationState.INSUFFICIENT
        )
    elif mutation == "counterpart_trust":
        updates["counterpart_record"] = _with(
            live.records[1], trust_state=TrustState.UNVERIFIED
        )
    elif mutation == "policy_flag":
        rule = policy.measurement_policies[0].model_copy(
            update={"delta_allowed_when_comparable": False}
        )
        updates["policy"] = type(policy).model_validate(
            {
                **policy.model_dump(mode="python"),
                "measurement_policies": (rule.model_dump(mode="python"),),
            }
        )
    else:
        updates["policy"] = type(policy).model_validate(
            {**policy.model_dump(mode="python"), "version": "1.0.1"}
        )
    with pytest.raises(MeasurementSourceArtifactRegistryConflict):
        _register(registry, live, e06_receipt, **updates)
    assert registry.list_selectors(live.selector_id, live.cohort_version).records == ()


@pytest.mark.parametrize("counterpart_state", tuple(InformationState))
def test_e06_decision_that_does_not_determine_the_counterpart_is_not_applicable(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    counterpart_state: InformationState,
) -> None:
    # A stale policy pin makes E05 return before it inspects information
    # state, so the counterpart's state (panel B) would be the caller's choice.
    counterpart = _with(live.records[1], information_state=counterpart_state)
    e06_receipt = _e06_register(
        e06, live, counterpart_record=counterpart, expected_policy_sha256="f" * 64
    )
    resolved = e06.resolve(
        e06_receipt.selector_id,
        e06_receipt.source_version,
        expected_member_sha256=live.bindings[0].member_sha256,
        expected_result_id=live.bindings[0].result.result_id,
    )
    assert resolved.source.compatibility_decision.outcome is (
        CompatibilityOutcome.UNKNOWN
    )
    with pytest.raises(MeasurementSourceArtifactNotApplicable, match="determine"):
        _register(registry, live, e06_receipt, counterpart_record=counterpart)


def test_counterpart_information_state_is_bound_when_e05_inspects_it(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    # Subject sufficient, counterpart insufficient: E05 stops at the
    # information gate, where "insufficient" and "unknown" give the same
    # decision, so the counterpart is not determined and there is no artifact.
    counterpart = _with(live.records[1], information_state=InformationState.INSUFFICIENT)
    e06_receipt = _e06_register(e06, live, counterpart_record=counterpart)
    with pytest.raises(MeasurementSourceArtifactNotApplicable, match="determine"):
        _register(registry, live, e06_receipt, counterpart_record=counterpart)
    # Both sufficient: the comparable decision pins the counterpart exactly.
    receipt, _ = _registered(registry, e06, live)
    assert receipt.state_version == 1


def test_e06_source_without_the_e04_measurement_digest_is_not_applicable(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    asserted = _with(live.records[0], result_sha256="e" * 64)
    e06_receipt = _e06_register(e06, live, record=asserted)
    with pytest.raises(MeasurementSourceArtifactNotApplicable, match="measurement"):
        _register(registry, live, e06_receipt)


def test_e06_record_verbatim_cannot_build_an_e07_source(live: Live) -> None:
    # E06 binds the E05 bundle digest to the E04 bundle tree; E07 binds it to
    # the canonical E02 manifest.  This is why the registry substitutes E04's
    # bundle_manifest_sha256 for E07 and changes nothing else.
    bundle, _ = live.results.verify_reference(live.bindings[0].result)
    assert live.records[0].bundle_sha256 == live.bindings[0].result.bundle_sha256
    assert live.bindings[0].result.bundle_manifest_sha256 == hashlib.sha256(
        canonical_json_bytes(bundle.manifest)
    ).hexdigest()
    with pytest.raises(ValueError, match="bundle identity"):
        fragment_source_from_verified_bundle(
            bundle,
            record=live.records[0],
            quantity=FragmentQuantity.ALIGNED_REFERENCE_SPAN,
        )


def test_resolve_requires_the_expected_e06_source_and_selector(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    base = {
        "expected_e06_selector_id": e06_receipt.selector_id,
        "expected_e06_source_version": e06_receipt.source_version,
        "expected_member_sha256": live.bindings[0].member_sha256,
        "expected_result_id": live.bindings[0].result.result_id,
    }
    for name, value in (
        ("expected_e06_source_version", 2),
        ("expected_member_sha256", live.bindings[1].member_sha256),
        ("expected_result_id", live.bindings[1].result.result_id),
        ("expected_e06_selector_id", "e06_source_" + "0" * 40),
    ):
        with pytest.raises(MeasurementSourceArtifactRegistryConflict):
            registry.resolve(receipt.selector_id, **{**base, name: value})
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="unavailable"):
        registry.resolve("familysrc_artifact_" + "0" * 40, **base)
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="invalid"):
        registry.resolve("e06_source_" + "0" * 40, **base)


def test_result_key_revocation_makes_artifact_stale(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    live.trust.revoke(live.key.key_id)
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        _resolve(registry, live, receipt, e06_receipt)
    page = registry.list_selectors(live.selector_id, live.cohort_version)
    assert [row.authority_state for row in page.records] == [
        ArtifactAuthorityState.STALE
    ]
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        _register(registry, live, e06_receipt)


def test_trust_store_mutation_and_linkage_advance_make_artifact_stale(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    live.trust.add_signing_key(generate_development_keypair(KeyPurpose.RESULT))
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        _resolve(registry, live, receipt, e06_receipt)
    e06_tests._commit(live.store, "e")
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        registry.list_selectors(live.selector_id, live.cohort_version)


def _inject_after_e06_resolve(monkeypatch: pytest.MonkeyPatch, action) -> None:
    """Run ``action`` after the E06 resolve, before the D06 fence (test-only).

    In-process replacement is outside the threat model; the alias seal is
    updated with the wrapper so the injection reaches the derivation.
    """

    original = registry_module._MS_COHORT_FENCE
    calls = {"count": 0}

    def wrapped(self, cohort_selector_id, cohort_version):
        calls["count"] += 1
        if calls["count"] == 1:
            action()
        return original(self, cohort_selector_id, cohort_version)

    monkeypatch.setattr(registry_module, "_MS_COHORT_FENCE", wrapped)
    monkeypatch.setattr(
        registry_module,
        "_REGISTRY_ALIAS_SEAL",
        {**registry_module._REGISTRY_ALIAS_SEAL, "_MS_COHORT_FENCE": wrapped},
    )


def test_authority_change_between_e06_resolve_and_d06_fence_is_stale(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e06_receipt = _e06_register(e06, live)
    _inject_after_e06_resolve(
        monkeypatch,
        lambda: live.trust.add_signing_key(
            generate_development_keypair(KeyPurpose.RESULT)
        ),
    )
    with pytest.raises(MeasurementSourceArtifactRegistryStale, match="changed after"):
        _register(registry, live, e06_receipt)
    monkeypatch.undo()
    assert registry.list_selectors(live.selector_id, live.cohort_version).records == ()


def test_fence_composition_and_lock_order(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    with CohortRecordCatalog.record_status_authority_fence(
        live.cohorts, live.selector_id, live.cohort_version
    ):
        # E04 re-verification composes inside the D06 fence on this thread.
        bundle, _ = live.results.verify_reference(live.bindings[0].result)
        assert bundle.measurement.definition_id == "aligned-reference-span.v1"
        # D01 is not reentrant, so neither E06 nor this registry can resolve
        # inside a held D06 fence; both fail closed as stale.
        with pytest.raises(ProviderLinkageStoreUnsafe, match="idle connection"):
            with ProviderLinkageStore.authority_read_fence(live.store):
                pass
        with pytest.raises(ResultViewSourceRegistryStale):
            e06.resolve(
                e06_receipt.selector_id,
                e06_receipt.source_version,
                expected_member_sha256=live.bindings[0].member_sha256,
                expected_result_id=live.bindings[0].result.result_id,
            )
        with pytest.raises(MeasurementSourceArtifactRegistryStale):
            _resolve(registry, live, receipt, e06_receipt)
    assert _resolve(registry, live, receipt, e06_receipt).object_sha256 == (
        receipt.object_sha256
    )


def test_resolve_holds_d06_fence_through_exact_return(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    original_sha256 = hashlib.sha256
    started = threading.Event()
    finished = threading.Event()
    workers: list[threading.Thread] = []

    def sha256(content=b"", *args, **kwargs):
        if bytes(content).startswith(b"traceback-e12-family-source-replay-v1") and (
            not workers
        ):

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
    resolved = _resolve(registry, live, receipt, e06_receipt)
    assert resolved.object_sha256 == receipt.object_sha256
    assert workers
    workers[0].join(timeout=5)
    assert finished.is_set()
    monkeypatch.undo()
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        _resolve(registry, live, receipt, e06_receipt)


def test_selector_page_carries_only_digests_and_states(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    first, _ = _registered(registry, e06, live)
    second, _ = _registered(registry, e06, live, index=1)
    page = registry.list_selectors(live.selector_id, live.cohort_version, limit=1)
    assert len(page.records) == 1
    assert page.next_after_selector_id == page.records[0].selector_id
    rest = registry.list_selectors(
        live.selector_id,
        live.cohort_version,
        after_selector_id=page.next_after_selector_id,
    )
    assert rest.next_after_selector_id is None
    rows = page.records + rest.records
    assert {row.selector_id for row in rows} == {first.selector_id, second.selector_id}
    assert all(row.authority_state is ArtifactAuthorityState.CURRENT for row in rows)
    assert set(SourceArtifactSelectorRecord.model_fields) == {
        "schema_version",
        "selector_id",
        "family",
        "object_sha256",
        "artifact_sha256",
        "e06_source_sha256",
        "authority_state",
    }
    text = json.dumps(page.model_dump(mode="json"))
    for binding in live.bindings:
        assert binding.result.result_id not in text
        assert binding.member_sha256 not in text
        assert binding.result.bundle_sha256 not in text
    assert live.selector_id not in text
    other = "cohort_selector_" + "0" * 40
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        registry.list_selectors(other, live.cohort_version)
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="bound"):
        registry.list_selectors(live.selector_id, live.cohort_version, limit=101)


def test_object_tamper_and_extra_entries_fail_closed(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"count":2', b'"count":3', 1))
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="digest"):
        _resolve(registry, live, receipt, e06_receipt)
    path.write_bytes(original)
    assert _resolve(registry, live, receipt, e06_receipt).object_sha256 == (
        receipt.object_sha256
    )
    (registry.root / "objects" / "notes.txt").write_bytes(b"x")
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="invalid object"):
        _resolve(registry, live, receipt, e06_receipt)


def test_journal_rollback_and_deletion_fail_closed(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    first, e06_first = _registered(registry, e06, live)
    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    second, _ = _registered(registry, e06, live, index=1)
    after = journal.read_bytes()
    with open(journal, "r+b") as handle:
        handle.truncate(len(before))
    (registry.root / "objects" / f"{second.object_sha256}.json").unlink()
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="rollback"):
        _resolve(registry, live, first, e06_first)
    with open(journal, "r+b") as handle:
        handle.write(after)
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="inconsistent"):
        _resolve(registry, live, first, e06_first)


def test_reopen_requires_exact_identity_and_the_same_e06_registry(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    root = registry.root
    registry.close()
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="required"):
        MeasurementSourceArtifactRegistry(root, result_view_source_registry=e06)
    values = {
        "result_view_source_registry": e06,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="identity or head"):
        MeasurementSourceArtifactRegistry(
            root, **{**values, "expected_state_head_sha256": "0" * 64}
        )
    other_e06 = ResultViewSourceRegistry(live.root / "e06-other", record_catalog=live.cohorts)
    try:
        with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="E06 authority"):
            MeasurementSourceArtifactRegistry(
                root, **{**values, "result_view_source_registry": other_e06}
            )
    finally:
        other_e06.close()
    with pytest.raises(TypeError, match="exact E06"):
        MeasurementSourceArtifactRegistry(
            live.root / "typed", result_view_source_registry=object()
        )
    reopened = MeasurementSourceArtifactRegistry(root, **values)
    try:
        assert _resolve(reopened, live, receipt, e06_receipt).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()


def test_backup_restore_preserves_identity_and_reverifies(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    backup = registry.backup_bytes()
    assert measurement_source_artifact_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    values = {
        "result_view_source_registry": e06,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="expected head"):
        MeasurementSourceArtifactRegistry.restore(
            live.root / "bad",
            backup,
            **{**values, "expected_state_head_sha256": "0" * 64},
        )
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="invalid"):
        MeasurementSourceArtifactRegistry.restore(live.root / "trunc", backup[:-10], **values)
    assert not (live.root / "bad").exists()
    assert not (live.root / "trunc").exists()
    restored = MeasurementSourceArtifactRegistry.restore(
        live.root / "restored", backup, **values
    )
    try:
        assert _resolve(restored, live, receipt, e06_receipt).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()
    with pytest.raises(MeasurementSourceArtifactRegistryConflict, match="already exists"):
        MeasurementSourceArtifactRegistry.restore(live.root / "restored", backup, **values)


def _forged_backup(backup: bytes, transform) -> tuple[bytes, str]:
    """Rebuild a backup whose single object is replaced but self-consistent."""

    parsed = measurement_source_artifact_backup_from_bytes(backup)
    value = registered_artifact_object_from_bytes(parsed.objects[0].object_json.encode())
    forged = transform(value)
    content = registered_artifact_object_bytes(forged)
    digest = hashlib.sha256(content).hexdigest()
    entry = registry_module._build_journal_entry(
        sequence=1,
        previous_entry_sha256=registry_module._metadata_genesis_sha256(parsed.metadata),
        selector_id=parsed.journal[0].selector_id,
        object_sha256=digest,
        object_bytes=len(content),
    )
    rebuilt = registry_module.MeasurementSourceArtifactBackup(
        metadata=parsed.metadata,
        state_version=1,
        state_head_sha256=entry.entry_sha256,
        journal=(entry,),
        objects=(
            registry_module.MeasurementSourceArtifactBackupObject(
                object_sha256=digest, object_json=content.decode()
            ),
        ),
    )
    return registry_module._canonical_backup_bytes(rebuilt), entry.entry_sha256


def test_stored_object_that_live_derivation_does_not_reproduce_is_stale(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    backup, head = _forged_backup(
        registry.backup_bytes(),
        lambda value: value.model_copy(
            update={"counterpart_catalog_result_sha256": "d" * 64}
        ),
    )
    restored = MeasurementSourceArtifactRegistry.restore(
        live.root / "forged",
        backup,
        result_view_source_registry=e06,
        expected_registry_id=receipt.registry_id,
        expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
        expected_state_head_sha256=head,
    )
    try:
        with pytest.raises(MeasurementSourceArtifactRegistryStale, match="no longer"):
            _resolve(restored, live, receipt, e06_receipt)
        page = restored.list_selectors(live.selector_id, live.cohort_version)
        assert [row.authority_state for row in page.records] == [
            ArtifactAuthorityState.STALE
        ]
    finally:
        restored.close()


def test_object_controls_and_panels_are_fixed(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, _ = _registered(registry, e06, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    value = registered_artifact_object_from_bytes(content)
    swapped = value.model_dump(mode="json")
    swapped["counterpart_record"] = live.records[0].model_dump(mode="json")
    with pytest.raises(ValueError):
        RegisteredFragmentSourceArtifactObject.model_validate(swapped)
    narrowed = value.model_dump(mode="json")
    narrowed["artifact"]["state"]["left_controls"]["minimum_count_inclusive"] = 1
    with pytest.raises(ValueError):
        RegisteredFragmentSourceArtifactObject.model_validate(narrowed)


def test_failed_restore_and_failed_reopen_remove_the_target(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    receipt, e06_receipt = _registered(registry, e06, live)
    backup = registry.backup_bytes()
    values = {
        "result_view_source_registry": e06,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = live.root / "partial-restore"
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="restore failed"):
        MeasurementSourceArtifactRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()

    original_flock = fcntl.flock
    restored_lock = target / ".registry.lock"

    def failing_flock(descriptor, operation):
        if restored_lock.exists():
            lock = restored_lock.stat()
            bound = os.fstat(descriptor)
            if (bound.st_dev, bound.st_ino) == (lock.st_dev, lock.st_ino):
                raise OSError("lock unavailable")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="restore failed"):
        MeasurementSourceArtifactRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()
    restored = MeasurementSourceArtifactRegistry.restore(target, backup, **values)
    try:
        assert _resolve(restored, live, receipt, e06_receipt).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()


def test_torn_journal_append_is_truncated_and_registration_retries(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e06_receipt = _e06_register(e06, live)
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"family-source-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="append failed"):
        _register(registry, live, e06_receipt)
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""
    receipt = _register(registry, live, e06_receipt)
    assert receipt.state_version == 1
    assert _resolve(registry, live, receipt, e06_receipt).object_sha256 == (
        receipt.object_sha256
    )


def test_callable_shadows_and_pinned_replacements_are_rejected(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    object.__setattr__(registry, "resolve", lambda *args, **kwargs: None)
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="callable changed"):
        registry.resolve(receipt.selector_id)
    del registry.__dict__["resolve"]
    monkeypatch.setattr(
        MeasurementSourceArtifactRegistry, "_derive_in_fence", lambda *args, **kw: None
    )
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="callable changed"):
        _resolve(registry, live, receipt, e06_receipt)
    monkeypatch.undo()
    for module, name, replacement in (
        (e07_module, "build_fragment_explorer_view", lambda request: None),
        (e07_module, "replay_fragment_explorer_view", lambda request, view: view),
        (registry_module, "_MS_RESOLVED", RegisteredFragmentSourceArtifact.model_construct),
        (registry_module, "_MS_FRAGMENT_REQUEST", dict),
        (ResultViewSourceRegistry, "resolve", lambda *args, **kwargs: None),
    ):
        monkeypatch.setattr(module, name, replacement)
        with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="authority"):
            _resolve(registry, live, receipt, e06_receipt)
        monkeypatch.undo()
    original = registry.__dict__["_trusted_head_sha256"]
    registry.__dict__["_trusted_head_sha256"] = "0" * 64
    with pytest.raises(MeasurementSourceArtifactRegistryUnsafe, match="authority state"):
        _resolve(registry, live, receipt, e06_receipt)
    registry.__dict__["_trusted_head_sha256"] = original
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert _resolve(registry, live, receipt, e06_receipt).object_sha256 == (
        receipt.object_sha256
    )


def test_concurrent_resolve_and_revoke_terminate_with_exact_outcomes(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    receipt, e06_receipt = _registered(registry, e06, live)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def reader() -> None:
        for _ in range(3):
            try:
                _resolve(registry, live, receipt, e06_receipt)
                outcomes.append("current")
            except MeasurementSourceArtifactRegistryStale:
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
    assert len(outcomes) == 6
    with pytest.raises(MeasurementSourceArtifactRegistryStale):
        _resolve(registry, live, receipt, e06_receipt)


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live
) -> None:
    e06_receipt = _e06_register(e06, live)
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: _register(registry, live, e06_receipt),
        lambda values: MeasurementSourceArtifactRegistry(
            registry.root,
            result_view_source_registry=e06,
            **storage_checks.expected(values),
        ),
        MeasurementSourceArtifactRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e06_receipt = _e06_register(e06, live)
    storage_checks.check_append_interrupt_truncates(
        registry,
        registry_module,
        lambda: _register(registry, live, e06_receipt),
        monkeypatch,
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    registry: MeasurementSourceArtifactRegistry, e06, live: Live, tmp_path: Path
) -> None:
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, MeasurementSourceArtifactRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
) -> None:
    e06_receipt = _e06_register(e06, live)
    _register(registry, live, e06_receipt)
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: MeasurementSourceArtifactRegistry(
            registry.root,
            result_view_source_registry=e06,
            **storage_checks.expected(values),
        ),
        MeasurementSourceArtifactRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_checks.check_interrupted_creation(
        lambda root: MeasurementSourceArtifactRegistry(
            root, result_view_source_registry=e06
        ),
        lambda root, values: MeasurementSourceArtifactRegistry(
            root, result_view_source_registry=e06, **storage_checks.expected(values)
        ),
        tmp_path / "created-by-storage-check",
        registry_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    registry: MeasurementSourceArtifactRegistry,
    e06,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e06_receipt = _e06_register(e06, live)
    _register(registry, live, e06_receipt)
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: MeasurementSourceArtifactRegistry.restore(
            target,
            backup,
            result_view_source_registry=e06,
            **storage_checks.expected(values),
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )
