"""Protected provider-local linkage and external authority contracts.

This module defines the D01/E12 identity boundary.  It deliberately contains no
storage, identity discovery, cohort construction, or scientific compatibility
logic.  Callers must supply an independently provisioned provider trust
snapshot and exact signed approvals.  Missing or invalid authority fails closed.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
)

MAX_ISSUERS = 16
MAX_APPROVALS = 8
MAX_REVISIONS = 10_000
MAX_CONSUMED_APPROVALS = 100_000


def _opaque_token(prefix: str):
    return Annotated[
        str,
        StringConstraints(pattern=rf"^{prefix}[0-9a-f]{{32}}$"),
    ]


ProviderNamespace = _opaque_token("provider_")
SubjectToken = _opaque_token("subject_")
CollectionToken = _opaque_token("collection_")
SpecimenToken = _opaque_token("specimen_")
AliquotToken = _opaque_token("aliquot_")
RunToken = _opaque_token("run_")
AnalysisRecordId = _opaque_token("analysis_")
MeasurementId = _opaque_token("measurement_")
LinkageId = _opaque_token("linkage_")
IssuerId = _opaque_token("issuer_")
PrincipalId = _opaque_token("principal_")
ApprovalId = _opaque_token("approval_")
TrustSnapshotId = _opaque_token("trust_")
SourceProjectionRef = _opaque_token("projection_")
KeyId = _opaque_token("key_")
Nonce = _opaque_token("nonce_")
Base64PublicKey = Annotated[
    str,
    StringConstraints(min_length=44, max_length=44),
    AfterValidator(lambda v: _b64(v, 32)),
]
Base64Signature = Annotated[
    str,
    StringConstraints(min_length=88, max_length=88),
    AfterValidator(lambda v: _b64(v, 64)),
]


def _b64(value: str, length: int) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("cryptographic value is not canonical base64") from exc
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("cryptographic value has an invalid length or encoding")
    return value


def _utc_second(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be an aware UTC timestamp")
    if value.microsecond:
        raise ValueError(f"{field} must use whole-second precision")
    return value


class LinkageContract(RegistryContract):
    """Strict immutable base for provider-local identity contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
        strict=True,
    )


class OptionalLineageState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class OptionalOpaqueToken(LinkageContract):
    """An optional lineage link that cannot silently become an empty string."""

    state: OptionalLineageState
    token: str | None = Field(default=None, pattern=r"^[a-z]+_[0-9a-f]{32}$")

    @model_validator(mode="after")
    def state_matches_token(self) -> OptionalOpaqueToken:
        if (self.state == OptionalLineageState.KNOWN) != (self.token is not None):
            raise ValueError(
                "known lineage requires a token and unknown lineage forbids one"
            )
        return self


class UnitOfAnalysis(StrEnum):
    SUBJECT = "subject"
    COLLECTION = "collection"
    SPECIMEN = "specimen"


class BiologicalLineage(LinkageContract):
    """Biological identities; technical reruns never create a new collection."""

    subject_token: SubjectToken
    collection_token: CollectionToken
    specimen_token: SpecimenToken
    aliquot: OptionalOpaqueToken

    @model_validator(mode="after")
    def aliquot_has_expected_type(self) -> BiologicalLineage:
        if self.aliquot.token is not None and not self.aliquot.token.startswith(
            "aliquot_"
        ):
            raise ValueError("aliquot lineage token has the wrong type")
        return self


class TechnicalLineage(LinkageContract):
    """Technical processing identity, separate from the biological collection."""

    run: OptionalOpaqueToken
    analysis_record_id: AnalysisRecordId
    measurement_id: MeasurementId
    reanalysis_of: OptionalOpaqueToken

    @model_validator(mode="after")
    def typed_optional_links(self) -> TechnicalLineage:
        if self.run.token is not None and not self.run.token.startswith("run_"):
            raise ValueError("run lineage token has the wrong type")
        if (
            self.reanalysis_of.token is not None
            and not self.reanalysis_of.token.startswith("analysis_")
        ):
            raise ValueError("reanalysis lineage token has the wrong type")
        if self.reanalysis_of.token == self.analysis_record_id:
            raise ValueError("an analysis cannot be its own reanalysis source")
        return self


class LinkageOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    TOMBSTONE = "tombstone"


class LinkageReasonCode(StrEnum):
    INITIAL_PROJECTION = "initial_projection"
    WRONG_SUBJECT = "wrong_subject"
    WRONG_COLLECTION = "wrong_collection"
    WRONG_SPECIMEN = "wrong_specimen"
    TECHNICAL_LINEAGE_CORRECTION = "technical_lineage_correction"
    SOURCE_AUTHORITY_UPDATE = "source_authority_update"
    RETENTION_TOMBSTONE = "retention_tombstone"


class LinkageRevision(LinkageContract):
    """One immutable proposal in an append-only provider-local linkage chain."""

    schema_version: Literal["traceback.provider-linkage-revision.v1"] = (
        "traceback.provider-linkage-revision.v1"
    )
    linkage_id: LinkageId
    provider_namespace: ProviderNamespace
    revision: int = Field(ge=1, le=MAX_REVISIONS)
    previous_revision_sha256: Sha256 | None
    operation: LinkageOperation
    reason_code: LinkageReasonCode
    source_projection_ref: SourceProjectionRef
    unit_of_analysis: UnitOfAnalysis
    biological: BiologicalLineage
    technical: TechnicalLineage
    proposed_at: datetime

    @model_validator(mode="after")
    def coherent_revision(self) -> LinkageRevision:
        _utc_second(self.proposed_at, "proposed_at")
        initial = self.operation == LinkageOperation.CREATE
        if initial != (self.revision == 1 and self.previous_revision_sha256 is None):
            raise ValueError("create must be revision one without a predecessor")
        if initial != (self.reason_code == LinkageReasonCode.INITIAL_PROJECTION):
            raise ValueError("initial projection reason is allowed only for create")
        if self.operation == LinkageOperation.TOMBSTONE:
            if self.reason_code != LinkageReasonCode.RETENTION_TOMBSTONE:
                raise ValueError("tombstone requires retention_tombstone reason")
        elif self.reason_code == LinkageReasonCode.RETENTION_TOMBSTONE:
            raise ValueError("retention_tombstone reason requires tombstone operation")
        return self


class ProviderRole(StrEnum):
    LINKER = "linker"
    REVIEWER = "reviewer"


class ApprovalPurpose(StrEnum):
    CREATE_LINKAGE = "create_linkage"
    CORRECT_LINKAGE = "correct_linkage"
    TOMBSTONE_LINKAGE = "tombstone_linkage"


class IssuerStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ProviderIssuerTrust(LinkageContract):
    issuer_id: IssuerId
    key_id: KeyId
    public_key_base64: Base64PublicKey
    status: IssuerStatus
    allowed_roles: tuple[ProviderRole, ...] = Field(min_length=1, max_length=2)
    allowed_purposes: tuple[ApprovalPurpose, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def sorted_unique_grants(self) -> ProviderIssuerTrust:
        if self.allowed_roles != tuple(sorted(set(self.allowed_roles), key=str)):
            raise ValueError("issuer roles must be uniquely sorted")
        if self.allowed_purposes != tuple(sorted(set(self.allowed_purposes), key=str)):
            raise ValueError("issuer purposes must be uniquely sorted")
        return self


class ProviderTrustSnapshot(LinkageContract):
    """Independently provisioned public trust; never derived from an approval."""

    schema_version: Literal["traceback.provider-trust-snapshot.v1"] = (
        "traceback.provider-trust-snapshot.v1"
    )
    snapshot_id: TrustSnapshotId
    provider_namespace: ProviderNamespace
    revision: int = Field(ge=1, le=MAX_REVISIONS)
    previous_snapshot_sha256: Sha256 | None
    issued_at: datetime
    expires_at: datetime
    issuers: tuple[ProviderIssuerTrust, ...] = Field(
        min_length=1, max_length=MAX_ISSUERS
    )

    @model_validator(mode="after")
    def coherent_snapshot(self) -> ProviderTrustSnapshot:
        _utc_second(self.issued_at, "trust issued_at")
        _utc_second(self.expires_at, "trust expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("trust snapshot must expire after issuance")
        if (self.revision == 1) != (self.previous_snapshot_sha256 is None):
            raise ValueError("only trust revision one may omit its predecessor")
        keys = [(item.issuer_id, item.key_id) for item in self.issuers]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("trusted issuers must be uniquely sorted")
        return self


class ProviderApprovalPayload(LinkageContract):
    schema_version: Literal["traceback.provider-linkage-approval.v1"] = (
        "traceback.provider-linkage-approval.v1"
    )
    approval_id: ApprovalId
    provider_namespace: ProviderNamespace
    issuer_id: IssuerId
    key_id: KeyId
    principal_id: PrincipalId
    role: ProviderRole
    purpose: ApprovalPurpose
    proposed_revision_sha256: Sha256
    trust_snapshot_id: TrustSnapshotId
    trust_snapshot_revision: int = Field(ge=1, le=MAX_REVISIONS)
    trust_snapshot_sha256: Sha256
    nonce: Nonce
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def coherent_window(self) -> ProviderApprovalPayload:
        _utc_second(self.issued_at, "approval issued_at")
        _utc_second(self.expires_at, "approval expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval must expire after issuance")
        return self


class SignedProviderApproval(LinkageContract):
    payload: ProviderApprovalPayload
    signature_base64: Base64Signature


class LinkageAuthorityReason(StrEnum):
    AUTHORIZED = "authorized"
    AUTHORITY_ABSENT = "authority_absent"
    TRUST_HEAD_MISMATCH = "trust_head_mismatch"
    TRUST_EXPIRED = "trust_expired"
    PROVIDER_MISMATCH = "provider_mismatch"
    APPROVAL_COUNT_INVALID = "approval_count_invalid"
    APPROVAL_REUSED = "approval_reused"
    DUPLICATE_PRINCIPAL = "duplicate_principal"
    REVISION_BINDING_MISMATCH = "revision_binding_mismatch"
    PURPOSE_MISMATCH = "purpose_mismatch"
    ROLE_REQUIREMENTS_UNMET = "role_requirements_unmet"
    APPROVAL_NOT_CURRENT = "approval_not_current"
    ISSUER_UNKNOWN = "issuer_unknown"
    ISSUER_REVOKED = "issuer_revoked"
    ISSUER_GRANT_MISMATCH = "issuer_grant_mismatch"
    SIGNATURE_INVALID = "signature_invalid"
    REVISION_CHAIN_INVALID = "revision_chain_invalid"
    CORRECTION_DELTA_INVALID = "correction_delta_invalid"
    TRUST_BINDING_MISMATCH = "trust_binding_mismatch"


class LinkageAuthorizationDecision(LinkageContract):
    schema_version: Literal["traceback.linkage-authorization-decision.v1"] = (
        "traceback.linkage-authorization-decision.v1"
    )
    proposed_revision_sha256: Sha256
    trust_snapshot_sha256: Sha256 | None
    evaluated_at: datetime
    linkage_authorized: bool
    comparison_linkage_eligible: Literal[False] = False
    reason_codes: tuple[LinkageAuthorityReason, ...] = Field(min_length=1)
    approval_ids: tuple[ApprovalId, ...] = Field(max_length=MAX_APPROVALS)
    principal_ids: tuple[PrincipalId, ...] = Field(max_length=MAX_APPROVALS)

    @model_validator(mode="after")
    def coherent_decision(self) -> LinkageAuthorizationDecision:
        _utc_second(self.evaluated_at, "evaluated_at")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=str)):
            raise ValueError("authority reasons must be uniquely sorted")
        if self.approval_ids != tuple(sorted(set(self.approval_ids))):
            raise ValueError("approval IDs must be uniquely sorted")
        if self.principal_ids != tuple(sorted(set(self.principal_ids))):
            raise ValueError("principal IDs must be uniquely sorted")
        if self.linkage_authorized != (
            self.reason_codes == (LinkageAuthorityReason.AUTHORIZED,)
        ):
            raise ValueError("authorized decision must contain only authorized reason")
        if self.linkage_authorized:
            if self.trust_snapshot_sha256 is None:
                raise ValueError("authorized decision requires exact trust snapshot")
            if not self.approval_ids or not self.principal_ids:
                raise ValueError("authorized decision requires approvals and principals")
            if len(self.approval_ids) != len(self.principal_ids):
                raise ValueError("authorized decision approval count is inconsistent")
        if self.comparison_linkage_eligible and not self.linkage_authorized:
            raise ValueError("comparison linkage eligibility requires authorization")
        return self


