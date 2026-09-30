"""D04 durable record supersession and comparison-invalidation tests."""

from __future__ import annotations

import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

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
