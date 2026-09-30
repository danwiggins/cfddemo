"""D04 durable record supersession and comparison-invalidation tests."""

from __future__ import annotations

import base64
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

import evidence_inspector.record_supersession_store as supersession_module
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage import (
    ApprovalPurpose,
    LinkageOperation,
    LinkageReasonCode,
    ProviderApprovalPayload,
    ProviderRole,
    SignedProviderApproval,
    approval_payload_bytes,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import committed_linkage_receipt_sha256
from evidence_inspector.record_supersession_store import (
    ComparisonState,
    DerivedComparison,
    InvalidationReason,
    RecordLineageRole,
    RecordSupersessionConflict,
    RecordSupersessionStore,
    RecordSupersessionUnsafe,
    SupersedingRecord,
    SupersessionReason,
    comparison_authority_statement_sha256,
    make_comparison_id,
    make_record_id,
    record_sha256,
    supersession_statement_sha256,
)
from tests.test_provider_linkage import (
    AFTER,
    ISSUER,
    KEY_ID,
    NOW,
    PRIVATE_KEY,
    T0,
    _consume,
    _correction_approvals,
    _create_approval,
    _known,
    _revision,
    _token,
    _trust,
)
from tests.test_provider_linkage_store import _store


def _superseding_record(
    snapshot,
    linkage_id: str,
    digit: str,
    *,
    role: RecordLineageRole = RecordLineageRole.PRIMARY_ANALYSIS,
    source: SupersedingRecord | None = None,
) -> SupersedingRecord:
    index = next(
        index
        for index, revision in enumerate(snapshot.revisions)
        if revision.linkage_id == linkage_id
    )
    revision = snapshot.revisions[index]
    receipt = snapshot.activation_receipts[index]
    result_id = "result_" + digit * 40
    result_sha256 = digit * 64
    bundle_sha256 = ({"a": "b", "b": "c", "c": "d"}.get(digit, "e")) * 64
    record_id = make_record_id(
        provider_namespace=revision.provider_namespace,
        analysis_record_id=revision.technical.analysis_record_id,
        result_id=result_id,
        result_sha256=result_sha256,
        bundle_sha256=bundle_sha256,
    )
    values = {
        "record_id": record_id,
        "provider_namespace": revision.provider_namespace,
        "analysis_record_id": revision.technical.analysis_record_id,
        "result_id": result_id,
        "result_sha256": result_sha256,
        "bundle_sha256": bundle_sha256,
        "linkage_id": revision.linkage_id,
        "linkage_revision": revision.revision,
        "linkage_revision_sha256": receipt.linkage_revision_sha256,
        "activation_receipt_sha256": committed_linkage_receipt_sha256(receipt),
        "lineage_role": role,
        "reanalysis_of_record_id": source.record_id if source else None,
        "supersedes_record_id": source.record_id if source else None,
    }
    if source is not None:
        values["supersession_reason"] = SupersessionReason.METHOD_REANALYSIS
        provisional = SupersedingRecord.model_construct(
            **values, supersession_authorization=None
        )
        trust = _trust()
        payload = ProviderApprovalPayload(
            approval_id=_token("approval", "7"),
            provider_namespace=revision.provider_namespace,
            issuer_id=ISSUER,
            key_id=KEY_ID,
            principal_id=_token("principal", "7"),
            role=ProviderRole.REVIEWER,
            purpose=ApprovalPurpose.SUPERSEDE_RECORD,
            proposed_revision_sha256=supersession_statement_sha256(provisional),
            trust_snapshot_id=trust.snapshot_id,
            trust_snapshot_revision=trust.revision,
            trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
            nonce=_token("nonce", "7"),
            issued_at=NOW,
            expires_at=AFTER,
        )
        values["supersession_authorization"] = SignedProviderApproval(
            payload=payload,
            signature_base64=base64.b64encode(
                PRIVATE_KEY.sign(approval_payload_bytes(payload))
            ).decode("ascii"),
        )
    return SupersedingRecord(**values)


@pytest.fixture
def durable(tmp_path: Path):
    linkage = _store(tmp_path / "linkage")
    first_revision = _revision()
    first_authorized, _ = _consume(
        first_revision, (_create_approval(first_revision, "c"),)
    )
    second_revision = _revision(
        linkage_id=_token("linkage", "b"),
        collection=_token("collection", "b"),
        specimen=_token("specimen", "b"),
        analysis=_token("analysis", "b"),
        measurement=_token("measurement", "b"),
    )
    second_authorized, _ = _consume(
        second_revision, (_create_approval(second_revision, "f"),)
    )
    linkage.commit_authorized_revision(first_authorized)
    linkage.commit_authorized_revision(second_authorized)
    ledger = RecordSupersessionStore(tmp_path / "records", linkage_store=linkage)
    snapshot = linkage.active_snapshot()
    first = _superseding_record(snapshot, first_revision.linkage_id, "a")
    second = _superseding_record(snapshot, second_revision.linkage_id, "b")
    try:
        yield linkage, ledger, first_revision, first, second
    finally:
        linkage.close()


def _comparison(first: SupersedingRecord, second: SupersedingRecord, snapshot):
    members = tuple(sorted((first.record_id, second.record_id)))
    digest = "e" * 64
    values = {
        "comparison_id": make_comparison_id(members, digest),
        "member_record_ids": members,
        "derived_artifact_sha256": digest,
        "provider_namespace": first.provider_namespace,
        "linkage_store_id": snapshot.linkage_store_id,
        "linkage_store_epoch_sha256": snapshot.linkage_store_epoch_sha256,
        "linkage_storage_identity_sha256": snapshot.linkage_storage_identity_sha256,
        "linkage_state_version": snapshot.linkage_state_version,
        "linkage_state_head_sha256": snapshot.linkage_state_head_sha256,
    }
    provisional = DerivedComparison.model_construct(
        **values, authority=_create_approval(_revision(), "c")
    )
    trust = _trust()
    payload = ProviderApprovalPayload(
        approval_id=_token("approval", "6"),
        provider_namespace=first.provider_namespace,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=_token("principal", "6"),
        role=ProviderRole.REVIEWER,
        purpose=ApprovalPurpose.REGISTER_COMPARISON,
        proposed_revision_sha256=comparison_authority_statement_sha256(provisional),
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        nonce=_token("nonce", "6"),
        issued_at=NOW,
        expires_at=AFTER,
    )
    return DerivedComparison(
        **values,
        authority=SignedProviderApproval(
            payload=payload,
            signature_base64=base64.b64encode(
                PRIVATE_KEY.sign(approval_payload_bytes(payload))
            ).decode("ascii"),
        ),
    )


def _authorized_reanalysis(linkage, first_revision, first):
    revision = _revision(
        linkage_id=_token("linkage", "c"),
        analysis=_token("analysis", "c"),
        measurement=_token("measurement", "c"),
    ).model_copy(
        update={
            "technical": _revision().technical.model_copy(
                update={
                    "analysis_record_id": _token("analysis", "c"),
                    "measurement_id": _token("measurement", "c"),
                    "reanalysis_of": _known(
                        "analysis",
                        first_revision.technical.analysis_record_id.split("_")[1][0],
                    ),
                }
            )
        }
    )
    authorized, _ = _consume(revision, (_create_approval(revision, "1"),))
    linkage.commit_authorized_revision(authorized)
    return revision, _superseding_record(
        linkage.active_snapshot(),
        revision.linkage_id,
        "c",
        role=RecordLineageRole.REANALYSIS,
        source=first,
    )


def _resign(approval, *, issued_at, expires_at):
    payload = approval.payload.model_copy(
        update={"issued_at": issued_at, "expires_at": expires_at}
    )
    return approval.model_copy(
        update={
            "payload": payload,
            "signature_base64": base64.b64encode(
                PRIVATE_KEY.sign(approval_payload_bytes(payload))
            ).decode("ascii"),
        }
    )


def _correct_first_linkage(linkage, first):
    current = next(
        item
        for item in linkage.active_snapshot().revisions
        if item.linkage_id == first.linkage_id
    )
    correction = _revision(
        linkage_id=current.linkage_id,
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.TECHNICAL_LINEAGE_CORRECTION,
        previous=current,
        subject=current.biological.subject_token,
        collection=current.biological.collection_token,
        specimen=current.biological.specimen_token,
        analysis=current.technical.analysis_record_id,
        measurement=_token("measurement", "d"),
    )
    authorized, _ = _consume(
        correction,
        _correction_approvals(correction),
        previous=current,
    )
    linkage.commit_authorized_revision(authorized)


def _tombstone_first_linkage(linkage, first):
    current = next(
        item
        for item in linkage.active_snapshot().revisions
        if item.linkage_id == first.linkage_id
    )
    tombstone = _revision(
        linkage_id=current.linkage_id,
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=current,
        subject=current.biological.subject_token,
        collection=current.biological.collection_token,
        specimen=current.biological.specimen_token,
        analysis=current.technical.analysis_record_id,
        measurement=current.technical.measurement_id,
    )
    authorized, _ = _consume(
        tombstone,
        _correction_approvals(tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE),
        previous=current,
    )
    linkage.commit_authorized_revision(authorized)


def test_exact_retry_active_selection_and_canonical_replay(durable) -> None:
    _, ledger, _, first, second = durable
    first_receipt = ledger.commit_record(first)
    assert ledger.commit_record(first) == first_receipt
    ledger.commit_record(second)
    assert ledger.commit_record(first) == first_receipt
    snapshot = ledger.active_snapshot()
    assert snapshot.records == tuple(
        sorted((first, second), key=lambda item: item.record_id)
    )
    assert ledger.replay_snapshot(snapshot) == snapshot
    assert snapshot.records[0].biological_timepoint_contribution is False
    with pytest.raises(RecordSupersessionConflict, match="stale"):
        ledger.replay_snapshot(
            snapshot.model_copy(update={"state_head_sha256": "0" * 64})
        )


def test_reanalysis_supersedes_without_becoming_biological_timepoint(durable) -> None:
    linkage, ledger, first_revision, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    comparison = _comparison(first, second, ledger.active_snapshot())
    comparison_receipt = ledger.register_comparison(comparison)
    assert ledger.register_comparison(comparison) == comparison_receipt
    assert (
        ledger.comparison_status(comparison.comparison_id).state
        == ComparisonState.CURRENT
    )

    reanalysis_revision = _revision(
        linkage_id=_token("linkage", "c"),
        analysis=_token("analysis", "c"),
        measurement=_token("measurement", "c"),
    ).model_copy(
        update={
            "technical": _revision().technical.model_copy(
                update={
                    "analysis_record_id": _token("analysis", "c"),
                    "measurement_id": _token("measurement", "c"),
                    "reanalysis_of": _known(
                        "analysis",
                        first_revision.technical.analysis_record_id.split("_")[1][0],
                    ),
                }
            )
        }
    )
    # _known repeats one hex digit; the fixture source is analysis_a...a.
    assert reanalysis_revision.technical.reanalysis_of.token == first.analysis_record_id
    authorized, _ = _consume(
        reanalysis_revision, (_create_approval(reanalysis_revision, "1"),)
    )
    linkage.commit_authorized_revision(authorized)
    updated = linkage.active_snapshot()
    reanalysis = _superseding_record(
        updated,
        reanalysis_revision.linkage_id,
        "c",
        role=RecordLineageRole.REANALYSIS,
        source=first,
    )
    ledger.commit_record(reanalysis)

    active = ledger.active_snapshot()
    assert first not in active.records
    assert reanalysis in active.records
    assert reanalysis.biological_timepoint_contribution is False
    status = ledger.comparison_status(comparison.comparison_id)
    assert status.state == ComparisonState.STALE
    assert InvalidationReason.RECORD_SUPERSEDED in status.reasons
    assert InvalidationReason.LINKAGE_AUTHORITY_ADVANCED in status.reasons


def test_authority_advance_and_tombstone_persist_staleness(durable) -> None:
    linkage, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    comparison = _comparison(first, second, ledger.active_snapshot())
    ledger.register_comparison(comparison)

    current = next(
        item
        for item in linkage.active_snapshot().revisions
        if item.linkage_id == second.linkage_id
    )
    tombstone = _revision(
        linkage_id=current.linkage_id,
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=current,
        subject=current.biological.subject_token,
        collection=current.biological.collection_token,
        specimen=current.biological.specimen_token,
        analysis=current.technical.analysis_record_id,
        measurement=current.technical.measurement_id,
    )
    authorized, _ = _consume(
        tombstone,
        _correction_approvals(tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE),
        previous=current,
    )
    linkage.commit_authorized_revision(authorized)
    status = ledger.comparison_status(comparison.comparison_id)
    assert status.reasons == (
        InvalidationReason.LINKAGE_AUTHORITY_ADVANCED,
        InvalidationReason.LINKAGE_CHANGED_OR_TOMBSTONED,
    )
    # The invalidation is durable and survives a new ledger instance.
    reopened = RecordSupersessionStore(ledger.root, linkage_store=linkage)
    assert reopened.comparison_status(comparison.comparison_id) == status


def test_unknown_source_self_link_cycle_and_branch_fail_closed(durable) -> None:
    _, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    self_link = first.model_copy(
        update={
            "lineage_role": RecordLineageRole.REANALYSIS,
            "reanalysis_of_record_id": first.record_id,
            "supersedes_record_id": first.record_id,
        }
    )
    with pytest.raises(ValidationError, match="itself"):
        SupersedingRecord.model_validate(self_link.model_dump())
    unknown = second.model_copy(
        update={
            "record_id": "record_" + "9" * 40,
            "lineage_role": RecordLineageRole.REANALYSIS,
            "reanalysis_of_record_id": "record_" + "8" * 40,
            "supersedes_record_id": "record_" + "8" * 40,
        }
    )
    with pytest.raises((ValidationError, RecordSupersessionConflict)):
        ledger.commit_record(unknown)


def test_concurrent_exact_commit_is_single_append(durable) -> None:
    _, ledger, _, first, _ = durable
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: ledger.commit_record(first), range(2)))
    assert receipts[0].record_id == receipts[1].record_id
    assert ledger.active_snapshot().records == (first,)


