"""Protected forward-only result trust registry and its D07 comparison wiring."""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from pathlib import Path

import pytest

import evidence_inspector.repeatability_comparison_registry as d07_registry_module
import evidence_inspector.result_trust_registry as trust_module
from evidence_inspector.longitudinal_compatibility import (
    longitudinal_anchor_policy_sha256,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    RepeatabilityReason,
    result_trust_document_sha256,
)
from evidence_inspector.repeatability_comparison_registry import (
    ComparisonAuthorityState,
    RepeatabilityComparisonRegistry,
    RepeatabilityComparisonRegistryConflict,
    RepeatabilityComparisonRegistryStale,
    RepeatabilityComparisonRegistryUnsafe,
    registered_comparison_object_from_bytes,
)
from evidence_inspector.result_trust_registry import (
    ResultTrustEventKind,
    ResultTrustRegistry,
    ResultTrustRegistryConflict,
    ResultTrustRegistryUnsafe,
    ResultTrustSnapshot,
    project_result_trust_document,
    result_trust_backup_from_bytes,
)
from tests.test_longitudinal_compatibility import HEAD_SHA256, PROVIDER, _policy
from tests.test_repeatability_comparison import (
    RESULT_TRUST_DOCUMENT,
    RESULT_TRUST_SHA256,
    SIGNING_KEY,
    _envelope,
    _observation,
)
from tests.test_repeatability_comparison_registry import (  # noqa: F401 - fixture
    PINS,
    Live,
    _advance_linkage,
    _pins,
    _register,
    live,
)
from traceback_runner.signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    PublicTrustedKey,
    TrustNamespace,
    development_trust_document_bytes,
    generate_development_keypair,
)
from tests import registry_storage_checks as storage_checks

RESULT_KEY = RESULT_TRUST_DOCUMENT.keys[0]


def _public(key: DevelopmentSigningKey) -> PublicTrustedKey:
    return PublicTrustedKey(
        key_id=key.key_id,
        purpose=key.purpose,
        public_key_base64=base64.b64encode(key.public_key_bytes()).decode("ascii"),
    )


def _identity(value) -> dict[str, str]:
    return {
        "expected_registry_id": value.registry_id,
        "expected_registry_epoch_sha256": value.registry_epoch_sha256,
        "expected_state_head_sha256": value.state_head_sha256,
    }


@pytest.fixture
def trust(tmp_path: Path):
    registry = ResultTrustRegistry(tmp_path / "trust")
    try:
        yield registry
    finally:
        registry.close()


# --- event model ----------------------------------------------------------------


def test_add_is_idempotent_and_revocation_is_permanent(
    trust: ResultTrustRegistry,
) -> None:
    other = _public(generate_development_keypair(KeyPurpose.RESULT))
    added = trust.add_key(RESULT_KEY)
    assert added.applied and added.state_version == 1
    assert added.event is ResultTrustEventKind.ADD_KEY
    again = trust.add_key(RESULT_KEY)
    assert not again.applied and again.state_head_sha256 == added.state_head_sha256
    trust.add_key(other)

    revoked = trust.revoke_key(RESULT_KEY.key_id)
    assert revoked.applied and revoked.state_version == 3
    assert not trust.revoke_key(RESULT_KEY.key_id).applied
    with pytest.raises(ResultTrustRegistryConflict, match="cannot be re-added"):
        trust.add_key(RESULT_KEY)
    # There is no un-revoke operation, and the revoked flag cannot be added.
    assert not hasattr(trust, "unrevoke_key")
    with pytest.raises(ResultTrustRegistryConflict, match="revoked"):
        trust.add_key(RESULT_KEY.model_copy(update={"revoked": True}))

    snapshot = trust.current_trust()
    assert snapshot.state_version == 3
    assert [(key.key_id, key.revoked) for key in snapshot.document.keys] == sorted(
        [(RESULT_KEY.key_id, True), (other.key_id, False)]
    )
    assert snapshot.document_sha256 == hashlib.sha256(
        development_trust_document_bytes(snapshot.document)
    ).hexdigest()
    _, store = trust.current_trust_store()
    assert store.resolve(RESULT_KEY.key_id).revoked
    assert not store.resolve(other.key_id).revoked


