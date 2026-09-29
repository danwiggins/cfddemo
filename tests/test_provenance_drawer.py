"""Deterministic and adversarial tests for the E10 provenance drawer."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import (
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
    decide_compatibility,
)
from evidence_inspector.method_registry import (
    QualificationState,
    method_definition_sha256,
)
from evidence_inspector.provenance_drawer import (
    BoundAssetEvidence,
    ComparisonFieldKey,
    CountEvidence,
    CountRole,
    DenominatorEvidence,
    DifferenceState,
    DrawerBuildRequest,
    DrawerError,
    FilterEvidence,
    LimitationEvidence,
    MeasurementValue,
    ProvenanceDrawer,
    SideEvidenceInput,
    VISIBLE_FIELD_ORDER,
    build_provenance_drawer,
    canonical_drawer_bytes,
    drawer_from_canonical_bytes,
)
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from tests.test_compatibility import (
    HEAD_SHA256,
    NOW,
    REGISTRY_SHA256,
    _capability,
    _key,
    _method,
    _policy_for,
)
from traceback_runner.assets import AssetVerification, IntegrityStatus
from traceback_runner.qualification import (
    AssetAuthorizationDecision,
    AssetLifecycleStatus,
    AuthorityFailure,
    AuthorityStatus,
)
from traceback_runner.release_evidence import (
    AssetContentIdentity,
    AssetKind,
    AssetLifecycle,
    AssetProvenance,
    AssetReference,
    AssetStatus,
    DigestDomain,
    domain_digest,
)
from traceback_runner.serialization import canonical_json_bytes


METHOD = _method()
METHOD_SHA256 = method_definition_sha256(METHOD)
FIXTURES = Path(__file__).parent / "fixtures" / "provenance_drawer"


def _result_id(bundle_sha256: str, method_sha256: str) -> str:
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": bundle_sha256,
                "method_definition_sha256": method_sha256,
            }
        )
    ).hexdigest()
    return f"result_{identity[:40]}"


def _record(
    name: str,
    bundle_digit: str,
    result_digit: str,
    *,
    method=METHOD,
) -> VerifiedMeasurementRecord:
    bundle_sha256 = bundle_digit * 64
    method_sha256 = method_definition_sha256(method)
    return VerifiedMeasurementRecord(
        result_id=_result_id(bundle_sha256, method_sha256),
        result_sha256=result_digit * 64,
        bundle_id=f"bundle_{name}",
        bundle_sha256=bundle_sha256,
        method=method,
        method_definition_sha256=method_sha256,
        current_capability=_capability(method),
        execution_state=ExecutionState.COMPLETE,
        information_state=InformationState.SUFFICIENT,
        trust_state=TrustState.VERIFIED,
        compatibility_key=_key(method),
    )


def _catalog(record: VerifiedMeasurementRecord, name: str) -> CatalogResultRef:
    capability = record.current_capability
    return CatalogResultRef(
        result_id=record.result_id,
        bundle_sha256=record.bundle_sha256,
        bundle_record_id=f"synthetic.record.{name}",
        bundle_manifest_sha256=("1" if name == "alpha" else "2") * 64,
        workflow_release_id="synthetic.workflow.v1",
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
        registry_sha256=capability.registry_sha256,
        registry_version=capability.registry_version,
        authority_head_sha256=capability.authority_head_sha256,
        authority_revision=capability.authority_revision,
        authority_scope=capability.authority_scope,
        capability_as_of=capability.as_of,
        qualification_state=CatalogQualificationState.DEVELOPMENT_UNQUALIFIED,
        display_role=capability.display_role,
        research_inspectable=capability.research_inspectable,
        current_provider_eligible=capability.current_provider_eligible,
    )


def _asset_proof(method_asset) -> BoundAssetEvidence:
    reference = AssetReference(
        content=AssetContentIdentity(
            asset_id=method_asset.asset_id,
            version=method_asset.version,
            kind=AssetKind.REFERENCE,
            content_sha256=method_asset.content_sha256,
            content_size_bytes=128,
        ),
        provenance=AssetProvenance(
            source_authority="Synthetic fixture authority.",
            license_id="synthetic-only",
        ),
        lifecycle=AssetLifecycle(status=AssetStatus.ACTIVE),
    )
    reference_sha256 = domain_digest(DigestDomain.ASSET_REFERENCE, reference)
    authorization = AssetAuthorizationDecision(
        authority_status=AuthorityStatus.VERIFIED,
        lifecycle_status=AssetLifecycleStatus.ACTIVE,
        failure=AuthorityFailure.NONE,
        release_id="synthetic-release",
        release_version="v1",
        package_sha256=hashlib.sha256(
            method_asset.asset_id.encode("ascii")
        ).hexdigest(),
        asset_id=method_asset.asset_id,
        asset_version=method_asset.version,
        asset_reference_sha256=reference_sha256,
        verified_as_of=NOW - timedelta(hours=1),
        fresh_until=NOW + timedelta(days=1),
        authorized_reference=reference,
    )
    verification = AssetVerification(
        asset_id=method_asset.asset_id,
        version=method_asset.version,
        installed=True,
        integrity_status=IntegrityStatus.VALID,
        authority_status=AuthorityStatus.VERIFIED.value,
        lifecycle_status=AssetLifecycleStatus.ACTIVE.value,
        authority_failure=AuthorityFailure.NONE.value,
        registered_reference_sha256=reference_sha256,
        current_reference_sha256=reference_sha256,
        content_sha256=method_asset.content_sha256,
        content_size_bytes=128,
        registration_matches_current_reference=True,
    )
    return BoundAssetEvidence(
        method_asset=method_asset,
        verification=verification,
        authorization=authorization,
    )


def _side(
    record: VerifiedMeasurementRecord,
    name: str,
    *,
    eligible: int,
    value: float,
) -> SideEvidenceInput:
    denominator = DenominatorEvidence(
        denominator_id="denom_complete_primary",
        semantics_id="sem_denominator_alpha",
        definition_sha256="3" * 64,
        total_count=100,
    )
    denominator_sha256 = hashlib.sha256(
        canonical_json_bytes(denominator)
    ).hexdigest()
    excluded = 100 - eligible
    counts = tuple(
        CountEvidence(
            count_id=f"count_{role.value}",
            role=role,
            value={
                CountRole.TOTAL: 100,
                CountRole.ELIGIBLE: eligible,
                CountRole.EXCLUDED: excluded,
            }[role],
            denominator_sha256=denominator_sha256,
        )
        for role in CountRole
    )
    return SideEvidenceInput(
        catalog_result=_catalog(record, name),
        measurement=record,
        assets=tuple(_asset_proof(item) for item in record.method.assets),
        denominator=denominator,
        counts=counts,
        filters=(
            FilterEvidence(
                filter_id="filter_primary_complete",
                version="1.0.0",
                definition_sha256="4" * 64,
                denominator_sha256=denominator_sha256,
                input_count=100,
                retained_count=eligible,
                excluded_count=excluded,
            ),
        ),
        limitations=(
            LimitationEvidence(
                limitation_id="limit_research_only",
                version="1.0.0",
                statement_sha256="5" * 64,
                method_definition_sha256=record.method_definition_sha256,
            ),
        ),
        measurement_value=MeasurementValue(
            numeric_value=value,
            display_value=f"{value:.12g} fraction",
            quantity_id=record.method.quantity_id,
            unit=record.method.unit,
        ),
    )


def _request() -> DrawerBuildRequest:
    left_record = _record("alpha", "a", "c")
    right_record = _record("beta", "b", "d")
    policy = _policy_for(left_record, right_record)
    compatibility_request = CompatibilityRequest(
        left=left_record,
        right=right_record,
        policy=policy,
        trusted_policy_sha256=compatibility_policy_sha256(policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )
    decision = decide_compatibility(compatibility_request)
    return DrawerBuildRequest(
        evaluated_at=NOW,
        left=_side(left_record, "alpha", eligible=90, value=0.25),
        right=_side(right_record, "beta", eligible=80, value=0.30),
        compatibility_request=compatibility_request,
        compatibility_decision=decision,
    )


def _method_difference_request() -> DrawerBuildRequest:
    left_record = _record("method_alpha", "6", "7")
    alternate_method = _method(version="2.0.0", parameter_digest="8" * 64)
    right_record = _record(
        "method_beta",
        "9",
        "0",
        method=alternate_method,
    )
    policy = _policy_for(left_record, right_record)
    compatibility_request = CompatibilityRequest(
        left=left_record,
        right=right_record,
        policy=policy,
        trusted_policy_sha256=compatibility_policy_sha256(policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )
    return DrawerBuildRequest(
        evaluated_at=NOW,
        left=_side(left_record, "method_alpha", eligible=90, value=0.25),
        right=_side(right_record, "method_beta", eligible=90, value=0.25),
        compatibility_request=compatibility_request,
        compatibility_decision=decide_compatibility(compatibility_request),
    )


def test_synthetic_fixture_is_canonical_complete_and_deterministic() -> None:
    expected = json.loads(
        (FIXTURES / "expected-comparable.json").read_text(encoding="utf-8")
    )
    first = build_provenance_drawer(_request())
    second = build_provenance_drawer(_request())

    assert first == second
    assert tuple(item.field for item in first.fields) == VISIBLE_FIELD_ORDER
    assert tuple(item.value for item in first.changed_fields) == tuple(
        expected["changed_fields"]
    )
    assert tuple(item.value for item in first.unchanged_fields) == tuple(
        expected["unchanged_fields"]
    )
    assert all(
        item.difference
        == (
            DifferenceState.CHANGED
            if item.field in first.changed_fields
            else DifferenceState.UNCHANGED
        )
        for item in first.fields
    )
    content = canonical_drawer_bytes(first)
    assert drawer_from_canonical_bytes(ProvenanceDrawer, content) == first
    assert hashlib.sha256(content).hexdigest() == expected["canonical_bytes_sha256"]
    assert first.drawer_sha256 == expected["drawer_sha256"]
    assert first.compatibility_decision_sha256 == (
        expected["compatibility_decision_sha256"]
    )


def test_every_visible_field_resolves_all_exact_identity_categories() -> None:
    drawer = build_provenance_drawer(_request())
    for field in drawer.fields:
        for lineage, side in (
            (field.left_lineage, drawer.left),
            (field.right_lineage, drawer.right),
        ):
            assert lineage.side_provenance_sha256 == side.provenance_sha256
            assert lineage.bundle_sha256 == side.bundle_sha256
            assert lineage.bundle_manifest_sha256 == side.bundle_manifest_sha256
            assert lineage.method_definition_sha256 == side.method_definition_sha256
            assert lineage.capability_sha256 == side.capability_sha256
            assert lineage.registry_sha256 == side.registry_sha256
            assert lineage.authority_head_sha256 == side.authority_head_sha256
            assert lineage.authority_scope == side.authority_scope
            assert lineage.capability_as_of == side.capability_as_of
            assert lineage.asset_reference_sha256s
            assert lineage.denominator_sha256 == side.denominator_sha256
            assert lineage.count_sha256s == side.count_sha256s
            assert lineage.filter_sha256s == side.filter_sha256s
            assert lineage.limitation_sha256s == side.limitation_sha256s
            assert (
                lineage.compatibility_decision_sha256
                == drawer.compatibility_decision_sha256
            )


def test_method_difference_fixture_is_explicit_and_withholds_inference() -> None:
    expected = json.loads(
        (FIXTURES / "expected-method-difference.json").read_text(
            encoding="utf-8"
        )
    )
    drawer = build_provenance_drawer(_method_difference_request())
    assert drawer.compatibility_outcome.value == expected["outcome"]
    assert tuple(item.value for item in drawer.changed_fields) == tuple(
        expected["changed_fields"]
    )
    assert tuple(item.value for item in drawer.unchanged_fields) == tuple(
        expected["unchanged_fields"]
    )
    assert tuple(item.value for item in drawer.compatibility_mismatch_keys) == tuple(
        expected["mismatch_keys"]
    )
    content = canonical_drawer_bytes(drawer)
    assert hashlib.sha256(content).hexdigest() == expected["canonical_bytes_sha256"]
    assert drawer.drawer_sha256 == expected["drawer_sha256"]
    assert drawer.compatibility_decision_sha256 == (
        expected["compatibility_decision_sha256"]
    )


def test_replay_mutation_and_cross_contract_drift_fail_closed() -> None:
    request = _request()
    bad_compatibility_request = request.compatibility_request.model_copy(
        update={"trusted_policy_sha256": "9" * 64}
    )
    replay_valid_but_wrong = decide_compatibility(bad_compatibility_request)
    with pytest.raises(ValidationError, match="replay failed"):
        DrawerBuildRequest(
            **request.model_dump(exclude={"compatibility_decision"}),
            compatibility_decision=replay_valid_but_wrong,
        )

    wrong_catalog = request.left.catalog_result.model_copy(
        update={"bundle_sha256": "e" * 64}
    )
    with pytest.raises(ValidationError, match="catalog result"):
        SideEvidenceInput(
            **request.left.model_dump(exclude={"catalog_result"}),
            catalog_result=wrong_catalog,
        )

    drawer = build_provenance_drawer(request)
    payload = drawer.model_dump(mode="json")
    payload["fields"][0]["left_lineage"]["bundle_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="lineage"):
        ProvenanceDrawer.model_validate_json(canonical_json_bytes(payload))

    bypassed_side = request.left.model_copy(
        update={"catalog_result": wrong_catalog}
    )
    bypassed_request = request.model_copy(update={"left": bypassed_side})
    with pytest.raises(DrawerError, match="build request"):
        build_provenance_drawer(bypassed_request)


def test_unknown_stale_revoked_and_incomplete_inputs_fail_closed() -> None:
    request = _request()
    bad_compatibility_request = request.compatibility_request.model_copy(
        update={"trusted_policy_sha256": "9" * 64}
    )
    unknown = decide_compatibility(bad_compatibility_request)
    with pytest.raises(ValidationError, match="unknown compatibility"):
        DrawerBuildRequest(
            evaluated_at=NOW,
            left=request.left,
            right=request.right,
            compatibility_request=bad_compatibility_request,
            compatibility_decision=unknown,
        )

    first_asset = request.left.assets[0]
    stale_authorization = first_asset.authorization.model_copy(
        update={"fresh_until": NOW}
    )
    stale_asset = first_asset.model_copy(
        update={"authorization": stale_authorization}
    )
    stale_left = request.left.model_copy(
        update={"assets": (stale_asset, *request.left.assets[1:])}
    )
    with pytest.raises(ValidationError, match="stale"):
        DrawerBuildRequest(
            **request.model_dump(exclude={"left"}), left=stale_left
        )

    revoked_verification = first_asset.verification.model_copy(
        update={
            "lifecycle_status": AssetLifecycleStatus.REVOKED.value,
            "authority_failure": AuthorityFailure.EXPECTED_ASSET_MISMATCH.value,
        }
    )
    with pytest.raises(ValidationError, match="incomplete, stale, or revoked"):
        BoundAssetEvidence(
            method_asset=first_asset.method_asset,
            verification=revoked_verification,
            authorization=first_asset.authorization,
        )

    wrong_reference_digest = first_asset.authorization.model_copy(
        update={"asset_reference_sha256": "8" * 64}
    )
    with pytest.raises(ValidationError, match="reference digest"):
        BoundAssetEvidence(
            method_asset=first_asset.method_asset,
            verification=first_asset.verification.model_copy(
                update={
                    "registered_reference_sha256": "8" * 64,
                    "current_reference_sha256": "8" * 64,
                }
            ),
            authorization=wrong_reference_digest,
        )

    with pytest.raises(ValidationError, match="at least 3 items"):
        SideEvidenceInput(
            **request.left.model_dump(exclude={"counts"}),
            counts=request.left.counts[:2],
        )


@pytest.mark.parametrize(
    "private_value",
    (
        "/private/tmp/result",
        "donor_alpha",
        "ACGTACGTACGTACGT",
    ),
)
def test_private_paths_identifiers_and_sequence_are_rejected(
    private_value: str,
) -> None:
    request = _request()
    catalog = request.left.catalog_result.model_copy(
        update={"workflow_release_id": private_value}
    )
    with pytest.raises((ValidationError, ValueError), match="path|privacy|sequence"):
        SideEvidenceInput(
            **request.left.model_dump(exclude={"catalog_result"}),
            catalog_result=catalog,
        )


def test_canonical_boundary_and_closed_schemas_reject_mutation() -> None:
    drawer = build_provenance_drawer(_request())
    content = canonical_drawer_bytes(drawer)
    noncanonical = json.dumps(
        json.loads(content), indent=2, sort_keys=False
    ).encode("utf-8")
    with pytest.raises(DrawerError, match="non-canonical"):
        drawer_from_canonical_bytes(ProvenanceDrawer, noncanonical)

    payload = drawer.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProvenanceDrawer.model_validate(payload)

    public = content.decode("utf-8")
    for forbidden in (
        "/private/",
        "donor_",
        "patient_",
        "sample_",
        "ACGTACGTACGT",
        "synthetic.record.alpha",
        "synthetic.workflow.v1",
        "Synthetic fixture authority.",
        "synthetic-release",
    ):
        assert forbidden not in public


def test_authority_constants_are_exact_fixture_inputs() -> None:
    request = _request()
    assert request.left.measurement.current_capability.registry_sha256 == (
        REGISTRY_SHA256
    )
    assert request.left.measurement.current_capability.qualification_state == (
        QualificationState.DEVELOPMENT_UNQUALIFIED
    )


def test_failure_fixture_enumerates_the_adversarial_boundary() -> None:
    fixture = json.loads(
        (FIXTURES / "fail-closed-cases.json").read_text(encoding="utf-8")
    )
    assert fixture["synthetic_only"] is True
    assert set(fixture["cases"]) == {
        "compatibility_replay_drift",
        "unknown_compatibility",
        "stale_asset_authority",
        "revoked_asset",
        "incomplete_counts",
        "catalog_bundle_mismatch",
        "field_lineage_mutation",
        "private_identifier",
        "local_path",
        "sequence_like_text",
        "noncanonical_bytes",
    }