def test_concurrent_initialization_converges_on_one_exact_schema(
    tmp_path: Path,
) -> None:
    linkage = _store(tmp_path / "linkage")
    root = tmp_path / "records"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(
                pool.map(
                    lambda _: RecordSupersessionStore(root, linkage_store=linkage),
                    range(2),
                )
            )
        first, second = (store.active_snapshot() for store in stores)
        assert first == second
    finally:
        linkage.close()


def test_tamper_schema_and_content_are_detected_without_private_text(durable) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE records SET record_json=? WHERE record_id=?",
            (b'{"patient":"leak"}', first.record_id),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="history") as captured:
        ledger.active_snapshot()
    assert "patient" not in str(captured.value)
    assert "leak" not in str(captured.value)


def test_resealed_durable_cycle_is_rejected_from_complete_history(durable) -> None:
    _, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    malicious = SupersedingRecord.model_validate(
        {
            **first.model_dump(mode="python"),
            "lineage_role": RecordLineageRole.REANALYSIS,
            "reanalysis_of_record_id": second.record_id,
            "supersedes_record_id": second.record_id,
            "supersession_reason": SupersessionReason.METHOD_REANALYSIS,
            "supersession_authorization": _create_approval(_revision(), "c"),
        }
    )
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE records SET supersedes_record_id=?, record_sha256=?, record_json=? WHERE record_id=?",
            (
                second.record_id,
                record_sha256(malicious),
                canonical_contract_bytes(malicious),
                first.record_id,
            ),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
            (RecordSupersessionStore._state_head(connection),),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="chain"):
        ledger.active_snapshot()


