"""Protected D09 policy-registry persistence, live rebuild, and projection tests."""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import evidence_inspector.denominator_policy_registry as registry_module
from evidence_inspector.cohort_import import CohortRecordCatalog
from evidence_inspector.cohort_manifest import cohort_manifest_sha256
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.cohort_summary import (
    CohortDenominatorPolicy,
    CohortDispositionPolicy,
    CohortMemberExclusionSet,
    CohortSummaryState,
    build_registered_cohort_denominator_summary,
)
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistry,
    DenominatorPolicyRegistryConflict,
    DenominatorPolicyRegistryStale,
    DenominatorPolicyRegistryUnsafe,
    PolicyAuthorityState,
    RegisteredDenominatorPolicyObject,
    denominator_policy_backup_from_bytes,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage import LinkageOperation, LinkageReasonCode
from evidence_inspector.result_catalog import DEFAULT_RESULT_BUNDLE_READER_REGISTRY
from tests.test_cohort_import import _import as _import_cohort_record
from tests.test_cohort_import import _setup as _setup_cohort_records
from tests.test_cohort_manifest import live as _cohort_live
from tests.test_cohort_summary import EMPTY_DISPOSITION_POLICY, POLICIES, _policy
from tests.test_provider_linkage import (
    _consume,
    _correction_approvals,
    _revision,
    _token,
)
from tests.test_provider_linkage_store import _pins


@dataclass
class Env:
    values: tuple
    manifest: object
    cohort_selector_id: str
    cohort_version: int
    manifest_sha256: str
    registry: DenominatorPolicyRegistry
    tmp_path: Path

    @property
    def cohort_registry(self) -> CohortRegistry:
        return self.values[11]

    @property
    def catalog(self) -> CohortRecordCatalog:
        return self.values[0]

    def policy(self, **changes: object) -> CohortDenominatorPolicy:
        values: dict[str, object] = {
            "inclusion_sha256": self.manifest.policies.inclusion_sha256,
            "exclusion_sha256": self.manifest.policies.exclusion_sha256,
            "missingness_sha256": self.manifest.policies.missingness_sha256,
        }
        values.update(changes)
        return _policy(**values)


@pytest.fixture
def env(tmp_path: Path):
    generator = _cohort_live.__wrapped__(tmp_path)
    live = next(generator)
    values = list(_setup_cohort_records(tmp_path / "records", live))
    values[2] = values[2].model_copy(update={"policies": POLICIES})
    values = tuple(values)
    manifest = values[2]
    values[11].register(manifest)
    selector = values[11].list_selectors().records[0]
    registry = DenominatorPolicyRegistry(
        tmp_path / "d09",
        cohort_registry=values[11],
        record_catalog=values[0],
    )
    try:
        yield Env(
            values=values,
            manifest=manifest,
            cohort_selector_id=selector.selector_id,
            cohort_version=selector.cohort_version,
            manifest_sha256=cohort_manifest_sha256(manifest),
            registry=registry,
            tmp_path=tmp_path,
        )
    finally:
        registry.close()
        values[0].close()
        values[1].close()
        values[11].close()
        with pytest.raises(StopIteration):
            next(generator)


def _register(env: Env, registry=None, policy=None, disposition=None, **overrides):
    values = {
        "expected_cohort_manifest_sha256": env.manifest_sha256,
    }
    values.update(overrides)
    return (registry or env.registry).register_policy(
        env.cohort_selector_id,
        env.cohort_version,
        policy or env.policy(),
        disposition or EMPTY_DISPOSITION_POLICY,
        **values,
    )


def _reopen(env: Env, receipt, **overrides):
    values = {
        "cohort_registry": env.cohort_registry,
        "record_catalog": env.catalog,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    values.update(overrides)
    return DenominatorPolicyRegistry(env.registry.root, **values)


def _correct_linkage(env: Env) -> None:
    store = env.cohort_registry._linkage_store
    previous = store.active_snapshot().revisions[0]
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
        proposed, _correction_approvals(proposed), previous=previous
    )
    store.commit_authorized_revision(correction)


