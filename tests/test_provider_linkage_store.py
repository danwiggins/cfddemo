"""D01/D04 protected transactional linkage-store tests."""

from __future__ import annotations

import base64
import os
import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from multiprocessing import get_all_start_methods, get_context
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import pytest

import evidence_inspector.provider_linkage_store as linkage_store_module
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
    AuthorityTimeSource,
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    ProviderLinkageStoreConflict,
    ProviderLinkageStoreSchemaError,
    ProviderLinkageStoreUnsafe,
    _trust_pins_sha256,
    provider_linkage_store_process_integrity_is_valid,
    require_provider_linkage_store_process_integrity,
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


def test_detected_module_tamper_is_explicit_process_integrity_failure() -> None:
    function = linkage_store_module._authority_time_text
    original = function.__kwdefaults__
    assert provider_linkage_store_process_integrity_is_valid()
    try:
        function.__kwdefaults__ = {"attacker_resealed": True}
        assert not provider_linkage_store_process_integrity_is_valid()
        with pytest.raises(
            ProviderLinkageStoreUnsafe,
            match="process integrity check failed",
        ):
            require_provider_linkage_store_process_integrity()
    finally:
        function.__kwdefaults__ = original

    require_provider_linkage_store_process_integrity()


@pytest.mark.parametrize("wrapped", (False, True))
def test_authority_fence_code_tamper_fails_process_integrity(wrapped: bool) -> None:
    public_fence = ProviderLinkageStore.authority_read_fence
    function = public_fence.__wrapped__ if wrapped else public_fence
    original = function.__code__

    if wrapped:
        replacement = ProviderLinkageStore.active_snapshot.__code__
    else:
        captured = object()

        def replacement_wrapper(*args: object, **kwargs: object) -> object:
            del args, kwargs
            return captured

        replacement = replacement_wrapper.__code__

    assert provider_linkage_store_process_integrity_is_valid()
    try:
        function.__code__ = replacement
        assert not provider_linkage_store_process_integrity_is_valid()
    finally:
        function.__code__ = original

    require_provider_linkage_store_process_integrity()


def _pins() -> dict[str, str]:
    return {PROVIDER: provider_trust_snapshot_sha256(_trust())}


class _OnePassPins(Mapping[str, str]):
    def __init__(self, pairs: tuple[tuple[str, str], ...], *, length: object) -> None:
        self._pairs = list(pairs)
        self._length = length
        self.iter_calls = 0
        self.get_calls = 0
        self.len_calls = 0
        self.items_calls = 0

    def __iter__(self) -> Iterator[str]:
        self.iter_calls += 1
        if self.iter_calls != 1:
            raise AssertionError("trust mapping was reiterated")
        return iter(tuple(key for key, _ in self._pairs))

    def __getitem__(self, key: str) -> str:
        self.get_calls += 1
        for index, (candidate, value) in enumerate(self._pairs):
            if candidate == key:
                self._pairs.pop(index)
                return value
        raise KeyError(key)

    def __len__(self) -> int:
        self.len_calls += 1
        if isinstance(self._length, BaseException):
            raise self._length
        assert type(self._length) is int
        return self._length

    def items(self) -> object:
        self.items_calls += 1
        raise AssertionError("trust mapping items view was accessed")


class _OverlongPins(Mapping[str, str]):
    def __init__(self) -> None:
        self.yielded = 0
        self.get_calls = 0
        self.len_calls = 0

    def __iter__(self) -> Iterator[str]:
        index = 0
        while True:
            self.yielded += 1
            yield f"provider_{index:032x}"
            index += 1

    def __getitem__(self, key: str) -> str:
        self.get_calls += 1
        return "a" * 64

    def __len__(self) -> int:
        self.len_calls += 1
        raise AssertionError("overlong mapping length was accessed")


class _DuplicatePins(Mapping[str, str]):
    def __init__(self, values: tuple[str, str]) -> None:
        self._values = values
        self.get_calls = 0

    def __iter__(self) -> Iterator[str]:
        return iter((PROVIDER, PROVIDER))

    def __getitem__(self, key: str) -> str:
        value = self._values[self.get_calls]
        self.get_calls += 1
        return value

    def __len__(self) -> int:
        raise AssertionError("duplicate mapping length was accessed")


