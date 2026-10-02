"""Protected anchor-policy registry: approval, live candidate page, explicit selection."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import evidence_inspector.anchor_policy_registry as registry_module
from evidence_inspector.anchor_policy_registry import (
    MAX_ANCHOR_CANDIDATES,
    AnchorEligibilityState,
    AnchorPolicyAuthorityState,
    AnchorPolicyRegistry,
    AnchorPolicyRegistryConflict,
    AnchorPolicyRegistryStale,
    AnchorPolicyRegistryUnsafe,
    RegisteredAnchorPolicyObject,
    ResolvedApprovedAnchor,
    anchor_policy_backup_from_bytes,
    registered_anchor_policy_object_from_bytes,
)
from evidence_inspector.cohort_manifest import (
    MemberLineageRole,
    build_cohort_member,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.compatibility import InformationState
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalRecord,
    longitudinal_anchor_policy_sha256,
    longitudinal_record_sha256,
    provider_measurement_id,
    provider_projection_ref,
)
from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.provider_linkage import (
    BiologicalLineage,
    LinkageOperation,
    LinkageReasonCode,
    LinkageRevision,
    OptionalLineageState,
    OptionalOpaqueToken,
    TechnicalLineage,
    UnitOfAnalysis,
    linkage_revision_sha256,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.repeatability_comparison import repeatability_envelope_sha256
from tests.test_cohort_manifest import TIME_AXIS, _authority, _collection_event, _manifest
from tests.test_longitudinal_compatibility import (
    HEAD_SHA256,
    NOW,
    _key,
    _measurement,
    _policy,
)
from tests.test_provider_linkage import (
    PROVIDER,
    _consume,
    _create_approval,
    _known,
    _token,
)
from tests.test_provider_linkage_store import _pins, _store
from tests.test_repeatability_comparison import _envelope
from tests import registry_storage_checks as storage_checks

SUBJECT = _token("subject", "1")
DAY = 86_400


def _unknown() -> OptionalOpaqueToken:
    return OptionalOpaqueToken(state=OptionalLineageState.UNKNOWN, token=None)


def _record(digit: str, *, insufficient: bool = False) -> LongitudinalRecord:
    """A D03 record whose linkage carries the known run lineage D05 requires."""

    measurement = _measurement(digit)
    if insufficient:
        measurement = measurement.model_copy(
            update={"information_state": InformationState.INSUFFICIENT}
        )
    revision = LinkageRevision(
        linkage_id=_token("linkage", digit),
        provider_namespace=PROVIDER,
        revision=1,
        previous_revision_sha256=None,
        operation=LinkageOperation.CREATE,
        reason_code=LinkageReasonCode.INITIAL_PROJECTION,
        source_projection_ref=provider_projection_ref(measurement),
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
        biological=BiologicalLineage(
            subject_token=SUBJECT,
            collection_token=_token("collection", digit),
            specimen_token=_token("specimen", digit),
            aliquot=_unknown(),
        ),
        technical=TechnicalLineage(
            run=_known("run", digit),
            analysis_record_id=_token("analysis", digit),
            measurement_id=provider_measurement_id(measurement),
            reanalysis_of=_unknown(),
        ),
        proposed_at=NOW,
    )
    authorized, _ = _consume(revision, (_create_approval(revision, digit),))
    return LongitudinalRecord(
        measurement=measurement,
        comparison_key=_key(measurement, change_digit=digit),
        linkage_revision=revision,
        authorized_linkage=authorized,
        activation_receipt=None,
    )


class Live:
    def __init__(self, store, cohorts, records, selector_id, manifest) -> None:
        self.store = store
        self.cohorts = cohorts
        self.records = records
        self.selector_id = selector_id
        self.manifest = manifest
        self.manifest_sha256 = cohort_manifest_sha256(manifest)


@pytest.fixture
def live(tmp_path: Path):
    store = _store(tmp_path / "linkage")
    cohorts = None
    try:
        drafts = (
            *(_record(digit) for digit in "1234"),
            _record("6", insufficient=True),
        )
        for draft in drafts:
            assert draft.authorized_linkage is not None
            store.commit_authorized_revision(draft.authorized_linkage)
        snapshot = store.active_snapshot()
        receipts = {item.linkage_id: item for item in snapshot.receipts}
        revisions = {item.linkage_id: item for item in snapshot.revisions}
        records = tuple(
            draft.model_copy(
                update={"activation_receipt": receipts[draft.linkage_revision.linkage_id]}
            )
            for draft in drafts
        )
        # Records 1-3 are cohort members on days 1, 2 and 3; record 4 is live
        # linkage that is not a member of the cohort; record 6 is a day-4 member
        # whose measurement is insufficient.
        members = []
        for day, record in enumerate((*records[:3], records[4]), start=1):
            linkage_id = record.linkage_revision.linkage_id
            members.append(
                build_cohort_member(
                    revision=revisions[linkage_id],
                    receipt=receipts[linkage_id],
                    collection_event=_collection_event(
                        subject_token=SUBJECT,
                        collection_token=record.linkage_revision.biological.collection_token,
                        collected_at=datetime(2026, 9, day, 12, tzinfo=UTC),
                    ),
                    time_axis=TIME_AXIS,
                    lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
                    denominator_contribution=True,
                    unit_of_analysis=UnitOfAnalysis.COLLECTION,
                )
            )
        manifest = _manifest(_authority(snapshot), tuple(members))
        cohorts = CohortRegistry(
            tmp_path / "cohorts",
            linkage_store=store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )
        cohorts.register(manifest)
        selector_id = cohorts.list_selectors().records[0].selector_id
        yield Live(store, cohorts, records, selector_id, manifest)
    finally:
        if cohorts is not None:
            cohorts.close()
        store.close()


def _open(root: Path, live: Live, **overrides) -> AnchorPolicyRegistry:
    values = {
        "linkage_store": live.store,
        "cohort_registry": live.cohorts,
        "expected_trust_snapshot_sha256_by_provider": _pins(),
    }
    values.update(overrides)
    return AnchorPolicyRegistry(root, **values)


@pytest.fixture
def registry(tmp_path: Path, live: Live):
    value = _open(tmp_path / "anchors", live)
    try:
        yield value
    finally:
        value.close()


def _register(
    registry: AnchorPolicyRegistry,
    live: Live,
    *,
    anchor_index: int = 0,
    candidates=None,
    policy=None,
    envelope=None,
    approval_version: int = 1,
    **overrides,
):
    anchor = live.records[anchor_index]
    policy = policy if policy is not None else _policy(anchor)
    envelope = envelope if envelope is not None else _envelope(anchor)
    values = {
        "approval_version": approval_version,
        "expected_cohort_manifest_sha256": live.manifest_sha256,
        "expected_policy_sha256": longitudinal_anchor_policy_sha256(policy),
        "expected_envelope_sha256": repeatability_envelope_sha256(envelope),
        "expected_authority_head_sha256": HEAD_SHA256,
    }
    values.update(overrides)
    return registry.register_policy(
        values.pop("cohort_selector_id", live.selector_id),
        values.pop("cohort_version", 1),
        policy,
        envelope,
        candidates if candidates is not None else (anchor,),
        **values,
    )


def _second_policy(live: Live, index: int):
    anchor = live.records[index]
    return _policy(anchor).model_copy(
        update={"policy_id": f"longpolicy_fragment_{'abcde'[index]}"}
    )


def _advance_linkage(live: Live) -> None:
    extra = _record("5")
    assert extra.authorized_linkage is not None
    live.store.commit_authorized_revision(extra.authorized_linkage)


PRIVATE_FRAGMENTS = (
    "result_",
    "bundle_",
    "provider_",
    "subject_",
    "collection_",
    "specimen_",
    "analysis_",
    "linkage_",
    "run_",
    "cohort_selector_",
    "mth_",
    "longpolicy_",
)


def _assert_private(content: bytes) -> None:
    text = content.decode("utf-8")
    for fragment in PRIVATE_FRAGMENTS:
        assert fragment not in text, fragment


def test_registered_policy_derives_one_live_explicit_candidate_page(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    first = _register(registry, live)
    second = _register(
        registry, live, anchor_index=1, policy=_second_policy(live, 1)
    )
    assert first.selector_id != second.selector_id
    assert first.candidate_count == 1 and first.state_version == 1

    page = registry.derive_candidate_page(first.selector_id, 1)
    assert page.explicit_selection_required is True
    assert page.policy_sha256 == first.policy_sha256
    assert page.envelope_sha256 == first.envelope_sha256
    assert page.anchor_key_sha256 == first.anchor_key_sha256
    assert page.cohort_manifest_sha256 == live.manifest_sha256
    (candidate,) = page.candidates
    assert candidate.biological_timepoint_ordinal == 1
    assert candidate.time_offset_seconds == 0
    assert candidate.method_version == "1.0.0"
    assert candidate.eligibility_state is AnchorEligibilityState.ELIGIBLE

    (other,) = registry.derive_candidate_page(second.selector_id, 1).candidates
    assert other.biological_timepoint_ordinal == 2
    assert other.time_offset_seconds == DAY
    assert other.anchor_selector_id != candidate.anchor_selector_id

    # Repeat derivation is byte-identical: the page is a pure function of the
    # approval plus unchanged live authority.
    assert registry.derive_candidate_page(first.selector_id, 1) == page.model_copy(
        update={"state_version": 2, "state_head_sha256": page.state_head_sha256}
    )


def test_resolution_requires_an_explicit_anchor_selector() -> None:
    parameters = inspect.signature(AnchorPolicyRegistry.resolve_anchor).parameters
    for name in (
        "selector_id",
        "approval_version",
        "anchor_selector_id",
        "expected_candidate_page_sha256",
    ):
        assert parameters[name].default is inspect.Parameter.empty
    assert "anchor_record" not in parameters
    assert "policy" not in parameters


def test_resolve_returns_the_exact_protected_approval_and_anchor(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    (candidate,) = page.candidates
    resolved = registry.resolve_anchor(
        receipt.selector_id,
        1,
        candidate.anchor_selector_id,
        expected_candidate_page_sha256=page.candidate_page_sha256,
    )
    assert type(resolved) is ResolvedApprovedAnchor
    assert resolved.protected_only is True
    assert resolved.anchor_record == live.records[0]
    assert resolved.anchor_record_sha256 == longitudinal_record_sha256(live.records[0])
    assert resolved.policy == _policy(live.records[0])
    assert resolved.envelope == _envelope(live.records[0])
    assert resolved.envelope_sha256 == receipt.envelope_sha256
    assert resolved.policy_sha256 == receipt.policy_sha256
    assert resolved.candidate == candidate
    assert resolved.candidate_page_sha256 == page.candidate_page_sha256
    assert resolved.cohort_selector_id == live.selector_id
    assert resolved.cohort_manifest_sha256 == live.manifest_sha256
    payload = resolved.model_dump()
    with pytest.raises(ValueError, match="anchor key"):
        ResolvedApprovedAnchor.model_validate(
            {
                **payload,
                "anchor_record": live.records[1],
                "anchor_record_sha256": longitudinal_record_sha256(live.records[1]),
            }
        )
    # Re-pointing provenance digests or swapping in another valid policy or
    # envelope no longer validates against the carried candidate page.
    swapped_policy = _policy(live.records[0]).model_copy(update={"version": "1.0.1"})
    swapped_envelope = _envelope(live.records[0], limit=0.05)
    for update in (
        {"candidate_page_sha256": "e" * 64},
        {"cohort_manifest_sha256": "e" * 64},
        {"object_sha256": "e" * 64},
        {
            "policy": swapped_policy,
            "policy_sha256": longitudinal_anchor_policy_sha256(swapped_policy),
        },
        {
            "envelope": swapped_envelope,
            "envelope_sha256": repeatability_envelope_sha256(swapped_envelope),
        },
        {"envelope": _envelope(live.records[0]).model_copy(update={"unit": "unit_other"})},
    ):
        with pytest.raises(ValueError):
            ResolvedApprovedAnchor.model_validate({**payload, **update})


def test_injected_stale_cross_policy_and_cross_registry_selectors_fail_closed(
    registry: AnchorPolicyRegistry, live: Live, tmp_path: Path
) -> None:
    first = _register(registry, live)
    second = _register(registry, live, anchor_index=1, policy=_second_policy(live, 1))
    page = registry.derive_candidate_page(first.selector_id, 1)
    other_page = registry.derive_candidate_page(second.selector_id, 1)
    anchor = page.candidates[0].anchor_selector_id

    def resolve(selector=first.selector_id, version=1, chosen=anchor, digest=None):
        return registry.resolve_anchor(
            selector,
            version,
            chosen,
            expected_candidate_page_sha256=digest or page.candidate_page_sha256,
        )

    assert resolve().anchor_record == live.records[0]
    # Injected or free-form record selectors.
    for injected in (
        "anchor_candidate_" + "0" * 40,
        live.records[0].measurement.result_id,
        live.records[0].linkage_revision.linkage_id,
        longitudinal_record_sha256(live.records[0]),
        "",
        None,
        anchor.upper(),
    ):
        with pytest.raises(AnchorPolicyRegistryConflict):
            resolve(chosen=injected)
    # Selection taken from another policy's page.
    with pytest.raises(AnchorPolicyRegistryConflict, match="not an approved"):
        resolve(chosen=other_page.candidates[0].anchor_selector_id)
    with pytest.raises(AnchorPolicyRegistryStale, match="page changed"):
        resolve(chosen=other_page.candidates[0].anchor_selector_id, digest=other_page.candidate_page_sha256)
    # Unregistered policy version and unknown policy selector.
    with pytest.raises(AnchorPolicyRegistryConflict, match="unavailable"):
        resolve(version=2)
    with pytest.raises(AnchorPolicyRegistryConflict, match="unavailable"):
        resolve(selector="anchor_policy_" + "1" * 40)
    # A page digest the registry did not derive.
    with pytest.raises(AnchorPolicyRegistryStale, match="page changed"):
        resolve(digest="f" * 64)

    # The same approval in another registry has a different epoch, so its
    # selectors are not valid here and ours are not valid there.
    peer = _open(tmp_path / "peer-anchors", live)
    try:
        peer_receipt = _register(peer, live)
        peer_page = peer.derive_candidate_page(peer_receipt.selector_id, 1)
        assert peer_receipt.selector_id != first.selector_id
        with pytest.raises(AnchorPolicyRegistryConflict):
            resolve(
                selector=peer_receipt.selector_id,
                chosen=peer_page.candidates[0].anchor_selector_id,
                digest=peer_page.candidate_page_sha256,
            )
        with pytest.raises(AnchorPolicyRegistryConflict):
            peer.resolve_anchor(
                peer_receipt.selector_id,
                1,
                anchor,
                expected_candidate_page_sha256=peer_page.candidate_page_sha256,
            )
    finally:
        peer.close()


def test_linkage_advance_between_selection_and_use_fails_closed(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    _advance_linkage(live)
    with pytest.raises(AnchorPolicyRegistryStale):
        registry.resolve_anchor(
            receipt.selector_id,
            1,
            page.candidates[0].anchor_selector_id,
            expected_candidate_page_sha256=page.candidate_page_sha256,
        )
    with pytest.raises(AnchorPolicyRegistryStale):
        registry.derive_candidate_page(receipt.selector_id, 1)
    row = registry.list_selectors().records[0]
    assert row.authority_state is AnchorPolicyAuthorityState.STALE
    assert row.candidate_count is None and row.candidate_page_sha256 is None


def test_candidate_page_mutation_never_selects_another_anchor(
    registry: AnchorPolicyRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    chosen = page.candidates[0].anchor_selector_id
    original = registry_module.AnchorPolicyRegistry._derive_candidates_in_fence

    def mutated(self, value, digest):
        (candidate, record), = original(self, value, digest)
        return (
            (
                candidate.model_copy(
                    update={"time_offset_seconds": candidate.time_offset_seconds + 1}
                ),
                record,
            ),
        )

    # The page is derived again inside the fence; a changed page is refused
    # even though the chosen selector is still on it.
    monkeypatch.setattr(registry_module, "_AP_DERIVE_CANDIDATES", mutated)
    sealed = dict(registry_module._REGISTRY_ALIAS_SEAL)
    sealed["_AP_DERIVE_CANDIDATES"] = mutated
    monkeypatch.setattr(
        registry_module, "_REGISTRY_ALIAS_SEAL", registry_module.MappingProxyType(sealed)
    )
    with pytest.raises(AnchorPolicyRegistryStale, match="page changed"):
        registry.resolve_anchor(
            receipt.selector_id,
            1,
            chosen,
            expected_candidate_page_sha256=page.candidate_page_sha256,
        )


def test_result_state_ineligible_candidate_is_listed_but_never_resolves(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live, anchor_index=4, policy=_second_policy(live, 4))
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    (candidate,) = page.candidates
    assert candidate.eligibility_state is AnchorEligibilityState.RESULT_STATE_INELIGIBLE
    assert candidate.biological_timepoint_ordinal == 4
    assert candidate.time_offset_seconds == 3 * DAY
    # A wrong E01 authority-head pin is not a result state: D03 rejects the
    # inputs outright, so the candidate is not admitted at all.
    with pytest.raises(AnchorPolicyRegistryConflict, match="live selection"):
        _register(registry, live, expected_authority_head_sha256="9" * 64)
    row = registry.list_selectors().records[0]
    assert row.candidate_count == 1 and row.eligible_count == 0
    with pytest.raises(AnchorPolicyRegistryConflict, match="not eligible"):
        registry.resolve_anchor(
            receipt.selector_id,
            1,
            candidate.anchor_selector_id,
            expected_candidate_page_sha256=page.candidate_page_sha256,
        )


def test_registration_admits_only_live_policy_anchored_cohort_members(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    anchor, member, _, outsider, _ = live.records
    # A record the policy does not pin as its anchor key.
    with pytest.raises(AnchorPolicyRegistryConflict, match="exact canonical"):
        _register(registry, live, candidates=(member,))
    with pytest.raises(AnchorPolicyRegistryConflict, match="exact canonical"):
        _register(registry, live, candidates=(anchor, member))
    # Live linkage that is not a member of the bound cohort version.
    with pytest.raises(AnchorPolicyRegistryConflict, match="live and admitted|live selection"):
        _register(registry, live, anchor_index=3)
    # A candidate without its activation receipt.
    with pytest.raises(AnchorPolicyRegistryConflict, match="exact canonical"):
        _register(
            registry,
            live,
            candidates=(anchor.model_copy(update={"activation_receipt": None}),),
        )
    # An envelope for another measurement identity.
    wrong = _envelope(anchor).model_copy(update={"unit": "unit_other"})
    with pytest.raises(AnchorPolicyRegistryConflict, match="exact canonical"):
        _register(registry, live, envelope=wrong)
    # Independent pins must equal the supplied contracts.
    for name in (
        "expected_policy_sha256",
        "expected_envelope_sha256",
    ):
        with pytest.raises(AnchorPolicyRegistryConflict):
            _register(registry, live, **{name: "e" * 64})
    with pytest.raises(AnchorPolicyRegistryConflict, match="live selection"):
        _register(registry, live, expected_cohort_manifest_sha256="e" * 64)
    with pytest.raises(AnchorPolicyRegistryConflict, match="live selection"):
        _register(registry, live, cohort_version=2)
    with pytest.raises(AnchorPolicyRegistryConflict, match="selection or pins"):
        _register(registry, live, cohort_selector_id="cohort_selector_x")
    assert registry.list_selectors().state_version == 0
    assert outsider.activation_receipt is not None


def test_candidate_bound_and_exact_tuple_reject_before_any_authority_read(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    # The tuple check precedes capture, pins and every authority read.
    oversized = (live.records[0],) * (MAX_ANCHOR_CANDIDATES + 1)
    for candidates in (oversized, [live.records[0]], ()):
        with pytest.raises(AnchorPolicyRegistryConflict, match="bounded exact tuple"):
            registry.register_policy(
                live.selector_id,
                1,
                _policy(live.records[0]),
                _envelope(live.records[0]),
                candidates,
                approval_version=1,
                expected_cohort_manifest_sha256=live.manifest_sha256,
                expected_policy_sha256="a" * 64,
                expected_envelope_sha256="a" * 64,
                expected_authority_head_sha256=HEAD_SHA256,
            )


def test_approval_versions_are_append_only_contiguous_and_idempotent(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    first = _register(registry, live)
    assert _register(registry, live) == first
    narrow = _envelope(live.records[0], limit=0.05)
    with pytest.raises(AnchorPolicyRegistryConflict, match="already registered"):
        _register(registry, live, envelope=narrow)
    with pytest.raises(AnchorPolicyRegistryConflict, match="extend"):
        _register(registry, live, envelope=narrow, approval_version=3)
    second = _register(registry, live, envelope=narrow, approval_version=2)
    assert second.selector_id == first.selector_id
    assert second.envelope_sha256 != first.envelope_sha256
    one = registry.derive_candidate_page(first.selector_id, 1)
    two = registry.derive_candidate_page(first.selector_id, 2)
    assert one.envelope_sha256 == first.envelope_sha256
    assert two.envelope_sha256 == second.envelope_sha256
    # The same anchor under another approval is a distinct opaque selection.
    assert one.candidates[0].anchor_selector_id != two.candidates[0].anchor_selector_id
    with pytest.raises(AnchorPolicyRegistryConflict, match="not an approved"):
        registry.resolve_anchor(
            first.selector_id,
            2,
            one.candidates[0].anchor_selector_id,
            expected_candidate_page_sha256=two.candidate_page_sha256,
        )


def test_public_pages_are_privacy_safe(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    selectors = registry.list_selectors()
    for content in (
        page.model_dump_json().encode(),
        selectors.model_dump_json().encode(),
        receipt.model_dump_json().encode(),
    ):
        _assert_private(content)
    record = live.records[0]
    seeded = {
        record.measurement.result_id,
        record.measurement.result_sha256,
        record.measurement.bundle_id,
        record.linkage_revision.linkage_id,
        record.linkage_revision.provider_namespace,
        record.linkage_revision.biological.subject_token,
        record.linkage_revision.biological.collection_token,
        record.linkage_revision.biological.specimen_token,
        record.linkage_revision.technical.analysis_record_id,
        live.selector_id,
        live.manifest.members[0].biological_timepoint_id,
        str(live.manifest.members[0].time_coordinate),
    }
    text = page.model_dump_json() + selectors.model_dump_json()
    for value in seeded:
        assert value not in text
    assert set(json.loads(page.candidates[0].model_dump_json())) == {
        "schema_version",
        "anchor_selector_id",
        "alias",
        "biological_timepoint_ordinal",
        "time_offset_seconds",
        "method_version",
        "eligibility_state",
    }


def test_selector_page_reports_live_counts_and_paginates(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    first = _register(registry, live)
    _register(registry, live, envelope=_envelope(live.records[0], limit=0.05), approval_version=2)
    _register(registry, live, anchor_index=2, policy=_second_policy(live, 2))
    whole = registry.list_selectors()
    assert whole.state_version == 3
    assert [row.authority_state for row in whole.records] == [
        AnchorPolicyAuthorityState.CURRENT
    ] * 3
    assert all(row.candidate_count == 1 and row.eligible_count == 1 for row in whole.records)
    keys = [(row.selector_id, row.approval_version) for row in whole.records]
    assert keys == sorted(keys)
    # One row per page crosses every boundary, including between two versions
    # of one selector.
    collected = []
    cursor: dict[str, object] = {}
    while True:
        step = registry.list_selectors(limit=1, **cursor)
        collected.extend(step.records)
        if step.next_after_selector_id is None:
            break
        cursor = {
            "after_selector_id": step.next_after_selector_id,
            "after_approval_version": step.next_after_approval_version,
        }
    assert collected == list(whole.records)
    page = registry.derive_candidate_page(first.selector_id, 1)
    assert whole.records[keys.index((first.selector_id, 1))].candidate_page_sha256 == (
        page.candidate_page_sha256
    )
    for bad in ({"limit": 0}, {"limit": 101}, {"after_selector_id": first.selector_id}):
        with pytest.raises(AnchorPolicyRegistryConflict):
            registry.list_selectors(**bad)


def test_resolve_holds_the_linkage_fence_through_exact_return(
    registry: AnchorPolicyRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    page = registry.derive_candidate_page(receipt.selector_id, 1)
    extra = _record("5")
    started = threading.Event()
    finished = threading.Event()
    workers: list[threading.Thread] = []
    blocked_at_return: list[bool] = []
    original = registry_module._AP_RESOLVED

    def write() -> None:
        started.set()
        live.store.commit_authorized_revision(extra.authorized_linkage)
        finished.set()

    def construct(**values):
        worker = threading.Thread(target=write)
        workers.append(worker)
        worker.start()
        assert started.wait(timeout=1)
        blocked_at_return.append(not finished.wait(timeout=0.1))
        return original(**values)

    sealed = dict(registry_module._REGISTRY_ALIAS_SEAL)
    sealed["_AP_RESOLVED"] = construct
    monkeypatch.setattr(registry_module, "_AP_RESOLVED", construct)
    monkeypatch.setattr(
        registry_module, "_REGISTRY_ALIAS_SEAL", registry_module.MappingProxyType(sealed)
    )
    resolved = registry.resolve_anchor(
        receipt.selector_id,
        1,
        page.candidates[0].anchor_selector_id,
        expected_candidate_page_sha256=page.candidate_page_sha256,
    )
    assert resolved.anchor_record == live.records[0]
    assert blocked_at_return == [True]
    workers[0].join(timeout=5)
    assert finished.is_set()
    monkeypatch.undo()
    with pytest.raises(AnchorPolicyRegistryStale):
        registry.derive_candidate_page(receipt.selector_id, 1)


def test_cohort_registry_reads_compose_in_the_global_fence_order(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    # D05's public read takes the linkage fence itself and cannot nest inside
    # one; the registry therefore uses the already-fenced D05 read.
    with ProviderLinkageStore.authority_read_fence(live.store):
        with pytest.raises(Exception, match="idle connection"):
            live.cohorts.resolve_history(live.selector_id, 1)
    receipt = _register(registry, live)
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(5):
                registry.derive_candidate_page(receipt.selector_id, 1)
                live.cohorts.resolve_history(live.selector_id, 1)
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors and not any(thread.is_alive() for thread in threads)


def test_stored_object_cannot_pair_policy_envelope_or_candidates_freely(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    content = (registry.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    stored = registered_anchor_policy_object_from_bytes(content)
    payload = stored.model_dump(mode="python")
    for update in (
        {"policy": _policy(live.records[1])},
        {"envelope": _envelope(live.records[1], limit=0.2)},
        {"anchor_candidates": (live.records[1],)},
        {"anchor_candidates": (live.records[0], live.records[0])},
        {"policy_sha256": "0" * 64},
    ):
        with pytest.raises(ValueError):
            RegisteredAnchorPolicyObject.model_validate({**payload, **update})


def test_object_tamper_rollback_and_extra_entries_fail_closed(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = path.read_bytes()
    os.chmod(path, 0o600)
    path.write_bytes(original.replace(b'"approval_version":1', b'"approval_version":2'))
    with pytest.raises(AnchorPolicyRegistryUnsafe):
        registry.derive_candidate_page(receipt.selector_id, 1)
    path.write_bytes(original)
    assert registry.derive_candidate_page(receipt.selector_id, 1)
    journal = registry.root / "registry-journal.jsonl"
    committed = journal.read_bytes()
    journal.write_bytes(b"")
    with pytest.raises(AnchorPolicyRegistryUnsafe):
        registry.list_selectors()
    journal.write_bytes(committed)
    (registry.root / "objects" / ("a" * 64 + ".json")).write_bytes(b"{}")
    (registry.root / "objects" / ("b" * 64 + ".json")).write_bytes(b"{}")
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()


def test_reopen_requires_exact_retained_identity_and_head(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    receipt = _register(registry, live)
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="required"):
        _open(registry.root, live)
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="invalid"):
        _open(
            registry.root,
            live,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256="0" * 64,
        )
    reopened = _open(
        registry.root,
        live,
        expected_registry_id=receipt.registry_id,
        expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
        expected_state_head_sha256=receipt.state_head_sha256,
    )
    try:
        page = reopened.derive_candidate_page(receipt.selector_id, 1)
        assert page == registry.derive_candidate_page(receipt.selector_id, 1)
    finally:
        reopened.close()


def test_registry_is_bound_to_its_linkage_store_and_cohort_registry(
    registry: AnchorPolicyRegistry, live: Live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    other_store = _store(tmp_path / "other-linkage")
    other_cohorts = CohortRegistry(
        tmp_path / "other-cohorts",
        linkage_store=other_store,
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        retained = {
            "expected_registry_id": receipt.registry_id,
            "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
            "expected_state_head_sha256": receipt.state_head_sha256,
        }
        with pytest.raises(AnchorPolicyRegistryUnsafe, match="does not match"):
            _open(registry.root, live, cohort_registry=other_cohorts, **retained)
        with pytest.raises(AnchorPolicyRegistryUnsafe, match="authority changed"):
            _open(
                registry.root,
                live,
                linkage_store=other_store,
                cohort_registry=other_cohorts,
                **retained,
            )
        with pytest.raises(TypeError):
            _open(tmp_path / "typed", live, cohort_registry=object())
    finally:
        other_cohorts.close()
        other_store.close()


def _restore_values(live: Live, receipt) -> dict[str, object]:
    return {
        "linkage_store": live.store,
        "cohort_registry": live.cohorts,
        "expected_trust_snapshot_sha256_by_provider": _pins(),
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }


def test_backup_restore_preserves_identity_and_rederives(
    registry: AnchorPolicyRegistry, live: Live, tmp_path: Path
) -> None:
    receipt = _register(registry, live)
    backup = registry.backup_bytes()
    assert anchor_policy_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    restored = AnchorPolicyRegistry.restore(
        tmp_path / "restored", backup, **_restore_values(live, receipt)
    )
    try:
        assert restored.derive_candidate_page(receipt.selector_id, 1) == (
            registry.derive_candidate_page(receipt.selector_id, 1)
        )
    finally:
        restored.close()
    with pytest.raises(AnchorPolicyRegistryConflict, match="already exists"):
        AnchorPolicyRegistry.restore(
            tmp_path / "restored", backup, **_restore_values(live, receipt)
        )
    stale = {**_restore_values(live, receipt), "expected_state_head_sha256": "0" * 64}
    with pytest.raises(AnchorPolicyRegistryConflict, match="expected head"):
        AnchorPolicyRegistry.restore(tmp_path / "other", backup, **stale)
    assert not (tmp_path / "other").exists()


def test_failed_restore_including_final_reopen_removes_its_target(
    registry: AnchorPolicyRegistry,
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

    target = tmp_path / "partial"
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="restore failed"):
        AnchorPolicyRegistry.restore(target, backup, **_restore_values(live, receipt))
    monkeypatch.undo()
    assert not target.exists()

    def failing_construct(*_args, **_kwargs):
        assert (target / "registry-journal.jsonl").exists()
        raise AnchorPolicyRegistryUnsafe("reopen failed")

    monkeypatch.setattr(registry_module, "_AP_CONSTRUCT", failing_construct)
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="reopen failed"):
        AnchorPolicyRegistry.restore(target, backup, **_restore_values(live, receipt))
    monkeypatch.undo()
    assert not target.exists()
    restored = AnchorPolicyRegistry.restore(
        target, backup, **_restore_values(live, receipt)
    )
    restored.close()


def test_torn_journal_append_is_truncated_and_registration_retries(
    registry: AnchorPolicyRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"anchor-policy-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="append failed"):
        _register(registry, live)
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""
    receipt = _register(registry, live)
    assert receipt.state_version == 1
    assert registry.derive_candidate_page(receipt.selector_id, 1)


def test_instance_class_and_pinned_authority_replacement_are_rejected(
    registry: AnchorPolicyRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _register(registry, live)
    instance = object.__getattribute__(registry, "__dict__")
    instance["resolve_anchor"] = lambda *args, **kwargs: None
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="callable changed"):
        registry.resolve_anchor
    del instance["resolve_anchor"]
    original = AnchorPolicyRegistry.derive_candidate_page
    monkeypatch.setattr(AnchorPolicyRegistry, "derive_candidate_page", original)
    monkeypatch.setattr(
        AnchorPolicyRegistry, "_derive_candidates_in_fence", lambda *a, **k: ()
    )
    with pytest.raises(AnchorPolicyRegistryUnsafe, match="callable changed"):
        registry.derive_candidate_page(receipt.selector_id, 1)
    monkeypatch.undo()
    for name in (
        "_PINNED_DECIDE_MEMBER",
        "_PINNED_COHORT_RESOLVE_IN_FENCE",
        "_AP_SELECTOR_ID",
        "_AP_CANDIDATE_PAGE_SHA256",
    ):
        monkeypatch.setattr(registry_module, name, lambda *a, **k: None)
        with pytest.raises(AnchorPolicyRegistryUnsafe, match="authority callable"):
            registry.derive_candidate_page(receipt.selector_id, 1)
        monkeypatch.undo()
    other_store = instance["_cohort_registry"]
    instance["_cohort_registry"] = object()
    try:
        with pytest.raises(AnchorPolicyRegistryUnsafe, match="authority state"):
            registry.derive_candidate_page(receipt.selector_id, 1)
    finally:
        instance["_cohort_registry"] = other_store
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert registry.derive_candidate_page(receipt.selector_id, 1)


def test_backup_parser_rejects_noncanonical_and_reordered_history(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    _register(registry, live)
    backup = registry.backup_bytes()
    with pytest.raises(AnchorPolicyRegistryConflict):
        anchor_policy_backup_from_bytes(backup + b" ")
    with pytest.raises(AnchorPolicyRegistryConflict):
        anchor_policy_backup_from_bytes(b"[" * 100 + b"]" * 100)
    with pytest.raises(AnchorPolicyRegistryConflict):
        anchor_policy_backup_from_bytes(backup.replace(b'"state_version":1', b'"state_version":0'))


def test_d03_rejected_or_non_live_candidates_are_never_admitted(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    anchor = live.records[0]
    # D03 rejects the policy itself while the cohort stays current.
    unsupported = _policy(anchor).model_copy(update={"engine_version": "9.9.9"})
    with pytest.raises(AnchorPolicyRegistryConflict, match="live selection"):
        _register(registry, live, policy=unsupported)
    # A record with the same anchor key under another, never-committed linkage
    # (with a self-consistent but non-live receipt) is not admitted, and one
    # non-admitted candidate rejects the whole approval rather than being
    # dropped.
    assert anchor.activation_receipt is not None
    revision = anchor.linkage_revision.model_copy(
        update={"linkage_id": _token("linkage", "8")}
    )
    alternate, _ = _consume(revision, (_create_approval(revision, "8"),))
    variant = anchor.model_copy(
        update={
            "linkage_revision": revision,
            "authorized_linkage": alternate,
            "activation_receipt": anchor.activation_receipt.model_copy(
                update={
                    "linkage_id": revision.linkage_id,
                    "linkage_revision_sha256": linkage_revision_sha256(revision),
                    "authorized_record_sha256": hashlib.sha256(
                        canonical_contract_bytes(alternate)
                    ).hexdigest(),
                }
            ),
        }
    )
    LongitudinalRecord.model_validate_json(variant.model_dump_json())
    with pytest.raises(AnchorPolicyRegistryConflict, match="live and admitted"):
        _register(registry, live, candidates=(anchor, variant))
    with pytest.raises(AnchorPolicyRegistryConflict, match="live selection"):
        _register(registry, live, candidates=(variant,))
    # Two snapshots of one member's record (here differing only in result
    # state) cannot both be candidates.
    snapshot = anchor.model_copy(
        update={
            "measurement": anchor.measurement.model_copy(
                update={"information_state": InformationState.INSUFFICIENT}
            )
        }
    )
    assert longitudinal_record_sha256(snapshot) != longitudinal_record_sha256(anchor)
    with pytest.raises(AnchorPolicyRegistryConflict, match="exact canonical"):
        _register(registry, live, candidates=(anchor, snapshot))
    assert registry.list_selectors().state_version == 0


def test_policy_selector_is_scoped_to_one_cohort_selection(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    second = _manifest(
        live.manifest.provider_authorities[0],
        live.manifest.members,
        cohort_id=live.manifest.cohort_id,
        version=2,
        previous_manifest_sha256=live.manifest_sha256,
        created_at=live.manifest.created_at + timedelta(seconds=1),
        measurement_anchor=live.manifest.measurement_anchor,
        policies=live.manifest.policies.model_copy(
            update={"missingness_sha256": "b" * 64}
        ),
    )
    vars(live.store)["_time_source"].advance_to(second.created_at)
    live.cohorts.register(second)
    one = _register(registry, live)
    two = _register(
        registry,
        live,
        cohort_version=2,
        expected_cohort_manifest_sha256=cohort_manifest_sha256(second),
    )
    assert one.selector_id != two.selector_id
    page_one = registry.derive_candidate_page(one.selector_id, 1)
    page_two = registry.derive_candidate_page(two.selector_id, 1)
    assert page_two.cohort_manifest_sha256 == cohort_manifest_sha256(second)
    with pytest.raises(AnchorPolicyRegistryConflict, match="not an approved"):
        registry.resolve_anchor(
            two.selector_id,
            1,
            page_one.candidates[0].anchor_selector_id,
            expected_candidate_page_sha256=page_two.candidate_page_sha256,
        )


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    registry: AnchorPolicyRegistry, live: Live
) -> None:
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: _register(registry, live),
        lambda values: _open(registry.root, live, **storage_checks.expected(values)),
        AnchorPolicyRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    registry: AnchorPolicyRegistry, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_append_interrupt_truncates(
        registry, registry_module, lambda: _register(registry, live), monkeypatch
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    registry: AnchorPolicyRegistry, live: Live, tmp_path: Path
) -> None:
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, AnchorPolicyRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    registry: AnchorPolicyRegistry,
    live: Live,
) -> None:
    _register(registry, live)
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: _open(registry.root, live, **storage_checks.expected(values)),
        AnchorPolicyRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    registry: AnchorPolicyRegistry,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_checks.check_interrupted_creation(
        lambda root: _open(root, live),
        lambda root, values: _open(root, live, **storage_checks.expected(values)),
        tmp_path / "created-by-storage-check",
        registry_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    registry: AnchorPolicyRegistry,
    live: Live,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(registry, live)
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: AnchorPolicyRegistry.restore(
            target, backup, **_restore_values(live, values)
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )


def test_storage_creation_under_a_symlinked_parent(
    registry: AnchorPolicyRegistry, live: Live, tmp_path: Path
) -> None:
    storage_checks.check_creation_under_symlinked_parent(
        lambda root: _open(root, live),
        lambda root, values: _open(root, live, **storage_checks.expected(values)),
        tmp_path,
    )
