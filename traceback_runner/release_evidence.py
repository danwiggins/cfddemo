"""Additive synthetic release, workstation, protocol, and evidence contracts.

These v1 contracts describe development evidence. They do not amend historical
runner schemas and cannot authorize real input or a qualification probe.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .contracts import Identifier, NonEmptyText, RunnerContract
from .serialization import canonical_json_bytes

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


def require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    """Return an aware UTC timestamp or reject ambiguous/non-UTC values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")
    return value


class DigestDomain(StrEnum):
    ASSET_REFERENCE = "traceback.asset-reference.v1"
    WORKSTATION_PROFILE = "traceback.workstation-profile.v1"
    PROTOCOL_REFERENCE = "traceback.protocol-reference.v1"
    EVIDENCE_MANIFEST = "traceback.qualification-evidence.v1"
    RELEASE_EVIDENCE = "traceback.release-evidence.v1"
    QUALIFICATION_DECISION = "traceback.qualification-decision.v1"


def canonical_domain_bytes(domain: DigestDomain, value: RunnerContract) -> bytes:
    """Domain-separate canonical contract bytes for hashing and signing."""

    if getattr(value, "schema_version", None) != domain.value:
        raise ValueError(
            f"{domain.value} digest domain requires matching schema_version"
        )
    return (
        b"traceback-domain\0"
        + domain.value.encode("ascii")
        + b"\0"
        + canonical_json_bytes(value)
    )


def domain_digest(domain: DigestDomain, value: RunnerContract) -> str:
    """Return SHA-256 over domain-separated canonical contract bytes."""

    return hashlib.sha256(canonical_domain_bytes(domain, value)).hexdigest()


class AssetKind(StrEnum):
    IMAGE = "image"
    MODEL = "model"
    REFERENCE = "reference"
    INDEX = "index"
    TOOL = "tool"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AssetContentIdentity(RunnerContract):
    """Identity of exact uncompressed framed payload bytes."""

    asset_id: Identifier
    version: Identifier
    kind: AssetKind
    content_sha256: Sha256
    content_size_bytes: PositiveStrictInt


class AssetProvenance(RunnerContract):
    source_authority: NonEmptyText
    license_id: Identifier


class AssetLifecycle(RunnerContract):
    status: AssetStatus
    revocation_reference: Identifier | None = None
    revoked_at: datetime | None = None

    @field_validator("revoked_at")
    @classmethod
    def revoked_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return (
            None if value is None else require_aware_utc(value, field_name="revoked_at")
        )

    @model_validator(mode="after")
    def revocation_fields_match_status(self) -> AssetLifecycle:
        has_reference = self.revocation_reference is not None
        has_time = self.revoked_at is not None
        if self.status == AssetStatus.ACTIVE and (has_reference or has_time):
            raise ValueError("active assets cannot contain revocation metadata")
        if self.status == AssetStatus.REVOKED and not (has_reference and has_time):
            raise ValueError(
                "revoked assets require revocation_reference and revoked_at"
            )
        return self


class AssetReference(RunnerContract):
    schema_version: Literal["traceback.asset-reference.v1"] = (
        "traceback.asset-reference.v1"
    )
    synthetic_only: Literal[True] = True
    content: AssetContentIdentity
    provenance: AssetProvenance
    lifecycle: AssetLifecycle


class WorkstationProfile(RunnerContract):
    schema_version: Literal["traceback.workstation-profile.v1"] = (
        "traceback.workstation-profile.v1"
    )
    profile_id: Identifier
    version: Identifier
    operating_system: Identifier
    architecture: Identifier
    kernel_version: Identifier
    runtime_id: Identifier
    runtime_version: Identifier
    accelerator_id: Identifier | None = None
    accelerator_version: Identifier | None = None
    minimum_cpu_cores: PositiveStrictInt
    minimum_memory_bytes: PositiveStrictInt
    minimum_free_disk_bytes: PositiveStrictInt
    synthetic_only: Literal[True] = True
    real_data_authorized: Literal[False] = False

    @model_validator(mode="after")
    def accelerator_fields_are_paired(self) -> WorkstationProfile:
        if (self.accelerator_id is None) != (self.accelerator_version is None):
            raise ValueError(
                "accelerator_id and accelerator_version must appear together"
            )
        return self


