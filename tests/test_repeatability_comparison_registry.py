"""Protected D07 comparison-registry derivation, replay, persistence, and projection."""

from __future__ import annotations

import hashlib
import inspect
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

import evidence_inspector.repeatability_comparison_registry as registry_module
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalOutcome,
    decide_longitudinal_member,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
)
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    RepeatabilityClassification,
    RepeatabilityReason,
    compare_repeatability,
    repeatability_comparison_sha256,
    repeatability_envelope_sha256,
    result_trust_document_sha256,
)
from evidence_inspector.repeatability_comparison_registry import (
    ComparisonAuthorityState,
    RegisteredComparisonObject,
    RepeatabilityComparisonRegistry,
    RepeatabilityComparisonRegistryConflict,
    RepeatabilityComparisonRegistryStale,
    RepeatabilityComparisonRegistryUnsafe,
    registered_comparison_object_from_bytes,
    repeatability_comparison_backup_from_bytes,
)
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    NOW,
    PROVIDER,
    TRUST_SHA256,
    _policy,
    _record,
)
from tests.test_repeatability_comparison import (
    AUTHORITY_SHA256,
    EVIDENCE_SHA256,
    PROTOCOL_SHA256,
    RESULT_TRUST_DOCUMENT,
    RESULT_TRUST_SHA256,
    _envelope,
    _observation,
)

PINS = {PROVIDER: TRUST_SHA256}


class Live:
    def __init__(self, store: ProviderLinkageStore, clock: AuthorityTimeSource, active):
        self.store = store
        self.clock = clock
        self.active = active


@pytest.fixture
def live(tmp_path: Path):
    clock = AuthorityTimeSource.fixed(NOW)
    store = ProviderLinkageStore(
        tmp_path / "linkage",
        expected_trust_snapshot_sha256_by_provider=PINS,
        time_source=clock,
    )
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
        yield Live(store, clock, active)
    finally:
        store.close()


def _open(root: Path, live: Live, **overrides) -> RepeatabilityComparisonRegistry:
    values = {
        "linkage_store": live.store,
        "expected_trust_snapshot_sha256_by_provider": PINS,
        "result_trust_document": RESULT_TRUST_DOCUMENT,
        "expected_result_trust_sha256": RESULT_TRUST_SHA256,
    }
    values.update(overrides)
    return RepeatabilityComparisonRegistry(root, **values)


@pytest.fixture
def registry(tmp_path: Path, live: Live):
    value = _open(tmp_path / "d07", live)
    try:
        yield value
    finally:
        value.close()


def _pins(envelope) -> dict[str, str]:
    return {
        "expected_envelope_sha256": repeatability_envelope_sha256(envelope),
        "expected_evidence_sha256": EVIDENCE_SHA256,
        "expected_protocol_sha256": PROTOCOL_SHA256,
        "expected_repeatability_authority_sha256": AUTHORITY_SHA256,
    }


def _register(
    registry: RepeatabilityComparisonRegistry,
    live: Live,
    *,
    member_index: int = 1,
    anchor_value: float = 0.5,
    member_value: float = 0.55,
    envelope=None,
    **overrides,
):
    anchor = live.active[0]
    member = live.active[member_index]
    policy = _policy(anchor)
    envelope = envelope if envelope is not None else _envelope(anchor)
    values = {
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_authority_head_sha256": HEAD_SHA256,
        **_pins(envelope),
    }
    values.update(overrides)
    return registry.register_comparison(
        anchor,
        member,
        policy,
        _observation(anchor, anchor_value),
        _observation(member, member_value),
        envelope,
        **values,
    )