def test_registry_binds_policy_pair_and_rebuilds_the_summary_on_resolve(
    env: Env,
) -> None:
    expected = build_registered_cohort_denominator_summary(
        registry=env.cohort_registry,
        selector_id=env.cohort_selector_id,
        cohort_version=env.cohort_version,
        record_catalog=env.catalog,
        policy=env.policy(),
        disposition_policy=EMPTY_DISPOSITION_POLICY,
    )
    receipt = _register(env)
    resolved = env.registry.resolve(receipt.selector_id, receipt.policy_version)

    assert receipt.state_version == 1
    assert receipt.policy_version == 1
    assert receipt.cohort_manifest_sha256 == env.manifest_sha256
    assert resolved.summary == expected
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert resolved.denominator_policy_sha256 == receipt.denominator_policy_sha256
    assert resolved.rebuilt_against_live_authority is True
    assert resolved.clinical_use_authorized is False
    assert resolved.summary.population.state is CohortSummaryState.NO_INCLUDED_UNITS


def test_resolve_never_returns_a_cached_summary_as_current(env: Env) -> None:
    receipt = _register(env)
    before = env.registry.resolve(receipt.selector_id, 1)
    assert before.summary.population.unavailable_denominator_units == 1

    _import_cohort_record(env.values)
    included = env.registry.resolve(receipt.selector_id, 1)
    assert included.summary.population.included_denominator_units == 1
    assert included.summary.summary_sha256 != before.summary.summary_sha256

    env.values[6].revoke(env.values[5].key_id)
    withheld = env.registry.resolve(receipt.selector_id, 1)
    assert withheld.summary.population.included_denominator_units == 0
    assert withheld.summary.population.unavailable_denominator_units == 1
    assert len({before.summary, included.summary, withheld.summary}) == 3
    # D06 changes are live data, not registry history.
    assert withheld.state_head_sha256 == receipt.state_head_sha256


def test_exact_same_inputs_are_idempotent(env: Env) -> None:
    first = _register(env)
    second = _register(env)
    assert second == first
    assert env.registry.list_selectors().state_version == 1


def test_policy_versions_extend_one_selector_contiguously(env: Env) -> None:
    first = _register(env)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="already registered"):
        _register(env, policy=env.policy(definition_sha256="1" * 64))
    with pytest.raises(DenominatorPolicyRegistryConflict, match="extend"):
        _register(env, policy=env.policy(version=3, definition_sha256="1" * 64))
    second = _register(env, policy=env.policy(version=2, definition_sha256="1" * 64))
    other = _register(env, policy=env.policy(policy_id="denominator_research_beta"))

    assert second.selector_id == first.selector_id
    assert second.policy_version == 2
    assert other.selector_id != first.selector_id
    assert env.registry.list_selectors().state_version == 3
    assert (
        env.registry.resolve(first.selector_id, 2).denominator_policy_sha256
        == second.denominator_policy_sha256
    )
    assert env.registry.resolve(first.selector_id, 1).object_sha256 == (
        first.object_sha256
    )


def test_registration_rejects_pairs_that_do_not_bind_the_live_manifest(
    env: Env,
) -> None:
    with pytest.raises(DenominatorPolicyRegistryConflict, match="live selection"):
        _register(env, expected_cohort_manifest_sha256="0" * 64)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="live selection"):
        _register(
            env,
            policy=env.policy(missingness_sha256="4" * 64),
            disposition=EMPTY_DISPOSITION_POLICY.model_copy(
                update={"missingness_sha256": "4" * 64}
            ),
        )
    mismatched = CohortDispositionPolicy(
        inclusion=CohortMemberExclusionSet(member_sha256s=("a" * 64,)),
        exclusion=EMPTY_DISPOSITION_POLICY.exclusion,
        missingness_sha256=EMPTY_DISPOSITION_POLICY.missingness_sha256,
    )
    with pytest.raises(DenominatorPolicyRegistryConflict, match="canonical"):
        _register(env, disposition=mismatched)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="selection"):
        env.registry.register_policy(
            "cohort_selector_x",
            env.cohort_version,
            env.policy(),
            EMPTY_DISPOSITION_POLICY,
            expected_cohort_manifest_sha256=env.manifest_sha256,
        )
    poisoned = env.policy().model_copy()
    object.__setattr__(poisoned, "__pydantic_private__", {"hidden": "x"})
    with pytest.raises(DenominatorPolicyRegistryConflict, match="canonical"):
        _register(env, policy=poisoned)
    assert env.registry.list_selectors().state_version == 0
    assert os.listdir(env.registry.root / "objects") == []


