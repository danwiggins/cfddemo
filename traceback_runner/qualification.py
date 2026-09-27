"""Development-only release and qualification authority verification.

Signatures prove exact bytes. Approval additionally requires externally supplied
key trust, role/scope policy, expected binding, and a fresh authoritative head.
No result from this module authorizes real input or qualification execution.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Identifier, RunnerContract
from .release_evidence import (
    AssetReference,
    AssetStatus,
    DigestDomain,
    ReleaseEvidencePackage,
    Sha256,
    canonical_domain_bytes,
    domain_digest,
    require_aware_utc,
)
from .signing import (
    DevelopmentSigningKey,
    KeyPurpose,
    SignatureEnvelope,
    SigningError,
    TrustStore,
    UnknownKeyError,
    sign_bytes,
    verify_signature,
)

PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


class AuthorityScope(StrEnum):
    RELEASE_EVIDENCE = "release-evidence"
    QUALIFICATION_DECISION = "qualification-decision"


class ApproverRole(StrEnum):
    RELEASE_REVIEWER = "release-reviewer"
    ENGINEERING_REVIEWER = "engineering-reviewer"
    SCIENTIFIC_REVIEWER = "scientific-reviewer"


class GrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AuthorityStatus(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class AssetLifecycleStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class AuthorityFailure(StrEnum):
    NONE = "none"
    AUTHORITY_HEAD_MISSING = "authority-head-missing"
    AUTHORITY_HEAD_EXPIRED = "authority-head-expired"
    AUTHORITY_HEAD_NOT_YET_VALID = "authority-head-not-yet-valid"
    AUTHORITY_HEAD_MISMATCH = "authority-head-mismatch"
    EXPECTED_BINDING_MISMATCH = "expected-binding-mismatch"
    EXPECTED_ASSET_MISMATCH = "expected-asset-mismatch"
    TRUST_POLICY_EXPIRED = "trust-policy-expired"
    TRUST_POLICY_NOT_YET_VALID = "trust-policy-not-yet-valid"
    TRUST_POLICY_MISSING_GRANT = "trust-policy-missing-grant"
    TRUST_GRANT_REVOKED = "trust-grant-revoked"
    TRUST_GRANT_EXPIRED = "trust-grant-expired"
    UNKNOWN_SIGNER = "unknown-signer"
    SIGNATURE_INVALID = "signature-invalid"
    DECISION_CHAIN_INVALID = "decision-chain-invalid"
    DECISION_EXPIRED = "decision-expired"
    DECISION_NOT_YET_VALID = "decision-not-yet-valid"


class QualificationOutcome(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class QualificationBinding(RunnerContract):
    schema_version: Literal["traceback.qualification-binding.v1"] = (
        "traceback.qualification-binding.v1"
    )
    release_id: Identifier
    release_version: Identifier
    release_evidence_sha256: Sha256
    workstation_profile_id: Identifier
    workstation_profile_version: Identifier
    workstation_profile_sha256: Sha256
    protocol_id: Identifier
    protocol_version: Identifier
    protocol_reference_sha256: Sha256
    evidence_manifest_id: Identifier
    evidence_manifest_version: Identifier
    evidence_manifest_sha256: Sha256


def qualification_binding(package: ReleaseEvidencePackage) -> QualificationBinding:
    return QualificationBinding(
        release_id=package.release_id,
        release_version=package.version,
        release_evidence_sha256=domain_digest(DigestDomain.RELEASE_EVIDENCE, package),
        workstation_profile_id=package.workstation_profile_id,
        workstation_profile_version=package.workstation_profile_version,
        workstation_profile_sha256=package.workstation_profile_sha256,
        protocol_id=package.protocol_id,
        protocol_version=package.protocol_version,
        protocol_reference_sha256=package.protocol_reference_sha256,
        evidence_manifest_id=package.evidence_manifest_id,
        evidence_manifest_version=package.evidence_manifest_version,
        evidence_manifest_sha256=package.evidence_manifest_sha256,
    )


class SignerRoleGrant(RunnerContract):
    scope: AuthorityScope
    role: ApproverRole
    key_id: Identifier
    binding: QualificationBinding
    valid_from: datetime
    expires_at: datetime
    status: GrantStatus
    revocation_reference: Identifier | None = None
    revoked_at: datetime | None = None

    @field_validator("valid_from", "expires_at", "revoked_at")
    @classmethod
    def timestamps_are_utc(
        cls, value: datetime | None, info: object
    ) -> datetime | None:
        if value is None:
            return None
        name = getattr(info, "field_name", "timestamp")
        return require_aware_utc(value, field_name=name)

    @model_validator(mode="after")
    def valid_window_and_revocation(self) -> SignerRoleGrant:
        if self.expires_at <= self.valid_from:
            raise ValueError("grant expires_at must be after valid_from")
        has_reference = self.revocation_reference is not None
        has_time = self.revoked_at is not None
        if self.status == GrantStatus.ACTIVE and (has_reference or has_time):
            raise ValueError("active grants forbid revocation metadata")
        if self.status == GrantStatus.REVOKED and not (has_reference and has_time):
            raise ValueError("revoked grants require complete revocation metadata")
        return self


class QualificationTrustPolicy(RunnerContract):
    schema_version: Literal["traceback.qualification-trust-policy.v1"] = (
        "traceback.qualification-trust-policy.v1"
    )
    policy_id: Identifier
    version: Identifier
    issued_at: datetime
    expires_at: datetime
    grants: tuple[SignerRoleGrant, ...]
    synthetic_only: Literal[True] = True

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return require_aware_utc(
            value, field_name=getattr(info, "field_name", "timestamp")
        )

    @model_validator(mode="after")
    def ordered_unique_grants(self) -> QualificationTrustPolicy:
        if self.expires_at <= self.issued_at:
            raise ValueError("policy expires_at must be after issued_at")
        keys = tuple(
            (
                item.scope.value,
                item.role.value,
                item.key_id,
                item.binding.release_evidence_sha256,
            )
            for item in self.grants
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError(
                "grants must be uniquely sorted by scope, role, key, and release"
            )
        return self


class ReleaseEvidenceEnvelope(RunnerContract):
    schema_version: Literal["traceback.release-evidence-envelope.v1"] = (
        "traceback.release-evidence-envelope.v1"
    )
    package: ReleaseEvidencePackage
    signer_role: ApproverRole
    signature: SignatureEnvelope


class ReleaseAuthorityHead(RunnerContract):
    schema_version: Literal["traceback.release-authority-head.v1"] = (
        "traceback.release-authority-head.v1"
    )
    release_id: Identifier
    release_version: Identifier
    package_sha256: Sha256
    as_of: datetime
    expires_at: datetime

    @field_validator("as_of", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return require_aware_utc(
            value, field_name=getattr(info, "field_name", "timestamp")
        )

    @model_validator(mode="after")
    def expiry_follows_snapshot(self) -> ReleaseAuthorityHead:
        if self.expires_at <= self.as_of:
            raise ValueError("authority head expires_at must be after as_of")
        return self


class AssetAuthorizationDecision(RunnerContract):
    schema_version: Literal["traceback.asset-authorization-decision.v1"] = (
        "traceback.asset-authorization-decision.v1"
    )
    authority_status: AuthorityStatus
    lifecycle_status: AssetLifecycleStatus
    failure: AuthorityFailure
    release_id: Identifier
    release_version: Identifier
    package_sha256: Sha256
    asset_id: Identifier
    asset_version: Identifier
    asset_reference_sha256: Sha256
    verified_as_of: datetime | None = None
    fresh_until: datetime | None = None
    authorized_reference: AssetReference | None = None
    real_data_authorized: Literal[False] = False
    qualification_probe_authorized: Literal[False] = False

    @field_validator("verified_as_of", "fresh_until")
    @classmethod
    def timestamps_are_utc(
        cls, value: datetime | None, info: object
    ) -> datetime | None:
        if value is None:
            return None
        return require_aware_utc(
            value, field_name=getattr(info, "field_name", "timestamp")
        )

    @model_validator(mode="after")
    def authorized_reference_only_on_success(self) -> AssetAuthorizationDecision:
        successful = (
            self.authority_status == AuthorityStatus.VERIFIED
            and self.lifecycle_status == AssetLifecycleStatus.ACTIVE
        )
        if successful != (self.authorized_reference is not None):
            raise ValueError(
                "authorized_reference exists only for verified active authority"
            )
        if successful and self.failure != AuthorityFailure.NONE:
            raise ValueError("successful authorization cannot contain a failure")
        return self


class QualificationDecision(RunnerContract):
    schema_version: Literal["traceback.qualification-decision.v1"] = (
        "traceback.qualification-decision.v1"
    )
    decision_id: Identifier
    binding: QualificationBinding
    sequence: PositiveStrictInt
    supersedes_decision_sha256: Sha256 | None = None
    outcome: QualificationOutcome
    approver_role: ApproverRole
    reason_code: Identifier
    decided_at: datetime
    expires_at: datetime
    synthetic_only: Literal[True] = True
    real_data_authorized: Literal[False] = False
    qualification_probe_authorized: Literal[False] = False

    @field_validator("decided_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return require_aware_utc(
            value, field_name=getattr(info, "field_name", "timestamp")
        )

    @model_validator(mode="after")
    def sequence_and_expiry(self) -> QualificationDecision:
        if self.expires_at <= self.decided_at:
            raise ValueError("decision expires_at must be after decided_at")
        if self.sequence == 1 and self.supersedes_decision_sha256 is not None:
            raise ValueError("first decision cannot supersede another decision")
        if self.sequence > 1 and self.supersedes_decision_sha256 is None:
            raise ValueError("later decisions must bind the superseded decision digest")
        return self


class QualificationDecisionEnvelope(RunnerContract):
    schema_version: Literal["traceback.qualification-decision-envelope.v1"] = (
        "traceback.qualification-decision-envelope.v1"
    )
    decision: QualificationDecision
    signature: SignatureEnvelope


class QualificationAuthorityHead(RunnerContract):
    schema_version: Literal["traceback.qualification-authority-head.v1"] = (
        "traceback.qualification-authority-head.v1"
    )
    binding: QualificationBinding
    latest_sequence: PositiveStrictInt
    latest_decision_sha256: Sha256
    as_of: datetime
    expires_at: datetime

    @field_validator("as_of", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return require_aware_utc(
            value, field_name=getattr(info, "field_name", "timestamp")
        )

    @model_validator(mode="after")
    def expiry_follows_snapshot(self) -> QualificationAuthorityHead:
        if self.expires_at <= self.as_of:
            raise ValueError("authority head expires_at must be after as_of")
        return self


class QualificationVerification(RunnerContract):
    schema_version: Literal["traceback.qualification-verification.v1"] = (
        "traceback.qualification-verification.v1"
    )
    authority_status: AuthorityStatus
    failure: AuthorityFailure
    outcome: QualificationOutcome | None
    accepted_as_development_test_evidence: bool
    binding: QualificationBinding
    latest_sequence: int | None = None
    latest_decision_sha256: Sha256 | None = None
    verified_as_of: datetime | None = None
    fresh_until: datetime | None = None
    real_data_authorized: Literal[False] = False
    qualification_probe_authorized: Literal[False] = False

    @model_validator(mode="after")
    def acceptance_requires_current_approved_authority(
        self,
    ) -> QualificationVerification:
        can_accept = (
            self.authority_status == AuthorityStatus.VERIFIED
            and self.outcome == QualificationOutcome.APPROVED
        )
        if self.accepted_as_development_test_evidence != can_accept:
            raise ValueError(
                "only verified current approval is accepted as development test evidence"
            )
        return self


def release_evidence_bytes(package: ReleaseEvidencePackage) -> bytes:
    return canonical_domain_bytes(DigestDomain.RELEASE_EVIDENCE, package)


def qualification_decision_bytes(decision: QualificationDecision) -> bytes:
    return canonical_domain_bytes(DigestDomain.QUALIFICATION_DECISION, decision)


def qualification_decision_digest(decision: QualificationDecision) -> str:
    return domain_digest(DigestDomain.QUALIFICATION_DECISION, decision)


def sign_development_release_evidence(
    package: ReleaseEvidencePackage,
    key: DevelopmentSigningKey,
    *,
    signer_role: ApproverRole,
) -> ReleaseEvidenceEnvelope:
    return ReleaseEvidenceEnvelope(
        package=package,
        signer_role=signer_role,
        signature=sign_bytes(
            release_evidence_bytes(package), key, purpose=KeyPurpose.RELEASE
        ),
    )


def sign_development_qualification_decision(
    decision: QualificationDecision,
    key: DevelopmentSigningKey,
) -> QualificationDecisionEnvelope:
    return QualificationDecisionEnvelope(
        decision=decision,
        signature=sign_bytes(
            qualification_decision_bytes(decision), key, purpose=KeyPurpose.RELEASE
        ),
    )


def _find_grant(
    policy: QualificationTrustPolicy,
    *,
    scope: AuthorityScope,
    role: ApproverRole,
    key_id: str,
    binding: QualificationBinding,
) -> SignerRoleGrant | None:
    return next(
        (
            grant
            for grant in policy.grants
            if grant.scope == scope
            and grant.role == role
            and grant.key_id == key_id
            and grant.binding == binding
        ),
        None,
    )


def _grant_failure(
    grant: SignerRoleGrant | None,
    *,
    policy: QualificationTrustPolicy,
    event_at: datetime,
    now: datetime,
) -> AuthorityFailure:
    if now < policy.issued_at:
        return AuthorityFailure.TRUST_POLICY_NOT_YET_VALID
    if now >= policy.expires_at:
        return AuthorityFailure.TRUST_POLICY_EXPIRED
    if grant is None:
        return AuthorityFailure.TRUST_POLICY_MISSING_GRANT
    if grant.status == GrantStatus.REVOKED:
        return AuthorityFailure.TRUST_GRANT_REVOKED
    if (
        event_at < grant.valid_from
        or event_at >= grant.expires_at
        or now >= grant.expires_at
    ):
        return AuthorityFailure.TRUST_GRANT_EXPIRED
    return AuthorityFailure.NONE


def _asset_result(
    *,
    status: AuthorityStatus,
    lifecycle: AssetLifecycleStatus,
    failure: AuthorityFailure,
    package: ReleaseEvidencePackage,
    package_sha256: str,
    asset_id: str,
    asset_version: str,
    asset_reference_sha256: str,
    head: ReleaseAuthorityHead | None,
    reference: AssetReference | None = None,
) -> AssetAuthorizationDecision:
    return AssetAuthorizationDecision(
        authority_status=status,
        lifecycle_status=lifecycle,
        failure=failure,
        release_id=package.release_id,
        release_version=package.version,
        package_sha256=package_sha256,
        asset_id=asset_id,
        asset_version=asset_version,
        asset_reference_sha256=asset_reference_sha256,
        verified_as_of=head.as_of if head is not None else None,
        fresh_until=head.expires_at if head is not None else None,
        authorized_reference=reference,
    )


def verify_release_asset_authorization(
    envelope: ReleaseEvidenceEnvelope,
    trust_store: TrustStore,
    role_policy: QualificationTrustPolicy,
    authority_head: ReleaseAuthorityHead | None,
    *,
    expected_binding: QualificationBinding,
    expected_package_sha256: str,
    expected_asset_id: str,
    expected_asset_version: str,
    expected_asset_reference_sha256: str,
    now: datetime,
) -> AssetAuthorizationDecision:
    """Verify a current signed release asset reference; never authorize execution."""

    now = require_aware_utc(now, field_name="now")
    package = envelope.package
    package_sha256 = domain_digest(DigestDomain.RELEASE_EVIDENCE, package)
    common = dict(
        package=package,
        package_sha256=package_sha256,
        asset_id=expected_asset_id,
        asset_version=expected_asset_version,
        asset_reference_sha256=expected_asset_reference_sha256,
    )
    if (
        package_sha256 != expected_package_sha256
        or qualification_binding(package) != expected_binding
    ):
        return _asset_result(
            status=AuthorityStatus.INVALID,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.EXPECTED_BINDING_MISMATCH,
            head=authority_head,
            **common,
        )
    if authority_head is None:
        return _asset_result(
            status=AuthorityStatus.UNKNOWN,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_MISSING,
            head=None,
            **common,
        )
    if now >= authority_head.expires_at:
        return _asset_result(
            status=AuthorityStatus.UNKNOWN,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_EXPIRED,
            head=authority_head,
            **common,
        )
    if now < authority_head.as_of:
        return _asset_result(
            status=AuthorityStatus.UNKNOWN,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_NOT_YET_VALID,
            head=authority_head,
            **common,
        )
    if (
        authority_head.release_id,
        authority_head.release_version,
        authority_head.package_sha256,
    ) != (package.release_id, package.version, package_sha256):
        return _asset_result(
            status=AuthorityStatus.UNKNOWN,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_MISMATCH,
            head=authority_head,
            **common,
        )
    reference = next(
        (
            item
            for item in package.assets
            if (item.content.asset_id, item.content.version)
            == (expected_asset_id, expected_asset_version)
        ),
        None,
    )
    if (
        reference is None
        or domain_digest(DigestDomain.ASSET_REFERENCE, reference)
        != expected_asset_reference_sha256
    ):
        return _asset_result(
            status=AuthorityStatus.INVALID,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.EXPECTED_ASSET_MISMATCH,
            head=authority_head,
            **common,
        )
    grant = _find_grant(
        role_policy,
        scope=AuthorityScope.RELEASE_EVIDENCE,
        role=envelope.signer_role,
        key_id=envelope.signature.key_id,
        binding=expected_binding,
    )
    failure = _grant_failure(
        grant, policy=role_policy, event_at=authority_head.as_of, now=now
    )
    if failure != AuthorityFailure.NONE:
        status = (
            AuthorityStatus.UNKNOWN
            if failure
            in {
                AuthorityFailure.TRUST_POLICY_MISSING_GRANT,
                AuthorityFailure.TRUST_POLICY_EXPIRED,
                AuthorityFailure.TRUST_POLICY_NOT_YET_VALID,
            }
            else AuthorityStatus.INVALID
        )
        return _asset_result(
            status=status,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=failure,
            head=authority_head,
            **common,
        )
    try:
        verify_signature(
            release_evidence_bytes(package),
            envelope.signature,
            trust_store,
            purpose=KeyPurpose.RELEASE,
        )
    except UnknownKeyError:
        return _asset_result(
            status=AuthorityStatus.UNKNOWN,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.UNKNOWN_SIGNER,
            head=authority_head,
            **common,
        )
    except SigningError:
        return _asset_result(
            status=AuthorityStatus.INVALID,
            lifecycle=AssetLifecycleStatus.UNKNOWN,
            failure=AuthorityFailure.SIGNATURE_INVALID,
            head=authority_head,
            **common,
        )
    if reference.lifecycle.status == AssetStatus.REVOKED:
        return _asset_result(
            status=AuthorityStatus.VERIFIED,
            lifecycle=AssetLifecycleStatus.REVOKED,
            failure=AuthorityFailure.NONE,
            head=authority_head,
            **common,
        )
    return _asset_result(
        status=AuthorityStatus.VERIFIED,
        lifecycle=AssetLifecycleStatus.ACTIVE,
        failure=AuthorityFailure.NONE,
        head=authority_head,
        reference=reference,
        **common,
    )


def _qualification_result(
    *,
    status: AuthorityStatus,
    failure: AuthorityFailure,
    binding: QualificationBinding,
    outcome: QualificationOutcome | None = None,
    latest: QualificationDecision | None = None,
    head: QualificationAuthorityHead | None = None,
) -> QualificationVerification:
    return QualificationVerification(
        authority_status=status,
        failure=failure,
        outcome=outcome,
        accepted_as_development_test_evidence=status == AuthorityStatus.VERIFIED
        and outcome == QualificationOutcome.APPROVED,
        binding=binding,
        latest_sequence=latest.sequence if latest else None,
        latest_decision_sha256=qualification_decision_digest(latest)
        if latest
        else None,
        verified_as_of=head.as_of if head else None,
        fresh_until=head.expires_at if head else None,
    )


def verify_development_qualification_history(
    history: tuple[QualificationDecisionEnvelope, ...],
    trust_store: TrustStore,
    role_policy: QualificationTrustPolicy,
    authority_head: QualificationAuthorityHead | None,
    *,
    expected_binding: QualificationBinding,
    now: datetime,
) -> QualificationVerification:
    """Verify append-only decisions against an independently trusted current head."""

    now = require_aware_utc(now, field_name="now")
    if authority_head is None:
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_MISSING,
            binding=expected_binding,
        )
    if now >= authority_head.expires_at:
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_EXPIRED,
            binding=expected_binding,
            head=authority_head,
        )
    if now < authority_head.as_of:
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_NOT_YET_VALID,
            binding=expected_binding,
            head=authority_head,
        )
    if authority_head.binding != expected_binding:
        return _qualification_result(
            status=AuthorityStatus.INVALID,
            failure=AuthorityFailure.EXPECTED_BINDING_MISMATCH,
            binding=expected_binding,
            head=authority_head,
        )
    if not history:
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_MISMATCH,
            binding=expected_binding,
            head=authority_head,
        )
    previous: QualificationDecision | None = None
    for envelope in history:
        decision = envelope.decision
        if decision.binding != expected_binding:
            return _qualification_result(
                status=AuthorityStatus.INVALID,
                failure=AuthorityFailure.EXPECTED_BINDING_MISMATCH,
                binding=expected_binding,
                head=authority_head,
            )
        if decision.decided_at > authority_head.as_of:
            return _qualification_result(
                status=AuthorityStatus.INVALID,
                failure=AuthorityFailure.DECISION_NOT_YET_VALID,
                binding=expected_binding,
                head=authority_head,
            )
        if decision.decided_at > now:
            return _qualification_result(
                status=AuthorityStatus.INVALID,
                failure=AuthorityFailure.DECISION_NOT_YET_VALID,
                binding=expected_binding,
                head=authority_head,
            )
        if previous is None:
            chain_ok = (
                decision.sequence == 1 and decision.supersedes_decision_sha256 is None
            )
        else:
            chain_ok = (
                decision.sequence == previous.sequence + 1
                and decision.supersedes_decision_sha256
                == qualification_decision_digest(previous)
                and decision.decided_at >= previous.decided_at
            )
        if not chain_ok:
            return _qualification_result(
                status=AuthorityStatus.INVALID,
                failure=AuthorityFailure.DECISION_CHAIN_INVALID,
                binding=expected_binding,
                head=authority_head,
            )
        grant = _find_grant(
            role_policy,
            scope=AuthorityScope.QUALIFICATION_DECISION,
            role=decision.approver_role,
            key_id=envelope.signature.key_id,
            binding=expected_binding,
        )
        failure = _grant_failure(
            grant, policy=role_policy, event_at=decision.decided_at, now=now
        )
        if failure != AuthorityFailure.NONE:
            status = (
                AuthorityStatus.UNKNOWN
                if failure
                in {
                    AuthorityFailure.TRUST_POLICY_MISSING_GRANT,
                    AuthorityFailure.TRUST_POLICY_EXPIRED,
                    AuthorityFailure.TRUST_POLICY_NOT_YET_VALID,
                }
                else AuthorityStatus.INVALID
            )
            return _qualification_result(
                status=status,
                failure=failure,
                binding=expected_binding,
                head=authority_head,
            )
        try:
            verify_signature(
                qualification_decision_bytes(decision),
                envelope.signature,
                trust_store,
                purpose=KeyPurpose.RELEASE,
            )
        except UnknownKeyError:
            return _qualification_result(
                status=AuthorityStatus.UNKNOWN,
                failure=AuthorityFailure.UNKNOWN_SIGNER,
                binding=expected_binding,
                head=authority_head,
            )
        except SigningError:
            return _qualification_result(
                status=AuthorityStatus.INVALID,
                failure=AuthorityFailure.SIGNATURE_INVALID,
                binding=expected_binding,
                head=authority_head,
            )
        previous = decision
    latest = history[-1].decision
    if (latest.sequence, qualification_decision_digest(latest)) != (
        authority_head.latest_sequence,
        authority_head.latest_decision_sha256,
    ):
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.AUTHORITY_HEAD_MISMATCH,
            binding=expected_binding,
            latest=latest,
            head=authority_head,
        )
    if now >= latest.expires_at:
        return _qualification_result(
            status=AuthorityStatus.UNKNOWN,
            failure=AuthorityFailure.DECISION_EXPIRED,
            binding=expected_binding,
            latest=latest,
            head=authority_head,
        )
    return _qualification_result(
        status=AuthorityStatus.VERIFIED,
        failure=AuthorityFailure.NONE,
        binding=expected_binding,
        outcome=latest.outcome,
        latest=latest,
        head=authority_head,
    )


__all__ = [
    "ApproverRole",
    "AssetAuthorizationDecision",
    "AssetLifecycleStatus",
    "AuthorityFailure",
    "AuthorityScope",
    "AuthorityStatus",
    "GrantStatus",
    "QualificationAuthorityHead",
    "QualificationBinding",
    "QualificationDecision",
    "QualificationDecisionEnvelope",
    "QualificationOutcome",
    "QualificationTrustPolicy",
    "QualificationVerification",
    "ReleaseAuthorityHead",
    "ReleaseEvidenceEnvelope",
    "SignerRoleGrant",
    "qualification_binding",
    "qualification_decision_bytes",
    "qualification_decision_digest",
    "release_evidence_bytes",
    "sign_development_qualification_decision",
    "sign_development_release_evidence",
    "verify_development_qualification_history",
    "verify_release_asset_authorization",
]
