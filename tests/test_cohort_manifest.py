"""D05 live-store cohort membership, denominator, and replay tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MeasurementAnchor,
    MemberLineageRole,
    PolicyDigests,
    ProviderAuthorityReference,
    ReanalysisRule,
    TechnicalReplicateRule,
    TimeAxis,
    TimeAxisKind,
    _domain_sha256,
    build_cohort_manifest,
    build_cohort_member,
    cohort_manifest_bytes,
    cohort_manifest_from_bytes,
    cohort_manifest_sha256,
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
from evidence_inspector.provider_linkage_store import ProviderLinkageStoreError
from tests.test_provider_linkage import (
    PROVIDER,
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

CREATED = datetime(2026, 9, 29, tzinfo=UTC)
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


@pytest.fixture
def live(tmp_path: Path):
    store = _store(tmp_path / "protected")
    revision = _known_run_revision()
    record, _ = _consume(revision, (_create_approval(revision, "c"),))
    store.commit_authorized_revision(record)
    snapshot = store.active_snapshot()
    authority = _authority(snapshot)
    member = build_cohort_member(
        revision=snapshot.revisions[0],
        receipt=snapshot.receipts[0],
        time_axis=TIME_AXIS,
        time_coordinate=int(snapshot.revisions[0].proposed_at.timestamp()),
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        denominator_contribution=True,
        unit_of_analysis=UnitOfAnalysis.COLLECTION,
    )
    try:
        yield store, snapshot, authority, member
    finally:
        store.close()


def _authority(snapshot) -> ProviderAuthorityReference:
    return ProviderAuthorityReference(
        provider_namespace=PROVIDER,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(_trust()),
        store_id=snapshot.store_id,
        store_epoch_sha256=snapshot.store_epoch_sha256,
        storage_identity_sha256=snapshot.storage_identity_sha256,
        trust_pins_sha256=snapshot.trust_pins_sha256,
        state_version=snapshot.state_version,
        state_head_sha256=snapshot.state_head_sha256,
    )


def _manifest(authority, members, **updates) -> CohortManifest:
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
    return build_cohort_manifest(
        provider_authorities=(authority,), members=members, **values
    )


def _retime(member: CohortMember, coordinate: int, **updates) -> CohortMember:
    payload = member.model_dump(mode="python")
    payload.update(updates)
    payload["time_coordinate"] = coordinate
    payload["time_coordinate_sha256"] = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": payload["collection_token"],
            "linkage_event_sha256": payload["linkage_event_sha256"],
            "time_axis": TIME_AXIS.model_dump(mode="json"),
            "time_coordinate": coordinate,
        },
    )
    return CohortMember.model_validate(payload)


def test_happy_manifest_replays_canonical_bytes_against_live_store(live) -> None:
    store, _, authority, member = live
    manifest = _manifest(authority, (member,))
    assert cohort_manifest_from_bytes(cohort_manifest_bytes(manifest)) == manifest
    validate_manifest_against_linkage_store(
        manifest, store, expected_trust_snapshot_sha256_by_provider=_pins()
    )
    assert manifest.synthetic_only and not manifest.clinical_use_authorized


def test_closed_store_and_detached_snapshot_cannot_authorize(live) -> None:
    store, snapshot, authority, member = live
    manifest = _manifest(authority, (member,))
    store.close()
    with pytest.raises(ProviderLinkageStoreError):
        validate_manifest_against_linkage_store(
            manifest, store, expected_trust_snapshot_sha256_by_provider=_pins()
        )
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
        validate_manifest_against_linkage_store(
            manifest, store, expected_trust_snapshot_sha256_by_provider=_pins()
        )


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
            validate_manifest_against_linkage_store(
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
    with pytest.raises(ValueError, match="live store trust pins"):
        validate_manifest_against_linkage_store(
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
        ("linkage_event_sha256", "f" * 64, "time source"),
    ),
)
def test_wrong_lineage_version_receipt_or_time_source_fails(
    live, field, value, message
) -> None:
    store, _, authority, member = live
    changed = _retime(member, member.time_coordinate, **{field: value})
    with pytest.raises(ValueError, match=message):
        validate_manifest_against_linkage_store(
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
    assert [m.time_coordinate for m in subject_manifest.members] == [100, 200]
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
    with pytest.raises(ValidationError, match="cycle"):
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
        ValidationError, match="technical replicate lineage contains a cycle"
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
            time_axis=TIME_AXIS,
            time_coordinate=1,
            lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
            denominator_contribution=True,
            unit_of_analysis=UnitOfAnalysis.COLLECTION,
        )


def test_time_coordinate_tamper_and_noncanonical_order_reject(live) -> None:
    _, _, authority, member = live
    tampered = member.model_copy(update={"time_coordinate": 999})
    with pytest.raises(ValidationError, match="time coordinate"):
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
        validate_manifest_against_linkage_store(
            manifest, store, expected_trust_snapshot_sha256_by_provider=_pins()
        )
    corrected_snapshot = store.active_snapshot()
    corrected_authority = _authority(corrected_snapshot)
    corrected_member = build_cohort_member(
        revision=corrected_snapshot.revisions[0],
        receipt=corrected_snapshot.receipts[0],
        time_axis=TIME_AXIS,
        time_coordinate=int(correction_revision.proposed_at.timestamp()),
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
        validate_manifest_against_linkage_store(
            corrected_manifest,
            store,
            expected_trust_snapshot_sha256_by_provider=_pins(),
        )