def _store(root: Path) -> ProviderLinkageStore:
    return ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=AuthorityTimeSource.fixed(NOW),
    )


def _record(digit: str = "c"):
    revision = _revision()
    record, _ = _consume(revision, (_create_approval(revision, digit),))
    return record


def _reseal_state_head(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='state_head_sha256'",
        (ProviderLinkageStore._state_head(connection),),
    )


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
        time_source=AuthorityTimeSource.fixed(NOW),
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


def test_authority_time_floor_is_monotonic_and_persists_across_reopen(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    time_source = AuthorityTimeSource.fixed(NOW)

    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    try:
        receipt = store.commit_authorized_revision(_record())
        time_source.advance_to(AFTER + timedelta(hours=1))
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.active_snapshot()
        with pytest.raises(ValueError, match="cannot move backwards"):
            time_source.advance_to(NOW)
        time_source._current = NOW  # type: ignore[attr-defined]
        with pytest.raises(ProviderLinkageStoreUnsafe, match="moved backwards"):
            store.active_snapshot()
    finally:
        store.close()

    reopened = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    try:
        with pytest.raises(ProviderLinkageStoreUnsafe, match="moved backwards"):
            reopened.active_snapshot()
        assert receipt.state_version == 1
    finally:
        reopened.close()


def test_authority_read_fence_persists_nested_time_floor_advance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    time_source = AuthorityTimeSource.fixed(NOW)
    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    later = AFTER + timedelta(hours=1)
    try:
        time_source.advance_to(later)
        with store.authority_read_fence():
            assert store.active_snapshot().state_version == 0
    finally:
        store.close()

    time_source._current = NOW  # type: ignore[attr-defined]
    reopened = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    try:
        with pytest.raises(ProviderLinkageStoreUnsafe, match="moved backwards"):
            reopened.active_snapshot()
    finally:
        reopened.close()


def test_persisted_floor_survives_registry_and_parser_poisoning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    time_source = AuthorityTimeSource.fixed(NOW)
    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    identity = id(store)
    original_parser = linkage_store_module._authority_time_from_text
    original_capture_defaults = (
        linkage_store_module._capture_pinned_store_now.__defaults__
    )
    original_entry = linkage_store_module._STORE_TIME_SOURCES[identity]
    try:
        store.commit_authorized_revision(_record())
        time_source.advance_to(AFTER + timedelta(hours=1))
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.active_snapshot()
        persisted_entry = linkage_store_module._STORE_TIME_SOURCES[identity]
        time_source._current = NOW  # type: ignore[attr-defined]
        linkage_store_module._STORE_TIME_SOURCES[identity] = (
            persisted_entry[0],
            time_source,
            None,
        )
        linkage_store_module._authority_time_from_text = lambda _: NOW
        linkage_store_module._capture_pinned_store_now.__defaults__ = (
            lambda _: NOW,
            lambda _: (AFTER + timedelta(hours=1)).isoformat(),
            lambda _: NOW,
        )

        with pytest.raises(ProviderLinkageStoreUnsafe, match="moved backwards"):
            store.active_snapshot()
    finally:
        linkage_store_module._authority_time_from_text = original_parser
        linkage_store_module._capture_pinned_store_now.__defaults__ = (
            original_capture_defaults
        )
        linkage_store_module._STORE_TIME_SOURCES[identity] = original_entry
        store.close()


def test_v1_store_metadata_migrates_once_to_persisted_authority_floor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    with _store(root):
        pass
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute("DELETE FROM metadata WHERE key='authority_time_floor'")
    connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with _store(root) as reopened:
        assert reopened.active_snapshot().state_version == 0

    connection = sqlite3.connect(root / "linkage.sqlite3")
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    connection.close()
    assert metadata["schema_version"] == "3"
    assert metadata["authority_time_floor"] == NOW.isoformat()


def test_v2_nonempty_store_migrates_immutable_activation_coordinates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "protected"
    record = _record("c")
    with _store(root) as store:
        original = store.commit_authorized_revision(record)
    with sqlite3.connect(root / "linkage.sqlite3") as connection:
        connection.execute(
            "ALTER TABLE linkage_revisions DROP COLUMN activation_state_version"
        )
        connection.execute(
            "ALTER TABLE linkage_revisions DROP COLUMN activation_state_head_sha256"
        )
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    with _store(root) as reopened:
        snapshot = reopened.active_snapshot()
        assert snapshot.activation_receipts == (original,)


@pytest.mark.parametrize(
    "invalid_floor",
    (
        "not-a-time",
        NOW.replace(tzinfo=None).isoformat(),
        NOW.replace(microsecond=1).isoformat(),
        NOW.isoformat().replace("+00:00", "Z"),
    ),
)
def test_malformed_persisted_authority_time_floor_is_rejected(
    tmp_path: Path,
    invalid_floor: str,
) -> None:
    root = tmp_path / "protected"
    _store(root).close()
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='authority_time_floor'",
        (invalid_floor,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProviderLinkageStoreSchemaError, match="authority time"):
        _store(root)


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
        time_source=AuthorityTimeSource.fixed(NOW),
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


def test_unrelated_commit_preserves_immutable_activation_receipt(
    tmp_path: Path,
) -> None:
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
        receipt = store.commit_authorized_revision(first)
        store.commit_authorized_revision(second)
        snapshot = store.active_snapshot()
        assert receipt in snapshot.activation_receipts
        with pytest.raises(ProviderLinkageStoreConflict, match="not current"):
            store.verify_current_receipt(receipt)


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
    time_source = AuthorityTimeSource.fixed(NOW)
    root = tmp_path / "protected"
    store = ProviderLinkageStore(
        root,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=time_source,
    )
    try:
        receipt = store.commit_authorized_revision(_record())
        store.verify_current_receipt(receipt)
        time_source.advance_to(_trust().expires_at)
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


def test_database_substitution_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    database = root / "linkage.sqlite3"
    moved = root / "linkage.sqlite3.moved"
    database.rename(moved)
    sqlite3.connect(database).close()
    database.chmod(0o600)
    try:
        with pytest.raises(ProviderLinkageStoreUnsafe, match="database changed"):
            store.active_snapshot()
    finally:
        store.close()


@pytest.mark.parametrize(
    "key",
    (
        "store_id",
        "store_epoch_sha256",
        "storage_identity_sha256",
        "trust_pins_sha256",
    ),
)
def test_resealed_persisted_store_identity_mutation_fails_closed(
    tmp_path: Path,
    key: str,
) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    store.commit_authorized_revision(_record())
    connection = sqlite3.connect(root / "linkage.sqlite3")
    current = connection.execute(
        "SELECT value FROM metadata WHERE key=?",
        (key,),
    ).fetchone()[0]
    candidates = (
        ("store_" + "0" * 32, "store_" + "1" * 32)
        if key == "store_id"
        else ("0" * 64, "1" * 64)
    )
    replacement = next(value for value in candidates if value != current)
    connection.execute(
        "UPDATE metadata SET value=? WHERE key=?",
        (replacement, key),
    )
    _reseal_state_head(connection)
    connection.commit()
    connection.close()
    try:
        with pytest.raises(ProviderLinkageStoreSchemaError, match="identity state"):
            store.active_snapshot()
    finally:
        store.close()


def test_resealed_persisted_trust_pin_drift_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    store.commit_authorized_revision(_record())
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute(
        "UPDATE trust_pins SET trust_snapshot_sha256=?",
        ("4" * 64,),
    )
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='trust_pins_sha256'",
        (_trust_pins_sha256({PROVIDER: "4" * 64}),),
    )
    _reseal_state_head(connection)
    connection.commit()
    connection.close()
    try:
        with pytest.raises(ProviderLinkageStoreSchemaError):
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


