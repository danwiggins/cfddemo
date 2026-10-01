"""D07 repeatability comparisons remain descriptive and fail closed."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta, tzinfo
from typing import ClassVar

import pytest
from pydantic import ValidationError

import evidence_inspector.repeatability_comparison as repeatability_module
from evidence_inspector.compatibility import ExecutionState, InformationState
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalDecisionReplayError,
    LongitudinalOutcome,
    decide_longitudinal_member,
    longitudinal_anchor_policy_sha256,
    longitudinal_record_sha256,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
)
from evidence_inspector.repeatability_comparison import (
    ALL_REPEATABILITY_FACTORS,
    ComparisonAvailability,
    ComparisonObservation,
    FactorEnvelope,
    MeasurementCondition,
    MeasurementDenominator,
    MeasurementEvidencePayload,
    MeasurementEvidenceReceipt,
    ObservationState,
    RepeatabilityClassification,
    RepeatabilityEnvelope,
    RepeatabilityFactor,
    RepeatabilityReason,
    compare_repeatability,
    compare_repeatability_in_fence,
    measurement_evidence_payload_sha256,
    measurement_evidence_receipt_signing_bytes,
    repeatability_comparison_sha256,
    repeatability_envelope_sha256,
    result_trust_document_sha256,
)
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    NOW,
    PROVIDER,
    TRUST_SHA256,
    _activated_records,
    _decide,
    _policy,
    _record,
)
from traceback_runner.signing import (
    DevelopmentTrustDocument,
    KeyPurpose,
    development_trust_bytes,
    generate_development_keypair,
    sign_bytes,
)

EVIDENCE_SHA256 = "b" * 64
PROTOCOL_SHA256 = "c" * 64
AUTHORITY_SHA256 = "e" * 64
SIGNING_KEY = generate_development_keypair(KeyPurpose.RESULT)
RESULT_TRUST_DOCUMENT = DevelopmentTrustDocument.model_validate_json(
    development_trust_bytes(SIGNING_KEY)
)
RESULT_TRUST_SHA256 = result_trust_document_sha256(RESULT_TRUST_DOCUMENT)


def _condition_policy(record, factor: RepeatabilityFactor) -> str:
    if factor == RepeatabilityFactor.PREANALYTICS:
        dimension = record.comparison_key.dimensions[
            list(ComparisonDimension).index(ComparisonDimension.PREANALYTICS_POLICY)
        ]
        assert dimension.content_sha256 is not None
        return dimension.content_sha256
    return {
        RepeatabilityFactor.BETWEEN_DAY: "1" * 64,
        RepeatabilityFactor.OPERATOR: "2" * 64,
        RepeatabilityFactor.LOT: "3" * 64,
    }[factor]


def _conditions(record) -> tuple[MeasurementCondition, ...]:
    return tuple(
        MeasurementCondition(
            factor=factor,
            condition_sha256=hashlib.sha256(
                f"condition:{factor.value}:alpha".encode()
            ).hexdigest(),
            condition_policy_sha256=_condition_policy(record, factor),
        )
        for factor in ALL_REPEATABILITY_FACTORS
    )


def _envelope(anchor, *, limit: float = 0.1) -> RepeatabilityEnvelope:
    key = anchor.comparison_key
    dimensions = {item.dimension: item for item in key.dimensions}
    uncertainty_sha256 = dimensions[
        ComparisonDimension.UNCERTAINTY_METHOD
    ].content_sha256
    denominator_sha256 = dimensions[
        ComparisonDimension.DENOMINATOR_SEMANTICS
    ].content_sha256
    assert uncertainty_sha256 is not None
    assert denominator_sha256 is not None
    return RepeatabilityEnvelope(
        schema_version="traceback.repeatability-envelope.v1",
        evidence_id="repeatability_fragment_alpha",
        evidence_version="1.0.0",
        evidence_sha256=EVIDENCE_SHA256,
        protocol_id="protocol_fragment_alpha",
        protocol_version="1.0.0",
        protocol_sha256=PROTOCOL_SHA256,
        authority_id="authority_fragment_alpha",
        authority_sha256=AUTHORITY_SHA256,
        valid_from=NOW - timedelta(days=30),
        valid_through=NOW + timedelta(days=30),
        method_ref=key.method_ref,
        method_definition_sha256=key.method_definition_sha256,
        quantity_id=key.quantity_id,
        unit=key.unit,
        uncertainty_method_id="uncertainty_bootstrap_alpha",
        uncertainty_method_sha256=uncertainty_sha256,
        denominator_semantics_id="denominator_fragments_alpha",
        denominator_semantics_sha256=denominator_sha256,
        factor_envelopes=tuple(
            FactorEnvelope(
                factor=condition.factor,
                anchor_condition_sha256=condition.condition_sha256,
                member_condition_sha256=condition.condition_sha256,
                condition_policy_sha256=condition.condition_policy_sha256,
                maximum_absolute_contribution=0.04,
            )
            for condition in _conditions(anchor)
        ),
        maximum_absolute_delta=limit,
        combination_rule="preapproved_combined_absolute_delta.v1",
    )


def _observation(
    record,
    value: float | None,
    *,
    state: ObservationState = ObservationState.AVAILABLE,
    denominator: MeasurementDenominator | None = None,
    conditions: tuple[MeasurementCondition, ...] | None = None,
    signing_key=SIGNING_KEY,
) -> ComparisonObservation:
    key = record.comparison_key
    dimensions = {item.dimension: item for item in key.dimensions}
    uncertainty_sha256 = dimensions[
        ComparisonDimension.UNCERTAINTY_METHOD
    ].content_sha256
    denominator_sha256 = dimensions[
        ComparisonDimension.DENOMINATOR_SEMANTICS
    ].content_sha256
    assert uncertainty_sha256 is not None
    assert denominator_sha256 is not None
    denominator = denominator or (
        MeasurementDenominator(
            total_count=110,
            included_count=100,
            excluded_count=10,
        )
        if state == ObservationState.AVAILABLE
        else None
    )
    evidence = MeasurementEvidencePayload(
        schema_version="traceback.comparison-measurement-evidence.v1",
        evidence_id=(
            "measurement_evidence_"
            + record.measurement.result_id.removeprefix("result_")
        ),
        record_sha256=longitudinal_record_sha256(record),
        result_id=record.measurement.result_id,
        result_sha256=record.measurement.result_sha256,
        bundle_sha256=record.measurement.bundle_sha256,
        method_ref=key.method_ref,
        method_definition_sha256=key.method_definition_sha256,
        quantity_id=key.quantity_id,
        unit=key.unit,
        uncertainty_method_sha256=uncertainty_sha256,
        denominator_semantics_sha256=denominator_sha256,
        conditions=conditions or _conditions(record),
        state=state,
        value=value,
        uncertainty_lower=value - 0.02 if value is not None else None,
        uncertainty_upper=value + 0.02 if value is not None else None,
        denominator=denominator,
    )
    receipt = MeasurementEvidenceReceipt(
        schema_version="traceback.comparison-measurement-receipt.v1",
        receipt_id=(
            "measurement_receipt_"
            + record.measurement.result_id.removeprefix("result_")
        ),
        evidence_sha256=measurement_evidence_payload_sha256(evidence),
        signature=sign_bytes(
            measurement_evidence_receipt_signing_bytes(
                "measurement_receipt_"
                + record.measurement.result_id.removeprefix("result_"),
                measurement_evidence_payload_sha256(evidence),
            ),
            signing_key,
            purpose=KeyPurpose.RESULT,
        ),
    )
    return ComparisonObservation(
        schema_version="traceback.comparison-observation.v1",
        evidence=evidence,
        receipt=receipt,
    )


def _compare(
    anchor,
    member,
    policy,
    expected_decision,
    anchor_observation,
    member_observation,
    envelope,
    **overrides,
):
    try:
        envelope_sha256 = (
            repeatability_envelope_sha256(envelope)
            if envelope is not None
            else "f" * 64
        )
    except (AttributeError, TypeError, ValueError):
        envelope_sha256 = "f" * 64
    observation_mutator = overrides.pop("observation_mutator", None)
    arguments = {
        "evaluated_at": NOW,
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: TRUST_SHA256},
        "expected_envelope_sha256": envelope_sha256,
        "expected_evidence_sha256": EVIDENCE_SHA256,
        "expected_protocol_sha256": PROTOCOL_SHA256,
        "expected_repeatability_authority_sha256": AUTHORITY_SHA256,
    }
    with _activated_records(anchor, member) as (records, store):
        anchor, member = records
        arguments["linkage_store"] = store
        arguments["result_trust_document"] = RESULT_TRUST_DOCUMENT
        arguments["expected_result_trust_sha256"] = RESULT_TRUST_SHA256
        arguments.update(overrides)
        decision = decide_longitudinal_member(
            anchor,
            member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        assert decision.outcome == expected_decision.outcome
        anchor_observation = _observation(
            anchor,
            anchor_observation.evidence.value,
            state=anchor_observation.evidence.state,
            denominator=anchor_observation.evidence.denominator,
            conditions=anchor_observation.evidence.conditions,
        )
        member_observation = _observation(
            member,
            member_observation.evidence.value,
            state=member_observation.evidence.state,
            denominator=member_observation.evidence.denominator,
            conditions=member_observation.evidence.conditions,
        )
        if observation_mutator is not None:
            anchor_observation, member_observation = observation_mutator(
                anchor,
                member,
                anchor_observation,
                member_observation,
            )
        return compare_repeatability(
            anchor,
            member,
            policy,
            decision,
            anchor_observation,
            member_observation,
            envelope,
            **arguments,
        )


def _direct_arguments(policy, store, envelope, **overrides):
    arguments = {
        "evaluated_at": NOW,
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: TRUST_SHA256},
        "linkage_store": store,
        "result_trust_document": RESULT_TRUST_DOCUMENT,
        "expected_result_trust_sha256": RESULT_TRUST_SHA256,
        "expected_envelope_sha256": repeatability_envelope_sha256(envelope),
        "expected_evidence_sha256": EVIDENCE_SHA256,
        "expected_protocol_sha256": PROTOCOL_SHA256,
        "expected_repeatability_authority_sha256": AUTHORITY_SHA256,
    }
    arguments.update(overrides)
    return arguments


@pytest.mark.parametrize(
    ("member_value", "classification", "reason"),
    (
        (
            0.5,
            RepeatabilityClassification.EXACT_SAME_VALUE,
            RepeatabilityReason.EXACT_SAME_VALUE,
        ),
        (
            0.6,
            RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
            RepeatabilityReason.WITHIN_PREAPPROVED_ENVELOPE,
        ),
    ),
)
def test_exact_and_inclusive_envelope_edge_are_available(
    member_value: float,
    classification: RepeatabilityClassification,
    reason: RepeatabilityReason,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    envelope = _envelope(anchor)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, member_value),
        envelope,
    )

    assert result.availability == ComparisonAvailability.AVAILABLE
    assert result.classification == classification
    assert result.reason_codes == (reason,)
    assert result.delta == pytest.approx(member_value - 0.5)
    assert result.anchor_denominator_count == 100
    assert result.member_uncertainty_upper == pytest.approx(member_value + 0.02)
    assert result.trend_allowed
    assert result.automatic_correction_applied is False
    assert result.repeatability_envelope_sha256 == repeatability_envelope_sha256(
        envelope
    )
    assert result.repeatability_evidence_sha256 == EVIDENCE_SHA256
    assert result.repeatability_protocol_sha256 == PROTOCOL_SHA256
    assert result.repeatability_authority_sha256 == AUTHORITY_SHA256
    assert result.anchor_measurement_evidence_sha256 is not None
    assert result.member_measurement_receipt_sha256 is not None
    assert len(result.factor_transition_sha256s) == 4
    assert repeatability_comparison_sha256(result)


def test_outside_envelope_suppresses_values_delta_and_trend() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    envelope = _envelope(anchor)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.6000000001),
        envelope,
    )

    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.classification == RepeatabilityClassification.OUTSIDE_ENVELOPE
    assert result.delta is None
    assert result.anchor_value is None
    assert result.member_value is None
    assert result.maximum_absolute_delta is None
    assert not result.trend_allowed


@pytest.mark.parametrize(
    ("override", "reason"),
    (
        (
            {"expected_evidence_sha256": "9" * 64},
            RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,
        ),
        (
            {"expected_protocol_sha256": "9" * 64},
            RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,
        ),
        (
            {"expected_repeatability_authority_sha256": "9" * 64},
            RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,
        ),
        (
            {"expected_envelope_sha256": "9" * 64},
            RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,
        ),
    ),
)
def test_any_evidence_substitution_is_unavailable(override, reason) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    envelope = _envelope(anchor)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        envelope,
        **override,
    )

    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.reason_codes == (reason,)
    assert result.delta is None


@pytest.mark.parametrize("mutation", ("value", "denominator", "uncertainty"))
def test_signed_measurement_payload_rejects_numeric_substitution(mutation: str) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    def mutate(_anchor, _member, anchor_observation, member_observation):
        evidence = member_observation.evidence
        if mutation == "value":
            evidence = evidence.model_copy(
                update={
                    "value": 50.0,
                    "uncertainty_lower": 49.0,
                    "uncertainty_upper": 51.0,
                }
            )
        elif mutation == "denominator":
            evidence = evidence.model_copy(
                update={
                    "denominator": MeasurementDenominator(
                        total_count=1_000_000_000,
                        included_count=999_999_999,
                        excluded_count=1,
                    )
                }
            )
        else:
            evidence = evidence.model_copy(update={"uncertainty_lower": None})
        return (
            anchor_observation,
            member_observation.model_copy(update={"evidence": evidence}),
        )

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
        observation_mutator=mutate,
    )

    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.reason_codes == (RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,)
    assert result.delta is None


def test_unregistered_factor_transition_is_unavailable_even_with_valid_signature() -> (
    None
):
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    def mutate(_anchor, active_member, anchor_observation, member_observation):
        conditions = list(member_observation.evidence.conditions)
        conditions[1] = conditions[1].model_copy(update={"condition_sha256": "9" * 64})
        return (
            anchor_observation,
            _observation(
                active_member,
                member_observation.evidence.value,
                denominator=member_observation.evidence.denominator,
                conditions=tuple(conditions),
            ),
        )

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
        observation_mutator=mutate,
    )

    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.reason_codes == (RepeatabilityReason.FACTOR_TRANSITION_UNREGISTERED,)
    assert result.factor_transition_sha256s == ()


def test_rehashed_numeric_evidence_still_requires_valid_authority_signature() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    def mutate(_anchor, _member, anchor_observation, member_observation):
        evidence = member_observation.evidence.model_copy(
            update={
                "value": 50.0,
                "uncertainty_lower": 49.0,
                "uncertainty_upper": 51.0,
            }
        )
        receipt = member_observation.receipt.model_copy(
            update={"evidence_sha256": measurement_evidence_payload_sha256(evidence)}
        )
        return (
            anchor_observation,
            ComparisonObservation(
                schema_version="traceback.comparison-observation.v1",
                evidence=evidence,
                receipt=receipt,
            ),
        )

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
        observation_mutator=mutate,
    )

    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.reason_codes == (RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID,)
    assert result.delta is None


def test_model_copy_cannot_bypass_canonical_envelope_or_comparison_validation() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    malformed = _envelope(anchor).model_copy(update={"factor_envelopes": ()})

    unavailable = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        malformed,
    )

    assert unavailable.availability == ComparisonAvailability.UNAVAILABLE
    assert unavailable.reason_codes == (RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,)

    valid = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
    )
    forged = valid.model_copy(update={"delta": 99.0})
    with pytest.raises(ValidationError, match="delta does not match"):
        repeatability_comparison_sha256(forged)


def test_denominator_must_reconcile_before_signing() -> None:
    with pytest.raises(ValidationError, match="reconcile exactly"):
        MeasurementDenominator(
            total_count=100,
            included_count=99,
            excluded_count=2,
        )


def test_missing_or_stale_evidence_is_unavailable() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    observations = (_observation(anchor, 0.5), _observation(member, 0.55))

    missing = _compare(anchor, member, policy, decision, *observations, None)
    stale_envelope = _envelope(anchor)
    stale = _compare(
        anchor,
        member,
        policy,
        decision,
        *observations,
        stale_envelope,
        evaluated_at=NOW + timedelta(days=31),
    )

    assert missing.classification == RepeatabilityClassification.EVIDENCE_UNAVAILABLE
    assert missing.reason_codes == (RepeatabilityReason.EVIDENCE_MISSING,)
    assert stale.reason_codes == (RepeatabilityReason.EVIDENCE_STALE,)
    assert missing.delta is stale.delta is None


def test_missing_draw_is_distinct_and_never_uses_zero_placeholder() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    missing = _observation(member, None, state=ObservationState.MISSING_DRAW)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        missing,
        _envelope(anchor),
    )

    assert result.classification == RepeatabilityClassification.MISSING_DRAW
    assert result.member_value is None
    assert result.delta is None


@pytest.mark.parametrize(
    ("execution", "information"),
    (
        (ExecutionState.FAILED, InformationState.SUFFICIENT),
        (ExecutionState.COMPLETE, InformationState.INSUFFICIENT),
    ),
)
def test_failed_or_insufficient_measurement_is_distinct(execution, information) -> None:
    anchor = _record("1")
    member = _record("2")
    member = member.model_copy(
        update={
            "measurement": member.measurement.model_copy(
                update={"execution_state": execution, "information_state": information}
            )
        }
    )
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
    )

    expected = (
        RepeatabilityClassification.FAILED_MEASUREMENT
        if execution == ExecutionState.FAILED
        else RepeatabilityClassification.INSUFFICIENT_MEASUREMENT
    )
    assert result.classification == expected
    assert result.delta is None


@pytest.mark.parametrize(
    ("outcome", "classification"),
    (
        (
            LongitudinalOutcome.INCOMPATIBLE,
            RepeatabilityClassification.INCOMPATIBLE,
        ),
        (
            LongitudinalOutcome.UNKNOWN,
            RepeatabilityClassification.UNKNOWN,
        ),
        (
            LongitudinalOutcome.REQUIRES_REANALYSIS,
            RepeatabilityClassification.REQUIRES_REANALYSIS,
        ),
        (
            LongitudinalOutcome.REGISTERED_BRIDGE,
            RepeatabilityClassification.REGISTERED_BRIDGE,
        ),
    ),
)
def test_noneligible_d03_states_remain_distinct_and_suppressed(
    outcome, classification
) -> None:
    anchor = _record("1")
    member = (
        _record("2", unknown=ComparisonDimension.ASSAY_PROTOCOL)
        if outcome == LongitudinalOutcome.UNKNOWN
        else _record("2", changed=ComparisonDimension.ASSAY_PROTOCOL)
    )
    member_value = member.comparison_key.dimensions[0]
    allowances = (
        None
        if outcome in {LongitudinalOutcome.INCOMPATIBLE, LongitudinalOutcome.UNKNOWN}
        else {ComparisonDimension.ASSAY_PROTOCOL: (member_value, outcome)}
    )
    policy = _policy(anchor, allowances)
    decision = _decide(anchor, member, policy)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
    )

    assert decision.outcome == outcome
    assert result.classification == classification
    assert result.delta is None
    assert not result.trend_allowed


def test_qualified_method_drift_is_outside_repeatability_evidence_identity() -> None:
    anchor = _record("1")
    member = _record("2", changed=ComparisonDimension.MEASUREMENT_DEFINITION)
    changed = member.comparison_key.dimensions[
        list(ComparisonDimension).index(ComparisonDimension.MEASUREMENT_DEFINITION)
    ]
    policy = _policy(
        anchor,
        {
            ComparisonDimension.MEASUREMENT_DEFINITION: (
                changed,
                LongitudinalOutcome.QUALIFIED_COMPATIBLE,
            )
        },
    )
    decision = _decide(anchor, member, policy)

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
    )

    assert decision.outcome == LongitudinalOutcome.QUALIFIED_COMPATIBLE
    assert result.reason_codes == (RepeatabilityReason.MEASUREMENT_IDENTITY_MISMATCH,)
    assert result.delta is None


def test_observation_subclass_cannot_run_hooks_after_d03_replay() -> None:
    anchor = _record("1")
    member = _record("2", changed=ComparisonDimension.ASSAY_PROTOCOL)
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        assert decision.outcome == LongitudinalOutcome.INCOMPATIBLE
        member_observation = _observation(active_member, 0.55)

        class CallerObservation(ComparisonObservation):
            calls: ClassVar[int] = 0

            def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
                type(self).calls += 1
                object.__setattr__(decision, "outcome", LongitudinalOutcome.EQUIVALENT)
                return super().model_dump(*args, **kwargs)

            def __eq__(self, other: object) -> bool:
                del other
                type(self).calls += 1
                object.__setattr__(decision, "outcome", LongitudinalOutcome.EQUIVALENT)
                return True

        poisoned = CallerObservation.model_validate_json(
            canonical_contract_bytes(member_observation)
        )
        CallerObservation.calls = 0
        result = compare_repeatability(
            active_anchor,
            active_member,
            policy,
            decision,
            _observation(active_anchor, 0.5),
            poisoned,
            envelope,
            **arguments,
        )
    assert CallerObservation.calls == 0
    assert decision.outcome == LongitudinalOutcome.INCOMPATIBLE
    assert result.availability == ComparisonAvailability.UNAVAILABLE
    assert result.delta is None
    assert not result.trend_allowed


def test_receipt_relabel_invalidates_authority_signature() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)

    def relabel(_anchor, _member, anchor_observation, member_observation):
        receipt = member_observation.receipt.model_copy(
            update={"receipt_id": "measurement_receipt_relabelled"}
        )
        return (
            anchor_observation,
            member_observation.model_copy(update={"receipt": receipt}),
        )

    result = _compare(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor),
        observation_mutator=relabel,
    )
    assert result.reason_codes == (RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID,)
    assert result.delta is None


def test_live_authority_is_replayed_again_after_signature_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _record("1")
    member = _record("2")
    unrelated = _record("3")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    original_verify = repeatability_module.verify_signature
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        calls = 0

        def verify_then_mutate(*args: object, **kwargs: object) -> None:
            nonlocal calls
            original_verify(*args, **kwargs)
            calls += 1
            if calls == 1:
                assert unrelated.authorized_linkage is not None
                store.commit_authorized_revision(unrelated.authorized_linkage)

        monkeypatch.setattr(
            repeatability_module, "verify_signature", verify_then_mutate
        )
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            compare_repeatability(
                active_anchor,
                active_member,
                policy,
                decision,
                _observation(active_anchor, 0.5),
                _observation(active_member, 0.55),
                envelope,
                **arguments,
            )
    assert calls == 2


@pytest.mark.parametrize("mutation_point", ("post_replay", "result_entry"))
def test_authority_fence_blocks_cross_store_commit_through_artifact_construction(
    monkeypatch: pytest.MonkeyPatch,
    mutation_point: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    unrelated = _record("3")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    original_replay = repeatability_module.replay_longitudinal_member_decision
    original_result = repeatability_module._result
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        second_store = ProviderLinkageStore(
            store.root,
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            time_source=AuthorityTimeSource.fixed(NOW),
        )
        writer_started = threading.Event()
        writer_done = threading.Event()
        writer_errors: list[BaseException] = []
        writer: threading.Thread | None = None

        def commit_unrelated() -> None:
            try:
                writer_started.set()
                assert unrelated.authorized_linkage is not None
                second_store.commit_authorized_revision(unrelated.authorized_linkage)
            except BaseException as error:
                writer_errors.append(error)
            finally:
                writer_done.set()

        def start_blocked_writer() -> None:
            nonlocal writer
            if writer is not None:
                return
            writer = threading.Thread(target=commit_unrelated)
            writer.start()
            assert writer_started.wait(5)
            assert not writer_done.wait(0.2)

        replay_calls = 0

        def replay_with_barrier(*args: object, **kwargs: object):
            nonlocal replay_calls
            replayed = original_replay(*args, **kwargs)
            replay_calls += 1
            if mutation_point == "post_replay" and replay_calls == 2:
                start_blocked_writer()
            return replayed

        def result_with_barrier(*args: object, **kwargs: object):
            if mutation_point == "result_entry" and kwargs.get("available") is True:
                start_blocked_writer()
            return original_result(*args, **kwargs)

        monkeypatch.setattr(
            repeatability_module,
            "replay_longitudinal_member_decision",
            replay_with_barrier,
        )
        monkeypatch.setattr(repeatability_module, "_result", result_with_barrier)
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        result = compare_repeatability(
            active_anchor,
            active_member,
            policy,
            decision,
            _observation(active_anchor, 0.5),
            _observation(active_member, 0.55),
            envelope,
            **arguments,
        )
        assert writer is not None
        assert writer_done.wait(5)
        writer.join(5)
        second_store.close()
        assert not writer_errors
        assert result.availability == ComparisonAvailability.AVAILABLE
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            original_replay(
                decision,
                active_anchor,
                active_member,
                policy,
                expected_policy_sha256=arguments["expected_policy_sha256"],
                expected_authority_head_sha256=arguments[
                    "expected_authority_head_sha256"
                ],
                expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                    "expected_linkage_trust_snapshot_sha256_by_provider"
                ],
                linkage_store=store,
            )


def test_result_trust_requires_independent_pin() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    attacker_key = generate_development_keypair(KeyPurpose.RESULT)
    attacker_document = DevelopmentTrustDocument.model_validate_json(
        development_trust_bytes(attacker_key)
    )
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(
            policy,
            store,
            envelope,
            result_trust_document=attacker_document,
        )
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        with pytest.raises(ValueError, match="independent pin"):
            compare_repeatability(
                active_anchor,
                active_member,
                policy,
                decision,
                _observation(active_anchor, 7.0, signing_key=attacker_key),
                _observation(active_member, 7.05, signing_key=attacker_key),
                envelope,
                **arguments,
            )


def test_unverified_sensitive_key_id_never_enters_output() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        member_observation = _observation(active_member, 0.55)
        poisoned_signature = member_observation.receipt.signature.model_copy(
            update={"key_id": "patient_private_key"}
        )
        poisoned_receipt = member_observation.receipt.model_copy(
            update={"signature": poisoned_signature}
        )
        result = compare_repeatability(
            active_anchor,
            active_member,
            policy,
            decision,
            _observation(active_anchor, 0.5),
            member_observation.model_copy(update={"receipt": poisoned_receipt}),
            envelope,
            **arguments,
        )
    assert result.reason_codes == (RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID,)
    assert result.measurement_signing_key_ids == ()
    assert b"patient_private_key" not in canonical_contract_bytes(result)


@pytest.mark.parametrize(
    "private_key_id",
    (
        "patient_private_key",
        "donor_private_key",
        "path_private_key",
        "patient%5fprivate%5fkey",
    ),
)
def test_comparison_parser_and_hash_reject_private_signing_key_ids(
    private_key_id: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    outside = _compare(
        anchor,
        member,
        policy,
        _decide(anchor, member, policy),
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor, limit=0.01),
    )
    tampered = outside.model_copy(
        update={"measurement_signing_key_ids": (private_key_id,)}
    )
    with pytest.raises(ValueError, match="measurement_signing_key_ids"):
        repeatability_comparison_sha256(tampered)


def test_caller_timezone_is_zero_hook_rejected_before_authority() -> None:
    class CallerTimezone(tzinfo):
        calls = 0

        def utcoffset(self, value: object) -> timedelta:
            del value
            type(self).calls += 1
            return timedelta(0)

        def dst(self, value: object) -> timedelta:
            del value
            type(self).calls += 1
            return timedelta(0)

    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    hostile_time = NOW.replace(tzinfo=CallerTimezone())
    CallerTimezone.calls = 0
    with pytest.raises(ValueError, match="trusted timezone-aware UTC"):
        _compare(
            anchor,
            member,
            policy,
            decision,
            _observation(anchor, 0.5),
            _observation(member, 0.55),
            _envelope(anchor),
            evaluated_at=hostile_time,
        )
    assert CallerTimezone.calls == 0


def test_d03_outcome_partition_is_exhaustive_and_positive_only() -> None:
    eligible = {
        LongitudinalOutcome.EQUIVALENT,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
    }
    explicitly_suppressed = {
        LongitudinalOutcome.INCOMPATIBLE,
        LongitudinalOutcome.UNKNOWN,
        LongitudinalOutcome.REQUIRES_REANALYSIS,
        LongitudinalOutcome.REGISTERED_BRIDGE,
    }
    assert eligible.isdisjoint(explicitly_suppressed)
    assert eligible | explicitly_suppressed == set(LongitudinalOutcome)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("anchor_value", 0.5),
        ("member_value", 0.55),
        ("delta", 0.05),
        ("anchor_uncertainty_lower", 0.48),
        ("anchor_uncertainty_upper", 0.52),
        ("member_uncertainty_lower", 0.53),
        ("member_uncertainty_upper", 0.57),
        ("anchor_denominator_count", 100),
        ("member_denominator_count", 100),
        ("maximum_absolute_delta", 0.01),
    ),
)
def test_unavailable_comparison_rejects_every_partial_numeric_field(
    field: str,
    value: float,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    outside = _compare(
        anchor,
        member,
        policy,
        _decide(anchor, member, policy),
        _observation(anchor, 0.5),
        _observation(member, 0.55),
        _envelope(anchor, limit=0.01),
    )
    assert outside.availability == ComparisonAvailability.UNAVAILABLE
    with pytest.raises(ValueError, match="all present or all suppressed"):
        repeatability_comparison_sha256(outside.model_copy(update={field: value}))


def test_result_trust_key_count_is_bounded_before_graph_traversal() -> None:
    oversized = RESULT_TRUST_DOCUMENT.model_copy(
        update={"keys": RESULT_TRUST_DOCUMENT.keys * 33}
    )
    with pytest.raises(ValueError, match="exact bound"):
        result_trust_document_sha256(oversized)


def test_maximum_valid_unique_result_trust_document_replays() -> None:
    template = RESULT_TRUST_DOCUMENT.keys[0]
    document = RESULT_TRUST_DOCUMENT.model_copy(
        update={
            "keys": tuple(
                template.model_copy(update={"key_id": f"dev-result-{index:024x}"})
                for index in range(32)
            )
        }
    )

    assert result_trust_document_sha256(document)


def test_oversized_contract_collections_reject_before_graph_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _record("1")
    member = _record("2")
    observation = _observation(anchor, 0.5)
    oversized_evidence = observation.evidence.model_copy(
        update={"conditions": observation.evidence.conditions * 25_000}
    )
    oversized_observation = observation.model_copy(
        update={"evidence": oversized_evidence}
    )
    envelope = _envelope(anchor)
    oversized_envelope = envelope.model_copy(
        update={"factor_envelopes": envelope.factor_envelopes * 25_000}
    )
    outside = _compare(
        anchor,
        member,
        _policy(anchor),
        _decide(anchor, member, _policy(anchor)),
        observation,
        _observation(member, 0.55),
        _envelope(anchor, limit=0.01),
    )
    oversized_comparisons = (
        outside.model_copy(update={"reason_codes": outside.reason_codes * 100_000}),
        outside.model_copy(
            update={
                "measurement_signing_key_ids": outside.measurement_signing_key_ids
                * 100_000
            }
        ),
        outside.model_copy(update={"factor_transition_sha256s": ("f" * 64,) * 100_000}),
    )
    graph_calls = 0

    def unexpected_graph_traversal(root: object) -> bool:
        nonlocal graph_calls
        del root
        graph_calls += 1
        return True

    monkeypatch.setattr(
        repeatability_module,
        "_contract_graph_is_trusted",
        unexpected_graph_traversal,
    )
    oversized_contracts = (
        (MeasurementEvidencePayload, oversized_evidence),
        (ComparisonObservation, oversized_observation),
        (RepeatabilityEnvelope, oversized_envelope),
        *((type(outside), item) for item in oversized_comparisons),
    )
    for model, contract in oversized_contracts:
        with pytest.raises(ValueError, match="exact bound"):
            repeatability_module._replay_contract(model, contract)
    assert graph_calls == 0


def test_oversized_contract_graph_values_reject_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation(_record("1"), 0.5)
    oversized_signature = observation.receipt.signature.model_copy(
        update={"signature_base64": "A" * 10_000_000}
    )
    oversized_receipt = observation.receipt.model_copy(
        update={"signature": oversized_signature}
    )
    assert observation.evidence.denominator is not None
    oversized_denominator = observation.evidence.denominator.model_copy(
        update={"total_count": 1 << 4_096}
    )
    oversized_integer = observation.evidence.model_copy(
        update={"denominator": oversized_denominator}
    )
    oversized_unexpected_tuple = observation.receipt.model_copy(
        update={"receipt_id": ("x",) * 100_000}
    )

    class PoisonStateKey:
        hash_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            return 1

    oversized_state = observation.receipt.model_copy()
    poison_key = PoisonStateKey()
    state = object.__getattribute__(oversized_state, "__dict__")
    state[poison_key] = None
    poison_key.hash_calls = 0
    equal_length_state = observation.receipt.model_copy()
    equal_poison_key = PoisonStateKey()
    equal_state = object.__getattribute__(equal_length_state, "__dict__")
    equal_state.pop("receipt_id")
    equal_state[equal_poison_key] = None
    equal_poison_key.hash_calls = 0
    deeply_nested: object = "x"
    for _ in range(100_000):
        deeply_nested = (deeply_nested,)
    oversized_depth = observation.receipt.model_copy(
        update={"receipt_id": deeply_nested}
    )
    alias_dag: object = "x"
    for _ in range(4):
        alias_dag = (alias_dag,) * 32
    oversized_alias_dag = observation.receipt.model_copy(
        update={"receipt_id": alias_dag}
    )
    cyclic = observation.receipt.model_copy()
    cyclic_state = object.__getattribute__(cyclic, "__dict__")
    cyclic_state["signature"] = cyclic
    original_graph_check = repeatability_module._contract_graph_is_trusted
    graph_calls = 0
    serializer_calls = 0

    def counted_graph_check(root: object) -> bool:
        nonlocal graph_calls
        graph_calls += 1
        return original_graph_check(root)

    class UnexpectedSerializer:
        def to_python(self, *args: object, **kwargs: object) -> object:
            nonlocal serializer_calls
            del args, kwargs
            serializer_calls += 1
            raise AssertionError("oversized primitive reached serialization")

    monkeypatch.setattr(
        repeatability_module,
        "_contract_graph_is_trusted",
        counted_graph_check,
    )
    serializer = UnexpectedSerializer()
    for model, contract in (
        (MeasurementEvidenceReceipt, oversized_receipt),
        (MeasurementEvidencePayload, oversized_integer),
        (MeasurementEvidenceReceipt, oversized_unexpected_tuple),
        (MeasurementEvidenceReceipt, oversized_state),
        (MeasurementEvidenceReceipt, equal_length_state),
        (MeasurementEvidenceReceipt, oversized_depth),
        (MeasurementEvidenceReceipt, oversized_alias_dag),
        (MeasurementEvidenceReceipt, cyclic),
    ):
        with pytest.raises(TypeError, match="contract graph"):
            repeatability_module._exact_contract_bytes(contract, model, serializer)
    assert graph_calls == 8
    assert serializer_calls == 0
    assert poison_key.hash_calls == 0
    assert equal_poison_key.hash_calls == 0


def test_every_member_is_compared_to_anchor_not_adjacent_member() -> None:
    anchor = _record("1")
    middle = _record("2")
    last = _record("3")
    policy = _policy(anchor)
    envelope = _envelope(anchor)

    middle_result = _compare(
        anchor,
        middle,
        policy,
        _decide(anchor, middle, policy),
        _observation(anchor, 0.0),
        _observation(middle, 0.08),
        envelope,
    )
    last_result = _compare(
        anchor,
        last,
        policy,
        _decide(anchor, last, policy),
        _observation(anchor, 0.0),
        _observation(last, 0.16),
        envelope,
    )

    assert middle_result.availability == ComparisonAvailability.AVAILABLE
    assert last_result.classification == RepeatabilityClassification.OUTSIDE_ENVELOPE
    assert last_result.delta is None


def test_contracts_are_canonical_bounded_and_private_safe() -> None:
    anchor = _record("1")
    envelope = _envelope(anchor)
    encoded = canonical_contract_bytes(envelope)

    assert encoded == canonical_contract_bytes(envelope)
    assert RepeatabilityEnvelope.model_validate_json(encoded) == envelope
    assert len(encoded) < 8_192
    assert json.loads(encoded)["factor_envelopes"][0]["factor"] == "between_day"
    with pytest.raises(ValidationError, match="reserved privacy term"):
        RepeatabilityEnvelope.model_validate(
            {
                **envelope.model_dump(mode="json"),
                "evidence_id": "repeatability_patient_alpha",
            }
        )
    with pytest.raises(ValidationError, match="every factor in order"):
        RepeatabilityEnvelope.model_validate(
            {
                **envelope.model_dump(),
                "factor_envelopes": tuple(reversed(envelope.factor_envelopes)),
            }
        )
    observation = _observation(anchor, 0.5)
    with pytest.raises(ValidationError, match="missing draw cannot contain numeric"):
        MeasurementEvidencePayload.model_validate(
            {
                **observation.evidence.model_dump(),
                "state": ObservationState.MISSING_DRAW,
            }
        )
    payload = envelope.model_dump(mode="json")
    del payload["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        RepeatabilityEnvelope.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RepeatabilityEnvelope.model_validate_json(
            json.dumps({**envelope.model_dump(mode="json"), "local_path": "/private"})
        )


def test_naive_evaluation_time_is_rejected() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    decision = _decide(anchor, member, policy)
    with pytest.raises(ValueError, match="timezone-aware"):
        _compare(
            anchor,
            member,
            policy,
            decision,
            _observation(anchor, 0.5),
            _observation(member, 0.55),
            _envelope(anchor),
            evaluated_at=NOW.replace(tzinfo=None),
        )


def test_in_fence_variant_requires_this_threads_held_fence_and_matches() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        inputs = (
            active_anchor,
            active_member,
            policy,
            decision,
            _observation(active_anchor, 0.5),
            _observation(active_member, 0.55),
            envelope,
        )
        expected = compare_repeatability(*inputs, **arguments)
        assert expected.availability == ComparisonAvailability.AVAILABLE

        with pytest.raises(LongitudinalDecisionReplayError, match="held live"):
            compare_repeatability_in_fence(*inputs, **arguments)

        # A nested fence cannot be opened, which is why the variant exists.
        with ProviderLinkageStore.authority_read_fence(store):
            with pytest.raises(Exception, match="idle connection"):
                compare_repeatability(*inputs, **arguments)
        with ProviderLinkageStore.authority_read_fence(store):
            fenced = compare_repeatability_in_fence(*inputs, **arguments)
        assert canonical_contract_bytes(fenced) == canonical_contract_bytes(expected)

        # Another thread's fence is not this caller's fence.
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with ProviderLinkageStore.authority_read_fence(store):
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert held.wait(5)
            with pytest.raises(LongitudinalDecisionReplayError, match="held live"):
                compare_repeatability_in_fence(*inputs, **arguments)
        finally:
            release.set()
            holder.join(5)


def test_in_fence_variant_checks_the_fence_before_any_unavailable_result() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = _direct_arguments(policy, store, envelope)
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=arguments["expected_policy_sha256"],
            expected_authority_head_sha256=arguments["expected_authority_head_sha256"],
            expected_linkage_trust_snapshot_sha256_by_provider=arguments[
                "expected_linkage_trust_snapshot_sha256_by_provider"
            ],
            linkage_store=store,
        )
        # A missing envelope returns before the final fenced block, so only the
        # entry check can refuse it.
        inputs = (
            active_anchor,
            active_member,
            policy,
            decision,
            _observation(active_anchor, 0.5),
            _observation(active_member, 0.55),
            None,
        )
        with pytest.raises(LongitudinalDecisionReplayError, match="held live"):
            compare_repeatability_in_fence(*inputs, **arguments)

        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with ProviderLinkageStore.authority_read_fence(store):
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            assert held.wait(5)
            with pytest.raises(LongitudinalDecisionReplayError, match="held live"):
                compare_repeatability_in_fence(*inputs, **arguments)
            assert not release.is_set()
        finally:
            timer.cancel()
            release.set()
            holder.join(5)
        with ProviderLinkageStore.authority_read_fence(store):
            unavailable = compare_repeatability_in_fence(*inputs, **arguments)
        assert unavailable.reason_codes == (RepeatabilityReason.EVIDENCE_MISSING,)
