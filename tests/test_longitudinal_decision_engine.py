"""D03 explanation, action, nontransitivity, and replay regressions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from typing import ClassVar

import pytest
from pydantic import ValidationError

import evidence_inspector.longitudinal_compatibility as longitudinal_module
from evidence_inspector.longitudinal_compatibility import (
    ALL_COMPARISON_DIMENSIONS,
    ComparisonDimension,
    ComparisonDimensionValue,
    DimensionDecisionDisposition,
    DimensionValueState,
    LegacyLongitudinalMemberDecisionV2,
    LegacyLongitudinalSeriesDecisionV2,
    LongitudinalAnchorPolicy,
    LongitudinalDecisionReplayError,
    LongitudinalMemberDecision,
    LongitudinalNextAction,
    LongitudinalOutcome,
    LongitudinalReason,
    LongitudinalRecord,
    LongitudinalSeriesDecision,
    decide_longitudinal_member,
    decide_longitudinal_series,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
    replay_longitudinal_member_decision,
    replay_longitudinal_series_decision,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
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
    with pytest.raises(ValidationError, match="member-decision.v3"):
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


def test_replay_rejects_arbitrary_and_subclass_inputs_without_equality_hooks() -> None:
    class EqualityTrap:
        calls = 0

        def __ne__(self, other: object) -> bool:
            del other
            type(self).calls += 1
            return False

    class DecisionSubclass(LongitudinalMemberDecision):
        calls: ClassVar[int] = 0

        def __ne__(self, other: object) -> bool:
            del other
            type(self).calls += 1
            return False

    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        decision = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        subclass = DecisionSubclass.model_validate_json(
            canonical_contract_bytes(decision)
        )
        for untrusted in (EqualityTrap(), subclass):
            with pytest.raises(LongitudinalDecisionReplayError, match="canonical"):
                replay_longitudinal_member_decision(
                    untrusted,  # type: ignore[arg-type]
                    active_anchor,
                    active_member,
                    policy,
                    **arguments,
                )
    assert EqualityTrap.calls == 0
    assert DecisionSubclass.calls == 0


def test_replay_uses_pinned_internal_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        decision = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        calls = 0

        def forged(*args: object, **kwargs: object) -> LongitudinalMemberDecision:
            nonlocal calls
            del args, kwargs
            calls += 1
            return decision

        monkeypatch.setattr(longitudinal_module, "decide_longitudinal_member", forged)
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                decision,
                active_anchor,
                active_member,
                policy,
                **{**arguments, "linkage_store": None},
            )
    assert calls == 0


@pytest.mark.parametrize("entrypoint", ("member", "series"))
@pytest.mark.parametrize("poisoned_input", ("anchor", "policy"))
def test_caller_model_dump_shadow_never_executes_inside_authority_window(
    entrypoint: str,
    poisoned_input: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        expected_policy_sha256 = longitudinal_anchor_policy_sha256(policy)
        calls = 0

        def poisoned_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            del args, kwargs
            calls += 1
            store.close()
            return {}

        if poisoned_input == "anchor":
            object.__setattr__(active_anchor, "model_dump", poisoned_model_dump)
        else:
            object.__setattr__(policy, "model_dump", poisoned_model_dump)
        arguments = {
            "expected_policy_sha256": expected_policy_sha256,
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        if entrypoint == "member":
            decision = decide_longitudinal_member(
                active_anchor, active_member, policy, **arguments
            )
        else:
            decision = decide_longitudinal_series(
                active_anchor, (active_member,), policy, **arguments
            ).decisions[0]
    assert calls == 0
    assert decision.outcome == LongitudinalOutcome.EQUIVALENT
    assert decision.delta_allowed


@pytest.mark.parametrize("entrypoint", ("member", "series"))
def test_live_authority_change_during_evaluation_fails_whole_result_closed(
    entrypoint: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    third = _record("3")
    late = _record("4")
    policy = _policy(anchor)
    selected = (anchor, member) if entrypoint == "member" else (anchor, member, third)
    with _activated_records(*selected) as (records, store):
        active_anchor = records[0]
        active_members = records[1:]
        triggered = False

        def trace(frame: object, event: str, arg: object):
            nonlocal triggered
            del arg
            if (
                not triggered
                and event == "line"
                and getattr(frame, "f_code", None)
                is longitudinal_module._evaluate_longitudinal_member.__code__
                and "anchor_key_sha256" in frame.f_locals  # type: ignore[attr-defined]
            ):
                triggered = True
                assert late.authorized_linkage is not None
                store.commit_authorized_revision(late.authorized_linkage)
            return trace

        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        sys.settrace(trace)
        try:
            if entrypoint == "member":
                decision = decide_longitudinal_member(
                    active_anchor, active_members[0], policy, **arguments
                )
            else:
                series = decide_longitudinal_series(
                    active_anchor, active_members, policy, **arguments
                )
                decision = series.decisions[0]
        finally:
            sys.settrace(None)
        assert triggered
        assert store.active_snapshot().state_version == len(selected) + 1
        assert decision.outcome == LongitudinalOutcome.EQUIVALENT
        assert decision.linkage_snapshot_state_version == len(selected)
        if entrypoint == "member":
            with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
                replay_longitudinal_member_decision(
                    decision, active_anchor, active_members[0], policy, **arguments
                )
        else:
            with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
                replay_longitudinal_series_decision(
                    series, active_anchor, active_members, policy, **arguments
                )


@pytest.mark.parametrize(
    "pin_name",
    ("policy", "authority", "provider", "trust"),
)
@pytest.mark.parametrize("kind", ("bytes", "subclass"))
def test_external_authority_pins_require_exact_strings_without_hooks(
    pin_name: str,
    kind: str,
) -> None:
    class HookedStr(str):
        calls = 0

        def __hash__(self) -> int:
            type(self).calls += 1
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            type(self).calls += 1
            return super().__eq__(other)

        def encode(self, *args: object, **kwargs: object) -> bytes:
            type(self).calls += 1
            return super().encode(*args, **kwargs)  # type: ignore[arg-type]

    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        policy_sha256: object = longitudinal_anchor_policy_sha256(policy)
        authority_head: object = HEAD_SHA256
        provider: object = PROVIDER
        trust: object = TRUST_SHA256
        original = {
            "policy": policy_sha256,
            "authority": authority_head,
            "provider": provider,
            "trust": trust,
        }[pin_name]
        poisoned = (
            str(original).encode("ascii")
            if kind == "bytes"
            else HookedStr(str(original))
        )
        if pin_name == "policy":
            policy_sha256 = poisoned
        elif pin_name == "authority":
            authority_head = poisoned
        elif pin_name == "provider":
            provider = poisoned
        else:
            trust = poisoned
        pins = {provider: trust}
        HookedStr.calls = 0
        decision = decide_longitudinal_member(
            active_anchor,
            active_member,
            policy,
            expected_policy_sha256=policy_sha256,  # type: ignore[arg-type]
            expected_authority_head_sha256=authority_head,  # type: ignore[arg-type]
            expected_linkage_trust_snapshot_sha256_by_provider=pins,  # type: ignore[arg-type]
            linkage_store=store,
        )
    assert HookedStr.calls == 0
    assert decision.anchor_result_id == "result_invalid_input"
    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    assert not decision.delta_allowed


def test_series_parse_rejects_decisions_from_different_anchor_snapshots() -> None:
    anchor = _record("1")
    member = _record("2")
    later_member = _record("3")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        first = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        assert later_member.authorized_linkage is not None
        store.commit_authorized_revision(later_member.authorized_linkage)
        snapshot = store.active_snapshot()
        receipts = {item.linkage_id: item for item in snapshot.receipts}
        refreshed_anchor = active_anchor.model_copy(
            update={"activation_receipt": receipts[anchor.linkage_revision.linkage_id]}
        )
        refreshed_member = later_member.model_copy(
            update={
                "activation_receipt": receipts[later_member.linkage_revision.linkage_id]
            }
        )
        second = decide_longitudinal_member(
            refreshed_anchor, refreshed_member, policy, **arguments
        )
        assert first.outcome == second.outcome == LongitudinalOutcome.EQUIVALENT
        assert first.anchor_record_sha256 != second.anchor_record_sha256
        assert (
            first.anchor_linkage_receipt_sha256 != second.anchor_linkage_receipt_sha256
        )
        with pytest.raises(ValidationError, match="one exact anchor"):
            LongitudinalSeriesDecision(
                schema_version="traceback.longitudinal-series-decision.v3",
                anchor_result_id=first.anchor_result_id,
                anchor_result_sha256=first.anchor_result_sha256,
                anchor_bundle_sha256=first.anchor_bundle_sha256,
                anchor_record_sha256=first.anchor_record_sha256,
                anchor_key_sha256=first.anchor_key_sha256,
                anchor_linkage_revision_sha256=(first.anchor_linkage_revision_sha256),
                anchor_linkage_receipt_sha256=first.anchor_linkage_receipt_sha256,
                authority_head_sha256=first.authority_head_sha256,
                authority_revision=first.authority_revision,
                policy_sha256=first.policy_sha256,
                linkage_snapshot_state_version=snapshot.state_version,
                linkage_snapshot_state_head_sha256=snapshot.state_head_sha256,
                linkage_snapshot_sha256=hashlib.sha256(
                    canonical_contract_bytes(snapshot)
                ).hexdigest(),
                linkage_receipts=snapshot.receipts,
                member_result_ids=(
                    first.member_result_id,
                    second.member_result_id,
                ),
                decisions=(first, second),
                decision_sha256s=(
                    longitudinal_member_decision_sha256(first),
                    longitudinal_member_decision_sha256(second),
                ),
            )


def test_unsupported_engine_version_fails_closed() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor).model_copy(update={"engine_version": "999.0.0"})
    with _activated_records(anchor, member) as (records, store):
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
    assert LongitudinalReason.POLICY_IDENTITY_INVALID in decision.reason_codes
    assert not decision.delta_allowed


def test_series_snapshot_work_is_bounded_not_per_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _record("1")
    members = (_record("2"), _record("3"), _record("4"))
    policy = _policy(anchor)
    with _activated_records(anchor, *members) as (records, store):
        calls = 0
        original = longitudinal_module._validate_store_input

        def counted(store_input: object):
            nonlocal calls
            calls += 1
            return original(store_input)

        monkeypatch.setattr(longitudinal_module, "_validate_store_input", counted)
        series = decide_longitudinal_series(
            records[0],
            records[1:],
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
        )
    assert len(series.decisions) == len(members)
    assert all(
        item.outcome == LongitudinalOutcome.EQUIVALENT for item in series.decisions
    )
    assert calls == 1


def test_series_receipt_membership_uses_one_index_and_one_anchor_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingReceipts(tuple):
        contains_calls = 0

        def __contains__(self, value: object) -> bool:
            type(self).contains_calls += 1
            return super().__contains__(value)

    anchor = _record("1")
    members = (_record("2"), _record("3"), _record("4"))
    policy = _policy(anchor)
    with _activated_records(anchor, *members) as (records, store):
        snapshot = store.active_snapshot()
        counted_snapshot = snapshot.model_copy(
            update={"receipts": CountingReceipts(snapshot.receipts)}
        )
        calls = 0
        original = longitudinal_module._linkage_authority_invalid

        def counted(*args: object, **kwargs: object) -> bool:
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(longitudinal_module, "_linkage_authority_invalid", counted)
        series = longitudinal_module._evaluate_longitudinal_series(
            records[0],
            records[1:],
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
            linkage_snapshot=counted_snapshot,
        )
    assert all(item.delta_allowed for item in series.decisions)
    assert calls == len(members) + 1
    assert CountingReceipts.contains_calls == 0


@pytest.mark.parametrize("member_count", (3, 1_000))
def test_series_snapshot_digest_is_computed_once_independent_of_members(
    monkeypatch: pytest.MonkeyPatch,
    member_count: int,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        snapshot = store.active_snapshot()
        active_anchor, active_member = records
        members = tuple(
            active_member.model_copy(
                update={
                    "measurement": active_member.measurement.model_copy(
                        update={"result_id": f"result_{index:016x}"}
                    )
                }
            )
            for index in range(member_count)
        )
        snapshot_serializations = 0
        original = longitudinal_module.canonical_contract_bytes

        def counted(value: object) -> bytes:
            nonlocal snapshot_serializations
            if type(value) is type(snapshot):
                snapshot_serializations += 1
            return original(value)  # type: ignore[arg-type]

        monkeypatch.setattr(longitudinal_module, "canonical_contract_bytes", counted)
        series = longitudinal_module._evaluate_longitudinal_series(
            active_anchor,
            members,
            policy,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
            expected_authority_head_sha256=HEAD_SHA256,
            expected_linkage_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            linkage_store=store,
            linkage_snapshot=snapshot,
        )
    assert len(series.decisions) == member_count
    assert snapshot_serializations == 1


def test_series_member_count_boundary_and_storeless_order_fail_closed() -> None:
    anchor = _record("1")
    member = _record("2")
    earlier = _record("3")
    policy = _policy(anchor)
    arguments = {
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: TRUST_SHA256},
        "linkage_store": None,
    }
    at_limit = decide_longitudinal_series(
        anchor, (member,) * 1_000, policy, **arguments
    )
    over_limit = decide_longitudinal_series(
        anchor, (member,) * 1_001, policy, **arguments
    )
    unsorted = decide_longitudinal_series(
        anchor, (earlier, member), policy, **arguments
    )
    duplicate = decide_longitudinal_series(
        anchor, (member, member), policy, **arguments
    )
    for rejected in (at_limit, over_limit, unsorted, duplicate):
        assert rejected.anchor_result_id == "result_invalid_input"
        assert rejected.decisions[0].outcome == LongitudinalOutcome.UNKNOWN
        assert not rejected.decisions[0].delta_allowed


def test_series_strict_pins_and_unsupported_engine_fail_closed() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        common = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        invalid_pin = decide_longitudinal_series(
            records[0],
            records[1:],
            policy,
            **{**common, "expected_authority_head_sha256": HEAD_SHA256.encode()},  # type: ignore[arg-type]
        )
        unsupported = policy.model_copy(update={"engine_version": "999.0.0"})
        unsupported_series = decide_longitudinal_series(
            records[0],
            records[1:],
            unsupported,
            **{
                **common,
                "expected_policy_sha256": longitudinal_anchor_policy_sha256(
                    unsupported
                ),
            },
        )
    assert invalid_pin.anchor_result_id == "result_invalid_input"
    assert all(
        item.outcome == LongitudinalOutcome.UNKNOWN
        and LongitudinalReason.POLICY_IDENTITY_INVALID in item.reason_codes
        and not item.delta_allowed
        for item in unsupported_series.decisions
    )


def test_writable_legacy_pin_names_cannot_replace_internal_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        genuine = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        assert genuine.delta_allowed
        monkeypatch.setattr(
            longitudinal_module,
            "_PINNED_MEMBER_EVALUATOR",
            lambda *args, **kwargs: genuine,
            raising=False,
        )
        monkeypatch.setattr(
            longitudinal_module,
            "_PINNED_DECIDE_LONGITUDINAL_MEMBER",
            lambda *args, **kwargs: genuine,
            raising=False,
        )
        missing_store = {**arguments, "linkage_store": None}
        rejected = decide_longitudinal_member(
            active_anchor, active_member, policy, **missing_store
        )
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                genuine,
                active_anchor,
                active_member,
                policy,
                **missing_store,
            )
    assert rejected.outcome == LongitudinalOutcome.UNKNOWN
    assert not rejected.delta_allowed


def test_member_replay_ignores_class_model_dump_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        genuine = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        genuine_payload = LongitudinalMemberDecision.__pydantic_serializer__.to_python(
            genuine, mode="json", exclude_none=False
        )
        tampered = genuine.model_copy(update={"member_result_sha256": "f" * 64})
        calls = 0

        def poisoned_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            del args, kwargs
            calls += 1
            return genuine_payload

        monkeypatch.setattr(
            LongitudinalMemberDecision, "model_dump", poisoned_model_dump
        )
        with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
            replay_longitudinal_member_decision(
                tampered,
                active_anchor,
                active_member,
                policy,
                **arguments,
            )
    assert calls == 0


@pytest.mark.parametrize(
    "contract_type,poisoned_input",
    (
        (LongitudinalRecord, "record"),
        (LongitudinalAnchorPolicy, "policy"),
    ),
)
def test_external_contract_class_model_dump_replacement_never_executes(
    monkeypatch: pytest.MonkeyPatch,
    contract_type: type[object],
    poisoned_input: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        calls = 0

        def poisoned_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            del args, kwargs
            calls += 1
            return {}

        monkeypatch.setattr(contract_type, "model_dump", poisoned_model_dump)
        decision = decide_longitudinal_member(
            records[0],
            records[1],
            policy,
            **arguments,
        )
    assert poisoned_input in {"record", "policy"}
    assert calls == 0
    assert decision.outcome == LongitudinalOutcome.EQUIVALENT
    assert decision.delta_allowed


def test_legacy_v2_member_and_series_envelopes_parse_but_cannot_current_replay() -> (
    None
):
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        current = decide_longitudinal_member(
            records[0], records[1], policy, **arguments
        )
        payload = current.model_dump(mode="json")
        payload["schema_version"] = "traceback.longitudinal-member-decision.v2"
        for field in (
            "linkage_snapshot_state_version",
            "linkage_snapshot_state_head_sha256",
            "linkage_snapshot_sha256",
        ):
            payload.pop(field)
        legacy_member = LegacyLongitudinalMemberDecisionV2.model_validate_json(
            json.dumps(payload)
        )
        legacy_digest = hashlib.sha256(
            canonical_contract_bytes(legacy_member)
        ).hexdigest()
        legacy_series = LegacyLongitudinalSeriesDecisionV2.model_validate_json(
            json.dumps(
                {
                    "schema_version": "traceback.longitudinal-series-decision.v2",
                    "anchor_result_id": legacy_member.anchor_result_id,
                    "policy_sha256": legacy_member.policy_sha256,
                    "member_result_ids": (legacy_member.member_result_id,),
                    "decisions": (legacy_member.model_dump(mode="json"),),
                    "decision_sha256s": (legacy_digest,),
                }
            )
        )
        assert legacy_series.decisions == (legacy_member,)
        with pytest.raises(LongitudinalDecisionReplayError, match="canonical v3"):
            replay_longitudinal_series_decision(
                legacy_series,  # type: ignore[arg-type]
                records[0],
                records[1:],
                policy,
                **arguments,
            )


def test_series_parse_and_replay_reject_tampered_snapshot_binding() -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        series = decide_longitudinal_series(
            records[0], records[1:], policy, **arguments
        )
        assert (
            replay_longitudinal_series_decision(
                series, records[0], records[1:], policy, **arguments
            )
            == series
        )
        payload = series.model_dump(mode="json")
        payload["linkage_snapshot_sha256"] = "f" * 64
        with pytest.raises(ValidationError, match="exact snapshot"):
            LongitudinalSeriesDecision.model_validate_json(json.dumps(payload))
        tampered = series.model_copy(update={"linkage_snapshot_sha256": "f" * 64})
        with pytest.raises(LongitudinalDecisionReplayError, match="canonical v3"):
            replay_longitudinal_series_decision(
                tampered, records[0], records[1:], policy, **arguments
            )


def test_series_serialization_excludes_unrelated_store_linkage_identities() -> None:
    anchor = _record("1")
    member = _record("2")
    unrelated = _record("3", subject_digit="9")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        before = decide_longitudinal_series(
            records[0], records[1:], policy, **arguments
        )
        before_bytes = canonical_contract_bytes(before)
        assert unrelated.authorized_linkage is not None
        store.commit_authorized_revision(unrelated.authorized_linkage)
        snapshot = store.active_snapshot()
        receipts = {receipt.linkage_id: receipt for receipt in snapshot.receipts}
        refreshed = tuple(
            record.model_copy(
                update={
                    "activation_receipt": receipts[record.linkage_revision.linkage_id]
                }
            )
            for record in (anchor, member)
        )
        after = decide_longitudinal_series(
            refreshed[0], refreshed[1:], policy, **arguments
        )
        after_bytes = canonical_contract_bytes(after)
    unrelated_tokens = (
        unrelated.linkage_revision.biological.subject_token,
        unrelated.linkage_revision.biological.collection_token,
        unrelated.linkage_revision.biological.specimen_token,
        unrelated.linkage_revision.technical.analysis_record_id,
    )
    assert len(after.linkage_receipts) == 2
    assert all(token.encode("ascii") not in after_bytes for token in unrelated_tokens)
    member_bytes = canonical_contract_bytes(after.decisions[0])
    assert all(token.encode("ascii") not in member_bytes for token in unrelated_tokens)
    assert abs(len(after_bytes) - len(before_bytes)) < 64


@pytest.mark.parametrize("entrypoint", ("member", "series"))
def test_nested_caller_proxy_cannot_mutate_evaluator_after_authority_capture(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    late = _record("3")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        assert late.authorized_linkage is not None
        store.commit_authorized_revision(late.authorized_linkage)
        authority_started = False
        hook_calls = 0
        genuine_measurement = active_anchor.measurement
        original_validate_store = longitudinal_module._validate_store_input

        def marked_validate_store(store_input: object):
            nonlocal authority_started
            authority_started = True
            return original_validate_store(store_input)

        class MeasurementProxy:
            @property
            def __dict__(self) -> dict[str, object]:
                nonlocal hook_calls
                hook_calls += 1
                assert not authority_started
                monkeypatch.setattr(
                    longitudinal_module,
                    "_linkage_authority_invalid",
                    lambda *args, **kwargs: False,
                )
                return vars(genuine_measurement)

        poisoned_anchor = active_anchor.model_copy()
        object.__setattr__(poisoned_anchor, "measurement", MeasurementProxy())
        monkeypatch.setattr(
            longitudinal_module, "_validate_store_input", marked_validate_store
        )
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        if entrypoint == "member":
            decision = decide_longitudinal_member(
                poisoned_anchor, active_member, policy, **arguments
            )
        else:
            decision = decide_longitudinal_series(
                poisoned_anchor, (active_member,), policy, **arguments
            ).decisions[0]
    assert hook_calls == 0
    assert not authority_started
    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    assert not decision.delta_allowed


@pytest.mark.parametrize("entrypoint", ("member", "series", "replay"))
def test_nested_caller_contract_subclass_is_zero_hook_rejected(
    entrypoint: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store):
        active_anchor, active_member = records
        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store,
        }
        genuine = decide_longitudinal_member(
            active_anchor, active_member, policy, **arguments
        )
        measurement_type = type(active_anchor.measurement)

        class CallerMeasurement(measurement_type):
            calls: ClassVar[int] = 0

            def __getattribute__(self, name: str) -> object:
                type(self).calls += 1
                return super().__getattribute__(name)

        CallerMeasurement.__module__ = "evidence_inspector.caller_supplied"
        forged_measurement = CallerMeasurement.model_validate_json(
            canonical_contract_bytes(active_anchor.measurement)
        )
        poisoned_anchor = active_anchor.model_copy()
        object.__setattr__(poisoned_anchor, "measurement", forged_measurement)
        CallerMeasurement.calls = 0
        if entrypoint == "member":
            rejected = decide_longitudinal_member(
                poisoned_anchor, active_member, policy, **arguments
            )
            assert rejected.outcome == LongitudinalOutcome.UNKNOWN
            assert not rejected.delta_allowed
        elif entrypoint == "series":
            rejected_series = decide_longitudinal_series(
                poisoned_anchor, (active_member,), policy, **arguments
            )
            assert rejected_series.decisions[0].outcome == LongitudinalOutcome.UNKNOWN
            assert not rejected_series.decisions[0].delta_allowed
        else:
            with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
                replay_longitudinal_member_decision(
                    genuine,
                    poisoned_anchor,
                    active_member,
                    policy,
                    **arguments,
                )
    assert CallerMeasurement.calls == 0


def test_preimport_nested_subclass_is_zero_hook_rejected_in_fresh_process() -> None:
    script = r"""