def test_revoking_an_unknown_key_is_a_permanent_tombstone(
    trust: ResultTrustRegistry,
) -> None:
    receipt = trust.revoke_key(RESULT_KEY.key_id)
    assert receipt.applied
    assert trust.current_trust().document.keys == ()
    with pytest.raises(ResultTrustRegistryConflict, match="cannot be re-added"):
        trust.add_key(RESULT_KEY)
    for invalid in ("dev-release-" + "0" * 24, "external-result-" + "0" * 24, "x"):
        with pytest.raises(ResultTrustRegistryConflict, match="identifier"):
            trust.revoke_key(invalid)


def test_keys_stay_bound_to_namespace_and_purpose(trust: ResultTrustRegistry) -> None:
    release = _public(generate_development_keypair(KeyPurpose.RELEASE))
    with pytest.raises(ResultTrustRegistryConflict, match="purpose"):
        trust.add_key(release)
    wrong_id = RESULT_KEY.model_copy(update={"key_id": "dev-result-" + "0" * 24})
    with pytest.raises(ResultTrustRegistryConflict, match="does not match"):
        trust.add_key(wrong_id)
    relabelled = RESULT_KEY.model_copy(update={"purpose": KeyPurpose.RELEASE})
    with pytest.raises(ResultTrustRegistryConflict):
        trust.add_key(relabelled)
    external = PublicTrustedKey.model_construct(
        key_id=RESULT_KEY.key_id,
        namespace=TrustNamespace.EXTERNAL_RELEASE,
        purpose=KeyPurpose.RESULT,
        public_key_base64=RESULT_KEY.public_key_base64,
        revoked=False,
    )
    with pytest.raises(ResultTrustRegistryConflict):
        trust.add_key(external)
    with pytest.raises(ResultTrustRegistryConflict, match="PublicTrustedKey"):
        trust.add_key(RESULT_KEY.model_dump())
    assert trust.current_trust().state_version == 0


def test_key_bound_is_enforced(trust: ResultTrustRegistry) -> None:
    for _ in range(trust_module.MAX_TRUST_KEYS):
        trust.add_key(_public(generate_development_keypair(KeyPurpose.RESULT)))
    with pytest.raises(ResultTrustRegistryConflict, match="full"):
        trust.add_key(RESULT_KEY)
    # Revocation still works when the key bound is reached.
    assert trust.revoke_key(RESULT_KEY.key_id).applied


def test_tombstones_cannot_exhaust_capacity_to_revoke_active_keys(
    trust: ResultTrustRegistry,
) -> None:
    for index in range(trust_module.MAX_TRUST_TOMBSTONES):
        trust.revoke_key(f"dev-result-{index:024x}")
    with pytest.raises(ResultTrustRegistryConflict, match="tombstone bound"):
        trust.revoke_key(f"dev-result-{10**6:024x}")
    keys = [
        _public(generate_development_keypair(KeyPurpose.RESULT))
        for _ in range(trust_module.MAX_TRUST_KEYS)
    ]
    for key in keys:
        trust.add_key(key)
    # Every active key can still be revoked once all bounds are reached.
    for key in keys:
        assert trust.revoke_key(key.key_id).applied
    assert trust.current_trust().state_version == trust_module.MAX_TRUST_EVENTS


def test_snapshot_rejects_a_mismatched_digest(trust: ResultTrustRegistry) -> None:
    trust.add_key(RESULT_KEY)
    snapshot = trust.current_trust()
    values = {name: getattr(snapshot, name) for name in type(snapshot).model_fields}
    with pytest.raises(ValueError, match="digest"):
        ResultTrustSnapshot(**{**values, "document_sha256": "0" * 64})
    projected = project_result_trust_document(snapshot.document, ("dev-result-x",))
    assert projected.keys == ()


