"""D04/D05 append-only supersession and immutable membership tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evidence_inspector.comparison_membership import (
    ComparisonMember,
    ComparisonSetDefinition,
    ComparisonSetLedger,
    ComparisonSetVersion,
    MemberDisposition,
    MemberReason,
    PolicyReference,
    ReplicateRule,
    TimeAxis,
    analysis_supersession_ledger_sha256,
    build_analysis_supersession_ledger,
    comparison_member_id,
    comparison_set_definition_sha256,
    comparison_set_version_sha256,
)
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    LongitudinalOutcome,
    LongitudinalRecord,
    longitudinal_anchor_policy_sha256,
)
from evidence_inspector.provider_linkage import (
    AuthorizedLinkageRevision,
    OptionalLineageState,
    OptionalOpaqueToken,
    ProviderApprovalConsumptionLedger,
    authorize_and_consume_linkage_revision,
)
from tests.test_longitudinal_compatibility import (
    METHOD_SHA256,
    PROVIDER,
    TRUST_SHA256,
    _decide,
    _policy,
    _record,
)
from tests.test_provider_linkage import _consume, _create_approval


def _authorized(record: LongitudinalRecord) -> AuthorizedLinkageRevision:
    assert record.authorized_linkage is not None
    return record.authorized_linkage


def _consumption_ledger(
    records: tuple[AuthorizedLinkageRevision, ...],
) -> ProviderApprovalConsumptionLedger:
    ledger = ProviderApprovalConsumptionLedger()
    for record in records:
        _, ledger = authorize_and_consume_linkage_revision(
            record.revision,
            previous_revision=record.previous_revision,
            approvals=record.approvals,
            trust_snapshot=record.trust_snapshot,
            expected_trust_snapshot_sha256=TRUST_SHA256,
            evaluated_at=record.authorization.evaluated_at,
            consumption_ledger=ledger,
        )
    return ledger


def _reanalysis(
    base: LongitudinalRecord,
    result_digit: str,
) -> LongitudinalRecord:
    successor = _record(result_digit)
    technical = successor.linkage_revision.technical.model_copy(
        update={
            "reanalysis_of": OptionalOpaqueToken(
                state=OptionalLineageState.KNOWN,
                token=base.linkage_revision.technical.analysis_record_id,
            )
        }
    )
    revision = successor.linkage_revision.model_copy(
        update={
            "biological": base.linkage_revision.biological,
            "technical": technical,
        }
    )
    authorized, _ = _consume(
        revision,
        (_create_approval(revision, result_digit),),
    )
    return successor.model_copy(
        update={"linkage_revision": revision, "authorized_linkage": authorized}
    )


def _decision(anchor: LongitudinalRecord, member: LongitudinalRecord):
    return _decide(anchor, member, _policy(anchor))


def _member(
    anchor: LongitudinalRecord,
    record: LongitudinalRecord,
    *,
    timepoint: int,
    replicate: int = 0,
    disposition: MemberDisposition = MemberDisposition.INCLUDED,
    reason: MemberReason = MemberReason.ELIGIBLE,
    decision=None,
) -> ComparisonMember:
    return ComparisonMember(
        member_id=comparison_member_id(record),
        record=record,
        decision=decision or _decision(anchor, record),
        biological_timepoint_index=timepoint,
        technical_replicate_index=replicate,
        disposition=disposition,
        reason=reason,
    )


def _definition(
    anchor: LongitudinalRecord,
    members: tuple[ComparisonMember, ...],
    *,
    replicate_rule: ReplicateRule = ReplicateRule.ONE_ANALYSIS_PER_COLLECTION,
    supersession_records: tuple[AuthorizedLinkageRevision, ...] | None = None,
) -> ComparisonSetDefinition:
    policy = _policy(anchor)
    ledger = build_analysis_supersession_ledger(
        (
            supersession_records
            if supersession_records is not None
            else tuple(_authorized(item.record) for item in members)
        ),
        consumption_ledger=_consumption_ledger(
            supersession_records
            if supersession_records is not None
            else tuple(_authorized(item.record) for item in members)
        ),
        expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
    )
    return ComparisonSetDefinition(
        provider_namespace=anchor.linkage_revision.provider_namespace,
        subject_token=anchor.linkage_revision.biological.subject_token,
        unit_of_analysis=anchor.linkage_revision.unit_of_analysis,
        replicate_rule=replicate_rule,
        time_axis=TimeAxis.COLLECTION_ORDINAL,
        measurement_definition_sha256=METHOD_SHA256,
        anchor_result_id=anchor.measurement.result_id,
        anchor_policy_id=policy.policy_id,
        anchor_policy_version=policy.version,
        anchor_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        missingness_policy=PolicyReference(
            policy_id="policy_missingness_alpha",
            version="1.0.0",
            policy_sha256="a" * 64,
        ),
        denominator_policy=PolicyReference(
            policy_id="policy_denominator_alpha",
            version="1.0.0",
            policy_sha256="b" * 64,
        ),
        authority_snapshot_sha256=TRUST_SHA256,
        supersession_ledger_sha256=analysis_supersession_ledger_sha256(ledger),
        supersession_ledger=ledger,
        members=tuple(sorted(members, key=lambda item: item.member_id)),
    )


def test_reanalysis_supersession_is_append_only_and_selects_current_tip() -> None:
    original = _record("1")
    successor = _reanalysis(original, "2")

    ledger = build_analysis_supersession_ledger(
        (_authorized(original), _authorized(successor)),
        consumption_ledger=_consumption_ledger(
            (_authorized(original), _authorized(successor))
        ),
        expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
    )

    assert len(ledger.events) == 1
    assert (
        ledger.events[0].predecessor_analysis_id
        == original.linkage_revision.technical.analysis_record_id
    )
    assert ledger.events[0].successor_analysis_id in ledger.current_analysis_ids
    assert original.linkage_revision.technical.analysis_record_id not in (
        ledger.current_analysis_ids
    )


def test_technical_rerun_is_distinct_without_superseding_biological_draw() -> None:
    first = _record("1")
    rerun = _record("2")
    rerun_revision = rerun.linkage_revision.model_copy(
        update={"biological": first.linkage_revision.biological}
    )
    rerun_authorized, _ = _consume(
        rerun_revision,
        (_create_approval(rerun_revision, "2"),),
    )
    rerun = rerun.model_copy(
        update={"linkage_revision": rerun_revision, "authorized_linkage": rerun_authorized}
    )

    ledger = build_analysis_supersession_ledger(
        (_authorized(first), _authorized(rerun)),
        consumption_ledger=_consumption_ledger(
            (_authorized(first), _authorized(rerun))
        ),
        expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
    )

    assert ledger.events == ()
    assert len(ledger.current_analysis_ids) == 2
    assert first.linkage_revision.biological == rerun.linkage_revision.biological


def test_supersession_rejects_cross_collection_branch_and_cycle() -> None:
    original = _record("1")
    successor = _reanalysis(original, "2")
    cross_collection = _reanalysis(original, "3")
    cross_revision = cross_collection.linkage_revision.model_copy(
        update={"biological": _record("4").linkage_revision.biological}
    )
    cross_authorized, _ = _consume(
        cross_revision,
        (_create_approval(cross_revision, "3"),),
    )
    cross_collection = cross_collection.model_copy(
        update={"linkage_revision": cross_revision, "authorized_linkage": cross_authorized}
    )
    cross_records = (_authorized(original), _authorized(cross_collection))
    with pytest.raises(ValueError, match="cross subject or collection"):
        build_analysis_supersession_ledger(
            cross_records,
            consumption_ledger=_consumption_ledger(cross_records),
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
        )

    branch = _reanalysis(original, "3")
    branch_records = (
        _authorized(original),
        _authorized(successor),
        _authorized(branch),
    )
    with pytest.raises(ValueError, match="cannot branch"):
        build_analysis_supersession_ledger(
            branch_records,
            consumption_ledger=_consumption_ledger(branch_records),
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
        )

    first_cycle = _reanalysis(successor, "1")
    first_revision = first_cycle.linkage_revision.model_copy(
        update={
            "linkage_id": original.linkage_revision.linkage_id,
            "technical": original.linkage_revision.technical.model_copy(
                update={
                    "reanalysis_of": OptionalOpaqueToken(
                        state=OptionalLineageState.KNOWN,
                        token=successor.linkage_revision.technical.analysis_record_id,
                    )
                }
            ),
        }
    )
    first_authorized, _ = _consume(
        first_revision,
        (_create_approval(first_revision, "1"),),
    )
    first_cycle = first_cycle.model_copy(
        update={"linkage_revision": first_revision, "authorized_linkage": first_authorized}
    )
    cycle_records = (_authorized(first_cycle), _authorized(successor))
    with pytest.raises(ValueError, match="cycle"):
        build_analysis_supersession_ledger(
            cycle_records,
            consumption_ledger=_consumption_ledger(cycle_records),
            expected_trust_snapshot_sha256_by_provider={PROVIDER: TRUST_SHA256},
        )


def test_immutable_definition_binds_members_policies_authority_and_timepoints() -> None:
    anchor = _record("1")
    later = _record("2")
    members = (
        _member(anchor, anchor, timepoint=0),
        _member(anchor, later, timepoint=1),
    )

    definition = _definition(anchor, members)

    assert len(definition.members) == 2
    assert definition.anchor_result_id == anchor.measurement.result_id
    assert definition.authority_snapshot_sha256 == TRUST_SHA256
    assert comparison_set_definition_sha256(definition)


def test_changed_membership_creates_new_version_and_preserves_old_bytes() -> None:
    anchor = _record("1")
    later = _record("2")
    first_definition = _definition(
        anchor, (_member(anchor, anchor, timepoint=0),)
    )
    first = ComparisonSetVersion(
        comparison_set_id="comparison_longitudinal_alpha",
        version=1,
        previous_version_sha256=None,
        definition_sha256=comparison_set_definition_sha256(first_definition),
        definition=first_definition,
    )
    second_definition = _definition(
        anchor,
        (
            _member(anchor, anchor, timepoint=0),
            _member(anchor, later, timepoint=1),
        ),
    )
    second = ComparisonSetVersion(
        comparison_set_id=first.comparison_set_id,
        version=2,
        previous_version_sha256=comparison_set_version_sha256(first),
        definition_sha256=comparison_set_definition_sha256(second_definition),
        definition=second_definition,
    )

    ledger = ComparisonSetLedger(versions=(first, second))

    assert ledger.versions[0] == first
    assert ledger.versions[1].definition.members != first.definition.members


def test_unchanged_or_broken_version_chain_is_rejected() -> None:
    anchor = _record("1")
    definition = _definition(anchor, (_member(anchor, anchor, timepoint=0),))
    first = ComparisonSetVersion(
        comparison_set_id="comparison_longitudinal_alpha",
        version=1,
        previous_version_sha256=None,
        definition_sha256=comparison_set_definition_sha256(definition),
        definition=definition,
    )
    duplicate = ComparisonSetVersion(
        comparison_set_id=first.comparison_set_id,
        version=2,
        previous_version_sha256=comparison_set_version_sha256(first),
        definition_sha256=first.definition_sha256,
        definition=definition,
    )
    with pytest.raises(ValidationError, match="must be idempotent"):
        ComparisonSetLedger(versions=(first, duplicate))

    broken = duplicate.model_copy(update={"previous_version_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="chain is invalid"):
        ComparisonSetLedger(versions=(first, broken))


def test_one_analysis_rule_rejects_pseudoreplicated_collection() -> None:
    anchor = _record("1")
    rerun = _record("2")
    rerun_revision = rerun.linkage_revision.model_copy(
        update={"biological": anchor.linkage_revision.biological}
    )
    rerun_authorized, _ = _consume(
        rerun_revision,
        (_create_approval(rerun_revision, "2"),),
    )
    rerun = rerun.model_copy(
        update={"linkage_revision": rerun_revision, "authorized_linkage": rerun_authorized}
    )
    members = (
        _member(anchor, anchor, timepoint=0, replicate=0),
        _member(anchor, rerun, timepoint=0, replicate=1),
    )

    with pytest.raises(ValidationError, match="one included analysis"):
        _definition(anchor, members)

    separate = _definition(
        anchor,
        members,
        replicate_rule=ReplicateRule.INCLUDE_TECHNICAL_SEPARATELY,
    )
    assert len(separate.members) == 2


def test_latest_reanalysis_rule_requires_old_analysis_excluded() -> None:
    anchor = _record("1")
    successor = _reanalysis(anchor, "2")
    ledger_records = (_authorized(anchor), _authorized(successor))
    successor_decision = _decision(successor, successor)
    old_decision = _decision(successor, anchor)
    old = _member(
        successor,
        anchor,
        timepoint=0,
        disposition=MemberDisposition.EXCLUDED,
        reason=MemberReason.SUPERSEDED_ANALYSIS,
        decision=old_decision,
    )
    current = _member(
        successor,
        successor,
        timepoint=0,
        decision=successor_decision,
    )

    definition = _definition(
        successor,
        (old, current),
        replicate_rule=ReplicateRule.LATEST_AUTHORIZED_REANALYSIS,
        supersession_records=ledger_records,
    )

    assert definition.anchor_result_id == successor.measurement.result_id
    assert old.disposition == MemberDisposition.EXCLUDED
    assert current.disposition == MemberDisposition.INCLUDED
    bad_current = current.model_copy(
        update={
            "disposition": MemberDisposition.EXCLUDED,
            "reason": MemberReason.SUPERSEDED_ANALYSIS,
        }
    )
    with pytest.raises(ValidationError, match="at least one included"):
        _definition(
            successor,
            (old, bad_current),
            replicate_rule=ReplicateRule.LATEST_AUTHORIZED_REANALYSIS,
            supersession_records=ledger_records,
        )


def test_incompatible_or_unknown_member_cannot_be_included() -> None:
    anchor = _record("1")
    incompatible = _record("2", unknown=ComparisonDimension.REFERENCE)
    policy = _policy(anchor)
    decision = _decide(anchor, incompatible, policy)
    assert decision.outcome == LongitudinalOutcome.UNKNOWN
    with pytest.raises(ValidationError, match="eligible compatibility"):
        _member(anchor, incompatible, timepoint=1, decision=decision)


def test_manifest_rejects_free_text_identity_and_policy_relabel() -> None:
    anchor = _record("1")
    definition = _definition(anchor, (_member(anchor, anchor, timepoint=0),))
    payload = definition.model_dump(mode="json")
    payload["subject_token"] = "free text identity"
    with pytest.raises(ValidationError):
        ComparisonSetDefinition.model_validate_json(json.dumps(payload))

    payload = definition.model_dump(mode="json")
    payload["anchor_policy_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="anchor policy"):
        ComparisonSetDefinition.model_validate_json(json.dumps(payload))