from typing import ClassVar
from evidence_inspector.compatibility import VerifiedMeasurementRecord

class PreloadedCallerMeasurement(VerifiedMeasurementRecord):
    calls: ClassVar[int] = 0
    def __getattribute__(self, name: str) -> object:
        type(self).calls += 1
        return super().__getattribute__(name)

PreloadedCallerMeasurement.__module__ = "evidence_inspector.caller_preloaded"

import evidence_inspector.longitudinal_compatibility as lc
from evidence_inspector.method_registry import canonical_contract_bytes
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256, PROVIDER, TRUST_SHA256, _activated_records, _policy, _record
)

anchor = _record("1")
member = _record("2")
policy = _policy(anchor)
with _activated_records(anchor, member) as (records, store):
    active_anchor, active_member = records
    arguments = {
        "expected_policy_sha256": lc.longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "expected_linkage_trust_snapshot_sha256_by_provider": {PROVIDER: TRUST_SHA256},
        "linkage_store": store,
    }
    genuine = lc.decide_longitudinal_member(
        active_anchor, active_member, policy, **arguments
    )
    forged = PreloadedCallerMeasurement.model_validate_json(
        canonical_contract_bytes(active_anchor.measurement)
    )
    poisoned = active_anchor.model_copy()
    object.__setattr__(poisoned, "measurement", forged)
    PreloadedCallerMeasurement.calls = 0
    member_result = lc.decide_longitudinal_member(
        poisoned, active_member, policy, **arguments
    )
    series_result = lc.decide_longitudinal_series(
        poisoned, (active_member,), policy, **arguments
    )
    replay_failed = False
    try:
        lc.replay_longitudinal_member_decision(
            genuine, poisoned, active_member, policy, **arguments
        )
    except lc.LongitudinalDecisionReplayError:
        replay_failed = True