# --- forward-only ---------------------------------------------------------------


def test_reopen_requires_the_retained_identity_and_current_head(
    trust: ResultTrustRegistry, tmp_path: Path
) -> None:
    first = trust.add_key(RESULT_KEY)
    current = trust.revoke_key(RESULT_KEY.key_id)
    root = trust.root
    trust.close()
    with pytest.raises(ResultTrustRegistryUnsafe, match="expected identity"):
        ResultTrustRegistry(root)
    with pytest.raises(ResultTrustRegistryUnsafe, match="expected identity"):
        ResultTrustRegistry(root, **_identity(first))
    reopened = ResultTrustRegistry(root, **_identity(current))
    try:
        assert reopened.current_trust().document.keys[0].revoked
    finally:
        reopened.close()
    with pytest.raises(ResultTrustRegistryUnsafe, match="cannot inherit"):
        ResultTrustRegistry(tmp_path / "fresh", **_identity(current))


def test_older_journal_is_rejected_as_rollback(trust: ResultTrustRegistry) -> None:
    trust.add_key(RESULT_KEY)
    journal = trust.root / "registry-journal.jsonl"
    before_revocation = journal.read_bytes()
    current = trust.revoke_key(RESULT_KEY.key_id)
    journal.write_bytes(before_revocation)
    with pytest.raises(ResultTrustRegistryUnsafe, match="rollback"):
        trust.current_trust()
    trust.close()
    # Even with the older head as the "retained" head, this process has seen
    # the revocation and refuses the older journal.
    with pytest.raises(ResultTrustRegistryUnsafe, match="rollback"):
        ResultTrustRegistry(
            trust.root,
            expected_registry_id=current.registry_id,
            expected_registry_epoch_sha256=current.registry_epoch_sha256,
            expected_state_head_sha256=_head_of(before_revocation),
        )


def _head_of(journal: bytes) -> str:
    last = journal.splitlines()[-1]
    return trust_module.ResultTrustJournalEntry.model_validate_json(last).entry_sha256


def test_restoring_an_older_backup_is_rejected_and_cleaned_up(
    trust: ResultTrustRegistry, tmp_path: Path
) -> None:
    added = trust.add_key(RESULT_KEY)
    old_backup = trust.backup_bytes()
    trust.revoke_key(RESULT_KEY.key_id)
    target = tmp_path / "old-restore"
    with pytest.raises(ResultTrustRegistryUnsafe, match="rollback"):
        ResultTrustRegistry.restore(target, old_backup, **_identity(added))
    assert not target.exists()


def test_same_identity_copy_behind_the_process_head_fails_closed(
    trust: ResultTrustRegistry, tmp_path: Path
) -> None:
    current = trust.add_key(RESULT_KEY)
    copy = ResultTrustRegistry.restore(
        tmp_path / "same-identity-copy", trust.backup_bytes(), **_identity(current)
    )
    try:
        assert copy.current_trust().state_head_sha256 == current.state_head_sha256
        trust.revoke_key(RESULT_KEY.key_id)
        # The copy is internally consistent but still shows the key as active;
        # the identity-keyed head fence refuses it rather than serve it.
        with pytest.raises(ResultTrustRegistryUnsafe, match="rollback"):
            copy.current_trust()
        with pytest.raises(ResultTrustRegistryUnsafe, match="rollback"):
            with copy.read_fence():
                pass
    finally:
        copy.close()


def test_journal_that_re_adds_a_revoked_key_fails_closed(
    trust: ResultTrustRegistry,
) -> None:
    trust.add_key(RESULT_KEY)
    revoked = trust.revoke_key(RESULT_KEY.key_id)
    forged = trust_module._build_journal_entry(
        sequence=3,
        previous_entry_sha256=revoked.state_head_sha256,
        event=ResultTrustEventKind.ADD_KEY,
        key_id=RESULT_KEY.key_id,
        public_key_base64=RESULT_KEY.public_key_base64,
    )
    journal = trust.root / "registry-journal.jsonl"
    with journal.open("ab") as handle:
        handle.write(canonical_contract_bytes(forged) + b"\n")
    with pytest.raises(ResultTrustRegistryUnsafe, match="journal is invalid"):
        trust.current_trust()