class AuthorizedLinkageRevision(LinkageContract):
    """Self-contained proof inputs plus decision for one authorized revision."""

    revision: LinkageRevision
    previous_revision: LinkageRevision | None
    trust_snapshot: ProviderTrustSnapshot
    approvals: tuple[SignedProviderApproval, ...] = Field(
        min_length=1, max_length=MAX_APPROVALS
    )
    authorization: LinkageAuthorizationDecision

    @model_validator(mode="after")
    def exact_binding(self) -> AuthorizedLinkageRevision:
        if self.authorization.proposed_revision_sha256 != linkage_revision_sha256(
            self.revision
        ):
            raise ValueError("authorization does not bind the exact linkage revision")
        if not self.authorization.linkage_authorized:
            raise ValueError("disabled linkage cannot enter the authorized ledger")
        trust_sha256 = provider_trust_snapshot_sha256(self.trust_snapshot)
        if self.authorization.trust_snapshot_sha256 != trust_sha256:
            raise ValueError("authorization does not bind the exact trust snapshot")
        replay = authorize_linkage_revision(
            self.revision,
            previous_revision=self.previous_revision,
            approvals=self.approvals,
            trust_snapshot=self.trust_snapshot,
            expected_trust_snapshot_sha256=trust_sha256,
            evaluated_at=self.authorization.evaluated_at,
        )
        if replay != self.authorization:
            raise ValueError("authorization decision does not replay from proof inputs")
        return self


class ApprovalConsumption(LinkageContract):
    provider_namespace: ProviderNamespace
    approval_id: ApprovalId
    nonce: Nonce
    proposed_revision_sha256: Sha256
    trust_snapshot_sha256: Sha256


