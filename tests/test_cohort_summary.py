"""D09 denominator and missingness contract tests with synthetic inputs only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import evidence_inspector.cohort_summary as cohort_summary_module
from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MeasurementAnchor,
    MemberLineageRole,
    PolicyDigests,
    ProviderAuthorityReference,
    ReanalysisRule,
    TechnicalReplicateRule,
    TimeAxis,
    TimeAxisKind,
    biological_timepoint_id,
    collection_event_reference_sha256,
    time_origin_authority_sha256,
)
from evidence_inspector.cohort_summary import (
    CohortDenominatorPolicy,
    CohortDenominatorSummary,
    CohortMemberEvidence,
    CohortSummaryState,
    MemberDisposition,
    MemberDispositionReason,
    RegisteredCohortDenominatorSummary,
    build_cohort_denominator_summary,
    build_registered_cohort_denominator_summary,
    cohort_denominator_summary_bytes,
    cohort_denominator_summary_from_bytes,
    registered_cohort_denominator_summary_bytes,
    registered_cohort_denominator_summary_from_bytes,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityPolicyReference,
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    MeasurementCompatibilityPolicy,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
    decide_compatibility,
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
    UnitOfAnalysis,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.result_view import (
    AttritionReason,
    AttritionStage,
    CountState,
    CountValue,
    DenominatorLedger,
    bind_result_view_source,
)
from tests.test_cohort_import import _import as _import_cohort_record
from tests.test_cohort_import import _setup as _setup_cohort_records
from tests.test_cohort_manifest import _collection_event, _trust
from tests.test_cohort_manifest import live as _cohort_live
from tests.test_provider_linkage_store import _pins

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
HEAD = "a" * 64
REGISTRY = "b" * 64
POLICIES = PolicyDigests(
    inclusion_sha256="1" * 64,
    exclusion_sha256="2" * 64,
    missingness_sha256="3" * 64,
)


def _token(prefix: str, digit: str) -> str:
    return f"{prefix}_{digit * 32}"


def _domain_sha256(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(domain + b"\0" + encoded).hexdigest()


TIME_AXIS = TimeAxis(
    kind=TimeAxisKind.COLLECTION_TIME,
    definition_sha256="4" * 64,
    unit_sha256="5" * 64,
    origin_authority_sha256=time_origin_authority_sha256(()),
)


def _member(digit: str, time_coordinate: int) -> CohortMember:
    collection = _token("collection", digit)
    event = _collection_event(
        provider_namespace=_token("provider", "a"),
        subject_token=_token("subject", "a"),
        collection_token=collection,
        collected_at=datetime.fromtimestamp(time_coordinate, tz=UTC),
    )
    event_sha256 = collection_event_reference_sha256(event)
    timepoint_id = biological_timepoint_id(event)
    coordinate_sha256 = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": collection,
            "collection_event_sha256": event_sha256,
            "biological_timepoint_id": timepoint_id,
            "time_axis": TIME_AXIS.model_dump(mode="json"),
            "time_coordinate": time_coordinate,
        },
    )
    return CohortMember(
        provider_namespace=_token("provider", "a"),
        linkage_id=_token("linkage", digit),
        linkage_revision=1,
        linkage_revision_sha256=digit * 64,
        committed_receipt_sha256=hex((int(digit, 16) + 1) % 16)[2:] * 64,
        subject_token=_token("subject", "a"),
        collection_token=collection,
        specimen_token=_token("specimen", digit),
        analysis_record_id=_token("analysis", digit),
        run_token=_token("run", digit),
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        analysis_unit_token=collection,
        denominator_contribution=True,
        collection_event_sha256=event_sha256,
        biological_timepoint_id=timepoint_id,
        time_coordinate=time_coordinate,
        time_coordinate_sha256=coordinate_sha256,
    )


def _manifest(*members: CohortMember) -> CohortManifest:
    provider_namespace = _token("provider", "a")
    trust = _trust().model_copy(update={"provider_namespace": provider_namespace})
    events = tuple(
        _collection_event(
            provider_namespace=member.provider_namespace,
            subject_token=member.subject_token,
            collection_token=member.collection_token,
            collected_at=datetime.fromtimestamp(member.time_coordinate, tz=UTC),
            trust=trust,
        )
        for member in {
            (
                item.provider_namespace,
                item.subject_token,
                item.collection_token,
            ): item
            for item in members
        }.values()
    )
    return CohortManifest(
        cohort_id="cohort_" + "f" * 32,
        version=1,
        previous_manifest_sha256=None,
        created_at=NOW,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
        technical_replicate_rule=TechnicalReplicateRule.COLLAPSE,
        reanalysis_rule=ReanalysisRule.COLLAPSE_TO_SOURCE,
        time_axis=TIME_AXIS,
        policies=POLICIES,
        measurement_anchor=MeasurementAnchor(
            measurement_definition_sha256="7" * 64,
            anchor_definition_sha256="8" * 64,
            authority_sha256="9" * 64,
        ),
        provider_authorities=(
            ProviderAuthorityReference(
                provider_namespace=provider_namespace,
                trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
                trust_snapshot_json=canonical_contract_bytes(trust).decode("utf-8"),
                store_id="store_" + "b" * 32,
                store_epoch_sha256="c" * 64,
                storage_identity_sha256="d" * 64,
                trust_pins_sha256="e" * 64,
                state_version=1,
                state_head_sha256="f" * 64,
            ),
        ),
        collection_events=tuple(
            sorted(
                events,
                key=lambda event: (
                    event.provider_namespace,
                    event.subject_token,
                    event.collection_token,
                ),
            )
        ),
        members=tuple(members),
    )


def _related_member(
    source: CohortMember,
    *,
    digit: str,
    time_coordinate: int,
    role: MemberLineageRole,
) -> CohortMember:
    candidate = _member(digit, source.time_coordinate)
    return candidate.model_copy(
        update={
            "subject_token": source.subject_token,
            "collection_token": source.collection_token,
            "specimen_token": source.specimen_token,
            "analysis_unit_token": source.analysis_unit_token,
            "lineage_role": role,
            "technical_replicate_of": (
                source.analysis_record_id
                if role == MemberLineageRole.TECHNICAL_REPLICATE
                else None
            ),
            "reanalysis_of": (
                source.analysis_record_id
                if role == MemberLineageRole.REANALYSIS
                else None
            ),
            "denominator_contribution": False,
            "collection_event_sha256": source.collection_event_sha256,
            "biological_timepoint_id": source.biological_timepoint_id,
            "time_coordinate": source.time_coordinate,
            "time_coordinate_sha256": source.time_coordinate_sha256,
        }
    )


def _policy(**changes: object) -> CohortDenominatorPolicy:
    values: dict[str, object] = {
        "policy_id": "denominator_research_alpha",
        "version": 1,
        "definition_sha256": "0" * 64,
        "inclusion_sha256": POLICIES.inclusion_sha256,
        "exclusion_sha256": POLICIES.exclusion_sha256,
        "missingness_sha256": POLICIES.missingness_sha256,
    }
    values.update(changes)
    return CohortDenominatorPolicy(**values)


def _asset() -> AssetReference:
    return AssetReference(
        asset_id="asset_reference_alpha",
        version="1.0.0",
        content_sha256="1" * 64,
    )


def _method() -> MethodDefinition:
    return MethodDefinition(
        method_id="mth_fragment_alpha",
        version="1.0.0",
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id="qty_fragment_fraction",
        unit="unit_fraction",
        parameter_schema_sha256="2" * 64,
        tools=(
            ToolReference(
                tool_id="tool_measurement_alpha",
                version="1.0.0",
                artifact_sha256="3" * 64,
            ),
        ),
        assets=(_asset(),),
    )


def _capability(
    method: MethodDefinition,
    *,
    qualification: QualificationState = QualificationState.QUALIFIED,
    provider_eligible: bool = True,
) -> CurrentMethodCapability:
    return CurrentMethodCapability(
        registry_sha256=REGISTRY,
        registry_version=2,
        authority_head_sha256=HEAD,
        authority_revision=4,
        method_definition_sha256=method_definition_sha256(method),
        method_ref=method.method_ref,
        authority_scope="scope_research_alpha",
        as_of=NOW,
        qualification_state=qualification,
        display_role=DisplayRole.PROVIDER_PRIMARY,
        research_inspectable=True,
        current_provider_eligible=provider_eligible,
        effective_approval_ref=(
            "approval_provider_alpha" if provider_eligible else None
        ),
    )


def _record(
    digit: str,
    *,
    execution: ExecutionState = ExecutionState.COMPLETE,
    information: InformationState = InformationState.SUFFICIENT,
    trust: TrustState = TrustState.VERIFIED,
    qualification: QualificationState = QualificationState.QUALIFIED,
    provider_eligible: bool = True,
) -> VerifiedMeasurementRecord:
    method = _method()
    bundle_sha256 = digit * 64
    definition_sha256 = method_definition_sha256(method)
    result_identity = hashlib.sha256(
        json.dumps(
            {
                "bundle_sha256": bundle_sha256,
                "method_definition_sha256": definition_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return VerifiedMeasurementRecord(
        result_id=f"result_{result_identity[:40]}",
        result_sha256=hex((int(digit, 16) + 1) % 16)[2:] * 64,
        bundle_id=f"bundle_{digit * 8}",
        bundle_sha256=bundle_sha256,
        method=method,
        method_definition_sha256=definition_sha256,
        current_capability=_capability(
            method,
            qualification=qualification,
            provider_eligible=provider_eligible,
        ),
        execution_state=execution,
        information_state=information,
        trust_state=trust,
        compatibility_key=MeasurementCompatibilityKey(
            measurement_family=method.family,
            quantity_id=method.quantity_id,
            unit=method.unit,
            result_schema=ResultSchemaReference(
                schema_id="schema_measurement_alpha", version="1.0.0"
            ),
            reference_asset=_asset(),
            grid_asset=_asset(),
            atlas_asset=_asset(),
            panel_asset=_asset(),
            normalization_semantics_id="sem_normalization_alpha",
            coordinate_semantics_id="sem_coordinate_alpha",
            denominator_semantics_id="sem_denominator_alpha",
            registered_policy=CompatibilityPolicyReference(
                policy_id="policy_longitudinal_alpha", version="1.0.0"
            ),
        ),
    )


def _compatibility_policy(record: VerifiedMeasurementRecord) -> CompatibilityPolicy:
    capability = record.current_capability
    return CompatibilityPolicy(
        policy_id="policy_longitudinal_alpha",
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


def _observed(value: int, label: str) -> CountValue:
    return CountValue(state=CountState.OBSERVED, value=value, accessible_label=label)


def _unavailable(state: CountState, label: str) -> CountValue:
    return CountValue(state=state, accessible_label=label)


def _ledger(*, state: CountState = CountState.OBSERVED) -> DenominatorLedger:
    if state == CountState.OBSERVED:
        values = (
            _observed(100, "Input records"),
            _observed(90, "Accepted records"),
            _observed(80, "Eligible records"),
            _observed(75, "Displayed records"),
        )
        attrition = (
            _observed(10, "Acceptance exclusions"),
            _observed(10, "Eligibility exclusions"),
            _observed(5, "Display exclusions"),
        )
    else:
        values = tuple(
            _unavailable(state, f"{name} unavailable")
            for name in ("Input", "Accepted", "Eligible", "Displayed")
        )
        attrition = tuple(
            _unavailable(state, f"{name} exclusions unavailable")
            for name in ("Acceptance", "Eligibility", "Display")
        )
    return DenominatorLedger(
        input_records=values[0],
        accepted_records=values[1],
        eligible_records=values[2],
        displayed_records=values[3],
        attrition=(
            AttritionReason(
                stage=AttritionStage.ACCEPTANCE,
                reason_code="reason_acceptance",
                accessible_label="Acceptance exclusions",
                count=attrition[0],
            ),
            AttritionReason(
                stage=AttritionStage.DISPLAY,
                reason_code="reason_display",
                accessible_label="Display exclusions",
                count=attrition[2],
            ),
            AttritionReason(
                stage=AttritionStage.ELIGIBILITY,
                reason_code="reason_eligibility",
                accessible_label="Eligibility exclusions",
                count=attrition[1],
            ),
        ),
    )


def _source(
    record: VerifiedMeasurementRecord,
    peer: VerifiedMeasurementRecord,
    *,
    ledger_state: CountState = CountState.OBSERVED,
):
    policy = _compatibility_policy(record)
    decision = decide_compatibility(
        CompatibilityRequest(
            left=record,
            right=peer,
            policy=policy,
            trusted_policy_sha256=compatibility_policy_sha256(policy),
            trusted_authority_head_sha256=(
                record.current_capability.authority_head_sha256
            ),
        )
    )
    return bind_result_view_source(
        record=record,
        compatibility_decision=decision,
        denominator=_ledger(state=ledger_state),
        accessible_label="Research aggregate",
        qc_label="Qualified research result",
    )


def _bound_source(values, catalog_result: CatalogResultRef):
    method = values[7].method_definitions[0]
    capability = values[10]
    asset = method.assets[0]
    record = VerifiedMeasurementRecord(
        result_id=catalog_result.result_id,
        result_sha256="d" * 64,
        bundle_id="bundle_registered_summary",
        bundle_sha256=catalog_result.bundle_sha256,
        method=method,
        method_definition_sha256=catalog_result.method_definition_sha256,
        current_capability=capability,
        execution_state=ExecutionState.COMPLETE,
        information_state=InformationState.SUFFICIENT,
        trust_state=TrustState.VERIFIED,
        compatibility_key=MeasurementCompatibilityKey(
            measurement_family=method.family,
            quantity_id=method.quantity_id,
            unit=method.unit,
            result_schema=ResultSchemaReference(
                schema_id="schema_registered_summary", version="1.0.0"
            ),
            reference_asset=asset,
            grid_asset=asset,
            atlas_asset=asset,
            panel_asset=asset,
            normalization_semantics_id="sem_normalization_registered",
            coordinate_semantics_id="sem_coordinate_registered",
            denominator_semantics_id="sem_denominator_registered",
            registered_policy=CompatibilityPolicyReference(
                policy_id="policy_longitudinal_alpha", version="1.0.0"
            ),
        ),
    )
    peer_values = record.model_dump(mode="python")
    peer_values.update(
        {
            "result_id": "result_registered_summary_peer",
            "result_sha256": "e" * 64,
            "bundle_id": "bundle_registered_summary_peer",
            "bundle_sha256": "f" * 64,
        }
    )
    peer = VerifiedMeasurementRecord.model_validate(peer_values)
    return _source(record, peer)


def _catalog(record: VerifiedMeasurementRecord) -> CatalogResultRef:
    capability = record.current_capability
    return CatalogResultRef(
        result_id=record.result_id,
        bundle_sha256=record.bundle_sha256,
        bundle_record_id="record.synthetic.alpha",
        bundle_manifest_sha256="4" * 64,
        workflow_release_id="release.synthetic.alpha",
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        authority_scope=capability.authority_scope,
        capability_as_of=capability.as_of,
        qualification_state=CatalogQualificationState(
            capability.qualification_state.value
        ),
        display_role=capability.display_role,
        research_inspectable=capability.research_inspectable,
        current_provider_eligible=capability.current_provider_eligible,
    )


def _member_sha256(member: CohortMember) -> str:
    return hashlib.sha256(canonical_contract_bytes(member)).hexdigest()


def _included(member: CohortMember, record, peer) -> CohortMemberEvidence:
    return CohortMemberEvidence(
        member_sha256=_member_sha256(member),
        disposition=MemberDisposition.INCLUDED,
        reason=MemberDispositionReason.INCLUDED_BY_POLICY,
        catalog_result=_catalog(record),
        result_source=_source(record, peer),
    )


def test_deterministic_summary_reconciles_members_units_and_exact_ledgers() -> None:
    first, second = _member("a", 100), _member("b", 200)
    manifest = _manifest(first, second)
    left, right = _record("a"), _record("b")
    first_evidence = _included(first, left, right)
    second_evidence = _included(second, right, left)

    expected = build_cohort_denominator_summary(
        manifest=manifest,
        policy=_policy(),
        evidence=(first_evidence, second_evidence),
    )
    permuted = build_cohort_denominator_summary(
        manifest=manifest,
        policy=_policy(),
        evidence=(second_evidence, first_evidence),
    )

    assert expected == permuted
    assert expected.state == CohortSummaryState.MULTIPLE_INCLUDED_UNITS
    assert (
        expected.declared_members,
        expected.included_members,
        expected.excluded_members,
        expected.unavailable_members,
    ) == (2, 2, 0, 0)
    assert (
        expected.declared_denominator_units,
        expected.included_denominator_units,
        expected.excluded_denominator_units,
        expected.unavailable_denominator_units,
    ) == (2, 2, 0, 0)
    encoded = cohort_denominator_summary_bytes(expected)
    assert cohort_denominator_summary_from_bytes(encoded) == expected
    for protected in (
        first.subject_token,
        first.collection_token,
        first.specimen_token,
        first.run_token,
        first.provider_namespace,
    ):
        assert protected.encode() not in encoded
    assert b'"scientific_qualification_claimed":false' in encoded


def test_zero_and_one_included_unit_states_are_explicit() -> None:
    first, second = _member("a", 100), _member("b", 200)
    manifest = _manifest(first, second)
    left, right = _record("a"), _record("b")
    unavailable = CohortMemberEvidence(
        member_sha256=_member_sha256(first),
        disposition=MemberDisposition.UNAVAILABLE,
        reason=MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT,
    )
    excluded = CohortMemberEvidence(
        member_sha256=_member_sha256(second),
        disposition=MemberDisposition.EXCLUDED,
        reason=MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
    )
    empty = build_cohort_denominator_summary(
        manifest=manifest, policy=_policy(), evidence=(unavailable, excluded)
    )
    assert empty.state == CohortSummaryState.NO_INCLUDED_UNITS
    assert empty.declared_denominator_units == 2
    assert empty.unavailable_denominator_units == 1
    assert empty.excluded_denominator_units == 1

    one = build_cohort_denominator_summary(
        manifest=manifest,
        policy=_policy(),
        evidence=(_included(first, left, right), excluded),
    )
    assert one.state == CohortSummaryState.ONE_INCLUDED_UNIT
    assert one.included_denominator_units == 1


def test_replicate_and_reanalysis_records_do_not_inflate_biological_units() -> None:
    draw = _member("a", 100)
    replicate = _related_member(
        draw,
        digit="b",
        time_coordinate=110,
        role=MemberLineageRole.TECHNICAL_REPLICATE,
    )
    reanalysis = _related_member(
        draw,
        digit="c",
        time_coordinate=120,
        role=MemberLineageRole.REANALYSIS,
    )
    manifest = _manifest(draw, replicate, reanalysis)
    left, right = _record("a"), _record("b")
    summary = build_cohort_denominator_summary(
        manifest=manifest,
        policy=_policy(),
        evidence=(
            _included(draw, left, right),
            CohortMemberEvidence(
                member_sha256=_member_sha256(replicate),
                disposition=MemberDisposition.EXCLUDED,
                reason=MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
            ),
            CohortMemberEvidence(
                member_sha256=_member_sha256(reanalysis),
                disposition=MemberDisposition.EXCLUDED,
                reason=MemberDispositionReason.REANALYSIS_COLLAPSED,
            ),
        ),
    )
    assert summary.declared_members == 3
    assert summary.included_members == 1
    assert summary.excluded_members == 2
    assert summary.declared_denominator_units == 1
    assert summary.included_denominator_units == 1
    assert summary.excluded_denominator_units == 0


@pytest.mark.parametrize(
    ("role", "disposition"),
    (
        (MemberLineageRole.TECHNICAL_REPLICATE, MemberDisposition.INCLUDED),
        (MemberLineageRole.TECHNICAL_REPLICATE, MemberDisposition.UNAVAILABLE),
        (MemberLineageRole.REANALYSIS, MemberDisposition.INCLUDED),
        (MemberLineageRole.REANALYSIS, MemberDisposition.UNAVAILABLE),
    ),
)
def test_collapsed_lineage_cannot_be_reclassified_as_population_evidence(
    role: MemberLineageRole,
    disposition: MemberDisposition,
) -> None:
    draw = _member("a", 100)
    derived = _related_member(
        draw,
        digit="b",
        time_coordinate=110,
        role=role,
    )
    left, right = _record("a"), _record("b")
    draw_evidence = CohortMemberEvidence(
        member_sha256=_member_sha256(draw),
        disposition=MemberDisposition.EXCLUDED,
        reason=MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
    )
    derived_evidence = (
        _included(derived, left, right)
        if disposition == MemberDisposition.INCLUDED
        else CohortMemberEvidence(
            member_sha256=_member_sha256(derived),
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT,
        )
    )

    with pytest.raises(ValidationError, match="collapsed exclusions"):
        build_cohort_denominator_summary(
            manifest=_manifest(draw, derived),
            policy=_policy(),
            evidence=(draw_evidence, derived_evidence),
        )


def test_collapsed_lineage_result_source_cannot_be_silently_ignored() -> None:
    draw = _member("a", 100)
    replicate = _related_member(
        draw,
        digit="b",
        time_coordinate=110,
        role=MemberLineageRole.TECHNICAL_REPLICATE,
    )
    left, right = _record("a"), _record("b")
    source = _source(left, right)
    evidence = (
        CohortMemberEvidence(
            member_sha256=_member_sha256(replicate),
            disposition=MemberDisposition.EXCLUDED,
            reason=MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
        ),
    )
    with pytest.raises(ValueError, match="not consumed"):
        cohort_summary_module._require_all_result_sources_consumed(
            evidence,
            {source.record.result_id: source},
        )


@pytest.mark.parametrize("state", (CountState.MISSING, CountState.WITHHELD))
def test_included_member_requires_fully_observed_e06_ledger(state: CountState) -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    evidence = _included(member, left, right).model_copy(
        update={"result_source": _source(left, right, ledger_state=state)}
    )
    with pytest.raises(ValueError, match="eligibility gate"):
        build_cohort_denominator_summary(
            manifest=_manifest(member), policy=_policy(), evidence=(evidence,)
        )


def test_missing_ledger_is_unavailable_and_never_rewritten_as_zero() -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    source = _source(left, right, ledger_state=CountState.MISSING)
    evidence = CohortMemberEvidence(
        member_sha256=_member_sha256(member),
        disposition=MemberDisposition.UNAVAILABLE,
        reason=MemberDispositionReason.DENOMINATOR_MISSING,
        catalog_result=_catalog(left),
        result_source=source,
    )
    summary = build_cohort_denominator_summary(
        manifest=_manifest(member), policy=_policy(), evidence=(evidence,)
    )
    assert summary.included_denominator_units == 0
    assert summary.unavailable_denominator_units == 1
    assert summary.rows[0].denominator_ledger_sha256 is not None
    assert b'"value":0' not in cohort_denominator_summary_bytes(summary)


def test_unqualified_or_noncomparable_result_cannot_be_included() -> None:
    member = _member("a", 100)
    qualified, peer = _record("a"), _record("b")
    unqualified = _record(
        "a",
        qualification=QualificationState.DEVELOPMENT_UNQUALIFIED,
        provider_eligible=False,
    )
    with pytest.raises(ValueError, match="eligibility gate"):
        build_cohort_denominator_summary(
            manifest=_manifest(member),
            policy=_policy(),
            evidence=(_included(member, unqualified, peer),),
        )

    unknown_source = _source(qualified, peer).model_copy(
        update={
            "compatibility_decision": _source(
                qualified, peer
            ).compatibility_decision.model_copy(
                update={"outcome": CompatibilityOutcome.UNKNOWN}
            )
        }
    )
    unknown = _included(member, qualified, peer).model_copy(
        update={"result_source": unknown_source}
    )
    with pytest.raises(ValueError):
        build_cohort_denominator_summary(
            manifest=_manifest(member), policy=_policy(), evidence=(unknown,)
        )


def test_policy_and_exact_member_coverage_are_mandatory() -> None:
    first, second = _member("a", 100), _member("b", 200)
    left, right = _record("a"), _record("b")
    evidence = _included(first, left, right)
    with pytest.raises(ValueError, match="manifest policies"):
        build_cohort_denominator_summary(
            manifest=_manifest(first),
            policy=_policy(missingness_sha256="f" * 64),
            evidence=(evidence,),
        )
    with pytest.raises(ValueError, match="every manifest member"):
        build_cohort_denominator_summary(
            manifest=_manifest(first, second), policy=_policy(), evidence=(evidence,)
        )
    with pytest.raises(ValueError, match="duplicated"):
        build_cohort_denominator_summary(
            manifest=_manifest(first, second),
            policy=_policy(),
            evidence=(evidence, evidence),
        )


def test_unavailable_reason_must_be_proven_by_exact_evidence() -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    unsupported = CohortMemberEvidence(
        member_sha256=_member_sha256(member),
        disposition=MemberDisposition.UNAVAILABLE,
        reason=MemberDispositionReason.EXECUTION_FAILED,
        catalog_result=_catalog(left),
        result_source=_source(left, right),
    )
    with pytest.raises(ValueError, match="not supported"):
        build_cohort_denominator_summary(
            manifest=_manifest(member), policy=_policy(), evidence=(unsupported,)
        )


def test_catalog_and_result_view_must_bind_the_same_exact_result() -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    evidence = _included(member, left, right).model_copy(
        update={"catalog_result": _catalog(right)}
    )
    with pytest.raises(ValueError, match="exact result view source"):
        build_cohort_denominator_summary(
            manifest=_manifest(member), policy=_policy(), evidence=(evidence,)
        )


def test_one_catalog_result_cannot_be_counted_for_two_members() -> None:
    first, second = _member("a", 100), _member("b", 200)
    left, right = _record("a"), _record("b")
    with pytest.raises(ValueError, match="multiple members"):
        build_cohort_denominator_summary(
            manifest=_manifest(first, second),
            policy=_policy(),
            evidence=(
                _included(first, left, right),
                _included(second, left, right),
            ),
        )


def test_summary_canonical_parser_rejects_tampering_and_unknown_fields() -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    summary = build_cohort_denominator_summary(
        manifest=_manifest(member),
        policy=_policy(),
        evidence=(_included(member, left, right),),
    )
    payload = json.loads(cohort_denominator_summary_bytes(summary))
    payload["included_members"] = 0
    with pytest.raises(ValueError):
        cohort_denominator_summary_from_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    payload = json.loads(cohort_denominator_summary_bytes(summary))
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        cohort_denominator_summary_from_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
    with pytest.raises(ValueError, match="not canonical"):
        cohort_denominator_summary_from_bytes(
            cohort_denominator_summary_bytes(summary) + b"\n"
        )


def test_summary_model_rejects_forged_count_and_identity() -> None:
    member = _member("a", 100)
    left, right = _record("a"), _record("b")
    summary = build_cohort_denominator_summary(
        manifest=_manifest(member),
        policy=_policy(),
        evidence=(_included(member, left, right),),
    )
    values = summary.model_dump(mode="python")
    values["population_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="population digest"):
        CohortDenominatorSummary.model_validate(values)
    values = summary.model_dump(mode="python")
    values["included_members"] = 0
    with pytest.raises(ValidationError, match="dispositions"):
        CohortDenominatorSummary.model_validate(values)

    forged_counts = {
        "included_members": 0,
        "excluded_members": 1,
        "included_denominator_units": 0,
        "excluded_denominator_units": 1,
        "state": CohortSummaryState.NO_INCLUDED_UNITS,
    }
    placeholder = summary.model_copy(
        update={
            **forged_counts,
            "population_id": "population_" + "0" * 40,
            "population_sha256": "0" * 64,
        }
    )
    forged_sha256 = hashlib.sha256(canonical_contract_bytes(placeholder)).hexdigest()
    values = summary.model_dump(mode="python")
    values.update(forged_counts)
    values["population_id"] = f"population_{forged_sha256[:40]}"
    values["population_sha256"] = forged_sha256
    with pytest.raises(ValidationError, match="dispositions"):
        CohortDenominatorSummary.model_validate(values)


@pytest.fixture
def live_catalog(tmp_path):
    generator = _cohort_live.__wrapped__(tmp_path)
    value = next(generator)
    try:
        yield value
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def test_registered_summary_derives_missing_and_included_from_live_d05_d06(
    tmp_path, live_catalog
) -> None:
    values = _setup_cohort_records(tmp_path / "records", live_catalog)
    manifest = values[2]
    policy = _policy(
        inclusion_sha256=manifest.policies.inclusion_sha256,
        exclusion_sha256=manifest.policies.exclusion_sha256,
        missingness_sha256=manifest.policies.missingness_sha256,
    )
    registry = CohortRegistry(
        tmp_path / "registry",
        linkage_store=live_catalog[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        registry.register(manifest)
        selector = registry.list_selectors().records[0]
        missing = build_registered_cohort_denominator_summary(
            registry=registry,
            selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            record_catalog=values[0],
            policy=policy,
            result_sources=(),
        )
        assert missing.population.unavailable_denominator_units == 1
        assert missing.population.included_denominator_units == 0

        binding = _import_cohort_record(values)
        source = _bound_source(values, binding.result)
        included = build_registered_cohort_denominator_summary(
            registry=registry,
            selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            record_catalog=values[0],
            policy=policy,
            result_sources=(source,),
        )
        assert included.population.included_denominator_units == 1
        assert included.population.unavailable_denominator_units == 0
        content = registered_cohort_denominator_summary_bytes(included)
        assert (
            registered_cohort_denominator_summary_from_bytes(content) == included
        )
        assert RegisteredCohortDenominatorSummary.model_validate_json(content) == included
        duplicate = content[:-1] + b',"summary_sha256":"' + b"0" * 64 + b'"}'
        huge_integer = content.replace(
            b'"cohort_version":1',
            b'"cohort_version":' + b"9" * 100_000,
            1,
        )
        assert huge_integer != content
        for hostile in (
            duplicate,
            huge_integer,
            b"[" * 2_000 + b"0" + b"]" * 2_000,
        ):
            with pytest.raises(ValueError, match="not canonical"):
                registered_cohort_denominator_summary_from_bytes(hostile)

        poisoned_policy = policy.model_copy()
        object.__setattr__(
            poisoned_policy,
            "__pydantic_private__",
            {"protected_identity": manifest.members[0].subject_token},
        )
        with pytest.raises(ValueError, match="input is not canonical"):
            build_registered_cohort_denominator_summary(
                registry=registry,
                selector_id=selector.selector_id,
                cohort_version=selector.cohort_version,
                record_catalog=values[0],
                policy=poisoned_policy,
                result_sources=(),
            )
        public = content.decode("utf-8")
        # The opaque cohort ID is the public comparison identity. Protected
        # provider linkage tokens, member commitments, and record lineage must
        # remain absent from this aggregate projection.
        for forbidden in (
            manifest.members[0].provider_namespace,
            manifest.members[0].subject_token,
            manifest.members[0].collection_token,
            manifest.members[0].specimen_token,
            manifest.members[0].analysis_record_id,
            manifest.members[0].run_token,
            _member_sha256(manifest.members[0]),
            binding.result.result_id,
        ):
            assert forbidden not in public
    finally:
        registry.close()
        values[0].close()
        values[1].close()
