"""D05 live-store cohort membership, denominator, and replay tests."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.method_registry import canonical_contract_bytes
from evidence_inspector.cohort_manifest import (
    MAX_MEMBERS,
    CohortManifest,
    CohortMember,
    CollectionEventReference,
    MeasurementAnchor,
    MemberLineageRole,
    PolicyDigests,
    ProviderAuthorityReference,
    ProviderDataAuthorityPayload,
    ProviderDataAuthorityPurpose,
    ProviderDataGrantPayload,
    ReanalysisRule,
    SignedProviderDataAuthority,
    SignedProviderDataGrant,
    TechnicalReplicateRule,
    TimeAxis,
    TimeAxisKind,
    TimeOriginReference,
    _domain_sha256,
    _verify_provider_data_authority,
    biological_timepoint_id,
    build_cohort_manifest,
    build_cohort_member,
    cohort_manifest_bytes,
    cohort_manifest_from_bytes,
    cohort_manifest_sha256,
    collection_event_reference_sha256,
    provider_data_authority_payload_bytes,
    provider_data_grant_payload_bytes,
    time_origin_authority_sha256,
    validate_manifest_against_linkage_store,
    validate_manifest_history,
)
from evidence_inspector.provider_linkage import (
    ApprovalPurpose,
    LinkageOperation,
    LinkageReasonCode,
    ProviderRole,
    UnitOfAnalysis,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    AuthorityTimeSource,
    ProviderLinkageStore,
    ProviderLinkageStoreError,
)
from tests.test_provider_linkage import (
    AFTER,
    ISSUER,
    KEY_ID,
    NOW,
    PRIVATE_KEY,
    PROVIDER,
    T0,
    _approval,
    _consume,
    _correction_approvals,
    _create_approval,
    _known,
    _revision,
    _token,
    _trust,
)
from tests.test_provider_linkage_store import _pins, _store

CREATED = NOW
COLLECTED = datetime(2026, 9, 1, 12, tzinfo=UTC)
TIME_AXIS = TimeAxis(
    kind=TimeAxisKind.COLLECTION_TIME,
    definition_sha256="2" * 64,
    unit_sha256="3" * 64,
    origin_authority_sha256="4" * 64,
)


def _known_run_revision(**values):
    run_digit = values.pop("run_digit", "b")
    revision = _revision(**values)
    return revision.model_copy(
        update={
            "technical": revision.technical.model_copy(
                update={"run": _known("run", run_digit)}
            )
        }
    )


def _data_grant(
    trust, seed: str, *, principal_id: str, purposes=None
) -> SignedProviderDataGrant:
    purposes = purposes or tuple(ProviderDataAuthorityPurpose)
    grant_seed = hashlib.sha256(f"grant:{seed}".encode()).hexdigest()
    payload = ProviderDataGrantPayload(
        grant_id=f"approval_{grant_seed[:32]}",
        provider_namespace=trust.provider_namespace,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=principal_id,
        role=ProviderRole.LINKER,
        allowed_purposes=tuple(sorted(purposes, key=str)),
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        nonce=f"nonce_{hashlib.sha256(grant_seed.encode()).hexdigest()[:32]}",
        issued_at=NOW,
        expires_at=AFTER,
    )
    return SignedProviderDataGrant(
        payload=payload,
        signature_base64=base64.b64encode(
            PRIVATE_KEY.sign(provider_data_grant_payload_bytes(payload))
        ).decode("ascii"),
    )


def _collection_event(
    *,
    provider_namespace: str = PROVIDER,
    subject_token: str | None = None,
    collection_token: str | None = None,
    collected_at: datetime = COLLECTED,
    trust=None,
) -> CollectionEventReference:
    subject_token = subject_token or _token("subject", "6")
    collection_token = collection_token or _token("collection", "6")
    target_sha256 = _domain_sha256(
        b"traceback-collection-event-statement-v1",
        {
            "provider_namespace": provider_namespace,
            "subject_token": subject_token,
            "collection_token": collection_token,
            "collected_at": collected_at.isoformat(),
        },
    )
    trust = (trust or _trust()).model_copy(
        update={"provider_namespace": provider_namespace}
    )
    seed = hashlib.sha256(
        f"{provider_namespace}:{subject_token}:{collection_token}".encode()
    ).hexdigest()
    payload = ProviderDataAuthorityPayload(
        authority_id=f"approval_{seed[:32]}",
        provider_namespace=provider_namespace,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=f"principal_{seed[32:64]}",
        role=ProviderRole.LINKER,
        purpose=ProviderDataAuthorityPurpose.COLLECTION_EVENT,
        target_sha256=target_sha256,
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        nonce=f"nonce_{hashlib.sha256(seed.encode()).hexdigest()[:32]}",
        issued_at=NOW,
        expires_at=AFTER,
    )
    authority = SignedProviderDataAuthority(
        grant=_data_grant(trust, seed, principal_id=payload.principal_id),
        payload=payload,
        signature_base64=base64.b64encode(
            PRIVATE_KEY.sign(provider_data_authority_payload_bytes(payload))
        ).decode("ascii"),
    )
    return CollectionEventReference(
        provider_namespace=provider_namespace,
        subject_token=subject_token,
        collection_token=collection_token,
        collected_at=collected_at,
        authority=authority,
    )


def _time_origin(
    *,
    kind: TimeAxisKind,
    origin_time: datetime,
    subject_token: str | None,
    definition_sha256: str = "d" * 64,
    trust=None,
) -> TimeOriginReference:
    target_sha256 = _domain_sha256(
        b"traceback-time-origin-statement-v1",
        {
            "provider_namespace": PROVIDER,
            "subject_token": subject_token,
            "kind": kind.value,
            "origin_time": origin_time.isoformat(),
            "axis_definition_sha256": definition_sha256,
        },
    )
    purpose = (
        ProviderDataAuthorityPurpose.SUBJECT_TIME_ORIGIN
        if kind == TimeAxisKind.SUBJECT_RELATIVE
        else ProviderDataAuthorityPurpose.STUDY_TIME_ORIGIN
    )
    seed = hashlib.sha256(
        f"origin:{kind.value}:{subject_token}:{origin_time.isoformat()}".encode()
    ).hexdigest()
    trust = trust or _trust()
    payload = ProviderDataAuthorityPayload(
        authority_id=f"approval_{seed[:32]}",
        provider_namespace=PROVIDER,
        issuer_id=ISSUER,
        key_id=KEY_ID,
        principal_id=f"principal_{seed[32:64]}",
        role=ProviderRole.LINKER,
        purpose=purpose,
        target_sha256=target_sha256,
        trust_snapshot_id=trust.snapshot_id,
        trust_snapshot_revision=trust.revision,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        nonce=f"nonce_{hashlib.sha256(seed.encode()).hexdigest()[:32]}",
        issued_at=NOW,
        expires_at=AFTER,
    )
    authority = SignedProviderDataAuthority(
        grant=_data_grant(trust, seed, principal_id=payload.principal_id),
        payload=payload,
        signature_base64=base64.b64encode(
            PRIVATE_KEY.sign(provider_data_authority_payload_bytes(payload))
        ).decode("ascii"),
    )
    return TimeOriginReference(
        provider_namespace=PROVIDER,
        subject_token=subject_token,
        kind=kind,
        origin_time=origin_time,
        axis_definition_sha256=definition_sha256,
        authority=authority,
    )


def _resign_authority(authority, **payload_updates):
    payload = authority.payload.model_copy(update=payload_updates)
    return authority.model_copy(
        update={
            "payload": payload,
            "signature_base64": base64.b64encode(
                PRIVATE_KEY.sign(provider_data_authority_payload_bytes(payload))
            ).decode("ascii"),
        }
    )


def _resign_grant(authority, **payload_updates):
    payload = authority.grant.payload.model_copy(update=payload_updates)
    grant = authority.grant.model_copy(
        update={
            "payload": payload,
            "signature_base64": base64.b64encode(
                PRIVATE_KEY.sign(provider_data_grant_payload_bytes(payload))
            ).decode("ascii"),
        }
    )
    return authority.model_copy(update={"grant": grant})


@pytest.fixture
def live(tmp_path: Path):
    store = _store(tmp_path / "protected")
    revision = _known_run_revision()
    record, _ = _consume(revision, (_create_approval(revision, "c"),))
    store.commit_authorized_revision(record)
    snapshot = store.active_snapshot()
    authority = _authority(snapshot)
    event = _collection_event(
        provider_namespace=snapshot.revisions[0].provider_namespace,
        subject_token=snapshot.revisions[0].biological.subject_token,
        collection_token=snapshot.revisions[0].biological.collection_token,
    )
    member = build_cohort_member(
        revision=snapshot.revisions[0],
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    try:
        yield store, snapshot, authority, member
    finally:
        store.close()


def _authority(snapshot, trust=None) -> ProviderAuthorityReference:
    trust = trust or _trust()
    return ProviderAuthorityReference(
        provider_namespace=PROVIDER,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(trust),
        trust_snapshot_json=canonical_contract_bytes(trust).decode("utf-8"),
        store_id=snapshot.store_id,
        store_epoch_sha256=snapshot.store_epoch_sha256,
        storage_identity_sha256=snapshot.storage_identity_sha256,
        trust_pins_sha256=snapshot.trust_pins_sha256,
        state_version=snapshot.state_version,
        state_head_sha256=snapshot.state_head_sha256,
    )


def _events_for_members(members) -> tuple[CollectionEventReference, ...]:
    events = {}
    for member in members:
        key = (
            member.provider_namespace,
            member.subject_token,
            member.collection_token,
        )
        event = _collection_event(
            provider_namespace=member.provider_namespace,
            subject_token=member.subject_token,
            collection_token=member.collection_token,
            collected_at=datetime.fromtimestamp(member.time_coordinate, tz=UTC),
        )
        if key in events and events[key] != event:
            raise ValueError("test fixture assigns conflicting collection events")
        events[key] = event
    return tuple(events[key] for key in sorted(events))


def _validate(
    manifest: CohortManifest,
    store,
    *,
    expected_trust_snapshot_sha256_by_provider,
) -> None:
    validate_manifest_against_linkage_store(
        manifest,
        store,
        expected_trust_snapshot_sha256_by_provider=(
            expected_trust_snapshot_sha256_by_provider
        ),
    )


def _manifest(authority, members, **updates) -> CohortManifest:
    values = _manifest_values(**updates)
    return build_cohort_manifest(
        provider_authorities=(authority,),
        collection_events=_events_for_members(members),
        members=members,
        **values,
    )


def _manifest_values(**updates):
    values = {
        "cohort_id": "cohort_" + "1" * 32,
        "version": 1,
        "previous_manifest_sha256": None,
        "created_at": CREATED,
        "unit_of_analysis": UnitOfAnalysis.COLLECTION,
        "technical_replicate_rule": TechnicalReplicateRule.COLLAPSE,
        "reanalysis_rule": ReanalysisRule.COLLAPSE_TO_SOURCE,
        "time_axis": TIME_AXIS,
        "policies": PolicyDigests(
            inclusion_sha256="5" * 64,
            exclusion_sha256="6" * 64,
            missingness_sha256="7" * 64,
        ),
        "measurement_anchor": MeasurementAnchor(
            measurement_definition_sha256="8" * 64,
            anchor_definition_sha256="9" * 64,
            authority_sha256="a" * 64,
        ),
    }
    values.update(updates)
    return values


def _retime(member: CohortMember, coordinate: int, **updates) -> CohortMember:
    payload = member.model_dump(mode="python")
    payload.update(updates)
    previous_lineage = (
        member.provider_namespace,
        member.subject_token,
        member.collection_token,
    )
    next_lineage = (
        payload["provider_namespace"],
        payload["subject_token"],
        payload["collection_token"],
    )
    if next_lineage == previous_lineage:
        coordinate = member.time_coordinate
    payload["time_coordinate"] = coordinate
    event = _collection_event(
        provider_namespace=payload["provider_namespace"],
        subject_token=payload["subject_token"],
        collection_token=payload["collection_token"],
        collected_at=datetime.fromtimestamp(coordinate, tz=UTC),
    )
    payload["collection_event_sha256"] = updates.get(
        "collection_event_sha256", collection_event_reference_sha256(event)
    )
    payload["biological_timepoint_id"] = biological_timepoint_id(event)
    payload["time_coordinate_sha256"] = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": payload["collection_token"],
            "collection_event_sha256": payload["collection_event_sha256"],
            "biological_timepoint_id": payload["biological_timepoint_id"],
            "time_axis": TIME_AXIS.model_dump(mode="json"),
            "time_coordinate": coordinate,
        },
    )
    return CohortMember.model_validate(payload)


def test_happy_manifest_replays_canonical_bytes_against_live_store(live) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    assert cohort_manifest_from_bytes(cohort_manifest_bytes(manifest)) == manifest
    _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())
    assert manifest.synthetic_only and not manifest.clinical_use_authorized


def test_linkage_proposal_time_never_defines_biological_timepoint(live) -> None:
    _, snapshot, _, original = live
    revision = snapshot.revisions[0]
    event = _collection_event(
        provider_namespace=revision.provider_namespace,
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    later_proposal = revision.model_copy(
        update={"proposed_at": revision.proposed_at + timedelta(days=90)}
    )
    rebuilt = build_cohort_member(
        revision=later_proposal,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    assert rebuilt.time_coordinate == int(COLLECTED.timestamp())
    assert rebuilt.time_coordinate == original.time_coordinate
    assert rebuilt.biological_timepoint_id == original.biological_timepoint_id


def test_sibling_specimens_share_one_collection_timepoint(live) -> None:
    _, _, authority, first = live
    sibling = _retime(
        first,
        first.time_coordinate + 99,
        linkage_id=_token("linkage", "d"),
        specimen_token=_token("specimen", "d"),
        analysis_record_id=_token("analysis", "d"),
        denominator_contribution=False,
    )
    manifest = _manifest(authority, (first, sibling))
    assert {member.biological_timepoint_id for member in manifest.members} == {
        first.biological_timepoint_id
    }
    assert {member.time_coordinate for member in manifest.members} == {
        first.time_coordinate
    }


def test_collection_event_cannot_self_authorize_changed_time(live) -> None:
    store, snapshot, authority, _ = live
    revision = snapshot.revisions[0]
    changed_at = COLLECTED + timedelta(days=777)
    legitimate = _collection_event(
        provider_namespace=revision.provider_namespace,
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    target_sha256 = _domain_sha256(
        b"traceback-collection-event-statement-v1",
        {
            "provider_namespace": revision.provider_namespace,
            "subject_token": revision.biological.subject_token,
            "collection_token": revision.biological.collection_token,
            "collected_at": changed_at.isoformat(),
        },
    )
    forged_authority = legitimate.authority.model_copy(
        update={
            "payload": legitimate.authority.payload.model_copy(
                update={"target_sha256": target_sha256}
            ),
            "signature_base64": base64.b64encode(b"x" * 64).decode("ascii"),
        }
    )
    forged_event = CollectionEventReference(
        provider_namespace=revision.provider_namespace,
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
        collected_at=changed_at,
        authority=forged_authority,
    )
    forged_member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=forged_event,
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    manifest = build_cohort_manifest(
        provider_authorities=(authority,),
        collection_events=(forged_event,),
        members=(forged_member,),
        **_manifest_values(),
    )
    with pytest.raises(ValueError, match="signature"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_study_relative_origin_is_exactly_authorized(live) -> None:
    store, snapshot, authority, _ = live
    revision = snapshot.revisions[0]
    event = _collection_event(
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    legitimate = _time_origin(
        kind=TimeAxisKind.STUDY_RELATIVE,
        origin_time=COLLECTED - timedelta(days=1),
        subject_token=None,
    )
    axis = TimeAxis(
        kind=TimeAxisKind.STUDY_RELATIVE,
        definition_sha256="d" * 64,
        unit_sha256="3" * 64,
        origin_authority_sha256=time_origin_authority_sha256((legitimate,)),
    )
    member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=axis,
        time_origin=legitimate,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    manifest = build_cohort_manifest(
        provider_authorities=(authority,),
        collection_events=(event,),
        time_origins=(legitimate,),
        members=(member,),
        **_manifest_values(time_axis=axis),
    )
    _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())
    assert manifest.members[0].time_coordinate == 86_400

    shifted = _time_origin(
        kind=TimeAxisKind.STUDY_RELATIVE,
        origin_time=legitimate.origin_time - timedelta(days=999),
        subject_token=None,
    )
    shifted = shifted.model_copy(
        update={
            "authority": shifted.authority.model_copy(
                update={
                    "signature_base64": base64.b64encode(b"x" * 64).decode("ascii")
                }
            )
        }
    )
    shifted_axis = axis.model_copy(
        update={"origin_authority_sha256": time_origin_authority_sha256((shifted,))}
    )
    shifted_member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=shifted_axis,
        time_origin=shifted,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    forged = build_cohort_manifest(
        provider_authorities=(authority,),
        collection_events=(event,),
        time_origins=(shifted,),
        members=(shifted_member,),
        **_manifest_values(time_axis=shifted_axis),
    )
    with pytest.raises(ValueError, match="signature"):
        _validate(forged, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_subject_relative_axis_requires_one_authorized_origin_per_subject(live) -> None:
    _, snapshot, authority, _ = live
    first_revision = snapshot.revisions[0]
    second_revision = _known_run_revision(
        linkage_id=_token("linkage", "d"),
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
        source=_token("projection", "d"),
        run_digit="d",
    )
    events = (
        _collection_event(
            subject_token=first_revision.biological.subject_token,
            collection_token=first_revision.biological.collection_token,
        ),
        _collection_event(
            subject_token=second_revision.biological.subject_token,
            collection_token=second_revision.biological.collection_token,
            collected_at=COLLECTED + timedelta(days=3),
        ),
    )
    origins = (
        _time_origin(
            kind=TimeAxisKind.SUBJECT_RELATIVE,
            origin_time=COLLECTED - timedelta(days=1),
            subject_token=first_revision.biological.subject_token,
        ),
        _time_origin(
            kind=TimeAxisKind.SUBJECT_RELATIVE,
            origin_time=COLLECTED + timedelta(days=1),
            subject_token=second_revision.biological.subject_token,
        ),
    )
    axis = TimeAxis(
        kind=TimeAxisKind.SUBJECT_RELATIVE,
        definition_sha256="d" * 64,
        unit_sha256="3" * 64,
        origin_authority_sha256=time_origin_authority_sha256(origins),
    )
    members = tuple(
        build_cohort_member(
            revision=revision,
            receipt=snapshot.receipts[0],
            collection_event=event,
            time_axis=axis,
            time_origin=origin,
            lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
            denominator_contribution=True,
            unit_of_analysis=UnitOfAnalysis.SUBJECT,
        )
        for revision, event, origin in zip(
            (first_revision, second_revision), events, origins, strict=True
        )
    )
    manifest = build_cohort_manifest(
        provider_authorities=(authority,),
        collection_events=events,
        time_origins=origins,
        members=members,
        **_manifest_values(time_axis=axis, unit_of_analysis=UnitOfAnalysis.SUBJECT),
    )
    assert [member.time_coordinate for member in manifest.members] == [86_400, 172_800]
    incomplete_axis = axis.model_copy(
        update={
            "origin_authority_sha256": time_origin_authority_sha256(origins[:1])
        }
    )
    with pytest.raises(ValidationError, match="one origin per subject"):
        build_cohort_manifest(
            provider_authorities=(authority,),
            collection_events=events,
            time_origins=origins[:1],
            members=members,
            **_manifest_values(
                time_axis=incomplete_axis, unit_of_analysis=UnitOfAnalysis.SUBJECT
            ),
        )


@pytest.mark.parametrize(
    ("authority_kind", "window"),
    (
        ("event", {"issued_at": CREATED + timedelta(seconds=1)}),
        ("event", {"issued_at": T0, "expires_at": CREATED}),
        ("event", {"issued_at": T0, "expires_at": CREATED - timedelta(seconds=1)}),
        ("origin", {"issued_at": CREATED + timedelta(seconds=1)}),
        ("origin", {"issued_at": T0, "expires_at": CREATED}),
        ("origin", {"issued_at": T0, "expires_at": CREATED - timedelta(seconds=1)}),
    ),
)
def test_data_authority_window_rejects_future_or_expired_at_protected_now(
    live, authority_kind, window
) -> None:
    store, snapshot, provider_authority, _ = live
    revision = snapshot.revisions[0]
    event = _collection_event(
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    origin = None
    axis = TIME_AXIS
    if authority_kind == "event":
        event = event.model_copy(
            update={"authority": _resign_authority(event.authority, **window)}
        )
    else:
        origin = _time_origin(
            kind=TimeAxisKind.STUDY_RELATIVE,
            origin_time=COLLECTED - timedelta(days=1),
            subject_token=None,
        )
        origin = origin.model_copy(
            update={"authority": _resign_authority(origin.authority, **window)}
        )
        axis = TimeAxis(
            kind=TimeAxisKind.STUDY_RELATIVE,
            definition_sha256="d" * 64,
            unit_sha256="3" * 64,
            origin_authority_sha256=time_origin_authority_sha256((origin,)),
        )
    member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=axis,
        time_origin=origin,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    manifest = build_cohort_manifest(
        provider_authorities=(provider_authority,),
        collection_events=(event,),
        time_origins=(() if origin is None else (origin,)),
        members=(member,),
        **_manifest_values(time_axis=axis),
    )
    with pytest.raises(ValueError, match="window"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def _short_lived_manifest(live, *, expire_grant: bool):
    store, snapshot, provider_authority, _ = live
    revision = snapshot.revisions[0]
    event = _collection_event(
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    expires_at = NOW + timedelta(minutes=5)
    authority = _resign_authority(event.authority, expires_at=expires_at)
    if expire_grant:
        authority = _resign_grant(authority, expires_at=expires_at)
    event = event.model_copy(update={"authority": authority})
    member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    return store, build_cohort_manifest(
        provider_authorities=(provider_authority,),
        collection_events=(event,),
        members=(member,),
        **_manifest_values(),
    )


def _manifest_with_data_authority_window(
    live,
    authority_kind: str,
    *,
    issued_at: datetime = NOW,
    grant_issued_at: datetime | None = None,
    expires_at: datetime = AFTER,
):
    store, snapshot, provider_authority, _ = live
    revision = snapshot.revisions[0]
    event = _collection_event(
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
    )
    origin = None
    axis = TIME_AXIS
    if authority_kind == "event":
        authority = _resign_grant(
            event.authority,
            issued_at=(issued_at if grant_issued_at is None else grant_issued_at),
        )
        authority = _resign_authority(
            authority, issued_at=issued_at, expires_at=expires_at
        )
        event = event.model_copy(update={"authority": authority})
    else:
        kind = (
            TimeAxisKind.SUBJECT_RELATIVE
            if authority_kind == "subject_origin"
            else TimeAxisKind.STUDY_RELATIVE
        )
        subject_token = (
            revision.biological.subject_token
            if kind == TimeAxisKind.SUBJECT_RELATIVE
            else None
        )
        origin = _time_origin(
            kind=kind,
            origin_time=COLLECTED - timedelta(days=1),
            subject_token=subject_token,
        )
        authority = _resign_grant(
            origin.authority,
            issued_at=(issued_at if grant_issued_at is None else grant_issued_at),
        )
        authority = _resign_authority(
            authority, issued_at=issued_at, expires_at=expires_at
        )
        origin = origin.model_copy(update={"authority": authority})
        axis = TimeAxis(
            kind=kind,
            definition_sha256="d" * 64,
            unit_sha256="3" * 64,
            origin_authority_sha256=time_origin_authority_sha256((origin,)),
        )
    member = build_cohort_member(
        revision=revision,
        receipt=snapshot.receipts[0],
        collection_event=event,
        time_axis=axis,
        time_origin=origin,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    manifest = build_cohort_manifest(
        provider_authorities=(provider_authority,),
        collection_events=(event,),
        time_origins=(() if origin is None else (origin,)),
        members=(member,),
        **_manifest_values(time_axis=axis),
    )
    return store, manifest


@pytest.mark.parametrize(
    "authority_kind", ("event", "subject_origin", "study_origin")
)
@pytest.mark.parametrize("authority_part", ("proof", "grant"))
def test_data_authority_must_exist_when_manifest_is_created(
    live, authority_kind: str, authority_part: str
) -> None:
    future = NOW + timedelta(minutes=5)
    store, manifest = _manifest_with_data_authority_window(
        live,
        authority_kind,
        issued_at=future,
        grant_issued_at=(NOW if authority_part == "proof" else future),
    )
    source = vars(store)["_time_source"]
    assert type(source) is AuthorityTimeSource
    source.advance_to(NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="window"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


@pytest.mark.parametrize(
    "authority_kind", ("event", "subject_origin", "study_origin")
)
def test_clock_rollback_cannot_revive_expired_data_authority(
    live, authority_kind: str
) -> None:
    store, manifest = _manifest_with_data_authority_window(
        live,
        authority_kind,
        expires_at=NOW + timedelta(minutes=5),
    )
    source = vars(store)["_time_source"]
    assert type(source) is AuthorityTimeSource
    source.advance_to(NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="window"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())

    # Fixed authority time is the documented deterministic clock seam. Simulate
    # an underlying system-clock rollback without calling its monotonic API.
    source._current = NOW  # type: ignore[attr-defined]
    with pytest.raises(ProviderLinkageStoreError, match="moved backwards"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_live_authority_time_rejects_expired_event_proof(live) -> None:
    store, manifest = _short_lived_manifest(live, expire_grant=False)
    source = vars(store)["_time_source"]
    assert type(source) is AuthorityTimeSource
    source.advance_to(NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="authority window"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_live_authority_time_rejects_expired_data_grant(live) -> None:
    store, manifest = _short_lived_manifest(live, expire_grant=True)
    source = vars(store)["_time_source"]
    assert type(source) is AuthorityTimeSource
    source.advance_to(NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="grant window"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


@pytest.mark.parametrize(
    "denied_purpose",
    tuple(ProviderDataAuthorityPurpose),
)
def test_create_linkage_grant_does_not_authorize_data_purpose(
    denied_purpose: ProviderDataAuthorityPurpose,
) -> None:
    trust = _trust()
    if denied_purpose == ProviderDataAuthorityPurpose.COLLECTION_EVENT:
        authority = _collection_event(trust=trust).authority
    else:
        kind = (
            TimeAxisKind.SUBJECT_RELATIVE
            if denied_purpose == ProviderDataAuthorityPurpose.SUBJECT_TIME_ORIGIN
            else TimeAxisKind.STUDY_RELATIVE
        )
        authority = _time_origin(
            kind=kind,
            origin_time=COLLECTED - timedelta(days=1),
            subject_token=(
                _token("subject", "6")
                if kind == TimeAxisKind.SUBJECT_RELATIVE
                else None
            ),
            trust=trust,
        ).authority
    other_purpose = next(
        purpose
        for purpose in ProviderDataAuthorityPurpose
        if purpose != denied_purpose
    )
    authority = authority.model_copy(
        update={
            "grant": _data_grant(
                trust,
                f"denied:{denied_purpose.value}",
                principal_id=authority.payload.principal_id,
                purposes=(other_purpose,),
            )
        }
    )
    with pytest.raises(ValueError, match="purpose"):
        _verify_provider_data_authority(
            authority,
            trust,
            manifest_created_at=CREATED,
            evaluated_at=CREATED,
        )


@pytest.mark.parametrize("purpose", tuple(ProviderDataAuthorityPurpose))
@pytest.mark.parametrize("mutation", ("principal", "retroactive"))
def test_data_grant_must_bind_principal_and_precede_proof(
    purpose: ProviderDataAuthorityPurpose,
    mutation: str,
) -> None:
    trust = _trust()
    if purpose == ProviderDataAuthorityPurpose.COLLECTION_EVENT:
        authority = _collection_event(trust=trust).authority
    else:
        kind = (
            TimeAxisKind.SUBJECT_RELATIVE
            if purpose == ProviderDataAuthorityPurpose.SUBJECT_TIME_ORIGIN
            else TimeAxisKind.STUDY_RELATIVE
        )
        authority = _time_origin(
            kind=kind,
            origin_time=COLLECTED - timedelta(days=1),
            subject_token=(
                _token("subject", "6")
                if kind == TimeAxisKind.SUBJECT_RELATIVE
                else None
            ),
            trust=trust,
        ).authority
    if mutation == "principal":
        authority = _resign_grant(
            authority,
            principal_id=_token("principal", "f"),
        )
        expected = "bind authority"
        evaluated_at = NOW
    else:
        authority = _resign_grant(
            authority,
            issued_at=NOW + timedelta(minutes=5),
        )
        expected = "window"
        evaluated_at = NOW + timedelta(minutes=10)
    with pytest.raises(ValueError, match=expected):
        _verify_provider_data_authority(
            authority,
            trust,
            manifest_created_at=CREATED,
            evaluated_at=evaluated_at,
        )


def test_technical_records_cannot_contribute_denominator(live) -> None:
    _, _, authority, draw = live
    replicate = _retime(
        draw,
        draw.time_coordinate,
        linkage_id=_token("linkage", "d"),
        analysis_record_id=_token("analysis", "d"),
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        technical_replicate_of=draw.analysis_record_id,
        denominator_contribution=True,
    )
    with pytest.raises(ValidationError, match="cannot contribute denominators"):
        _manifest(authority, (draw, replicate))


def test_legacy_v1_timestamp_semantics_are_explicitly_historical(live) -> None:
    _, _, authority, member = live
    current = cohort_manifest_bytes(_manifest(authority, (member,)))
    legacy = current.replace(
        b"traceback.cohort-manifest.v2", b"traceback.cohort-manifest.v1", 1
    )
    with pytest.raises(ValueError, match="historical-only"):
        cohort_manifest_from_bytes(legacy)


def test_live_validation_rejects_caller_timezone_without_hooks(live) -> None:
    store, _, authority, member = live
    calls = 0

    class CallerTimezone(tzinfo):
        def utcoffset(self, dt):
            nonlocal calls
            calls += 1
            return timedelta(0)

        def dst(self, dt):
            nonlocal calls
            calls += 1
            return timedelta(0)

        def tzname(self, dt):
            nonlocal calls
            calls += 1
            return "UTC"

    hostile = datetime(2026, 9, 1, 12, tzinfo=CallerTimezone())
    base = _manifest(authority, (member,))
    hostile_event = base.collection_events[0].model_copy(
        update={"collected_at": hostile}
    )
    hostile_origin = _time_origin(
        kind=TimeAxisKind.STUDY_RELATIVE,
        origin_time=COLLECTED,
        subject_token=None,
    ).model_copy(update={"origin_time": hostile})
    mutations = (
        base.model_copy(update={"created_at": hostile}),
        base.model_copy(update={"collection_events": (hostile_event,)}),
        base.model_copy(update={"time_origins": (hostile_origin,)}),
    )
    for manifest in mutations:
        with pytest.raises(ValueError, match="approved UTC timezone"):
            _validate(
                manifest, store, expected_trust_snapshot_sha256_by_provider=_pins()
            )
        with pytest.raises(ValueError, match="approved UTC timezone"):
            validate_manifest_history((manifest,))
    assert calls == 0


def test_graph_preflight_rejects_cycles_and_oversized_fields(live) -> None:
    _, _, authority, member = live
    base = _manifest(authority, (member,))
    cyclic = base.model_copy()
    object.__getattribute__(cyclic, "__dict__")["members"] = (cyclic,)
    with pytest.raises(ValueError, match="cycle"):
        cohort_manifest_bytes(cyclic)

    oversized = base.model_copy(update={"members": (member,) * (MAX_MEMBERS + 1)})
    with pytest.raises(ValueError, match="tuple is oversized"):
        cohort_manifest_bytes(oversized)
    with pytest.raises(ValueError, match="tuple is oversized"):
        validate_manifest_history((oversized,))


def test_closed_store_and_detached_snapshot_cannot_authorize(live) -> None:
    store, snapshot, authority, member = live
    manifest = _manifest(authority, (member,))
    store.close()
    with pytest.raises(ProviderLinkageStoreError):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())
    assert snapshot.revisions  # detached data alone has no validation entry point


def test_state_advance_invalidates_manifest_authority_and_receipt(live) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    revision = _known_run_revision(
        linkage_id=_token("linkage", "d"),
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
        analysis=_token("analysis", "d"),
        measurement=_token("measurement", "d"),
        source=_token("projection", "d"),
        run_digit="d",
    )
    record, _ = _consume(revision, (_create_approval(revision, "d"),))
    store.commit_authorized_revision(record)
    with pytest.raises(ValueError, match="exact live store"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_cross_store_authority_and_receipt_substitution_rejects(
    live, tmp_path: Path
) -> None:
    _, _, authority, member = live
    other = _store(tmp_path / "other")
    revision = _known_run_revision()
    record, _ = _consume(revision, (_create_approval(revision, "c"),))
    other.commit_authorized_revision(record)
    try:
        with pytest.raises(ValueError, match="exact live store"):
            _validate(
                _manifest(authority, (member,)),
                other,
                expected_trust_snapshot_sha256_by_provider=_pins(),
            )
    finally:
        other.close()


def test_caller_arbitrary_trust_pin_digest_cannot_authorize(live) -> None:
    store, _, authority, member = live
    arbitrary = {PROVIDER: "f" * 64}
    forged = authority.model_copy(update={"trust_snapshot_sha256": "f" * 64})
    with pytest.raises(ValueError, match="exact trust snapshot"):
        _validate(
            _manifest(forged, (member,)),
            store,
            expected_trust_snapshot_sha256_by_provider=arbitrary,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("subject_token", _token("subject", "d"), "lineage"),
        ("linkage_revision_sha256", "f" * 64, "digest"),
        ("committed_receipt_sha256", "f" * 64, "receipt"),
        ("collection_event_sha256", "f" * 64, "collection event"),
    ),
)
def test_wrong_lineage_version_receipt_or_time_source_fails(
    live, field, value, message
) -> None:
    store, _, authority, member = live
    changed = _retime(member, member.time_coordinate, **{field: value})
    with pytest.raises(ValueError, match=message):
        _validate(
            _manifest(authority, (changed,)),
            store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )


def test_subject_and_collection_units_allow_lower_level_longitudinal_draws(
    live,
) -> None:
    _, _, authority, first = live
    second = _retime(
        first,
        200,
        linkage_id=_token("linkage", "d"),
        collection_token=_token("collection", "d"),
        specimen_token=_token("specimen", "d"),
        analysis_record_id=_token("analysis", "d"),
        denominator_contribution=False,
        analysis_unit_token=first.subject_token,
    )
    subject_first = _retime(first, 100, analysis_unit_token=first.subject_token)
    subject_manifest = _manifest(
        authority, (second, subject_first), unit_of_analysis=UnitOfAnalysis.SUBJECT
    )
    assert [m.time_coordinate for m in subject_manifest.members] == [
        200,
        first.time_coordinate,
    ]
    sibling = _retime(
        first,
        200,
        linkage_id=_token("linkage", "e"),
        specimen_token=_token("specimen", "e"),
        analysis_record_id=_token("analysis", "e"),
        denominator_contribution=False,
    )
    _manifest(authority, (first, sibling))


def test_same_biological_lineage_cannot_be_two_draws(live) -> None:
    _, _, authority, member = live
    duplicate = _retime(
        member,
        200,
        linkage_id=_token("linkage", "d"),
        analysis_record_id=_token("analysis", "d"),
        denominator_contribution=False,
    )
    with pytest.raises(ValidationError, match="technical reruns"):
        _manifest(authority, (member, duplicate))


def test_replicate_and_reanalysis_rules_are_separate(live) -> None:
    _, _, authority, draw = live
    replicate = _retime(
        draw,
        110,
        linkage_id=_token("linkage", "d"),
        analysis_record_id=_token("analysis", "d"),
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        technical_replicate_of=draw.analysis_record_id,
        denominator_contribution=False,
    )
    reanalysis = _retime(
        draw,
        120,
        linkage_id=_token("linkage", "e"),
        analysis_record_id=_token("analysis", "e"),
        reanalysis_of=draw.analysis_record_id,
        lineage_role=MemberLineageRole.REANALYSIS,
        denominator_contribution=False,
    )
    _manifest(authority, (draw, replicate, reanalysis))
    with pytest.raises(ValidationError, match="technical replicate policy"):
        _manifest(
            authority,
            (draw, replicate),
            technical_replicate_rule=TechnicalReplicateRule.EXCLUDE,
        )
    with pytest.raises(ValidationError, match="reanalysis policy"):
        _manifest(
            authority,
            (draw, reanalysis),
            reanalysis_rule=ReanalysisRule.EXCLUDE,
        )


def test_reanalysis_cycle_and_unknown_source_reject(live) -> None:
    _, _, authority, draw = live
    a_id, b_id = _token("analysis", "d"), _token("analysis", "e")
    a = _retime(
        draw,
        110,
        linkage_id=_token("linkage", "d"),
        analysis_record_id=a_id,
        reanalysis_of=b_id,
        lineage_role=MemberLineageRole.REANALYSIS,
        denominator_contribution=False,
    )
    b = _retime(
        draw,
        120,
        linkage_id=_token("linkage", "e"),
        analysis_record_id=b_id,
        reanalysis_of=a_id,
        lineage_role=MemberLineageRole.REANALYSIS,
        denominator_contribution=False,
    )
    with pytest.raises(
        ValidationError, match="combined analysis dependency graph contains a cycle"
    ):
        _manifest(authority, (draw, a, b))
    unknown = _retime(a, 110, reanalysis_of=_token("analysis", "f"))
    with pytest.raises(ValidationError, match="outside"):
        _manifest(authority, (draw, unknown))
    technical_a = _retime(
        draw,
        130,
        linkage_id=_token("linkage", "a"),
        analysis_record_id=a_id,
        technical_replicate_of=b_id,
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        denominator_contribution=False,
    )
    technical_b = _retime(
        draw,
        140,
        linkage_id=_token("linkage", "b"),
        analysis_record_id=b_id,
        technical_replicate_of=a_id,
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        denominator_contribution=False,
    )
    with pytest.raises(
        ValidationError, match="combined analysis dependency graph contains a cycle"
    ):
        _manifest(authority, (draw, technical_a, technical_b))


def test_unknown_run_lineage_rejected_before_membership(live) -> None:
    _, snapshot, _, _ = live
    revision = snapshot.revisions[0].model_copy(
        update={
            "technical": snapshot.revisions[0].technical.model_copy(
                update={
                    "run": snapshot.revisions[0].technical.run.model_copy(
                        update={"state": "unknown", "token": None}
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="known run"):
        build_cohort_member(
            revision=revision,
            receipt=snapshot.receipts[0],
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


def test_time_coordinate_tamper_and_noncanonical_order_reject(live) -> None:
    _, _, authority, member = live
    tampered = member.model_copy(update={"time_coordinate": 999})
    with pytest.raises(ValidationError, match="timepoint"):
        _manifest(authority, (tampered,))
    later = _retime(
        member,
        member.time_coordinate + 100,
        linkage_id=_token("linkage", "d"),
        collection_token=_token("collection", "d"),
        specimen_token=_token("specimen", "d"),
        analysis_record_id=_token("analysis", "d"),
        analysis_unit_token=_token("collection", "d"),
    )
    canonical = _manifest(authority, (later, member))
    assert canonical.members == (member, later)
    with pytest.raises(ValidationError, match="canonical longitudinal"):
        CohortManifest.model_validate(
            {**canonical.model_dump(mode="python"), "members": (later, member)}
        )


def test_history_requires_substantive_change_monotonic_time_and_exact_chain(
    live,
) -> None:
    _, _, authority, member = live
    first = _manifest(authority, (member,))
    base = {
        "version": 2,
        "previous_manifest_sha256": cohort_manifest_sha256(first),
        "created_at": CREATED + timedelta(days=1),
    }
    authority_only = _manifest(
        authority.model_copy(update={"state_head_sha256": "f" * 64}), (member,), **base
    )
    with pytest.raises(ValueError, match="membership or cohort policy"):
        validate_manifest_history((first, authority_only))
    changed = _manifest(
        authority,
        (member,),
        **base,
        policies=first.policies.model_copy(update={"missingness_sha256": "b" * 64}),
    )
    validate_manifest_history((first, changed))
    with pytest.raises(ValueError, match="increase strictly"):
        validate_manifest_history(
            (first, changed.model_copy(update={"created_at": CREATED}))
        )
    with pytest.raises(ValueError, match="predecessor"):
        validate_manifest_history(
            (first, changed.model_copy(update={"previous_manifest_sha256": "f" * 64}))
        )
    with pytest.raises(ValueError, match="consecutive"):
        validate_manifest_history((first, first.model_copy()))


def test_canonical_parser_rejects_whitespace_duplicate_keys_and_extras(live) -> None:
    _, _, authority, member = live
    content = cohort_manifest_bytes(_manifest(authority, (member,)))
    assert cohort_manifest_from_bytes(content)
    with pytest.raises(ValueError, match="not canonical"):
        cohort_manifest_from_bytes(b" " + content)
    payload = json.loads(content)
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="not canonical"):
        cohort_manifest_from_bytes(json.dumps(payload, separators=(",", ":")).encode())
    duplicate = content[:-1] + b',"version":1}'
    with pytest.raises(ValueError, match="not canonical"):
        cohort_manifest_from_bytes(duplicate)


def test_privacy_free_text_duplicate_members_and_boundaries_reject(live) -> None:
    _, _, authority, member = live
    with pytest.raises(ValidationError):
        CohortMember.model_validate(
            {**member.model_dump(mode="python"), "subject_token": "patient Jane Doe"}
        )
    with pytest.raises(ValidationError, match="canonical longitudinal"):
        CohortManifest.model_validate(
            {
                **_manifest(authority, (member,)).model_dump(mode="python"),
                "members": (member, member),
            }
        )
    with pytest.raises(ValidationError):
        _manifest(authority, ())
    with pytest.raises(ValidationError, match="whole-second"):
        _manifest(authority, (member,), created_at=CREATED.replace(microsecond=1))


def test_correction_then_tombstone_invalidates_prior_manifest(live) -> None:
    store, snapshot, authority, member = live
    manifest = _manifest(authority, (member,))
    previous = snapshot.revisions[0]
    correction_revision = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=previous,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    ).model_copy(update={"technical": previous.technical})
    correction, _ = _consume(
        correction_revision,
        _correction_approvals(correction_revision),
        previous=previous,
    )
    store.commit_authorized_revision(correction)
    with pytest.raises(ValueError, match="exact live store"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())
    corrected_snapshot = store.active_snapshot()
    corrected_authority = _authority(corrected_snapshot)
    corrected_member = build_cohort_member(
        revision=corrected_snapshot.revisions[0],
        receipt=corrected_snapshot.receipts[0],
        collection_event=_collection_event(
            provider_namespace=correction_revision.provider_namespace,
            subject_token=correction_revision.biological.subject_token,
            collection_token=correction_revision.biological.collection_token,
        ),
        time_axis=TIME_AXIS,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    corrected_manifest = _manifest(corrected_authority, (corrected_member,))
    tombstone_revision = _revision(
        revision=3,
        operation=LinkageOperation.TOMBSTONE,
        reason=LinkageReasonCode.RETENTION_TOMBSTONE,
        previous=correction_revision,
        subject=correction_revision.biological.subject_token,
        collection=correction_revision.biological.collection_token,
        specimen=correction_revision.biological.specimen_token,
    ).model_copy(update={"technical": correction_revision.technical})
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
    store.commit_authorized_revision(tombstone)
    with pytest.raises(ValueError, match="exact live store"):
        _validate(
            corrected_manifest,
            store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )


def test_public_commit_cannot_cross_the_manifest_validation_fence(live) -> None:
    store, snapshot, authority, member = live
    manifest = _manifest(authority, (member,))
    previous = snapshot.revisions[0]
    correction_revision = _revision(
        revision=2,
        operation=LinkageOperation.CORRECT,
        reason=LinkageReasonCode.WRONG_SUBJECT,
        previous=previous,
        subject=_token("subject", "d"),
        collection=_token("collection", "d"),
        specimen=_token("specimen", "d"),
    ).model_copy(update={"technical": previous.technical})
    correction, _ = _consume(
        correction_revision,
        _correction_approvals(correction_revision),
        previous=previous,
    )
    trigger = threading.Event()
    commit_started = threading.Event()
    commit_finished = threading.Event()

    class RacingPins(Mapping[str, str]):
        def __iter__(self):
            return iter(_pins())

        def __len__(self):
            return len(_pins())

        def __getitem__(self, key: str) -> str:
            trigger.set()
            assert commit_started.wait(timeout=2)
            assert not commit_finished.wait(timeout=0.05)
            return _pins()[key]

    def commit_correction() -> None:
        assert trigger.wait(timeout=2)
        commit_started.set()
        store.commit_authorized_revision(correction)
        commit_finished.set()

    thread = threading.Thread(target=commit_correction)
    thread.start()
    _validate(
        manifest,
        store,
        expected_trust_snapshot_sha256_by_provider=RacingPins(),
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert commit_finished.is_set()
    assert store.active_snapshot().state_version == 2


def test_live_store_boundary_rejects_fake_subclass_and_instance_shadow(
    live, tmp_path: Path
) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    with pytest.raises(TypeError, match="exact live linkage store type"):
        _validate(
            manifest,
            object(),
            expected_trust_snapshot_sha256_by_provider=_pins(),  # type: ignore[arg-type]
        )

    class DerivedStore(ProviderLinkageStore):
        pass

    derived = DerivedStore(
        tmp_path / "derived",
        expected_trust_snapshot_sha256_by_provider=_pins(),
    )
    try:
        with pytest.raises(TypeError, match="exact live linkage store type"):
            _validate(
                manifest, derived, expected_trust_snapshot_sha256_by_provider=_pins()
            )
    finally:
        derived.close()

    store.active_snapshot = lambda: None  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="callable was shadowed"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())
    del store.active_snapshot


def test_live_store_boundary_rejects_class_callable_shadow(live, monkeypatch) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    monkeypatch.setattr(ProviderLinkageStore, "active_snapshot", lambda self: None)
    with pytest.raises(TypeError, match="callable was shadowed"):
        _validate(manifest, store, expected_trust_snapshot_sha256_by_provider=_pins())


def test_live_store_trust_pins_are_captured_once_without_mapping_views(live) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    provider, digest = next(iter(_pins().items()))

    class OneShotPins(Mapping[str, str]):
        def __init__(self) -> None:
            self.iterations = 0
            self.lookups = 0

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("mapping was reiterated")
            yield provider

        def __len__(self) -> int:
            raise AssertionError("length must not be consulted")

        def __getitem__(self, key: str) -> str:
            assert key == provider
            self.lookups += 1
            if self.lookups > 1:
                raise AssertionError("value was looked up twice")
            return digest

        def items(self):
            raise AssertionError("items view must not be used")

    pins = OneShotPins()
    validate_manifest_against_linkage_store(
        manifest,
        store,
        expected_trust_snapshot_sha256_by_provider=pins,
    )
    assert pins.iterations == pins.lookups == 1


def test_live_store_trust_pin_capture_rejects_duplicate_before_second_lookup(
    live,
) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    provider, digest = next(iter(_pins().items()))

    class DuplicatePins(Mapping[str, str]):
        lookups = 0

        def __iter__(self):
            yield provider
            yield provider

        def __len__(self) -> int:
            return 2

        def __getitem__(self, key: str) -> str:
            self.lookups += 1
            return digest

    pins = DuplicatePins()
    with pytest.raises(ValueError, match="invalid"):
        validate_manifest_against_linkage_store(
            manifest,
            store,
            expected_trust_snapshot_sha256_by_provider=pins,
        )
    assert pins.lookups == 1


def test_live_boundary_revalidates_model_copy_mutations(live) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    mutations = (
        manifest.model_copy(
            update={
                "members": (
                    member.model_copy(update={"denominator_contribution": False}),
                )
            }
        ),
        manifest.model_copy(
            update={
                "members": (
                    member.model_copy(
                        update={"analysis_unit_token": member.subject_token}
                    ),
                )
            }
        ),
        manifest.model_copy(
            update={
                "members": (
                    member.model_copy(
                        update={"lineage_role": MemberLineageRole.REANALYSIS}
                    ),
                )
            }
        ),
        manifest.model_copy(
            update={
                "provider_authorities": (
                    authority.model_copy(update={"state_head_sha256": "f" * 64}),
                )
            }
        ),
        manifest.model_copy(
            update={
                "members": (
                    member.model_copy(update={"linkage_revision_sha256": "f" * 64}),
                )
            }
        ),
    )
    for mutated in mutations:
        with pytest.raises((ValueError, ValidationError)):
            _validate(
                mutated, store, expected_trust_snapshot_sha256_by_provider=_pins()
            )


def test_history_boundary_revalidates_model_copy_mutations(live) -> None:
    _, _, authority, member = live
    first = _manifest(authority, (member,))
    invalid_first = first.model_copy(
        update={
            "members": (member.model_copy(update={"denominator_contribution": False}),)
        }
    )
    with pytest.raises(ValueError):
        validate_manifest_history((invalid_first,))


def test_mixed_replicate_reanalysis_dependency_cycle_rejects(live) -> None:
    _, _, authority, draw = live
    a_id, b_id = _token("analysis", "a"), _token("analysis", "b")
    technical = _retime(
        draw,
        210,
        linkage_id=_token("linkage", "a"),
        analysis_record_id=a_id,
        technical_replicate_of=b_id,
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        denominator_contribution=False,
    )
    reanalysis = _retime(
        draw,
        220,
        linkage_id=_token("linkage", "b"),
        analysis_record_id=b_id,
        reanalysis_of=a_id,
        lineage_role=MemberLineageRole.REANALYSIS,
        denominator_contribution=False,
    )
    with pytest.raises(
        ValidationError, match="combined analysis dependency graph contains a cycle"
    ):
        _manifest(authority, (draw, technical, reanalysis))


def _oversized_manifest_variants(
    manifest: CohortManifest,
) -> tuple[CohortManifest, ...]:
    member = manifest.members[0]
    oversized_time = 10**1000
    time_digest = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": member.collection_token,
            "collection_event_sha256": member.collection_event_sha256,
            "biological_timepoint_id": member.biological_timepoint_id,
            "time_axis": manifest.time_axis.model_dump(mode="json"),
            "time_coordinate": oversized_time,
        },
    )
    return (
        manifest.model_copy(
            update={
                "provider_authorities": (
                    manifest.provider_authorities[0].model_copy(
                        update={"state_version": 10**1000}
                    ),
                )
            }
        ),
        manifest.model_copy(
            update={
                "members": (member.model_copy(update={"linkage_revision": 10**1000}),)
            }
        ),
        manifest.model_copy(
            update={
                "members": (
                    member.model_copy(
                        update={
                            "time_coordinate": oversized_time,
                            "time_coordinate_sha256": time_digest,
                        }
                    ),
                )
            }
        ),
    )


def test_integer_boundaries_accept_documented_maxima(live) -> None:
    from evidence_inspector.cohort_manifest import (
        MAX_TIME_COORDINATE,
        MIN_TIME_COORDINATE,
    )
    from evidence_inspector.provider_linkage import MAX_REVISIONS

    _, _, authority, member = live
    ProviderAuthorityReference.model_validate(
        {**authority.model_dump(mode="python"), "state_version": MAX_REVISIONS}
    )
    CohortMember.model_validate(
        {**member.model_dump(mode="python"), "linkage_revision": MAX_REVISIONS}
    )
    for coordinate in (MIN_TIME_COORDINATE, MAX_TIME_COORDINATE):
        _retime(member, coordinate)


@pytest.mark.parametrize("variant_index", range(3))
def test_canonical_parser_rejects_oversized_integer_domains(
    live, variant_index: int
) -> None:
    _, _, authority, member = live
    variant = _oversized_manifest_variants(_manifest(authority, (member,)))[
        variant_index
    ]
    payload = variant.model_dump(mode="json")
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    with pytest.raises(ValueError, match="not canonical"):
        cohort_manifest_from_bytes(encoded)


@pytest.mark.parametrize("variant_index", range(3))
def test_live_boundary_rejects_oversized_integer_model_copies(
    live, variant_index: int
) -> None:
    store, _, authority, member = live
    variant = _oversized_manifest_variants(_manifest(authority, (member,)))[
        variant_index
    ]
    with pytest.raises(ValueError, match="not canonical"):
        _validate(variant, store, expected_trust_snapshot_sha256_by_provider=_pins())


@pytest.mark.parametrize("variant_index", range(3))
def test_history_boundary_rejects_oversized_integer_model_copies(
    live, variant_index: int
) -> None:
    _, _, authority, member = live
    variant = _oversized_manifest_variants(_manifest(authority, (member,)))[
        variant_index
    ]
    with pytest.raises(ValueError, match="not canonical"):
        validate_manifest_history((variant,))


def _second_provider_authority(
    authority: ProviderAuthorityReference,
) -> ProviderAuthorityReference:
    provider = _token("provider", "d")
    trust = _trust().model_copy(update={"provider_namespace": provider})
    return authority.model_copy(
        update={
            "provider_namespace": provider,
            "trust_snapshot_sha256": provider_trust_snapshot_sha256(trust),
            "trust_snapshot_json": canonical_contract_bytes(trust).decode("utf-8"),
        }
    )


def _multi_provider_manifest(
    authority: ProviderAuthorityReference,
    members: tuple[CohortMember, ...],
) -> CohortManifest:
    first_provider_member = next(
        item
        for item in members
        if item.provider_namespace == authority.provider_namespace
    )
    base = _manifest(authority, (first_provider_member,))
    second = _second_provider_authority(authority)
    return build_cohort_manifest(
        provider_authorities=(authority, second),
        collection_events=_events_for_members(members),
        members=members,
        cohort_id=base.cohort_id,
        version=base.version,
        previous_manifest_sha256=base.previous_manifest_sha256,
        created_at=base.created_at,
        unit_of_analysis=base.unit_of_analysis,
        technical_replicate_rule=base.technical_replicate_rule,
        reanalysis_rule=base.reanalysis_rule,
        time_axis=base.time_axis,
        policies=base.policies,
        measurement_anchor=base.measurement_anchor,
    )


def test_equal_provider_local_tokens_remain_distinct_denominators(live) -> None:
    _, _, authority, first = live
    second = _retime(
        first,
        first.time_coordinate,
        provider_namespace=_token("provider", "d"),
        linkage_id=_token("linkage", "d"),
    )
    manifest = _multi_provider_manifest(authority, (second, first))
    assert len(manifest.members) == 2
    assert sum(item.denominator_contribution for item in manifest.members) == 2
    assert cohort_manifest_from_bytes(cohort_manifest_bytes(manifest)) == manifest


def _cross_provider_dependency_copy(live) -> tuple[CohortManifest, CohortManifest]:
    _, _, authority, first = live
    provider_b = _token("provider", "d")
    draw_b = _retime(
        first,
        first.time_coordinate + 10,
        provider_namespace=provider_b,
        linkage_id=_token("linkage", "d"),
        analysis_record_id=_token("analysis", "d"),
    )
    replicate_b = _retime(
        first,
        first.time_coordinate + 10,
        provider_namespace=provider_b,
        linkage_id=_token("linkage", "e"),
        analysis_record_id=_token("analysis", "e"),
        lineage_role=MemberLineageRole.TECHNICAL_REPLICATE,
        technical_replicate_of=draw_b.analysis_record_id,
        denominator_contribution=False,
    )
    valid = _multi_provider_manifest(authority, (first, draw_b, replicate_b))
    forged_replicate = replicate_b.model_copy(
        update={"technical_replicate_of": first.analysis_record_id}
    )
    forged = valid.model_copy(update={"members": (first, draw_b, forged_replicate)})
    return valid, forged


def test_cross_provider_dependency_rejects_canonical_live_and_history(live) -> None:
    store, _, _, _ = live
    valid, forged = _cross_provider_dependency_copy(live)
    assert cohort_manifest_from_bytes(cohort_manifest_bytes(valid)) == valid
    with pytest.raises(ValueError, match="not canonical"):
        cohort_manifest_from_bytes(cohort_manifest_bytes(forged))
    with pytest.raises(ValueError, match="not canonical"):
        _validate(
            forged,
            store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )
    with pytest.raises(ValueError, match="not canonical"):
        validate_manifest_history((forged,))
