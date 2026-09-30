"""D07 repeatability comparisons remain descriptive and fail closed."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import ExecutionState, InformationState
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalOutcome,
    decide_longitudinal_member,
    longitudinal_anchor_policy_sha256,
    longitudinal_record_sha256,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.repeatability_comparison import (
    ALL_REPEATABILITY_FACTORS,
    ComparisonAvailability,
    ComparisonObservation,
    FactorEnvelope,
    ObservationState,
    RepeatabilityClassification,
    RepeatabilityEnvelope,
    RepeatabilityReason,
    compare_repeatability,
    repeatability_comparison_sha256,
    repeatability_envelope_sha256,
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

EVIDENCE_SHA256 = "b" * 64
PROTOCOL_SHA256 = "c" * 64
AUTHORITY_SHA256 = "e" * 64


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
            FactorEnvelope(factor=factor, maximum_absolute_contribution=0.04)
            for factor in ALL_REPEATABILITY_FACTORS
        ),
        maximum_absolute_delta=limit,
        combination_rule="preapproved_combined_absolute_delta.v1",
    )


def _observation(record, value: float) -> ComparisonObservation:
    return ComparisonObservation(
        record_sha256=longitudinal_record_sha256(record),
        state=ObservationState.AVAILABLE,
        value=value,
        uncertainty_lower=value - 0.02,
        uncertainty_upper=value + 0.02,
        denominator_count=100,
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
    arguments = {
        "evaluated_at": NOW,
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: TRUST_SHA256},
        "expected_envelope_sha256": (
            repeatability_envelope_sha256(envelope)
            if envelope is not None
            else "f" * 64
        ),
        "expected_evidence_sha256": EVIDENCE_SHA256,
        "expected_protocol_sha256": PROTOCOL_SHA256,
        "expected_repeatability_authority_sha256": AUTHORITY_SHA256,
    }
    with _activated_records(anchor, member) as (records, store):
        anchor, member = records
        arguments["linkage_store"] = store
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
        anchor_observation = anchor_observation.model_copy(
            update={"record_sha256": longitudinal_record_sha256(anchor)}
        )
        member_observation = member_observation.model_copy(
            update={"record_sha256": longitudinal_record_sha256(member)}
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
    missing = ComparisonObservation(
        record_sha256=longitudinal_record_sha256(member),
        state=ObservationState.MISSING_DRAW,
        value=None,
        uncertainty_lower=None,
        uncertainty_upper=None,
        denominator_count=None,
    )

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

    assert (
        result.classification
        == RepeatabilityClassification.FAILED_OR_INSUFFICIENT_MEASUREMENT
    )
    assert result.delta is None


@pytest.mark.parametrize(
    ("outcome", "classification"),
    (
        (
            LongitudinalOutcome.INCOMPATIBLE,
            RepeatabilityClassification.INCOMPATIBLE_OR_UNKNOWN,
        ),
        (
            LongitudinalOutcome.REQUIRES_REANALYSIS,
            RepeatabilityClassification.REQUIRES_REANALYSIS_OR_BRIDGE,
        ),
        (
            LongitudinalOutcome.REGISTERED_BRIDGE,
            RepeatabilityClassification.REQUIRES_REANALYSIS_OR_BRIDGE,
        ),
    ),
)
def test_noneligible_d03_states_remain_distinct_and_suppressed(
    outcome, classification
) -> None:
    anchor = _record("1")
    member = _record("2", changed=ComparisonDimension.ASSAY_PROTOCOL)
    member_value = member.comparison_key.dimensions[0]
    allowances = (
        None
        if outcome == LongitudinalOutcome.INCOMPATIBLE
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
    with pytest.raises(ValidationError, match="missing draw cannot contain numeric"):
        ComparisonObservation(
            record_sha256="1" * 64,
            state=ObservationState.MISSING_DRAW,
            value=0.0,
            uncertainty_lower=None,
            uncertainty_upper=None,
            denominator_count=None,
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