class ProviderApprovalConsumptionLedger(LinkageContract):
    """Append-only replay fence; durable atomic storage is a D04 dependency."""

    schema_version: Literal["traceback.provider-approval-consumption-ledger.v1"] = (
        "traceback.provider-approval-consumption-ledger.v1"
    )
    entries: tuple[ApprovalConsumption, ...] = Field(
        default=(), max_length=MAX_CONSUMED_APPROVALS
    )

    @model_validator(mode="after")
    def unique_sorted_consumptions(self) -> ProviderApprovalConsumptionLedger:
        keys = [
            (item.provider_namespace, item.approval_id, item.nonce)
            for item in self.entries
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("approval consumptions must be uniquely sorted")
        approval_keys = [
            (item.provider_namespace, item.approval_id) for item in self.entries
        ]
        nonce_keys = [(item.provider_namespace, item.nonce) for item in self.entries]
        if len(approval_keys) != len(set(approval_keys)):
            raise ValueError("an approval ID cannot be consumed more than once")
        if len(nonce_keys) != len(set(nonce_keys)):
            raise ValueError("an approval nonce cannot be consumed more than once")
        return self


def linkage_revision_sha256(revision: LinkageRevision) -> str:
    return hashlib.sha256(canonical_contract_bytes(revision)).hexdigest()


def provider_trust_snapshot_sha256(snapshot: ProviderTrustSnapshot) -> str:
    return hashlib.sha256(canonical_contract_bytes(snapshot)).hexdigest()


def approval_payload_bytes(payload: ProviderApprovalPayload) -> bytes:
    return b"traceback-provider-linkage-approval\0" + canonical_contract_bytes(payload)


def _purpose(operation: LinkageOperation) -> ApprovalPurpose:
    return {
        LinkageOperation.CREATE: ApprovalPurpose.CREATE_LINKAGE,
        LinkageOperation.CORRECT: ApprovalPurpose.CORRECT_LINKAGE,
        LinkageOperation.TOMBSTONE: ApprovalPurpose.TOMBSTONE_LINKAGE,
    }[operation]


def _correction_delta_matches_reason(
    previous: LinkageRevision,
    current: LinkageRevision,
) -> bool:
    """Require one typed correction class instead of accepting a free relink."""

    subject_changed = (
        previous.biological.subject_token != current.biological.subject_token
    )
    collection_changed = (
        previous.biological.collection_token != current.biological.collection_token
    )
    specimen_token_changed = (
        previous.biological.specimen_token != current.biological.specimen_token
    )
    aliquot_changed = previous.biological.aliquot != current.biological.aliquot
    specimen_changed = specimen_token_changed or aliquot_changed
    aliquot_rotation_valid = (
        previous.biological.aliquot.token is None
        or (
            current.biological.aliquot.token is not None
            and aliquot_changed
        )
    )
    technical_changed = previous.technical != current.technical
    source_changed = previous.source_projection_ref != current.source_projection_ref
    unit_changed = previous.unit_of_analysis != current.unit_of_analysis
    changes = (
        subject_changed,
        collection_changed,
        specimen_changed,
        technical_changed,
        source_changed,
        unit_changed,
    )
    if current.operation == LinkageOperation.TOMBSTONE:
        return not any(changes)
    if current.reason_code == LinkageReasonCode.WRONG_SUBJECT:
        return (
            subject_changed
            and collection_changed
            and specimen_token_changed
            and aliquot_rotation_valid
            and not technical_changed
            and not source_changed
            and not unit_changed
        )
    if current.reason_code == LinkageReasonCode.WRONG_COLLECTION:
        return (
            not subject_changed
            and collection_changed
            and specimen_token_changed
            and aliquot_rotation_valid
            and not technical_changed
            and not source_changed
            and not unit_changed
        )
    expected = {
        LinkageReasonCode.WRONG_SPECIMEN: (
            False,
            False,
            True,
            False,
            False,
            False,
        ),
        LinkageReasonCode.TECHNICAL_LINEAGE_CORRECTION: (
            False,
            False,
            False,
            True,
            False,
            False,
        ),
        LinkageReasonCode.SOURCE_AUTHORITY_UPDATE: (
            False,
            False,
            False,
            False,
            True,
            False,
        ),
    }.get(current.reason_code)
    return expected is not None and changes == expected


def _disabled(
    revision: LinkageRevision,
    evaluated_at: datetime,
    reasons: set[LinkageAuthorityReason],
    *,
    trust_sha256: str | None,
    approvals: Sequence[SignedProviderApproval],
) -> LinkageAuthorizationDecision:
    return LinkageAuthorizationDecision(
        proposed_revision_sha256=linkage_revision_sha256(revision),
        trust_snapshot_sha256=trust_sha256,
        evaluated_at=evaluated_at,
        linkage_authorized=False,
        comparison_linkage_eligible=False,
        reason_codes=tuple(sorted(reasons, key=str)),
        approval_ids=tuple(sorted({item.payload.approval_id for item in approvals})),
        principal_ids=tuple(sorted({item.payload.principal_id for item in approvals})),
    )


def authorize_linkage_revision(
    revision: LinkageRevision,
    *,
    previous_revision: LinkageRevision | None,
    approvals: Sequence[SignedProviderApproval],
    trust_snapshot: ProviderTrustSnapshot | None,
    expected_trust_snapshot_sha256: str | None,
    evaluated_at: datetime,
) -> LinkageAuthorizationDecision:
    """Verify authority for one exact revision and return a fail-closed decision."""

    _utc_second(evaluated_at, "evaluated_at")
    revision_sha256 = linkage_revision_sha256(revision)
    if trust_snapshot is None or expected_trust_snapshot_sha256 is None:
        return _disabled(
            revision,
            evaluated_at,
            {LinkageAuthorityReason.AUTHORITY_ABSENT},
            trust_sha256=None,
            approvals=approvals,
        )
    trust_sha256 = provider_trust_snapshot_sha256(trust_snapshot)
    reasons: set[LinkageAuthorityReason] = set()
    if trust_sha256 != expected_trust_snapshot_sha256:
        reasons.add(LinkageAuthorityReason.TRUST_HEAD_MISMATCH)
    if trust_snapshot.provider_namespace != revision.provider_namespace:
        reasons.add(LinkageAuthorityReason.PROVIDER_MISMATCH)
    if not (trust_snapshot.issued_at <= evaluated_at < trust_snapshot.expires_at):
        reasons.add(LinkageAuthorityReason.TRUST_EXPIRED)

    if revision.operation == LinkageOperation.CREATE:
        if previous_revision is not None:
            reasons.add(LinkageAuthorityReason.REVISION_CHAIN_INVALID)
        required_roles = {ProviderRole.LINKER}
        required_count = 1
    else:
        if (
            previous_revision is None
            or previous_revision.operation == LinkageOperation.TOMBSTONE
            or previous_revision.linkage_id != revision.linkage_id
            or previous_revision.provider_namespace != revision.provider_namespace
            or revision.revision != previous_revision.revision + 1
            or revision.previous_revision_sha256
            != linkage_revision_sha256(previous_revision)
        ):
            reasons.add(LinkageAuthorityReason.REVISION_CHAIN_INVALID)
        elif not _correction_delta_matches_reason(previous_revision, revision):
            reasons.add(LinkageAuthorityReason.CORRECTION_DELTA_INVALID)
        required_roles = {ProviderRole.LINKER, ProviderRole.REVIEWER}
        required_count = 2

    if not 1 <= len(approvals) <= MAX_APPROVALS or len(approvals) != required_count:
        reasons.add(LinkageAuthorityReason.APPROVAL_COUNT_INVALID)
    approval_ids = [item.payload.approval_id for item in approvals]
    principals = [item.payload.principal_id for item in approvals]
    nonces = [item.payload.nonce for item in approvals]
    if len(approval_ids) != len(set(approval_ids)) or len(nonces) != len(set(nonces)):
        reasons.add(LinkageAuthorityReason.APPROVAL_REUSED)
    if len(principals) != len(set(principals)):
        reasons.add(LinkageAuthorityReason.DUPLICATE_PRINCIPAL)

    issuers = {
        (item.issuer_id, item.key_id): item for item in trust_snapshot.issuers
    }
    observed_roles: set[ProviderRole] = set()
    expected_purpose = _purpose(revision.operation)
    for approval in approvals:
        payload = approval.payload
        if payload.provider_namespace != revision.provider_namespace:
            reasons.add(LinkageAuthorityReason.PROVIDER_MISMATCH)
        if payload.proposed_revision_sha256 != revision_sha256:
            reasons.add(LinkageAuthorityReason.REVISION_BINDING_MISMATCH)
        if (
            payload.trust_snapshot_id != trust_snapshot.snapshot_id
            or payload.trust_snapshot_revision != trust_snapshot.revision
            or payload.trust_snapshot_sha256 != trust_sha256
        ):
            reasons.add(LinkageAuthorityReason.TRUST_BINDING_MISMATCH)
        if payload.purpose != expected_purpose:
            reasons.add(LinkageAuthorityReason.PURPOSE_MISMATCH)
        if not (
            trust_snapshot.issued_at
            <= payload.issued_at
            <= evaluated_at
            < payload.expires_at
        ):
            reasons.add(LinkageAuthorityReason.APPROVAL_NOT_CURRENT)
        issuer = issuers.get((payload.issuer_id, payload.key_id))
        if issuer is None:
            reasons.add(LinkageAuthorityReason.ISSUER_UNKNOWN)
            continue
        if issuer.status != IssuerStatus.ACTIVE:
            reasons.add(LinkageAuthorityReason.ISSUER_REVOKED)
        if (
            payload.role not in issuer.allowed_roles
            or payload.purpose not in issuer.allowed_purposes
        ):
            reasons.add(LinkageAuthorityReason.ISSUER_GRANT_MISMATCH)
        observed_roles.add(payload.role)
        try:
            public_key = base64.b64decode(issuer.public_key_base64, validate=True)
            signature = base64.b64decode(approval.signature_base64, validate=True)
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature, approval_payload_bytes(payload)
            )
        except (InvalidSignature, ValueError):
            reasons.add(LinkageAuthorityReason.SIGNATURE_INVALID)
    if observed_roles != required_roles:
        reasons.add(LinkageAuthorityReason.ROLE_REQUIREMENTS_UNMET)

    if reasons:
        return _disabled(
            revision,
            evaluated_at,
            reasons,
            trust_sha256=trust_sha256,
            approvals=approvals,
        )
    return LinkageAuthorizationDecision(
        proposed_revision_sha256=revision_sha256,
        trust_snapshot_sha256=trust_sha256,
        evaluated_at=evaluated_at,
        linkage_authorized=True,
        comparison_linkage_eligible=False,
        reason_codes=(LinkageAuthorityReason.AUTHORIZED,),
        approval_ids=tuple(sorted(approval_ids)),
        principal_ids=tuple(sorted(principals)),
    )


