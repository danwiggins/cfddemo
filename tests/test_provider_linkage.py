"""D01 provider-local linkage, authority, replay, and projection tests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from evidence_inspector.provider_linkage import (
    MAX_REVISIONS,
    ApprovalConsumption,
    ApprovalPurpose,
    BiologicalLineage,
    IssuerStatus,
    LinkageAuthorityReason,
    LinkageAuthorizationDecision,
    LinkageOperation,
    LinkageReasonCode,
    LinkageRevision,
    OptionalLineageState,
    OptionalOpaqueToken,
    ProviderApprovalConsumptionLedger,
    ProviderApprovalPayload,
    ProviderIssuerTrust,
    ProviderRole,
    ProviderTrustSnapshot,
    SignedProviderApproval,
    TechnicalLineage,
    UnitOfAnalysis,
    approval_payload_bytes,
    authorize_and_consume_linkage_revision,
    authorize_linkage_revision,
    linkage_revision_sha256,
    prepare_authorized_linkage_revision,
    project_active_linkages,
    provider_trust_snapshot_sha256,
    validate_linkage_history,
)

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
AFTER = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)


def _token(prefix: str, value: str) -> str:
    return f"{prefix}_{value * 32}"


PROVIDER = _token("provider", "1")
LINKAGE = _token("linkage", "2")
ISSUER = _token("issuer", "3")
KEY_ID = _token("key", "4")
SUBJECT = _token("subject", "5")
COLLECTION = _token("collection", "6")
SPECIMEN = _token("specimen", "7")
ANALYSIS = _token("analysis", "8")
MEASUREMENT = _token("measurement", "9")
PROJECTION = _token("projection", "a")

PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC_KEY = PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)


def _unknown() -> OptionalOpaqueToken:
    return OptionalOpaqueToken(state=OptionalLineageState.UNKNOWN, token=None)


def _known(prefix: str, digit: str) -> OptionalOpaqueToken:
    return OptionalOpaqueToken(
        state=OptionalLineageState.KNOWN,
        token=_token(prefix, digit),
    )


def _revision(
    *,
    revision: int = 1,
    operation: LinkageOperation = LinkageOperation.CREATE,
    reason: LinkageReasonCode = LinkageReasonCode.INITIAL_PROJECTION,
    previous: LinkageRevision | None = None,
    subject: str = SUBJECT,
    collection: str = COLLECTION,
    specimen: str = SPECIMEN,
    aliquot: OptionalOpaqueToken | None = None,
    analysis: str = ANALYSIS,
    measurement: str = MEASUREMENT,
    source: str = PROJECTION,
    linkage_id: str = LINKAGE,
) -> LinkageRevision:
    return LinkageRevision(
        linkage_id=linkage_id,
        provider_namespace=PROVIDER,
        revision=revision,
        previous_revision_sha256=(
            linkage_revision_sha256(previous) if previous is not None else None
        ),
        operation=operation,
        reason_code=reason,
        source_projection_ref=source,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
        biological=BiologicalLineage(
            subject_token=subject,
            collection_token=collection,
            specimen_token=specimen,
            aliquot=aliquot or _unknown(),
        ),
        technical=TechnicalLineage(
            run=_unknown(),
            analysis_record_id=analysis,
            measurement_id=measurement,
            reanalysis_of=_unknown(),
        ),
        proposed_at=NOW,
    )


def _trust(
    *,
    status: IssuerStatus = IssuerStatus.ACTIVE,
    snapshot_digit: str = "b",
) -> ProviderTrustSnapshot:
    return ProviderTrustSnapshot(
        snapshot_id=_token("trust", snapshot_digit),
        provider_namespace=PROVIDER,
        revision=1,
        previous_snapshot_sha256=None,
        issued_at=T0,
        expires_at=AFTER,
        issuers=(
            ProviderIssuerTrust(
                issuer_id=ISSUER,
                key_id=KEY_ID,
                public_key_base64=base64.b64encode(PUBLIC_KEY).decode("ascii"),
                status=status,
                allowed_roles=(ProviderRole.LINKER, ProviderRole.REVIEWER),
                allowed_purposes=(
                    ApprovalPurpose.CORRECT_LINKAGE,
                    ApprovalPurpose.CREATE_LINKAGE,
                    ApprovalPurpose.TOMBSTONE_LINKAGE,
                ),
            ),
        ),
    )


def _approval(
    revision: LinkageRevision,
    *,
    role: ProviderRole,
    purpose: ApprovalPurpose,
    digit: str,
    trust: ProviderTrustSnapshot | None = None,
    principal_digit: str | None = None,
    revision_sha256: str | None = None,
    issued_at: datetime = NOW,
) -> SignedProviderApproval:
    trust = trust or _trust()
    payload = ProviderApprovalPayload(
        approval_id=_token("approval", digit),
        provider_namespace=PROVIDER,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=_token("principal", principal_digit or digit),
        role=role,
        purpose=purpose,
        proposed_revision_sha256=revision_sha256 or linkage_revision_sha256(revision),
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        nonce=_token("nonce", digit),
        issued_at=issued_at,
        expires_at=AFTER,
    )
    signature = PRIVATE_KEY.sign(approval_payload_bytes(payload))
    return SignedProviderApproval(
        payload=payload,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    )


def _decision(
    revision: LinkageRevision,
    approvals: tuple[SignedProviderApproval, ...],
    *,
    previous: LinkageRevision | None = None,
    trust: ProviderTrustSnapshot | None = None,
    expected: str | None = None,
) -> LinkageAuthorizationDecision:
    trust = trust or _trust()
    return authorize_linkage_revision(
        revision,
        previous_revision=previous,
        approvals=approvals,
        trust_snapshot=trust,
        expected_trust_snapshot_sha256=(
            expected or provider_trust_snapshot_sha256(trust)
        ),
        evaluated_at=NOW,
    )


def _consume(
    revision: LinkageRevision,
    approvals: tuple[SignedProviderApproval, ...],
    *,
    previous: LinkageRevision | None = None,
    trust: ProviderTrustSnapshot | None = None,
    ledger: ProviderApprovalConsumptionLedger | None = None,
):
    trust = trust or _trust()
    record = prepare_authorized_linkage_revision(
        revision,
        previous_revision=previous,
        approvals=approvals,
        trust_snapshot=trust,
        expected_trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        evaluated_at=NOW,
    )
    return record, ledger or ProviderApprovalConsumptionLedger()


def _create_approval(
    revision: LinkageRevision,
    digit: str,
    *,
    trust: ProviderTrustSnapshot | None = None,
) -> SignedProviderApproval:
    return _approval(
        revision,
        role=ProviderRole.LINKER,
        purpose=ApprovalPurpose.CREATE_LINKAGE,
        digit=digit,
        trust=trust,
    )


def _correction_approvals(
    revision: LinkageRevision,
    *,
    purpose: ApprovalPurpose = ApprovalPurpose.CORRECT_LINKAGE,
) -> tuple[SignedProviderApproval, SignedProviderApproval]:
    return (
        _approval(
            revision,
            role=ProviderRole.LINKER,
            purpose=purpose,
            digit="d",
        ),
        _approval(
            revision,
            role=ProviderRole.REVIEWER,
            purpose=purpose,
            digit="e",
        ),
    )


def _project(records, ledger):
    del ledger
    validate_linkage_history(
        records,
        expected_trust_snapshot_sha256_by_provider={
            PROVIDER: provider_trust_snapshot_sha256(_trust())
        },
    )


def test_create_binds_external_trust_but_remains_inactive_without_store() -> None:
    revision = _revision()
    record, ledger = _consume(revision, (_create_approval(revision, "c"),))

    assert record.authorization.linkage_authorized
    assert not record.authorization.comparison_linkage_eligible
    assert ledger.entries == ()
    with pytest.raises(RuntimeError, match="durable atomic linkage persistence"):
        project_active_linkages(
            (record,),
            consumption_ledger=ledger,
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: provider_trust_snapshot_sha256(_trust())
            },
        )


def test_absent_authority_disables_linkage_and_comparison() -> None:
    decision = authorize_linkage_revision(
        _revision(),
        previous_revision=None,
        approvals=(),
        trust_snapshot=None,
        expected_trust_snapshot_sha256=None,
        evaluated_at=NOW,
    )
    assert not decision.linkage_authorized
    assert not decision.comparison_linkage_eligible
    assert decision.reason_codes == (LinkageAuthorityReason.AUTHORITY_ABSENT,)


def test_authorized_decision_cannot_omit_trust_or_approvals() -> None:
    with pytest.raises(ValidationError, match="exact trust snapshot"):
        LinkageAuthorizationDecision(
            proposed_revision_sha256="a" * 64,
            trust_snapshot_sha256=None,
            evaluated_at=NOW,
            linkage_authorized=True,
            comparison_linkage_eligible=True,
            reason_codes=(LinkageAuthorityReason.AUTHORIZED,),
            approval_ids=(),
            principal_ids=(),
        )


def test_activation_api_rejects_caller_supplied_consumption_state() -> None:
    revision = _revision()
    record, ledger = _consume(revision, (_create_approval(revision, "c"),))
    with pytest.raises(RuntimeError, match="durable atomic linkage persistence"):
        project_active_linkages(
            (record,),
            consumption_ledger=ProviderApprovalConsumptionLedger(),
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: provider_trust_snapshot_sha256(_trust())
            },
        )
    with pytest.raises(RuntimeError, match="durable atomic linkage persistence"):
        authorize_and_consume_linkage_revision(
            revision,
            previous_revision=None,
            approvals=record.approvals,
            trust_snapshot=record.trust_snapshot,
            expected_trust_snapshot_sha256=provider_trust_snapshot_sha256(_trust()),
            evaluated_at=NOW,
            consumption_ledger=ledger,
        )

    forged = ProviderApprovalConsumptionLedger(
        entries=(
            ApprovalConsumption(
                provider_namespace=PROVIDER,
                approval_id=record.approvals[0].payload.approval_id,
                nonce=record.approvals[0].payload.nonce,
                proposed_revision_sha256=linkage_revision_sha256(revision),
                trust_snapshot_sha256=provider_trust_snapshot_sha256(_trust()),
            ),
        )
    )
    with pytest.raises(RuntimeError, match="durable atomic linkage persistence"):
        project_active_linkages(
            (record,),
            consumption_ledger=forged,
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: provider_trust_snapshot_sha256(_trust())
            },
        )


def test_caller_cannot_reset_replay_state_between_calls() -> None:
    first = _revision()
    approval = _create_approval(first, "c")
    for supplied in (ProviderApprovalConsumptionLedger(), ProviderApprovalConsumptionLedger()):
        with pytest.raises(RuntimeError, match="durable atomic linkage persistence"):
            authorize_and_consume_linkage_revision(
                first,
                previous_revision=None,
                approvals=(approval,),
                trust_snapshot=_trust(),
                expected_trust_snapshot_sha256=provider_trust_snapshot_sha256(_trust()),
                evaluated_at=NOW,
                consumption_ledger=supplied,
            )


def test_wrong_subject_correction_requires_distinct_principals() -> None:
    original = _revision()
    correction = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=original,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    )
    linker, reviewer = _correction_approvals(correction)
    repeated = _approval(
        correction,
        role=ProviderRole.REVIEWER,
        purpose=ApprovalPurpose.CORRECT_LINKAGE,
        digit="e",
        principal_digit="d",
    )
    assert LinkageAuthorityReason.DUPLICATE_PRINCIPAL in _decision(
        correction, (linker, repeated), previous=original
    ).reason_codes
    assert _decision(
        correction, (linker, reviewer), previous=original
    ).linkage_authorized


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("wrong_digest", LinkageAuthorityReason.REVISION_BINDING_MISMATCH),
        ("wrong_purpose", LinkageAuthorityReason.PURPOSE_MISMATCH),
        ("bad_signature", LinkageAuthorityReason.SIGNATURE_INVALID),
        ("revoked_issuer", LinkageAuthorityReason.ISSUER_REVOKED),
        ("wrong_head", LinkageAuthorityReason.TRUST_HEAD_MISMATCH),
    ),
)
def test_authority_mutations_fail_closed(
    mutation: str, reason: LinkageAuthorityReason
) -> None:
    revision = _revision()
    trust = _trust(
        status=(
            IssuerStatus.REVOKED
            if mutation == "revoked_issuer"
            else IssuerStatus.ACTIVE
        )
    )
    approval = _approval(
        revision,
        role=ProviderRole.LINKER,
        purpose=(
            ApprovalPurpose.CORRECT_LINKAGE
            if mutation == "wrong_purpose"
            else ApprovalPurpose.CREATE_LINKAGE
        ),
        digit="c",
        trust=trust,
        revision_sha256="0" * 64 if mutation == "wrong_digest" else None,
    )
    if mutation == "bad_signature":
        approval = approval.model_copy(
            update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
        )
    decision = _decision(
        revision,
        (approval,),
        trust=trust,
        expected=(
            "0" * 64
            if mutation == "wrong_head"
            else provider_trust_snapshot_sha256(trust)
        ),
    )
    assert not decision.linkage_authorized
    assert reason in decision.reason_codes


def test_approval_must_follow_and_bind_pinned_trust() -> None:
    revision = _revision()
    trust = _trust()
    predating = _approval(
        revision,
        role=ProviderRole.LINKER,
        purpose=ApprovalPurpose.CREATE_LINKAGE,
        digit="c",
        trust=trust,
        issued_at=datetime(2026, 9, 29, 9, 0, tzinfo=UTC),
    )
    assert LinkageAuthorityReason.APPROVAL_NOT_CURRENT in _decision(
        revision, (predating,), trust=trust
    ).reason_codes

    other_trust = _trust(snapshot_digit="f")
    wrong_binding = _create_approval(revision, "c", trust=other_trust)
    assert LinkageAuthorityReason.TRUST_BINDING_MISMATCH in _decision(
        revision, (wrong_binding,), trust=trust
    ).reason_codes


@pytest.mark.parametrize(
    ("reason", "updates"),
    (
        (
            LinkageReasonCode.WRONG_SUBJECT,
            {
                "subject": _token("subject", "d"),
                "collection": _token("collection", "d"),
                "specimen": _token("specimen", "d"),
            },
        ),
        (
            LinkageReasonCode.WRONG_COLLECTION,
            {
                "collection": _token("collection", "d"),
                "specimen": _token("specimen", "d"),
            },
        ),
        (
            LinkageReasonCode.WRONG_SPECIMEN,
            {"specimen": _token("specimen", "d")},
        ),
        (
            LinkageReasonCode.TECHNICAL_LINEAGE_CORRECTION,
            {"analysis": _token("analysis", "d")},
        ),
        (
            LinkageReasonCode.SOURCE_AUTHORITY_UPDATE,
            {"source": _token("projection", "d")},
        ),
    ),
)
def test_correction_reason_matches_exact_field_delta(
    reason: LinkageReasonCode,
    updates: dict[str, str],
) -> None:
    original = _revision()
    correction = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=reason,
        previous=original,
        **updates,
    )
    assert _decision(
        correction,
        _correction_approvals(correction),
        previous=original,
    ).linkage_authorized

    mislabeled = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.TECHNICAL_LINEAGE_CORRECTION,
        previous=original,
        subject=_token("subject", "e"),
    )
    assert LinkageAuthorityReason.CORRECTION_DELTA_INVALID in _decision(
        mislabeled,
        _correction_approvals(mislabeled),
        previous=original,
    ).reason_codes


def test_history_retains_original_and_validates_correction_chain() -> None:
    first = _revision()
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    correction = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=first,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    )
    correction_record, ledger = _consume(
        correction,
        _correction_approvals(correction),
        previous=first,
        ledger=ledger,
    )
    assert _project((first_record, correction_record), ledger) is None
    assert first_record.revision.biological.subject_token == SUBJECT


def test_same_collection_allows_distinct_technical_reruns() -> None:
    first = _revision()
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    rerun = _revision(
        linkage_id=_token("linkage", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    rerun_record, ledger = _consume(
        rerun, (_create_approval(rerun, "e"),), ledger=ledger
    )
    assert _project((first_record, rerun_record), ledger) is None


def test_same_collection_allows_sibling_specimens_and_aliquots() -> None:
    first = _revision(aliquot=_known("aliquot", "c"))
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    sibling = _revision(
        linkage_id=_token("linkage", "d"),
        specimen=_token("specimen", "d"),
        aliquot=_known("aliquot", "e"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    sibling_record, ledger = _consume(
        sibling, (_create_approval(sibling, "e"),), ledger=ledger
    )

    assert _project((first_record, sibling_record), ledger) is None


@pytest.mark.parametrize("reused", ("analysis", "measurement"))
def test_history_never_reassigns_technical_ids_after_tombstone(reused: str) -> None:
    first = _revision()
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    tombstone = _revision(
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=first,
    )
    tombstone_record, ledger = _consume(
        tombstone,
        _correction_approvals(
            tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE
        ),
        previous=first,
        ledger=ledger,
    )
    replacement = _revision(
        linkage_id=_token("linkage", "f"),
        collection=_token("collection", "f"),
        specimen=_token("specimen", "f"),
        analysis=(ANALYSIS if reused == "analysis" else _token("analysis", "f")),
        measurement=(
            MEASUREMENT if reused == "measurement" else _token("measurement", "f")
        ),
    )
    replacement_record, ledger = _consume(
        replacement, (_create_approval(replacement, "f"),), ledger=ledger
    )

    with pytest.raises(ValueError, match=f"reused {reused}"):
        _project((first_record, tombstone_record, replacement_record), ledger)


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {
                "subject": _token("subject", "d"),
                "analysis": _token("analysis", "d"),
                "measurement": _token("measurement", "d"),
            },
            "conflicting collection parent",
        ),
        (
            {
                "collection": _token("collection", "d"),
                "analysis": _token("analysis", "d"),
                "measurement": _token("measurement", "d"),
            },
            "conflicting specimen parent",
        ),
        ({"analysis": _token("analysis", "d")}, "reused measurement"),
    ),
)
def test_projection_rejects_parent_conflicts_and_measurement_reuse(
    updates: dict[str, str], message: str
) -> None:
    first = _revision()
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    second = _revision(linkage_id=_token("linkage", "d"), **updates)
    second_record, ledger = _consume(
        second, (_create_approval(second, "e"),), ledger=ledger
    )
    with pytest.raises(ValueError, match=message):
        _project((first_record, second_record), ledger)


def test_projection_rejects_aliquot_parent_conflict() -> None:
    first = _revision(aliquot=_known("aliquot", "c"))
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    second = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        aliquot=_known("aliquot", "c"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    second_record, ledger = _consume(
        second, (_create_approval(second, "e"),), ledger=ledger
    )
    with pytest.raises(ValueError, match="conflicting aliquot parent"):
        _project((first_record, second_record), ledger)


def test_tombstone_history_validates_and_cannot_be_revived() -> None:
    first = _revision()
    first_record, ledger = _consume(first, (_create_approval(first, "c"),))
    tombstone = _revision(
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=first,
    )
    tombstone_record, ledger = _consume(
        tombstone,
        _correction_approvals(
            tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE
        ),
        previous=first,
        ledger=ledger,
    )
    assert _project((first_record, tombstone_record), ledger) is None

    attempted_revival = _revision(
        revision=3,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.SOURCE_AUTHORITY_UPDATE,
        previous=tombstone,
        source=_token("projection", "f"),
    )
    assert LinkageAuthorityReason.REVISION_CHAIN_INVALID in _decision(
        attempted_revival,
        _correction_approvals(attempted_revival),
        previous=tombstone,
    ).reason_codes


def test_projection_enforces_total_revision_bound() -> None:
    revision = _revision()
    record, ledger = _consume(revision, (_create_approval(revision, "c"),))
    with pytest.raises(ValueError, match="revision bound"):
        _project((record,) * (MAX_REVISIONS + 1), ledger)


def test_opaque_contracts_reject_free_text_and_unknown_fields() -> None:
    payload = _revision().model_dump(mode="json")
    payload["biological"]["subject_token"] = "free text identity"
    with pytest.raises(ValueError):
        LinkageRevision.model_validate(payload)
    payload = _revision().model_dump(mode="json")
    payload["note"] = "free text identity"
    with pytest.raises(ValueError):
        LinkageRevision.model_validate(payload)
