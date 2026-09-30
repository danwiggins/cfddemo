"""D01/D04 protected transactional linkage-store tests."""

from __future__ import annotations

import base64
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_all_start_methods, get_context
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import pytest

from evidence_inspector.provider_linkage import (
    ApprovalPurpose,
    AuthorizedLinkageRevision,
    LinkageOperation,
    LinkageReasonCode,
    ProviderApprovalPayload,
    ProviderRole,
    SignedProviderApproval,
    approval_payload_bytes,
    linkage_revision_sha256,
    prepare_authorized_linkage_revision,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    ProviderLinkageStoreConflict,
    ProviderLinkageStoreSchemaError,
    ProviderLinkageStoreUnsafe,
)
from tests.test_provider_linkage import (
    AFTER,
    ISSUER,
    KEY_ID,
    NOW,
    PRIVATE_KEY,
    PROVIDER,
    _approval,
    _consume,
    _correction_approvals,
    _create_approval,
    _revision,
    _token,
    _trust,
)


def _pins() -> dict[str, str]:
    return {PROVIDER: provider_trust_snapshot_sha256(_trust())}


def _store(root: Path) -> ProviderLinkageStore:
    return ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        clock=lambda: NOW,
    )


def _record(digit: str = "c"):
    revision = _revision()
    record, _ = _consume(revision, (_create_approval(revision, digit),))
    return record


def _process_commit(
    root: str,
    record_json: str,
    ready: Queue,
    start: EventType,
    outcomes: Queue,
) -> None:
    record = AuthorizedLinkageRevision.model_validate_json(record_json)
    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        clock=lambda: NOW,
    )
    try:
        ready.put("ready")
        start.wait(timeout=10)
        try:
            store.commit_authorized_revision(record)
        except ProviderLinkageStoreConflict:
            outcomes.put("conflict")
        else:
            outcomes.put("committed")
    finally:
        store.close()


def test_commit_consumes_approval_and_enables_only_live_store_projection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    record = _record()
    with _store(root) as store:
        receipt = store.commit_authorized_revision(record)
        snapshot = store.active_snapshot()
        store.verify_current_receipt(receipt)

    assert snapshot.revisions == (record.revision,)
    assert snapshot.receipts == (receipt,)
    assert receipt.state_version == 1
    assert receipt.store_id == snapshot.store_id
    assert receipt.authorized_record_sha256
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "linkage.sqlite3").stat().st_mode & 0o777) == 0o600


def test_fresh_store_has_verified_empty_snapshot(tmp_path: Path) -> None:
    with _store(tmp_path / "protected") as store:
        snapshot = store.active_snapshot()

    assert snapshot.state_version == 0
    assert snapshot.revisions == ()
    assert snapshot.receipts == ()


def test_exact_retry_is_idempotent_without_advancing_state(tmp_path: Path) -> None:
    record = _record()
    with _store(tmp_path / "protected") as store:
        first = store.commit_authorized_revision(record)
        repeated = store.commit_authorized_revision(record)

    assert repeated == first


def test_receipt_is_bound_to_one_store_epoch_and_storage_identity(
    tmp_path: Path,
) -> None:
    record = _record()
    with (
        _store(tmp_path / "first") as first_store,
        _store(tmp_path / "second") as second_store,
    ):
        first = first_store.commit_authorized_revision(record)
        second = second_store.commit_authorized_revision(record)
        assert first != second
        assert first.store_id != second.store_id
        assert first.storage_identity_sha256 != second.storage_identity_sha256
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            second_store.verify_current_receipt(first)


def test_approval_and_nonce_cannot_be_reused_on_another_revision(
    tmp_path: Path,
) -> None:
    first = _record("c")
    other_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    other, _ = _consume(
        other_revision,
        (_create_approval(other_revision, "c"),),
    )
    with _store(tmp_path / "protected") as store:
        store.commit_authorized_revision(first)
        with pytest.raises(ProviderLinkageStoreConflict, match="approval or identity"):
            store.commit_authorized_revision(other)
        assert store.active_snapshot().revisions == (first.revision,)


