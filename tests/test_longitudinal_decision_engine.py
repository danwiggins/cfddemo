"""D03 explanation, action, nontransitivity, and replay regressions."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evidence_inspector.longitudinal_compatibility import (
    ALL_COMPARISON_DIMENSIONS,
    ComparisonDimension,
    ComparisonDimensionValue,
    DimensionDecisionDisposition,
    DimensionValueState,
    LongitudinalAnchorPolicy,
    LongitudinalDecisionReplayError,
    LongitudinalMemberDecision,
    LongitudinalNextAction,
    LongitudinalOutcome,
    LongitudinalReason,
    LongitudinalSeriesDecision,
    decide_longitudinal_member,
    decide_longitudinal_series,
    longitudinal_anchor_policy_sha256,
    replay_longitudinal_member_decision,
)
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    PROVIDER,
    TRUST_SHA256,
    _activated_records,
    _decide,
    _policy,
    _record,
)


def _changed_value(record, dimension: ComparisonDimension):
    return record.comparison_key.dimensions[ALL_COMPARISON_DIMENSIONS.index(dimension)]


def test_every_outcome_has_one_safe_action_and_strict_rendering_gate() -> None:
    dimension = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    changed = _record("2", changed=dimension)
    changed_value = _changed_value(changed, dimension)
    decisions = {
        LongitudinalOutcome.EQUIVALENT: _decide(anchor, _record("2"), _policy(anchor)),
        LongitudinalOutcome.QUALIFIED_COMPATIBLE: _decide(
            anchor,
            changed,
            _policy(
                anchor,
                {
                    dimension: (
                        changed_value,
                        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
                    )
                },
            ),
        ),
        LongitudinalOutcome.REQUIRES_REANALYSIS: _decide(
            anchor,
            changed,
            _policy(
                anchor,
                {
                    dimension: (
                        changed_value,
                        LongitudinalOutcome.REQUIRES_REANALYSIS,
                    )
                },
            ),
        ),
        LongitudinalOutcome.REGISTERED_BRIDGE: _decide(
            anchor,
            changed,
            _policy(
                anchor,
                {
                    dimension: (
                        changed_value,
                        LongitudinalOutcome.REGISTERED_BRIDGE,
                    )
                },
            ),
        ),
        LongitudinalOutcome.INCOMPATIBLE: _decide(anchor, changed, _policy(anchor)),
        LongitudinalOutcome.UNKNOWN: _decide(
            anchor,
            _record("2", unknown=ComparisonDimension.UNCERTAINTY_METHOD),
            _policy(anchor),
        ),
    }
    expected_actions = {
        LongitudinalOutcome.EQUIVALENT: (LongitudinalNextAction.USE_DIRECT_COMPARISON),
        LongitudinalOutcome.QUALIFIED_COMPATIBLE: (
            LongitudinalNextAction.USE_QUALIFIED_COMPARISON
        ),
        LongitudinalOutcome.REQUIRES_REANALYSIS: (
            LongitudinalNextAction.REQUEST_REANALYSIS
        ),
        LongitudinalOutcome.REGISTERED_BRIDGE: (
            LongitudinalNextAction.REVIEW_REGISTERED_BRIDGE
        ),
        LongitudinalOutcome.INCOMPATIBLE: (
            LongitudinalNextAction.START_SEPARATE_SERIES
        ),
        LongitudinalOutcome.UNKNOWN: (LongitudinalNextAction.RESOLVE_UNKNOWN_INPUTS),
    }
    assert set(decisions) == set(LongitudinalOutcome)
    for outcome, decision in decisions.items():
        assert decision.outcome == outcome
        assert decision.next_action == expected_actions[outcome]
        eligible = outcome in {
            LongitudinalOutcome.EQUIVALENT,
            LongitudinalOutcome.QUALIFIED_COMPATIBLE,
        }
        assert decision.delta_allowed == eligible
        assert decision.connecting_trend_allowed == eligible
        assert decision.bridge_execution_state == "not_executed"


@pytest.mark.parametrize("claimed_outcome", tuple(LongitudinalOutcome))
def test_parse_rejects_top_level_outcome_contradicting_all_explanations(
    claimed_outcome: LongitudinalOutcome,
) -> None:
    dimension = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    member = _record("2", changed=dimension)
    value = _changed_value(member, dimension)
    explanation_outcome = (
        LongitudinalOutcome.REQUIRES_REANALYSIS
        if claimed_outcome == LongitudinalOutcome.QUALIFIED_COMPATIBLE
        else LongitudinalOutcome.QUALIFIED_COMPATIBLE
    )
    decision = _decide(
        anchor,
        member,
        _policy(anchor, {dimension: (value, explanation_outcome)}),
    )
    reasons = {
        LongitudinalOutcome.EQUIVALENT: LongitudinalReason.EXACT_MATCH,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE: (
            LongitudinalReason.QUALIFIED_ENVELOPE
        ),
        LongitudinalOutcome.REQUIRES_REANALYSIS: (
            LongitudinalReason.REANALYSIS_REQUIRED
        ),
        LongitudinalOutcome.REGISTERED_BRIDGE: LongitudinalReason.BRIDGE_AVAILABLE,
        LongitudinalOutcome.INCOMPATIBLE: LongitudinalReason.DISALLOWED_MISMATCH,
        LongitudinalOutcome.UNKNOWN: LongitudinalReason.UNKNOWN_DIMENSION,
    }
    actions = {
        LongitudinalOutcome.EQUIVALENT: LongitudinalNextAction.USE_DIRECT_COMPARISON,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE: (
            LongitudinalNextAction.USE_QUALIFIED_COMPARISON
        ),
        LongitudinalOutcome.REQUIRES_REANALYSIS: (
            LongitudinalNextAction.REQUEST_REANALYSIS
        ),
        LongitudinalOutcome.REGISTERED_BRIDGE: (
            LongitudinalNextAction.REVIEW_REGISTERED_BRIDGE
        ),
        LongitudinalOutcome.INCOMPATIBLE: (
            LongitudinalNextAction.START_SEPARATE_SERIES
        ),
        LongitudinalOutcome.UNKNOWN: LongitudinalNextAction.RESOLVE_UNKNOWN_INPUTS,
    }
    eligible = claimed_outcome in {
        LongitudinalOutcome.EQUIVALENT,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
    }
    payload = decision.model_dump(mode="json")
    payload.update(
        {
            "outcome": claimed_outcome.value,
            "reason_codes": [reasons[claimed_outcome].value],
            "next_action": actions[claimed_outcome].value,
            "delta_allowed": eligible,
            "connecting_trend_allowed": eligible,
            "bridge_refs": [],
        }
    )

    with pytest.raises(
        ValidationError,
        match="explanation aggregation|unknown reason",
    ):
        LongitudinalMemberDecision.model_validate_json(json.dumps(payload))


def test_every_outcome_rejects_reason_set_substitution() -> None:
    dimension = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    member = _record("2", changed=dimension)
    value = _changed_value(member, dimension)
    decisions_and_wrong_reasons = (
        (
            _decide(
                anchor,
                member,
                _policy(
                    anchor,
                    {
                        dimension: (
                            value,
                            LongitudinalOutcome.REQUIRES_REANALYSIS,
                        )
                    },
                ),
            ),
            (LongitudinalReason.BRIDGE_AVAILABLE,),
        ),
        (
            _decide(
                anchor,
                member,
                _policy(
                    anchor,
                    {
                        dimension: (
                            value,
                            LongitudinalOutcome.REGISTERED_BRIDGE,
                        )
                    },
                ),
            ),
            (LongitudinalReason.REANALYSIS_REQUIRED,),
        ),
        (
            _decide(anchor, member, _policy(anchor)),
            (LongitudinalReason.EXACT_MATCH,),
        ),
        (
            _decide(
                anchor,
                _record("2", unknown=ComparisonDimension.UNCERTAINTY_METHOD),
                _policy(anchor),
            ),
            (LongitudinalReason.EXACT_MATCH,),
        ),
    )
    for decision, reasons in decisions_and_wrong_reasons:
        payload = decision.model_dump(mode="json")
        payload["reason_codes"] = [item.value for item in reasons]
        with pytest.raises(ValidationError, match="invalid.*reason set"):
            LongitudinalMemberDecision.model_validate_json(json.dumps(payload))


def test_mixed_subject_and_unknown_reason_sets_are_exactly_controlled() -> None:
    anchor = _record("1")
    subject_mismatch = _decide(
        anchor,
        _record("2", subject_digit="f"),
        _policy(anchor),
    )
    assert subject_mismatch.reason_codes == (
        LongitudinalReason.SUBJECT_LINKAGE_MISMATCH,
    )

    unknown = _decide(
        anchor,
        _record("2", unknown=ComparisonDimension.UNCERTAINTY_METHOD),
        _policy(anchor),
    )
    assert unknown.reason_codes == (LongitudinalReason.UNKNOWN_DIMENSION,)


def test_mismatch_explanation_binds_exact_values_evidence_and_bridge() -> None:
    dimension = ComparisonDimension.ASSAY_PROTOCOL
    anchor = _record("1")
    member = _record("2", changed=dimension)
    member_value = _changed_value(member, dimension)
    decision = _decide(
        anchor,
        member,
        _policy(
            anchor,
            {dimension: (member_value, LongitudinalOutcome.REGISTERED_BRIDGE)},
        ),
    )
    explanation = decision.dimension_explanations[0]
    assert explanation.dimension == dimension
    assert explanation.anchor_value_sha256 != explanation.member_value_sha256
    assert explanation.disposition == (DimensionDecisionDisposition.REGISTERED_BRIDGE)
    assert explanation.evidence_ref == "evidence_assay_protocol_alpha"
    assert explanation.evidence_sha256 == "a" * 64
    assert explanation.bridge_ref == "bridge_assay_protocol_alpha"
    assert decision.bridge_refs == ("bridge_assay_protocol_alpha",)
    assert not decision.delta_allowed


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": "traceback.longitudinal-anchor-policy.v0"}, "literal"),
        ({"schema_version": None}, "schema_version"),
        ({"engine_version": None}, "engine_version"),
        ({"rules": None}, "rules"),
    ],
)
def test_old_or_missing_policy_fields_never_reach_compatibility(
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = _policy(_record("1")).model_dump(mode="json")
    for field, value in mutation.items():
        if value is None:
            payload.pop(field)
        else:
            payload[field] = value
    with pytest.raises(ValidationError, match=message):
        LongitudinalAnchorPolicy.model_validate_json(json.dumps(payload))


def test_v1_or_incomplete_decision_cannot_be_relabelled_as_d03() -> None:
    anchor = _record("1")
    decision = _decide(anchor, _record("2"), _policy(anchor))
    payload = decision.model_dump(mode="json")
    payload["schema_version"] = "traceback.longitudinal-member-decision.v1"
    with pytest.raises(ValidationError, match="member-decision.v2"):
        LongitudinalMemberDecision.model_validate_json(json.dumps(payload))

    payload = decision.model_dump(mode="json")
    payload.pop("dimension_explanations")
    with pytest.raises(ValidationError, match="dimension_explanations"):
        LongitudinalMemberDecision.model_validate_json(json.dumps(payload))

    for field in ("schema_version", "bridge_execution_state"):
        payload = decision.model_dump(mode="json")
        payload.pop(field)
        with pytest.raises(ValidationError, match=field):
            LongitudinalMemberDecision.model_validate_json(json.dumps(payload))

    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        series = decide_longitudinal_series(
            records[0],
            (records[1],),
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
    series_payload = series.model_dump(mode="json")
    series_payload.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        LongitudinalSeriesDecision.model_validate_json(json.dumps(series_payload))


def test_series_is_anchor_direct_and_does_not_inherit_adjacent_compatibility() -> None:
    dimension = ComparisonDimension.ASSAY_PROTOCOL
    anchor = _record("1")
    middle = _record("2", changed=dimension)
    last = _record("3", changed=dimension)
    middle_policy = _policy(
        anchor,
        {
            dimension: (
                _changed_value(middle, dimension),
                LongitudinalOutcome.QUALIFIED_COMPATIBLE,
            )
        },
    )
    adjacent_policy = _policy(
        middle,
        {
            dimension: (
                _changed_value(last, dimension),
                LongitudinalOutcome.QUALIFIED_COMPATIBLE,
            )
        },
    )
    assert _decide(middle, last, adjacent_policy).outcome == (
        LongitudinalOutcome.QUALIFIED_COMPATIBLE
    )
    with _activated_records(anchor, middle, last) as (records, store):
        series = decide_longitudinal_series(
            records[0],
            (records[1], records[2]),
            middle_policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(middle_policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
    assert series.decisions[0].outcome == (LongitudinalOutcome.QUALIFIED_COMPATIBLE)
    assert series.decisions[1].outcome == LongitudinalOutcome.INCOMPATIBLE


def test_exact_e05_e01_and_lineage_authority_replay_rejects_tampering() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    policy_sha256 = longitudinal_anchor_policy_sha256(policy)
    with _activated_records(anchor, member) as (records, store):
        anchor, member = records
        pins = {
            "expected_policy_sha256": policy_sha256,
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        decision = decide_longitudinal_member(anchor, member, policy, **pins)
        assert (
            replay_longitudinal_member_decision(
                decision, anchor, member, policy, **pins
            )
            == decision
        )

        tampered = decision.model_copy(update={"member_result_sha256": "f" * 64})
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                tampered, anchor, member, policy, **pins
            )

        stale_pins = {
            **pins,
            "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: "f" * 64},
        }
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                decision, anchor, member, policy, **stale_pins
            )

        no_store_pins = {**pins, "linkage_store": None}
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                decision, anchor, member, policy, **no_store_pins
            )


def test_explanations_actions_and_bridge_execution_fail_closed_on_tamper() -> None:
    dimension = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    member = _record("2", changed=dimension)
    value = _changed_value(member, dimension)
    bridge = _decide(
        anchor,
        member,
        _policy(
            anchor,
            {dimension: (value, LongitudinalOutcome.REGISTERED_BRIDGE)},
        ),
    )

    payload = bridge.model_dump(mode="json")
    explanation = next(
        item
        for item in payload["dimension_explanations"]
        if item["dimension"] == dimension.value
    )
    explanation.pop("evidence_sha256")
    with pytest.raises(ValidationError, match="registered allowances carry evidence"):
        bridge.__class__.model_validate_json(json.dumps(payload))

    payload = bridge.model_dump(mode="json")
    payload["next_action"] = LongitudinalNextAction.USE_DIRECT_COMPARISON.value
    with pytest.raises(ValidationError, match="next action"):
        bridge.__class__.model_validate_json(json.dumps(payload))

    payload = bridge.model_dump(mode="json")
    payload["bridge_execution_state"] = "executed"
    with pytest.raises(ValidationError, match="not_executed"):
        bridge.__class__.model_validate_json(json.dumps(payload))


def test_mixed_bridge_disposition_is_explained_but_never_actionable() -> None:
    first = ComparisonDimension.ASSAY_PROTOCOL
    second = ComparisonDimension.PREANALYTICS_POLICY
    anchor = _record("1")
    member = _record("2", changed=first)
    values = list(member.comparison_key.dimensions)
    second_index = ALL_COMPARISON_DIMENSIONS.index(second)
    values[second_index] = ComparisonDimensionValue(
        dimension=second,
        state=DimensionValueState.KNOWN,
        identity_id="cmpid_preanalytics_policy_3",
        version="1.0.0",
        content_sha256="3" * 64,
    )
    member = member.model_copy(
        update={
            "comparison_key": member.comparison_key.model_copy(
                update={"dimensions": tuple(values)}
            )
        }
    )
    policy = _policy(
        anchor,
        {
            first: (
                values[0],
                LongitudinalOutcome.REGISTERED_BRIDGE,
            ),
            second: (
                values[second_index],
                LongitudinalOutcome.REQUIRES_REANALYSIS,
            ),
        },
    )
    decision = _decide(anchor, member, policy)
    assert decision.outcome == LongitudinalOutcome.INCOMPATIBLE
    assert decision.reason_codes == (LongitudinalReason.MIXED_DISPOSITIONS,)
    assert decision.next_action == LongitudinalNextAction.START_SEPARATE_SERIES
    assert decision.bridge_refs == ()
    bridge_explanation = next(
        item for item in decision.dimension_explanations if item.dimension == first
    )
    assert bridge_explanation.bridge_ref == "bridge_assay_protocol_alpha"
    assert decision.bridge_execution_state == "not_executed"
