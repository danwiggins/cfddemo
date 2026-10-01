"""Protected D03 decision-registry persistence, replay, and projection tests."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import evidence_inspector.longitudinal_decision_registry as registry_module
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalOutcome,
    decide_longitudinal_series,
    longitudinal_anchor_policy_sha256,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    LongitudinalDecisionRegistryConflict,
    LongitudinalDecisionRegistryStale,
    LongitudinalDecisionRegistryUnsafe,
    RegisteredSeriesObject,
    SeriesAuthorityState,
    longitudinal_decision_backup_from_bytes,
    registered_series_object_bytes,
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
    _policy,
    _record,
)

PINS = {PROVIDER: TRUST_SHA256}


def _store(path: Path) -> ProviderLinkageStore:
    return ProviderLinkageStore(
        path,
        expected_trust_snapshot_sha256_by_provider=PINS,
        time_source=AuthorityTimeSource.fixed(NOW),
    )


@pytest.fixture
def live(tmp_path: Path):
    store = _store(tmp_path / "linkage")
    try:
        records = (_record("1"), _record("2"), _record("3"))
        for record in records:
            assert record.authorized_linkage is not None
            store.commit_authorized_revision(record.authorized_linkage)
        receipts = {item.linkage_id: item for item in store.active_snapshot().receipts}
        active = tuple(
            record.model_copy(
                update={
                    "activation_receipt": receipts[record.linkage_revision.linkage_id]
                }
            )
            for record in records
        )
        yield store, active
    finally:
        store.close()


@pytest.fixture
def registry(tmp_path: Path, live):
    value = LongitudinalDecisionRegistry(
        tmp_path / "d03",
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=PINS,
    )
    try:
        yield value
    finally:
        value.close()


def _register(registry: LongitudinalDecisionRegistry, live, members=None):
    _, active = live
    policy = _policy(active[0])
    return registry.register_series(
        active[0],
        active[1:] if members is None else members,
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
    )


def _reopen(registry: LongitudinalDecisionRegistry, live, receipt, **overrides):
    values = {
        "linkage_store": live[0],
        "expected_trust_snapshot_sha256_by_provider": PINS,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    values.update(overrides)
    return LongitudinalDecisionRegistry(registry.root, **values)


def _advance_linkage(live) -> None:
    extra = _record("4")
    assert extra.authorized_linkage is not None
    live[0].commit_authorized_revision(extra.authorized_linkage)


def test_registry_derives_decision_and_replays_it_on_resolve(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    store, active = live
    policy = _policy(active[0])
    expected = decide_longitudinal_series(
        active[0],
        active[1:],
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
        expected_linkage_trust_snapshot_sha256_by_provider=PINS,
        linkage_store=store,
    )
    receipt = _register(registry, live)
    resolved = registry.resolve(receipt.selector_id)

    assert receipt.state_version == 1
    assert resolved.decision == expected
    assert resolved.decision_sha256 == receipt.decision_sha256
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert resolved.replayed_against_live_linkage is True
    assert resolved.clinical_use_authorized is False
    assert all(
        item.outcome is LongitudinalOutcome.EQUIVALENT
        for item in resolved.decision.decisions
    )


def test_exact_same_inputs_are_idempotent(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    first = _register(registry, live)
    second = _register(registry, live)

    assert second == first
    assert registry.list_selectors().state_version == 1


def test_inputs_that_do_not_form_a_valid_series_are_rejected(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    _, active = live
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="valid series"):
        _register(registry, live, members=list(active[1:]))
    policy = _policy(active[0])
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="pins"):
        registry.register_series(
            active[0],
            active[1:],
            policy,
            expected_policy_sha256="not-a-digest",
            expected_authority_head_sha256=HEAD_SHA256,
        )
    assert registry.list_selectors().state_version == 0


def test_stored_object_cannot_pair_a_decision_with_other_inputs(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    _, active = live
    receipt = _register(registry, live)
    stored = registry.resolve(receipt.selector_id)
    policy = _policy(active[0])
    values = {
        "anchor": active[0],
        "members": active[1:],
        "policy": policy,
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        "decision": stored.decision,
    }
    assert registered_series_object_bytes(RegisteredSeriesObject(**values))

    for field, value, match in (
        ("anchor", active[1], "anchor"),
        ("members", active[1:2], "members"),
        (
            "policy",
            policy.model_copy(update={"version": "1.0.1"}),
            "policy",
        ),
    ):
        with pytest.raises(ValueError, match=match):
            RegisteredSeriesObject(**{**values, field: value})


def test_linkage_advance_makes_selector_stale_without_returning_a_decision(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    assert registry.list_selectors().records[0].authority_state is (
        SeriesAuthorityState.CURRENT
    )

    _advance_linkage(live)

    with pytest.raises(LongitudinalDecisionRegistryStale, match="live authority"):
        registry.resolve(receipt.selector_id)
    page = registry.list_selectors()
    assert page.records[0].authority_state is SeriesAuthorityState.STALE
    assert page.records[0].selector_id == receipt.selector_id


def test_resolve_holds_linkage_fence_through_exact_return(
    registry: LongitudinalDecisionRegistry,
    live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(registry, live)
    extra = _record("4")
    assert extra.authorized_linkage is not None
    original_model = registry_module.RegisteredLongitudinalSeriesDecision
    writer_started = threading.Event()
    writer_finished = threading.Event()
    workers: list[threading.Thread] = []

    def construct(**values):
        def write() -> None:
            writer_started.set()
            live[0].commit_authorized_revision(extra.authorized_linkage)
            writer_finished.set()

        worker = threading.Thread(target=write)
        workers.append(worker)
        worker.start()
        assert writer_started.wait(timeout=1)
        assert not writer_finished.wait(timeout=0.05)
        return original_model(**values)

    monkeypatch.setattr(
        registry_module, "RegisteredLongitudinalSeriesDecision", construct
    )
    resolved = registry.resolve(receipt.selector_id)
    assert resolved.decision_sha256 == receipt.decision_sha256
    workers[0].join(timeout=2)
    assert writer_finished.is_set()
    monkeypatch.setattr(
        registry_module, "RegisteredLongitudinalSeriesDecision", original_model
    )
    with pytest.raises(LongitudinalDecisionRegistryStale):
        registry.resolve(receipt.selector_id)


def test_selector_page_is_private_counted_and_paginated(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    _, active = live
    first = _register(registry, live, members=active[1:2])
    second = _register(registry, live, members=active[1:])

    page = registry.list_selectors(limit=1)
    rest = registry.list_selectors(after_selector_id=page.next_after_selector_id)
    rows = (*page.records, *rest.records)

    assert {row.selector_id for row in rows} == {first.selector_id, second.selector_id}
    assert rest.next_after_selector_id is None
    assert [row.selector_id for row in rows] == sorted(row.selector_id for row in rows)
    for row in rows:
        counts = {item.outcome: item.count for item in row.outcome_counts}
        assert counts[LongitudinalOutcome.EQUIVALENT] == row.member_count
        assert row.delta_allowed_count == row.member_count
    content = canonical_contract_bytes(page) + canonical_contract_bytes(rest)
    private_values = {
        PROVIDER,
        *(record.measurement.result_id for record in active),
        *(record.linkage_revision.linkage_id for record in active),
    }
    assert all(value.encode("ascii") not in content for value in private_values)


def test_selector_and_page_bounds_are_sanitized(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    _register(registry, live)
    for selector in ("", "d03_series_" + "g" * 40, "d03_series_" + "a" * 39, 7):
        with pytest.raises(LongitudinalDecisionRegistryConflict, match="selector"):
            registry.resolve(selector)  # type: ignore[arg-type]
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="unavailable"):
        registry.resolve("d03_series_" + "a" * 40)
    for limit in (0, 101, True):
        with pytest.raises(LongitudinalDecisionRegistryConflict, match="page bound"):
            registry.list_selectors(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="cursor"):
        registry.list_selectors(after_selector_id="cohort_selector_x")


def test_cumulative_object_byte_bound_rejects_before_publication(
    registry: LongitudinalDecisionRegistry,
    live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, active = live
    first = _register(registry, live, members=active[1:2])
    stored = (registry.root / "objects" / f"{first.object_sha256}.json").stat()
    monkeypatch.setattr(registry_module, "MAX_TOTAL_OBJECT_BYTES", stored.st_size + 1)
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="byte bound"):
        _register(registry, live)
    monkeypatch.undo()
    assert registry.list_selectors().state_version == 1
    assert len(os.listdir(registry.root / "objects")) == 1


def test_object_tamper_and_extra_entries_fail_closed(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    object_path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = object_path.read_bytes()
    object_path.write_bytes(b"{}")
    object_path.chmod(0o600)
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="digest"):
        registry.list_selectors()

    object_path.write_bytes(original)
    object_path.chmod(0o600)
    (registry.root / "objects" / "notes.txt").write_text("private")
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="invalid object"):
        registry.list_selectors()


def test_committed_object_deletion_and_journal_rollback_fail_closed(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    root = registry.root
    object_path = root / "objects" / f"{receipt.object_sha256}.json"
    content = object_path.read_bytes()
    object_path.unlink()
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()
    registry.close()

    object_path.write_bytes(content)
    object_path.chmod(0o600)
    (root / "registry-journal.jsonl").write_bytes(b"")
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="rollback"):
        _reopen(registry, live, receipt)


def _object_bytes_from_scratch(live, tmp_path: Path, members) -> tuple[str, bytes]:
    scratch = LongitudinalDecisionRegistry(
        tmp_path / f"scratch-{len(members)}",
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=PINS,
    )
    try:
        receipt = _register(scratch, live, members=members)
        content = (
            scratch.root / "objects" / f"{receipt.object_sha256}.json"
        ).read_bytes()
    finally:
        scratch.close()
    return receipt.object_sha256, content


def _plant(registry: LongitudinalDecisionRegistry, digest: str, content: bytes) -> Path:
    path = registry.root / "objects" / f"{digest}.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_interrupted_publication_orphan_is_removed_on_next_registration(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    _, active = live
    orphan_path = _plant(
        registry, *_object_bytes_from_scratch(live, tmp_path, active[1:2])
    )
    assert registry.list_selectors().state_version == 0

    receipt = _register(registry, live)

    assert receipt.state_version == 1
    assert not orphan_path.exists()
    assert os.listdir(registry.root / "objects") == [f"{receipt.object_sha256}.json"]


def test_exact_uncommitted_object_is_adopted_and_two_orphans_fail_closed(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    _, active = live
    digest, content = _object_bytes_from_scratch(live, tmp_path, active[1:])
    _plant(registry, digest, content)
    receipt = _register(registry, live)
    assert receipt.object_sha256 == digest
    assert receipt.state_version == 1

    _plant(registry, *_object_bytes_from_scratch(live, tmp_path, active[1:2]))
    _plant(registry, "f" * 64, b"{}")
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()


def test_exact_crash_temporary_is_recovered_on_reopen(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    registry.close()
    temporary = registry.root / "objects" / (".tmp-" + "a" * 32)
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    reopened = _reopen(registry, live, receipt)
    try:
        assert not temporary.exists()
        assert reopened.resolve(receipt.selector_id).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()


def test_reopen_requires_exact_retained_identity_and_head(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    registry.close()
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="required"):
        LongitudinalDecisionRegistry(
            registry.root,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=PINS,
        )
    for override in (
        {"expected_state_head_sha256": "0" * 64},
        {"expected_registry_epoch_sha256": "0" * 64},
        {"expected_registry_id": "d03_registry_" + "0" * 32},
        {"expected_registry_id": "cohort_registry_" + "0" * 32},
    ):
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="expected"):
            _reopen(registry, live, receipt, **override)


def test_missing_metadata_never_bootstraps_existing_storage(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    receipt = _register(registry, live)
    registry.close()
    (registry.root / "registry-metadata.json").unlink()
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="metadata is missing"):
        _reopen(registry, live, receipt)


def test_registry_cannot_be_rebound_to_another_linkage_store(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    registry.close()
    other = _store(tmp_path / "other-linkage")
    try:
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="linkage authority"):
            _reopen(registry, live, receipt, linkage_store=other)
    finally:
        other.close()


def test_backup_restore_preserves_identity_and_replays(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    assert longitudinal_decision_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    restored = LongitudinalDecisionRegistry.restore(
        tmp_path / "restored",
        backup,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=PINS,
        expected_registry_id=receipt.registry_id,
        expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
        expected_state_head_sha256=receipt.state_head_sha256,
    )
    try:
        resolved = restored.resolve(receipt.selector_id)
        assert resolved.decision_sha256 == receipt.decision_sha256
        assert resolved.state_head_sha256 == receipt.state_head_sha256
    finally:
        restored.close()
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="already exists"):
        LongitudinalDecisionRegistry.restore(
            tmp_path / "restored",
            backup,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=PINS,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256=receipt.state_head_sha256,
        )


def test_old_backup_cannot_authenticate_as_current_state(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    _, active = live
    _register(registry, live, members=active[1:2])
    old_backup = registry.backup_bytes()
    current = _register(registry, live)
    target = tmp_path / "rollback-restore"
    with pytest.raises(LongitudinalDecisionRegistryConflict, match="expected head"):
        LongitudinalDecisionRegistry.restore(
            target,
            old_backup,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=PINS,
            expected_registry_id=current.registry_id,
            expected_registry_epoch_sha256=current.registry_epoch_sha256,
            expected_state_head_sha256=current.state_head_sha256,
        )
    assert not target.exists()


def test_tampered_backup_rejects_before_creating_restore_target(
    registry: LongitudinalDecisionRegistry, live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    tampered = backup.replace(b"equivalent", b"incompatible", 1)
    assert tampered != backup
    target = tmp_path / "tampered-restore"
    with pytest.raises(LongitudinalDecisionRegistryConflict):
        LongitudinalDecisionRegistry.restore(
            target,
            tampered,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=PINS,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256=receipt.state_head_sha256,
        )
    assert not target.exists()


def test_peer_rejects_rollback_to_its_own_preappend_head(
    registry: LongitudinalDecisionRegistry, live
) -> None:
    identity = registry.list_selectors()
    peer = LongitudinalDecisionRegistry(
        registry.root,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=PINS,
        expected_registry_id=identity.registry_id,
        expected_registry_epoch_sha256=identity.registry_epoch_sha256,
        expected_state_head_sha256=identity.state_head_sha256,
    )
    journal_path = registry.root / "registry-journal.jsonl"
    empty_journal = journal_path.read_bytes()
    try:
        _register(registry, live)
        journal_path.write_bytes(empty_journal)
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="rollback"):
            peer.list_selectors()
    finally:
        peer.close()


def test_instance_and_class_callable_shadows_are_rejected(
    registry: LongitudinalDecisionRegistry, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    for name in ("resolve", "register_series", "list_selectors", "backup_bytes"):
        object.__getattribute__(registry, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="callable"):
            getattr(registry, name)
        del object.__getattribute__(registry, "__dict__")[name]
    monkeypatch.setattr(
        LongitudinalDecisionRegistry, "_replay_in_fence", lambda self, value: None
    )
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="callable"):
        registry.resolve(receipt.selector_id)


def test_pinned_d03_authority_replacement_is_rejected(
    registry: LongitudinalDecisionRegistry, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    stored = registry.resolve(receipt.selector_id).decision
    monkeypatch.setattr(
        registry_module, "_PINNED_REPLAY_SERIES", lambda expected, *a, **k: expected
    )
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="authority callable"):
        registry.resolve(receipt.selector_id)
    monkeypatch.undo()
    monkeypatch.setattr(
        registry_module.d03_module,
        "replay_longitudinal_series_decision",
        lambda expected, *a, **k: stored,
    )
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="authority callable"):
        registry.resolve(receipt.selector_id)


@pytest.mark.parametrize(
    "name", ("_metadata", "_trusted_head_sha256", "_head_key", "_trust_pins")
)
def test_instance_authority_state_replacement_is_rejected(
    registry: LongitudinalDecisionRegistry, live, name: str
) -> None:
    receipt = _register(registry, live)
    instance = object.__getattribute__(registry, "__dict__")
    original = instance[name]
    replacement = {
        "_metadata": original.model_copy(
            update={"registry_epoch_sha256": "0" * 64}
        )
        if name == "_metadata"
        else None,
        "_trusted_head_sha256": "0" * 64,
        "_head_key": (0, 0, "x", "y"),
        "_trust_pins": {PROVIDER: "0" * 64},
    }[name]
    instance[name] = replacement
    try:
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="authority state"):
            registry.resolve(receipt.selector_id)
    finally:
        instance[name] = original


@pytest.mark.parametrize("name", (".registry.lock", "registry-metadata.json"))
def test_bound_control_file_substitution_fails_closed(
    registry: LongitudinalDecisionRegistry, live, name: str
) -> None:
    _register(registry, live)
    path = registry.root / name
    bound = registry.root / f"{name}.bound"
    os.replace(path, bound)
    path.write_bytes(bound.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="storage"):
            registry.list_selectors()
    finally:
        path.unlink()
        os.replace(bound, path)


def test_wrong_trust_pins_and_store_type_are_rejected(tmp_path: Path, live) -> None:
    with pytest.raises(LongitudinalDecisionRegistryUnsafe, match="trust pins"):
        LongitudinalDecisionRegistry(
            tmp_path / "pins",
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider={PROVIDER: "0" * 64},
        )
    with pytest.raises(TypeError, match="exact linkage store"):
        LongitudinalDecisionRegistry(
            tmp_path / "type",
            linkage_store=object(),  # type: ignore[arg-type]
            expected_trust_snapshot_sha256_by_provider=PINS,
        )
    assert not (tmp_path / "pins").exists()
