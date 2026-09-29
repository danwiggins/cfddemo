"""Deterministic and adversarial tests for the E10 provenance drawer."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
    MAX_CANONICAL_DRAWER_BYTES,
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
    ApproverRole,
    AssetLifecycleStatus,
    AuthorityFailure,
    AuthorityStatus,
    AuthorityScope as ReleaseAuthorityScope,
    GrantStatus,
    QualificationTrustPolicy,
    ReleaseAuthorityHead,
    SignerRoleGrant,
    qualification_binding,
    sign_development_release_evidence,
    verify_release_asset_authorization,
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
from traceback_runner.signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    development_trust_bytes,
    load_development_trust,
)
from tests.test_release_evidence import _package


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
    result_sha256: str,
    *,
    method=METHOD,
) -> VerifiedMeasurementRecord:
    bundle_sha256 = bundle_digit * 64
    method_sha256 = method_definition_sha256(method)
    return VerifiedMeasurementRecord(
        result_id=_result_id(bundle_sha256, method_sha256),
        result_sha256=result_sha256,
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
    package = _package(asset=reference)
    package_sha256 = domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
    binding = qualification_binding(package)
    private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(method_asset.asset_id.encode("ascii")).digest()
    )
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key = DevelopmentSigningKey(
        key_id=f"dev-release-{hashlib.sha256(public).hexdigest()[:24]}",
        purpose=KeyPurpose.RELEASE,
        private_key=private_key,
    )
    envelope = sign_development_release_evidence(
        package, key, signer_role=ApproverRole.RELEASE_REVIEWER
    )
    policy = QualificationTrustPolicy(
        policy_id="synthetic-policy",
        version="v1",
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        grants=(
            SignerRoleGrant(
                scope=ReleaseAuthorityScope.RELEASE_EVIDENCE,
                role=ApproverRole.RELEASE_REVIEWER,
                key_id=key.key_id,
                binding=binding,
                valid_from=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=1),
                status=GrantStatus.ACTIVE,
            ),
        ),
    )
    head = ReleaseAuthorityHead(
        release_id=package.release_id,
        release_version=package.version,
        package_sha256=package_sha256,
        as_of=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )
    trust_document_bytes = development_trust_bytes(key)
    trust = load_development_trust(trust_document_bytes)
    authorization = verify_release_asset_authorization(
        envelope,
        trust,
        policy,
        head,
        expected_binding=binding,
        expected_package_sha256=package_sha256,
        expected_asset_id=method_asset.asset_id,
        expected_asset_version=method_asset.version,
        expected_asset_reference_sha256=reference_sha256,
        now=NOW,
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
        release_envelope=envelope,
        trust_document=json.loads(trust_document_bytes),
        role_policy=policy,
        authority_head=head,
        expected_binding=binding,
    )


def _evidence_parts(method, *, eligible: int, value: float) -> dict[str, object]:
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
    measurement_value = MeasurementValue(
        numeric_value=value,
        display_value=f"{value:.12g} fraction",
        quantity_id=method.quantity_id,
        unit=method.unit,
    )
    filters = (
        FilterEvidence(
            filter_id="filter_primary_complete",
            version="1.0.0",
            definition_sha256="4" * 64,
            denominator_sha256=denominator_sha256,
            input_count=100,
            retained_count=eligible,
            excluded_count=excluded,
        ),
    )
    limitations = (
        LimitationEvidence(
            limitation_id="limit_research_only",
            version="1.0.0",
            statement_sha256="5" * 64,
            method_definition_sha256=method_definition_sha256(method),
        ),
    )
    payload = {
        "schema_version": "traceback.drawer-evidence-payload.v1",
        "measurement_value": measurement_value.model_dump(mode="json"),
        "denominator": denominator.model_dump(mode="json"),
        "counts": [item.model_dump(mode="json") for item in counts],
        "filters": [item.model_dump(mode="json") for item in filters],
        "limitations": [item.model_dump(mode="json") for item in limitations],
    }
    return {
        "denominator": denominator,
        "counts": counts,
        "filters": filters,
        "limitations": limitations,
        "measurement_value": measurement_value,
        "evidence_payload_sha256": hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest(),
    }


def _side(
    record: VerifiedMeasurementRecord,
    name: str,
    *,
    eligible: int,
    value: float,
) -> SideEvidenceInput:
    evidence = _evidence_parts(record.method, eligible=eligible, value=value)
    return SideEvidenceInput(
        catalog_result=_catalog(record, name),
        measurement=record,
        assets=tuple(_asset_proof(item) for item in record.method.assets),
        **evidence,
    )


def _request() -> DrawerBuildRequest:
    left_digest = _evidence_parts(METHOD, eligible=90, value=0.25)[
        "evidence_payload_sha256"
    ]
    right_digest = _evidence_parts(METHOD, eligible=80, value=0.30)[
        "evidence_payload_sha256"
    ]
    left_record = _record("alpha", "a", str(left_digest))
    right_record = _record("beta", "b", str(right_digest))
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
    alternate_method = _method(version="2.0.0", parameter_digest="8" * 64)
    left_digest = _evidence_parts(METHOD, eligible=90, value=0.25)[
        "evidence_payload_sha256"
    ]
    right_digest = _evidence_parts(alternate_method, eligible=90, value=0.25)[
        "evidence_payload_sha256"
    ]
    left_record = _record("method_alpha", "6", str(left_digest))
    right_record = _record(
        "method_beta",
        "9",
        str(right_digest),
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
            assert lineage.asset_verified_as_of == tuple(
                item.verified_as_of for item in side.assets
            )
            assert lineage.asset_fresh_until == tuple(
                item.fresh_until for item in side.assets
            )
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
    with pytest.raises(ValidationError, match="replay"):
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
            **first_asset.model_dump(
                exclude={"method_asset", "verification", "authorization"}
            ),
            method_asset=first_asset.method_asset,
            verification=revoked_verification,
            authorization=first_asset.authorization,
        )

    wrong_reference_digest = first_asset.authorization.model_copy(
        update={"asset_reference_sha256": "8" * 64}
    )
    with pytest.raises(ValidationError, match="reference digest"):
        BoundAssetEvidence(
            **first_asset.model_dump(
                exclude={"method_asset", "verification", "authorization"}
            ),
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
        "source_evidence_mutation",
        "compatibility_output_rewrite",
        "canonical_freshness_drift",
        "encoded_path",
        "full_iupac_sequence",
        "asset_authorization_reassertion",
        "oversized_canonical_input",
        "duplicate_count_id",
    }


def _rehash_drawer_payload(payload: dict[str, object]) -> bytes:
    payload["drawer_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "drawer_sha256"}
        )
    ).hexdigest()
    return canonical_json_bytes(payload)


def test_canonical_source_evidence_mutation_cannot_retain_result_identity() -> None:
    drawer = build_provenance_drawer(_request())
    payload = drawer.model_dump(mode="json")
    left = payload["replay_request"]["left"]
    left["measurement_value"]["numeric_value"] = 0.99
    left["measurement_value"]["display_value"] = "0.99 fraction"
    left["counts"][1]["value"] = 0
    left["counts"][2]["value"] = 100
    left["filters"][0]["retained_count"] = 0
    left["filters"][0]["excluded_count"] = 100
    left["limitations"][0]["statement_sha256"] = "9" * 64

    with pytest.raises(DrawerError, match="invalid"):
        drawer_from_canonical_bytes(ProvenanceDrawer, _rehash_drawer_payload(payload))


def test_canonical_compatibility_rewrite_and_derived_rows_fail_replay() -> None:
    drawer = build_provenance_drawer(_method_difference_request())
    payload = drawer.model_dump(mode="json")
    payload["compatibility_outcome"] = "comparable"
    payload["compatibility_mismatch_keys"] = []
    payload["compatibility_remediation_code"] = "none"
    for field in payload["fields"]:
        if field["field"] == "compatibility_decision":
            field["left"]["display_value"] = "comparable"
            field["right"]["display_value"] = "comparable"

    with pytest.raises(DrawerError, match="invalid"):
        drawer_from_canonical_bytes(ProvenanceDrawer, _rehash_drawer_payload(payload))


def test_canonical_parse_rechecks_capability_and_asset_freshness() -> None:
    drawer = build_provenance_drawer(_request())
    payload = drawer.model_dump(mode="json")
    payload["evaluated_at"] = "2026-10-01T12:00:00Z"
    payload["replay_request"]["evaluated_at"] = "2026-10-01T12:00:00Z"

    with pytest.raises(DrawerError, match="invalid"):
        drawer_from_canonical_bytes(ProvenanceDrawer, _rehash_drawer_payload(payload))


@pytest.mark.parametrize(
    "private_value",
    (
        "A" * 64,
        "%2Fprivate%2Ftmp%2Fevidence",
        "%252e%252e%252fprivate%252fresult",
    ),
)
def test_canonical_privacy_scan_decodes_paths_and_rejects_full_iupac(
    private_value: str,
) -> None:
    drawer = build_provenance_drawer(_request())
    payload = drawer.model_dump(mode="json")
    payload["fields"][0]["left"]["display_value"] = private_value
    with pytest.raises(DrawerError, match="invalid"):
        drawer_from_canonical_bytes(ProvenanceDrawer, _rehash_drawer_payload(payload))


@pytest.mark.parametrize(
    ("target", "replacement"),
    (
        ("content_size_bytes", 129),
        ("release_version", "v2"),
        ("package_sha256", "9" * 64),
        ("fresh_until", "2026-09-29T12:00:00Z"),
    ),
)
def test_authenticated_asset_fields_cannot_be_reasserted(
    target: str, replacement: object
) -> None:
    drawer = build_provenance_drawer(_request())
    payload = drawer.model_dump(mode="json")
    asset = payload["replay_request"]["left"]["assets"][0]
    if target == "content_size_bytes":
        asset["verification"][target] = replacement
    else:
        asset["authorization"][target] = replacement
    with pytest.raises(DrawerError, match="invalid"):
        drawer_from_canonical_bytes(ProvenanceDrawer, _rehash_drawer_payload(payload))


def test_schema_input_limit_is_enforced_before_json_parse() -> None:
    content = b"{" + b" " * MAX_CANONICAL_DRAWER_BYTES + b"}"
    with pytest.raises(DrawerError, match="schema input limit"):
        drawer_from_canonical_bytes(ProvenanceDrawer, content)


def test_duplicate_count_id_across_roles_is_rejected() -> None:
    request = _request()
    counts = list(request.left.counts)
    counts[1] = counts[1].model_copy(update={"count_id": counts[0].count_id})
    with pytest.raises(ValidationError, match="count IDs must be unique"):
        SideEvidenceInput(
            **request.left.model_dump(exclude={"counts"}),
            counts=tuple(counts),
        )