def test_resealed_activation_receipt_substitution_is_rejected(durable) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    forged = first.model_copy(update={"activation_receipt_sha256": "f" * 64})
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE records SET record_sha256=?, record_json=? WHERE record_id=?",
            (record_sha256(forged), canonical_contract_bytes(forged), first.record_id),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
            (RecordSupersessionStore._state_head(connection),),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="authority binding"):
        ledger.active_snapshot()


def test_resealed_comparison_authority_columns_cannot_restore_current(durable) -> None:
    _, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    comparison = _comparison(first, second, ledger.active_snapshot())
    ledger.register_comparison(comparison)
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE comparisons SET linkage_state_version=0, linkage_state_head_sha256=?",
            ("f" * 64,),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
            (RecordSupersessionStore._state_head(connection),),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="history binding"):
        ledger.comparison_status(comparison.comparison_id)


def test_public_hash_replay_and_status_paths_reject_hooks_and_redact() -> None:
    class HookedRecord(SupersedingRecord):
        def model_dump(self, *args, **kwargs):
            raise AssertionError("hook executed")

    hooked = HookedRecord.model_construct()
    with pytest.raises(ValueError, match="record contract is invalid"):
        record_sha256(hooked)
    private = "/Users/alice/private/patient_alice_private"
    with pytest.raises(RecordSupersessionConflict) as captured:
        RecordSupersessionStore.comparison_status(object(), private)  # type: ignore[arg-type]
    assert private not in str(captured.value)


