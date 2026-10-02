"""Composite authority fence: order, capture, revalidation, hold-through-return."""

from __future__ import annotations

import inspect
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

import pytest

import evidence_inspector.composite_authority_fence as fence_module
import evidence_inspector.reader_authorization_registry as reader_module
from evidence_inspector.anchor_policy_registry import AnchorPolicyRegistry
from evidence_inspector.cohort_import import CohortImportError, CohortRecordCatalog
from evidence_inspector.cohort_manifest import (
    MeasurementAnchor,
    MemberLineageRole,
    build_cohort_member,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import (
    CohortRegistry,
    CohortRegistryHead,
    CohortRegistryUnsafe,
)
from evidence_inspector.composite_authority_fence import (
    GLOBAL_LOCK_ORDER,
    CompositeAuthorityCoordinator,
    CompositeAuthorityFence,
    CompositeAuthorityRetry,
    CompositeAuthoritySnapshotV1,
    CompositeAuthorityStale,
    CompositeAuthorityUnsafe,
    CompositeLockStep,
)
from evidence_inspector.covariate_context_registry import CovariateContextRegistry
from evidence_inspector.denominator_policy_registry import DenominatorPolicyRegistry
from evidence_inspector.longitudinal_comparison_registry import (
    _bindings_from_heads as registry_bindings_from_heads,
)
from evidence_inspector.longitudinal_comparison_registry import (
    DependencyFenceKind,
    DependencySlot,
    LongitudinalComparisonRegistry,
    LongitudinalComparisonRegistryError,
    LongitudinalComparisonRegistryStale,
    LongitudinalComparisonRegistryUnsafe,
    SAVED_DEPENDENCY_HEADS_SCHEMA_V2,
    SavedComparisonAuthorityState,
    SavedComparisonDependencyScopeV1,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
)
from evidence_inspector.measurement_source_artifact_registry import (
    MeasurementSourceArtifactRegistry,
)
from evidence_inspector.projection_policy_registry import ProjectionPolicyRegistry
from evidence_inspector.provider_linkage import UnitOfAnalysis
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
)
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
from evidence_inspector.result_catalog import (
    DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    CatalogAliases,
    CatalogConflict,
    CatalogResultRef,
    ResultCatalog,
    catalog_authority_sha256,
    catalog_dependency_head_sha256,
)
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from evidence_inspector.result_view_source_registry import (
    ResultViewSourceRegistry,
    ResultViewSourceRegistryIdentity,
)
from tests.test_bundles import _bundle
from tests.test_cohort_import import _authority as _method_authority
from tests.test_cohort_import import _import as _import_cohort_record
from tests.test_cohort_manifest import TIME_AXIS, _collection_event, _manifest
from tests.test_cohort_manifest import _authority as _provider_authority
from tests.test_cohort_summary import EMPTY_DISPOSITION_POLICY, POLICIES, _policy
from tests.test_covariate_context_live import _shared_record
from tests.test_longitudinal_comparison_registry import CREATED_AT, make_saved
from tests.test_longitudinal_compatibility import HEAD_SHA256, _record
from tests.test_longitudinal_compatibility import _policy as _anchor_policy
from tests.test_projection_policy_registry import (
    _cell_origin,
    _chromosome,
    _fragment,
    _segment,
)
from tests.test_provider_linkage_store import _pins, _store
from tests.test_result_catalog import ALIASES
from tests.test_result_catalog_trust_registry import public_result_key
from evidence_inspector.longitudinal_compatibility import (
    longitudinal_anchor_policy_sha256,
)

JOIN_SECONDS = 120


@dataclass
class World:
    linkage: ProviderLinkageStore
    history: RecordSupersessionStore
    cohort: CohortRegistry
    reader: ReaderAuthorizationRegistry
    records: CohortRecordCatalog
    results: ResultCatalog
    trust: ResultTrustRegistry
    sources: ResultViewSourceRegistry
    d03: LongitudinalDecisionRegistry
    d07: RepeatabilityComparisonRegistry
    d09: DenominatorPolicyRegistry
    d10: CovariateContextRegistry
    family: MeasurementSourceArtifactRegistry
    anchors: AnchorPolicyRegistry
    projections: ProjectionPolicyRegistry
    cohort_selector_id: str
    trust_key_id: str
    root: Path
    # Set when a deadlock leaves threads holding store locks; teardown then
    # abandons the stores instead of blocking on those locks.
    abandoned: bool = False

    @property
    def scope(self) -> SavedComparisonDependencyScopeV1:
        return SavedComparisonDependencyScopeV1(
            cohort_selector_id=self.cohort_selector_id, cohort_version=1
        )

    def coordinator(self) -> CompositeAuthorityCoordinator:
        return CompositeAuthorityCoordinator(**self.store_arguments())

    def store_arguments(self) -> dict[str, object]:
        return {
            "linkage_store": self.linkage,
            "record_history_store": self.history,
            "cohort_registry": self.cohort,
            "reader_registry": self.reader,
            "record_catalog": self.records,
            "result_catalog": self.results,
            "result_trust_registry": self.trust,
            "source_registry": self.sources,
            "decision_registry": self.d03,
            "comparison_registry": self.d07,
            "d09_registry": self.d09,
            "d10_registry": self.d10,
            "family_source_registry": self.family,
            "anchor_registry": self.anchors,
            "projection_registry": self.projections,
        }



