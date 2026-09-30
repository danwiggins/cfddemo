"""Immutable D05 cohort manifests over protected provider linkage snapshots.

The contracts are synthetic/local foundations. They define denominators and
lineage only; they make no clinical, scientific, or provider approval claim.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    AnalysisRecordId,
    CollectionToken,
    LinkageId,
    LinkageOperation,
    ProviderNamespace,
    RunToken,
    SpecimenToken,
    SubjectToken,
    UnitOfAnalysis,
    linkage_revision_sha256,
)
from evidence_inspector.provider_linkage_store import (
    ActiveLinkageSnapshot,
    committed_linkage_receipt_sha256,
)

MAX_MEMBERS = 100_000
CohortId = Annotated[str, StringConstraints(pattern=r"^cohort_[0-9a-f]{32}$")]


def _utc_second(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError("created_at must be UTC at whole-second precision")
    return value


class TechnicalReplicateRule(StrEnum):
    COLLAPSE_TO_BIOLOGICAL_UNIT = "collapse_to_biological_unit"
    EXCLUDE_TECHNICAL_REPLICATES = "exclude_technical_replicates"


class MemberLineageRole(StrEnum):
    BIOLOGICAL_DRAW = "biological_draw"
    TECHNICAL_REPLICATE = "technical_replicate"
    REANALYSIS = "reanalysis"


class TimeAxisKind(StrEnum):
    COLLECTION_TIME = "collection_time"
    SUBJECT_RELATIVE = "subject_relative"
    STUDY_RELATIVE = "study_relative"


class PolicyDigests(RegistryContract):
    inclusion_sha256: Sha256
    exclusion_sha256: Sha256
    missingness_sha256: Sha256


class TimeAxis(RegistryContract):
    kind: TimeAxisKind
    definition_sha256: Sha256
    unit_sha256: Sha256
    origin_authority_sha256: Sha256


class MeasurementAnchor(RegistryContract):
    measurement_definition_sha256: Sha256
    anchor_definition_sha256: Sha256
    authority_sha256: Sha256


class ProviderAuthorityReference(RegistryContract):
    provider_namespace: ProviderNamespace
    trust_snapshot_sha256: Sha256
    store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    store_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    trust_pins_sha256: Sha256
    state_version: int = Field(ge=1)
    state_head_sha256: Sha256


class CohortMember(RegistryContract):
    provider_namespace: ProviderNamespace
    linkage_id: LinkageId
    linkage_revision: int = Field(ge=1)
    linkage_revision_sha256: Sha256
    committed_receipt_sha256: Sha256
    subject_token: SubjectToken
    collection_token: CollectionToken
    specimen_token: SpecimenToken
    analysis_record_id: AnalysisRecordId
    run_token: RunToken | None = None
    reanalysis_of: AnalysisRecordId | None = None
    lineage_role: MemberLineageRole
    analysis_unit_token: str = Field(
        pattern=r"^(?:subject|collection|specimen)_[0-9a-f]{32}$"
    )
    denominator_contribution: bool


class CohortManifest(RegistryContract):
    schema_version: Literal["traceback.cohort-manifest.v1"] = (
        "traceback.cohort-manifest.v1"
    )
    cohort_id: CohortId
    version: int = Field(ge=1, le=100_000)
    previous_manifest_sha256: Sha256 | None
    created_at: datetime
    unit_of_analysis: UnitOfAnalysis
    technical_replicate_rule: TechnicalReplicateRule
    time_axis: TimeAxis
    policies: PolicyDigests
    measurement_anchor: MeasurementAnchor
    provider_authorities: tuple[ProviderAuthorityReference, ...] = Field(
        min_length=1, max_length=256
    )
    members: tuple[CohortMember, ...] = Field(min_length=1, max_length=MAX_MEMBERS)
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_denominators_and_lineage(self) -> CohortManifest:
        _utc_second(self.created_at)
        if (self.version == 1) != (self.previous_manifest_sha256 is None):
            raise ValueError("only manifest version one may omit its predecessor")
        authority_keys = [item.provider_namespace for item in self.provider_authorities]
        if authority_keys != sorted(authority_keys) or len(authority_keys) != len(
            set(authority_keys)
        ):
            raise ValueError("provider authorities must be uniquely sorted")
        keys = [
            (m.provider_namespace, m.linkage_id, m.linkage_revision)
            for m in self.members
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError(
                "cohort members must be uniquely sorted by exact linkage version"
            )
        if {m.provider_namespace for m in self.members} != set(authority_keys):
            raise ValueError("every and only member providers require exact authority")
        analyses = [m.analysis_record_id for m in self.members]
        if len(analyses) != len(set(analyses)):
            raise ValueError("analysis records cannot be counted twice")
        expected_prefix = f"{self.unit_of_analysis.value}_"
        groups: dict[str, list[CohortMember]] = defaultdict(list)
        for member in self.members:
            expected_token = {
                UnitOfAnalysis.SUBJECT: member.subject_token,
                UnitOfAnalysis.COLLECTION: member.collection_token,
                UnitOfAnalysis.SPECIMEN: member.specimen_token,
            }[self.unit_of_analysis]
            if (
                member.analysis_unit_token != expected_token
                or not expected_token.startswith(expected_prefix)
            ):
                raise ValueError("member analysis unit does not match declared lineage")
            if (member.lineage_role == MemberLineageRole.REANALYSIS) != (
                member.reanalysis_of is not None
            ):
                raise ValueError("reanalysis role must bind its source analysis")
            groups[expected_token].append(member)
        analysis_set = set(analyses)
        for members in groups.values():
            contributors = [m for m in members if m.denominator_contribution]
            if (
                len(contributors) != 1
                or contributors[0].lineage_role != MemberLineageRole.BIOLOGICAL_DRAW
            ):
                raise ValueError(
                    "each biological analysis unit contributes exactly one denominator"
                )
            anchor = contributors[0]
            for member in members:
                if (
                    member.subject_token,
                    member.collection_token,
                    member.specimen_token,
                ) != (
                    anchor.subject_token,
                    anchor.collection_token,
                    anchor.specimen_token,
                ):
                    raise ValueError("cross-unit pseudoreplication is forbidden")
                if (
                    member is not anchor
                    and member.lineage_role == MemberLineageRole.BIOLOGICAL_DRAW
                ):
                    raise ValueError(
                        "technical reruns cannot count as biological draws"
                    )
                if (
                    member.reanalysis_of is not None
                    and member.reanalysis_of not in analysis_set
                ):
                    raise ValueError("reanalysis source is outside the manifest")
                if (
                    self.technical_replicate_rule
                    == TechnicalReplicateRule.EXCLUDE_TECHNICAL_REPLICATES
                    and member is not anchor
                ):
                    raise ValueError(
                        "technical replicate rule excludes duplicate unit members"
                    )
        return self


def cohort_manifest_bytes(manifest: CohortManifest) -> bytes:
    return canonical_contract_bytes(manifest)


def cohort_manifest_sha256(manifest: CohortManifest) -> str:
    return hashlib.sha256(cohort_manifest_bytes(manifest)).hexdigest()


def build_cohort_manifest(
    *,
    provider_authorities: Sequence[ProviderAuthorityReference],
    members: Sequence[CohortMember],
    **values: object,
) -> CohortManifest:
    """Build canonical membership order from already-controlled inputs."""

    return CohortManifest.model_validate(
        {
            **values,
            "provider_authorities": tuple(
                sorted(provider_authorities, key=lambda item: item.provider_namespace)
            ),
            "members": tuple(
                sorted(
                    members,
                    key=lambda item: (
                        item.provider_namespace,
                        item.linkage_id,
                        item.linkage_revision,
                    ),
                )
            ),
        }
    )


def _semantic_sha256(manifest: CohortManifest) -> str:
    payload = manifest.model_dump(mode="json")
    for key in ("version", "previous_manifest_sha256", "created_at"):
        payload.pop(key)
    import json

    content = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(content).hexdigest()


def validate_manifest_history(history: Sequence[CohortManifest]) -> None:
    if not history:
        raise ValueError("manifest history cannot be empty")
    cohort_id = history[0].cohort_id
    for index, manifest in enumerate(history, start=1):
        if manifest.cohort_id != cohort_id or manifest.version != index:
            raise ValueError("manifest history must be one consecutive cohort chain")
    for previous, current in pairwise(history):
        if current.previous_manifest_sha256 != cohort_manifest_sha256(previous):
            raise ValueError("manifest predecessor digest is stale or mismatched")
        if _semantic_sha256(current) == _semantic_sha256(previous):
            raise ValueError("a new manifest version must change membership or policy")


def validate_manifest_against_linkage_snapshot(
    manifest: CohortManifest,
    snapshot: ActiveLinkageSnapshot,
    *,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> None:
    authorities = {
        item.provider_namespace: item for item in manifest.provider_authorities
    }
    expected_providers = set(authorities)
    if set(expected_trust_snapshot_sha256_by_provider) != expected_providers:
        raise ValueError("provider authority set is not independently pinned")
    common = (
        snapshot.store_id,
        snapshot.store_epoch_sha256,
        snapshot.storage_identity_sha256,
        snapshot.trust_pins_sha256,
        snapshot.state_version,
        snapshot.state_head_sha256,
    )
    for provider, authority in authorities.items():
        if (
            authority.trust_snapshot_sha256
            != expected_trust_snapshot_sha256_by_provider[provider]
        ):
            raise ValueError("provider trust authority does not match independent pin")
        if (
            authority.store_id,
            authority.store_epoch_sha256,
            authority.storage_identity_sha256,
            authority.trust_pins_sha256,
            authority.state_version,
            authority.state_head_sha256,
        ) != common:
            raise ValueError("provider authority does not bind the exact live snapshot")
    active = {
        (revision.provider_namespace, revision.linkage_id, revision.revision): (
            revision,
            receipt,
        )
        for revision, receipt in zip(snapshot.revisions, snapshot.receipts, strict=True)
    }
    for member in manifest.members:
        item = active.get(
            (member.provider_namespace, member.linkage_id, member.linkage_revision)
        )
        if item is None:
            raise ValueError("cohort member linkage is stale, tombstoned, or unknown")
        revision, receipt = item
        if revision.operation == LinkageOperation.TOMBSTONE:
            raise ValueError("tombstoned linkage cannot enter a cohort")
        if member.linkage_revision_sha256 != linkage_revision_sha256(revision):
            raise ValueError("member linkage digest does not match live authority")
        if member.committed_receipt_sha256 != committed_linkage_receipt_sha256(receipt):
            raise ValueError("member receipt does not match live authority")
        biological = revision.biological
        technical = revision.technical
        if (
            member.subject_token,
            member.collection_token,
            member.specimen_token,
            member.analysis_record_id,
            member.run_token,
            member.reanalysis_of,
        ) != (
            biological.subject_token,
            biological.collection_token,
            biological.specimen_token,
            technical.analysis_record_id,
            technical.run.token,
            technical.reanalysis_of.token,
        ):
            raise ValueError("member lineage does not match exact linkage revision")


__all__ = [
    "CohortManifest",
    "CohortMember",
    "MeasurementAnchor",
    "MemberLineageRole",
    "PolicyDigests",
    "ProviderAuthorityReference",
    "TechnicalReplicateRule",
    "TimeAxis",
    "TimeAxisKind",
    "build_cohort_manifest",
    "cohort_manifest_bytes",
    "cohort_manifest_sha256",
    "validate_manifest_against_linkage_snapshot",
    "validate_manifest_history",
]