def _reopen(registry: RepeatabilityComparisonRegistry, live: Live, receipt, **overrides):
    values = {
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    values.update(overrides)
    return _open(registry.root, live, **values)


def _advance_linkage(live: Live) -> None:
    extra = _record("4")
    assert extra.authorized_linkage is not None
    live.store.commit_authorized_revision(extra.authorized_linkage)


def _direct(live: Live, *, member_value: float = 0.55, evaluated_at=NOW):
    anchor, member = live.active[0], live.active[1]
    policy = _policy(anchor)
    envelope = _envelope(anchor)
    decision = decide_longitudinal_member(
        anchor,
        member,
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
        expected_linkage_trust_snapshot_sha256_by_provider=PINS,
        linkage_store=live.store,
    )
    return compare_repeatability(
        anchor,
        member,
        policy,
        decision,
        _observation(anchor, 0.5),
        _observation(member, member_value),
        envelope,
        evaluated_at=evaluated_at,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
        expected_linkage_trust_snapshot_sha256_by_provider=PINS,
        linkage_store=live.store,
        result_trust_document=RESULT_TRUST_DOCUMENT,
        expected_result_trust_sha256=RESULT_TRUST_SHA256,
        **_pins(envelope),
    )


def test_registry_derives_comparison_and_replays_it_byte_identically(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    expected = _direct(live)
    receipt = _register(registry, live)
    resolved = registry.resolve(receipt.selector_id)

    assert receipt.state_version == 1
    assert receipt.availability is ComparisonAvailability.AVAILABLE
    assert receipt.classification is RepeatabilityClassification.NOISY_WITHIN_ENVELOPE
    assert canonical_contract_bytes(resolved.comparison) == canonical_contract_bytes(
        expected
    )
    assert resolved.comparison_sha256 == repeatability_comparison_sha256(expected)
    assert resolved.comparison_sha256 == receipt.comparison_sha256
    assert resolved.object_sha256 == receipt.object_sha256
    assert resolved.state_head_sha256 == receipt.state_head_sha256
    assert resolved.comparison.evaluated_at == NOW
    assert resolved.replayed_at == NOW
    assert resolved.replayed_against_live_linkage is True
    assert resolved.clinical_use_authorized is False
    assert resolved.comparison.trend_allowed is True


def test_registration_never_accepts_a_comparison_decision_or_time() -> None:
    parameters = inspect.signature(
        RepeatabilityComparisonRegistry.register_comparison
    ).parameters
    assert not {"decision", "comparison", "evaluated_at"} & set(parameters)
    assert not {"result_trust_document", "linkage_store"} & set(parameters)


def test_evaluation_time_is_the_live_authority_clock_not_the_caller(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    later = NOW + timedelta(minutes=20)
    live.clock.advance_to(later)
    receipt = _register(registry, live)
    resolved = registry.resolve(receipt.selector_id)
    assert resolved.comparison.evaluated_at == later
    assert resolved.replayed_at == later

    live.clock.advance_to(later + timedelta(minutes=20))
    current = registry.resolve(receipt.selector_id)
    # The stored artifact is byte-identical; the read carries its live as-of time.
    assert current.comparison_sha256 == receipt.comparison_sha256
    assert current.comparison.evaluated_at == later
    assert current.replayed_at == later + timedelta(minutes=20)


def test_linkage_authority_expiry_is_stale_not_a_storage_error(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    live.clock.advance_to(NOW + timedelta(hours=2))
    with pytest.raises(RepeatabilityComparisonRegistryStale, match="not current"):
        registry.resolve(receipt.selector_id)
    assert registry.list_selectors().records[0].authority_state is (
        ComparisonAuthorityState.STALE
    )
    with pytest.raises(RepeatabilityComparisonRegistryStale, match="not current"):
        _register(registry, live, member_index=2)
    assert registry.list_selectors().state_version == 1


def test_d03_decision_binds_to_the_registered_d03_series_member(
    tmp_path: Path, registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    anchor = live.active[0]
    policy = _policy(anchor)
    d03 = LongitudinalDecisionRegistry(
        tmp_path / "d03",
        linkage_store=live.store,
        expected_trust_snapshot_sha256_by_provider=PINS,
    )
    try:
        series = d03.resolve(
            d03.register_series(
                anchor,
                live.active[1:],
                policy,
                expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
                expected_authority_head_sha256=HEAD_SHA256,
            ).selector_id
        )
    finally:
        d03.close()
    receipt = _register(registry, live, member_index=1)
    comparison = registry.resolve(receipt.selector_id).comparison
    member_digests = {
        longitudinal_member_decision_sha256(item) for item in series.decision.decisions
    }
    assert comparison.d03_decision_sha256 in member_digests
    row = registry.list_selectors().records[0]
    assert row.d03_decision_sha256 == comparison.d03_decision_sha256
    assert row.d03_outcome is LongitudinalOutcome.EQUIVALENT


def test_exact_same_inputs_are_idempotent(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    first = _register(registry, live)
    second = _register(registry, live)
    assert second == first
    assert registry.list_selectors().state_version == 1


def test_invalid_inputs_are_rejected_without_publication(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="pins"):
        _register(registry, live, expected_policy_sha256="not-a-digest")
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="pins"):
        _register(registry, live, expected_envelope_sha256=None)
    anchor, member = live.active[0], live.active[1]
    envelope = _envelope(anchor)
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="D03 member"):
        registry.register_comparison(
            anchor,
            "not-a-record",  # type: ignore[arg-type]
            _policy(anchor),
            _observation(anchor, 0.5),
            _observation(member, 0.55),
            envelope,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(_policy(anchor)),
            expected_authority_head_sha256=HEAD_SHA256,
            **_pins(envelope),
        )
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="replayable"):
        registry.register_comparison(
            anchor,
            member,
            _policy(anchor),
            object(),  # type: ignore[arg-type]
            _observation(member, 0.55),
            envelope,
            expected_policy_sha256=longitudinal_anchor_policy_sha256(_policy(anchor)),
            expected_authority_head_sha256=HEAD_SHA256,
            **_pins(envelope),
        )
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="replayable"):
        registry.register_comparison(
            anchor,
            member,
            _policy(anchor),
            _observation(anchor, 0.5),
            _observation(member, 0.55),
            None,  # type: ignore[arg-type]
            expected_policy_sha256=longitudinal_anchor_policy_sha256(_policy(anchor)),
            expected_authority_head_sha256=HEAD_SHA256,
            **_pins(envelope),
        )
    assert registry.list_selectors().state_version == 0
    assert os.listdir(registry.root / "objects") == []


def test_stored_object_cannot_pair_a_comparison_with_other_inputs(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    stored = registered_comparison_object_from_bytes(content)
    values = {name: getattr(stored, name) for name in type(stored).model_fields}
    assert RegisteredComparisonObject(**values) == stored
    anchor = live.active[0]
    for field, value, match in (
        ("anchor", live.active[1], "anchor"),
        ("member", live.active[2], "member"),
        ("policy", stored.policy.model_copy(update={"version": "1.0.1"}), "policy"),
        ("envelope", _envelope(anchor, limit=0.2), "envelope"),
    ):
        with pytest.raises(ValueError, match=match):
            RegisteredComparisonObject(**{**values, field: value})


def test_unavailable_comparison_registers_with_every_value_suppressed(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live, member_value=0.9)
    assert receipt.availability is ComparisonAvailability.UNAVAILABLE
    assert receipt.classification is RepeatabilityClassification.OUTSIDE_ENVELOPE
    comparison = registry.resolve(receipt.selector_id).comparison
    assert comparison.trend_allowed is False
    assert all(
        getattr(comparison, name) is None
        for name in (
            "anchor_value",
            "member_value",
            "delta",
            "anchor_uncertainty_lower",
            "anchor_uncertainty_upper",
            "member_uncertainty_lower",
            "member_uncertainty_upper",
            "anchor_denominator_count",
            "member_denominator_count",
            "maximum_absolute_delta",
        )
    )
    row = registry.list_selectors().records[0]
    assert row.availability is ComparisonAvailability.UNAVAILABLE
    assert row.authority_state is ComparisonAuthorityState.CURRENT


def test_linkage_advance_makes_comparison_stale_without_returning_it(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    assert registry.list_selectors().records[0].authority_state is (
        ComparisonAuthorityState.CURRENT
    )
    _advance_linkage(live)
    with pytest.raises(RepeatabilityComparisonRegistryStale, match="live authority"):
        registry.resolve(receipt.selector_id)
    page = registry.list_selectors()
    assert page.records[0].authority_state is ComparisonAuthorityState.STALE
    assert page.records[0].selector_id == receipt.selector_id


def test_envelope_expiry_after_registration_is_stale(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    # The linkage fixture's approvals expire an hour after NOW, so the envelope
    # window must close first for this to isolate envelope expiry.
    envelope = _envelope(live.active[0]).model_copy(
        update={"valid_through": NOW + timedelta(minutes=30)}
    )
    receipt = _register(registry, live, envelope=envelope)
    assert receipt.availability is ComparisonAvailability.AVAILABLE
    live.clock.advance_to(envelope.valid_through)
    assert registry.resolve(receipt.selector_id).replayed_at == envelope.valid_through

    live.clock.advance_to(envelope.valid_through + timedelta(seconds=1))
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        registry.resolve(receipt.selector_id)
    assert registry.list_selectors().records[0].authority_state is (
        ComparisonAuthorityState.STALE
    )
    # A registration now derives the expired-evidence state, never a value.
    expired = _register(registry, live, member_index=2, envelope=envelope)
    assert expired.availability is ComparisonAvailability.UNAVAILABLE
    assert expired.classification is RepeatabilityClassification.EVIDENCE_UNAVAILABLE
    stored = registry.resolve(expired.selector_id).comparison
    assert stored.reason_codes == (RepeatabilityReason.EVIDENCE_STALE,)


def test_not_yet_valid_envelope_becomes_stale_when_its_window_opens(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    anchor = live.active[0]
    future = _envelope(anchor).model_copy(
        update={
            "valid_from": NOW + timedelta(days=1),
            "valid_through": NOW + timedelta(days=30),
        }
    )
    receipt = _register(registry, live, envelope=future)
    assert receipt.classification is RepeatabilityClassification.EVIDENCE_UNAVAILABLE
    assert registry.resolve(receipt.selector_id).comparison.delta is None

    live.clock.advance_to(NOW + timedelta(days=2))
    # The suppressed verdict no longer describes the inputs; it is not current.
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        registry.resolve(receipt.selector_id)


def test_result_trust_revocation_makes_comparison_stale(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    registry.close()
    key = RESULT_TRUST_DOCUMENT.keys[0]
    revoked = RESULT_TRUST_DOCUMENT.model_copy(
        update={"keys": (key.model_copy(update={"revoked": True}),)}
    )
    reopened = _reopen(
        registry,
        live,
        receipt,
        result_trust_document=revoked,
        expected_result_trust_sha256=result_trust_document_sha256(revoked),
    )
    try:
        with pytest.raises(RepeatabilityComparisonRegistryStale):
            reopened.resolve(receipt.selector_id)
        assert reopened.list_selectors().records[0].authority_state is (
            ComparisonAuthorityState.STALE
        )
    finally:
        reopened.close()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
        _reopen(registry, live, receipt, expected_result_trust_sha256="0" * 64)


def _start_writer_on_first_comparison_digest(live: Live, monkeypatch):
    extra = _record("4")
    assert extra.authorized_linkage is not None
    original_sha256 = hashlib.sha256
    writer_started = threading.Event()
    writer_finished = threading.Event()
    workers: list[threading.Thread] = []
    finished_while_fenced: list[bool] = []

    def sha256(content=b"", *args, **kwargs):
        # D07 comparison canonical bytes start with this key.  The first such
        # digest happens inside the registry's held fence, before publication
        # or return; the last one validates the returned receipt or result.
        if not bytes(content).startswith(b'{"anchor_denominator_count"'):
            return original_sha256(content, *args, **kwargs)
        if workers:
            finished_while_fenced.append(writer_finished.wait(timeout=0.02))
        else:

            def write() -> None:
                writer_started.set()
                live.store.commit_authorized_revision(extra.authorized_linkage)
                writer_finished.set()

            worker = threading.Thread(target=write)
            workers.append(worker)
            worker.start()
            assert writer_started.wait(timeout=1)
            assert not writer_finished.wait(timeout=0.05)
        return original_sha256(content, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", sha256)
    return workers, writer_finished, finished_while_fenced


def test_register_holds_one_fence_through_commit_and_return(
    registry: RepeatabilityComparisonRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers, finished, observed = _start_writer_on_first_comparison_digest(
        live, monkeypatch
    )
    receipt = _register(registry, live)
    assert workers
    # Digests after publication (the receipt's) still saw the writer blocked.
    assert len(observed) >= 2 and not any(observed)
    workers[0].join(timeout=2)
    assert finished.is_set()
    monkeypatch.undo()
    assert receipt.availability is ComparisonAvailability.AVAILABLE
    # The writer landed only after registration returned, so the registered
    # comparison binds the pre-write linkage state and is now stale.
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        registry.resolve(receipt.selector_id)


def test_resolve_holds_one_fence_through_exact_return(
    registry: RepeatabilityComparisonRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(registry, live)
    workers, finished, observed = _start_writer_on_first_comparison_digest(
        live, monkeypatch
    )
    resolved = registry.resolve(receipt.selector_id)
    assert resolved.comparison_sha256 == receipt.comparison_sha256
    assert workers
    # The last digest validates the returned value; the writer was still blocked.
    assert len(observed) >= 2 and not any(observed)
    workers[0].join(timeout=2)
    assert finished.is_set()
    monkeypatch.undo()
    with pytest.raises(RepeatabilityComparisonRegistryStale):
        registry.resolve(receipt.selector_id)


def test_result_constructor_cannot_pair_a_selector_with_another_comparison(
    registry: RepeatabilityComparisonRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _register(registry, live)
    two = _register(registry, live, member_index=2, member_value=0.5)
    other = registry.resolve(two.selector_id)

    def substitute(**values):
        return registry_module.RegisteredRepeatabilityComparison.model_construct(
            **{
                **values,
                "comparison": other.comparison,
                "comparison_sha256": other.comparison_sha256,
            }
        )

    monkeypatch.setattr(registry_module, "_CR_RESOLVED", substitute)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="authority callable"):
        registry.resolve(one.selector_id)
    monkeypatch.undo()
    monkeypatch.setattr(
        registry_module, "RegisteredRepeatabilityComparison", substitute
    )
    assert registry.resolve(one.selector_id).comparison_sha256 == one.comparison_sha256
    monkeypatch.undo()

    resolved = registry.resolve(one.selector_id)
    values = {name: getattr(resolved, name) for name in type(resolved).model_fields}
    for update, match in (
        ({"comparison": other.comparison}, "digest"),
        ({"object_sha256": two.object_sha256}, "selector"),
        ({"replayed_at": NOW - timedelta(seconds=1)}, "replay time"),
    ):
        with pytest.raises(ValueError, match=match):
            type(resolved)(**{**values, **update})


def test_selector_page_is_private_and_paginated(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    first = _register(registry, live)
    second = _register(registry, live, member_index=2, member_value=0.9)

    page = registry.list_selectors(limit=1)
    rest = registry.list_selectors(after_selector_id=page.next_after_selector_id)
    rows = (*page.records, *rest.records)

    assert {row.selector_id for row in rows} == {first.selector_id, second.selector_id}
    assert rest.next_after_selector_id is None
    assert [row.selector_id for row in rows] == sorted(row.selector_id for row in rows)
    assert {row.classification for row in rows} == {
        RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
        RepeatabilityClassification.OUTSIDE_ENVELOPE,
    }
    content = canonical_contract_bytes(page) + canonical_contract_bytes(rest)
    private_values = {
        PROVIDER,
        "0.55",
        "0.9",
        "0.5",
        *(record.measurement.result_id for record in live.active),
        *(record.linkage_revision.linkage_id for record in live.active),
        *(key.key_id for key in RESULT_TRUST_DOCUMENT.keys),
    }
    assert all(value.encode("ascii") not in content for value in private_values)
    for forbidden in (b"value", b"delta", b"denominator", b"uncertainty", b"token"):
        assert forbidden not in content


def test_selector_and_page_bounds_are_sanitized(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    _register(registry, live)
    for selector in (
        "",
        "d07_comparison_" + "g" * 40,
        "d07_comparison_" + "a" * 39,
        "d03_series_" + "a" * 40,
        7,
    ):
        with pytest.raises(RepeatabilityComparisonRegistryConflict, match="selector"):
            registry.resolve(selector)  # type: ignore[arg-type]
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="unavailable"):
        registry.resolve("d07_comparison_" + "a" * 40)
    for limit in (0, 101, True):
        with pytest.raises(RepeatabilityComparisonRegistryConflict, match="page bound"):
            registry.list_selectors(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="cursor"):
        registry.list_selectors(after_selector_id="cohort_selector_x")


def test_cumulative_object_byte_bound_rejects_before_publication(
    registry: RepeatabilityComparisonRegistry,
    live: Live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _register(registry, live)
    stored = (registry.root / "objects" / f"{first.object_sha256}.json").stat()
    monkeypatch.setattr(registry_module, "MAX_TOTAL_OBJECT_BYTES", stored.st_size + 1)
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="byte bound"):
        _register(registry, live, member_index=2)
    monkeypatch.undo()
    assert registry.list_selectors().state_version == 1
    assert len(os.listdir(registry.root / "objects")) == 1


def test_object_tamper_and_extra_entries_fail_closed(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    object_path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = object_path.read_bytes()
    object_path.write_bytes(original.replace(b"0.55", b"0.56", 1))
    object_path.chmod(0o600)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="digest"):
        registry.resolve(receipt.selector_id)

    object_path.write_bytes(original)
    object_path.chmod(0o600)
    (registry.root / "objects" / "notes.txt").write_text("private")
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="invalid object"):
        registry.list_selectors()


def test_committed_object_deletion_and_journal_rollback_fail_closed(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    root = registry.root
    object_path = root / "objects" / f"{receipt.object_sha256}.json"
    content = object_path.read_bytes()
    object_path.unlink()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()
    registry.close()

    object_path.write_bytes(content)
    object_path.chmod(0o600)
    (root / "registry-journal.jsonl").write_bytes(b"")
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="rollback"):
        _reopen(registry, live, receipt)


def _object_bytes_from_scratch(live: Live, tmp_path: Path, member_index: int):
    scratch = _open(tmp_path / f"scratch-{member_index}", live)
    try:
        receipt = _register(scratch, live, member_index=member_index)
        content = (
            scratch.root / "objects" / f"{receipt.object_sha256}.json"
        ).read_bytes()
    finally:
        scratch.close()
    return receipt.object_sha256, content


def _plant(registry: RepeatabilityComparisonRegistry, digest: str, content: bytes):
    path = registry.root / "objects" / f"{digest}.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_interrupted_publication_orphan_is_removed_on_next_registration(
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    orphan_path = _plant(registry, *_object_bytes_from_scratch(live, tmp_path, 2))
    assert registry.list_selectors().state_version == 0
    receipt = _register(registry, live)
    assert receipt.state_version == 1
    assert not orphan_path.exists()
    assert os.listdir(registry.root / "objects") == [f"{receipt.object_sha256}.json"]


def test_exact_uncommitted_object_is_adopted_and_two_orphans_fail_closed(
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    digest, content = _object_bytes_from_scratch(live, tmp_path, 1)
    _plant(registry, digest, content)
    receipt = _register(registry, live)
    assert receipt.object_sha256 == digest
    assert receipt.state_version == 1

    _plant(registry, *_object_bytes_from_scratch(live, tmp_path, 2))
    _plant(registry, "f" * 64, b"{}")
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()


def test_exact_crash_temporary_is_recovered_on_reopen(
    registry: RepeatabilityComparisonRegistry, live: Live
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
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    registry.close()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="required"):
        _open(registry.root, live)
    for override in (
        {"expected_state_head_sha256": "0" * 64},
        {"expected_registry_epoch_sha256": "0" * 64},
        {"expected_registry_id": "d07_registry_" + "0" * 32},
        {"expected_registry_id": "d03_registry_" + "0" * 32},
    ):
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="expected"):
            _reopen(registry, live, receipt, **override)
    (registry.root / "registry-metadata.json").unlink()
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="metadata is missing"):
        _reopen(registry, live, receipt)


def test_registry_cannot_be_rebound_to_another_linkage_store(
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    registry.close()
    other = ProviderLinkageStore(
        tmp_path / "other-linkage",
        expected_trust_snapshot_sha256_by_provider=PINS,
        time_source=AuthorityTimeSource.fixed(NOW),
    )
    try:
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="linkage authority"):
            _reopen(registry, live, receipt, linkage_store=other)
    finally:
        other.close()


def _restore_values(live: Live, receipt, **overrides):
    values = {
        "linkage_store": live.store,
        "expected_trust_snapshot_sha256_by_provider": PINS,
        "result_trust_document": RESULT_TRUST_DOCUMENT,
        "expected_result_trust_sha256": RESULT_TRUST_SHA256,
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    values.update(overrides)
    return values


def test_backup_restore_preserves_identity_and_replays(
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    assert repeatability_comparison_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    target = tmp_path / "restored"
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="result trust"):
        RepeatabilityComparisonRegistry.restore(
            target,
            backup,
            **_restore_values(live, receipt, expected_result_trust_sha256="0" * 64),
        )
    assert not target.exists()
    restored = RepeatabilityComparisonRegistry.restore(
        target, backup, **_restore_values(live, receipt)
    )
    try:
        resolved = restored.resolve(receipt.selector_id)
        assert resolved.comparison_sha256 == receipt.comparison_sha256
        assert resolved.state_head_sha256 == receipt.state_head_sha256
    finally:
        restored.close()
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="already exists"):
        RepeatabilityComparisonRegistry.restore(
            target, backup, **_restore_values(live, receipt)
        )


def test_old_or_tampered_backup_is_rejected_before_creating_a_target(
    registry: RepeatabilityComparisonRegistry, live: Live, tmp_path: Path
) -> None:
    _register(registry, live)
    old_backup = registry.backup_bytes()
    current = _register(registry, live, member_index=2)
    target = tmp_path / "rollback-restore"
    with pytest.raises(RepeatabilityComparisonRegistryConflict, match="expected head"):
        RepeatabilityComparisonRegistry.restore(
            target, old_backup, **_restore_values(live, current)
        )
    assert not target.exists()
    backup = registry.backup_bytes()
    tampered = backup.replace(b"noisy_within_envelope", b"exact_same_value", 1)
    assert tampered != backup
    with pytest.raises(RepeatabilityComparisonRegistryConflict):
        RepeatabilityComparisonRegistry.restore(
            target, tampered, **_restore_values(live, current)
        )
    assert not target.exists()


def test_peer_rejects_rollback_to_its_own_preappend_head(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    identity = registry.list_selectors()
    peer = _open(
        registry.root,
        live,
        expected_registry_id=identity.registry_id,
        expected_registry_epoch_sha256=identity.registry_epoch_sha256,
        expected_state_head_sha256=identity.state_head_sha256,
    )
    journal_path = registry.root / "registry-journal.jsonl"
    empty_journal = journal_path.read_bytes()
    try:
        _register(registry, live)
        journal_path.write_bytes(empty_journal)
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="rollback"):
            peer.list_selectors()
    finally:
        peer.close()


def test_instance_and_class_callable_shadows_are_rejected(
    registry: RepeatabilityComparisonRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    for name in ("resolve", "register_comparison", "list_selectors", "backup_bytes"):
        object.__getattribute__(registry, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="callable"):
            getattr(registry, name)
        del object.__getattribute__(registry, "__dict__")[name]
    for name in ("_replay_in_fence", "_compare_in_fence", "_live_time_in_fence"):
        monkeypatch.setattr(
            RepeatabilityComparisonRegistry, name, lambda self, *a: None
        )
        with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="callable"):
            registry.resolve(receipt.selector_id)
        monkeypatch.undo()
    assert registry.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256


def test_pinned_authority_replacement_is_rejected(
    registry: RepeatabilityComparisonRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    stored = registry.resolve(receipt.selector_id).comparison
    for name in (
        "_PINNED_COMPARE_IN_FENCE",
        "_PINNED_DECIDE_MEMBER",
        "_PINNED_COMPARISON_SHA256",
        "_PINNED_AUTHORITY_TIME_IN_FENCE",
        "_CR_REPLAY_IN_FENCE",
        "_CR_COMPARISON_SHA256",
    ):
        monkeypatch.setattr(registry_module, name, lambda *a, **k: stored)
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority callable"
        ):
            registry.resolve(receipt.selector_id)
        monkeypatch.undo()
    monkeypatch.setattr(
        registry_module.d07_module,
        "compare_repeatability_in_fence",
        lambda *a, **k: stored,
    )
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="authority callable"):
        registry.resolve(receipt.selector_id)
    monkeypatch.undo()
    monkeypatch.setattr(
        registry_module.d03_module, "decide_longitudinal_member", lambda *a, **k: None
    )
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="authority callable"):
        _register(registry, live)


@pytest.mark.parametrize(
    "name",
    (
        "_metadata",
        "_trusted_head_sha256",
        "_head_key",
        "_trust_pins",
        "_result_trust_sha256",
        "_result_trust_document",
    ),
)
def test_instance_authority_state_replacement_is_rejected(
    registry: RepeatabilityComparisonRegistry, live: Live, name: str
) -> None:
    receipt = _register(registry, live)
    instance = object.__getattribute__(registry, "__dict__")
    original = instance[name]
    key = RESULT_TRUST_DOCUMENT.keys[0]
    revoked = RESULT_TRUST_DOCUMENT.model_copy(
        update={"keys": (key.model_copy(update={"revoked": True}),)}
    )
    replacement = {
        "_metadata": original.model_copy(update={"registry_epoch_sha256": "0" * 64})
        if name == "_metadata"
        else None,
        "_trusted_head_sha256": "0" * 64,
        "_head_key": (0, 0, "x", "y"),
        "_trust_pins": {PROVIDER: "0" * 64},
        "_result_trust_sha256": result_trust_document_sha256(revoked),
        "_result_trust_document": revoked,
    }[name]
    instance[name] = replacement
    try:
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority state"
        ):
            registry.resolve(receipt.selector_id)
    finally:
        instance[name] = original


def test_bound_control_file_substitution_fails_closed(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    _register(registry, live)
    for name in (".registry.lock", "registry-metadata.json"):
        path = registry.root / name
        bound = registry.root / f"{name}.bound"
        os.replace(path, bound)
        path.write_bytes(bound.read_bytes())
        path.chmod(0o600)
        try:
            with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="storage"):
                registry.list_selectors()
        finally:
            path.unlink()
            os.replace(bound, path)


def test_wrong_pins_trust_and_store_type_are_rejected(tmp_path: Path, live: Live) -> None:
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="trust pins"):
        _open(
            tmp_path / "pins",
            live,
            expected_trust_snapshot_sha256_by_provider={PROVIDER: "0" * 64},
        )
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
        _open(tmp_path / "trust", live, expected_result_trust_sha256="0" * 64)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="result trust"):
        _open(tmp_path / "trust-type", live, result_trust_document=object())
    with pytest.raises(TypeError, match="exact linkage store"):
        _open(tmp_path / "type", live, linkage_store=object())
    for name in ("pins", "trust", "trust-type", "type"):
        assert not (tmp_path / name).exists()


def test_torn_journal_append_is_truncated_and_registration_retries(
    registry: RepeatabilityComparisonRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"d07-comparison-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="append failed"):
        _register(registry, live)
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""
    assert registry.list_selectors().state_version == 0

    receipt = _register(registry, live)
    assert receipt.state_version == 1
    assert registry.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256


def test_failed_restore_removes_its_partial_target_and_can_retry(
    registry: RepeatabilityComparisonRegistry,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = tmp_path / "partial-restore"
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(RepeatabilityComparisonRegistryUnsafe, match="restore failed"):
        RepeatabilityComparisonRegistry.restore(
            target, backup, **_restore_values(live, receipt)
        )
    monkeypatch.undo()
    assert not target.exists()

    restored = RepeatabilityComparisonRegistry.restore(
        target, backup, **_restore_values(live, receipt)
    )
    try:
        assert restored.resolve(receipt.selector_id).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()


def test_interpreter_warning_registry_does_not_disable_the_registry(
    registry: RepeatabilityComparisonRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert registry.resolve(receipt.selector_id).object_sha256 == receipt.object_sha256
    assert registry.list_selectors().state_version == 1


def test_consistent_result_trust_swap_is_rejected_by_the_instance_seal(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    instance = object.__getattribute__(registry, "__dict__")
    original = (instance["_result_trust_document"], instance["_result_trust_sha256"])
    key = RESULT_TRUST_DOCUMENT.keys[0]
    revoked = RESULT_TRUST_DOCUMENT.model_copy(
        update={"keys": (key.model_copy(update={"revoked": True}),)}
    )
    instance["_result_trust_document"] = revoked
    instance["_result_trust_sha256"] = result_trust_document_sha256(revoked)
    try:
        with pytest.raises(
            RepeatabilityComparisonRegistryUnsafe, match="authority state"
        ):
            registry.resolve(receipt.selector_id)
    finally:
        instance["_result_trust_document"], instance["_result_trust_sha256"] = original


def test_replay_rejects_a_live_time_before_registration(
    registry: RepeatabilityComparisonRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    stored = registered_comparison_object_from_bytes(content)
    with ProviderLinkageStore.authority_read_fence(live.store):
        assert registry._replay_in_fence(stored, NOW) == stored.comparison
        with pytest.raises(RepeatabilityComparisonRegistryStale):
            registry._replay_in_fence(stored, NOW - timedelta(seconds=1))
