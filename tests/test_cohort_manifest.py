"""D05 immutable cohort membership, denominator, and linkage tests."""

from __future__ import annotations

from datetime import UTC, datetime
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
    TechnicalReplicateRule,
    TimeAxis,
    TimeAxisKind,
    build_cohort_manifest,
    cohort_manifest_bytes,
    cohort_manifest_sha256,
    validate_manifest_against_linkage_snapshot,
    validate_manifest_history,
)
from evidence_inspector.provider_linkage import UnitOfAnalysis, linkage_revision_sha256
from evidence_inspector.provider_linkage_store import committed_linkage_receipt_sha256
from tests.test_provider_linkage import (
    PROVIDER,
    _token,
    _trust,
    provider_trust_snapshot_sha256,
)
from tests.test_provider_linkage_store import _record, _store

CREATED = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.fixture
def linked(tmp_path: Path):
    with _store(tmp_path / "protected") as store:
        record = _record()
        store.commit_authorized_revision(record)
        snapshot = store.active_snapshot()
    revision = snapshot.revisions[0]
    receipt = snapshot.receipts[0]
    authority = ProviderAuthorityReference(
        provider_namespace=PROVIDER,
        trust_snapshot_sha256=provider_trust_snapshot_sha256(_trust()),
        store_id=snapshot.store_id,
        store_epoch_sha256=snapshot.store_epoch_sha256,
        storage_identity_sha256=snapshot.storage_identity_sha256,
        trust_pins_sha256=snapshot.trust_pins_sha256,
        state_version=snapshot.state_version,
        state_head_sha256=snapshot.state_head_sha256,
    )
    member = CohortMember(
        provider_namespace=PROVIDER,
        linkage_id=revision.linkage_id,
        linkage_revision=revision.revision,
        linkage_revision_sha256=linkage_revision_sha256(revision),
        committed_receipt_sha256=committed_linkage_receipt_sha256(receipt),
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
        specimen_token=revision.biological.specimen_token,
        analysis_record_id=revision.technical.analysis_record_id,
        run_token=revision.technical.run.token,
        reanalysis_of=revision.technical.reanalysis_of.token,
        lineage_role=MemberLineageRole.BIOLOGICAL_DRAW,
        analysis_unit_token=revision.biological.collection_token,
        denominator_contribution=True,
    )
    return snapshot, authority, member


def _manifest(authority, members, **updates) -> CohortManifest:
    values = {
        "cohort_id": "cohort_" + "1" * 32,
        "version": 1,
        "previous_manifest_sha256": None,
        "created_at": CREATED,
        "unit_of_analysis": UnitOfAnalysis.COLLECTION,
        "technical_replicate_rule": TechnicalReplicateRule.COLLAPSE_TO_BIOLOGICAL_UNIT,
        "time_axis": TimeAxis(
            kind=TimeAxisKind.COLLECTION_TIME,
            definition_sha256="2" * 64,
            unit_sha256="3" * 64,
            origin_authority_sha256="4" * 64,
        ),
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


def test_happy_manifest_is_canonical_and_validates_live_authority(linked) -> None:
    snapshot, authority, member = linked
    first = _manifest(authority, (member,))
    second = _manifest(authority, tuple(reversed((member,))))
    assert cohort_manifest_bytes(first) == cohort_manifest_bytes(second)
    validate_manifest_against_linkage_snapshot(
        first,
        snapshot,
        expected_trust_snapshot_sha256_by_provider={
            PROVIDER: authority.trust_snapshot_sha256
        },
    )
    assert first.synthetic_only and not first.clinical_use_authorized


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("subject_token", _token("subject", "d"), "lineage"),
        ("provider_namespace", _token("provider", "d"), "providers"),
        ("linkage_revision_sha256", "f" * 64, "digest"),
        ("committed_receipt_sha256", "f" * 64, "receipt"),
    ),
)
def test_wrong_or_stale_member_fails_closed(linked, field, value, message) -> None:
    snapshot, authority, member = linked
    changed = member.model_copy(update={field: value})
    if field == "provider_namespace":
        with pytest.raises(ValidationError, match=message):
            _manifest(authority, (changed,))
        return
    manifest = _manifest(authority, (changed,))
    with pytest.raises(ValueError, match=message):
        validate_manifest_against_linkage_snapshot(
            manifest,
            snapshot,
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: authority.trust_snapshot_sha256
            },
        )


def test_stale_snapshot_and_wrong_provider_authority_fail(linked) -> None:
    snapshot, authority, member = linked
    manifest = _manifest(authority, (member,))
    with pytest.raises(ValueError, match="independently pinned"):
        validate_manifest_against_linkage_snapshot(
            manifest, snapshot, expected_trust_snapshot_sha256_by_provider={}
        )
    stale = authority.model_copy(update={"state_head_sha256": "f" * 64})
    with pytest.raises(ValueError, match="exact live snapshot"):
        validate_manifest_against_linkage_snapshot(
            _manifest(stale, (member,)),
            snapshot,
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: stale.trust_snapshot_sha256
            },
        )


def test_duplicate_member_and_analysis_are_rejected(linked) -> None:
    _, authority, member = linked
    with pytest.raises(ValidationError, match="uniquely sorted"):
        CohortManifest.model_validate(
            _manifest(authority, (member,))
            .model_copy(update={"members": (member, member)})
            .model_dump(mode="python")
        )