def test_deleted_consumption_cannot_be_healed_by_next_commit(
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
    receipt = store.commit_authorized_revision(first)
    approval = first.approvals[0]
    connection = sqlite3.connect(root / "linkage.sqlite3")
    connection.execute("DELETE FROM approval_consumptions")
    connection.commit()
    connection.close()
    try:
        with pytest.raises(
            ProviderLinkageStoreSchemaError, match="consumption history"
        ):
            store.commit_authorized_revision(other)

        connection = sqlite3.connect(root / "linkage.sqlite3")
        connection.execute(
            "INSERT INTO approval_consumptions VALUES(?, ?, ?, ?, ?)",
            (
                first.revision.provider_namespace,
                approval.payload.approval_id,
                approval.payload.nonce,
                receipt.linkage_revision_sha256,
                provider_trust_snapshot_sha256(first.trust_snapshot),
            ),
        )
        connection.commit()
        connection.close()
        store.verify_current_receipt(receipt)
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutation",
    ("delete_revision", "alter_revision_index", "alter_operation"),
)
def test_deleted_or_altered_revision_history_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "protected"
    store = _store(root)
    store.commit_authorized_revision(_record("c"))
    connection = sqlite3.connect(root / "linkage.sqlite3")
    if mutation == "delete_revision":
        connection.execute("DELETE FROM approval_consumptions")
        connection.execute("DELETE FROM linkage_revisions")
    elif mutation == "alter_revision_index":
        connection.execute("UPDATE linkage_revisions SET revision=2")
    else:
        connection.execute("UPDATE linkage_revisions SET operation='tombstone'")
    connection.commit()
    connection.close()
    try:
        with pytest.raises(ProviderLinkageStoreSchemaError):
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