def test_approval_and_nonce_replay_namespace_is_global_across_providers(
    tmp_path: Path,
) -> None:
    first = _record("c")
    other_provider = _token("provider", "f")
    revision = _revision(linkage_id=_token("linkage", "f")).model_copy(
        update={"provider_namespace": other_provider}
    )
    trust = _trust(snapshot_digit="f").model_copy(
        update={"provider_namespace": other_provider}
    )
    trust_sha256 = provider_trust_snapshot_sha256(trust)
    payload = ProviderApprovalPayload(
        approval_id=first.approvals[0].payload.approval_id,
        provider_namespace=other_provider,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=_token("principal", "f"),
        role=ProviderRole.LINKER,
        purpose=ApprovalPurpose.CREATE_LINKAGE,
        proposed_revision_sha256=linkage_revision_sha256(revision),
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=trust_sha256,
        nonce=first.approvals[0].payload.nonce,
        issued_at=NOW,
        expires_at=AFTER,
    )
    approval = SignedProviderApproval(
        payload=payload,
        signature_base64=base64.b64encode(
            PRIVATE_KEY.sign(approval_payload_bytes(payload))
        ).decode("ascii"),
    )
    other = prepare_authorized_linkage_revision(
        revision,
        previous_revision=None,
        approvals=(approval,),
        trust_snapshot=trust,
        expected_trust_snapshot_sha256=trust_sha256,
        evaluated_at=NOW,
    )
    store = ProviderLinkageStore(
        tmp_path / "protected",
        expected_trust_snapshot_sha256_by_provider={
            PROVIDER: provider_trust_snapshot_sha256(_trust()),
            other_provider: trust_sha256,
        },
        clock=lambda: NOW,
    )
    try:
        store.commit_authorized_revision(first)
        with pytest.raises(ProviderLinkageStoreConflict, match="approval or identity"):
            store.commit_authorized_revision(other)
    finally:
        store.close()


def test_cross_connection_race_consumes_approval_once(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    first_store = _store(root)
    second_store = _store(root)
    first = _record("c")
    other_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    other, _ = _consume(
        other_revision,
        (_create_approval(other_revision, "c"),),
    )

    def commit(store: ProviderLinkageStore, record: object) -> str:
        try:
            store.commit_authorized_revision(record)  # type: ignore[arg-type]
        except ProviderLinkageStoreConflict:
            return "conflict"
        return "committed"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(commit, first_store, first),
                pool.submit(commit, second_store, other),
            )
            outcomes = sorted(item.result() for item in futures)
        assert outcomes == ["committed", "conflict"]
        assert len(first_store.active_snapshot().revisions) == 1
    finally:
        first_store.close()
        second_store.close()


@pytest.mark.skipif("fork" not in get_all_start_methods(), reason="requires fork")
def test_cross_process_race_consumes_approval_once(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    _store(root).close()
    first = _record("c")
    other_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    other, _ = _consume(
        other_revision,
        (_create_approval(other_revision, "c"),),
    )
    context = get_context("fork")
    ready = context.Queue()
    outcomes = context.Queue()
    start = context.Event()
    processes = (
        context.Process(
            target=_process_commit,
            args=(str(root), first.model_dump_json(), ready, start, outcomes),
        ),
        context.Process(
            target=_process_commit,
            args=(str(root), other.model_dump_json(), ready, start, outcomes),
        ),
    )
    for process in processes:
        process.start()
    assert [ready.get(timeout=10), ready.get(timeout=10)] == ["ready", "ready"]
    start.set()
    observed = sorted((outcomes.get(timeout=15), outcomes.get(timeout=15)))
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert observed == ["committed", "conflict"]
    with _store(root) as store:
        assert len(store.active_snapshot().revisions) == 1


@pytest.mark.parametrize("reused", ("analysis", "measurement"))
def test_full_history_identity_ownership_rolls_back_conflict(
    tmp_path: Path,
    reused: str,
) -> None:
    first = _record("c")
    other_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=(
            first.revision.technical.analysis_record_id
            if reused == "analysis"
            else _token("analysis", "d")
        ),
        measurement=(
            first.revision.technical.measurement_id
            if reused == "measurement"
            else _token("measurement", "d")
        ),
    )
    other, _ = _consume(
        other_revision,
        (_create_approval(other_revision, "d"),),
    )
    with _store(tmp_path / "protected") as store:
        first_receipt = store.commit_authorized_revision(first)
        with pytest.raises(ProviderLinkageStoreConflict, match="history conflicts"):
            store.commit_authorized_revision(other)
        store.verify_current_receipt(first_receipt)


