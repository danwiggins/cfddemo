"""Immutable D05 cohort manifests over a live protected linkage store.

Synthetic/local contract only. It defines lineage and denominators and makes no
clinical, scientific, identity, or provider-approval claim.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.method_registry import (
    RegistryContract,
    RegistryIdentityError,
    Sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage import (
    AnalysisRecordId,
    CollectionToken,
    LinkageId,
    ProviderNamespace,
    RunToken,
    SpecimenToken,
    SubjectToken,
    UnitOfAnalysis,
    linkage_revision_sha256,
)
from evidence_inspector.provider_linkage_store import (
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    committed_linkage_receipt_sha256,
)

MAX_MEMBERS = 100_000
CohortId = Annotated[str, StringConstraints(pattern=r"^cohort_[0-9a-f]{32}$")]
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_VERIFY_CURRENT_RECEIPT = ProviderLinkageStore.verify_current_receipt
_PINNED_STORE_CALLABLES = {
    name: getattr(ProviderLinkageStore, name)
    for name in vars(ProviderLinkageStore)
    if callable(getattr(ProviderLinkageStore, name))
}


def _utc_second(value: datetime, field: str = "created_at") -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError(f"{field} must be UTC at whole-second precision")
    return value


def _domain_sha256(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(domain + b"\0" + encoded).hexdigest()


def trust_pins_sha256(pins: Mapping[str, str]) -> str:
    return _domain_sha256(b"traceback-linkage-trust-pins-v1", sorted(pins.items()))


class TechnicalReplicateRule(StrEnum):
    COLLAPSE = "collapse"
    EXCLUDE = "exclude"


class ReanalysisRule(StrEnum):
    COLLAPSE_TO_SOURCE = "collapse_to_source"
    EXCLUDE = "exclude"


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
    run_token: RunToken
    technical_replicate_of: AnalysisRecordId | None = None
    reanalysis_of: AnalysisRecordId | None = None
    lineage_role: MemberLineageRole
    analysis_unit_token: str = Field(
        pattern=r"^(?:subject|collection|specimen)_[0-9a-f]{32}$"
    )
    denominator_contribution: bool
    linkage_event_sha256: Sha256
    time_coordinate: int
    time_coordinate_sha256: Sha256


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
    reanalysis_rule: ReanalysisRule
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
        order = [
            (m.time_coordinate, m.provider_namespace, m.linkage_id, m.linkage_revision)
            for m in self.members
        ]
        exact_keys = [
            (m.provider_namespace, m.linkage_id, m.linkage_revision)
            for m in self.members
        ]
        if order != sorted(order) or len(exact_keys) != len(set(exact_keys)):
            raise ValueError("cohort members must use canonical longitudinal order")
        if {m.provider_namespace for m in self.members} != set(authority_keys):
            raise ValueError("every and only member providers require exact authority")
        analyses = {m.analysis_record_id: m for m in self.members}
        if len(analyses) != len(self.members):
            raise ValueError("analysis records cannot be counted twice")
        groups: dict[str, list[CohortMember]] = defaultdict(list)
        biological_groups: dict[tuple[str, str, str], list[CohortMember]] = defaultdict(
            list
        )
        for member in self.members:
            expected_token = {
                UnitOfAnalysis.SUBJECT: member.subject_token,
                UnitOfAnalysis.COLLECTION: member.collection_token,
                UnitOfAnalysis.SPECIMEN: member.specimen_token,
            }[self.unit_of_analysis]
            if member.analysis_unit_token != expected_token:
                raise ValueError("member analysis unit does not match declared lineage")
            if (member.lineage_role == MemberLineageRole.REANALYSIS) != (
                member.reanalysis_of is not None
            ):
                raise ValueError("reanalysis role must bind its source analysis")
            if (member.lineage_role == MemberLineageRole.TECHNICAL_REPLICATE) != (
                member.technical_replicate_of is not None
            ):
                raise ValueError(
                    "technical replicate role must bind its source analysis"
                )
            if (
                member.reanalysis_of is not None
                and member.technical_replicate_of is not None
            ):
                raise ValueError("one member cannot be both replicate and reanalysis")
            expected_time = _domain_sha256(
                b"traceback-cohort-time-coordinate-v1",
                {
                    "collection_token": member.collection_token,
                    "linkage_event_sha256": member.linkage_event_sha256,
                    "time_axis": self.time_axis.model_dump(mode="json"),
                    "time_coordinate": member.time_coordinate,
                },
            )
            if member.time_coordinate_sha256 != expected_time:
                raise ValueError(
                    "member time coordinate is not bound to its linkage event"
                )
            groups[expected_token].append(member)
            biological_groups[
                (member.subject_token, member.collection_token, member.specimen_token)
            ].append(member)
        for members in groups.values():
            contributors = [m for m in members if m.denominator_contribution]
            if (
                len(contributors) != 1
                or contributors[0].lineage_role != MemberLineageRole.BIOLOGICAL_DRAW
            ):
                raise ValueError(
                    "each declared analysis unit contributes one denominator"
                )
        for members in biological_groups.values():
            draws = [
                m
                for m in members
                if m.lineage_role == MemberLineageRole.BIOLOGICAL_DRAW
            ]
            if len(draws) > 1:
                raise ValueError("technical reruns cannot become biological draws")
        for member in self.members:
            if member.lineage_role == MemberLineageRole.TECHNICAL_REPLICATE:
                if self.technical_replicate_rule == TechnicalReplicateRule.EXCLUDE:
                    raise ValueError("technical replicate policy excludes this member")
                source = analyses.get(member.technical_replicate_of)
                if source is None:
                    raise ValueError(
                        "technical replicate source is outside the manifest"
                    )
                if (
                    source.subject_token,
                    source.collection_token,
                    source.specimen_token,
                ) != (
                    member.subject_token,
                    member.collection_token,
                    member.specimen_token,
                ):
                    raise ValueError(
                        "technical replicate source has different biological lineage"
                    )
            elif member.lineage_role == MemberLineageRole.REANALYSIS:
                if self.reanalysis_rule == ReanalysisRule.EXCLUDE:
                    raise ValueError("reanalysis policy excludes this member")
                source = analyses.get(member.reanalysis_of)
                if source is None:
                    raise ValueError("reanalysis source is outside the manifest")
                if (
                    source.subject_token,
                    source.collection_token,
                    source.specimen_token,
                ) != (
                    member.subject_token,
                    member.collection_token,
                    member.specimen_token,
                ):
                    raise ValueError(
                        "reanalysis source has different biological lineage"
                    )
        for start in analyses:
            seen: set[str] = set()
            current = start
            while True:
                member = analyses[current]
                source = member.technical_replicate_of or member.reanalysis_of
                if source is None:
                    break
                if current in seen:
                    raise ValueError(
                        "combined analysis dependency graph contains a cycle"
                    )
                seen.add(current)
                if source not in analyses:
                    break
                current = source
        return self


def cohort_manifest_bytes(manifest: CohortManifest) -> bytes:
    return canonical_contract_bytes(manifest)


def cohort_manifest_from_bytes(content: bytes) -> CohortManifest:
    try:
        return contract_from_canonical_bytes(CohortManifest, content)
    except RegistryIdentityError as exc:
        raise ValueError("cohort manifest is not canonical") from exc


def cohort_manifest_sha256(manifest: CohortManifest) -> str:
    return hashlib.sha256(cohort_manifest_bytes(manifest)).hexdigest()


def build_cohort_member(
    *,
    revision: object,
    receipt: CommittedLinkageReceipt,
    time_axis: TimeAxis,
    time_coordinate: int,
    lineage_role: MemberLineageRole,
    denominator_contribution: bool,
    unit_of_analysis: UnitOfAnalysis,
    technical_replicate_of: str | None = None,
) -> CohortMember:
    """Project one exact live linkage revision into a bounded cohort member."""

    run_token = revision.technical.run.token
    if run_token is None:
        raise ValueError("cohort membership requires known run lineage")
    event_sha256 = _linkage_event_sha256(revision)
    coordinate_sha256 = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": revision.biological.collection_token,
            "linkage_event_sha256": event_sha256,
            "time_axis": time_axis.model_dump(mode="json"),
            "time_coordinate": time_coordinate,
        },
    )
    analysis_unit = {
        UnitOfAnalysis.SUBJECT: revision.biological.subject_token,
        UnitOfAnalysis.COLLECTION: revision.biological.collection_token,
        UnitOfAnalysis.SPECIMEN: revision.biological.specimen_token,
    }[unit_of_analysis]
    return CohortMember(
        provider_namespace=revision.provider_namespace,
        linkage_id=revision.linkage_id,
        linkage_revision=revision.revision,
        linkage_revision_sha256=linkage_revision_sha256(revision),
        committed_receipt_sha256=committed_linkage_receipt_sha256(receipt),
        subject_token=revision.biological.subject_token,
        collection_token=revision.biological.collection_token,
        specimen_token=revision.biological.specimen_token,
        analysis_record_id=revision.technical.analysis_record_id,
        run_token=run_token,
        technical_replicate_of=technical_replicate_of,
        reanalysis_of=revision.technical.reanalysis_of.token,
        lineage_role=lineage_role,
        analysis_unit_token=analysis_unit,
        denominator_contribution=denominator_contribution,
        linkage_event_sha256=event_sha256,
        time_coordinate=time_coordinate,
        time_coordinate_sha256=coordinate_sha256,
    )


def build_cohort_manifest(
    *,
    provider_authorities: Sequence[ProviderAuthorityReference],
    members: Sequence[CohortMember],
    **values: object,
) -> CohortManifest:
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
                        item.time_coordinate,
                        item.provider_namespace,
                        item.linkage_id,
                        item.linkage_revision,
                    ),
                )
            ),
        }
    )


def _substantive_sha256(manifest: CohortManifest) -> str:
    payload = manifest.model_dump(mode="json")
    for key in (
        "version",
        "previous_manifest_sha256",
        "created_at",
        "provider_authorities",
    ):
        payload.pop(key)
    return _domain_sha256(b"traceback-cohort-substance-v1", payload)


def validate_manifest_history(history: Sequence[CohortManifest]) -> None:
    if not history:
        raise ValueError("manifest history cannot be empty")
    reparsed = tuple(
        cohort_manifest_from_bytes(cohort_manifest_bytes(manifest))
        for manifest in history
    )
    cohort_id = reparsed[0].cohort_id
    for index, manifest in enumerate(reparsed, start=1):
        if manifest.cohort_id != cohort_id or manifest.version != index:
            raise ValueError("manifest history must be one consecutive cohort chain")
    for previous, current in pairwise(reparsed):
        if current.previous_manifest_sha256 != cohort_manifest_sha256(previous):
            raise ValueError("manifest predecessor digest is stale or mismatched")
        if current.created_at <= previous.created_at:
            raise ValueError("manifest creation times must increase strictly")
        if _substantive_sha256(current) == _substantive_sha256(previous):
            raise ValueError("a new version must change membership or cohort policy")


def _linkage_event_sha256(revision: object) -> str:
    return _domain_sha256(
        b"traceback-cohort-linkage-event-v1",
        {
            "provider_namespace": revision.provider_namespace,
            "collection_token": revision.biological.collection_token,
            "source_projection_ref": revision.source_projection_ref,
            "proposed_at": revision.proposed_at.isoformat(),
        },
    )


def validate_manifest_against_linkage_store(
    manifest: CohortManifest,
    store: ProviderLinkageStore,
    *,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> None:
    manifest = cohort_manifest_from_bytes(cohort_manifest_bytes(manifest))
    if type(store) is not ProviderLinkageStore:
        raise TypeError("cohort validation requires the exact live linkage store type")
    for name, pinned in _PINNED_STORE_CALLABLES.items():
        if name in vars(store) or getattr(ProviderLinkageStore, name) is not pinned:
            raise TypeError("live linkage store authority callable was shadowed")
    snapshot = _PINNED_ACTIVE_SNAPSHOT(store)
    authorities = {
        item.provider_namespace: item for item in manifest.provider_authorities
    }
    if set(expected_trust_snapshot_sha256_by_provider) != set(authorities):
        raise ValueError("provider authority set is not independently pinned")
    recomputed_pins = trust_pins_sha256(expected_trust_snapshot_sha256_by_provider)
    if snapshot.trust_pins_sha256 != recomputed_pins:
        raise ValueError("live store trust pins do not match independent pins")
    common = (
        snapshot.store_id,
        snapshot.store_epoch_sha256,
        snapshot.storage_identity_sha256,
        recomputed_pins,
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
            raise ValueError("provider authority does not bind the exact live store")
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
        expected_receipt_common = (
            member.provider_namespace,
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            recomputed_pins,
            snapshot.state_version,
            snapshot.state_head_sha256,
        )
        if (
            receipt.provider_namespace,
            receipt.store_id,
            receipt.store_epoch_sha256,
            receipt.storage_identity_sha256,
            receipt.trust_pins_sha256,
            receipt.state_version,
            receipt.state_head_sha256,
        ) != expected_receipt_common:
            raise ValueError("receipt does not bind every live store authority field")
        _PINNED_VERIFY_CURRENT_RECEIPT(store, receipt)
        if member.linkage_revision_sha256 != linkage_revision_sha256(revision):
            raise ValueError("member linkage digest does not match live authority")
        if member.committed_receipt_sha256 != committed_linkage_receipt_sha256(receipt):
            raise ValueError("member receipt does not match live authority")
        biological, technical = revision.biological, revision.technical
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
        if member.linkage_event_sha256 != _linkage_event_sha256(revision):
            raise ValueError("member time source does not match exact linkage event")
        if member.time_coordinate != int(revision.proposed_at.timestamp()):
            raise ValueError("member time coordinate is not derived from linkage event")


__all__ = [
    "CohortManifest",
    "CohortMember",
    "MeasurementAnchor",
    "MemberLineageRole",
    "PolicyDigests",
    "ProviderAuthorityReference",
    "ReanalysisRule",
    "TechnicalReplicateRule",
    "TimeAxis",
    "TimeAxisKind",
    "build_cohort_manifest",
    "build_cohort_member",
    "cohort_manifest_bytes",
    "cohort_manifest_from_bytes",
    "cohort_manifest_sha256",
    "trust_pins_sha256",
    "validate_manifest_against_linkage_store",
    "validate_manifest_history",
]