def test_invalid_trust_pin_or_time_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProviderLinkageStoreUnsafe, match="trust pins are invalid"):
        ProviderLinkageStore(
            tmp_path / "invalid-pins",
            expected_trust_snapshot_sha256_by_provider={"free text": "0" * 64},
        )
    with pytest.raises(ProviderLinkageStoreUnsafe, match="time source is invalid"):
        AuthorityTimeSource.fixed(NOW.replace(tzinfo=None))


@pytest.mark.parametrize("reported_length", (0, 10_000, RuntimeError("unused")))
def test_trust_pin_capture_ignores_caller_length_and_items_views(
    tmp_path: Path,
    reported_length: object,
) -> None:
    pins = _OnePassPins(tuple(_pins().items()), length=reported_length)
    with ProviderLinkageStore(
        tmp_path / f"one-pass-{type(reported_length).__name__}-{reported_length!s}",
        expected_trust_snapshot_sha256_by_provider=pins,
        time_source=AuthorityTimeSource.fixed(NOW),
    ) as store:
        assert store.active_snapshot().state_version == 0
    assert pins.iter_calls == 1
    assert pins.get_calls == 1
    assert pins.len_calls == 0
    assert pins.items_calls == 0


def test_overlong_trust_pin_iterator_stops_at_max_plus_one_without_root(
    tmp_path: Path,
) -> None:
    pins = _OverlongPins()
    root = tmp_path / "overlong-pins"
    with pytest.raises(ProviderLinkageStoreUnsafe, match="pin count"):
        ProviderLinkageStore(
            root,
            expected_trust_snapshot_sha256_by_provider=pins,
            time_source=AuthorityTimeSource.fixed(NOW),
        )
    assert pins.yielded == linkage_store_module.MAX_PROVIDER_TRUST_PINS + 1
    assert pins.get_calls == linkage_store_module.MAX_PROVIDER_TRUST_PINS
    assert pins.len_calls == 0
    assert not root.exists()


@pytest.mark.parametrize("conflicting", (False, True))
def test_duplicate_or_conflicting_trust_pin_iteration_is_rejected(
    tmp_path: Path,
    conflicting: bool,
) -> None:
    first = "a" * 64
    pins = _DuplicatePins((first, "b" * 64 if conflicting else first))
    root = tmp_path / f"duplicate-pins-{conflicting}"
    with pytest.raises(ProviderLinkageStoreUnsafe, match="pins are invalid"):
        ProviderLinkageStore(
            root,
            expected_trust_snapshot_sha256_by_provider=pins,
            time_source=AuthorityTimeSource.fixed(NOW),
        )
    assert pins.get_calls == 1
    assert not root.exists()