def test_stored_object_cannot_pair_a_policy_with_other_rule_sets(env: Env) -> None:
    values = {
        "cohort_registry_id": env.registry.list_selectors().registry_id.replace(
            "d09_registry_", "cohort_registry_"
        ),
        "cohort_registry_epoch_sha256": "1" * 64,
        "cohort_selector_id": env.cohort_selector_id,
        "cohort_version": 1,
        "cohort_manifest_sha256": env.manifest_sha256,
        "policy": env.policy(),
        "disposition_policy": EMPTY_DISPOSITION_POLICY,
    }
    assert RegisteredDenominatorPolicyObject(**values)
    for update in (
        {"policy": env.policy(missingness_sha256="4" * 64)},
        {"policy": env.policy(inclusion_sha256="4" * 64)},
        {"policy": env.policy(exclusion_sha256="4" * 64)},
    ):
        with pytest.raises(ValueError, match="rule sets"):
            RegisteredDenominatorPolicyObject(**{**values, **update})


def test_linkage_correction_makes_selector_stale_without_a_summary(
    env: Env,
) -> None:
    receipt = _register(env)
    row = env.registry.list_selectors().records[0]
    assert row.authority_state is PolicyAuthorityState.CURRENT
    assert row.declared_members == 1

    _correct_linkage(env)

    with pytest.raises(DenominatorPolicyRegistryStale, match="live authority"):
        env.registry.resolve(receipt.selector_id, 1)
    stale = env.registry.list_selectors().records[0]
    assert stale.selector_id == receipt.selector_id
    assert stale.authority_state is PolicyAuthorityState.STALE
    assert stale.summary_sha256 is None
    assert stale.declared_members is None
    assert stale.cohort_manifest_sha256 == env.manifest_sha256


