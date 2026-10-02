"""Durable saved-comparison registry: publication, recovery, reads and backup."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path

import pytest
from pydantic import ValidationError

import evidence_inspector.longitudinal_comparison_registry as registry_module
from evidence_inspector.longitudinal_comparison_registry import (
    MAX_BACKUP_BYTES,
    MAX_JOURNAL_BYTES,
    MAX_RECOVERY_BYTES,
    MAX_SAVED_COMPARISONS,
    DependencyFenceKind,
    DependencyHeadV1,
    DependencySlot,
    HeldSavedComparisonDependencies,
    LongitudinalComparisonRegistry,
    LongitudinalComparisonRegistryConflict,
    LongitudinalComparisonRegistryReadConflict,
    LongitudinalComparisonRegistryStale,
    LongitudinalComparisonRegistryUnsafe,
    RegisteredSavedComparisonV1,
    SavedComparisonAuthorityState,
    SavedComparisonCommitmentsV1,
    SavedComparisonDependencyFence,
    SavedComparisonDependencyHeadsV1,
    SavedComparisonFiltersV1,
    SavedComparisonJournalEntryV1,
    SavedComparisonMeasurementV1,
    SavedComparisonRecoveryRecordV1,
    SavedComparisonSelectionV1,
    SavedE06SourceRefV1,
    SavedFamilyProjectionRequestV1,
    SavedLongitudinalComparisonV1,
    build_saved_longitudinal_comparison,
    saved_comparison_backup_from_bytes,
    saved_comparison_object_bytes,
)
from evidence_inspector.method_registry import MethodFamily
from evidence_inspector.projection_policy_registry import (
    CnaSegmentStatistic,
    FragmentStatistic,
    ProjectionFamily,
    ProjectionSelectionRule,
    StatisticUnit,
)
from evidence_inspector.reader_authorization_registry import MeasurementScope

CREATED_AT = datetime(2026, 10, 1, 12, tzinfo=UTC)
COHORT = "cohort_selector_" + "1" * 40


def _head(prefix: str) -> DependencyHeadV1:
    return DependencyHeadV1(
        id=prefix + secrets.token_hex(16),
        epoch=secrets.token_hex(32),
        head=secrets.token_hex(32),
    )


def make_heads() -> SavedComparisonDependencyHeadsV1:
    d05 = _head("cohort_registry_")
    storage = secrets.token_hex(32)
    return SavedComparisonDependencyHeadsV1(
        d01_linkage=_head("store_"),
        d04_history=_head("ledger_"),
        d05_cohort=d05,
        reader_authorization=_head("reader_registry_"),
        d06_record_catalog=DependencyHeadV1(
            id=d05.id, epoch=d05.epoch, head=secrets.token_hex(32)
        ),
        e04_catalog=DependencyHeadV1(
            id="e04_catalog_" + storage[:32], epoch=storage, head=secrets.token_hex(32)
        ),
        result_trust=_head("result_trust_registry_"),
        e06_source=_head("e06_registry_"),
        d03_decision=_head("d03_registry_"),
        d07_comparison=_head("d07_registry_"),
        d09_summary=_head("d09_registry_"),
        d10_context=_head("d10_registry_"),
        anchor_policy=_head("anchor_registry_"),
        projection_policy=_head("projection_registry_"),
    )


def advance(
    heads: SavedComparisonDependencyHeadsV1, slot: DependencySlot
) -> SavedComparisonDependencyHeadsV1:
    current = getattr(heads, slot.value)
    update = {slot.value: current.model_copy(update={"head": secrets.token_hex(32)})}
    return SavedComparisonDependencyHeadsV1(
        **{**heads.model_dump(mode="python", exclude={"schema_version"}), **update}
    )


class FakeHeld(HeldSavedComparisonDependencies):
    def __init__(self, fence: FakeFence) -> None:
        self._fence = fence

    @property
    def fence_kind(self) -> DependencyFenceKind:
        return self._fence.kind

    def read_heads(self, scope):
        self._fence.reads += 1
        if self._fence.on_read is not None:
            self._fence.on_read(self._fence)
        return self._fence.heads

    def read_bindings(self):
        return registry_module._bindings_from_heads(self._fence.heads)


class FakeFence(SavedComparisonDependencyFence):
    """Test fence: mutable heads plus a hook on every head read."""

    def __init__(self, heads: SavedComparisonDependencyHeadsV1 | None = None) -> None:
        self.heads = heads or make_heads()
        self.kind = DependencyFenceKind.DIRECT_HEAD_REREAD
        self.reads = 0
        self.on_read: Callable[[FakeFence], None] | None = None

    @contextmanager
    def hold(self) -> Iterator[HeldSavedComparisonDependencies]:
        yield FakeHeld(self)


def family_request(**overrides) -> SavedFamilyProjectionRequestV1:
    values = {
        "family": ProjectionFamily.FRAGMENT,
        "selection_rule": ProjectionSelectionRule.FINITE_COMPONENTS,
        "statistics": (FragmentStatistic.FRACTION,),
        "statistic_units": (StatisticUnit.FRACTION,),
        "projection_policy_selector_id": "projection_policy_" + "4" * 40,
        "projection_policy_version": 1,
        "projection_policy_sha256": "7" * 64,
        "component_count": 1,
    }
    values.update(overrides)
    return SavedFamilyProjectionRequestV1(**values)


def make_saved(
    heads: SavedComparisonDependencyHeadsV1,
    *,
    version: int = 1,
    cohort: str = COHORT,
    replay: str = "a" * 64,
    anchor_version: int = 1,
) -> SavedLongitudinalComparisonV1:
    return build_saved_longitudinal_comparison(
        selection=SavedComparisonSelectionV1(
            cohort_selector_id=cohort,
            cohort_version=1,
            anchor_policy_selector_id="anchor_policy_" + "2" * 40,
            anchor_policy_version=anchor_version,
            approved_anchor_selector_id="anchor_candidate_" + "3" * 40,
            approved_anchor_version=1,
            projection_policy_selector_id="projection_policy_" + "4" * 40,
            projection_policy_version=1,
            d09_policy_selector_id="d09_policy_" + "5" * 40,
            d09_policy_version=1,
            measurement=SavedComparisonMeasurementV1(
                measurement_definition_sha256="6" * 64,
                scope=MeasurementScope(
                    family=MethodFamily.FRAGMENT_MEASUREMENT,
                    quantity_id="qty_short_fraction",
                    unit="unit_fraction",
                ),
            ),
            filters=SavedComparisonFiltersV1(),
        ),
        comparison_version=version,
        family_projection_request=family_request(),
        commitments=SavedComparisonCommitmentsV1(
            **{name: "b" * 64 for name in SavedComparisonCommitmentsV1.model_fields}
        ),
        e06_registry_id=heads.e06_source.id,
        e06_registry_epoch_sha256=heads.e06_source.epoch,
        e06_state_head_sha256=heads.e06_source.head,
        e06_sources=(
            SavedE06SourceRefV1(selector_id="e06_source_" + "c" * 40, source_version=1),
        ),
        dependency_heads=heads,
        workspace_replay_sha256=replay,
        created_at=CREATED_AT,
    )


@pytest.fixture
def fence() -> FakeFence:
    return FakeFence()


@pytest.fixture
def registry(tmp_path: Path, fence: FakeFence):
    value = LongitudinalComparisonRegistry(tmp_path / "saved", dependency_fence=fence)
    try:
        yield value
    finally:
        value.close()


def reopen(registry: LongitudinalComparisonRegistry, fence, **overrides):
    registry_id, epoch, _, head = registry.identity()
    values = {
        "dependency_fence": fence,
        "expected_registry_id": registry_id,
        "expected_registry_epoch_sha256": epoch,
        "expected_state_head_sha256": head,
    }
    values.update(overrides)
    return LongitudinalComparisonRegistry(registry.root, **values)


def root_names(registry: LongitudinalComparisonRegistry) -> set[str]:
    return set(os.listdir(registry.root))


def object_names(registry: LongitudinalComparisonRegistry) -> set[str]:
    return set(os.listdir(registry.root / "objects"))


# --- publication -------------------------------------------------------------


def test_publication_is_content_addressed_and_reopens_current(registry, fence) -> None:
    saved = make_saved(fence.heads)
    receipt = registry.register(saved, dependency_fence=fence)
    content = saved_comparison_object_bytes(saved)
    assert receipt.applied is True
    assert receipt.state_version == 1
    assert receipt.object_sha256 == hashlib.sha256(content).hexdigest()
    assert receipt.dependency_heads == fence.heads
    assert receipt.dependency_fence_kind is DependencyFenceKind.DIRECT_HEAD_REREAD
    assert receipt.saving_authorizes_export is False
    assert object_names(registry) == {f"{receipt.object_sha256}.json"}
    assert (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes() == (
        content
    )
    assert "publication-candidate.json" not in root_names(registry)
    reopened = registry.resolve(
        receipt.selector_id, 1, dependency_fence=fence
    )
    assert reopened.authority_state is SavedComparisonAuthorityState.CURRENT
    assert reopened.stale_dependencies == ()
    assert reopened.saved_object_json.encode() == content
    assert reopened.saved == saved
    assert reopened.saved_digest_is_current_authority is False
    assert reopened.current_values_require_fresh_replay is True
    assert reopened.state_head_sha256 == receipt.state_head_sha256


def test_exact_retry_is_idempotent(registry, fence) -> None:
    saved = make_saved(fence.heads)
    first = registry.register(saved, dependency_fence=fence)
    journal = (registry.root / "registry-journal.jsonl").read_bytes()
    second = registry.register(saved, dependency_fence=fence)
    assert second.applied is False
    assert second.model_dump(exclude={"applied"}) == first.model_dump(
        exclude={"applied"}
    )
    assert (registry.root / "registry-journal.jsonl").read_bytes() == journal
    assert len(object_names(registry)) == 1


def test_same_selector_and_version_with_other_bytes_never_overwrites(
    registry, fence
) -> None:
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    path = registry.root / "objects" / f"{first.object_sha256}.json"
    original = path.read_bytes()
    other = make_saved(fence.heads, replay="d" * 64)
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="other bytes"):
        registry.register(other, dependency_fence=fence)
    assert path.read_bytes() == original
    assert object_names(registry) == {path.name}
    assert registry.identity()[2] == 1


def test_versions_are_sequential_per_selector(registry, fence) -> None:
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="next version"):
        registry.register(make_saved(fence.heads, version=2), dependency_fence=fence)
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    second = registry.register(
        make_saved(fence.heads, version=2, replay="e" * 64), dependency_fence=fence
    )
    assert first.selector_id == second.selector_id
    assert second.state_version == 2
    other = registry.register(
        make_saved(fence.heads, anchor_version=2), dependency_fence=fence
    )
    assert other.selector_id != first.selector_id
    assert other.comparison_version == 1


def test_a_comparison_built_against_older_heads_is_refused(registry, fence) -> None:
    saved = make_saved(fence.heads)
    fence.heads = advance(fence.heads, DependencySlot.D07_COMPARISON)
    with pytest.raises(LongitudinalComparisonRegistryStale):
        registry.register(saved, dependency_fence=fence)
    assert object_names(registry) == set()
    assert registry.identity()[2] == 0


def test_pre_commit_head_race_rolls_the_candidate_back(registry, fence) -> None:
    saved = make_saved(fence.heads)

    def move_on_second_read(state: FakeFence) -> None:
        if state.reads == 2:
            state.heads = advance(state.heads, DependencySlot.D01_LINKAGE)

    fence.on_read = move_on_second_read
    with pytest.raises(LongitudinalComparisonRegistryStale, match="before commit"):
        registry.register(saved, dependency_fence=fence)
    assert object_names(registry) == set()
    assert "publication-candidate.json" not in root_names(registry)
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""


def test_final_head_race_returns_no_receipt_and_reopens_stale(registry, fence) -> None:
    saved = make_saved(fence.heads)

    def move_on_final_read(state: FakeFence) -> None:
        if state.reads == 3:
            state.heads = advance(state.heads, DependencySlot.D10_CONTEXT)

    fence.on_read = move_on_final_read
    with pytest.raises(LongitudinalComparisonRegistryStale, match="final return"):
        registry.register(saved, dependency_fence=fence)
    fence.on_read = None
    assert registry.identity()[2] == 1
    page = registry.list_selectors(dependency_fence=fence)
    assert page.records[0].authority_state is SavedComparisonAuthorityState.STALE
    assert page.records[0].stale_dependencies == (DependencySlot.D10_CONTEXT,)
    # An exact retry after authority moved is a conflict, never a receipt.
    with pytest.raises(LongitudinalComparisonRegistryStale):
        registry.register(saved, dependency_fence=fence)


def test_a_receiptless_commit_reopens_from_the_retained_predecessor_head(
    registry, fence
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    retained = registry.identity()

    def move_on_final_read(state: FakeFence) -> None:
        if state.reads == reads + 3:
            state.heads = advance(state.heads, DependencySlot.D10_CONTEXT)

    reads = fence.reads
    fence.on_read = move_on_final_read
    with pytest.raises(LongitudinalComparisonRegistryStale, match="final return"):
        registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    fence.on_read = None
    assert "publication-candidate.json" not in root_names(registry)
    reopened = reopen_with(registry.root, fence, retained)
    try:
        assert reopened.identity()[2] == 2
    finally:
        reopened.close()
    # Only one committed extension is accepted, never an older head.
    registry.register(make_saved(fence.heads, anchor_version=3), dependency_fence=fence)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        reopen_with(registry.root, fence, retained)


def test_temporary_names_are_owned_and_unlinked_without_touching_data(
    registry, fence, tmp_path
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    target = tmp_path / "keep.txt"
    target.write_bytes(b"not registry data")
    # A symlink under a temporary name: the name goes, the target is untouched.
    link = registry.root / "objects" / (".tmp-" + "9" * 32)
    link.symlink_to(target)
    assert registry.list_selectors(dependency_fence=fence).state_version == 1
    assert not os.path.lexists(link)
    assert target.read_bytes() == b"not registry data"
    # A hard link under a temporary name: the name goes, the other name still
    # reads the same bytes.
    linked = registry.root / (".tmp-" + "6" * 32)
    os.link(target, linked)
    assert registry.list_selectors(dependency_fence=fence).state_version == 1
    assert not linked.exists()
    assert target.read_bytes() == b"not registry data"
    # Ordinary interrupted-write residue is removed.
    leftover = registry.root / (".tmp-" + "7" * 32)
    leftover.write_bytes(b"partial")
    assert registry.list_selectors(dependency_fence=fence).state_version == 1
    assert not leftover.exists()
    # A directory under a temporary name makes unlink fail: fail closed.
    directory = registry.root / (".tmp-" + "8" * 32)
    directory.mkdir()
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.list_selectors(dependency_fence=fence)
    assert directory.is_dir()


def test_family_projection_request_is_exact() -> None:
    both = (FragmentStatistic.COUNT, FragmentStatistic.FRACTION)
    canonical = family_request(
        selection_rule=ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS,
        statistics=both,
        statistic_units=(StatisticUnit.ALIGNMENT_COUNT, StatisticUnit.FRACTION),
        component_count=0,
    )
    assert canonical.component_count == 0
    for overrides in (
        {
            "statistics": both,
            "statistic_units": (StatisticUnit.ALIGNMENT_COUNT, StatisticUnit.FRACTION),
            "component_count": 1,
        },
        {"statistics": (CnaSegmentStatistic.MEDIAN_LOG2,)},
        {"component_count": 0},
        {
            "selection_rule": ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS,
            "component_count": 1,
        },
        {
            "statistics": (FragmentStatistic.COUNT,),
            "statistic_units": (StatisticUnit.FRACTION,),
        },
        {
            "statistics": tuple(reversed(both)),
            "statistic_units": (StatisticUnit.FRACTION, StatisticUnit.ALIGNMENT_COUNT),
        },
        {
            "statistics": (FragmentStatistic.FRACTION, FragmentStatistic.FRACTION),
            "statistic_units": (StatisticUnit.FRACTION, StatisticUnit.FRACTION),
        },
    ):
        with pytest.raises(ValidationError):
            family_request(**overrides)
    heads = make_heads()
    saved = make_saved(heads)
    with pytest.raises(ValidationError):
        SavedLongitudinalComparisonV1.model_validate(
            {
                **saved.model_dump(),
                "family_projection_request": family_request(
                    projection_policy_version=2
                ).model_dump(),
            }
        )


def test_exact_retry_after_authority_moved_is_a_conflict(registry, fence) -> None:
    saved = make_saved(fence.heads)
    registry.register(saved, dependency_fence=fence)
    fence.heads = advance(fence.heads, DependencySlot.RESULT_TRUST)
    with pytest.raises(LongitudinalComparisonRegistryStale):
        registry.register(saved, dependency_fence=fence)


def test_exact_retry_rechecks_authority_inside_the_lock(registry, fence) -> None:
    saved = make_saved(fence.heads)
    registry.register(saved, dependency_fence=fence)
    reads = fence.reads

    def move_after_first_check(state: FakeFence) -> None:
        if state.reads == reads + 2:
            state.heads = advance(state.heads, DependencySlot.D04_HISTORY)

    fence.on_read = move_after_first_check
    with pytest.raises(LongitudinalComparisonRegistryStale):
        registry.register(saved, dependency_fence=fence)


def test_file_publication_never_overwrites(tmp_path) -> None:
    directory = tmp_path / "publish"
    directory.mkdir()
    (directory / "name").write_bytes(b"original")
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        with pytest.raises(FileExistsError):
            registry_module._publish_file(descriptor, "name", b"replacement")
    finally:
        os.close(descriptor)
    assert (directory / "name").read_bytes() == b"original"
    assert os.listdir(directory) == ["name"]


def test_journal_append_truncates_a_torn_suffix_itself(
    registry, fence, monkeypatch
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    journal = registry.root / "registry-journal.jsonl"
    before = journal.read_bytes()
    entry = registry_module.SavedComparisonJournalEntryV1.model_validate_json(
        before.splitlines()[0]
    )

    def torn(descriptor, content):
        os.write(descriptor, content[:7])
        raise KeyboardInterrupt

    monkeypatch.setattr(registry_module, "_write_all", torn)
    with pytest.raises(KeyboardInterrupt):
        LongitudinalComparisonRegistry._append_journal(registry, entry)
    assert journal.read_bytes() == before


# --- reopen and pages ----------------------------------------------------------


@pytest.mark.parametrize(
    "slot", [slot for slot in DependencySlot if slot is not DependencySlot.FAMILY_SOURCE]
)
def test_any_dependency_advance_reopens_stale(registry, fence, slot) -> None:
    saved = make_saved(fence.heads)
    receipt = registry.register(saved, dependency_fence=fence)
    fence.heads = advance(fence.heads, slot)
    reopened = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    assert reopened.authority_state is SavedComparisonAuthorityState.STALE
    assert reopened.stale_dependencies == (slot,)
    assert reopened.saved == saved
    assert reopened.saved.dependency_heads != reopened.live_dependency_heads


def test_family_source_slot_filling_reopens_stale(registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    fence.heads = fence.heads.model_copy(
        update={"family_source": _head("family_source_registry_")}
    )
    fence.heads = SavedComparisonDependencyHeadsV1.model_validate(
        fence.heads.model_dump()
    )
    reopened = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    assert reopened.stale_dependencies == (DependencySlot.FAMILY_SOURCE,)


def test_a_stale_reopen_cannot_be_constructed_as_current(registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    fence.heads = advance(fence.heads, DependencySlot.D03_DECISION)
    stale = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    payload = stale.model_dump()
    with pytest.raises(ValidationError):
        RegisteredSavedComparisonV1.model_validate(
            {**payload, "authority_state": "current", "stale_dependencies": ()}
        )
    with pytest.raises(ValidationError):
        RegisteredSavedComparisonV1.model_validate(
            {**payload, "authority_state": "current"}
        )


def test_reopen_read_race_is_a_retry_error(registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    reads = fence.reads

    def move_on_second_read(state: FakeFence) -> None:
        if state.reads == reads + 2:
            state.heads = advance(state.heads, DependencySlot.D09_SUMMARY)

    fence.on_read = move_on_second_read
    with pytest.raises(LongitudinalComparisonRegistryReadConflict):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)


def test_selector_pages_are_bounded_ordered_and_private(registry, fence) -> None:
    receipts = [
        registry.register(
            make_saved(fence.heads, anchor_version=index + 1), dependency_fence=fence
        )
        for index in range(5)
    ]
    for limit in (0, 101, True, 1.0):
        with pytest.raises(LongitudinalComparisonRegistryConflict):
            registry.list_selectors(dependency_fence=fence, limit=limit)
    with pytest.raises(LongitudinalComparisonRegistryConflict):
        registry.list_selectors(
            dependency_fence=fence, after_selector_id=receipts[0].selector_id
        )
    seen = []
    page = registry.list_selectors(dependency_fence=fence, limit=2)
    while True:
        seen.extend((row.selector_id, row.comparison_version) for row in page.records)
        if page.next_after_selector_id is None:
            break
        page = registry.list_selectors(
            dependency_fence=fence,
            after_selector_id=page.next_after_selector_id,
            after_version=page.next_after_version,
            limit=2,
        )
    assert seen == sorted((item.selector_id, 1) for item in receipts)
    encoded = page.model_dump_json()
    for token in (COHORT, fence.heads.d05_cohort.id, "anchor_policy_", "e06_source_"):
        assert token not in encoded


def test_selector_is_opaque_and_errors_carry_no_identity(registry, fence) -> None:
    saved = make_saved(fence.heads)
    receipt = registry.register(saved, dependency_fence=fence)
    assert COHORT[-40:] not in receipt.selector_id
    with pytest.raises(LongitudinalComparisonRegistryConflict) as caught:
        registry.resolve("saved_comparison_" + "f" * 40, 1, dependency_fence=fence)
    assert COHORT not in str(caught.value)
    for value in ("cohort_selector_" + "1" * 40, "saved_comparison_x", 7, None):
        with pytest.raises(LongitudinalComparisonRegistryConflict):
            registry.resolve(value, 1, dependency_fence=fence)


# --- bounds --------------------------------------------------------------------


def test_one_full_journal_fits_its_bound() -> None:
    heads = make_heads()
    heads = SavedComparisonDependencyHeadsV1.model_validate(
        {
            **heads.model_dump(),
            "family_source": DependencyHeadV1(
                id="f" * 41 + "_" + "0" * 32, epoch="1" * 64, head="2" * 64
            ),
        }
    )
    entry = registry_module._build_journal_entry(
        sequence=MAX_SAVED_COMPARISONS,
        previous_entry_sha256="0" * 64,
        selector_id="saved_comparison_" + "0" * 40,
        comparison_version=MAX_SAVED_COMPARISONS,
        object_sha256="0" * 64,
        object_bytes=registry_module.MAX_OBJECT_BYTES,
        dependency_heads=heads,
        dependency_fence_kind=DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE,
    )
    line = len(registry_module._entry_bytes(entry)) + 1
    assert line * MAX_SAVED_COMPARISONS <= MAX_JOURNAL_BYTES
    record = registry_module._build_recovery_record(
        registry_id="saved_comparison_registry_" + "0" * 32,
        registry_epoch_sha256="0" * 64,
        base_state_version=MAX_SAVED_COMPARISONS - 1,
        base_state_head_sha256="0" * 64,
        base_journal_bytes=MAX_JOURNAL_BYTES,
        entry=entry.model_copy(update={"previous_entry_sha256": "0" * 64}),
    )
    assert len(registry_module._recovery_bytes(record)) <= MAX_RECOVERY_BYTES
    # A complete backup at every bound stays inside 520 MiB.
    assert (
        registry_module._projected_backup_bytes(
            registry_module.MAX_BACKUP_HEADER_BYTES,
            MAX_SAVED_COMPARISONS * registry_module.MAX_OBJECT_BYTES,
        )
        <= MAX_BACKUP_BYTES
    )


def test_object_count_bound(registry, fence, monkeypatch) -> None:
    monkeypatch.setattr(registry_module, "MAX_SAVED_COMPARISONS", 2)
    for index in range(2):
        registry.register(
            make_saved(fence.heads, anchor_version=index + 1), dependency_fence=fence
        )
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="full"):
        registry.register(make_saved(fence.heads, anchor_version=9), dependency_fence=fence)
    assert len(object_names(registry)) == 2


def test_object_byte_bound_rejects_before_any_lock(registry, fence, monkeypatch) -> None:
    saved = make_saved(fence.heads)
    monkeypatch.setattr(
        registry_module, "MAX_OBJECT_BYTES", len(saved_comparison_object_bytes(saved)) - 1
    )
    fence.on_read = lambda _: pytest.fail("no dependency read before the bound")
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="bounded"):
        registry.register(saved, dependency_fence=fence)


def test_journal_bound_is_admission_checked(registry, fence, monkeypatch) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    size = (registry.root / "registry-journal.jsonl").stat().st_size
    monkeypatch.setattr(registry_module, "MAX_JOURNAL_BYTES", size + 10)
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="journal bound"):
        registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    assert len(object_names(registry)) == 1


def test_cumulative_backup_bytes_are_admission_checked(
    registry, fence, monkeypatch
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    current = len(registry.backup_bytes())
    monkeypatch.setattr(registry_module, "MAX_BACKUP_BYTES", current + 100)
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="backup bound"):
        registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    assert len(object_names(registry)) == 1
    assert "publication-candidate.json" not in root_names(registry)


# --- exact inputs ----------------------------------------------------------------


def test_inputs_must_be_exact_contracts(registry, fence) -> None:
    saved = make_saved(fence.heads)

    class Shadow(SavedLongitudinalComparisonV1):
        pass

    shadow = Shadow.model_validate(saved.model_dump())
    for value in (shadow, saved.model_dump(), None):
        with pytest.raises(LongitudinalComparisonRegistryConflict):
            registry.register(value, dependency_fence=fence)
    forged = SavedLongitudinalComparisonV1.model_construct(
        **{**dict(saved), "release_export_authorized": True}
    )
    with pytest.raises(LongitudinalComparisonRegistryConflict):
        registry.register(forged, dependency_fence=fence)
    with_private = saved.model_copy()
    object.__setattr__(with_private, "__pydantic_extra__", {"x": 1})
    with pytest.raises(LongitudinalComparisonRegistryConflict):
        registry.register(with_private, dependency_fence=fence)
    with pytest.raises(ValidationError):
        make_saved(fence.heads).model_validate(
            {**saved.model_dump(), "content_sha256": "0" * 64}
        )
    with pytest.raises(ValidationError):
        SavedComparisonFiltersV1(lineage_roles=("reanalysis", "biological_draw"))
    with pytest.raises(TypeError):
        registry.register(saved, dependency_fence=object())
    assert object_names(registry) == set()


def test_head_vector_slots_are_closed() -> None:
    heads = make_heads()
    payload = heads.model_dump()
    with pytest.raises(ValidationError):
        SavedComparisonDependencyHeadsV1.model_validate(
            {**payload, "d03_decision": payload["d07_comparison"]}
        )
    with pytest.raises(ValidationError):
        SavedComparisonDependencyHeadsV1.model_validate({**payload, "extra": 1})
    with pytest.raises(ValidationError):
        SavedComparisonDependencyHeadsV1.model_validate(
            {**payload, "d06_record_catalog": _head("cohort_registry_").model_dump()}
        )
    missing = dict(payload)
    del missing["d01_linkage"]
    with pytest.raises(ValidationError):
        SavedComparisonDependencyHeadsV1.model_validate(missing)


def test_misbehaving_fences_fail_closed(registry, fence) -> None:
    saved = make_saved(fence.heads)

    class WrongType(FakeHeld):
        def read_heads(self, scope):
            return self._fence.heads.model_dump()

    class Raising(FakeHeld):
        def read_heads(self, scope):
            raise RuntimeError("secret store path /private/x")

    class WrongKind(FakeHeld):
        @property
        def fence_kind(self):
            return "composite_authority_fence"

    for held_type, error in (
        (WrongType, LongitudinalComparisonRegistryUnsafe),
        (Raising, LongitudinalComparisonRegistryStale),
        (WrongKind, LongitudinalComparisonRegistryUnsafe),
    ):

        class Fence(FakeFence):
            @contextmanager
            def hold(self, _type=held_type):
                yield _type(self)

        bad = Fence(fence.heads)
        with pytest.raises(error) as caught:
            registry.register(saved, dependency_fence=bad)
        assert "/private" not in str(caught.value)
    assert object_names(registry) == set()


def test_registry_is_bound_to_its_dependency_stores(tmp_path, registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    other = FakeFence()
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="other dependency"):
        registry.register(make_saved(other.heads), dependency_fence=other)
    with pytest.raises(LongitudinalComparisonRegistryConflict):
        registry.resolve(receipt.selector_id, 1, dependency_fence=other)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe, match="dependency"):
        reopen(registry, other)


# --- crash windows -----------------------------------------------------------------


def _crash_child(root: Path, identity, heads, saved, point: str) -> None:
    fence = FakeFence(heads)
    registry_id, epoch, _, head = identity
    registry = LongitudinalComparisonRegistry(
        root,
        dependency_fence=fence,
        expected_registry_id=registry_id,
        expected_registry_epoch_sha256=epoch,
        expected_state_head_sha256=head,
    )
    original_publish = registry_module._publish_file
    original_write = registry_module._write_all
    original_open = registry_module._open_private_file

    def publish(directory_fd, name, content):
        linked = (
            point == "object_linked" and name != "publication-candidate.json"
        ) or (point == "candidate_linked" and name == "publication-candidate.json")
        if linked:
            # Crash after link(temp, name) and before unlink(temp).
            temporary = ".tmp-" + "c" * 32
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd
            )
            os.write(descriptor, content)
            os.fsync(descriptor)
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os._exit(0)
        if name != "publication-candidate.json":
            if point == "before_object":
                os._exit(0)
            if point == "object_temporary":
                descriptor = os.open(
                    ".tmp-" + "a" * 32,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.write(descriptor, content[: len(content) // 2])
                os._exit(0)
        if point == "candidate_temporary":
            descriptor = os.open(
                ".tmp-" + "b" * 32,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(descriptor, content[:10])
            os._exit(0)
        original_publish(directory_fd, name, content)

    def write(descriptor, content):
        if content.startswith(b'{"comparison_version"'):
            if point == "before_journal":
                os._exit(0)
            if point == "torn_journal":
                os.write(descriptor, content[: len(content) // 2])
                os._exit(0)
            if point == "journal_unsynced":
                os.write(descriptor, content)
                os._exit(0)
        original_write(descriptor, content)

    def open_private(directory_fd, name, maximum):
        if point == "after_commit" and name == "publication-candidate.json":
            os._exit(0)
        return original_open(directory_fd, name, maximum)

    registry_module._publish_file = publish
    registry_module._write_all = write
    registry_module._open_private_file = open_private
    registry.register(saved, dependency_fence=fence)
    os._exit(3)


COMMITTED_POINTS = {"journal_unsynced", "after_commit"}


@pytest.mark.parametrize(
    "point",
    [
        "candidate_temporary",
        "candidate_linked",
        "before_object",
        "object_linked",
        "object_temporary",
        "before_journal",
        "torn_journal",
        "journal_unsynced",
        "after_commit",
    ],
)
def test_every_crash_window_recovers(tmp_path, registry, fence, point) -> None:
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    identity = registry.identity()
    journal_before = (registry.root / "registry-journal.jsonl").read_bytes()
    saved = make_saved(fence.heads, anchor_version=2)
    process = get_context("fork").Process(
        target=_crash_child, args=(registry.root, identity, fence.heads, saved, point)
    )
    process.start()
    process.join(30)
    assert process.exitcode == 0
    if point not in {"candidate_temporary"}:
        assert "publication-candidate.json" in root_names(registry)
    # Startup recovery, holding only the retained base head.
    recovered = reopen_with(registry.root, fence, identity)
    try:
        assert "publication-candidate.json" not in root_names(recovered)
        assert not any(name.startswith(".tmp-") for name in root_names(recovered))
        assert not any(name.startswith(".tmp-") for name in object_names(recovered))
        digest = hashlib.sha256(saved_comparison_object_bytes(saved)).hexdigest()
        if point in COMMITTED_POINTS:
            assert recovered.identity()[2] == 2
            assert object_names(recovered) == {
                f"{first.object_sha256}.json",
                f"{digest}.json",
            }
            retry = recovered.register(saved, dependency_fence=fence)
            assert retry.applied is False
        else:
            assert recovered.identity()[2] == 1
            assert (recovered.root / "registry-journal.jsonl").read_bytes() == (
                journal_before
            )
            assert object_names(recovered) == {f"{first.object_sha256}.json"}
            retry = recovered.register(saved, dependency_fence=fence)
            assert retry.applied is True
        assert recovered.resolve(
            retry.selector_id, 1, dependency_fence=fence
        ).authority_state is SavedComparisonAuthorityState.CURRENT
    finally:
        recovered.close()


def test_a_live_reader_recovers_a_crashed_writer(tmp_path, registry, fence) -> None:
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    saved = make_saved(fence.heads, anchor_version=2)
    process = get_context("fork").Process(
        target=_crash_child,
        args=(registry.root, registry.identity(), fence.heads, saved, "torn_journal"),
    )
    process.start()
    process.join(30)
    assert process.exitcode == 0
    reopened = registry.resolve(first.selector_id, 1, dependency_fence=fence)
    assert reopened.authority_state is SavedComparisonAuthorityState.CURRENT
    assert registry.identity()[2] == 1


def _write_candidate(registry, record: SavedComparisonRecoveryRecordV1) -> None:
    path = registry.root / "publication-candidate.json"
    path.write_bytes(registry_module._recovery_bytes(record))
    path.chmod(0o600)


def _pending_record(registry, fence, saved) -> SavedComparisonRecoveryRecordV1:
    registry_id, epoch, version, head = registry.identity()
    content = saved_comparison_object_bytes(saved)
    entry = registry_module._build_journal_entry(
        sequence=version + 1,
        previous_entry_sha256=head,
        selector_id=registry_module._selector_id(epoch, saved.selection),
        comparison_version=saved.comparison_version,
        object_sha256=hashlib.sha256(content).hexdigest(),
        object_bytes=len(content),
        dependency_heads=fence.heads,
        dependency_fence_kind=DependencyFenceKind.DIRECT_HEAD_REREAD,
    )
    return registry_module._build_recovery_record(
        registry_id=registry_id,
        registry_epoch_sha256=epoch,
        base_state_version=version,
        base_state_head_sha256=head,
        base_journal_bytes=(registry.root / "registry-journal.jsonl").stat().st_size,
        entry=entry,
    )


def test_recovery_fails_closed_on_substitution_and_divergence(registry, fence) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    saved = make_saved(fence.heads, anchor_version=2)
    record = _pending_record(registry, fence, saved)
    # A substituted candidate object is never removed or adopted.
    _write_candidate(registry, record)
    substituted = registry.root / "objects" / f"{record.entry.object_sha256}.json"
    substituted.write_bytes(b"{}")
    substituted.chmod(0o600)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve("saved_comparison_" + "0" * 40, 1, dependency_fence=fence)
    assert substituted.exists()
    substituted.unlink()
    # A tampered recovery record fails closed.
    tampered = registry.root / "publication-candidate.json"
    tampered.write_bytes(tampered.read_bytes().replace(b'"base_state_version":1', b'"base_state_version":0'))
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.list_selectors(dependency_fence=fence)
    # A journal that diverged from the record fails closed.
    _write_candidate(registry, record)
    with (registry.root / "registry-journal.jsonl").open("ab") as handle:
        handle.write(b'{"other":1}\n')
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.list_selectors(dependency_fence=fence)


def test_candidate_fifo_and_symlink_fail_closed(registry, fence) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    candidate = registry.root / "publication-candidate.json"
    os.mkfifo(candidate, 0o600)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.list_selectors(dependency_fence=fence)
    candidate.unlink()
    candidate.symlink_to(registry.root / "registry-metadata.json")
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.list_selectors(dependency_fence=fence)


def test_journal_interrupt_truncates_and_rolls_back(registry, fence, monkeypatch) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    journal = (registry.root / "registry-journal.jsonl").read_bytes()
    original = registry_module._write_all

    def interrupt(descriptor, content):
        if content.startswith(b'{"comparison_version"'):
            os.write(descriptor, content[:20])
            raise KeyboardInterrupt
        original(descriptor, content)

    monkeypatch.setattr(registry_module, "_write_all", interrupt)
    with pytest.raises(KeyboardInterrupt):
        registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    monkeypatch.setattr(registry_module, "_write_all", original)
    assert (registry.root / "registry-journal.jsonl").read_bytes() == journal
    assert len(object_names(registry)) == 1
    assert "publication-candidate.json" not in root_names(registry)
    assert registry.register(
        make_saved(fence.heads, anchor_version=2), dependency_fence=fence
    ).applied


# --- storage attacks ----------------------------------------------------------------


def test_object_tamper_and_extra_files_fail_closed(registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"a', b'"b', 1))
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    path.write_bytes(original[:-1])
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    path.write_bytes(original)
    extra = registry.root / "objects" / ("0" * 64 + ".json")
    extra.write_bytes(b"{}")
    extra.chmod(0o600)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    extra.unlink()
    stray = registry.root / "notes.txt"
    stray.write_bytes(b"x")
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    stray.unlink()
    path.unlink()
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)


def test_a_self_consistent_substituted_object_fails_closed(registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    substitute = saved_comparison_object_bytes(make_saved(fence.heads, replay="d" * 64))
    assert len(substitute) == len(path.read_bytes())
    path.write_bytes(substitute)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.backup_bytes()


def test_links_and_permissions_fail_closed(tmp_path, registry, fence) -> None:
    receipt = registry.register(make_saved(fence.heads), dependency_fence=fence)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    (tmp_path / "hardlink").unlink()
    moved = tmp_path / "moved.json"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    path.unlink()
    moved.rename(path)
    path.chmod(0o644)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    path.chmod(0o600)
    registry.root.chmod(0o755)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
    registry.root.chmod(0o700)
    assert registry.resolve(receipt.selector_id, 1, dependency_fence=fence)


def test_root_journal_and_metadata_replacement_fail_closed(
    tmp_path, registry, fence
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    identity = registry.identity()
    journal = registry.root / "registry-journal.jsonl"
    content = journal.read_bytes()
    journal.unlink()
    journal.write_bytes(content)
    journal.chmod(0o600)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.identity()
    registry.close()
    reopened = reopen_with(registry.root, fence, identity)
    reopened.close()
    metadata = registry.root / "registry-metadata.json"
    metadata.unlink()
    with pytest.raises(LongitudinalComparisonRegistryUnsafe, match="missing"):
        reopen_with(registry.root, fence, identity)
    swapped = tmp_path / "swapped"
    registry.root.rename(swapped)
    fresh = LongitudinalComparisonRegistry(registry.root, dependency_fence=fence)
    assert fresh.identity()[0] != identity[0]
    fresh.close()


def reopen_with(root: Path, fence, identity):
    return LongitudinalComparisonRegistry(
        root,
        dependency_fence=fence,
        expected_registry_id=identity[0],
        expected_registry_epoch_sha256=identity[1],
        expected_state_head_sha256=identity[3],
    )


def test_existing_roots_require_the_retained_identity_and_head(
    registry, fence
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    identity = registry.identity()
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        LongitudinalComparisonRegistry(registry.root, dependency_fence=fence)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        reopen_with(registry.root, fence, (*identity[:3], "0" * 64))
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        reopen_with(registry.root, fence, ("x", *identity[1:]))


def test_journal_tamper_and_truncation_fail_closed(registry, fence) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    journal = registry.root / "registry-journal.jsonl"
    content = journal.read_bytes()
    lines = content.splitlines(keepends=True)
    with journal.open("r+b") as handle:
        handle.truncate(len(content) - 5)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.identity()
    with journal.open("r+b") as handle:
        handle.truncate(0)
        handle.write(lines[1] + lines[0])
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.identity()
    with journal.open("r+b") as handle:
        handle.truncate(0)
        handle.write(lines[0])
    # Dropping a committed tail is a rollback against the observed head.
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.identity()


def test_instance_head_alone_detects_rollback(registry, fence, monkeypatch) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    journal = registry.root / "registry-journal.jsonl"
    first = journal.read_bytes()
    second = registry.register(
        make_saved(fence.heads, anchor_version=2), dependency_fence=fence
    )
    # Isolate the per-instance layer from the process-wide head fence, then
    # roll storage back to a fully consistent older state.
    monkeypatch.setattr(registry_module, "_REGISTRY_PROCESS_HEADS", {})
    with journal.open("r+b") as handle:
        handle.truncate(len(first))
    (registry.root / "objects" / f"{second.object_sha256}.json").unlink()
    with pytest.raises(LongitudinalComparisonRegistryUnsafe, match="rollback"):
        registry.identity()


def test_peer_instances_advance_forward(registry, fence) -> None:
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    peer = reopen(registry, fence)
    try:
        second = peer.register(
            make_saved(fence.heads, anchor_version=2), dependency_fence=fence
        )
        assert registry.identity()[3] == second.state_head_sha256
        assert registry.resolve(first.selector_id, 1, dependency_fence=fence)
    finally:
        peer.close()


def _race_child(root, identity, heads, saved, queue, barrier) -> None:
    fence = FakeFence(heads)
    # Both contenders open at the same retained head, then publish together.
    registry = reopen_with(root, fence, identity)
    barrier.wait(30)
    try:
        receipt = registry.register(saved, dependency_fence=fence)
        queue.put(("ok", receipt.object_sha256, receipt.applied))
    except LongitudinalComparisonRegistryConflict as error:
        queue.put(("conflict", type(error).__name__, None))
    finally:
        registry.close()


def test_two_process_race_publishes_exactly_one_version(registry, fence) -> None:
    identity = registry.identity()
    context = get_context("fork")
    queue = context.Queue()
    barrier = context.Barrier(2)
    contenders = [
        make_saved(fence.heads, replay="1" * 64),
        make_saved(fence.heads, replay="2" * 64),
    ]
    processes = [
        context.Process(
            target=_race_child,
            args=(registry.root, identity, fence.heads, saved, queue, barrier),
        )
        for saved in contenders
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert sorted(item[0] for item in results) == ["conflict", "ok"]
    assert registry.identity()[2] == 1
    assert len(object_names(registry)) == 1


def test_two_process_exact_retry_race_is_idempotent(registry, fence) -> None:
    identity = registry.identity()
    context = get_context("fork")
    queue = context.Queue()
    barrier = context.Barrier(2)
    saved = make_saved(fence.heads)
    processes = [
        context.Process(
            target=_race_child,
            args=(registry.root, identity, fence.heads, saved, queue, barrier),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(30)
    assert sorted(item[2] for item in results) == [False, True]
    assert registry.identity()[2] == 1


# --- backup and restore -------------------------------------------------------------


def test_backup_restore_roundtrip(tmp_path, registry, fence) -> None:
    first = registry.register(make_saved(fence.heads), dependency_fence=fence)
    registry.register(make_saved(fence.heads, version=2, replay="9" * 64), dependency_fence=fence)
    content = registry.backup_bytes()
    header, objects = saved_comparison_backup_from_bytes(content)
    assert header.state_version == 2 and len(objects) == 2
    identity = registry.identity()
    restored = LongitudinalComparisonRegistry.restore(
        tmp_path / "restored",
        content,
        dependency_fence=fence,
        expected_registry_id=identity[0],
        expected_registry_epoch_sha256=identity[1],
        expected_state_head_sha256=identity[3],
    )
    try:
        assert restored.identity() == identity
        original = registry.resolve(first.selector_id, 1, dependency_fence=fence)
        copy = restored.resolve(first.selector_id, 1, dependency_fence=fence)
        assert copy.saved_object_json == original.saved_object_json
        assert stat.S_IMODE(os.stat(restored.root).st_mode) == 0o700
    finally:
        restored.close()


def test_restore_rejects_truncation_tamper_identity_and_targets(
    tmp_path, registry, fence
) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    content = registry.backup_bytes()
    identity = registry.identity()

    def attempt(data, target="restored", fence_value=fence, ident=identity):
        return LongitudinalComparisonRegistry.restore(
            tmp_path / target,
            data,
            dependency_fence=fence_value,
            expected_registry_id=ident[0],
            expected_registry_epoch_sha256=ident[1],
            expected_state_head_sha256=ident[3],
        )

    for data in (
        content[:-1],
        content[:-1] + bytes([content[-1] ^ 1]),
        content + b"x",
        b"",
        content.replace(b"traceback-saved-comparison-backup-v1", b"traceback-saved-comparison-backup-v2"),
    ):
        with pytest.raises(LongitudinalComparisonRegistryConflict):
            attempt(data)
    with pytest.raises(LongitudinalComparisonRegistryConflict):
        attempt(content, ident=(*identity[:3], "0" * 64))
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="other dependency"):
        attempt(content, fence_value=FakeFence())
    (tmp_path / "occupied").mkdir()
    with pytest.raises(LongitudinalComparisonRegistryConflict, match="exists"):
        attempt(content, target="occupied")
    assert not (tmp_path / "restored").exists()


def test_restoring_an_older_backup_is_a_rollback(tmp_path, registry, fence) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    old = registry.backup_bytes()
    old_identity = registry.identity()
    registry.register(make_saved(fence.heads, anchor_version=2), dependency_fence=fence)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe, match="rollback"):
        LongitudinalComparisonRegistry.restore(
            tmp_path / "rolled-back",
            old,
            dependency_fence=fence,
            expected_registry_id=old_identity[0],
            expected_registry_epoch_sha256=old_identity[1],
            expected_state_head_sha256=old_identity[3],
        )
    assert not (tmp_path / "rolled-back").exists()


def test_backup_captures_no_pending_recovery_state(registry, fence) -> None:
    registry.register(make_saved(fence.heads), dependency_fence=fence)
    record = _pending_record(registry, fence, make_saved(fence.heads, anchor_version=2))
    _write_candidate(registry, record)
    content = registry.backup_bytes()
    assert b"publication-candidate" not in content
    assert "publication-candidate.json" not in root_names(registry)
    assert saved_comparison_backup_from_bytes(content)[0].state_version == 1


# --- seals -------------------------------------------------------------------------------


def test_public_method_shadows_and_state_changes_fail_closed(
    registry, fence, monkeypatch
) -> None:
    saved = make_saved(fence.heads)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        object.__setattr__(registry, "register", lambda *_a, **_k: None)
        registry.register(saved, dependency_fence=fence)
    del registry.__dict__["register"]
    trusted = registry._trusted_head_sha256
    registry._trusted_head_sha256 = "0" * 64
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.register(saved, dependency_fence=fence)
    registry._trusted_head_sha256 = trusted
    assert registry.register(saved, dependency_fence=fence).applied


def test_class_and_alias_replacement_fail_closed(registry, fence, monkeypatch) -> None:
    saved = make_saved(fence.heads)
    monkeypatch.setattr(LongitudinalComparisonRegistry, "resolve", lambda *a, **k: None)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.register(saved, dependency_fence=fence)
    monkeypatch.undo()
    monkeypatch.setattr(registry_module, "_CR_RESOLVED", dict)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        registry.register(saved, dependency_fence=fence)


def test_saved_objects_are_immutable_contracts(fence) -> None:
    saved = make_saved(fence.heads)
    with pytest.raises(ValidationError):
        saved.comparison_version = 2  # type: ignore[misc]
    assert json.loads(saved_comparison_object_bytes(saved))["release_export_authorized"] is False
    with pytest.raises(ValidationError):
        SavedLongitudinalComparisonV1.model_validate(
            {**saved.model_dump(), "product_release_authorized": True}
        )
    with pytest.raises(ValidationError):
        SavedLongitudinalComparisonV1.model_validate(
            {**saved.model_dump(), "e06_state_head_sha256": "0" * 64}
        )
    entry_fields = set(SavedComparisonJournalEntryV1.model_fields)
    assert {"sequence", "previous_entry_sha256", "selector_id", "object_sha256"} <= (
        entry_fields
    )


# --- interim live fence over the merged stores ------------------------------------


@pytest.fixture
def live_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import evidence_inspector.reader_authorization_registry as reader_module
    from evidence_inspector.anchor_policy_registry import AnchorPolicyRegistry
    from evidence_inspector.covariate_context_registry import CovariateContextRegistry
    from evidence_inspector.projection_policy_registry import ProjectionPolicyRegistry
    from evidence_inspector.provider_linkage_store import AuthorityTimeSource
    from evidence_inspector.reader_authorization_registry import (
        ReaderAuthorizationProfile,
        ReaderAuthorizationRegistry,
        reader_trust_sha256,
    )
    from evidence_inspector.reader_authorization_synthetic import synthetic_reader_trust
    from evidence_inspector.record_supersession_store import RecordSupersessionStore
    from evidence_inspector.repeatability_comparison_registry import (
        RepeatabilityComparisonRegistry,
    )
    from evidence_inspector.result_trust_registry import ResultTrustRegistry
    from evidence_inspector.result_view_source_registry import ResultViewSourceRegistry
    from tests.test_covariate_context_live import make_live
    from tests.test_provider_linkage_store import _pins

    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    live, closers = make_live(tmp_path / "live")
    try:
        trust = ResultTrustRegistry(tmp_path / "trust")
        closers.append(trust)
        history = RecordSupersessionStore(tmp_path / "d04", linkage_store=live.store)
        closers.append(history)
        reader_trust = synthetic_reader_trust()
        reader = ReaderAuthorizationRegistry.create(
            tmp_path / "reader",
            profile=ReaderAuthorizationProfile.SYNTHETIC,
            configured_trust=reader_trust,
            expected_trust_sha256=reader_trust_sha256(reader_trust),
            time_source=AuthorityTimeSource.fixed(CREATED_AT),
        )
        closers.append(reader)
        sources = ResultViewSourceRegistry(tmp_path / "e06", record_catalog=live.catalog)
        closers.append(sources)
        d07 = RepeatabilityComparisonRegistry(
            tmp_path / "d07",
            linkage_store=live.store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
            result_trust_registry=trust,
        )
        closers.append(d07)
        d10 = CovariateContextRegistry(
            tmp_path / "d10", d09_registry=live.d09, decision_registry=live.d03
        )
        closers.append(d10)
        anchors = AnchorPolicyRegistry(
            tmp_path / "anchors",
            linkage_store=live.store,
            cohort_registry=live.cohort_registry,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )
        closers.append(anchors)
        projections = ProjectionPolicyRegistry(tmp_path / "projections")
        closers.append(projections)
        fence = registry_module.LiveRegistryDependencyFence(
            linkage_store=live.store,
            record_history_store=history,
            cohort_registry=live.cohort_registry,
            reader_registry=reader,
            record_catalog=live.catalog,
            result_catalog=live.values[1],
            result_trust_registry=trust,
            source_registry=sources,
            decision_registry=live.d03,
            comparison_registry=d07,
            d09_registry=live.d09,
            d10_registry=d10,
            anchor_registry=anchors,
            projection_registry=projections,
        )
        yield live, fence, trust
    finally:
        for item in reversed(closers):
            item.close()


def test_live_fence_reads_real_heads_and_detects_a_trust_advance(
    tmp_path, live_fence
) -> None:
    live, fence, trust = live_fence
    scope = registry_module.SavedComparisonDependencyScopeV1(
        cohort_selector_id=live.cohort_selector_id, cohort_version=1
    )
    with fence.hold() as held:
        heads = held.read_heads(scope)
        assert held.read_heads(scope) == heads
        assert held.fence_kind is DependencyFenceKind.DIRECT_HEAD_REREAD
        bindings = held.read_bindings()
    assert heads.family_source is None
    assert heads.d05_cohort.id.startswith("cohort_registry_")
    assert heads.d03_decision.id == live.d03_receipt.registry_id
    assert heads.d09_summary.id == live.d09_receipt.registry_id
    assert bindings == registry_module._bindings_from_heads(heads)
    registry = LongitudinalComparisonRegistry(tmp_path / "saved", dependency_fence=fence)
    try:
        saved = make_saved(heads, cohort=live.cohort_selector_id)
        receipt = registry.register(saved, dependency_fence=fence)
        assert receipt.dependency_fence_kind is DependencyFenceKind.DIRECT_HEAD_REREAD
        current = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
        assert current.authority_state is SavedComparisonAuthorityState.CURRENT
        trust.revoke_key("dev-result-" + "0" * 24)
        stale = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
        assert stale.authority_state is SavedComparisonAuthorityState.STALE
        assert DependencySlot.RESULT_TRUST in stale.stale_dependencies
        assert stale.saved_object_json == current.saved_object_json
    finally:
        registry.close()


def test_live_fence_requires_exact_store_types() -> None:
    names = (
        "linkage_store",
        "record_history_store",
        "cohort_registry",
        "reader_registry",
        "record_catalog",
        "result_catalog",
        "result_trust_registry",
        "source_registry",
        "decision_registry",
        "comparison_registry",
        "d09_registry",
        "d10_registry",
        "anchor_registry",
        "projection_registry",
    )
    with pytest.raises(TypeError):
        registry_module.LiveRegistryDependencyFence(**{name: object() for name in names})
