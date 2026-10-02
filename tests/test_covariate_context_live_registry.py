"""Protected D10 context registry: persistence, live rebuild, and projection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import evidence_inspector.covariate_context_registry as registry_module
import evidence_inspector.denominator_policy_registry as d09_registry_module
from evidence_inspector.covariate_context import (
    CovariateClassification,
    covariate_context_result_sha256,
)
from evidence_inspector.covariate_context_registry import (
    ContextAuthorityState,
    CovariateContextRegistry,
    CovariateContextRegistryConflict,
    CovariateContextRegistryStale,
    CovariateContextRegistryUnsafe,
    covariate_context_backup_from_bytes,
)
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistryUnsafe,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from tests.test_covariate_context_live import (
    Live,
    _advance_linkage,
    build,
    covariates,
    make_live,
)
from tests.test_provider_linkage_store import _pins, _store
from tests import registry_storage_checks as storage_checks


@pytest.fixture
def env(tmp_path: Path):
    live, closers = make_live(tmp_path)
    registry = CovariateContextRegistry(
        tmp_path / "d10", d09_registry=live.d09, decision_registry=live.d03
    )
    closers.append(registry)
    try:
        yield live, registry
    finally:
        for item in reversed(closers):
            item.close()


def _register(live: Live, registry, values=None, **overrides):
    arguments = {
        "d09_selector_id": live.d09_receipt.selector_id,
        "d09_policy_version": 1,
        "d03_series_selector_id": live.d03_receipt.selector_id,
        "expected_d02_anchor_policy_sha256": live.policy_sha256,
    }
    arguments.update(overrides)
    return registry.register_context(
        covariates(live) if values is None else values, **arguments
    )


def _reopen(live: Live, registry, receipt, **overrides):
    arguments = {
        "d09_registry": live.d09,
        "decision_registry": live.d03,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    arguments.update(overrides)
    return CovariateContextRegistry(registry.root, **arguments)


def test_registration_derives_and_resolve_rebuilds_the_live_context(env) -> None:
    live, registry = env
    receipt = _register(live, registry)
    resolved = registry.resolve(receipt.selector_id)
    direct = build(live)

    assert receipt.state_version == 1
    assert resolved.live.context == direct.context
    assert resolved.live.live_d09_registry_verified is True
    assert resolved.live.d03_authority_verified is True
    assert resolved.context_sha256 == receipt.context_sha256
    assert receipt.context_sha256 == covariate_context_result_sha256(direct.context)
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.rebuilt_against_live_authority is True
    assert resolved.protected_local_only is True
    # Exact retry is idempotent.
    assert _register(live, registry) == receipt


def test_registration_rejects_inputs_that_do_not_derive_a_context(env) -> None:
    live, registry = env
    for values, overrides in (
        ((), {}),
        (covariates(live, member="f" * 64), {}),
        (None, {"expected_d02_anchor_policy_sha256": "e" * 64}),
        (None, {"d09_policy_version": 2}),
        (None, {"d03_series_selector_id": "d03_series_" + "0" * 40}),
    ):
        with pytest.raises(CovariateContextRegistryConflict):
            _register(live, registry, values, **overrides)
    for overrides in (
        {"d09_selector_id": "not-a-selector"},
        {"d09_policy_version": True},
        {"expected_d02_anchor_policy_sha256": "E" * 64},
    ):
        with pytest.raises(CovariateContextRegistryConflict):
            _register(live, registry, **overrides)
    with pytest.raises(CovariateContextRegistryConflict):
        _register(live, registry, list(covariates(live)))
    assert registry.list_selectors().state_version == 0


def test_resolve_never_returns_a_cached_context(env) -> None:
    live, registry = env
    receipt = _register(live, registry)
    live.values[6].revoke(live.values[5].key_id)
    with pytest.raises(CovariateContextRegistryStale):
        registry.resolve(receipt.selector_id)
    page = registry.list_selectors()
    (row,) = page.records
    assert row.authority_state is ContextAuthorityState.STALE
    assert row.classification is None and row.included_member_count is None


def test_linkage_change_makes_the_selector_stale(env) -> None:
    live, registry = env
    receipt = _register(live, registry)
    _advance_linkage(live)
    with pytest.raises(CovariateContextRegistryStale):
        registry.resolve(receipt.selector_id)


def test_selector_page_is_private_counted_and_paginated(env) -> None:
    live, registry = env
    first = _register(live, registry)
    second = _register(live, registry, covariates(live, protocol="5"))
    page = registry.list_selectors(limit=1)
    assert len(page.records) == 1 and page.next_after_selector_id is not None
    rest = registry.list_selectors(after_selector_id=page.next_after_selector_id)
    rows = page.records + rest.records
    assert {row.selector_id for row in rows} == {
        first.selector_id,
        second.selector_id,
    }
    assert rest.next_after_selector_id is None
    for row in rows:
        assert row.authority_state is ContextAuthorityState.CURRENT
        assert row.classification is CovariateClassification.CLEAR
        assert (row.included_member_count, row.group_count) == (1, 1)
    encoded = canonical_contract_bytes(page) + canonical_contract_bytes(rest)
    resolved = registry.resolve(first.selector_id).live
    for secret in (
        live.member_result_sha256,
        resolved.d09_population.population_sha256,
        resolved.d09_population.included_members[0].result_id,
        resolved.context.member_contexts[0].values[0].token,
        live.d09_receipt.selector_id,
        live.d03_receipt.selector_id,
    ):
        assert secret.encode() not in encoded
    for limit in (0, 101, True):
        with pytest.raises(CovariateContextRegistryConflict):
            registry.list_selectors(limit=limit)
    with pytest.raises(CovariateContextRegistryConflict):
        registry.list_selectors(after_selector_id="d10_context_bad")
    with pytest.raises(CovariateContextRegistryConflict):
        registry.resolve("d10_context_" + "0" * 40)


def test_reopen_requires_retained_identity_head_and_the_same_authority(
    env, tmp_path: Path
) -> None:
    live, registry = env
    receipt = _register(live, registry)
    reopened = _reopen(live, registry, receipt)
    try:
        assert reopened.resolve(receipt.selector_id).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()
    with pytest.raises(CovariateContextRegistryUnsafe):
        CovariateContextRegistry(
            registry.root, d09_registry=live.d09, decision_registry=live.d03
        )
    with pytest.raises(CovariateContextRegistryUnsafe):
        _reopen(live, registry, receipt, expected_state_head_sha256="0" * 64)
    other_d03 = LongitudinalDecisionRegistry(
        tmp_path / "other-d03",
        linkage_store=live.store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        with pytest.raises(CovariateContextRegistryUnsafe):
            _reopen(live, registry, receipt, decision_registry=other_d03)
    finally:
        other_d03.close()


def test_registry_requires_d09_and_d03_on_one_linkage_store(
    env, tmp_path: Path
) -> None:
    live, _ = env
    other_store = _store(tmp_path / "other-linkage")
    other_d03 = LongitudinalDecisionRegistry(
        tmp_path / "other-d03",
        linkage_store=other_store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        with pytest.raises(CovariateContextRegistryUnsafe, match="one linkage"):
            CovariateContextRegistry(
                tmp_path / "d10-other",
                d09_registry=live.d09,
                decision_registry=other_d03,
            )
        with pytest.raises(TypeError):
            CovariateContextRegistry(
                tmp_path / "d10-bad", d09_registry=object(), decision_registry=live.d03
            )
    finally:
        other_d03.close()
        other_store.close()


def test_backup_restore_preserves_identity_and_rebuilds(env, tmp_path: Path) -> None:
    live, registry = env
    receipt = _register(live, registry)
    content = registry.backup_bytes()
    assert covariate_context_backup_from_bytes(content).state_head_sha256 == (
        receipt.state_head_sha256
    )
    restored = CovariateContextRegistry.restore(
        tmp_path / "restored",
        content,
        d09_registry=live.d09,
        decision_registry=live.d03,
        expected_registry_id=receipt.registry_id,
        expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
        expected_state_head_sha256=receipt.state_head_sha256,
    )
    try:
        assert restored.resolve(receipt.selector_id).live == registry.resolve(
            receipt.selector_id
        ).live
    finally:
        restored.close()
    with pytest.raises(CovariateContextRegistryConflict):
        CovariateContextRegistry.restore(
            tmp_path / "restored-wrong-head",
            content,
            d09_registry=live.d09,
            decision_registry=live.d03,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256="0" * 64,
        )
    assert not (tmp_path / "restored-wrong-head").exists()
    with pytest.raises(CovariateContextRegistryConflict):
        covariate_context_backup_from_bytes(content.replace(b'"objects"', b'"object"'))


def test_object_tamper_and_rollback_fail_closed(env) -> None:
    live, registry = env
    first = _register(live, registry)
    objects = registry.root / "objects"
    path = objects / f"{first.object_sha256}.json"
    original = path.read_bytes()
    os.chmod(path, 0o600)
    path.write_bytes(original.replace(b'"covariate_', b'"covariate_f', 1)[:-1] + b"}")
    with pytest.raises(CovariateContextRegistryUnsafe):
        registry.resolve(first.selector_id)
    path.write_bytes(original)
    assert registry.resolve(first.selector_id).object_sha256 == first.object_sha256

    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    _register(live, registry, covariates(live, protocol="5"))
    after = journal.read_bytes()
    journal.write_bytes(before)
    with pytest.raises(CovariateContextRegistryUnsafe):
        registry.list_selectors()
    journal.write_bytes(after)
    assert registry.list_selectors().state_version == 2


def test_instance_class_and_authority_shadows_are_rejected(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, registry = env
    receipt = _register(live, registry)
    registry.__dict__["resolve"] = lambda selector_id: None
    with pytest.raises(CovariateContextRegistryUnsafe):
        registry.resolve(receipt.selector_id)
    del registry.__dict__["resolve"]
    with monkeypatch.context() as patch:
        patch.setattr(registry_module, "_PINNED_BUILD_LIVE", lambda *a, **k: None)
        with pytest.raises(CovariateContextRegistryUnsafe):
            registry.resolve(receipt.selector_id)
    with monkeypatch.context() as patch:
        patch.setattr(registry_module, "_CR_RESOLVED", object)
        with pytest.raises(CovariateContextRegistryUnsafe):
            registry.resolve(receipt.selector_id)
    with monkeypatch.context() as patch:
        patch.setattr(registry, "_d09_registry", live.d09, raising=True)
        registry.__dict__["_decision_registry"] = object()
        with pytest.raises(CovariateContextRegistryUnsafe):
            registry.resolve(receipt.selector_id)
        registry.__dict__["_decision_registry"] = live.d03
    assert registry.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256


def test_upstream_tamper_propagates_as_unsafe_not_stale(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, registry = env
    receipt = _register(live, registry)
    monkeypatch.setattr(
        d09_registry_module, "_PINNED_BUILD_POPULATION", lambda **kwargs: None
    )
    with pytest.raises(DenominatorPolicyRegistryUnsafe):
        registry.resolve(receipt.selector_id)


def test_resolve_cannot_run_inside_a_held_linkage_fence(env) -> None:
    live, registry = env
    receipt = _register(live, registry)
    with type(live.store).authority_read_fence(live.store):
        with pytest.raises(CovariateContextRegistryStale):
            registry.resolve(receipt.selector_id)
    assert registry.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(env) -> None:
    live, registry = env
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: _register(live, registry),
        lambda values: _reopen(live, registry, values),
        CovariateContextRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, registry = env
    storage_checks.check_append_interrupt_truncates(
        registry, registry_module, lambda: _register(live, registry), monkeypatch
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    env, tmp_path: Path
) -> None:
    live, registry = env
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, CovariateContextRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    env,
) -> None:
    live, registry = env
    _register(live, registry)
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: _reopen(live, registry, values),
        CovariateContextRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, registry = env
    storage_checks.check_interrupted_creation(
        lambda root: CovariateContextRegistry(
            root, d09_registry=live.d09, decision_registry=live.d03
        ),
        lambda root, values: CovariateContextRegistry(
            root,
            d09_registry=live.d09,
            decision_registry=live.d03,
            **storage_checks.expected(values),
        ),
        tmp_path / "created-by-storage-check",
        registry_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, registry = env
    _register(live, registry)
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: CovariateContextRegistry.restore(
            target,
            backup,
            d09_registry=live.d09,
            decision_registry=live.d03,
            **storage_checks.expected(values),
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )


def test_storage_creation_under_a_symlinked_parent(env, tmp_path: Path) -> None:
    live, _ = env
    storage_checks.check_creation_under_symlinked_parent(
        lambda root: CovariateContextRegistry(
            root, d09_registry=live.d09, decision_registry=live.d03
        ),
        lambda root, values: CovariateContextRegistry(
            root,
            d09_registry=live.d09,
            decision_registry=live.d03,
            **storage_checks.expected(values),
        ),
        tmp_path,
    )