def test_resolve_holds_the_d09_lock_through_exact_return(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    beta = env.policy(policy_id="denominator_research_beta")
    original_sha256 = hashlib.sha256
    writer_started = threading.Event()
    writer_finished = threading.Event()
    workers: list[threading.Thread] = []
    armed = {"value": True}

    def sha256(content=b"", *args, **kwargs):
        if armed["value"] and b"traceback-d09-disposition-policy-v1" in bytes(content):
            # Digested while the resolved result is constructed, after rebuild.
            armed["value"] = False

            def write() -> None:
                writer_started.set()
                _register(env, policy=beta)
                writer_finished.set()

            worker = threading.Thread(target=write)
            workers.append(worker)
            worker.start()
            assert writer_started.wait(timeout=1)
            # A registration takes well under this bound when unblocked, so
            # only a held D09 lock keeps the writer out until resolve returns.
            assert not writer_finished.wait(timeout=2)
        return original_sha256(content, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", sha256)
    resolved = env.registry.resolve(receipt.selector_id, 1)
    assert workers
    workers[0].join(timeout=10)
    monkeypatch.undo()
    assert writer_finished.is_set()
    assert resolved.state_version == 1
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert env.registry.list_selectors().state_version == 2


def test_result_constructor_cannot_pair_a_selector_with_another_summary(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _register(env)
    two = _register(env, policy=env.policy(policy_id="denominator_research_beta"))
    other = env.registry.resolve(two.selector_id, 1)

    def substitute(**values):
        return registry_module.RegisteredDenominatorPolicySummary.model_construct(
            **{**values, "summary": other.summary}
        )

    monkeypatch.setattr(registry_module, "_PR_RESOLVED", substitute)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="authority callable"):
        env.registry.resolve(one.selector_id, 1)
    monkeypatch.undo()
    monkeypatch.setattr(registry_module, "RegisteredDenominatorPolicySummary", substitute)
    assert env.registry.resolve(one.selector_id, 1).object_sha256 == one.object_sha256
    monkeypatch.undo()

    resolved = env.registry.resolve(one.selector_id, 1)
    values = resolved.model_dump(mode="python")
    for update, match in (
        ({"summary": other.summary}, "selector"),
        ({"denominator_policy_sha256": two.denominator_policy_sha256}, "policy digest"),
        ({"selector_id": two.selector_id}, "selector"),
    ):
        with pytest.raises(ValueError, match=match):
            type(resolved)(**{**values, **update})


def test_selector_page_is_private_counted_and_paginated(env: Env) -> None:
    first = _register(env)
    second = _register(env, policy=env.policy(version=2, definition_sha256="1" * 64))
    third = _register(env, policy=env.policy(policy_id="denominator_research_beta"))

    page = env.registry.list_selectors(limit=1)
    rest = env.registry.list_selectors(
        after_selector_id=page.next_after_selector_id,
        after_policy_version=page.next_after_policy_version,
    )
    rows = (*page.records, *rest.records)
    assert {(row.selector_id, row.policy_version) for row in rows} == {
        (first.selector_id, 1),
        (second.selector_id, 2),
        (third.selector_id, 1),
    }
    assert rest.next_after_selector_id is None
    assert [(row.selector_id, row.policy_version) for row in rows] == sorted(
        (row.selector_id, row.policy_version) for row in rows
    )
    for row in rows:
        assert row.authority_state is PolicyAuthorityState.CURRENT
        assert row.declared_members == 1
        assert row.unavailable_denominator_units == 1
    content = canonical_contract_bytes(page) + canonical_contract_bytes(rest)
    member = env.manifest.members[0]
    private_values = {
        member.provider_namespace,
        member.subject_token,
        member.collection_token,
        member.specimen_token,
        member.analysis_record_id,
        member.run_token,
        env.cohort_selector_id,
        "denominator_research_alpha",
    }
    assert all(value.encode("ascii") not in content for value in private_values)


def test_selector_and_page_bounds_are_sanitized(env: Env) -> None:
    _register(env)
    for selector, version in (
        ("", 1),
        ("d09_policy_" + "g" * 40, 1),
        ("d09_policy_" + "a" * 39, 1),
        (7, 1),
        ("d09_policy_" + "a" * 40, 0),
        ("d09_policy_" + "a" * 40, True),
    ):
        with pytest.raises(DenominatorPolicyRegistryConflict, match="selector"):
            env.registry.resolve(selector, version)  # type: ignore[arg-type]
    with pytest.raises(DenominatorPolicyRegistryConflict, match="unavailable"):
        env.registry.resolve("d09_policy_" + "a" * 40, 1)
    for limit in (0, 101, True):
        with pytest.raises(DenominatorPolicyRegistryConflict, match="page bound"):
            env.registry.list_selectors(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(DenominatorPolicyRegistryConflict, match="incomplete"):
        env.registry.list_selectors(after_selector_id="d09_policy_" + "a" * 40)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="cursor"):
        env.registry.list_selectors(
            after_selector_id="cohort_selector_x", after_policy_version=1
        )


def test_cumulative_object_byte_bound_rejects_before_publication(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _register(env)
    stored = (env.registry.root / "objects" / f"{first.object_sha256}.json").stat()
    monkeypatch.setattr(registry_module, "MAX_TOTAL_OBJECT_BYTES", stored.st_size + 1)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="byte bound"):
        _register(env, policy=env.policy(policy_id="denominator_research_beta"))
    monkeypatch.undo()
    assert env.registry.list_selectors().state_version == 1
    assert len(os.listdir(env.registry.root / "objects")) == 1


def test_object_tamper_and_extra_entries_fail_closed(env: Env) -> None:
    receipt = _register(env)
    object_path = env.registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = object_path.read_bytes()
    object_path.write_bytes(b"{}")
    object_path.chmod(0o600)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="digest"):
        env.registry.list_selectors()
    object_path.write_bytes(original)
    object_path.chmod(0o600)
    (env.registry.root / "objects" / "notes.txt").write_text("private")
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="invalid object"):
        env.registry.list_selectors()