def test_signed_supersession_rejects_wrong_purpose_and_signature(durable) -> None:
    linkage, ledger, first_revision, first, _ = durable
    ledger.commit_record(first)
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    approval = reanalysis.supersession_authorization
    assert approval is not None
    wrong_payload = approval.payload.model_copy(
        update={"purpose": ApprovalPurpose.CREATE_LINKAGE}
    )
    wrong = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"payload": wrong_payload}
            )
        }
    )
    with pytest.raises(RecordSupersessionConflict, match="authority is invalid"):
        ledger.commit_record(wrong)
    bad_signature = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"signature_base64": base64.b64encode(b"x" * 64).decode()}
            )
        }
    )
    with pytest.raises(RecordSupersessionConflict, match="authority is invalid"):
        ledger.commit_record(bad_signature)


def test_reanalysis_is_inactive_when_any_source_linkage_is_tombstoned(durable) -> None:
    linkage, ledger, first_revision, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    ledger.commit_record(reanalysis)
    current = next(
        item
        for item in linkage.active_snapshot().revisions
        if item.linkage_id == first.linkage_id
    )
    tombstone = _revision(
        linkage_id=current.linkage_id,
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=current,
        subject=current.biological.subject_token,
        collection=current.biological.collection_token,
        specimen=current.biological.specimen_token,
        analysis=current.technical.analysis_record_id,
        measurement=current.technical.measurement_id,
    )
    authorized, _ = _consume(
        tombstone,
        _correction_approvals(tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE),
        previous=current,
    )
    linkage.commit_authorized_revision(authorized)
    assert reanalysis not in ledger.active_snapshot().records