def test_correction_invalidates_old_receipt_and_tombstone_removes_active(
    tmp_path: Path,
) -> None:
    first = _record("c")
    correction_revision = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=first.revision,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    )
    correction, _ = _consume(
        correction_revision,
        _correction_approvals(correction_revision),
        previous=first.revision,
    )
    tombstone_revision = _revision(
        revision=3,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=correction_revision,
        subject=correction_revision.biological.subject_token,
        collection=correction_revision.biological.collection_token,
        specimen=correction_revision.biological.specimen_token,
    )
    tombstone, _ = _consume(
        tombstone_revision,
        (
            _approval(
                tombstone_revision,
                role=ProviderRole.LINKER,
                purpose=ApprovalPurpose.TOMBSTONE_LINKAGE,
                digit="a",
            ),
            _approval(
                tombstone_revision,
                role=ProviderRole.REVIEWER,
                purpose=ApprovalPurpose.TOMBSTONE_LINKAGE,
                digit="b",
            ),
        ),
        previous=correction_revision,
    )
    with _store(tmp_path / "protected") as store:
        old = store.commit_authorized_revision(first)
        current = store.commit_authorized_revision(correction)
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.verify_current_receipt(old)
        store.verify_current_receipt(current)
        store.commit_authorized_revision(tombstone)
        assert store.active_snapshot().revisions == ()


def test_unrelated_commit_stales_snapshot_receipt(tmp_path: Path) -> None:
    first = _record("c")
    second_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    second, _ = _consume(
        second_revision,
        (_create_approval(second_revision, "d"),),
    )
    with _store(tmp_path / "protected") as store:
        stale = store.commit_authorized_revision(first)
        store.commit_authorized_revision(second)
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.verify_current_receipt(stale)


def test_forged_receipt_is_not_authority(tmp_path: Path) -> None:
    record = _record()
    with _store(tmp_path / "protected") as store:
        receipt = store.commit_authorized_revision(record)
        forged = receipt.model_copy(update={"state_head_sha256": "0" * 64})
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.verify_current_receipt(forged)


def test_record_bytes_and_digest_are_bound_into_state_head_and_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    record = _record("c")
    with _store(root) as store:
        receipt = store.commit_authorized_revision(record)
        alternate, _ = _consume(
            record.revision,
            (_create_approval(record.revision, "d"),),
        )
        connection = sqlite3.connect(root / "linkage.sqlite3")
        connection.execute(
            "UPDATE linkage_revisions SET record_json=?",
            (alternate.model_dump_json().encode("utf-8"),),
        )
        connection.commit()
        connection.close()

        with pytest.raises(ProviderLinkageStoreSchemaError, match="record is invalid"):
            store.verify_current_receipt(receipt)


def test_expired_authority_disables_commit_and_active_projection(
    tmp_path: Path,
) -> None:
    current = [NOW]
    root = tmp_path / "protected"
    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        clock=lambda: current[0],
    )
    try:
        receipt = store.commit_authorized_revision(_record())
        store.verify_current_receipt(receipt)
        current[0] = _trust().expires_at
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.active_snapshot()
    finally:
        store.close()