def test_committed_object_deletion_and_journal_rollback_fail_closed(
    env: Env,
) -> None:
    receipt = _register(env)
    root = env.registry.root
    object_path = root / "objects" / f"{receipt.object_sha256}.json"
    content = object_path.read_bytes()
    object_path.unlink()
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="inconsistent"):
        env.registry.list_selectors()
    env.registry.close()
    object_path.write_bytes(content)
    object_path.chmod(0o600)
    (root / "registry-journal.jsonl").write_bytes(b"")
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="rollback"):
        _reopen(env, receipt)


def _object_bytes_from_scratch(env: Env, name: str, policy) -> tuple[str, bytes]:
    scratch = DenominatorPolicyRegistry(
        env.tmp_path / f"scratch-{name}",
        cohort_registry=env.cohort_registry,
        record_catalog=env.catalog,
    )
    try:
        receipt = _register(env, registry=scratch, policy=policy)
        content = (
            scratch.root / "objects" / f"{receipt.object_sha256}.json"
        ).read_bytes()
    finally:
        scratch.close()
    return receipt.object_sha256, content


def _plant(env: Env, digest: str, content: bytes) -> Path:
    path = env.registry.root / "objects" / f"{digest}.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_interrupted_publication_orphan_is_removed_on_next_registration(
    env: Env,
) -> None:
    orphan = _plant(
        env,
        *_object_bytes_from_scratch(
            env, "beta", env.policy(policy_id="denominator_research_beta")
        ),
    )
    assert env.registry.list_selectors().state_version == 0
    receipt = _register(env)
    assert receipt.state_version == 1
    assert not orphan.exists()
    assert os.listdir(env.registry.root / "objects") == [
        f"{receipt.object_sha256}.json"
    ]


def test_exact_uncommitted_object_is_adopted_and_two_orphans_fail_closed(
    env: Env,
) -> None:
    digest, content = _object_bytes_from_scratch(env, "alpha", env.policy())
    _plant(env, digest, content)
    receipt = _register(env)
    assert receipt.object_sha256 == digest
    assert receipt.state_version == 1
    _plant(
        env,
        *_object_bytes_from_scratch(
            env, "beta", env.policy(policy_id="denominator_research_beta")
        ),
    )
    _plant(env, "f" * 64, b"{}")
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="inconsistent"):
        env.registry.list_selectors()


def test_exact_crash_temporary_is_recovered_on_reopen(env: Env) -> None:
    receipt = _register(env)
    env.registry.close()
    temporary = env.registry.root / "objects" / (".tmp-" + "a" * 32)
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    reopened = _reopen(env, receipt)
    try:
        assert not temporary.exists()
        assert reopened.resolve(receipt.selector_id, 1).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()


def test_reopen_requires_exact_retained_identity_and_head(env: Env) -> None:
    receipt = _register(env)
    env.registry.close()
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="required"):
        DenominatorPolicyRegistry(
            env.registry.root,
            cohort_registry=env.cohort_registry,
            record_catalog=env.catalog,
        )
    for override in (
        {"expected_state_head_sha256": "0" * 64},
        {"expected_registry_epoch_sha256": "0" * 64},
        {"expected_registry_id": "d09_registry_" + "0" * 32},
        {"expected_registry_id": "d03_registry_" + "0" * 32},
    ):
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="expected"):
            _reopen(env, receipt, **override)


def test_missing_metadata_never_bootstraps_existing_storage(env: Env) -> None:
    receipt = _register(env)
    env.registry.close()
    (env.registry.root / "registry-metadata.json").unlink()
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="metadata is missing"):
        _reopen(env, receipt)


def test_registry_is_bound_to_one_d05_registry_and_d06_catalog(env: Env) -> None:
    receipt = _register(env)
    env.registry.close()
    store = env.cohort_registry._linkage_store
    other_registry = CohortRegistry(
        env.tmp_path / "other-cohorts",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    other_catalog = CohortRecordCatalog(
        env.tmp_path / "other-records",
        result_catalog=env.values[1],
        linkage_store=store,
        cohort_registry=env.cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="does not match"):
            _reopen(env, receipt, cohort_registry=other_registry)
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="authority changed"):
            _reopen(env, receipt, record_catalog=other_catalog)
        with pytest.raises(TypeError, match="exact cohort registry"):
            _reopen(env, receipt, cohort_registry=object())
        with pytest.raises(TypeError, match="exact record catalog"):
            _reopen(env, receipt, record_catalog=object())
    finally:
        other_catalog.close()
        other_registry.close()


