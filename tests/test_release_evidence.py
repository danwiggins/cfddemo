"""Release evidence and development qualification authority tests."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from traceback_runner.qualification import (
    ApproverRole,
    AssetLifecycleStatus,
    AuthorityFailure,
    AuthorityScope,
    AuthorityStatus,
    GrantStatus,
    QualificationAuthorityHead,
    QualificationDecision,
    QualificationOutcome,
    QualificationTrustPolicy,
    ReleaseAuthorityHead,
    SignerRoleGrant,
    qualification_binding,
    qualification_decision_digest,
    sign_development_qualification_decision,
    sign_development_release_evidence,
    verify_development_qualification_history,
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
    EvidenceRequirementKind,
    EvidenceStatus,
    ProtocolApprovalStatus,
    ProtocolReference,
    QualificationEvidenceItem,
    QualificationEvidenceManifest,
    WorkstationProfile,
    build_release_evidence_package,
    canonical_domain_bytes,
    domain_digest,
)
from traceback_runner.signing import (
    KeyPurpose,
    TrustStore,
    generate_development_keypair,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _asset(*, status: AssetStatus = AssetStatus.ACTIVE) -> AssetReference:
    lifecycle = (
        AssetLifecycle(status=status)
        if status == AssetStatus.ACTIVE
        else AssetLifecycle(
            status=status,
            revocation_reference="synthetic-revocation-1",
            revoked_at=T0,
        )
    )
    return AssetReference(
        content=AssetContentIdentity(
            asset_id="synthetic-reference",
            version="v1",
            kind=AssetKind.REFERENCE,
            content_sha256="1" * 64,
            content_size_bytes=128,
        ),
        provenance=AssetProvenance(
            source_authority="Synthetic fixture authority; not a scientific source.",
            license_id="synthetic-only",
        ),
        lifecycle=lifecycle,
    )


def _profile() -> WorkstationProfile:
    return WorkstationProfile(
        profile_id="synthetic-host",
        version="v1",
        operating_system="linux",
        architecture="x86_64",
        kernel_version="synthetic-kernel.v1",
        runtime_id="synthetic-runtime",
        runtime_version="v1",
        accelerator_id=None,
        accelerator_version=None,
        minimum_cpu_cores=2,
        minimum_memory_bytes=1024,
        minimum_free_disk_bytes=2048,
    )


def _protocol() -> ProtocolReference:
    return ProtocolReference(
        protocol_id="synthetic-protocol",
        version="v1",
        document_sha256="2" * 64,
        reported_approval_status=ProtocolApprovalStatus.PENDING,
    )


def _evidence() -> QualificationEvidenceManifest:
    return QualificationEvidenceManifest(
        manifest_id="synthetic-evidence",
        version="v1",
        items=tuple(
            QualificationEvidenceItem(
                requirement=requirement,
                status=EvidenceStatus.UNKNOWN,
            )
            for requirement in EvidenceRequirementKind
        ),
    )


def _package(*, asset: AssetReference | None = None):
    return build_release_evidence_package(
        release_id="synthetic-release",
        version="v1",
        workflow_release_sha256="3" * 64,
        workstation_profile=_profile(),
        protocol_reference=_protocol(),
        evidence_manifest=_evidence(),
        assets=(asset or _asset(),),
    )


def _policy(binding, key_id: str, *, grant_status: GrantStatus = GrantStatus.ACTIVE):
    revocation = (
        {}
        if grant_status == GrantStatus.ACTIVE
        else {"revocation_reference": "revocation-1", "revoked_at": T0}
    )
    grants = (
        SignerRoleGrant(
            scope=AuthorityScope.QUALIFICATION_DECISION,
            role=ApproverRole.ENGINEERING_REVIEWER,
            key_id=key_id,
            binding=binding,
            valid_from=T0 - timedelta(hours=1),
            expires_at=T0 + timedelta(days=1),
            status=grant_status,
            **revocation,
        ),
        SignerRoleGrant(
            scope=AuthorityScope.RELEASE_EVIDENCE,
            role=ApproverRole.RELEASE_REVIEWER,
            key_id=key_id,
            binding=binding,
            valid_from=T0 - timedelta(hours=1),
            expires_at=T0 + timedelta(days=1),
            status=grant_status,
            **revocation,
        ),
    )
    return QualificationTrustPolicy(
        policy_id="synthetic-policy",
        version="v1",
        issued_at=T0 - timedelta(hours=1),
        expires_at=T0 + timedelta(days=1),
        grants=grants,
    )


def _decision(
    binding,
    *,
    outcome: QualificationOutcome = QualificationOutcome.APPROVED,
    sequence: int = 1,
    supersedes: str | None = None,
):
    return QualificationDecision(
        decision_id=f"decision-{sequence}",
        binding=binding,
        sequence=sequence,
        supersedes_decision_sha256=supersedes,
        outcome=outcome,
        approver_role=ApproverRole.ENGINEERING_REVIEWER,
        reason_code="synthetic-test-evidence",
        decided_at=T0,
        expires_at=T0 + timedelta(hours=2),
    )


def test_domain_separation_and_golden_canonical_vectors() -> None:
    asset = _asset()
    content = canonical_domain_bytes(DigestDomain.ASSET_REFERENCE, asset)
    assert content.startswith(b"traceback-domain\x00traceback.asset-reference.v1\x00{")
    assert (
        domain_digest(DigestDomain.ASSET_REFERENCE, asset)
        == "d78dd00861153081698ba69dbedeb027a4de0c2f3903786795bab62fe9090118"
    )
    package = _package(asset=asset)
    assert (
        domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
        == "936dfed797a110cf142a15275e3b239c2017eb62b6597e3b52c3b873bb30a7a9"
    )
    with pytest.raises(ValueError, match="requires matching schema_version"):
        domain_digest(DigestDomain.RELEASE_EVIDENCE, asset)


def test_strict_contracts_reject_unknown_fields_bad_times_and_incomplete_revocation() -> (
    None
):
    with pytest.raises(ValidationError, match="Extra inputs"):
        AssetReference.model_validate(
            {**_asset().model_dump(mode="json"), "path": "/private/asset"}
        )
    with pytest.raises(ValidationError, match="revoked assets require"):
        AssetLifecycle(status=AssetStatus.REVOKED)
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        AssetLifecycle(
            status=AssetStatus.REVOKED,
            revocation_reference="revocation-1",
            revoked_at=datetime(2026, 9, 27, 12, 0),
        )
    for invalid in (True, "128"):
        with pytest.raises(ValidationError):
            AssetContentIdentity(
                asset_id="synthetic-reference",
                version="v1",
                kind=AssetKind.REFERENCE,
                content_sha256="1" * 64,
                content_size_bytes=invalid,
            )
    binding = qualification_binding(_package())
    for invalid in (True, "1"):
        with pytest.raises(ValidationError):
            QualificationDecision(
                **{
                    **_decision(binding).model_dump(),
                    "sequence": invalid,
                }
            )


def test_missing_scientific_evidence_stays_explicit_and_complete() -> None:
    manifest = _evidence()
    assert len(manifest.items) == len(EvidenceRequirementKind)
    assert all(item.status == EvidenceStatus.UNKNOWN for item in manifest.items)
    with pytest.raises(ValidationError, match="every requirement"):
        QualificationEvidenceManifest(
            manifest_id="incomplete",
            version="v1",
            items=manifest.items[:-1],
        )
    with pytest.raises(ValidationError, match="requires id, version, digest"):
        QualificationEvidenceItem(
            requirement=EvidenceRequirementKind.ACCEPTANCE_CRITERIA,
            status=EvidenceStatus.PRESENT,
        )


def test_release_asset_authorization_requires_external_trust_role_head_and_exact_binding() -> (
    None
):
    package = _package()
    binding = qualification_binding(package)
    key = generate_development_keypair(KeyPurpose.RELEASE)
    trust = TrustStore()
    trust.add_signing_key(key)
    policy = _policy(binding, key.key_id)
    envelope = sign_development_release_evidence(
        package,
        key,
        signer_role=ApproverRole.RELEASE_REVIEWER,
    )
    package_digest = domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
    reference = package.assets[0]
    reference_digest = domain_digest(DigestDomain.ASSET_REFERENCE, reference)
    head = ReleaseAuthorityHead(
        release_id=package.release_id,
        release_version=package.version,
        package_sha256=package_digest,
        as_of=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    verified = verify_release_asset_authorization(
        envelope,
        trust,
        policy,
        head,
        expected_binding=binding,
        expected_package_sha256=package_digest,
        expected_asset_id=reference.content.asset_id,
        expected_asset_version=reference.content.version,
        expected_asset_reference_sha256=reference_digest,
        now=T0,
    )
    assert verified.authority_status == AuthorityStatus.VERIFIED
    assert verified.lifecycle_status == AssetLifecycleStatus.ACTIVE
    assert verified.authorized_reference == reference
    assert verified.real_data_authorized is False
    assert verified.qualification_probe_authorized is False

    unknown = verify_release_asset_authorization(
        envelope,
        trust,
        policy,
        None,
        expected_binding=binding,
        expected_package_sha256=package_digest,
        expected_asset_id=reference.content.asset_id,
        expected_asset_version=reference.content.version,
        expected_asset_reference_sha256=reference_digest,
        now=T0,
    )
    assert (
        unknown.authority_status,
        unknown.failure,
        unknown.authorized_reference,
    ) == (
        AuthorityStatus.UNKNOWN,
        AuthorityFailure.AUTHORITY_HEAD_MISSING,
        None,
    )

    wrong_binding = binding.model_copy(update={"workstation_profile_sha256": "f" * 64})
    mismatch = verify_release_asset_authorization(
        envelope,
        trust,
        policy,
        head,
        expected_binding=wrong_binding,
        expected_package_sha256=package_digest,
        expected_asset_id=reference.content.asset_id,
        expected_asset_version=reference.content.version,
        expected_asset_reference_sha256=reference_digest,
        now=T0,
    )
    assert (mismatch.authority_status, mismatch.failure) == (
        AuthorityStatus.INVALID,
        AuthorityFailure.EXPECTED_BINDING_MISMATCH,
    )

    forged_signature = envelope.signature.model_copy(
        update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
    )
    forged = verify_release_asset_authorization(
        envelope.model_copy(update={"signature": forged_signature}),
        trust,
        policy,
        head,
        expected_binding=binding,
        expected_package_sha256=package_digest,
        expected_asset_id=reference.content.asset_id,
        expected_asset_version=reference.content.version,
        expected_asset_reference_sha256=reference_digest,
        now=T0,
    )
    assert (forged.authority_status, forged.failure) == (
        AuthorityStatus.INVALID,
        AuthorityFailure.SIGNATURE_INVALID,
    )


def test_revoked_asset_is_verified_history_but_not_install_authority() -> None:
    package = _package(asset=_asset(status=AssetStatus.REVOKED))
    binding = qualification_binding(package)
    key = generate_development_keypair(KeyPurpose.RELEASE)
    trust = TrustStore()
    trust.add_signing_key(key)
    policy = _policy(binding, key.key_id)
    envelope = sign_development_release_evidence(
        package, key, signer_role=ApproverRole.RELEASE_REVIEWER
    )
    package_digest = domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
    reference = package.assets[0]
    result = verify_release_asset_authorization(
        envelope,
        trust,
        policy,
        ReleaseAuthorityHead(
            release_id=package.release_id,
            release_version=package.version,
            package_sha256=package_digest,
            as_of=T0,
            expires_at=T0 + timedelta(hours=1),
        ),
        expected_binding=binding,
        expected_package_sha256=package_digest,
        expected_asset_id=reference.content.asset_id,
        expected_asset_version=reference.content.version,
        expected_asset_reference_sha256=domain_digest(
            DigestDomain.ASSET_REFERENCE, reference
        ),
        now=T0,
    )
    assert result.authority_status == AuthorityStatus.VERIFIED
    assert result.lifecycle_status == AssetLifecycleStatus.REVOKED
    assert result.authorized_reference is None


@pytest.mark.parametrize(
    ("outcome", "accepted"),
    [
        (QualificationOutcome.APPROVED, True),
        (QualificationOutcome.PENDING, False),
        (QualificationOutcome.REJECTED, False),
        (QualificationOutcome.REVOKED, False),
    ],
)
def test_only_current_approved_decision_is_development_test_evidence(
    outcome, accepted
) -> None:
    binding = qualification_binding(_package())
    key = generate_development_keypair(KeyPurpose.RELEASE)
    trust = TrustStore()
    trust.add_signing_key(key)
    policy = _policy(binding, key.key_id)
    decision = _decision(binding, outcome=outcome)
    envelope = sign_development_qualification_decision(decision, key)
    digest = qualification_decision_digest(decision)
    result = verify_development_qualification_history(
        (envelope,),
        trust,
        policy,
        QualificationAuthorityHead(
            binding=binding,
            latest_sequence=1,
            latest_decision_sha256=digest,
            as_of=T0,
            expires_at=T0 + timedelta(hours=1),
        ),
        expected_binding=binding,
        now=T0,
    )
    assert result.accepted_as_development_test_evidence is accepted
    assert result.real_data_authorized is False
    assert result.qualification_probe_authorized is False


def test_truncated_history_wrong_role_revoked_grant_and_expiry_cannot_approve() -> None:
    binding = qualification_binding(_package())
    key = generate_development_keypair(KeyPurpose.RELEASE)
    trust = TrustStore()
    trust.add_signing_key(key)
    first = _decision(binding)
    second = _decision(
        binding,
        outcome=QualificationOutcome.REVOKED,
        sequence=2,
        supersedes=qualification_decision_digest(first),
    ).model_copy(update={"decided_at": T0 + timedelta(minutes=1)})
    first_envelope = sign_development_qualification_decision(first, key)
    second_envelope = sign_development_qualification_decision(second, key)
    head = QualificationAuthorityHead(
        binding=binding,
        latest_sequence=2,
        latest_decision_sha256=qualification_decision_digest(second),
        as_of=T0 + timedelta(minutes=1),
        expires_at=T0 + timedelta(hours=1),
    )
    truncated = verify_development_qualification_history(
        (first_envelope,),
        trust,
        _policy(binding, key.key_id),
        head,
        expected_binding=binding,
        now=T0 + timedelta(minutes=2),
    )
    assert (
        truncated.authority_status,
        truncated.failure,
        truncated.accepted_as_development_test_evidence,
    ) == (
        AuthorityStatus.UNKNOWN,
        AuthorityFailure.AUTHORITY_HEAD_MISMATCH,
        False,
    )
    current = verify_development_qualification_history(
        (first_envelope, second_envelope),
        trust,
        _policy(binding, key.key_id),
        head,
        expected_binding=binding,
        now=T0 + timedelta(minutes=2),
    )
    assert current.outcome == QualificationOutcome.REVOKED
    assert current.accepted_as_development_test_evidence is False

    wrong_role = first.model_copy(
        update={"approver_role": ApproverRole.SCIENTIFIC_REVIEWER}
    )
    wrong = verify_development_qualification_history(
        (sign_development_qualification_decision(wrong_role, key),),
        trust,
        _policy(binding, key.key_id),
        QualificationAuthorityHead(
            binding=binding,
            latest_sequence=1,
            latest_decision_sha256=qualification_decision_digest(wrong_role),
            as_of=T0,
            expires_at=T0 + timedelta(hours=1),
        ),
        expected_binding=binding,
        now=T0,
    )
    assert wrong.failure == AuthorityFailure.TRUST_POLICY_MISSING_GRANT
    assert wrong.accepted_as_development_test_evidence is False

    revoked = verify_development_qualification_history(
        (first_envelope,),
        trust,
        _policy(binding, key.key_id, grant_status=GrantStatus.REVOKED),
        QualificationAuthorityHead(
            binding=binding,
            latest_sequence=1,
            latest_decision_sha256=qualification_decision_digest(first),
            as_of=T0,
            expires_at=T0 + timedelta(hours=1),
        ),
        expected_binding=binding,
        now=T0,
    )
    assert revoked.failure == AuthorityFailure.TRUST_GRANT_REVOKED
    assert revoked.accepted_as_development_test_evidence is False

    expired = verify_development_qualification_history(
        (first_envelope,),
        trust,
        _policy(binding, key.key_id),
        QualificationAuthorityHead(
            binding=binding,
            latest_sequence=1,
            latest_decision_sha256=qualification_decision_digest(first),
            as_of=T0,
            expires_at=T0 + timedelta(minutes=1),
        ),
        expected_binding=binding,
        now=T0 + timedelta(minutes=1),
    )
    assert expired.failure == AuthorityFailure.AUTHORITY_HEAD_EXPIRED
    assert expired.accepted_as_development_test_evidence is False


def test_qualification_rejects_forgery_wrong_expected_profile_and_decision_expiry_boundary() -> (
    None
):
    binding = qualification_binding(_package())
    key = generate_development_keypair(KeyPurpose.RELEASE)
    trust = TrustStore()
    trust.add_signing_key(key)
    policy = _policy(binding, key.key_id)
    decision = _decision(binding)
    envelope = sign_development_qualification_decision(decision, key)
    digest = qualification_decision_digest(decision)
    head = QualificationAuthorityHead(
        binding=binding,
        latest_sequence=1,
        latest_decision_sha256=digest,
        as_of=T0,
        expires_at=T0 + timedelta(hours=3),
    )
    forged = envelope.model_copy(
        update={
            "signature": envelope.signature.model_copy(
                update={"signature_base64": base64.b64encode(b"x" * 64).decode("ascii")}
            )
        }
    )
    forged_result = verify_development_qualification_history(
        (forged,),
        trust,
        policy,
        head,
        expected_binding=binding,
        now=T0,
    )
    assert (forged_result.authority_status, forged_result.failure) == (
        AuthorityStatus.INVALID,
        AuthorityFailure.SIGNATURE_INVALID,
    )

    wrong_profile = binding.model_copy(update={"workstation_profile_sha256": "f" * 64})
    wrong_result = verify_development_qualification_history(
        (envelope,),
        trust,
        policy,
        head,
        expected_binding=wrong_profile,
        now=T0,
    )
    assert (wrong_result.authority_status, wrong_result.failure) == (
        AuthorityStatus.INVALID,
        AuthorityFailure.EXPECTED_BINDING_MISMATCH,
    )

    expired_result = verify_development_qualification_history(
        (envelope,),
        trust,
        policy,
        head,
        expected_binding=binding,
        now=decision.expires_at,
    )
    assert (expired_result.authority_status, expired_result.failure) == (
        AuthorityStatus.UNKNOWN,
        AuthorityFailure.DECISION_EXPIRED,
    )
    assert expired_result.accepted_as_development_test_evidence is False