def make_world(tmp_path: Path) -> tuple[World, list[object]]:
    """One coherent world: every store bound to one D01, D05, D06, E04, trust.

    The same construction as ``tests.test_covariate_context_live.make_live``,
    except that E04 verifies through a durable ``ResultTrustRegistry`` (the
    only trust authority with a cross-process fence) shared with D07.
    """

    closers: list[object] = []
    store = _store(tmp_path / "protected")
    closers.append(store)
    registry, head, head_sha256, capability = _method_authority()
    import_root = tmp_path / "imports"
    import_root.mkdir(parents=True)
    method = {
        "method_id": capability.method_ref.method_id,
        "version": capability.method_ref.version,
        "method_definition_sha256": capability.method_definition_sha256,
    }
    bundle, key, trust_store = _bundle(import_root / "incoming", method=method)
    probe = ResultCatalog(
        tmp_path / "probe-results",
        import_roots={"root_primary": import_root},
        trust_store=trust_store,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        ref = probe.import_bundle(
            root_id="root_primary",
            relative_path="incoming/record",
            registry=registry,
            authority_head=head,
            expected_authority_head_sha256=head_sha256,
            capability=capability,
            aliases=ALIASES,
        )
    finally:
        probe.close()
    member_record = _shared_record(ref, "2")
    anchor_record = _record("1")
    for record in (anchor_record, member_record):
        store.commit_authorized_revision(record.authorized_linkage)
    snapshot = store.active_snapshot()
    receipts = {item.linkage_id: item for item in snapshot.receipts}
    revisions = {item.linkage_id: item for item in snapshot.revisions}
    anchor_record, member_record = (
        record.model_copy(
            update={"activation_receipt": receipts[record.linkage_revision.linkage_id]}
        )
        for record in (anchor_record, member_record)
    )
    linkage_id = member_record.linkage_revision.linkage_id
    revision = revisions[linkage_id]
    member = build_cohort_member(
        revision=revision,
        receipt=receipts[linkage_id],
        collection_event=_collection_event(
            provider_namespace=revision.provider_namespace,
            subject_token=revision.biological.subject_token,
            collection_token=revision.biological.collection_token,
        ),
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    anchor = MeasurementAnchor(
        measurement_definition_sha256=capability.method_definition_sha256,
        anchor_definition_sha256="9" * 64,
        authority_sha256="a" * 64,
    )
    manifest = _manifest(
        _provider_authority(snapshot), (member,), measurement_anchor=anchor
    ).model_copy(update={"policies": POLICIES})
    trust = ResultTrustRegistry(tmp_path / "trust")
    closers.append(trust)
    trust.add_key(public_result_key(key))
    results = ResultCatalog(
        tmp_path / "results",
        import_roots={"root_primary": import_root},
        result_trust_registry=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(results)
    cohort_registry = CohortRegistry(
        tmp_path / "cohort-registry",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(cohort_registry)
    records = CohortRecordCatalog(
        tmp_path / "cohort-records",
        result_catalog=results,
        linkage_store=store,
        cohort_registry=cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    closers.append(records)
    values = (
        records,
        results,
        manifest,
        member,
        bundle,
        key,
        trust_store,
        registry,
        head,
        head_sha256,
        capability,
        cohort_registry,
    )
    cohort_registry.register(manifest)
    selector = cohort_registry.list_selectors().records[0]
    d09 = DenominatorPolicyRegistry(
        tmp_path / "d09", cohort_registry=cohort_registry, record_catalog=records
    )
    closers.append(d09)
    d03 = LongitudinalDecisionRegistry(
        tmp_path / "d03",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(d03)
    d09.register_policy(
        selector.selector_id,
        selector.cohort_version,
        _policy(
            inclusion_sha256=manifest.policies.inclusion_sha256,
            exclusion_sha256=manifest.policies.exclusion_sha256,
            missingness_sha256=manifest.policies.missingness_sha256,
        ),
        EMPTY_DISPOSITION_POLICY,
        expected_cohort_manifest_sha256=cohort_manifest_sha256(manifest),
    )
    policy = _anchor_policy(anchor_record)
    d03.register_series(
        anchor_record,
        (member_record,),
        policy,
        expected_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        expected_authority_head_sha256=HEAD_SHA256,
    )
    _import_cohort_record(values, selection=(selector.selector_id, 1))
    history = RecordSupersessionStore(tmp_path / "d04", linkage_store=store)
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
    sources = ResultViewSourceRegistry(tmp_path / "e06", record_catalog=records)
    closers.append(sources)
    d07 = RepeatabilityComparisonRegistry(
        tmp_path / "d07",
        linkage_store=store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
        result_trust_registry=trust,
    )
    closers.append(d07)
    d10 = CovariateContextRegistry(tmp_path / "d10", d09_registry=d09, decision_registry=d03)
    closers.append(d10)
    family = MeasurementSourceArtifactRegistry(
        tmp_path / "family", result_view_source_registry=sources
    )
    closers.append(family)
    anchors = AnchorPolicyRegistry(
        tmp_path / "anchors",
        linkage_store=store,
        cohort_registry=cohort_registry,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    closers.append(anchors)
    projections = ProjectionPolicyRegistry(tmp_path / "projections")
    closers.append(projections)
    world = World(
        linkage=store,
        history=history,
        cohort=cohort_registry,
        reader=reader,
        records=records,
        results=results,
        trust=trust,
        sources=sources,
        d03=d03,
        d07=d07,
        d09=d09,
        d10=d10,
        family=family,
        anchors=anchors,
        projections=projections,
        cohort_selector_id=selector.selector_id,
        trust_key_id=key.key_id,
        root=tmp_path,
    )
    return world, closers


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    value, closers = make_world(tmp_path)
    try:
        yield value
    finally:
        if not value.abandoned:
            for item in reversed(closers):
                item.close()


@pytest.fixture
def coordinator(world: World) -> CompositeAuthorityCoordinator:
    return world.coordinator()


def _advance_linkage(world: World, digit: str = "4") -> None:
    world.linkage.commit_authorized_revision(_record(digit).authorized_linkage)


# --- order and capture ---------------------------------------------------------------


def test_global_lock_order_is_the_documented_derived_order() -> None:
    assert GLOBAL_LOCK_ORDER == (
        CompositeLockStep.READER_AUTHORIZATION,
        CompositeLockStep.D10_CONTEXT,
        CompositeLockStep.D09_SUMMARY,
        CompositeLockStep.D01_LINKAGE,
        CompositeLockStep.D04_HISTORY,
        CompositeLockStep.D05_COHORT,
        CompositeLockStep.E04_CATALOG_TRUST,
        CompositeLockStep.D06_RECORD_CATALOG,
        CompositeLockStep.E06_SOURCE,
        CompositeLockStep.D03_DECISION,
        CompositeLockStep.D07_COMPARISON,
        CompositeLockStep.FAMILY_SOURCE,
        CompositeLockStep.ANCHOR_POLICY,
        CompositeLockStep.PROJECTION_POLICY,
    )
    doc = (Path(__file__).parents[1] / "docs" / "COMPOSITE-AUTHORITY-FENCE.md").read_text()
    assert " -> ".join(step.value for step in GLOBAL_LOCK_ORDER) in " ".join(doc.split())


def test_snapshot_matches_every_store_public_read(world, coordinator) -> None:
    snapshot = coordinator.snapshot(world.scope)
    assert type(snapshot) is CompositeAuthoritySnapshotV1
    assert snapshot.fence_kind is DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
    assert snapshot.lock_order == GLOBAL_LOCK_ORDER
    # Each head equals the store's own public read, taken outside the hold.
    heads = snapshot.heads

    def page(value) -> tuple[str, str, str]:
        return (value.registry_id, value.registry_epoch_sha256, value.state_head_sha256)

    def slot(name: str) -> tuple[str, str, str]:
        head = getattr(heads, name)
        return (head.id, head.epoch, head.head)

    linkage = world.linkage.active_snapshot()
    assert slot("d01_linkage") == (
        linkage.store_id,
        linkage.store_epoch_sha256,
        linkage.state_head_sha256,
    )
    history = world.history.active_snapshot()
    assert slot("d04_history") == (
        history.ledger_id,
        history.ledger_epoch_sha256,
        history.state_head_sha256,
    )
    assert slot("d05_cohort") == page(world.cohort.list_selectors(limit=1))
    reader = world.reader.identity()
    assert slot("reader_authorization") == page(reader)
    status = world.records.record_status_for_manifest(world.cohort_selector_id, 1)
    assert slot("d06_record_catalog") == (
        status.registry_id,
        status.registry_epoch_sha256,
        status.status_sha256,
    )
    authority = world.results.authority_snapshot()
    content = world.results.content_snapshot()
    assert heads.schema_version == SAVED_DEPENDENCY_HEADS_SCHEMA_V2
    assert slot("e04_catalog") == (
        "e04_catalog_" + authority.storage_identity_sha256[:32],
        authority.storage_identity_sha256,
        catalog_dependency_head_sha256(authority, content),
    )
    # The v2 E04 head binds content: it is not the v1 authority-only head.
    assert heads.e04_catalog.head != catalog_authority_sha256(authority)
    assert slot("result_trust") == page(world.trust.current_trust())
    assert slot("e06_source") == page(
        world.sources.list_selectors(world.cohort_selector_id, 1, limit=1)
    )
    assert slot("d03_decision") == page(world.d03.list_selectors(limit=1))
    assert slot("d07_comparison") == page(world.d07.list_selectors(limit=1))
    assert slot("d09_summary") == page(world.d09.list_selectors(limit=1))
    assert slot("d10_context") == page(world.d10.list_selectors(limit=1))
    assert slot("family_source") == page(
        world.family.list_selectors(world.cohort_selector_id, 1, limit=1)
    )
    assert heads.family_source.id.startswith("familysrc_registry_")
    assert slot("anchor_policy") == page(world.anchors.list_selectors(limit=1))
    assert slot("projection_policy") == page(world.projections.list_selectors(limit=1))
    identity = world.sources.registry_identity()
    assert snapshot.bindings == registry_bindings_from_heads(heads)
    assert (snapshot.bindings.e06_registry_id, snapshot.bindings.e06_registry_epoch_sha256) == (
        identity.registry_id,
        identity.registry_epoch_sha256,
    )
    assert coordinator.snapshot(world.scope) == snapshot


def _instrument(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Log acquire/capture/revalidate/construct/release events in order."""

    events: list[tuple[str, str]] = []
    phase = {"value": "capture"}
    classes = {
        cls
        for adapter in _ADAPTER_SAMPLE
        for cls in type(adapter).__mro__
        if cls.__module__ == fence_module.__name__ and cls is not fence_module._Adapter
    }
    for cls in classes:
        original_acquire = cls.acquire
        original_capture = cls.capture

        def acquire(self, stack, _original=original_acquire):
            events.append(("acquire", self.step.value))
            _original(self, stack)
            stack.callback(events.append, ("release", self.step.value))

        def capture(self, _original=original_capture):
            events.append((phase["value"], self.step.value))
            return _original(self)

        if "acquire" in cls.__dict__:
            monkeypatch.setattr(cls, "acquire", acquire)
        if "capture" in cls.__dict__:
            monkeypatch.setattr(cls, "capture", capture)
    original_scope = fence_module._RecordCatalogAdapter.capture_scope

    def capture_scope(self, scope):
        events.append((phase["value"], "d06_scope"))
        return original_scope(self, scope)

    monkeypatch.setattr(fence_module._RecordCatalogAdapter, "capture_scope", capture_scope)
    original_revalidate = fence_module._CompositeHold.revalidate

    def revalidate(self):
        phase["value"] = "revalidate"
        try:
            original_revalidate(self)
        finally:
            phase["value"] = "after"

    monkeypatch.setattr(fence_module._CompositeHold, "revalidate", revalidate)
    original_snapshot = fence_module._SNAPSHOT

    def construct(**values):
        events.append(("construct", "snapshot"))
        return original_snapshot(**values)

    monkeypatch.setattr(fence_module, "_SNAPSHOT", construct)
    return events


_ADAPTER_SAMPLE: list[object] = []


def test_hold_acquires_in_order_revalidates_in_reverse_and_releases_after_return(
    world, coordinator, monkeypatch
) -> None:
    _ADAPTER_SAMPLE[:] = coordinator._adapters
    events = _instrument(monkeypatch)
    snapshot = coordinator.snapshot(world.scope)
    order = [step.value for step in GLOBAL_LOCK_ORDER]
    acquires = [step for kind, step in events if kind == "acquire"]
    releases = [step for kind, step in events if kind == "release"]
    assert acquires == order
    assert releases == order[::-1]
    first_release = events.index(("release", order[-1]))
    construct = events.index(("construct", "snapshot"))
    assert max(i for i, event in enumerate(events) if event[0] == "acquire") < construct
    assert construct < first_release
    # The explicit revalidation runs before construction, the hold's final
    # revalidation after it; both in exact reverse order, both before release.
    revalidations = [
        index for index, event in enumerate(events) if event[0] == "revalidate"
    ]
    assert revalidations and max(revalidations) < first_release
    expected_reverse = [
        "d06_scope" if step == "d06_record_catalog" else step for step in order[::-1]
    ]
    first_pass = [events[i][1] for i in revalidations if i < construct]
    second_pass = [events[i][1] for i in revalidations if i > construct]
    assert first_pass == expected_reverse
    assert second_pass == expected_reverse
    captures = [step for kind, step in events if kind == "capture"]
    assert captures == [*order, "d06_scope"]
    assert snapshot.heads.d06_record_catalog.id == snapshot.heads.d05_cohort.id


def test_snapshot_accepts_no_callback_and_captures_inputs_before_any_lock(
    world, coordinator, monkeypatch
) -> None:
    parameters = inspect.signature(CompositeAuthorityCoordinator.snapshot).parameters
    assert tuple(parameters) == ("self", "scope")
    _ADAPTER_SAMPLE[:] = coordinator._adapters
    events = _instrument(monkeypatch)

    class Hooked(SavedComparisonDependencyScopeV1):
        pass

    hooked = Hooked(cohort_selector_id=world.cohort_selector_id, cohort_version=1)
    with pytest.raises(CompositeAuthorityUnsafe):
        coordinator.snapshot(hooked)
    with pytest.raises(CompositeAuthorityUnsafe):
        coordinator.snapshot(lambda: world.scope)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        coordinator.snapshot(world.scope, lambda: None)  # type: ignore[call-arg]
    assert events == []
    assert not hasattr(CompositeAuthorityFence(coordinator), "snapshot")


# --- revalidation failures -----------------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        CompositeLockStep.PROJECTION_POLICY,
        CompositeLockStep.E04_CATALOG_TRUST,
        CompositeLockStep.D01_LINKAGE,
        CompositeLockStep.READER_AUTHORIZATION,
    ],
)
def test_a_head_that_moves_under_the_hold_raises_retry_and_returns_nothing(
    world, coordinator, monkeypatch, step
) -> None:
    adapter = next(item for item in coordinator._adapters if item.step is step)
    original = type(adapter).capture
    calls = {"count": 0}

    def drifting(self):
        heads = original(self)
        if self is adapter:
            calls["count"] += 1
            if calls["count"] > 1:
                slot = next(iter(heads))
                heads[slot] = heads[slot].model_copy(update={"head": secrets.token_hex(32)})
        return heads

    monkeypatch.setattr(type(adapter), "capture", drifting)
    with pytest.raises(CompositeAuthorityRetry):
        coordinator.snapshot(world.scope)
    fence = CompositeAuthorityFence(coordinator)
    calls["count"] = 0
    with pytest.raises(LongitudinalComparisonRegistryStale):
        with fence.hold() as held:
            held.read_heads(world.scope)
    monkeypatch.undo()
    # Every fence was released: writers and a fresh snapshot both proceed.
    world.trust.add_key(_extra_result_key())
    assert coordinator.snapshot(world.scope)


def test_a_moved_scoped_record_status_raises_retry(world, coordinator, monkeypatch) -> None:
    original = fence_module._RecordCatalogAdapter.capture_scope
    calls = {"count": 0}

    def drifting(self, scope):
        head = original(self, scope)
        calls["count"] += 1
        if calls["count"] > 1:
            head = head.model_copy(update={"head": secrets.token_hex(32)})
        return head

    monkeypatch.setattr(fence_module._RecordCatalogAdapter, "capture_scope", drifting)
    with pytest.raises(CompositeAuthorityRetry):
        coordinator.snapshot(world.scope)


def _extra_result_key():
    from traceback_runner.signing import KeyPurpose, generate_development_keypair

    return public_result_key(generate_development_keypair(KeyPurpose.RESULT))


# --- writers block until release -----------------------------------------------------


def _writers(world: World) -> dict[str, object]:
    return {
        "trust_revoke": lambda: world.trust.revoke_key(world.trust_key_id),
        "linkage_commit": lambda: _advance_linkage(world),
        "projection_register": lambda: world.projections.register_policy(_fragment()),
        "cohort_reregister_and_import": lambda: world.cohort.register(
            world.cohort.resolve(world.cohort_selector_id, 1).manifest
        ),
    }


@pytest.mark.parametrize(
    "writer", ["trust_revoke", "linkage_commit", "projection_register"]
)
def test_a_writer_blocks_until_the_hold_releases(world, coordinator, writer) -> None:
    fence = CompositeAuthorityFence(coordinator)
    before = coordinator.snapshot(world.scope).heads
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _writers(world)[writer]()
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=run)
    with fence.hold() as held:
        thread.start()
        assert not done.wait(0.5)
        assert held.read_heads(world.scope) == before
        assert not done.is_set()
    thread.join(JOIN_SECONDS)
    assert not thread.is_alive() and not errors
    try:
        after = coordinator.snapshot(world.scope).heads
    except CompositeAuthorityStale:
        # A D01 advance makes the registered D05 cohort version non-current.
        assert writer == "linkage_commit"
        assert world.linkage.active_snapshot().state_head_sha256 != before.d01_linkage.head
    else:
        assert after != before


def _trust_writer_child(root, identity, key_id, started, queue) -> None:
    # Opening the registry already needs its lock, so it blocks here too.
    started.set()
    trust = ResultTrustRegistry(
        root,
        expected_registry_id=identity[0],
        expected_registry_epoch_sha256=identity[1],
        expected_state_head_sha256=identity[2],
    )
    try:
        trust.revoke_key(key_id)
        queue.put(("ok", time.monotonic()))
    except BaseException as error:  # noqa: BLE001 - reported to the parent
        queue.put(("error", type(error).__name__))
    finally:
        trust.close()


def test_a_writer_in_another_process_blocks_until_the_hold_releases(
    world, coordinator
) -> None:
    current = world.trust.current_trust()
    identity = (
        current.registry_id,
        current.registry_epoch_sha256,
        current.state_head_sha256,
    )
    before = coordinator.snapshot(world.scope).heads
    context = get_context("spawn")
    started = context.Event()
    queue = context.Queue()
    process = context.Process(
        target=_trust_writer_child,
        args=(world.trust.root, identity, world.trust_key_id, started, queue),
    )
    fence = CompositeAuthorityFence(coordinator)
    with fence.hold() as held:
        process.start()
        assert started.wait(JOIN_SECONDS)
        time.sleep(0.5)
        assert queue.empty()
        assert held.read_heads(world.scope) == before
        released_at = time.monotonic()
    result = queue.get(timeout=JOIN_SECONDS)
    process.join(JOIN_SECONDS)
    assert process.exitcode == 0
    assert result[0] == "ok" and result[1] >= released_at
    after = coordinator.snapshot(world.scope).heads
    assert after.result_trust != before.result_trust


# --- E04 catalog content ------------------------------------------------------------

EXTRA_ALIASES = CatalogAliases(
    display_alias="dsp_extraaaa",
    run_alias="rnx_extrabbb",
    timepoint_alias="tpt_extraccc",
)


def _extra_bundle(world: World, token: str) -> str:
    """Build a new bundle (new record ID) signed by a newly trusted key."""

    from traceback_runner.bundles import build_result_bundle
    from traceback_runner.signing import KeyPurpose, generate_development_keypair

    from tests.test_bundles import _measurement, _provenance

    key = generate_development_keypair(KeyPurpose.RESULT)
    world.trust.add_key(public_result_key(key))
    *_, capability = _method_authority()
    build_result_bundle(
        world.root / "imports" / f"extra-{token}" / "record",
        measurement=_measurement(),
        provenance=_provenance(run_token=f"synthetic.run.{token}"),
        method={
            "method_id": capability.method_ref.method_id,
            "version": capability.method_ref.version,
            "method_definition_sha256": capability.method_definition_sha256,
        },
        signing_key=key,
    )
    return f"extra-{token}/record"


def _import_arguments(relative: str, aliases: CatalogAliases | None = None) -> dict:
    registry, head, head_sha256, capability = _method_authority()
    return {
        "root_id": "root_primary",
        "relative_path": relative,
        "registry": registry,
        "authority_head": head,
        "expected_authority_head_sha256": head_sha256,
        "capability": capability,
        "aliases": aliases or EXTRA_ALIASES,
    }


def _e04_writer_child(
    results_root, import_root, trust_root, identity, operation, payload, ready, go, queue
) -> None:
    # Both stores open (and E04 initializes) before the parent's hold.
    trust = ResultTrustRegistry(
        trust_root,
        expected_registry_id=identity[0],
        expected_registry_epoch_sha256=identity[1],
        expected_state_head_sha256=identity[2],
    )
    catalog = ResultCatalog(
        results_root,
        import_roots={"root_primary": import_root},
        result_trust_registry=trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    try:
        ready.set()
        go.wait(JOIN_SECONDS)
        if operation == "import":
            catalog.import_bundle(**_import_arguments(payload))
            outcome = "imported"
        else:
            publication_id, reference_json, scope = payload
            outcome = catalog.recover_pending_publication(
                publication_id=publication_id,
                reference=CatalogResultRef.model_validate_json(reference_json),
                recovery_scope_sha256=scope,
                retain_adopted=False,
            )
        queue.put((outcome, time.monotonic()))
    except BaseException as error:  # noqa: BLE001 - reported to the parent
        queue.put(("error", repr(error)))
    finally:
        catalog.close()
        trust.close()


@pytest.mark.parametrize("operation", ["import", "recover"])
def test_an_e04_writer_in_another_process_blocks_until_the_hold_releases(
    world, coordinator, operation
) -> None:
    """A second process importing into, or recovering, E04 waits for release.

    The child opens its own catalog and trust registry before the hold; its
    write starts during the hold and must commit only after release.  The
    E04 head (authority plus content) is unchanged throughout the hold and
    advances afterwards.
    """

    relative = _extra_bundle(world, "child")
    if operation == "import":
        payload: object = relative
        expected = "imported"
    else:
        scope = secrets.token_hex(32)
        prepared = world.results.prepare_bundle_import(
            **_import_arguments(relative), recovery_scope_sha256=scope
        )
        world.results.stage_prepared_import(prepared)
        payload = (
            prepared.publication_id,
            prepared.reference.model_dump_json(),
            scope,
        )
        expected = "pending_removed"
    current = world.trust.current_trust()
    identity = (
        current.registry_id,
        current.registry_epoch_sha256,
        current.state_head_sha256,
    )
    before = coordinator.snapshot(world.scope).heads
    context = get_context("spawn")
    ready = context.Event()
    go = context.Event()
    queue = context.Queue()
    process = context.Process(
        target=_e04_writer_child,
        args=(
            world.results.root,
            world.root / "imports",
            world.trust.root,
            identity,
            operation,
            payload,
            ready,
            go,
            queue,
        ),
    )
    process.start()
    try:
        assert ready.wait(JOIN_SECONDS)
        fence = CompositeAuthorityFence(coordinator)
        with fence.hold() as held:
            go.set()
            time.sleep(0.75)
            assert queue.empty()
            assert held.read_heads(world.scope) == before
            released_at = time.monotonic()
        result = queue.get(timeout=JOIN_SECONDS)
    finally:
        go.set()
        process.join(JOIN_SECONDS)
    assert process.exitcode == 0
    assert result[0] == expected and result[1] >= released_at
    after = coordinator.snapshot(world.scope).heads
    assert (after.e04_catalog.id, after.e04_catalog.epoch) == (
        before.e04_catalog.id,
        before.e04_catalog.epoch,
    )
    assert after.e04_catalog.head != before.e04_catalog.head
    assert after.result_trust == before.result_trust


def test_e04_content_that_moves_under_the_hold_raises_retry(
    world, coordinator, monkeypatch
) -> None:
    """A catalog row that changes between capture and revalidation is a retry.

    Cooperating writers cannot land under the hold; this simulates a row
    change by writing the SQLite file directly (outside the threat model),
    which only the content head can observe.
    """

    original = fence_module._CompositeHold.revalidate
    calls = {"count": 0}

    def drift(self):
        calls["count"] += 1
        if calls["count"] == 1:
            connection = sqlite3.connect(world.results.database)
            try:
                connection.execute(
                    "INSERT INTO coordinated_results VALUES(?)",
                    ("result_" + secrets.token_hex(20),),
                )
                connection.commit()
            finally:
                connection.close()
        return original(self)

    before = coordinator.snapshot(world.scope).heads
    monkeypatch.setattr(fence_module._CompositeHold, "revalidate", drift)
    with pytest.raises(CompositeAuthorityRetry):
        coordinator.snapshot(world.scope)
    monkeypatch.undo()
    # Every fence was released; the new content is a new E04 head.
    after = coordinator.snapshot(world.scope).heads
    assert after.e04_catalog.head != before.e04_catalog.head
    assert after.result_trust == before.result_trust
    world.trust.add_key(_extra_result_key())


def test_record_status_fence_requires_the_e04_content_lock(world) -> None:
    """The D06 step refuses a trust fence held without the content lock."""

    with world.linkage.authority_read_fence():
        with world.cohort.authority_read_fence():
            with world.results._trust_fence():
                with pytest.raises(CohortImportError, match="result trust fence"):
                    with world.records.record_status_read_fence():
                        pass
            with world.results.trust_authority_fence():
                with world.records.record_status_read_fence():
                    pass


def test_a_shared_e04_content_hold_is_never_upgraded(world) -> None:
    relative = _extra_bundle(world, "upgrade")
    with world.results.trust_authority_fence():
        with pytest.raises(CatalogConflict, match="upgraded"):
            world.results.import_bundle(**_import_arguments(relative))
    assert world.results.import_bundle(**_import_arguments(relative))


# --- saved-comparison fence ----------------------------------------------------------


def test_saved_registry_publishes_and_reopens_under_the_composite_fence(
    world, coordinator
) -> None:
    fence = CompositeAuthorityFence(coordinator)
    heads = coordinator.snapshot(world.scope).heads
    registry = LongitudinalComparisonRegistry(world.root / "saved", dependency_fence=fence)
    try:
        receipt = registry.register(
            make_saved(heads, cohort=world.cohort_selector_id), dependency_fence=fence
        )
        assert receipt.dependency_fence_kind is DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
        current = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
        assert current.authority_state is SavedComparisonAuthorityState.CURRENT
        assert current.publication_fence_kind is DependencyFenceKind.COMPOSITE_AUTHORITY_FENCE
        page = registry.list_selectors(dependency_fence=fence)
        assert page.records[0].authority_state is SavedComparisonAuthorityState.CURRENT
        world.trust.revoke_key(world.trust_key_id)
        stale = registry.resolve(receipt.selector_id, 1, dependency_fence=fence)
        assert stale.authority_state is SavedComparisonAuthorityState.STALE
        assert DependencySlot.RESULT_TRUST in stale.stale_dependencies
        assert DependencySlot.D06_RECORD_CATALOG in stale.stale_dependencies
    finally:
        registry.close()


def test_saved_publication_cannot_be_overtaken_by_a_blocked_writer(
    world, coordinator, monkeypatch
) -> None:
    """A writer that starts during publication lands only after the receipt.

    The writer starts at the first in-fence head read.  The receipt must carry
    the pre-writer heads, and the writer's commit must be later than the
    hold's last revalidation (the last thing before release).
    """

    fence = CompositeAuthorityFence(coordinator)
    heads = coordinator.snapshot(world.scope).heads
    registry = LongitudinalComparisonRegistry(world.root / "saved", dependency_fence=fence)
    times: dict[str, float] = {}
    original_read = fence_module._CompositeHeld.read_heads
    original_revalidate = fence_module._CompositeHold.revalidate
    started: list[threading.Thread] = []

    def writer() -> None:
        world.trust.revoke_key(world.trust_key_id)
        times["committed"] = time.monotonic()

    def read_heads(self, scope):
        if not started:
            thread = threading.Thread(target=writer)
            started.append(thread)
            thread.start()
        return original_read(self, scope)

    def revalidate(self):
        original_revalidate(self)
        times["revalidated"] = time.monotonic()

    monkeypatch.setattr(fence_module._CompositeHeld, "read_heads", read_heads)
    monkeypatch.setattr(fence_module._CompositeHold, "revalidate", revalidate)
    try:
        receipt = registry.register(
            make_saved(heads, cohort=world.cohort_selector_id), dependency_fence=fence
        )
        assert receipt.dependency_heads == heads
    finally:
        registry.close()
    started[0].join(JOIN_SECONDS)
    assert times["committed"] > times["revalidated"]
    monkeypatch.undo()
    assert coordinator.snapshot(world.scope).heads.result_trust != heads.result_trust


# --- deadlock freedom ---------------------------------------------------------------


def test_opposing_mutation_read_and_save_operations_terminate(world, coordinator) -> None:
    """Every store's own composed operation runs against the composite hold."""

    fence = CompositeAuthorityFence(coordinator)
    saved_registry = LongitudinalComparisonRegistry(
        world.root / "saved", dependency_fence=fence
    )
    scope = world.scope
    policies = iter((_fragment(), _cell_origin(), _chromosome(), _segment()))
    digits = iter("456789")
    tolerated = (
        CompositeAuthorityStale,
        LongitudinalComparisonRegistryError,
    )

    def save() -> None:
        heads = coordinator.snapshot(scope).heads
        saved_registry.register(
            make_saved(heads, cohort=world.cohort_selector_id, replay=secrets.token_hex(32)),
            dependency_fence=fence,
        )

    operations = {
        "snapshot": lambda: coordinator.snapshot(scope),
        "save": save,
        "d06_status": lambda: world.records.record_status_for_manifest(
            world.cohort_selector_id, 1
        ),
        "d09_live_summary": lambda: world.d09.list_selectors(),
        "d10_live_context": lambda: world.d10.list_selectors(),
        "e06_page": lambda: world.sources.list_selectors(world.cohort_selector_id, 1),
        "family_page": lambda: world.family.list_selectors(world.cohort_selector_id, 1),
        "d07_page": lambda: world.d07.list_selectors(),
        "d03_page": lambda: world.d03.list_selectors(),
        "anchor_page": lambda: world.anchors.list_selectors(),
        "d05_page": lambda: world.cohort.list_selectors(),
        "d04_history": lambda: world.history.record_history_snapshot(),
        "e04_authority": lambda: world.results.authority_snapshot(),
        "reader_identity": lambda: world.reader.identity(),
        "trust_add": lambda: world.trust.add_key(_extra_result_key()),
        "linkage_commit": lambda: _advance_linkage(world, next(digits)),
        "projection_register": lambda: world.projections.register_policy(next(policies)),
        "e04_content": lambda: world.results.content_snapshot(),
        # A second catalog instance on the same root: its content lock is a
        # separate open file, so it contends with the hold through flock.
        "e04_import_other_instance": lambda: other_catalog.import_bundle(
            **next(extra_imports)
        ),
    }
    extra_imports = iter(
        _import_arguments(
            _extra_bundle(world, f"opposing{index}"),
            CatalogAliases(
                display_alias=f"dsp_opposing{index}",
                run_alias=f"rnx_opposing{index}",
                timepoint_alias=f"tpt_opposing{index}",
            ),
        )
        for index in range(3)
    )
    extra_imports = iter(tuple(extra_imports))
    other_catalog = ResultCatalog(
        world.results.root,
        import_roots={"root_primary": world.root / "imports"},
        result_trust_registry=world.trust,
        reader_registry=DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
    )
    # The D01 advance makes the registered cohort version non-current, so it
    # runs once, after the first composite snapshot and save have succeeded,
    # while the remaining rounds still contend with it.
    rounds = {"trust_add": 2, "linkage_commit": 1, "projection_register": 3}
    first_success = {"snapshot": threading.Event(), "save": threading.Event()}
    barrier = threading.Barrier(len(operations))
    failures: dict[str, BaseException] = {}
    successes: dict[str, int] = {name: 0 for name in operations}

    def run(name: str) -> None:
        barrier.wait(JOIN_SECONDS)
        if name == "linkage_commit":
            for event in first_success.values():
                event.wait(JOIN_SECONDS)
        for _ in range(rounds.get(name, 3)):
            try:
                operations[name]()
                successes[name] += 1
                if name in first_success:
                    first_success[name].set()
            except tolerated:
                pass
            except Exception as error:  # noqa: BLE001 - classified below
                # Concurrent authority advance yields each store's own typed
                # error; anything outside the evidence-inspector error
                # hierarchy is a real failure.
                if not type(error).__module__.startswith("evidence_inspector."):
                    failures[name] = error
                    return

    threads = [
        threading.Thread(target=run, args=(name,), daemon=True) for name in operations
    ]
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + JOIN_SECONDS
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stuck = [thread for thread in threads if thread.is_alive()]
        # Deadlocked daemon threads are abandoned; the fixture's close then
        # fails loudly rather than hanging the run.
        if stuck:
            world.abandoned = True
        assert not stuck, "deadlock"
    finally:
        if not any(thread.is_alive() for thread in threads):
            saved_registry.close()
            other_catalog.close()
    assert not failures, failures
    assert successes["e04_import_other_instance"] == 3
    assert successes["e04_content"] == 3
    assert successes["snapshot"] >= 1 and successes["save"] >= 1
    assert successes["d09_live_summary"] >= 1 and successes["d10_live_context"] >= 1
    assert successes["trust_add"] == 2 and successes["projection_register"] == 3


# --- in-fence public reads and fail-closed checks -----------------------------------


def test_new_public_in_fence_reads_require_their_fences(world) -> None:
    with pytest.raises(CohortRegistryUnsafe, match="read fence is absent"):
        world.cohort.resolve_history_in_fence(world.cohort_selector_id, 1)
    with pytest.raises(CohortRegistryUnsafe, match="read fence is absent"):
        world.cohort.head_in_fence()
    with pytest.raises(CohortRegistryUnsafe, match="held linkage fence"):
        with world.cohort.authority_read_fence():
            pass
    with pytest.raises(CohortImportError):
        with world.records.record_status_read_fence():
            pass
    with pytest.raises(CohortImportError, match="fence is absent"):
        world.records.record_status_in_fence(world.cohort_selector_id, 1)
    page = world.cohort.list_selectors()
    with world.linkage.authority_read_fence():
        with world.cohort.authority_read_fence():
            head = world.cohort.head_in_fence()
            history = world.cohort.resolve_history_in_fence(world.cohort_selector_id, 1)
            with pytest.raises(CohortRegistryUnsafe, match="already held"):
                with world.cohort.authority_read_fence():
                    pass
            # Every public D05 read reopens the linkage fence and is refused.
            with pytest.raises(Exception):
                world.cohort.list_selectors()
            assert world.cohort.head_in_fence() == head
    assert type(head) is CohortRegistryHead
    assert (head.registry_id, head.state_head_sha256) == (
        page.registry_id,
        page.state_head_sha256,
    )
    assert history == world.cohort.resolve_history(world.cohort_selector_id, 1)
    identity = world.sources.registry_identity()
    assert type(identity) is ResultViewSourceRegistryIdentity
    assert (identity.cohort_registry_id, identity.cohort_registry_epoch_sha256) == (
        page.registry_id,
        page.registry_epoch_sha256,
    )


def test_in_fence_record_status_equals_the_public_status(world, coordinator) -> None:
    public = world.records.record_status_for_manifest(world.cohort_selector_id, 1)
    snapshot = coordinator.snapshot(world.scope)
    assert snapshot.heads.d06_record_catalog.head == public.status_sha256


def test_public_store_reads_inside_the_hold_fail_without_breaking_it(
    world, coordinator
) -> None:
    fence = CompositeAuthorityFence(coordinator)
    with fence.hold() as held:
        before = held.read_heads(world.scope)
        for operation in (
            lambda: world.cohort.list_selectors(),
            lambda: world.records.record_status_for_manifest(world.cohort_selector_id, 1),
            lambda: world.trust.current_trust(),
            lambda: world.reader.identity(),
            lambda: coordinator.snapshot(world.scope),
        ):
            with pytest.raises(Exception):
                operation()
        assert held.read_heads(world.scope) == before
    assert coordinator.snapshot(world.scope).heads == before


def test_hold_is_unusable_after_exit_and_on_another_thread(world, coordinator) -> None:
    fence = CompositeAuthorityFence(coordinator)
    with fence.hold() as held:
        errors: list[BaseException] = []
        thread = threading.Thread(
            target=lambda: _capture(errors, lambda: held.read_heads(world.scope))
        )
        thread.start()
        thread.join(JOIN_SECONDS)
        assert type(errors[0]) is LongitudinalComparisonRegistryUnsafe
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        held.read_heads(world.scope)


def _capture(errors: list[BaseException], operation) -> None:
    try:
        operation()
    except BaseException as error:  # noqa: BLE001 - inspected by the caller
        errors.append(error)


def test_construction_requires_exact_and_mutually_bound_stores(world, tmp_path) -> None:
    arguments = world.store_arguments()
    with pytest.raises(TypeError):
        CompositeAuthorityCoordinator(**{name: object() for name in arguments})
    other_trust = ResultTrustRegistry(tmp_path / "other-trust")
    try:
        with pytest.raises(CompositeAuthorityUnsafe, match="not bound"):
            CompositeAuthorityCoordinator(**{**arguments, "result_trust_registry": other_trust})
    finally:
        other_trust.close()
    other_projections = ProjectionPolicyRegistry(tmp_path / "other-projections")
    try:
        assert CompositeAuthorityCoordinator(
            **{**arguments, "projection_registry": other_projections}
        )
    finally:
        other_projections.close()
    with pytest.raises(TypeError):
        CompositeAuthorityFence(object())  # type: ignore[arg-type]


def test_store_class_and_instance_shadows_fail_closed(
    world, coordinator, monkeypatch
) -> None:
    original = ProjectionPolicyRegistry._load_state
    monkeypatch.setattr(
        ProjectionPolicyRegistry,
        "_load_state",
        lambda self, **kwargs: original(self, **kwargs),
    )
    with pytest.raises(CompositeAuthorityUnsafe):
        coordinator.snapshot(world.scope)
    monkeypatch.undo()
    state = object.__getattribute__(world.d03, "__dict__")
    state["_lock"] = lambda *a, **k: None
    try:
        with pytest.raises(CompositeAuthorityUnsafe):
            coordinator.snapshot(world.scope)
    finally:
        del state["_lock"]
    assert coordinator.snapshot(world.scope)


def test_unknown_scope_is_stale_and_releases_every_fence(world, coordinator) -> None:
    unknown = SavedComparisonDependencyScopeV1(
        cohort_selector_id="cohort_selector_" + "f" * 40, cohort_version=1
    )
    with pytest.raises(CompositeAuthorityStale):
        coordinator.snapshot(unknown)
    world.trust.add_key(_extra_result_key())
    assert coordinator.snapshot(world.scope)


def test_store_integrity_failure_is_unsafe_not_stale_and_releases(
    world, coordinator
) -> None:
    extra = world.projections.root / "objects" / "notes.txt"
    extra.write_text("private")
    with pytest.raises(CompositeAuthorityUnsafe):
        coordinator.snapshot(world.scope)
    fence = CompositeAuthorityFence(coordinator)
    with pytest.raises(LongitudinalComparisonRegistryUnsafe):
        with fence.hold():
            pass
    extra.unlink()
    # Every fence was released on the failure path.
    world.trust.add_key(_extra_result_key())
    assert coordinator.snapshot(world.scope)


def test_entry_while_holding_any_store_fence_is_refused(world, coordinator) -> None:
    """The coordinator must be a thread's first lock.

    Entering under E04's public fence would hold E04 while waiting for D01,
    the reverse of D06's own order, and deadlock against a concurrent D06
    status read.  Entry is refused before any acquisition instead.
    """

    for fence in (
        lambda: world.results.trust_authority_fence(),
        lambda: world.linkage.authority_read_fence(),
        lambda: world.reader.authority_read_fence(),
        lambda: world.trust.read_fence(),
    ):
        with fence():
            with pytest.raises(CompositeAuthorityUnsafe, match="before any store fence"):
                coordinator.snapshot(world.scope)
    with world.linkage.authority_read_fence(), world.cohort.authority_read_fence():
        with pytest.raises(CompositeAuthorityUnsafe, match="before any store fence"):
            coordinator.snapshot(world.scope)
    # The scenario itself terminates: a concurrent D06 status read and a
    # thread that tries to enter under E04 both finish.
    errors: list[BaseException] = []
    entered = threading.Event()

    def under_e04() -> None:
        with world.results.trust_authority_fence():
            entered.set()
            time.sleep(0.2)
            _capture(errors, lambda: coordinator.snapshot(world.scope))

    first = threading.Thread(target=under_e04, daemon=True)
    second = threading.Thread(
        target=lambda: (
            entered.wait(JOIN_SECONDS),
            world.records.record_status_for_manifest(world.cohort_selector_id, 1),
        ),
        daemon=True,
    )
    first.start()
    second.start()
    first.join(JOIN_SECONDS)
    second.join(JOIN_SECONDS)
    assert not first.is_alive() and not second.is_alive()
    assert [type(error) for error in errors] == [CompositeAuthorityUnsafe]
    assert coordinator.snapshot(world.scope)