def test_backup_restore_preserves_identity_and_rebuilds(
    env: Env, tmp_path: Path
) -> None:
    receipt = _register(env)
    backup = env.registry.backup_bytes()
    assert denominator_policy_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    values = {
        "cohort_registry": env.cohort_registry,
        "record_catalog": env.catalog,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    restored = DenominatorPolicyRegistry.restore(tmp_path / "restored", backup, **values)
    try:
        resolved = restored.resolve(receipt.selector_id, 1)
        assert resolved.object_sha256 == receipt.object_sha256
        assert resolved.state_head_sha256 == receipt.state_head_sha256
    finally:
        restored.close()
    with pytest.raises(DenominatorPolicyRegistryConflict, match="already exists"):
        DenominatorPolicyRegistry.restore(tmp_path / "restored", backup, **values)


def test_old_and_tampered_backups_reject_before_creating_a_target(
    env: Env, tmp_path: Path
) -> None:
    _register(env)
    old_backup = env.registry.backup_bytes()
    current = _register(env, policy=env.policy(policy_id="denominator_research_beta"))
    values = {
        "cohort_registry": env.cohort_registry,
        "record_catalog": env.catalog,
        "expected_registry_id": current.registry_id,
        "expected_registry_epoch_sha256": current.registry_epoch_sha256,
        "expected_state_head_sha256": current.state_head_sha256,
    }
    target = tmp_path / "rollback-restore"
    with pytest.raises(DenominatorPolicyRegistryConflict, match="expected head"):
        DenominatorPolicyRegistry.restore(target, old_backup, **values)
    assert not target.exists()

    backup = env.registry.backup_bytes()
    tampered = backup.replace(b"denominator_research_beta", b"denominator_research_gama", 1)
    assert tampered != backup
    with pytest.raises(DenominatorPolicyRegistryConflict):
        DenominatorPolicyRegistry.restore(target, tampered, **values)
    assert not target.exists()


def test_peer_rejects_rollback_to_its_own_preappend_head(env: Env) -> None:
    identity = env.registry.list_selectors()
    peer = DenominatorPolicyRegistry(
        env.registry.root,
        cohort_registry=env.cohort_registry,
        record_catalog=env.catalog,
        expected_registry_id=identity.registry_id,
        expected_registry_epoch_sha256=identity.registry_epoch_sha256,
        expected_state_head_sha256=identity.state_head_sha256,
    )
    journal_path = env.registry.root / "registry-journal.jsonl"
    empty_journal = journal_path.read_bytes()
    try:
        _register(env)
        journal_path.write_bytes(empty_journal)
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="rollback"):
            peer.list_selectors()
    finally:
        peer.close()


def test_instance_and_class_callable_shadows_are_rejected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    for name in ("resolve", "register_policy", "list_selectors", "backup_bytes"):
        object.__getattribute__(env.registry, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="callable"):
            getattr(env.registry, name)
        del object.__getattribute__(env.registry, "__dict__")[name]
    monkeypatch.setattr(
        DenominatorPolicyRegistry, "_build_live_summary", lambda self, value: None
    )
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="callable"):
        env.registry.resolve(receipt.selector_id, 1)


def test_pinned_d09_authority_replacement_is_rejected(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    cached = env.registry.resolve(receipt.selector_id, 1).summary
    monkeypatch.setattr(
        registry_module, "_PINNED_BUILD_SUMMARY", lambda **kwargs: cached
    )
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="authority callable"):
        env.registry.resolve(receipt.selector_id, 1)
    monkeypatch.undo()
    monkeypatch.setattr(
        registry_module.d09_module,
        "build_registered_cohort_denominator_summary",
        lambda **kwargs: cached,
    )
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="authority callable"):
        env.registry.resolve(receipt.selector_id, 1)