def test_torn_append_is_truncated_and_the_event_retries(
    trust: ResultTrustRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust.add_key(RESULT_KEY)
    journal = trust.root / "registry-journal.jsonl"
    committed = journal.read_bytes()
    original_write = os.write

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if b"result-trust-journal-entry" in data:
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(ResultTrustRegistryUnsafe, match="append failed"):
        trust.revoke_key(RESULT_KEY.key_id)
    monkeypatch.undo()
    assert journal.read_bytes() == committed

    def interrupted_write(descriptor: int, content) -> int:
        data = bytes(content)
        if b"result-trust-journal-entry" in data:
            original_write(descriptor, data[: len(data) // 2])
            raise KeyboardInterrupt
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", interrupted_write)
    with pytest.raises(KeyboardInterrupt):
        trust.revoke_key(RESULT_KEY.key_id)
    monkeypatch.undo()
    assert journal.read_bytes() == committed
    assert trust.revoke_key(RESULT_KEY.key_id).state_version == 2


def test_backup_restore_round_trip_and_failed_restore_cleanup(
    trust: ResultTrustRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = trust.add_key(RESULT_KEY)
    backup = trust.backup_bytes()
    assert result_trust_backup_from_bytes(backup).state_head_sha256 == (
        current.state_head_sha256
    )
    tampered = backup.replace(b"add_key", b"revoke_key", 1)
    with pytest.raises(ResultTrustRegistryConflict):
        ResultTrustRegistry.restore(tmp_path / "t", tampered, **_identity(current))
    assert not (tmp_path / "t").exists()

    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = tmp_path / "restored"
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(ResultTrustRegistryUnsafe, match="restore failed"):
        ResultTrustRegistry.restore(target, backup, **_identity(current))
    monkeypatch.undo()
    assert not target.exists()

    def failing_construct(*args, **kwargs):
        raise ResultTrustRegistryUnsafe("reopen failed")

    monkeypatch.setattr(trust_module, "_RT_CONSTRUCT", failing_construct)
    with pytest.raises(ResultTrustRegistryUnsafe):
        ResultTrustRegistry.restore(target, backup, **_identity(current))
    monkeypatch.undo()
    assert not target.exists()

    restored = ResultTrustRegistry.restore(target, backup, **_identity(current))
    try:
        snapshot = restored.current_trust()
        assert snapshot.state_head_sha256 == current.state_head_sha256
        assert snapshot.registry_id == current.registry_id
    finally:
        restored.close()
    with pytest.raises(ResultTrustRegistryConflict, match="already exists"):
        ResultTrustRegistry.restore(target, backup, **_identity(current))


# --- fence and seals ------------------------------------------------------------


def test_read_fence_blocks_trust_events_until_exit(trust: ResultTrustRegistry) -> None:
    trust.add_key(RESULT_KEY)
    finished = threading.Event()

    def revoke() -> None:
        trust.revoke_key(RESULT_KEY.key_id)
        finished.set()

    with trust.read_fence() as snapshot:
        worker = threading.Thread(target=revoke)
        worker.start()
        assert not finished.wait(timeout=0.1)
        assert not snapshot.document.keys[0].revoked
        # Nested acquisition on the holding thread is refused, not upgraded.
        with pytest.raises(ResultTrustRegistryUnsafe, match="not reentrant"):
            trust.current_trust()
    worker.join(timeout=2)
    assert finished.is_set()
    assert trust.current_trust().document.keys[0].revoked


def test_concurrent_reads_and_trust_events_on_one_instance_never_fail_spuriously(
    trust: ResultTrustRegistry,
) -> None:
    # A trust event updates the instance's trusted head and then its seal;
    # a reader's integrity check on another thread must not see one without
    # the other.
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                trust.current_trust()
            except BaseException as exc:  # noqa: BLE001 - exact thread outcome
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for _ in range(24):
            trust.add_key(_public(generate_development_keypair(KeyPurpose.RESULT)))
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in readers)
    assert not errors
    assert trust.current_trust().state_version == 24


def test_closed_registry_lock_fails_closed(trust: ResultTrustRegistry) -> None:
    trust.close()
    with pytest.raises(ResultTrustRegistryUnsafe, match="closed"):
        trust.current_trust()


def test_callable_and_authority_replacement_is_rejected(
    trust: ResultTrustRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust.add_key(RESULT_KEY)
    for name in ("add_key", "revoke_key", "read_fence", "current_trust"):
        object.__getattribute__(trust, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(ResultTrustRegistryUnsafe, match="callable"):
            getattr(trust, name)
        del object.__getattribute__(trust, "__dict__")[name]
    monkeypatch.setattr(ResultTrustRegistry, "_append_event", lambda *a: None)
    with pytest.raises(ResultTrustRegistryUnsafe, match="callable"):
        trust.revoke_key(RESULT_KEY.key_id)
    monkeypatch.undo()
    for name in ("_PINNED_TRUSTED_KEY_ID", "_RT_SNAPSHOT", "_RT_LOAD_STATE"):
        monkeypatch.setattr(trust_module, name, lambda *a, **k: None)
        with pytest.raises(ResultTrustRegistryUnsafe, match="authority callable"):
            trust.current_trust()
        monkeypatch.undo()
    monkeypatch.setattr(
        trust_module.signing_module, "load_development_trust", lambda *a: None
    )
    with pytest.raises(ResultTrustRegistryUnsafe, match="authority callable"):
        trust.current_trust_store()
    monkeypatch.undo()
    monkeypatch.setitem(trust_module.__dict__, "__warningregistry__", {})
    assert trust.current_trust().state_version == 1


@pytest.mark.parametrize("name", ("_metadata", "_trusted_head_sha256", "_head_key"))
def test_instance_authority_state_replacement_is_rejected(
    trust: ResultTrustRegistry, name: str
) -> None:
    trust.add_key(RESULT_KEY)
    instance = object.__getattribute__(trust, "__dict__")
    original = instance[name]
    instance[name] = {
        "_metadata": original.model_copy(update={"registry_epoch_sha256": "0" * 64})
        if name == "_metadata"
        else None,
        "_trusted_head_sha256": "0" * 64,
        "_head_key": ("x", "y"),
    }[name]
    try:
        with pytest.raises(ResultTrustRegistryUnsafe, match="authority state"):
            trust.current_trust()
    finally:
        instance[name] = original


def test_bound_control_file_substitution_fails_closed(
    trust: ResultTrustRegistry,
) -> None:
    trust.add_key(RESULT_KEY)
    for name in (".registry.lock", "registry-metadata.json", "registry-journal.jsonl"):
        path = trust.root / name
        bound = trust.root / f"{name}.bound"
        os.replace(path, bound)
        path.write_bytes(bound.read_bytes())
        path.chmod(0o600)
        try:
            with pytest.raises(ResultTrustRegistryUnsafe, match="storage"):
                trust.current_trust()
        finally:
            path.unlink()
            os.replace(bound, path)
    assert trust.current_trust().state_version == 1


# --- D07 comparison registry on the trust registry ------------------------------


def _open_d07(root: Path, state: Live, trust: ResultTrustRegistry, **overrides):
    values = {
        "linkage_store": state.store,
        "expected_trust_snapshot_sha256_by_provider": PINS,
        "result_trust_registry": trust,
    }
    values.update(overrides)
    return RepeatabilityComparisonRegistry(root, **values)


@pytest.fixture
def d07(tmp_path: Path, live: Live, trust: ResultTrustRegistry):  # noqa: F811
    trust.add_key(RESULT_KEY)
    registry = _open_d07(tmp_path / "d07", live, trust)
    try:
        yield registry
    finally:
        registry.close()


def _register_signed_by(registry, state: Live, signing_key, *, member_index: int = 1):
    anchor, member = state.active[0], state.active[member_index]
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    return registry.register_comparison(
        anchor,
        member,
        policy,
        _observation(anchor, 0.5, signing_key=signing_key),
        _observation(member, 0.55, signing_key=signing_key),
        envelope,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
        **_pins(envelope),
    )


def test_d07_revocation_is_stale_on_the_next_read_without_reopen(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
) -> None:
    receipt = _register(d07, live)
    assert receipt.availability is ComparisonAvailability.AVAILABLE
    current = trust.current_trust()
    assert receipt.result_trust_registry_id == current.registry_id
    assert receipt.result_trust_state_head_sha256 == current.state_head_sha256
    resolved = d07.resolve(receipt.selector_id)
    assert resolved.comparison_sha256 == receipt.comparison_sha256
    # The comparison binds the trust restricted to the keys it was signed by.
    assert resolved.comparison.result_trust_sha256 == RESULT_TRUST_SHA256

    trust.revoke_key(RESULT_KEY.key_id)
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        d07.resolve(receipt.selector_id)
    page = d07.list_selectors()
    assert page.records[0].authority_state is ComparisonAuthorityState.STALE
    assert page.result_trust_state_head_sha256 == (
        trust.current_trust().state_head_sha256
    )
    # A revoked key can never come back, so the comparison stays stale.
    with pytest.raises(ResultTrustRegistryConflict):
        trust.add_key(RESULT_KEY)
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        d07.resolve(receipt.selector_id)


def test_d07_unrelated_key_events_leave_comparisons_current(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
) -> None:
    receipt = _register(d07, live)
    other = generate_development_keypair(KeyPurpose.RESULT)
    trust.add_key(_public(other))
    trust.revoke_key(other.key_id)
    resolved = d07.resolve(receipt.selector_id)
    assert resolved.comparison_sha256 == receipt.comparison_sha256
    assert resolved.result_trust_state_head_sha256 != (
        receipt.result_trust_state_head_sha256
    )


def test_d07_unknown_key_registers_unavailable_and_a_later_add_makes_it_stale(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
) -> None:
    unknown = generate_development_keypair(KeyPurpose.RESULT)
    receipt = _register_signed_by(d07, live, unknown)
    assert receipt.availability is ComparisonAvailability.UNAVAILABLE
    resolved = d07.resolve(receipt.selector_id)
    assert resolved.comparison.reason_codes == (
        RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID,
    )
    trust.add_key(_public(unknown))
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        d07.resolve(receipt.selector_id)
    current = _register_signed_by(d07, live, unknown)
    assert current.availability is ComparisonAvailability.AVAILABLE


def test_d07_registration_against_an_empty_trust_registry_is_rejected(
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    tmp_path: Path,
) -> None:
    trust.revoke_key(RESULT_KEY.key_id)
    registry = _open_d07(tmp_path / "empty", live, trust)
    try:
        with pytest.raises(RepeatabilityComparisonRegistryConflict, match="no keys"):
            _register(registry, live)
        assert registry.list_selectors().state_version == 0
    finally:
        registry.close()


def test_d07_trust_registry_binding_cannot_be_swapped(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    tmp_path: Path,
) -> None:
    receipt = _register(d07, live)
    identity = {
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    d07.close()
    # The old, self-consistent fixed document (key active) cannot be replayed.
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
        RepeatabilityComparisonRegistry(
            d07.root,
            linkage_store=live.store,
            expected_trust_snapshot_sha256_by_provider=PINS,
            result_trust_document=RESULT_TRUST_DOCUMENT,
            expected_result_trust_sha256=RESULT_TRUST_SHA256,
            **identity,
        )
    # Nor can a fresh trust registry in which the key was never revoked.
    fresh = ResultTrustRegistry(tmp_path / "fresh-trust")
    try:
        fresh.add_key(RESULT_KEY)
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
            _open_d07(d07.root, live, fresh, **identity)
    finally:
        fresh.close()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="not both"):
        _open_d07(
            tmp_path / "both",
            live,
            trust,
            result_trust_document=RESULT_TRUST_DOCUMENT,
            expected_result_trust_sha256=RESULT_TRUST_SHA256,
        )
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="registry is invalid"):
        _open_d07(tmp_path / "type", live, object())
    reopened = _open_d07(d07.root, live, trust, **identity)
    try:
        assert reopened.resolve(receipt.selector_id).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()


def test_fixed_document_registry_cannot_switch_to_a_trust_registry(
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    tmp_path: Path,
) -> None:
    trust.add_key(RESULT_KEY)
    fixed = RepeatabilityComparisonRegistry(
        tmp_path / "fixed",
        linkage_store=live.store,
        expected_trust_snapshot_sha256_by_provider=PINS,
        result_trust_document=RESULT_TRUST_DOCUMENT,
        expected_result_trust_sha256=RESULT_TRUST_SHA256,
    )
    receipt = _register(fixed, live)
    assert receipt.result_trust_registry_id is None
    fixed.close()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
        _open_d07(
            fixed.root,
            live,
            trust,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256=receipt.state_head_sha256,
        )


def test_d07_replay_requires_the_held_trust_fence(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
) -> None:
    receipt = _register(d07, live)
    content = (d07.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    stored = registered_comparison_object_from_bytes(content)
    live_time = stored.comparison.evaluated_at
    with ProviderLinkageStore.authority_read_fence(live.store):
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="trust fence"):
            d07._replay_in_fence(stored, live_time)
        with trust.read_fence() as snapshot:
            assert d07._replay_in_fence(stored, live_time, snapshot) == (
                stored.comparison
            )


def test_d07_holds_the_trust_fence_through_return_with_linkage_fence(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe lock composition: linkage fence, then trust fence, then D07 lock.

    A revocation started inside the D07 evaluation stays blocked until the
    receipt (and later the resolved value) is constructed, and neither side
    deadlocks.
    """

    original_sha256 = hashlib.sha256
    workers: list[threading.Thread] = []
    finished = threading.Event()
    blocked: list[bool] = []

    def sha256(content=b"", *args, **kwargs):
        if bytes(content).startswith(b'{"anchor_denominator_count"'):
            if workers:
                blocked.append(not finished.wait(timeout=0.02))
            else:

                def revoke() -> None:
                    trust.revoke_key(RESULT_KEY.key_id)
                    finished.set()

                worker = threading.Thread(target=revoke)
                workers.append(worker)
                worker.start()
                assert not finished.wait(timeout=0.05)
        return original_sha256(content, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", sha256)
    receipt = _register(d07, live)
    monkeypatch.undo()
    assert workers and len(blocked) >= 2 and all(blocked)
    workers[0].join(timeout=2)
    assert finished.is_set()
    assert receipt.availability is ComparisonAvailability.AVAILABLE
    # The revocation landed only after registration returned.
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        d07.resolve(receipt.selector_id)
    # The linkage fence still composes: a linkage write after the read works.
    _advance_linkage(live)


def test_d07_backup_restore_keeps_the_trust_registry_binding(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    tmp_path: Path,
) -> None:
    receipt = _register(d07, live)
    backup = d07.backup_bytes()
    assert b'"schema_version":"traceback.d07-comparison-backup.v2"' in backup
    downgraded = backup.replace(
        b'"schema_version":"traceback.d07-comparison-backup.v2"',
        b'"schema_version":"traceback.d07-comparison-backup.v1"',
    )
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="invalid"):
        d07_registry_module.repeatability_comparison_backup_from_bytes(downgraded)
    values = {
        "linkage_store": live.store,
        "expected_trust_snapshot_sha256_by_provider": PINS,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    target = tmp_path / "restored-d07"
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="result trust"):
        RepeatabilityComparisonRegistry.restore(
            target,
            backup,
            **values,
            result_trust_document=RESULT_TRUST_DOCUMENT,
            expected_result_trust_sha256=RESULT_TRUST_SHA256,
        )
    assert not target.exists()
    restored = RepeatabilityComparisonRegistry.restore(
        target, backup, **values, result_trust_registry=trust
    )
    try:
        assert restored.resolve(receipt.selector_id).object_sha256 == (
            receipt.object_sha256
        )
        trust.revoke_key(RESULT_KEY.key_id)
        with pytest.raises(RepeatabilityComparisonRegistryStale):
            restored.resolve(receipt.selector_id)
    finally:
        restored.close()


def test_d07_trust_seals_reject_replacement(
    d07: RepeatabilityComparisonRegistry,
    live: Live,  # noqa: F811
    trust: ResultTrustRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(d07, live)
    for name in ("_PINNED_TRUST_READ_FENCE", "_PINNED_PROJECT_TRUST", "_CR_RESULT_TRUST_FOR"):
        monkeypatch.setattr(d07_registry_module, name, lambda *a, **k: None)
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority callable"
        ):
            d07.resolve(receipt.selector_id)
        monkeypatch.undo()
    monkeypatch.setattr(ResultTrustRegistry, "read_fence", lambda self: None)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="authority callable"):
        d07.resolve(receipt.selector_id)
    monkeypatch.undo()
    other = ResultTrustRegistry(tmp_path / "other-trust")
    instance = object.__getattribute__(d07, "__dict__")
    original = instance["_result_trust_registry"]
    instance["_result_trust_registry"] = other
    try:
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority state"
        ):
            d07.resolve(receipt.selector_id)
    finally:
        instance["_result_trust_registry"] = original
        other.close()
    instance["_result_trust_document"] = RESULT_TRUST_DOCUMENT
    try:
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority state"
        ):
            d07.resolve(receipt.selector_id)
    finally:
        instance["_result_trust_document"] = None
    assert d07.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256


def test_projection_matches_the_whole_document_digest_for_one_key() -> None:
    assert result_trust_document_sha256(
        project_result_trust_document(RESULT_TRUST_DOCUMENT, (SIGNING_KEY.key_id,))
    ) == RESULT_TRUST_SHA256
    assert PROVIDER


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    trust: ResultTrustRegistry,
) -> None:
    registry = trust
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: trust.add_key(RESULT_KEY),
        lambda values: ResultTrustRegistry(
            trust.root, **storage_checks.expected(values)
        ),
        ResultTrustRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    trust: ResultTrustRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = trust
    storage_checks.check_append_interrupt_truncates(
        registry, trust_module, lambda: trust.add_key(RESULT_KEY), monkeypatch
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    trust: ResultTrustRegistry, tmp_path: Path
) -> None:
    registry = trust
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, ResultTrustRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    trust: ResultTrustRegistry,
) -> None:
    registry = trust
    trust.add_key(RESULT_KEY)
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: ResultTrustRegistry(
            trust.root, **storage_checks.expected(values)
        ),
        ResultTrustRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    trust: ResultTrustRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_interrupted_creation(
        lambda root: ResultTrustRegistry(root),
        lambda root, values: ResultTrustRegistry(
            root, **storage_checks.expected(values)
        ),
        tmp_path / "created-by-storage-check",
        trust_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    trust: ResultTrustRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = trust
    trust.add_key(RESULT_KEY)
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: ResultTrustRegistry.restore(
            target, backup, **storage_checks.expected(values)
        ),
        trust_module,
        tmp_path,
        monkeypatch,
    )


def test_storage_creation_under_a_symlinked_parent(
    trust: ResultTrustRegistry, tmp_path: Path
) -> None:
    storage_checks.check_creation_under_symlinked_parent(
        lambda root: ResultTrustRegistry(root),
        lambda root, values: ResultTrustRegistry(
            root, **storage_checks.expected(values)
        ),
        tmp_path,
    )