def authorize_and_consume_linkage_revision(
    revision: LinkageRevision,
    *,
    previous_revision: LinkageRevision | None,
    approvals: tuple[SignedProviderApproval, ...],
    trust_snapshot: ProviderTrustSnapshot,
    expected_trust_snapshot_sha256: str,
    evaluated_at: datetime,
    consumption_ledger: ProviderApprovalConsumptionLedger,
) -> tuple[AuthorizedLinkageRevision, ProviderApprovalConsumptionLedger]:
    """Refuse activation until the protected transactional store is present."""

    del (
        revision,
        previous_revision,
        approvals,
        trust_snapshot,
        expected_trust_snapshot_sha256,
        evaluated_at,
        consumption_ledger,
    )
    raise RuntimeError(
        "durable atomic linkage persistence is required before approval consumption"
    )


def prepare_authorized_linkage_revision(
    revision: LinkageRevision,
    *,
    previous_revision: LinkageRevision | None,
    approvals: tuple[SignedProviderApproval, ...],
    trust_snapshot: ProviderTrustSnapshot,
    expected_trust_snapshot_sha256: str,
    evaluated_at: datetime,
) -> AuthorizedLinkageRevision:
    """Build proof bytes for a future store transaction without activating them."""

    decision = authorize_linkage_revision(
        revision,
        previous_revision=previous_revision,
        approvals=approvals,
        trust_snapshot=trust_snapshot,
        expected_trust_snapshot_sha256=expected_trust_snapshot_sha256,
        evaluated_at=evaluated_at,
    )
    if not decision.linkage_authorized:
        raise ValueError("linkage authority verification failed")
    return AuthorizedLinkageRevision(
        revision=revision,
        previous_revision=previous_revision,
        trust_snapshot=trust_snapshot,
        approvals=approvals,
        authorization=decision,
    )