def test_linkage_writer_cannot_cross_dependent_commit_fence(durable) -> None:
    linkage, ledger, _, first, _ = durable
    current = next(
        item
        for item in linkage.active_snapshot().revisions
        if item.linkage_id == first.linkage_id
    )
    tombstone = _revision(
        linkage_id=current.linkage_id,
        revision=2,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=current,
        subject=current.biological.subject_token,
        collection=current.biological.collection_token,
        specimen=current.biological.specimen_token,
        analysis=current.technical.analysis_record_id,
        measurement=current.technical.measurement_id,
    )
    authorized, _ = _consume(
        tombstone,
        _correction_approvals(tombstone, purpose=ApprovalPurpose.TOMBSTONE_LINKAGE),
        previous=current,
    )
    other = _store(linkage.root)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with ledger._linkage_fence():
                future = pool.submit(other.commit_authorized_revision, authorized)
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.1)
            future.result(timeout=2)
    finally:
        other.close()


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    ((NOW + timedelta(minutes=1), AFTER), (NOW - timedelta(hours=1), NOW)),
)
def test_supersession_approval_must_be_current_at_commit(
    durable, issued_at, expires_at
) -> None:
    linkage, ledger, first_revision, first, _ = durable
    ledger.commit_record(first)
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    assert reanalysis.supersession_authorization is not None
    changed = reanalysis.model_copy(
        update={
            "supersession_authorization": _resign(
                reanalysis.supersession_authorization,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        }
    )
    with pytest.raises(RecordSupersessionConflict, match="authority is invalid"):
        ledger.commit_record(changed)


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    ((NOW + timedelta(minutes=1), AFTER), (NOW - timedelta(hours=1), NOW)),
)
def test_comparison_approval_must_be_current_at_commit(
    durable, issued_at, expires_at
) -> None:
    _, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    comparison = _comparison(first, second, ledger.active_snapshot())
    changed = comparison.model_copy(
        update={
            "authority": _resign(
                comparison.authority,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        }
    )
    with pytest.raises(RecordSupersessionConflict, match="authority is invalid"):
        ledger.register_comparison(changed)


def test_exact_retry_remains_idempotent_after_action_approval_expiry(durable) -> None:
    linkage, ledger, first_revision, first, _ = durable
    ledger.commit_record(first)
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    assert reanalysis.supersession_authorization is not None
    expiry = NOW + timedelta(minutes=5)
    reanalysis = reanalysis.model_copy(
        update={
            "supersession_authorization": _resign(
                reanalysis.supersession_authorization,
                issued_at=NOW,
                expires_at=expiry,
            )
        }
    )
    receipt = ledger.commit_record(reanalysis)
    linkage._time_source.advance_to(expiry + timedelta(minutes=1))
    assert ledger.commit_record(reanalysis) == receipt


@pytest.mark.parametrize("action", ("supersession", "comparison"))
@pytest.mark.parametrize(
    ("issued_at", "expires_at", "advance_to"),
    (
        (T0 - timedelta(seconds=1), AFTER, None),
        (NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=1)),
        (NOW, AFTER + timedelta(seconds=1), None),
    ),
)
def test_action_approval_requires_full_trust_and_evaluation_window(
    durable, action, issued_at, expires_at, advance_to
) -> None:
    linkage, ledger, first_revision, first, second = durable
    ledger.commit_record(first)
    if action == "supersession":
        _, candidate = _authorized_reanalysis(linkage, first_revision, first)
        assert candidate.supersession_authorization is not None
        candidate = candidate.model_copy(
            update={
                "supersession_authorization": _resign(
                    candidate.supersession_authorization,
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
            }
        )
        operation = lambda: ledger.commit_record(candidate)
    else:
        ledger.commit_record(second)
        comparison = _comparison(first, second, ledger.active_snapshot())
        comparison = comparison.model_copy(
            update={
                "authority": _resign(
                    comparison.authority,
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
            }
        )
        operation = lambda: ledger.register_comparison(comparison)
    if advance_to is not None:
        linkage._time_source.advance_to(advance_to)
    with pytest.raises(RecordSupersessionConflict, match="authority is invalid"):
        operation()


def test_nested_approval_preflight_rejects_huge_fields_before_serialization(
    durable,
) -> None:
    linkage, ledger, first_revision, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    approval = reanalysis.supersession_authorization
    assert approval is not None
    huge_payload = approval.payload.model_copy(update={"approval_id": "x" * 10_000_000})
    huge_record = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"payload": huge_payload}
            )
        }
    )
    with pytest.raises(ValueError, match="record contract is invalid"):
        record_sha256(huge_record)
    with pytest.raises(RecordSupersessionConflict, match="record contract is invalid"):
        ledger.commit_record(huge_record)

    comparison = _comparison(first, second, ledger.active_snapshot())
    huge_comparison = comparison.model_copy(
        update={
            "authority": comparison.authority.model_copy(
                update={"payload": huge_payload}
            )
        }
    )
    with pytest.raises(ValueError, match="comparison contract is invalid"):
        supersession_module.comparison_sha256(huge_comparison)
    with pytest.raises(
        RecordSupersessionConflict, match="comparison contract is invalid"
    ):
        ledger.register_comparison(huge_comparison)
    huge_snapshot = ledger.active_snapshot().model_copy(
        update={"records": (huge_record,)}
    )
    with pytest.raises(RecordSupersessionConflict, match="record contract is invalid"):
        ledger.replay_snapshot(huge_snapshot)
    with pytest.raises(
        RecordSupersessionConflict, match="comparison identity is invalid"
    ):
        ledger.comparison_status("x" * 10_000_000)


