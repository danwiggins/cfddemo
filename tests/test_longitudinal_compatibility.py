"""D02 exact-provenance and pinned-anchor compatibility tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import (
    CompatibilityPolicyReference,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_key_sha256,
)
from evidence_inspector.longitudinal_compatibility import (
    ALL_COMPARISON_DIMENSIONS,
    MAX_DECISION_EVIDENCE,
    ComparisonDimension,
    ComparisonDimensionValue,
    DimensionAllowance,
    DimensionAnchorRule,
    DimensionValueState,
    LongitudinalAnchorPolicy,
    LongitudinalComparisonKey,
    LongitudinalMemberDecision,
    LongitudinalOutcome,
    LongitudinalReason,
    LongitudinalRecord,
    capability_sha256,
    comparison_dimension_value_sha256,
    composite_contract_sha256,
    decide_longitudinal_member,
    decide_longitudinal_series,
    longitudinal_anchor_policy_sha256,
    longitudinal_comparison_key_sha256,
    longitudinal_member_decision_sha256,
    longitudinal_record_sha256,
    measurement_definition_sha256,
    optional_contract_sha256,
    optional_text_sha256,
    provider_measurement_id,
    provider_projection_ref,
)
from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    DisplayRole,
    MethodDefinition,
    MethodFamily,
    QualificationState,
    ToolReference,
    canonical_contract_bytes,
    method_definition_sha256,
)
from evidence_inspector.provider_linkage import (
    BiologicalLineage,
    LinkageOperation,
    LinkageReasonCode,
    LinkageRevision,
    OptionalLineageState,
    OptionalOpaqueToken,
    TechnicalLineage,
    UnitOfAnalysis,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    ProviderLinkageStore,
)
from tests.test_provider_linkage import (
    PROVIDER,
    _consume,
    _create_approval,
    _token,
    _trust,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
HEAD_SHA256 = "8" * 64
REGISTRY_SHA256 = "7" * 64
TRUST_SHA256 = provider_trust_snapshot_sha256(_trust())


def _asset(name: str, digit: str) -> AssetReference:
    return AssetReference(
        asset_id=f"asset_{name}",
        version="1.0.0",
        content_sha256=digit * 64,
    )


ASSETS = tuple(
    sorted(
        (
            _asset("atlas_alpha", "1"),
            _asset("atlas_beta", "2"),
            _asset("grid_alpha", "3"),
            _asset("panel_alpha", "4"),
            _asset("reference_alpha", "5"),
            _asset("reference_beta", "6"),
        ),
        key=lambda item: (item.asset_id, item.version),
    )
)
ASSET = {item.asset_id: item for item in ASSETS}


def _method(*, alternate: bool = False) -> MethodDefinition:
    return MethodDefinition(
        method_id="mth_fragment_alpha",
        version="1.0.1" if alternate else "1.0.0",
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id="qty_fragment_fraction",
        unit="unit_fraction",
        parameter_schema_sha256=("f" if alternate else "9") * 64,
        tools=(
            ToolReference(
                tool_id="tool_fragment_alpha",
                version="1.0.0",
                artifact_sha256="a" * 64,
            ),
        ),
        assets=ASSETS,
    )


METHOD = _method()
METHOD_SHA256 = method_definition_sha256(METHOD)


def _capability(
    method: MethodDefinition,
    *,
    authority_head_sha256: str = HEAD_SHA256,
    authority_revision: int = 4,
) -> CurrentMethodCapability:
    return CurrentMethodCapability(
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=authority_head_sha256,
        authority_revision=authority_revision,
        method_definition_sha256=method_definition_sha256(method),
        method_ref=method.method_ref,
        authority_scope="scope_research_alpha",
        as_of=NOW,
        qualification_state=QualificationState.DEVELOPMENT_UNQUALIFIED,
        display_role=DisplayRole.RESEARCH_BASELINE,
        research_inspectable=True,
        current_provider_eligible=False,
        effective_approval_ref=None,
    )


def _measurement(
    result_digit: str,
    *,
    changed: ComparisonDimension | None = None,
    unknown: ComparisonDimension | None = None,
    authority_head_sha256: str = HEAD_SHA256,
    authority_revision: int = 4,
) -> VerifiedMeasurementRecord:
    method = _method(alternate=changed == ComparisonDimension.MEASUREMENT_DEFINITION)
    reference = ASSET["asset_reference_alpha"]
    atlas = ASSET["asset_atlas_alpha"]
    grid = ASSET["asset_grid_alpha"]
    panel = ASSET["asset_panel_alpha"]
    normalization = "sem_normalization_alpha"
    coordinate = "sem_coordinate_alpha"
    denominator = "sem_denominator_alpha"
    result_schema = ResultSchemaReference(
        schema_id="schema_fragment_alpha", version="1.0.0"
    )
    if changed == ComparisonDimension.REFERENCE:
        reference = ASSET["asset_reference_beta"]
    elif changed == ComparisonDimension.ATLAS_MARKER_SET:
        atlas = ASSET["asset_atlas_beta"]
    elif changed == ComparisonDimension.FILTER_QC_POLICY:
        normalization = "sem_normalization_beta"
    elif changed == ComparisonDimension.COORDINATE_SEMANTICS:
        coordinate = "sem_coordinate_beta"
    elif changed == ComparisonDimension.DENOMINATOR_SEMANTICS:
        denominator = "sem_denominator_beta"
    elif changed == ComparisonDimension.RESULT_SCHEMA:
        result_schema = ResultSchemaReference(
            schema_id="schema_fragment_alpha", version="1.0.1"
        )
    if unknown == ComparisonDimension.REFERENCE:
        reference = None
    elif unknown == ComparisonDimension.ATLAS_MARKER_SET:
        atlas = grid = panel = None
    elif unknown == ComparisonDimension.FILTER_QC_POLICY:
        normalization = None
    elif unknown == ComparisonDimension.COORDINATE_SEMANTICS:
        coordinate = None
    elif unknown == ComparisonDimension.DENOMINATOR_SEMANTICS:
        denominator = None
    return VerifiedMeasurementRecord(
        result_id=f"result_{result_digit * 16}",
        result_sha256=result_digit * 64,
        bundle_id=f"bundle_{result_digit * 16}",
        bundle_sha256=("b" if result_digit == "a" else "c") * 64,
        method=method,
        method_definition_sha256=method_definition_sha256(method),
        current_capability=_capability(
            method,
            authority_head_sha256=authority_head_sha256,
            authority_revision=authority_revision,
        ),
        execution_state=ExecutionState.COMPLETE,
        information_state=InformationState.SUFFICIENT,
        trust_state=TrustState.VERIFIED,
        compatibility_key=MeasurementCompatibilityKey(
            measurement_family=method.family,
            quantity_id=method.quantity_id,
            unit=method.unit,
            result_schema=result_schema,
            reference_asset=reference,
            grid_asset=grid,
            atlas_asset=atlas,
            panel_asset=panel,
            normalization_semantics_id=normalization,
            coordinate_semantics_id=coordinate,
            denominator_semantics_id=denominator,
            registered_policy=CompatibilityPolicyReference(
                policy_id="policy_fragment_alpha", version="1.0.0"
            ),
        ),
    )


def _overlap_digest(
    measurement: VerifiedMeasurementRecord,
    dimension: ComparisonDimension,
) -> str | None:
    e05 = measurement.compatibility_key
    return {
        ComparisonDimension.REFERENCE: optional_contract_sha256(e05.reference_asset),
        ComparisonDimension.ATLAS_MARKER_SET: composite_contract_sha256(
            e05.atlas_asset, e05.grid_asset, e05.panel_asset
        ),
        ComparisonDimension.FILTER_QC_POLICY: optional_text_sha256(
            e05.normalization_semantics_id
        ),
        ComparisonDimension.COORDINATE_SEMANTICS: optional_text_sha256(
            e05.coordinate_semantics_id
        ),
        ComparisonDimension.DENOMINATOR_SEMANTICS: optional_text_sha256(
            e05.denominator_semantics_id
        ),
        ComparisonDimension.RESULT_SCHEMA: hashlib.sha256(
            canonical_contract_bytes(e05.result_schema)
        ).hexdigest(),
    }.get(dimension)


def _key(
    measurement: VerifiedMeasurementRecord,
    *,
    changed: ComparisonDimension | None = None,
    unknown: ComparisonDimension | None = None,
    change_digit: str = "2",
) -> LongitudinalComparisonKey:
    dimensions = []
    overlap = {
        ComparisonDimension.REFERENCE,
        ComparisonDimension.ATLAS_MARKER_SET,
        ComparisonDimension.FILTER_QC_POLICY,
        ComparisonDimension.COORDINATE_SEMANTICS,
        ComparisonDimension.DENOMINATOR_SEMANTICS,
        ComparisonDimension.RESULT_SCHEMA,
    }
    for dimension in ALL_COMPARISON_DIMENSIONS:
        if dimension == ComparisonDimension.MEASUREMENT_DEFINITION:
            digest = measurement_definition_sha256(
                measurement.method.method_ref,
                measurement.method_definition_sha256,
                measurement.method.quantity_id,
                measurement.method.unit,
            )
        elif dimension in overlap:
            digest = _overlap_digest(measurement, dimension)
        elif dimension == unknown:
            digest = None
        else:
            digest = (change_digit if dimension == changed else "1") * 64
        if digest is None:
            dimensions.append(
                ComparisonDimensionValue(
                    dimension=dimension,
                    state=DimensionValueState.UNKNOWN,
                )
            )
        else:
            dimensions.append(
                ComparisonDimensionValue(
                    dimension=dimension,
                    state=DimensionValueState.KNOWN,
                    identity_id=(
                        f"cmpid_{dimension.value}_"
                        f"{change_digit if dimension == changed else '1'}"
                    ),
                    version="1.0.0",
                    content_sha256=digest,
                )
            )
    capability = measurement.current_capability
    return LongitudinalComparisonKey(
        result_id=measurement.result_id,
        result_sha256=measurement.result_sha256,
        bundle_id=measurement.bundle_id,
        bundle_sha256=measurement.bundle_sha256,
        e05_compatibility_key_sha256=compatibility_key_sha256(
            measurement.compatibility_key
        ),
        method_ref=measurement.method.method_ref,
        method_definition_sha256=measurement.method_definition_sha256,
        quantity_id=measurement.method.quantity_id,
        unit=measurement.method.unit,
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        capability_sha256=capability_sha256(capability),
        dimensions=tuple(dimensions),
    )


def _unknown_link() -> OptionalOpaqueToken:
    return OptionalOpaqueToken(state=OptionalLineageState.UNKNOWN, token=None)


def _record(
    result_digit: str,
    *,
    changed: ComparisonDimension | None = None,
    unknown: ComparisonDimension | None = None,
    subject_digit: str = "1",
    authorized: bool = True,
    authority_head_sha256: str = HEAD_SHA256,
    authority_revision: int = 4,
) -> LongitudinalRecord:
    measurement = _measurement(
        result_digit,
        changed=changed,
        unknown=unknown,
        authority_head_sha256=authority_head_sha256,
        authority_revision=authority_revision,
    )
    revision = LinkageRevision(
        linkage_id=_token("linkage", result_digit),
        provider_namespace=PROVIDER,
        revision=1,
        previous_revision_sha256=None,
        operation=LinkageOperation.CREATE,
        reason_code=LinkageReasonCode.INITIAL_PROJECTION,
        source_projection_ref=provider_projection_ref(measurement),
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
        biological=BiologicalLineage(
            subject_token=_token("subject", subject_digit),
            collection_token=_token("collection", result_digit),
            specimen_token=_token("specimen", result_digit),
            aliquot=_unknown_link(),
        ),
        technical=TechnicalLineage(
            run=_unknown_link(),
            analysis_record_id=_token("analysis", result_digit),
            measurement_id=provider_measurement_id(measurement),
            reanalysis_of=_unknown_link(),
        ),
        proposed_at=NOW,
    )
    authorized_linkage = None
    if authorized:
        authorized_linkage, _ = _consume(
            revision,
            (_create_approval(revision, result_digit),),
        )
    return LongitudinalRecord(
        measurement=measurement,
        comparison_key=_key(
            measurement,
            changed=changed,
            unknown=unknown,
            change_digit=result_digit,
        ),
        linkage_revision=revision,
        authorized_linkage=authorized_linkage,
        activation_receipt=None,
    )


def _policy(
    anchor: LongitudinalRecord,
    allowances: dict[
        ComparisonDimension, tuple[ComparisonDimensionValue, LongitudinalOutcome]
    ]
    | None = None,
) -> LongitudinalAnchorPolicy:
    allowances = allowances or {}
    rules = []
    for value in anchor.comparison_key.dimensions:
        allowed = ()
        if value.dimension in allowances:
            member_value, outcome = allowances[value.dimension]
            allowed = (
                DimensionAllowance(
                    member_value_sha256=comparison_dimension_value_sha256(member_value),
                    outcome=outcome,
                    evidence_ref=f"evidence_{value.dimension.value}_alpha",
                    evidence_sha256="a" * 64,
                    bridge_ref=(
                        f"bridge_{value.dimension.value}_alpha"
                        if outcome == LongitudinalOutcome.REGISTERED_BRIDGE
                        else None
                    ),
                ),
            )
        rules.append(
            DimensionAnchorRule(
                dimension=value.dimension,
                anchor_value_sha256=comparison_dimension_value_sha256(value),
                allowances=allowed,
            )
        )
    return LongitudinalAnchorPolicy(
        policy_id="longpolicy_fragment_alpha",
        version="1.0.0",
        engine_version="1.0.0",
        anchor_key_sha256=longitudinal_comparison_key_sha256(anchor.comparison_key),
        rules=tuple(rules),
    )


def _decide(
    anchor: LongitudinalRecord,
    member: LongitudinalRecord,
    policy: LongitudinalAnchorPolicy,
    *,
    expected_policy: str | None = None,
    expected_head: str = HEAD_SHA256,
):
    with _activated_records(anchor, member) as (records, store):
        return decide_longitudinal_member(
            records[0],
            records[1],
            policy,
            expected_policy_sha256=(
                expected_policy or longitudinal_anchor_policy_sha256(policy)
            ),
            expected_authority_head_sha256=expected_head,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )


@contextmanager
def _activated_records(
    *records: LongitudinalRecord,
) -> Iterator[tuple[tuple[LongitudinalRecord, ...], ProviderLinkageStore]]:
    with TemporaryDirectory(prefix="traceback-linkage-test-") as directory:
        store = ProviderLinkageStore(
            Path(directory),
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            clock=lambda: NOW,
        )
        try:
            for record in records:
                if record.authorized_linkage is not None:
                    store.commit_authorized_revision(record.authorized_linkage)
            receipts = {
                (item.provider_namespace, item.linkage_id, item.revision): item
                for item in store.active_snapshot().receipts
            }
            activated = tuple(
                record.model_copy(
                    update={
                        "activation_receipt": receipts.get(
                            (
                                record.linkage_revision.provider_namespace,
                                record.linkage_revision.linkage_id,
                                record.linkage_revision.revision,
                            )
                        )
                    }
                )
                for record in records
            )
            yield activated, store
        finally:
            store.close()


def test_exact_complete_key_is_equivalent_and_replay_bound() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    assert decision.outcome == LongitudinalOutcome.EQUIVALENT
    assert decision.reason_codes == (LongitudinalReason.EXACT_MATCH,)
    assert decision.delta_allowed and decision.connecting_trend_allowed
    assert decision.anchor_result_sha256 == anchor.measurement.result_sha256
    assert decision.member_bundle_sha256 == member.measurement.bundle_sha256
    assert decision.anchor_linkage_receipt_sha256 is not None
    assert longitudinal_member_decision_sha256(decision)


@pytest.mark.parametrize("dimension", ALL_COMPARISON_DIMENSIONS)
def test_every_unregistered_dimension_mismatch_is_incompatible(
    dimension: ComparisonDimension,
) -> None:
    anchor = _record("1")
    member = _record("2", changed=dimension)
    decision = _decide(anchor, member, _policy(anchor))

    assert decision.outcome == LongitudinalOutcome.INCOMPATIBLE
    assert dimension in decision.mismatch_dimensions
    assert not decision.delta_allowed
    assert not decision.connecting_trend_allowed


@pytest.mark.parametrize(
    "outcome",
    (
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
        LongitudinalOutcome.REQUIRES_REANALYSIS,
        LongitudinalOutcome.REGISTERED_BRIDGE,
    ),
)
def test_registered_allowance_has_exact_rendering_semantics(
    outcome: LongitudinalOutcome,
) -> None:
    dimension = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    member = _record("2", changed=dimension)
    changed = member.comparison_key.dimensions[
        ALL_COMPARISON_DIMENSIONS.index(dimension)
    ]
    decision = _decide(
        anchor,
        member,
        _policy(anchor, {dimension: (changed, outcome)}),
    )

    assert decision.outcome == outcome
    assert decision.delta_allowed == (
        outcome == LongitudinalOutcome.QUALIFIED_COMPATIBLE
    )
    assert decision.connecting_trend_allowed == decision.delta_allowed
    assert bool(decision.bridge_refs) == (
        outcome == LongitudinalOutcome.REGISTERED_BRIDGE
    )


def test_unknown_absent_linkage_and_stale_authority_suppress_rendering() -> None:
    anchor = _record("1")
    policy = _policy(anchor)
    unknown = _record("2", unknown=ComparisonDimension.UNCERTAINTY_METHOD)
    unknown_decision = _decide(anchor, unknown, policy)
    assert unknown_decision.outcome == LongitudinalOutcome.UNKNOWN
    assert not unknown_decision.connecting_trend_allowed

    unauthorized = _record("2", authorized=False)
    authority_decision = _decide(anchor, unauthorized, policy)
    assert authority_decision.outcome == LongitudinalOutcome.UNKNOWN
    assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in (
        authority_decision.reason_codes
    )

    stale = _record("2", authority_head_sha256="f" * 64, authority_revision=5)
    stale_decision = _decide(anchor, stale, policy)
    assert stale_decision.outcome == LongitudinalOutcome.UNKNOWN
    assert LongitudinalReason.RESULT_STATE_INVALID in stale_decision.reason_codes
    assert stale_decision.member_linkage_receipt_sha256 is not None
    assert stale_decision.member_record_sha256 != longitudinal_record_sha256(stale)
    assert stale_decision.member_record_sha256 != longitudinal_record_sha256(unknown)


def test_live_store_receipts_are_required_and_replayed(tmp_path: Path) -> None:
    anchor = _record("1")
    member = _record("2")
    assert anchor.authorized_linkage is not None
    assert member.authorized_linkage is not None
    store = ProviderLinkageStore(
        tmp_path / "protected",
        expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
        clock=lambda: NOW,
    )
    try:
        store.commit_authorized_revision(anchor.authorized_linkage)
        store.commit_authorized_revision(member.authorized_linkage)
        snapshot = store.active_snapshot()
        receipts = {
            (item.provider_namespace, item.linkage_id): item
            for item in snapshot.receipts
        }
        anchor = anchor.model_copy(
            update={
                "activation_receipt": receipts[
                    (
                        anchor.linkage_revision.provider_namespace,
                        anchor.linkage_revision.linkage_id,
                    )
                ]
            }
        )
        member = member.model_copy(
            update={
                "activation_receipt": receipts[
                    (
                        member.linkage_revision.provider_namespace,
                        member.linkage_revision.linkage_id,
                    )
                ]
            }
        )
        policy = _policy(anchor)
        decision = decide_longitudinal_member(
            anchor,
            member,
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
        assert decision.outcome == LongitudinalOutcome.EQUIVALENT

        no_verifier = decide_longitudinal_member(
            anchor,
            member,
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=None,
        )
        assert no_verifier.outcome == LongitudinalOutcome.UNKNOWN
        assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in (
            no_verifier.reason_codes
        )

        third = _record("3")
        assert third.authorized_linkage is not None
        store.commit_authorized_revision(third.authorized_linkage)
        stale = decide_longitudinal_member(
            anchor,
            member,
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
        assert stale.outcome == LongitudinalOutcome.UNKNOWN
        assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in stale.reason_codes
    finally:
        store.close()


def test_fake_or_cross_store_verifier_cannot_enable_comparison(
    tmp_path: Path,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)

    class AcceptAll:
        @staticmethod
        def verify_current_receipt(_receipt) -> None:
            return None

    class AcceptAllSubclass(ProviderLinkageStore):
        def verify_current_receipt(self, _receipt) -> None:
            return None

    with _activated_records(anchor, member) as (records, _source_store):
        fake_verifiers = (
            AcceptAll(),
            object.__new__(AcceptAllSubclass),
        )
        for fake_verifier in fake_verifiers:
            fake = decide_longitudinal_member(
                records[0],
                records[1],
                policy,
                expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
                expected_authority_head_sha256=HEAD_SHA256,
                expected_linkage_trust_snapshot_sha256_by_provider={
                    PROVIDER: TRUST_SHA256
                },
                linkage_store=fake_verifier,  # type: ignore[arg-type]
            )
            assert fake.outcome == LongitudinalOutcome.UNKNOWN
            assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in fake.reason_codes

        other_store = ProviderLinkageStore(
            tmp_path / "other-store",
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            clock=lambda: NOW,
        )
        try:
            assert records[0].authorized_linkage is not None
            assert records[1].authorized_linkage is not None
            other_store.commit_authorized_revision(records[0].authorized_linkage)
            other_store.commit_authorized_revision(records[1].authorized_linkage)
            cross_store = decide_longitudinal_member(
                records[0],
                records[1],
                policy,
                expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
                expected_authority_head_sha256=HEAD_SHA256,
                expected_linkage_trust_snapshot_sha256_by_provider={
                    PROVIDER: TRUST_SHA256
                },
                linkage_store=other_store,
            )
            assert cross_store.outcome == LongitudinalOutcome.UNKNOWN
            assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in (
                cross_store.reason_codes
            )
        finally:
            other_store.close()


def test_exact_e05_result_bundle_and_overlapping_key_cannot_drift() -> None:
    record = _record("1")
    changed_measurement = record.measurement.model_copy(
        update={"result_sha256": "f" * 64}
    )
    with pytest.raises(ValidationError, match="exact E05 result"):
        LongitudinalRecord(
            measurement=changed_measurement,
            comparison_key=record.comparison_key,
            linkage_revision=record.linkage_revision,
            authorized_linkage=record.authorized_linkage,
        )

    changed_e05_key = record.measurement.compatibility_key.model_copy(
        update={
            "result_schema": ResultSchemaReference(
                schema_id="schema_fragment_alpha", version="1.0.1"
            )
        }
    )
    changed_measurement = record.measurement.model_copy(
        update={"compatibility_key": changed_e05_key}
    )
    changed_key = record.comparison_key.model_copy(
        update={
            "e05_compatibility_key_sha256": compatibility_key_sha256(changed_e05_key)
        }
    )
    with pytest.raises(ValidationError, match="conflicts with E05 identity"):
        LongitudinalRecord(
            measurement=changed_measurement,
            comparison_key=changed_key,
            linkage_revision=record.linkage_revision,
            authorized_linkage=record.authorized_linkage,
        )


def test_provider_linkage_must_bind_measurement_and_authority_projection() -> None:
    record = _record("1")
    wrong_technical = record.linkage_revision.technical.model_copy(
        update={"measurement_id": _token("measurement", "f")}
    )
    wrong_revision = record.linkage_revision.model_copy(
        update={"technical": wrong_technical}
    )
    with pytest.raises(ValidationError, match="does not bind E05 result"):
        LongitudinalRecord(
            measurement=record.measurement,
            comparison_key=record.comparison_key,
            linkage_revision=wrong_revision,
            authorized_linkage=None,
        )

    wrong_revision = record.linkage_revision.model_copy(
        update={"source_projection_ref": _token("projection", "f")}
    )
    with pytest.raises(ValidationError, match="does not bind E01 authority"):
        LongitudinalRecord(
            measurement=record.measurement,
            comparison_key=record.comparison_key,
            linkage_revision=wrong_revision,
            authorized_linkage=None,
        )


def test_activation_receipt_binds_exact_signed_linkage_proof() -> None:
    record = _record("1")
    with _activated_records(record) as (activated, _):
        current = activated[0]
        alternate, _ = _consume(
            record.linkage_revision,
            (_create_approval(record.linkage_revision, "f"),),
        )
        payload = current.model_dump(mode="python")
        payload["authorized_linkage"] = alternate
        with pytest.raises(ValidationError, match="exact signed linkage proof"):
            LongitudinalRecord.model_validate(payload)


@pytest.mark.parametrize(
    "shadowed_method", ("verify_current_receipt", "active_snapshot")
)
def test_exact_store_instance_method_shadow_cannot_bypass_authority(
    shadowed_method: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        setattr(store, shadowed_method, lambda *_: None)
        decision = decide_longitudinal_member(
            records[0],
            records[1],
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )

    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    assert not decision.delta_allowed
    assert not decision.connecting_trend_allowed
    assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in decision.reason_codes


@pytest.mark.parametrize(
    "shadowed_method", ("verify_current_receipt", "active_snapshot")
)
def test_store_class_method_shadow_cannot_bypass_authority(
    monkeypatch: pytest.MonkeyPatch,
    shadowed_method: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        monkeypatch.setattr(ProviderLinkageStore, shadowed_method, lambda *_: None)
        decision = decide_longitudinal_member(
            records[0],
            records[1],
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )

    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    assert not decision.delta_allowed
    assert not decision.connecting_trend_allowed
    assert LongitudinalReason.LINKAGE_AUTHORITY_INVALID in decision.reason_codes


@pytest.mark.parametrize("target", ("anchor", "member"))
def test_truncated_dimensions_return_unknown_without_strict_zip_failure(
    target: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        selected = active_anchor if target == "anchor" else active_member
        truncated = selected.model_copy(
            update={
                "comparison_key": selected.comparison_key.model_copy(
                    update={"dimensions": selected.comparison_key.dimensions[:-1]}
                )
            }
        )
        decision = decide_longitudinal_member(
            truncated if target == "anchor" else active_anchor,
            truncated if target == "member" else active_member,
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )

    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    assert decision.unknown_dimensions == tuple(
        sorted(ALL_COMPARISON_DIMENSIONS, key=str)
    )
    assert not decision.delta_allowed
    assert not decision.connecting_trend_allowed


def test_decision_boundary_replays_every_nested_record_binding() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        measurement = active_member.measurement
        alternate_proof, _ = _consume(
            active_member.linkage_revision,
            (_create_approval(active_member.linkage_revision, "f"),),
        )
        mutations = (
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={"result_sha256": "f" * 64}
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={"bundle_sha256": "f" * 64}
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "comparison_key": active_member.comparison_key.model_copy(
                        update={"result_sha256": "f" * 64}
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={"method": _method(alternate=True)}
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={
                            "method": measurement.method.model_copy(
                                update={"quantity_id": "qty_fragment_count"}
                            )
                        }
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={
                            "method": measurement.method.model_copy(
                                update={"unit": "unit_count"}
                            )
                        }
                    )
                }
            ),
            active_member.model_copy(
                update={
                    "measurement": measurement.model_copy(
                        update={
                            "current_capability": measurement.current_capability.model_copy(
                                update={"authority_head_sha256": "f" * 64}
                            )
                        }
                    )
                }
            ),
            active_member.model_copy(update={"authorized_linkage": alternate_proof}),
        )
        decisions = tuple(
            decide_longitudinal_member(
                active_anchor,
                mutated,
                policy,
                expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
                expected_authority_head_sha256=HEAD_SHA256,
                expected_linkage_trust_snapshot_sha256_by_provider={
                    PROVIDER: TRUST_SHA256
                },
                linkage_store=store,
            )
            for mutated in mutations
        )

    assert all(item.outcome == LongitudinalOutcome.UNKNOWN for item in decisions)
    assert all(not item.delta_allowed for item in decisions)
    assert all(not item.connecting_trend_allowed for item in decisions)
    assert all(
        LongitudinalReason.RESULT_STATE_INVALID in item.reason_codes
        for item in decisions
    )


def test_stale_policy_wrong_subject_and_mixed_dispositions_fail_closed() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    stale = _decide(anchor, member, policy, expected_policy="0" * 64)
    assert stale.outcome == LongitudinalOutcome.UNKNOWN

    wrong_subject = _record("2", subject_digit="f")
    mismatch = _decide(anchor, wrong_subject, policy)
    assert mismatch.outcome == LongitudinalOutcome.INCOMPATIBLE
    assert not mismatch.connecting_trend_allowed

    first = ComparisonDimension.ASSAY_PROTOCOL
    second = ComparisonDimension.PREANALYTICS_POLICY
    mixed_member = _record("2", changed=first)
    values = list(mixed_member.comparison_key.dimensions)
    second_index = ALL_COMPARISON_DIMENSIONS.index(second)
    values[second_index] = ComparisonDimensionValue(
        dimension=second,
        state=DimensionValueState.KNOWN,
        identity_id="cmpid_preanalytics_policy_2",
        version="1.0.0",
        content_sha256="2" * 64,
    )
    mixed_key = mixed_member.comparison_key.model_copy(
        update={"dimensions": tuple(values)}
    )
    mixed_member = mixed_member.model_copy(update={"comparison_key": mixed_key})
    mixed_policy = _policy(
        anchor,
        {
            first: (values[0], LongitudinalOutcome.QUALIFIED_COMPATIBLE),
            second: (
                values[second_index],
                LongitudinalOutcome.REQUIRES_REANALYSIS,
            ),
        },
    )
    decision = _decide(anchor, mixed_member, mixed_policy)
    assert decision.outcome == LongitudinalOutcome.INCOMPATIBLE
    assert LongitudinalReason.MIXED_DISPOSITIONS in decision.reason_codes


def test_series_uses_one_anchor_and_seals_every_decision_digest() -> None:
    dimension = ComparisonDimension.ASSAY_PROTOCOL
    anchor = _record("1")
    middle = _record("2", changed=dimension)
    last = _record("3", changed=dimension)
    middle_value = middle.comparison_key.dimensions[0]
    policy = _policy(
        anchor,
        {
            dimension: (
                middle_value,
                LongitudinalOutcome.QUALIFIED_COMPATIBLE,
            )
        },
    )
    with _activated_records(anchor, middle, last) as (records, store):
        series = decide_longitudinal_series(
            records[0],
            (records[1], records[2]),
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
    assert series.decisions[0].outcome == LongitudinalOutcome.QUALIFIED_COMPATIBLE
    assert series.decisions[1].outcome == LongitudinalOutcome.INCOMPATIBLE
    assert series.decision_sha256s == tuple(
        longitudinal_member_decision_sha256(item) for item in series.decisions
    )
    with pytest.raises(ValidationError, match="digests"):
        series.__class__(
            **{
                **series.model_dump(),
                "decision_sha256s": ("0" * 64,) * len(series.decisions),
            }
        )


def test_decision_semantics_bounds_and_private_tokens_fail_closed() -> None:
    anchor = _record("1")
    member = _record("2")
    decision = _decide(anchor, member, _policy(anchor))
    payload = decision.model_dump(mode="json")
    payload["mismatch_dimensions"] = [ComparisonDimension.ASSAY_PROTOCOL.value]
    with pytest.raises(ValidationError, match="exact identity match"):
        LongitudinalMemberDecision.model_validate_json(json.dumps(payload))

    payload = decision.model_dump(mode="json")
    payload["evidence_refs"] = [
        f"evidence_alpha_{index}" for index in range(MAX_DECISION_EVIDENCE + 1)
    ]
    with pytest.raises(ValidationError):
        LongitudinalMemberDecision.model_validate_json(json.dumps(payload))

    key_payload = anchor.comparison_key.model_dump(mode="json")
    key_payload["dimensions"][0]["identity_id"] = "cmpid_patient_name"
    with pytest.raises(ValidationError, match="reserved privacy term"):
        LongitudinalComparisonKey.model_validate_json(json.dumps(key_payload))


def test_key_policy_order_and_measurement_definition_cannot_be_relabelled() -> None:
    key_payload = _record("1").comparison_key.model_dump(mode="json")
    key_payload["dimensions"] = key_payload["dimensions"][:-1]
    with pytest.raises(ValidationError, match="dimensions"):
        LongitudinalComparisonKey.model_validate_json(json.dumps(key_payload))

    anchor = _record("1")
    policy_payload = _policy(anchor).model_dump(mode="json")
    policy_payload["rules"] = list(reversed(policy_payload["rules"]))
    with pytest.raises(ValidationError, match="every dimension in order"):
        LongitudinalAnchorPolicy.model_validate_json(json.dumps(policy_payload))

    key_payload = anchor.comparison_key.model_dump(mode="json")
    index = ALL_COMPARISON_DIMENSIONS.index(ComparisonDimension.MEASUREMENT_DEFINITION)
    key_payload["dimensions"][index]["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="bind exact method and quantity"):
        LongitudinalComparisonKey.model_validate_json(json.dumps(key_payload))