assert PreloadedCallerMeasurement not in lc._TRUSTED_CONTRACT_TYPES
assert PreloadedCallerMeasurement.calls == 0
assert member_result.outcome == lc.LongitudinalOutcome.UNKNOWN
assert not member_result.delta_allowed
assert series_result.decisions[0].outcome == lc.LongitudinalOutcome.UNKNOWN
assert not series_result.decisions[0].delta_allowed
assert replay_failed
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("entrypoint", ("member", "series"))
def test_second_store_concurrent_commit_is_bound_as_of_and_breaks_replay(
    entrypoint: str,
) -> None:
    anchor = _record("1")
    member = _record("2")
    late = _record("3")
    policy = _policy(anchor)
    with _activated_records(anchor, member) as (records, store_a):
        store_b = ProviderLinkageStore(
            store_a.root,
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
            time_source=AuthorityTimeSource.fixed(NOW),
        )
        writer_started = threading.Event()
        writer_done = threading.Event()
        writer_error: list[BaseException] = []
        triggered = False

        def writer() -> None:
            try:
                writer_started.set()
                assert late.authorized_linkage is not None
                store_b.commit_authorized_revision(late.authorized_linkage)
            except Exception as error:  # noqa: BLE001 - preserve writer failure for main thread
                writer_error.append(error)
            finally:
                writer_done.set()

        def trace(frame: object, event: str, arg: object):
            nonlocal triggered
            del arg
            if (
                not triggered
                and event == "line"
                and getattr(frame, "f_code", None)
                is longitudinal_module._evaluate_longitudinal_member.__code__
                and "anchor_key_sha256" in frame.f_locals  # type: ignore[attr-defined]
            ):
                triggered = True
                threading.Thread(target=writer, daemon=True).start()
                assert writer_started.wait(1)
            return trace

        arguments = {
            "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
            "expected_authority_head_sha256": HEAD_SHA256,
            "expected_linkage_trust_snapshot_sha256_by_provider": {
                PROVIDER: TRUST_SHA256
            },
            "linkage_store": store_a,
        }
        sys.settrace(trace)
        try:
            if entrypoint == "member":
                result = decide_longitudinal_member(
                    records[0], records[1], policy, **arguments
                )
            else:
                result = decide_longitudinal_series(
                    records[0], records[1:], policy, **arguments
                )
        finally:
            sys.settrace(None)
        assert writer_done.wait(5)
        store_b.close()
        assert not writer_error
        assert triggered
        expected_version = 2
        if entrypoint == "member":
            assert result.linkage_snapshot_state_version == expected_version
            with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
                replay_longitudinal_member_decision(
                    result,  # type: ignore[arg-type]
                    records[0],
                    records[1],
                    policy,
                    **arguments,
                )
        else:
            assert result.linkage_snapshot_state_version == expected_version  # type: ignore[union-attr]
            with pytest.raises(LongitudinalDecisionReplayError, match="replay exactly"):
                replay_longitudinal_series_decision(
                    result,  # type: ignore[arg-type]
                    records[0],
                    records[1:],
                    policy,
                    **arguments,
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