@pytest.mark.parametrize(
    "name",
    (
        "_metadata",
        "_trusted_head_sha256",
        "_head_key",
        "_cohort_registry",
        "_record_catalog",
    ),
)
def test_instance_authority_state_replacement_is_rejected(
    env: Env, name: str
) -> None:
    receipt = _register(env)
    instance = object.__getattribute__(env.registry, "__dict__")
    original = instance[name]
    replacement = {
        "_metadata": lambda: original.model_copy(
            update={"registry_epoch_sha256": "0" * 64}
        ),
        "_trusted_head_sha256": lambda: "0" * 64,
        "_head_key": lambda: (0, 0, "x", "y"),
        "_cohort_registry": object,
        "_record_catalog": object,
    }[name]()
    instance[name] = replacement
    try:
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="authority state"):
            env.registry.resolve(receipt.selector_id, 1)
    finally:
        instance[name] = original


@pytest.mark.parametrize("name", (".registry.lock", "registry-metadata.json"))
def test_bound_control_file_substitution_fails_closed(env: Env, name: str) -> None:
    _register(env)
    path = env.registry.root / name
    bound = env.registry.root / f"{name}.bound"
    os.replace(path, bound)
    path.write_bytes(bound.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(DenominatorPolicyRegistryUnsafe, match="storage"):
            env.registry.list_selectors()
    finally:
        path.unlink()
        os.replace(bound, path)


def test_torn_journal_append_is_truncated_and_registration_retries(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"d09-policy-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="append failed"):
        _register(env)
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (env.registry.root / "registry-journal.jsonl").read_bytes() == b""
    assert env.registry.list_selectors().state_version == 0
    receipt = _register(env)
    assert receipt.state_version == 1
    assert env.registry.resolve(receipt.selector_id, 1).object_sha256 == (
        receipt.object_sha256
    )


def test_failed_restore_removes_its_partial_target_and_can_retry(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    backup = env.registry.backup_bytes()
    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = tmp_path / "partial-restore"
    values = {
        "cohort_registry": env.cohort_registry,
        "record_catalog": env.catalog,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="restore failed"):
        DenominatorPolicyRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()
    restored = DenominatorPolicyRegistry.restore(target, backup, **values)
    try:
        assert restored.resolve(receipt.selector_id, 1).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()


def test_interpreter_warning_registry_does_not_disable_the_registry(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    # Python adds this key to a module's globals whenever a warning is attributed
    # to it, even if the warning is filtered; the registry must stay usable.
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert env.registry.resolve(receipt.selector_id, 1).object_sha256 == (
        receipt.object_sha256
    )
    assert env.registry.list_selectors().state_version == 1


def test_selector_record_carries_live_counts_only_when_current(env: Env) -> None:
    _register(env)
    row = env.registry.list_selectors().records[0]
    values = row.model_dump(mode="python")
    with pytest.raises(ValueError, match="stale"):
        type(row)(**{**values, "authority_state": PolicyAuthorityState.STALE})
    with pytest.raises(ValueError, match="requires live counts"):
        type(row)(**{**values, "summary_sha256": None})
    with pytest.raises(ValueError, match="reconcile"):
        type(row)(**{**values, "included_members": 1})


def _forge_backup(backup, digests: tuple[str, ...]) -> tuple[bytes, str]:
    """Re-chain a backup so its journal commits exactly ``digests`` in order."""

    sizes = {
        item.object_sha256: len(item.object_json.encode("utf-8"))
        for item in backup.objects
    }
    previous = registry_module._metadata_genesis_sha256(backup.metadata)
    journal = []
    for sequence, digest in enumerate(digests, start=1):
        entry = registry_module._build_journal_entry(
            sequence=sequence,
            previous_entry_sha256=previous,
            object_sha256=digest,
            object_bytes=sizes[digest],
        )
        journal.append(entry)
        previous = entry.entry_sha256
    forged = backup.model_copy(
        update={
            "state_version": len(journal),
            "state_head_sha256": previous,
            "journal": tuple(journal),
            "objects": tuple(
                item for item in backup.objects if item.object_sha256 in digests
            ),
        }
    )
    return registry_module._canonical_backup_bytes(forged), previous


@pytest.mark.parametrize("shape", ("gap", "reordered"))
def test_backup_with_a_noncontiguous_policy_history_is_rejected(
    env: Env, tmp_path: Path, shape: str
) -> None:
    first = _register(env)
    second = _register(env, policy=env.policy(version=2, definition_sha256="1" * 64))
    backup = denominator_policy_backup_from_bytes(env.registry.backup_bytes())
    digests = (
        (second.object_sha256,)
        if shape == "gap"
        else (second.object_sha256, first.object_sha256)
    )
    content, head = _forge_backup(backup, digests)
    with pytest.raises(DenominatorPolicyRegistryConflict, match="history"):
        denominator_policy_backup_from_bytes(content)
    target = tmp_path / "gap-restore"
    with pytest.raises(DenominatorPolicyRegistryConflict, match="history"):
        DenominatorPolicyRegistry.restore(
            target,
            content,
            cohort_registry=env.cohort_registry,
            record_catalog=env.catalog,
            expected_registry_id=backup.metadata.registry_id,
            expected_registry_epoch_sha256=backup.metadata.registry_epoch_sha256,
            expected_state_head_sha256=head,
        )
    assert not target.exists()


def test_reordered_history_on_disk_fails_closed(env: Env) -> None:
    first = _register(env)
    second = _register(env, policy=env.policy(version=2, definition_sha256="1" * 64))
    backup = denominator_policy_backup_from_bytes(env.registry.backup_bytes())
    content, head = _forge_backup(backup, (second.object_sha256, first.object_sha256))
    env.registry.close()
    journal = b"".join(
        canonical_contract_bytes(entry) + b"\n"
        for entry in registry_module.DenominatorPolicyBackup.model_validate_json(
            content
        ).journal
    )
    (env.registry.root / "registry-journal.jsonl").write_bytes(journal)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="history"):
        DenominatorPolicyRegistry(
            env.registry.root,
            cohort_registry=env.cohort_registry,
            record_catalog=env.catalog,
            expected_registry_id=first.registry_id,
            expected_registry_epoch_sha256=first.registry_epoch_sha256,
            expected_state_head_sha256=head,
        )


def test_restore_rejects_other_d05_d06_authority_before_creating_a_target(
    env: Env, tmp_path: Path
) -> None:
    receipt = _register(env)
    backup = env.registry.backup_bytes()
    store = env.cohort_registry._linkage_store
    other_catalog = CohortRecordCatalog(
        tmp_path / "other-records",
        result_catalog=env.values[1],
        linkage_store=store,
        cohort_registry=env.cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    target = tmp_path / "other-restore"
    try:
        with pytest.raises(DenominatorPolicyRegistryConflict, match="authority"):
            DenominatorPolicyRegistry.restore(
                target,
                backup,
                cohort_registry=env.cohort_registry,
                record_catalog=other_catalog,
                expected_registry_id=receipt.registry_id,
                expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
                expected_state_head_sha256=receipt.state_head_sha256,
            )
    finally:
        other_catalog.close()
    assert not target.exists()


def test_failed_restore_reopen_removes_the_published_target(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(env)
    backup = env.registry.backup_bytes()
    original_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        # Restore creates the lock with O_CREAT; only the final reopen omits it.
        if path == ".registry.lock" and not flags & os.O_CREAT:
            raise OSError("descriptor exhausted")
        return original_open(path, flags, *args, **kwargs)

    target = tmp_path / "reopen-restore"
    values = {
        "cohort_registry": env.cohort_registry,
        "record_catalog": env.catalog,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(DenominatorPolicyRegistryUnsafe, match="restore failed"):
        DenominatorPolicyRegistry.restore(target, backup, **values)
    monkeypatch.undo()
    assert not target.exists()
    restored = DenominatorPolicyRegistry.restore(target, backup, **values)
    restored.close()


def test_resolve_cannot_run_inside_a_held_linkage_fence(env: Env) -> None:
    # Measured composition: the builder re-enters the non-nestable linkage
    # fence, so a caller-held fence turns resolve into a typed stale error.
    receipt = _register(env)
    store = env.cohort_registry._linkage_store
    with type(store).authority_read_fence(store):
        with pytest.raises(DenominatorPolicyRegistryStale):
            env.registry.resolve(receipt.selector_id, 1)
    assert env.registry.resolve(receipt.selector_id, 1).object_sha256 == (
        receipt.object_sha256
    )