@pytest.mark.parametrize("subclass_field", ("provider", "digest"))
def test_trust_pin_capture_requires_exact_string_types_without_root(
    tmp_path: Path,
    subclass_field: str,
) -> None:
    class StringSubclass(str):
        pass

    provider = StringSubclass(PROVIDER) if subclass_field == "provider" else PROVIDER
    digest: str = "a" * 64
    if subclass_field == "digest":
        digest = StringSubclass(digest)
    root = tmp_path / f"subclass-{subclass_field}"
    with pytest.raises(ProviderLinkageStoreUnsafe, match="pins are invalid"):
        ProviderLinkageStore(
            root,
            expected_trust_snapshot_sha256_by_provider={provider: digest},
            time_source=AuthorityTimeSource.fixed(NOW),
        )
    assert not root.exists()


@pytest.mark.parametrize("count", (1, linkage_store_module.MAX_PROVIDER_TRUST_PINS))
def test_exact_dict_trust_pin_boundaries_are_accepted(
    tmp_path: Path,
    count: int,
) -> None:
    pins = {f"provider_{index:032x}": f"{index + 1:064x}" for index in range(count)}
    with ProviderLinkageStore(
        tmp_path / f"exact-pins-{count}",
        expected_trust_snapshot_sha256_by_provider=pins,
        time_source=AuthorityTimeSource.fixed(NOW),
    ) as store:
        assert len(store._trust_pins) == count  # type: ignore[attr-defined]


@pytest.mark.parametrize("count", (0, linkage_store_module.MAX_PROVIDER_TRUST_PINS + 1))
def test_exact_dict_trust_pin_out_of_bounds_has_no_root(
    tmp_path: Path,
    count: int,
) -> None:
    pins = {f"provider_{index:032x}": f"{index + 1:064x}" for index in range(count)}
    root = tmp_path / f"invalid-exact-pins-{count}"
    with pytest.raises(ProviderLinkageStoreUnsafe, match="trust pin"):
        ProviderLinkageStore(
            root,
            expected_trust_snapshot_sha256_by_provider=pins,
            time_source=AuthorityTimeSource.fixed(NOW),
        )
    assert not root.exists()


def test_time_source_subclass_is_rejected_without_object_execution(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class AttackingTimeSource(AuthorityTimeSource):
        def __bool__(self) -> bool:
            calls.append("bool")
            raise AssertionError("subclass truthiness executed")

        def __getattribute__(self, name: str) -> object:
            calls.append(f"getattribute:{name}")
            raise AssertionError("subclass attribute access executed")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("subclass repr executed")

    attacking_source = AttackingTimeSource.fixed(NOW)
    with pytest.raises(ProviderLinkageStoreUnsafe, match="time source type"):
        ProviderLinkageStore(
            tmp_path / "attacking-time-source",
            expected_trust_snapshot_sha256_by_provider=_pins(),
            time_source=attacking_source,
        )
    assert calls == []
    assert not (tmp_path / "attacking-time-source").exists()


def test_none_and_exact_time_source_instances_are_accepted(tmp_path: Path) -> None:
    with ProviderLinkageStore(
        tmp_path / "system-source",
        expected_trust_snapshot_sha256_by_provider=_pins(),
    ) as system_store:
        assert system_store.active_snapshot().state_version == 0

    exact_source = AuthorityTimeSource.fixed(NOW)
    with ProviderLinkageStore(
        tmp_path / "fixed-source",
        expected_trust_snapshot_sha256_by_provider=_pins(),
        time_source=exact_source,
    ) as fixed_store:
        assert fixed_store.active_snapshot().state_version == 0


def test_legacy_arbitrary_clock_callback_is_not_an_authority_input(
    tmp_path: Path,
) -> None:
    calls = 0

    def attacking_clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    with pytest.raises(TypeError, match="unexpected keyword argument 'clock'"):
        ProviderLinkageStore(  # type: ignore[call-arg]
            tmp_path / "callback-clock",
            expected_trust_snapshot_sha256_by_provider=_pins(),
            clock=attacking_clock,
        )
    assert calls == 0


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