def test_nested_approval_preflight_rejects_hostile_timezone_key_and_extra(
    durable,
) -> None:
    class HostileTimezone(tzinfo):
        def utcoffset(self, value):
            raise AssertionError("timezone hook executed")

        def dst(self, value):
            raise AssertionError("timezone hook executed")

    class HostileKey(str):
        armed = False

        def __hash__(self):
            if self.armed:
                raise AssertionError("key hook executed")
            return super().__hash__()

        def __eq__(self, other):
            if self.armed:
                raise AssertionError("key hook executed")
            return super().__eq__(other)

    class HostileExtra(dict):
        def __eq__(self, other):
            raise AssertionError("extra hook executed")

    linkage, _, first_revision, first, _ = durable
    _, reanalysis = _authorized_reanalysis(linkage, first_revision, first)
    approval = reanalysis.supersession_authorization
    assert approval is not None

    hostile_time = datetime(2026, 9, 29, 12, tzinfo=HostileTimezone())
    time_payload = approval.payload.model_copy(update={"issued_at": hostile_time})
    time_record = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"payload": time_payload}
            )
        }
    )
    with pytest.raises(ValueError, match="record contract is invalid"):
        record_sha256(time_record)

    key_payload = approval.payload.model_copy(deep=True)
    key = HostileKey("approval_id")
    key_payload.__dict__[key] = key_payload.__dict__.pop("approval_id")
    HostileKey.armed = True
    key_record = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"payload": key_payload}
            )
        }
    )
    with pytest.raises(ValueError, match="record contract is invalid"):
        record_sha256(key_record)
    HostileKey.armed = False

    extra_payload = approval.payload.model_copy(deep=True)
    object.__setattr__(extra_payload, "__pydantic_extra__", HostileExtra())
    extra_record = reanalysis.model_copy(
        update={
            "supersession_authorization": approval.model_copy(
                update={"payload": extra_payload}
            )
        }
    )
    with pytest.raises(ValueError, match="record contract is invalid"):
        record_sha256(extra_record)


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_live_sidecar_inode_substitution_fails_closed(durable, suffix) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    sidecar = Path(str(ledger.database) + suffix)
    backup = Path(str(sidecar) + ".bound-backup")
    try:
        with (
            pytest.raises(RecordSupersessionUnsafe, match="sidecar"),
            ledger._connect() as connection,
        ):
            assert sidecar.exists()
            os.replace(sidecar, backup)
            sidecar.write_bytes(b"substituted")
            sidecar.chmod(0o600)
            connection.execute("SELECT COUNT(*) FROM records").fetchone()
    finally:
        if sidecar.exists():
            sidecar.unlink()
        if backup.exists():
            os.replace(backup, sidecar)


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_sidecar_chmod_race_never_follows_substituted_symlink(
    durable, suffix, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    sidecar = Path(str(ledger.database) + suffix)
    backup = Path(str(sidecar) + ".bound-backup")
    target = tmp_path / f"target{suffix}"
    target.write_bytes(b"private")
    target.chmod(0o640)
    original_fchmod = os.fchmod
    swapped = False

    def swapping_fchmod(descriptor, mode):
        nonlocal swapped
        descriptor_metadata = os.fstat(descriptor)
        if not swapped and sidecar.exists():
            sidecar_metadata = sidecar.stat(follow_symlinks=False)
            if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) == (
                sidecar_metadata.st_dev,
                sidecar_metadata.st_ino,
            ):
                os.replace(sidecar, backup)
                sidecar.symlink_to(target)
                swapped = True
        return original_fchmod(descriptor, mode)

    monkeypatch.setattr(supersession_module.os, "fchmod", swapping_fchmod)
    try:
        with pytest.raises(RecordSupersessionUnsafe, match="sidecar"):
            ledger.active_snapshot()
        assert swapped
        assert target.stat().st_mode & 0o777 == 0o640
    finally:
        monkeypatch.setattr(supersession_module.os, "fchmod", original_fchmod)
        if sidecar.is_symlink() or sidecar.exists():
            sidecar.unlink()
        if backup.exists():
            os.replace(backup, sidecar)