def test_technical_rerun_cannot_be_a_biological_draw(linked) -> None:
    _, authority, member = linked
    duplicate = member.model_copy(
        update={
            "linkage_id": _token("linkage", "d"),
            "analysis_record_id": _token("analysis", "d"),
            "lineage_role": MemberLineageRole.BIOLOGICAL_DRAW,
            "denominator_contribution": True,
        }
    )
    with pytest.raises(ValidationError, match="exactly one denominator"):
        _manifest(authority, (member, duplicate))


def test_cross_unit_pseudoreplication_and_unknown_reanalysis_fail(linked) -> None:
    _, authority, member = linked
    rerun = member.model_copy(
        update={
            "linkage_id": _token("linkage", "d"),
            "analysis_record_id": _token("analysis", "d"),
            "specimen_token": _token("specimen", "d"),
            "lineage_role": MemberLineageRole.REANALYSIS,
            "reanalysis_of": _token("analysis", "e"),
            "denominator_contribution": False,
        }
    )
    with pytest.raises(ValidationError, match="pseudoreplication|outside"):
        _manifest(authority, (member, rerun))


def test_declared_subject_unit_must_match_member_key(linked) -> None:
    _, authority, member = linked
    with pytest.raises(ValidationError, match="declared lineage"):
        _manifest(authority, (member,), unit_of_analysis=UnitOfAnalysis.SUBJECT)


def test_version_history_requires_change_and_exact_predecessor(linked) -> None:
    _, authority, member = linked
    first = _manifest(authority, (member,))
    unchanged = _manifest(
        authority,
        (member,),
        version=2,
        previous_manifest_sha256=cohort_manifest_sha256(first),
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="must change"):
        validate_manifest_history((first, unchanged))
    changed = unchanged.model_copy(
        update={
            "policies": unchanged.policies.model_copy(
                update={"missingness_sha256": "b" * 64}
            )
        }
    )
    validate_manifest_history((first, changed))
    stale = changed.model_copy(update={"previous_manifest_sha256": "f" * 64})
    with pytest.raises(ValueError, match="predecessor"):
        validate_manifest_history((first, stale))
    with pytest.raises(ValueError, match="consecutive"):
        validate_manifest_history((first, first.model_copy()))


def test_privacy_sensitive_free_text_and_unknown_lineage_rejected(linked) -> None:
    _, _authority, member = linked
    payload = member.model_dump(mode="python")
    payload["subject_token"] = "patient Jane Doe"
    with pytest.raises(ValidationError):
        CohortMember.model_validate(payload)
    payload = member.model_dump(mode="python")
    payload["run_token"] = "unknown"
    with pytest.raises(ValidationError):
        CohortMember.model_validate(payload)


def test_boundary_rejects_empty_members_and_fractional_time(linked) -> None:
    _, authority, member = linked
    with pytest.raises(ValidationError):
        _manifest(authority, ())
    with pytest.raises(ValidationError, match="whole-second"):
        _manifest(authority, (member,), created_at=CREATED.replace(microsecond=1))


def test_builder_canonicalizes_member_permutations(linked) -> None:
    _, authority, first = linked
    second = first.model_copy(
        update={
            "linkage_id": _token("linkage", "d"),
            "subject_token": _token("subject", "d"),
            "collection_token": _token("collection", "d"),
            "specimen_token": _token("specimen", "d"),
            "analysis_record_id": _token("analysis", "d"),
            "analysis_unit_token": _token("collection", "d"),
        }
    )
    forward = _manifest(authority, (first, second))
    reverse = _manifest(authority, (second, first))
    assert cohort_manifest_bytes(forward) == cohort_manifest_bytes(reverse)


def test_explicit_technical_replicate_rule_controls_duplicate_unit(linked) -> None:
    _, authority, draw = linked
    replicate = draw.model_copy(
        update={
            "linkage_id": _token("linkage", "d"),
            "analysis_record_id": _token("analysis", "d"),
            "lineage_role": MemberLineageRole.TECHNICAL_REPLICATE,
            "denominator_contribution": False,
        }
    )
    collapsed = _manifest(authority, (draw, replicate))
    assert sum(member.denominator_contribution for member in collapsed.members) == 1
    with pytest.raises(ValidationError, match="excludes"):
        _manifest(
            authority,
            (draw, replicate),
            technical_replicate_rule=TechnicalReplicateRule.EXCLUDE_TECHNICAL_REPLICATES,
        )


def test_changed_content_cannot_reuse_same_version(linked) -> None:
    _, authority, member = linked
    first = _manifest(authority, (member,))
    mutated = first.model_copy(
        update={
            "policies": first.policies.model_copy(update={"inclusion_sha256": "c" * 64})
        }
    )
    with pytest.raises(ValueError, match="consecutive"):
        validate_manifest_history((first, mutated))


def test_missing_active_linkage_is_treated_as_stale_or_tombstoned(linked) -> None:
    snapshot, authority, member = linked
    manifest = _manifest(authority, (member,))
    empty = snapshot.model_copy(update={"revisions": (), "receipts": ()})
    with pytest.raises(ValueError, match="stale, tombstoned, or unknown"):
        validate_manifest_against_linkage_snapshot(
            manifest,
            empty,
            expected_trust_snapshot_sha256_by_provider={
                PROVIDER: authority.trust_snapshot_sha256
            },
        )
