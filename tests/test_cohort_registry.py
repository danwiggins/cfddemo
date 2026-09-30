"""Protected D05 cohort-registry persistence and projection tests."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from evidence_inspector.cohort_manifest import (
    cohort_manifest_bytes,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import (
    CohortAuthorityState,
    CohortRegistry,
    CohortRegistryConflict,
    CohortRegistryUnsafe,
    cohort_registry_backup_from_bytes,
)
from evidence_inspector.provider_linkage import LinkageOperation, LinkageReasonCode
from tests.test_cohort_manifest import _manifest, live as _cohort_live
from tests.test_provider_linkage import (
    _consume,
    _correction_approvals,
    _revision,
    _token,
)
from tests.test_provider_linkage_store import _pins, _store


@pytest.fixture
def live(tmp_path: Path):
    generator = _cohort_live.__wrapped__(tmp_path)
    value = next(generator)
    try:
        yield value
    finally:
        with pytest.raises(StopIteration):
            next(generator)


@pytest.fixture
def registry(tmp_path: Path, live):
    value = CohortRegistry(
        tmp_path / "cohorts",
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        yield value
    finally:
        value.close()


def _first(live):
    _, _, authority, member = live
    return _manifest(authority, (member,))


def _second(first, live):
    second = _manifest(
        first.provider_authorities[0],
        first.members,
        cohort_id=first.cohort_id,
        version=2,
        previous_manifest_sha256=cohort_manifest_sha256(first),
        created_at=first.created_at + timedelta(seconds=1),
        measurement_anchor=first.measurement_anchor,
        policies=first.policies.model_copy(update={"missingness_sha256": "b" * 64}),
    )
    vars(live[0])["_time_source"].advance_to(second.created_at)
    return second


def test_register_resolve_and_public_projection_are_exact_and_private(
    registry: CohortRegistry, live
) -> None:
    manifest = _first(live)
    receipt = registry.register(manifest)
    repeated = registry.register(manifest)
    assert repeated == receipt

    page = registry.list_selectors(limit=1)
    assert page.state_version == 1
    assert len(page.records) == 1
    row = page.records[0]
    assert row.authority_state == CohortAuthorityState.CURRENT
    assert row.member_count == len(manifest.members)
    assert row.denominator_count == 1
    assert row.manifest_sha256 == cohort_manifest_sha256(manifest)
    public = json.dumps(page.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        manifest.cohort_id,
        manifest.members[0].provider_namespace,
        manifest.members[0].subject_token,
        manifest.members[0].collection_token,
        manifest.members[0].specimen_token,
        manifest.members[0].analysis_record_id,
        manifest.members[0].run_token,
    ):
        assert forbidden not in public

    resolved = registry.resolve(row.selector_id, row.cohort_version)
    assert resolved.manifest == manifest
    assert resolved.manifest_sha256 == row.manifest_sha256
    assert resolved.state_head_sha256 == page.state_head_sha256


def test_versions_are_append_only_consecutive_and_paginated(
    registry: CohortRegistry, live
) -> None:
    first = _first(live)
    registry.register(first)
    second = _second(first, live)
    registry.register(second)
    page1 = registry.list_selectors(limit=1)
    assert len(page1.records) == 1
    assert page1.next_after_selector_id is not None
    page2 = registry.list_selectors(
        after_selector_id=page1.next_after_selector_id,
        after_version=page1.next_after_version,
        limit=1,
    )
    assert len(page2.records) == 1
    assert {page1.records[0].cohort_version, page2.records[0].cohort_version} == {
        1,
        2,
    }
    assert page2.next_after_selector_id is None
    selected = max((page1.records + page2.records), key=lambda item: item.cohort_version)
    history = registry.resolve_history(selected.selector_id, selected.cohort_version)
    assert history.manifests == (first, second)
    assert history.selected_manifest_sha256 == cohort_manifest_sha256(second)

    conflicting = second.model_copy(
        update={
            "policies": second.policies.model_copy(
                update={"missingness_sha256": "c" * 64}
            )
        }
    )
    with pytest.raises(CohortRegistryConflict):
        registry.register(conflicting)


def test_concurrent_same_bytes_adopt_once(registry: CohortRegistry, live) -> None:
    manifest = _first(live)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = tuple(pool.map(lambda _: registry.register(manifest), range(32)))
    assert len(set(receipt.manifest_sha256 for receipt in receipts)) == 1
    assert {receipt.state_version for receipt in receipts} == {1}
    assert registry.list_selectors().state_version == 1


def test_two_instances_in_one_process_serialize_publication(
    registry: CohortRegistry, live
) -> None:
    peer = CohortRegistry(
        registry.root,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        expected_state_head_sha256="0" * 64,
    )
    manifest = _first(live)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit((registry if index % 2 else peer).register, manifest)
                for index in range(32)
            ]
            receipts = tuple(future.result() for future in futures)
        assert {receipt.state_version for receipt in receipts} == {1}
        assert registry.list_selectors().state_version == 1
        assert peer.list_selectors().state_head_sha256 == (
            registry.list_selectors().state_head_sha256
        )
    finally:
        peer.close()


def test_linkage_correction_keeps_history_but_stales_public_selector(
    registry: CohortRegistry, live
) -> None:
    store, snapshot, _, _ = live
    manifest = _first(live)
    registry.register(manifest)
    selector = registry.list_selectors().records[0]
    previous = snapshot.revisions[0]
    proposed = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=previous,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    ).model_copy(update={"technical": previous.technical})
    correction, _ = _consume(
        proposed,
        _correction_approvals(proposed),
        previous=previous,
    )
    store.commit_authorized_revision(correction)

    stale = registry.list_selectors().records[0]
    assert stale.manifest_sha256 == selector.manifest_sha256
    assert stale.authority_state == CohortAuthorityState.STALE
    with pytest.raises(CohortRegistryConflict, match="stale"):
        registry.resolve(stale.selector_id, stale.cohort_version)


def test_object_tamper_and_extra_entries_fail_closed(
    registry: CohortRegistry, live
) -> None:
    receipt = registry.register(_first(live))
    object_path = registry.root / "objects" / f"{receipt.manifest_sha256}.json"
    object_path.write_bytes(b"{}")
    object_path.chmod(0o600)
    with pytest.raises(CohortRegistryUnsafe, match="digest"):
        registry.list_selectors()

    object_path.unlink()
    (registry.root / "objects" / "notes.txt").write_text("private")
    with pytest.raises(CohortRegistryUnsafe, match="invalid object"):
        registry.list_selectors()


@pytest.mark.parametrize("name", (".registry.lock", "registry-metadata.json"))
def test_bound_control_file_substitution_fails_closed(
    registry: CohortRegistry, live, name: str
) -> None:
    registry.register(_first(live))
    path = registry.root / name
    backup = registry.root / f"{name}.bound"
    os.replace(path, backup)
    path.write_bytes(backup.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(CohortRegistryUnsafe, match="storage"):
            registry.list_selectors()
    finally:
        path.unlink()
        os.replace(backup, path)


def test_exact_crash_temporary_is_recovered_on_reopen(
    registry: CohortRegistry, live
) -> None:
    receipt = registry.register(_first(live))
    root = registry.root
    registry.close()
    temporary = root / "objects" / (".tmp-" + "a" * 32)
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    reopened = CohortRegistry(
        root,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        expected_state_head_sha256=receipt.state_head_sha256,
    )
    try:
        assert not temporary.exists()
        assert reopened.list_selectors().state_version == 1
    finally:
        reopened.close()


def test_registry_cannot_be_rebound_to_another_linkage_store(
    registry: CohortRegistry, live, tmp_path: Path
) -> None:
    registry.register(_first(live))
    root = registry.root
    registry.close()
    other = _store(tmp_path / "other-linkage")
    try:
        with pytest.raises(CohortRegistryUnsafe, match="linkage authority"):
            CohortRegistry(
                root,
                linkage_store=other,
                expected_trust_snapshot_sha256_by_provider=_pins(),
            )
    finally:
        other.close()


def test_backup_restore_rehearsal_preserves_exact_identity_and_state(
    registry: CohortRegistry, live, tmp_path: Path
) -> None:
    first = _first(live)
    registry.register(first)
    registry.register(_second(first, live))
    before = registry.list_selectors()
    backup = registry.backup_bytes()
    assert cohort_registry_backup_from_bytes(backup).state_head_sha256 == (
        before.state_head_sha256
    )

    restored = CohortRegistry.restore(
        tmp_path / "restored-cohorts",
        backup,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        expected_state_head_sha256=before.state_head_sha256,
    )
    try:
        after = restored.list_selectors()
        assert after == before
        assert restored.backup_bytes() == backup
    finally:
        restored.close()


def test_invalid_backup_rejects_before_creating_restore_target(
    registry: CohortRegistry, live, tmp_path: Path
) -> None:
    registry.register(_first(live))
    content = registry.backup_bytes()
    expected_head = cohort_registry_backup_from_bytes(content).state_head_sha256
    target = tmp_path / "must-not-exist"
    with pytest.raises(CohortRegistryConflict, match="invalid"):
        CohortRegistry.restore(
            target,
            content + b" ",
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            expected_state_head_sha256=expected_head,
        )
    assert not target.exists()


def test_root_path_substitution_fails_without_populating_target(
    registry: CohortRegistry, live, tmp_path: Path
) -> None:
    registry.register(_first(live))
    root = registry.root
    backup = tmp_path / "cohorts.bound"
    target = tmp_path / "attacker"
    target.mkdir(mode=0o750)
    os.replace(root, backup)
    root.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(CohortRegistryUnsafe, match="storage"):
            registry.list_selectors()
        assert tuple(target.iterdir()) == ()
        assert target.stat().st_mode & 0o777 == 0o750
    finally:
        root.unlink()
        os.replace(backup, root)


def test_constructor_snapshots_exact_path_parts(live, tmp_path: Path) -> None:
    caller = tmp_path / "captured-registry"
    caller_parts = object.__getattribute__(caller, "_parts")
    registry = CohortRegistry(
        caller,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        caller_parts[:] = ["/", "private", "mutated-after-capture"]
        assert registry.root == tmp_path / "captured-registry"
        registry.register(_first(live))
    finally:
        registry.close()


def test_closed_registry_and_cursor_bounds_are_sanitized(registry, live) -> None:
    registry.register(_first(live))
    with pytest.raises(CohortRegistryConflict, match="cursor"):
        registry.list_selectors(after_selector_id="cohort_selector_" + "a" * 40)
    with pytest.raises(CohortRegistryConflict, match="bound"):
        registry.list_selectors(limit=101)
    registry.close()
    with pytest.raises(CohortRegistryUnsafe, match="closed"):
        registry.list_selectors()


def test_exact_uncommitted_object_is_adopted_after_crash(
    registry: CohortRegistry, live
) -> None:
    manifest = _first(live)
    digest = cohort_manifest_sha256(manifest)
    path = registry.root / "objects" / f"{digest}.json"
    path.write_bytes(cohort_manifest_bytes(manifest))
    path.chmod(0o600)
    receipt = registry.register(manifest)
    assert receipt.manifest_sha256 == digest
    assert receipt.state_version == 1


def test_committed_object_deletion_and_journal_rollback_fail_closed(
    registry: CohortRegistry, live
) -> None:
    receipt = registry.register(_first(live))
    root = registry.root
    object_path = root / "objects" / f"{receipt.manifest_sha256}.json"
    object_content = object_path.read_bytes()
    object_path.unlink()
    with pytest.raises(CohortRegistryUnsafe, match="committed object is missing"):
        registry.list_selectors()
    registry.close()

    object_path.write_bytes(object_content)
    object_path.chmod(0o600)
    (root / "registry-journal.jsonl").write_bytes(b"")
    with pytest.raises(CohortRegistryUnsafe, match="expected head"):
        CohortRegistry(
            root,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            expected_state_head_sha256=receipt.state_head_sha256,
        )


def test_old_valid_backup_cannot_authenticate_as_current_state(
    registry: CohortRegistry, live, tmp_path: Path
) -> None:
    first = _first(live)
    first_receipt = registry.register(first)
    old_backup = registry.backup_bytes()
    current_receipt = registry.register(_second(first, live))
    target = tmp_path / "rollback-restore"
    with pytest.raises(CohortRegistryConflict, match="expected head"):
        CohortRegistry.restore(
            target,
            old_backup,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
            expected_state_head_sha256=current_receipt.state_head_sha256,
        )
    assert first_receipt.state_head_sha256 != current_receipt.state_head_sha256
    assert not target.exists()


def test_instance_callable_shadow_is_rejected(registry: CohortRegistry, live) -> None:
    registry.register(_first(live))
    vars(registry)["_load_state"] = lambda: ({}, "0" * 64)
    try:
        with pytest.raises(CohortRegistryUnsafe, match="callable changed"):
            registry.list_selectors()
    finally:
        del vars(registry)["_load_state"]


def test_class_callable_replacement_is_rejected(
    registry: CohortRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(CohortRegistry, "_load_state", lambda self: ({}, "0" * 64))
    with pytest.raises(CohortRegistryUnsafe, match="callable changed"):
        registry.list_selectors()


def test_existing_empty_registry_requires_protected_expected_head(
    registry: CohortRegistry, live
) -> None:
    root = registry.root
    registry.close()
    with pytest.raises(CohortRegistryUnsafe, match="expected head is required"):
        CohortRegistry(
            root,
            linkage_store=live[0],
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )
    reopened = CohortRegistry(
        root,
        linkage_store=live[0],
        expected_trust_snapshot_sha256_by_provider=_pins(),
        expected_state_head_sha256="0" * 64,
    )
    reopened.close()