def test_preopened_sidecar_descriptors_do_not_prove_sqlite_ownership(durable) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    keeper = sqlite3.connect(ledger.database, isolation_level=None)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("SELECT COUNT(*) FROM records").fetchone()
        for suffix in ("-wal", "-shm"):
            Path(str(ledger.database) + suffix).chmod(0o600)
        before = supersession_module._open_descriptor_identities()
        with pytest.raises(RecordSupersessionUnsafe, match="sidecar"):
            ledger._bind_sidecars(before)
    finally:
        keeper.close()


def test_initialization_main_database_race_never_chmods_substituted_symlink(
    durable, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    linkage, _, _, _, _ = durable
    root = tmp_path / "raced-records"
    database = root / "record-supersession.sqlite3"
    backup = root / "record-supersession.sqlite3.bound-backup"
    target = tmp_path / "main-target"
    target.write_bytes(b"private")
    target.chmod(0o640)
    original_fchmod = os.fchmod
    swapped = False

    def swapping_fchmod(descriptor, mode):
        nonlocal swapped
        descriptor_metadata = os.fstat(descriptor)
        if not swapped and database.exists():
            database_metadata = database.stat(follow_symlinks=False)
            if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) == (
                database_metadata.st_dev,
                database_metadata.st_ino,
            ):
                os.replace(database, backup)
                database.symlink_to(target)
                swapped = True
        return original_fchmod(descriptor, mode)

    monkeypatch.setattr(supersession_module.os, "fchmod", swapping_fchmod)
    try:
        with pytest.raises(RecordSupersessionUnsafe):
            RecordSupersessionStore(root, linkage_store=linkage)
        assert swapped
        assert target.stat().st_mode & 0o777 == 0o640
    finally:
        monkeypatch.setattr(supersession_module.os, "fchmod", original_fchmod)
        if database.is_symlink() or database.exists():
            database.unlink()
        if backup.exists():
            os.replace(backup, database)