def test_reopen_rejects_trust_pin_drift(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    _store(root).close()
    with pytest.raises(ProviderLinkageStoreConflict, match="trust pins"):
        ProviderLinkageStore(
            root,
            expected_trust_snapshot_sha256_by_provider={PROVIDER: "0" * 64},
        )


def test_root_or_database_substitution_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    moved = tmp_path / "moved"
    os.rename(root, moved)
    root.mkdir(mode=0o700)
    try:
        with pytest.raises(ProviderLinkageStoreUnsafe, match="root changed"):
            store.active_snapshot()
    finally:
        store.close()


def test_private_modes_are_revalidated_before_every_authority_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    try:
        root.chmod(0o777)
        with pytest.raises(ProviderLinkageStoreUnsafe, match="root changed"):
            store.active_snapshot()
        root.chmod(0o700)
        (root / "linkage.sqlite3").chmod(0o666)
        with pytest.raises(ProviderLinkageStoreUnsafe, match="database changed"):
            store.active_snapshot()
    finally:
        store.close()

    root.chmod(0o777)
    with pytest.raises(ProviderLinkageStoreUnsafe, match="root is unsafe"):
        _store(root)
    root.chmod(0o700)
    (root / "linkage.sqlite3").chmod(0o666)
    with pytest.raises(ProviderLinkageStoreUnsafe, match="database is unsafe"):
        _store(root)


def test_same_inventory_malformed_schema_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    _store(root).close()
    database = root / "linkage.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX linkage_revision_order")
    connection.execute(
        "CREATE INDEX linkage_revision_order ON linkage_revisions(linkage_id, revision)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderLinkageStoreSchemaError, match="schema"):
        _store(root)


def test_live_store_rechecks_exact_schema_before_authority_reads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute("DROP INDEX linkage_revision_order")
    connection.execute(
        "CREATE INDEX linkage_revision_order ON linkage_revisions(linkage_id, revision)"
    )
    connection.commit()
    connection.close()
    try:
        with pytest.raises(ProviderLinkageStoreSchemaError, match="schema"):
            store.active_snapshot()
    finally:
        store.close()


def test_live_trigger_cannot_delete_consumption_and_reopen_replay(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    first = _record("c")
    other_revision = _revision(
        linkage_id=_token("linkage", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
    )
    other, _ = _consume(
        other_revision,
        (_create_approval(other_revision, "c"),),
    )
    store = _store(root)
    store.commit_authorized_revision(first)
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute("DELETE FROM approval_consumptions")
    connection.execute(
        """CREATE TRIGGER erase_consumption
           AFTER INSERT ON approval_consumptions
           BEGIN DELETE FROM approval_consumptions; END"""
    )
    connection.commit()
    connection.close()
    try:
        with pytest.raises(ProviderLinkageStoreSchemaError, match="schema"):
            store.commit_authorized_revision(other)
        with pytest.raises(ProviderLinkageStoreSchemaError, match="schema"):
            store.active_snapshot()
    finally:
        store.close()


def test_relative_or_symlink_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProviderLinkageStoreUnsafe, match="absolute"):
        _store(Path("relative"))
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProviderLinkageStoreUnsafe, match="root is unsafe"):
        _store(alias)


def test_invalid_trust_pin_or_clock_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProviderLinkageStoreUnsafe, match="trust pins are invalid"):
        ProviderLinkageStore(
            tmp_path / "invalid-pins",
            expected_trust_snapshot_sha256_by_provider={"free text": "0" * 64},
        )
    store = ProviderLinkageStore(
        tmp_path / "invalid-clock",
        expected_trust_snapshot_sha256_by_provider=_pins(),
        clock=lambda: NOW.replace(tzinfo=None),
    )
    try:
        with pytest.raises(ProviderLinkageStoreUnsafe, match="clock is invalid"):
            store.commit_authorized_revision(_record())
    finally:
        store.close()


def test_receipt_schema_rejects_free_text_identity() -> None:
    with pytest.raises(ValueError):
        CommittedLinkageReceipt(
            provider_namespace="free text",
            linkage_id=_token("linkage", "c"),
            revision=1,
            linkage_revision_sha256="a" * 64,
            state_version=1,
            state_head_sha256="b" * 64,
        )