class ProtocolApprovalStatus(StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class ProtocolReference(RunnerContract):
    schema_version: Literal["traceback.protocol-reference.v1"] = (
        "traceback.protocol-reference.v1"
    )
    protocol_id: Identifier
    version: Identifier
    document_sha256: Sha256
    reported_approval_status: ProtocolApprovalStatus
    external_approval_reference: Identifier | None = None
    synthetic_only: Literal[True] = True
    real_data_authorized: Literal[False] = False

    @model_validator(mode="after")
    def substantive_status_has_reference(self) -> ProtocolReference:
        substantive = {
            ProtocolApprovalStatus.APPROVED,
            ProtocolApprovalStatus.REJECTED,
            ProtocolApprovalStatus.REVOKED,
        }
        if (
            self.reported_approval_status in substantive
            and self.external_approval_reference is None
        ):
            raise ValueError(
                "substantive protocol status requires external_approval_reference"
            )
        return self


class EvidenceStatus(StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    PRESENT = "present"
    REJECTED = "rejected"
    REVOKED = "revoked"


class EvidenceRequirementKind(StrEnum):
    INTENDED_MEASUREMENT = "01-intended-measurement"
    INTENDED_CLAIM = "02-intended-claim"
    EXACT_SOP = "03-exact-sop"
    DATASET_MANIFEST = "04-dataset-manifest"
    RIGHTS_USE_RETENTION = "05-rights-use-retention"
    COMPARATOR = "06-comparator"
    REPLICATE_PLAN = "07-replicate-plan"
    PARTITION_HOLDOUT_PLAN = "08-partition-holdout-plan"
    ACCEPTANCE_CRITERIA = "09-acceptance-criteria"
    FAILURE_ACCOUNTING = "10-failure-accounting"
    REQUALIFICATION_TRIGGERS = "11-requalification-triggers"


class QualificationEvidenceItem(RunnerContract):
    requirement: EvidenceRequirementKind
    status: EvidenceStatus
    evidence_id: Identifier | None = None
    version: Identifier | None = None
    artifact_sha256: Sha256 | None = None
    recorded_at: datetime | None = None

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return (
            None
            if value is None
            else require_aware_utc(value, field_name="recorded_at")
        )

    @model_validator(mode="after")
    def evidence_fields_match_status(self) -> QualificationEvidenceItem:
        fields = (
            self.evidence_id,
            self.version,
            self.artifact_sha256,
            self.recorded_at,
        )
        populated = tuple(value is not None for value in fields)
        if self.status == EvidenceStatus.UNKNOWN and any(populated):
            raise ValueError("unknown evidence cannot claim an evidence artifact")
        if self.status in {
            EvidenceStatus.PRESENT,
            EvidenceStatus.REJECTED,
            EvidenceStatus.REVOKED,
        } and not all(populated):
            raise ValueError(
                f"{self.status.value} evidence requires id, version, digest, and recorded_at"
            )
        if (
            self.status == EvidenceStatus.PENDING
            and any(populated)
            and not all(populated)
        ):
            raise ValueError(
                "pending evidence metadata must be either absent or complete"
            )
        return self


class QualificationEvidenceManifest(RunnerContract):
    schema_version: Literal["traceback.qualification-evidence.v1"] = (
        "traceback.qualification-evidence.v1"
    )
    manifest_id: Identifier
    version: Identifier
    items: tuple[QualificationEvidenceItem, ...]
    synthetic_only: Literal[True] = True
    scientific_qualification_established: Literal[False] = False

    @model_validator(mode="after")
    def exact_requirements_in_canonical_order(self) -> QualificationEvidenceManifest:
        expected = tuple(EvidenceRequirementKind)
        actual = tuple(item.requirement for item in self.items)
        if actual != expected:
            raise ValueError(
                "evidence items must contain every requirement exactly once in canonical order"
            )
        return self


class ReleaseEvidencePackage(RunnerContract):
    schema_version: Literal["traceback.release-evidence.v1"] = (
        "traceback.release-evidence.v1"
    )
    release_id: Identifier
    version: Identifier
    workflow_release_sha256: Sha256
    workstation_profile_id: Identifier
    workstation_profile_version: Identifier
    workstation_profile_sha256: Sha256
    protocol_id: Identifier
    protocol_version: Identifier
    protocol_reference_sha256: Sha256
    evidence_manifest_id: Identifier
    evidence_manifest_version: Identifier
    evidence_manifest_sha256: Sha256
    assets: tuple[AssetReference, ...] = Field(min_length=1)
    synthetic_only: Literal[True] = True
    real_data_authorized: Literal[False] = False
    qualification_probe_authorized: Literal[False] = False

    @model_validator(mode="after")
    def assets_are_unique_and_sorted(self) -> ReleaseEvidencePackage:
        keys = tuple(
            (item.content.asset_id, item.content.version) for item in self.assets
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("assets must be uniquely sorted by asset_id and version")
        return self


def build_release_evidence_package(
    *,
    release_id: str,
    version: str,
    workflow_release_sha256: str,
    workstation_profile: WorkstationProfile,
    protocol_reference: ProtocolReference,
    evidence_manifest: QualificationEvidenceManifest,
    assets: tuple[AssetReference, ...],
) -> ReleaseEvidencePackage:
    """Bind exact evidence contracts without inferring any approval."""

    return ReleaseEvidencePackage(
        release_id=release_id,
        version=version,
        workflow_release_sha256=workflow_release_sha256,
        workstation_profile_id=workstation_profile.profile_id,
        workstation_profile_version=workstation_profile.version,
        workstation_profile_sha256=domain_digest(
            DigestDomain.WORKSTATION_PROFILE, workstation_profile
        ),
        protocol_id=protocol_reference.protocol_id,
        protocol_version=protocol_reference.version,
        protocol_reference_sha256=domain_digest(
            DigestDomain.PROTOCOL_REFERENCE, protocol_reference
        ),
        evidence_manifest_id=evidence_manifest.manifest_id,
        evidence_manifest_version=evidence_manifest.version,
        evidence_manifest_sha256=domain_digest(
            DigestDomain.EVIDENCE_MANIFEST, evidence_manifest
        ),
        assets=assets,
    )


__all__ = [
    "AssetContentIdentity",
    "AssetKind",
    "AssetLifecycle",
    "AssetProvenance",
    "AssetReference",
    "AssetStatus",
    "DigestDomain",
    "EvidenceRequirementKind",
    "EvidenceStatus",
    "ProtocolApprovalStatus",
    "ProtocolReference",
    "QualificationEvidenceItem",
    "QualificationEvidenceManifest",
    "ReleaseEvidencePackage",
    "WorkstationProfile",
    "build_release_evidence_package",
    "canonical_domain_bytes",
    "domain_digest",
    "require_aware_utc",
]