def test_constructor_root_swap_never_mutates_or_populates_substituted_directory(
    durable, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    linkage, _, _, _, _ = durable
    root = tmp_path / "existing-root"
    backup = tmp_path / "existing-root.bound-backup"
    target = tmp_path / "attacker-target"
    root.mkdir(mode=0o700)
    target.mkdir(mode=0o750)
    original_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path) == root:
            os.replace(root, backup)
            root.symlink_to(target, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(supersession_module.os, "open", swapping_open)
    try:
        with pytest.raises(RecordSupersessionUnsafe, match="root"):
            RecordSupersessionStore(root, linkage_store=linkage)
        assert swapped
        assert target.stat().st_mode & 0o777 == 0o750
        assert not (target / "record-supersession.sqlite3").exists()
    finally:
        monkeypatch.setattr(supersession_module.os, "open", original_open)
        if root.is_symlink() or root.exists():
            root.unlink()
        if backup.exists():
            os.replace(backup, root)


def test_constructor_rejects_hostile_path_objects_before_hooks(durable) -> None:
    class HostilePath(type(Path())):
        def __fspath__(self):
            raise AssertionError("path hook executed")

        @property
        def parts(self):
            raise AssertionError("parts hook executed")

    class HostileProxy:
        def __fspath__(self):
            raise AssertionError("proxy hook executed")

    linkage, _, _, _, _ = durable
    hostile = HostilePath("/private/hostile")
    with pytest.raises(TypeError, match="exact local path"):
        RecordSupersessionStore(hostile, linkage_store=linkage)
    with pytest.raises(TypeError, match="exact local path"):
        RecordSupersessionStore(HostileProxy(), linkage_store=linkage)  # type: ignore[arg-type]
    with pytest.raises(RecordSupersessionUnsafe, match="root is unsafe"):
        RecordSupersessionStore("x" * 4097, linkage_store=linkage)
    with pytest.raises(RecordSupersessionUnsafe, match="root is unsafe"):
        RecordSupersessionStore("unsafe\0path", linkage_store=linkage)


def test_constructor_snapshots_exact_path_parts(durable, tmp_path: Path) -> None:
    linkage, _, _, _, _ = durable
    caller = tmp_path / "captured-root"
    caller_parts = object.__getattribute__(caller, "_parts")
    store = RecordSupersessionStore(caller, linkage_store=linkage)
    try:
        caller_parts[:] = ["/", "private", "mutated-after-capture"]
        assert store.root == tmp_path / "captured-root"
        assert store.database.parent == store.root
    finally:
        store.close()


def test_sqlite_connect_root_swap_cannot_write_substituted_directory(
    durable, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    root = ledger.root
    backup = tmp_path / "records.bound-backup"
    target = tmp_path / "sqlite-target"
    target.mkdir(mode=0o750)
    original_connect = sqlite3.connect
    swapped = False

    def swapping_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            os.replace(root, backup)
            root.symlink_to(target, target_is_directory=True)
            swapped = True
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(supersession_module.sqlite3, "connect", swapping_connect)
    try:
        with pytest.raises(RecordSupersessionUnsafe):
            ledger.active_snapshot()
        assert swapped
        assert target.stat().st_mode & 0o777 == 0o750
        assert tuple(target.iterdir()) == ()
    finally:
        monkeypatch.setattr(supersession_module.sqlite3, "connect", original_connect)
        if root.is_symlink() or root.exists():
            root.unlink()
        if backup.exists():
            os.replace(backup, root)


def test_paused_sqlite_connect_never_redirects_unrelated_thread_cwd_or_io(
    durable, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    original_connect = sqlite3.connect
    entered = Event()
    release = Event()
    host = tmp_path / "unrelated-host"
    host.mkdir(mode=0o700)
    original_cwd = Path.cwd()

    def paused_connect(database, *args, **kwargs):
        entered.set()
        if not release.wait(timeout=3):
            raise AssertionError("paused connect was not released")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(supersession_module.sqlite3, "connect", paused_connect)
    os.chdir(host)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ledger.active_snapshot)
            assert entered.wait(timeout=2)
            assert Path.cwd() == host
            Path("unrelated-relative-output.txt").write_text("outside-ledger")
            assert (host / "unrelated-relative-output.txt").read_text() == (
                "outside-ledger"
            )
            assert not (ledger.root / "unrelated-relative-output.txt").exists()
            release.set()
            future.result(timeout=3)
    finally:
        release.set()
        os.chdir(original_cwd)
        monkeypatch.setattr(supersession_module.sqlite3, "connect", original_connect)


def test_database_path_substitution_during_connect_fails_closed(
    durable, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger, _, first, _ = durable
    ledger.commit_record(first)
    backup = ledger.database.with_suffix(".bound-backup")
    original_connect = sqlite3.connect
    swapped = False

    def substituting_connect(database, *args, **kwargs):
        nonlocal swapped
        if Path(database).name == ledger.database.name and not swapped:
            swapped = True
            os.replace(ledger.database, backup)
            original_connect(ledger.database).close()
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(supersession_module.sqlite3, "connect", substituting_connect)
    try:
        with pytest.raises(RecordSupersessionUnsafe, match="identity|private"):
            ledger.active_snapshot()
    finally:
        monkeypatch.setattr(supersession_module.sqlite3, "connect", original_connect)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(ledger.database) + suffix)
            if path.exists():
                path.unlink()
        if backup.exists():
            os.replace(backup, ledger.database)


def test_same_key_correction_preserves_history_and_stales_comparison(durable) -> None:
    linkage, ledger, _, first, second = durable
    ledger.commit_record(first)
    ledger.commit_record(second)
    comparison = _comparison(first, second, ledger.active_snapshot())
    ledger.register_comparison(comparison)
    _correct_first_linkage(linkage, first)
    snapshot = ledger.active_snapshot()
    assert first not in snapshot.records
    status = ledger.comparison_status(comparison.comparison_id)
    assert status.state == ComparisonState.STALE
    assert InvalidationReason.LINKAGE_CHANGED_OR_TOMBSTONED in status.reasons


def test_resealed_historical_receipt_tamper_fails_after_correction(durable) -> None:
    linkage, ledger, _, first, _ = durable
    ledger.commit_record(first)
    _correct_first_linkage(linkage, first)
    forged = first.model_copy(update={"activation_receipt_sha256": "f" * 64})
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE records SET record_sha256=?, record_json=? WHERE record_id=?",
            (record_sha256(forged), canonical_contract_bytes(forged), first.record_id),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
            (RecordSupersessionStore._state_head(connection),),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="authority binding"):
        ledger.active_snapshot()


def test_resealed_historical_receipt_tamper_fails_after_tombstone(durable) -> None:
    linkage, ledger, _, first, _ = durable
    ledger.commit_record(first)
    _tombstone_first_linkage(linkage, first)
    forged = first.model_copy(update={"activation_receipt_sha256": "f" * 64})
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE records SET record_sha256=?, record_json=? WHERE record_id=?",
            (record_sha256(forged), canonical_contract_bytes(forged), first.record_id),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
            (RecordSupersessionStore._state_head(connection),),
        )
    connection.close()
    with pytest.raises(RecordSupersessionUnsafe, match="authority binding"):
        ledger.active_snapshot()