def project_active_linkages(
    ledger: Sequence[AuthorizedLinkageRevision],
    *,
    consumption_ledger: ProviderApprovalConsumptionLedger,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> tuple[LinkageRevision, ...]:
    """Refuse active projection from caller-supplied, non-durable state."""

    del ledger, consumption_ledger, expected_trust_snapshot_sha256_by_provider
    raise RuntimeError(
        "durable atomic linkage persistence is required before active projection"
    )


def validate_linkage_history(
    ledger: Sequence[AuthorizedLinkageRevision],
    *,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> None:
    """Validate bounded append-only history without asserting active authority."""

    if len(ledger) > MAX_REVISIONS:
        raise ValueError("linkage ledger exceeds its revision bound")
    if not ledger:
        return
    grouped: dict[tuple[str, str], list[LinkageRevision]] = {}
    for record in ledger:
        expected_trust = expected_trust_snapshot_sha256_by_provider.get(
            record.revision.provider_namespace
        )
        if expected_trust is None:
            raise ValueError("linkage provider trust pin is absent")
        trust_sha256 = provider_trust_snapshot_sha256(record.trust_snapshot)
        if trust_sha256 != expected_trust:
            raise ValueError("authorized linkage trust snapshot is not independently pinned")
        replay = authorize_linkage_revision(
            record.revision,
            previous_revision=record.previous_revision,
            approvals=record.approvals,
            trust_snapshot=record.trust_snapshot,
            expected_trust_snapshot_sha256=expected_trust,
            evaluated_at=record.authorization.evaluated_at,
        )
        if replay != record.authorization or not replay.linkage_authorized:
            raise ValueError("authorized linkage proof does not replay")
        grouped.setdefault(
            (record.revision.provider_namespace, record.revision.linkage_id), []
        ).append(record.revision)
    for revisions in grouped.values():
        ordered = sorted(revisions, key=lambda item: item.revision)
        if ordered[0].revision != 1:
            raise ValueError("linkage ledger must begin at revision one")
        for previous, current in pairwise(ordered):
            if (
                current.revision != previous.revision + 1
                or current.previous_revision_sha256 != linkage_revision_sha256(previous)
                or current.provider_namespace != previous.provider_namespace
            ):
                raise ValueError("linkage ledger revision chain is invalid")
    collection_parents: dict[tuple[str, str], str] = {}
    specimen_parents: dict[tuple[str, str], tuple[str, str]] = {}
    aliquot_parents: dict[tuple[str, str], tuple[str, str, str]] = {}
    analysis_owners: dict[tuple[str, str], tuple[str, int]] = {}
    measurement_owners: dict[tuple[str, str], tuple[str, int]] = {}
    ordered_records = sorted(
        ledger,
        key=lambda item: (
            item.revision.provider_namespace,
            item.revision.linkage_id,
            item.revision.revision,
        ),
    )
    for record in ordered_records:
        revision = record.revision
        collection_key = (
            revision.provider_namespace,
            revision.biological.collection_token,
        )
        analysis_key = (
            revision.provider_namespace,
            revision.technical.analysis_record_id,
        )
        measurement_key = (
            revision.provider_namespace,
            revision.technical.measurement_id,
        )
        known_subject = collection_parents.setdefault(
            collection_key, revision.biological.subject_token
        )
        if known_subject != revision.biological.subject_token:
            raise ValueError("linkage history contains conflicting collection parent")
        specimen_key = (
            revision.provider_namespace,
            revision.biological.specimen_token,
        )
        specimen_parent = (
            revision.biological.subject_token,
            revision.biological.collection_token,
        )
        if specimen_parents.setdefault(specimen_key, specimen_parent) != specimen_parent:
            raise ValueError("linkage history contains conflicting specimen parent")
        if revision.biological.aliquot.token is not None:
            aliquot_key = (
                revision.provider_namespace,
                revision.biological.aliquot.token,
            )
            aliquot_parent = (
                revision.biological.subject_token,
                revision.biological.collection_token,
                revision.biological.specimen_token,
            )
            if (
                aliquot_parents.setdefault(aliquot_key, aliquot_parent)
                != aliquot_parent
            ):
                raise ValueError("linkage history contains conflicting aliquot parent")
        analysis_owner = analysis_owners.get(analysis_key)
        if analysis_owner is not None and (
            analysis_owner[0] != revision.linkage_id
            or analysis_owner[1] != revision.revision - 1
        ):
            raise ValueError("linkage history contains a reused analysis record")
        analysis_owners[analysis_key] = (revision.linkage_id, revision.revision)
        measurement_owner = measurement_owners.get(measurement_key)
        if measurement_owner is not None and (
            measurement_owner[0] != revision.linkage_id
            or measurement_owner[1] != revision.revision - 1
        ):
            raise ValueError("linkage history contains a reused measurement")
        measurement_owners[measurement_key] = (
            revision.linkage_id,
            revision.revision,
        )


__all__ = [
    "ApprovalConsumption",
    "ApprovalPurpose",
    "AuthorizedLinkageRevision",
    "BiologicalLineage",
    "IssuerStatus",
    "LinkageAuthorityReason",
    "LinkageAuthorizationDecision",
    "LinkageOperation",
    "LinkageReasonCode",
    "LinkageRevision",
    "OptionalLineageState",
    "OptionalOpaqueToken",
    "ProviderApprovalConsumptionLedger",
    "ProviderApprovalPayload",
    "ProviderIssuerTrust",
    "ProviderRole",
    "ProviderTrustSnapshot",
    "SignedProviderApproval",
    "TechnicalLineage",
    "UnitOfAnalysis",
    "approval_payload_bytes",
    "authorize_and_consume_linkage_revision",
    "authorize_linkage_revision",
    "linkage_revision_sha256",
    "prepare_authorized_linkage_revision",
    "project_active_linkages",
    "provider_trust_snapshot_sha256",
    "validate_linkage_history",
]
