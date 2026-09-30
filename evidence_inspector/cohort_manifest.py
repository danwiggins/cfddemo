"""Immutable D05 cohort manifests over a live protected linkage store.

Synthetic/local contract only. It defines lineage and denominators and makes no
clinical, scientific, identity, or provider-approval claim.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, StringConstraints, model_validator
from pydantic_core import TzInfo

from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    MAX_REVISIONS,
    ApprovalId,
    AnalysisRecordId,
    Base64Signature,
    CollectionToken,
    IssuerId,
    IssuerStatus,
    KeyId,
    LinkageId,
    Nonce,
    PrincipalId,
    ProviderNamespace,
    ProviderRole,
    ProviderTrustSnapshot,
    RunToken,
    SpecimenToken,
    SubjectToken,
    UnitOfAnalysis,
    linkage_revision_sha256,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    ProviderLinkageStoreUnsafe,
    committed_linkage_receipt_sha256,
    provider_linkage_store_time_source_is_pinned,
    require_provider_linkage_store_process_integrity,
)
from evidence_inspector.provider_linkage_store import (
    capture_expected_trust_pins as _capture_expected_trust_pins,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_MEMBERS = 100_000
MAX_TRUSTED_GRAPH_DEPTH = 64
MAX_TRUSTED_GRAPH_NODES = 8_000_000
MAX_TRUSTED_SCALAR_BYTES = 16_384
MAX_COHORT_MANIFEST_BYTES = 512 * 1024 * 1024
# Whole UTC seconds representable by Python's datetime range, years 1..9999.
MIN_TIME_COORDINATE = -62_135_596_800
MAX_TIME_COORDINATE = 253_402_300_799
CohortId = Annotated[str, StringConstraints(pattern=r"^cohort_[0-9a-f]{32}$")]
BiologicalTimepointId = Annotated[
    str, StringConstraints(pattern=r"^timepoint_[0-9a-f]{32}$")
]
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_AUTHORITY_READ_FENCE = ProviderLinkageStore.authority_read_fence
_PINNED_AUTHORITY_TIME_IN_FENCE = ProviderLinkageStore.authority_time_in_fence
_PINNED_TIME_SOURCE_IDENTITY_CHECK = provider_linkage_store_time_source_is_pinned
_PINNED_PROCESS_INTEGRITY_CHECK = require_provider_linkage_store_process_integrity
_PINNED_STORE_CALLABLES = {
    name: getattr(ProviderLinkageStore, name)
    for name in vars(ProviderLinkageStore)
    if callable(getattr(ProviderLinkageStore, name))
}


def _utc_second(value: datetime, field: str = "created_at") -> datetime:
    if type(value) is not datetime:
        raise ValueError(f"{field} must use the exact datetime type")
    timezone = value.tzinfo
    if timezone is not UTC and type(timezone) is not TzInfo:
        raise ValueError(f"{field} must use an approved UTC timezone")
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be UTC at whole-second precision")
    if value.microsecond:
        raise ValueError(f"{field} must be UTC at whole-second precision")
    return value


def _domain_sha256(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(domain + b"\0" + encoded).hexdigest()


def capture_expected_trust_pins(pins: Mapping[str, str]) -> dict[str, str]:
    """Expose D02's bounded one-pass capture with the D05 error contract."""
    try:
        return _capture_expected_trust_pins(pins)
    except ProviderLinkageStoreUnsafe as exc:
        raise ValueError("provider trust pins are invalid") from exc


def _captured_trust_pins_sha256(pins: dict[str, str]) -> str:
    return _domain_sha256(b"traceback-linkage-trust-pins-v1", sorted(pins.items()))


def trust_pins_sha256(pins: Mapping[str, str]) -> str:
    return _captured_trust_pins_sha256(capture_expected_trust_pins(pins))


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


class ProviderDataAuthorityPurpose(StrEnum):
    COLLECTION_EVENT = "collection_event"
    SUBJECT_TIME_ORIGIN = "subject_time_origin"
    STUDY_TIME_ORIGIN = "study_time_origin"


class ProviderDataGrantPayload(RegistryContract):
    schema_version: Literal["traceback.provider-data-grant.v1"] = (
        "traceback.provider-data-grant.v1"
    )
    grant_id: ApprovalId
    provider_namespace: ProviderNamespace
    issuer_id: IssuerId
    key_id: KeyId
    principal_id: PrincipalId
    role: ProviderRole
    allowed_purposes: tuple[ProviderDataAuthorityPurpose, ...] = Field(
        min_length=1, max_length=3
    )
    trust_snapshot_id: str = Field(pattern=r"^trust_[0-9a-f]{32}$")
    trust_snapshot_revision: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
    trust_snapshot_sha256: Sha256
    nonce: Nonce
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def exact_grant(self) -> ProviderDataGrantPayload:
        _utc_second(self.issued_at, "data grant issued_at")
        _utc_second(self.expires_at, "data grant expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("provider data grant window is invalid")
        if self.role != ProviderRole.LINKER:
            raise ValueError("provider data grant requires the linker role")
        if self.allowed_purposes != tuple(
            sorted(set(self.allowed_purposes), key=str)
        ):
            raise ValueError("provider data purposes must be uniquely sorted")
        return self


class SignedProviderDataGrant(RegistryContract):
    payload: ProviderDataGrantPayload
    signature_base64: Base64Signature


class ProviderDataAuthorityPayload(RegistryContract):
    schema_version: Literal["traceback.provider-data-authority.v1"] = (
        "traceback.provider-data-authority.v1"
    )
    authority_id: ApprovalId
    provider_namespace: ProviderNamespace
    issuer_id: IssuerId
    key_id: KeyId
    principal_id: PrincipalId
    role: ProviderRole
    purpose: ProviderDataAuthorityPurpose
    target_sha256: Sha256
    trust_snapshot_id: str = Field(pattern=r"^trust_[0-9a-f]{32}$")
    trust_snapshot_revision: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
    trust_snapshot_sha256: Sha256
    nonce: Nonce
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def coherent_window(self) -> ProviderDataAuthorityPayload:
        _utc_second(self.issued_at, "authority issued_at")
        _utc_second(self.expires_at, "authority expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("provider data authority window is invalid")
        if self.role != ProviderRole.LINKER:
            raise ValueError("provider data authority requires the linker role")
        return self


class SignedProviderDataAuthority(RegistryContract):
    grant: SignedProviderDataGrant
    payload: ProviderDataAuthorityPayload
    signature_base64: Base64Signature


def provider_data_authority_payload_bytes(
    payload: ProviderDataAuthorityPayload,
) -> bytes:
    return _trusted_contract_bytes(payload)


def provider_data_grant_payload_bytes(payload: ProviderDataGrantPayload) -> bytes:
    return _trusted_contract_bytes(payload)


class PolicyDigests(RegistryContract):
    inclusion_sha256: Sha256
    exclusion_sha256: Sha256
    missingness_sha256: Sha256


class TimeAxis(RegistryContract):
    kind: TimeAxisKind
    definition_sha256: Sha256
    unit_sha256: Sha256
    origin_authority_sha256: Sha256


class CollectionEventReference(RegistryContract):
    """Exact provider-authority input for one biological collection event."""

    schema_version: Literal["traceback.collection-event-reference.v1"] = (
        "traceback.collection-event-reference.v1"
    )
    provider_namespace: ProviderNamespace
    subject_token: SubjectToken
    collection_token: CollectionToken
    collected_at: datetime
    authority: SignedProviderDataAuthority

    @model_validator(mode="after")
    def exact_collection_time(self) -> CollectionEventReference:
        _utc_second(self.collected_at, "collected_at")
        payload = self.authority.payload
        if (
            payload.provider_namespace != self.provider_namespace
            or payload.purpose != ProviderDataAuthorityPurpose.COLLECTION_EVENT
            or payload.target_sha256 != collection_event_statement_sha256(self)
        ):
            raise ValueError("collection event authority does not bind exact event")
        return self


def collection_event_statement_sha256(event: CollectionEventReference) -> str:
    return _domain_sha256(
        b"traceback-collection-event-statement-v1",
        {
            "provider_namespace": event.provider_namespace,
            "subject_token": event.subject_token,
            "collection_token": event.collection_token,
            "collected_at": event.collected_at.isoformat(),
        },
    )


class TimeOriginReference(RegistryContract):
    schema_version: Literal["traceback.time-origin-reference.v1"] = (
        "traceback.time-origin-reference.v1"
    )
    provider_namespace: ProviderNamespace
    subject_token: SubjectToken | None
    kind: Literal[TimeAxisKind.SUBJECT_RELATIVE, TimeAxisKind.STUDY_RELATIVE]
    origin_time: datetime
    axis_definition_sha256: Sha256
    authority: SignedProviderDataAuthority

    @model_validator(mode="after")
    def exact_origin_authority(self) -> TimeOriginReference:
        _utc_second(self.origin_time, "origin_time")
        expected_purpose = (
            ProviderDataAuthorityPurpose.SUBJECT_TIME_ORIGIN
            if self.kind == TimeAxisKind.SUBJECT_RELATIVE
            else ProviderDataAuthorityPurpose.STUDY_TIME_ORIGIN
        )
        if (self.kind == TimeAxisKind.SUBJECT_RELATIVE) != (
            self.subject_token is not None
        ):
            raise ValueError("subject-relative origins require one exact subject")
        payload = self.authority.payload
        if (
            payload.provider_namespace != self.provider_namespace
            or payload.purpose != expected_purpose
            or payload.target_sha256 != time_origin_statement_sha256(self)
        ):
            raise ValueError("time-origin authority does not bind exact origin")
        return self


def time_origin_statement_sha256(origin: TimeOriginReference) -> str:
    return _domain_sha256(
        b"traceback-time-origin-statement-v1",
        {
            "provider_namespace": origin.provider_namespace,
            "subject_token": origin.subject_token,
            "kind": origin.kind.value,
            "origin_time": origin.origin_time.isoformat(),
            "axis_definition_sha256": origin.axis_definition_sha256,
        },
    )


def time_origin_authority_sha256(origins: Sequence[TimeOriginReference]) -> str:
    return _domain_sha256(
        b"traceback-time-origin-authority-v1",
        [
            hashlib.sha256(_trusted_contract_bytes(origin)).hexdigest()
            for origin in origins
        ],
    )


def collection_event_reference_sha256(event: CollectionEventReference) -> str:
    return hashlib.sha256(_trusted_contract_bytes(event)).hexdigest()


def biological_timepoint_id(event: CollectionEventReference) -> str:
    digest = _domain_sha256(
        b"traceback-biological-timepoint-v1",
        {
            "provider_namespace": event.provider_namespace,
            "subject_token": event.subject_token,
            "collection_token": event.collection_token,
        },
    )
    return f"timepoint_{digest[:32]}"


def collection_time_coordinate(
    event: CollectionEventReference,
    axis: TimeAxis,
    origin: TimeOriginReference | None = None,
) -> int:
    """Derive the only permitted coordinate from collection-event semantics."""

    if axis.kind == TimeAxisKind.COLLECTION_TIME:
        if origin is not None:
            raise ValueError("collection-time coordinates cannot use an origin")
        return int(event.collected_at.timestamp())
    if origin is None:
        raise ValueError("relative coordinates require an authorized origin")
    return int((event.collected_at - origin.origin_time).total_seconds())


class MeasurementAnchor(RegistryContract):
    measurement_definition_sha256: Sha256
    anchor_definition_sha256: Sha256
    authority_sha256: Sha256


class ProviderAuthorityReference(RegistryContract):
    provider_namespace: ProviderNamespace
    trust_snapshot_sha256: Sha256
    trust_snapshot_json: str = Field(min_length=1, max_length=16_384)
    store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    store_epoch_sha256: Sha256
    storage_identity_sha256: Sha256
    trust_pins_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
    state_head_sha256: Sha256

    @model_validator(mode="after")
    def exact_trust_snapshot(self) -> ProviderAuthorityReference:
        trust = _provider_trust_snapshot(self)
        if (
            trust.provider_namespace != self.provider_namespace
            or provider_trust_snapshot_sha256(trust) != self.trust_snapshot_sha256
        ):
            raise ValueError("provider authority does not bind exact trust snapshot")
        return self


def _provider_trust_snapshot(
    authority: ProviderAuthorityReference,
) -> ProviderTrustSnapshot:
    try:
        encoded = authority.trust_snapshot_json.encode("utf-8")
        trust = ProviderTrustSnapshot.model_validate_json(encoded)
    except (UnicodeError, ValueError):
        raise ValueError("provider trust snapshot JSON is invalid") from None
    if canonical_contract_bytes(trust) != encoded:
        raise ValueError("provider trust snapshot JSON is not canonical")
    return trust


class CohortMember(RegistryContract):
    provider_namespace: ProviderNamespace
    linkage_id: LinkageId
    linkage_revision: int = Field(ge=1, le=MAX_REVISIONS, strict=True)
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
    collection_event_sha256: Sha256
    biological_timepoint_id: BiologicalTimepointId
    time_coordinate: int = Field(
        ge=MIN_TIME_COORDINATE, le=MAX_TIME_COORDINATE, strict=True
    )
    time_coordinate_sha256: Sha256


class CohortManifest(RegistryContract):
    schema_version: Literal["traceback.cohort-manifest.v2"] = (
        "traceback.cohort-manifest.v2"
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
    collection_events: tuple[CollectionEventReference, ...] = Field(
        min_length=1, max_length=MAX_MEMBERS
    )
    time_origins: tuple[TimeOriginReference, ...] = Field(
        default=(), max_length=MAX_MEMBERS
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
        event_keys = [
            (event.provider_namespace, event.subject_token, event.collection_token)
            for event in self.collection_events
        ]
        if event_keys != sorted(event_keys) or len(event_keys) != len(set(event_keys)):
            raise ValueError("collection events must be uniquely sorted")
        events = {
            (
                event.provider_namespace,
                event.subject_token,
                event.collection_token,
            ): event
            for event in self.collection_events
        }
        origin_keys = [
            (origin.provider_namespace, origin.subject_token or "")
            for origin in self.time_origins
        ]
        if origin_keys != sorted(origin_keys) or len(origin_keys) != len(
            set(origin_keys)
        ):
            raise ValueError("time origins must be uniquely sorted")
        origins = {
            (origin.provider_namespace, origin.subject_token): origin
            for origin in self.time_origins
        }
        member_subjects = {
            (member.provider_namespace, member.subject_token) for member in self.members
        }
        if self.time_axis.kind == TimeAxisKind.COLLECTION_TIME:
            if self.time_origins:
                raise ValueError("collection-time manifests cannot declare origins")
            if self.time_axis.origin_authority_sha256 != time_origin_authority_sha256(
                ()
            ):
                raise ValueError(
                    "collection-time axes require the canonical empty origin authority"
                )
        else:
            if self.time_axis.origin_authority_sha256 != time_origin_authority_sha256(
                self.time_origins
            ):
                raise ValueError("time axis does not bind exact origin authorities")
            if any(
                origin.kind != self.time_axis.kind
                or origin.axis_definition_sha256
                != self.time_axis.definition_sha256
                for origin in self.time_origins
            ):
                raise ValueError("time origin does not match selected axis")
            if self.time_axis.kind == TimeAxisKind.SUBJECT_RELATIVE:
                if set(origins) != member_subjects:
                    raise ValueError(
                        "subject-relative axes require one origin per subject"
                    )
            else:
                providers = {member.provider_namespace for member in self.members}
                if set(origins) != {(provider, None) for provider in providers}:
                    raise ValueError(
                        "study-relative axes require one origin per provider"
                    )
                if len({origin.origin_time for origin in self.time_origins}) != 1:
                    raise ValueError(
                        "study-relative providers must authorize one origin"
                    )
        analyses = {
            (m.provider_namespace, m.analysis_record_id): m for m in self.members
        }
        if len(analyses) != len(self.members):
            raise ValueError("analysis records cannot be counted twice")
        groups: dict[tuple[str, str], list[CohortMember]] = defaultdict(list)
        biological_groups: dict[tuple[str, str, str, str], list[CohortMember]] = (
            defaultdict(list)
        )
        for member in self.members:
            event = events.get(
                (
                    member.provider_namespace,
                    member.subject_token,
                    member.collection_token,
                )
            )
            if event is None:
                raise ValueError("every member requires its exact collection event")
            origin = None
            if self.time_axis.kind == TimeAxisKind.SUBJECT_RELATIVE:
                origin = origins.get(
                    (member.provider_namespace, member.subject_token)
                )
            elif self.time_axis.kind == TimeAxisKind.STUDY_RELATIVE:
                origin = origins.get((member.provider_namespace, None))
            if (
                member.collection_event_sha256
                != collection_event_reference_sha256(event)
                or member.biological_timepoint_id != biological_timepoint_id(event)
                or member.time_coordinate
                != collection_time_coordinate(event, self.time_axis, origin)
            ):
                raise ValueError(
                    "member timepoint is not derived from its collection event"
                )
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
            if (
                member.lineage_role != MemberLineageRole.BIOLOGICAL_DRAW
                and member.denominator_contribution
            ):
                raise ValueError(
                    "technical replicates and reanalysis cannot contribute denominators"
                )
            expected_time = _domain_sha256(
                b"traceback-cohort-time-coordinate-v1",
                {
                    "collection_token": member.collection_token,
                    "collection_event_sha256": member.collection_event_sha256,
                    "biological_timepoint_id": member.biological_timepoint_id,
                    "time_axis": self.time_axis.model_dump(mode="json"),
                    "time_coordinate": member.time_coordinate,
                },
            )
            if member.time_coordinate_sha256 != expected_time:
                raise ValueError(
                    "member time coordinate is not bound to its collection event"
                )
            groups[(member.provider_namespace, expected_token)].append(member)
            biological_groups[
                (
                    member.provider_namespace,
                    member.subject_token,
                    member.collection_token,
                    member.specimen_token,
                )
            ].append(member)
        if set(events) != {
            (member.provider_namespace, member.subject_token, member.collection_token)
            for member in self.members
        }:
            raise ValueError("collection events cannot be unused or missing")
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
                source = analyses.get(
                    (member.provider_namespace, member.technical_replicate_of)
                )
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
                if source.biological_timepoint_id != member.biological_timepoint_id:
                    raise ValueError("technical replicate cannot create a timepoint")
            elif member.lineage_role == MemberLineageRole.REANALYSIS:
                if self.reanalysis_rule == ReanalysisRule.EXCLUDE:
                    raise ValueError("reanalysis policy excludes this member")
                source = analyses.get((member.provider_namespace, member.reanalysis_of))
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
                if source.biological_timepoint_id != member.biological_timepoint_id:
                    raise ValueError("reanalysis cannot create a timepoint")
        for start in analyses:
            seen: set[tuple[str, str]] = set()
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
                source_key = (member.provider_namespace, source)
                if source_key not in analyses:
                    break
                current = source_key
        return self


_TRUSTED_MODEL_TYPES, _TRUSTED_ENUM_TYPES = contract_type_graph(
    CohortManifest,
    ProviderTrustSnapshot,
)


def _require_trusted_graph(value: object) -> None:
    """Reject caller-owned executable object shape before any serialization."""

    # Retain the D05-specific diagnostics while exact_model_bytes below remains
    # the authoritative closed-graph and expanded-cost check.
    stack: list[tuple[object, int, bool, int | None]] = [(value, 0, False, None)]
    active: set[int] = set()
    visited: set[int] = set()
    nodes = 0
    while stack:
        item, depth, exiting, tuple_bound = stack.pop()
        item_type = type(item)
        if exiting:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_TRUSTED_GRAPH_NODES:
            raise ValueError("cohort authority graph exceeds its node budget")
        if depth > MAX_TRUSTED_GRAPH_DEPTH:
            raise ValueError("cohort authority graph exceeds its depth budget")
        if item is None or item_type is bool:
            continue
        if item_type is int:
            if item.bit_length() > 64:
                raise ValueError("cohort manifest is not canonical")
            continue
        if item_type is str:
            scalar_bound = (
                MAX_TRUSTED_SCALAR_BYTES if tuple_bound is None else tuple_bound
            )
            try:
                encoded_length = len(item.encode("utf-8"))
            except UnicodeError:
                raise ValueError("cohort manifest is not canonical") from None
            if len(item) > scalar_bound or encoded_length > scalar_bound:
                raise ValueError("cohort authority graph scalar is oversized")
            continue
        if item_type is datetime:
            _utc_second(item, "contract timestamp")
            continue
        if item_type in _TRUSTED_ENUM_TYPES:
            continue
        if item_type is not tuple and item_type not in _TRUSTED_MODEL_TYPES:
            raise TypeError("cohort authority graph contains an untrusted object type")
        identity = id(item)
        if identity in active:
            raise ValueError("cohort authority graph contains a cycle")
        if identity in visited:
            continue
        active.add(identity)
        visited.add(identity)
        stack.append((item, depth, True, tuple_bound))
        if item_type is tuple:
            bound = MAX_MEMBERS if tuple_bound is None else tuple_bound
            if len(item) > bound:
                raise ValueError("cohort authority graph tuple is oversized")
            for child in reversed(item):
                stack.append((child, depth + 1, False, None))
            continue
        values = object.__getattribute__(item, "__dict__")
        fields = vars(item_type).get("__pydantic_fields__")
        extra = object.__getattribute__(item, "__pydantic_extra__")
        private = object.__getattribute__(item, "__pydantic_private__")
        if (
            type(values) is not dict
            or type(fields) is not dict
            or set(values) != set(fields)
            or (extra is not None and (type(extra) is not dict or extra))
            or (private is not None and (type(private) is not dict or private))
        ):
            raise ValueError("cohort manifest is not canonical")
        for name in reversed(tuple(fields)):
            field = fields[name]
            maximum = next(
                (
                    constraint.max_length
                    for constraint in object.__getattribute__(field, "metadata")
                    if getattr(constraint, "max_length", None) is not None
                ),
                None,
            )
            stack.append((values[name], depth + 1, False, maximum))


def _trusted_contract_bytes(value: RegistryContract) -> bytes:
    value_type = type(value)
    if value_type not in _TRUSTED_MODEL_TYPES:
        raise TypeError("cohort authority graph contains an untrusted object type")
    _require_trusted_graph(value)
    return exact_model_bytes(
        value,
        value_type,
        model_types=_TRUSTED_MODEL_TYPES,
        enum_types=_TRUSTED_ENUM_TYPES,
        max_bytes=MAX_COHORT_MANIFEST_BYTES,
        max_nodes=MAX_TRUSTED_GRAPH_NODES,
        max_depth=MAX_TRUSTED_GRAPH_DEPTH,
        max_collection_items=MAX_MEMBERS,
        max_string_bytes=MAX_TRUSTED_SCALAR_BYTES,
    )


def cohort_manifest_bytes(manifest: CohortManifest) -> bytes:
    if type(manifest) is not CohortManifest:
        raise TypeError("cohort manifest requires the exact current schema type")
    return _trusted_contract_bytes(manifest)


def cohort_manifest_from_bytes(content: bytes) -> CohortManifest:
    try:
        decoded = bounded_json_loads(
            content,
            max_bytes=MAX_COHORT_MANIFEST_BYTES,
            max_depth=MAX_TRUSTED_GRAPH_DEPTH,
            max_nodes=MAX_TRUSTED_GRAPH_NODES,
            max_collection_items=MAX_MEMBERS,
            max_string_bytes=MAX_TRUSTED_SCALAR_BYTES,
        )
    except (TypeError, ValueError):
        raise ValueError("cohort manifest is not canonical") from None
    if type(decoded) is dict and decoded.get("schema_version") == (
        "traceback.cohort-manifest.v1"
    ):
        raise ValueError(
            "legacy cohort manifest v1 is historical-only because it used "
            "linkage proposal time; rebuild v2 from collection-event authority"
        )
    try:
        manifest = CohortManifest.model_validate(decoded)
        if cohort_manifest_bytes(manifest) != content:
            raise ValueError("cohort manifest bytes are not canonical")
        return manifest
    except (TypeError, ValueError):
        raise ValueError("cohort manifest is not canonical") from None


def cohort_manifest_sha256(manifest: CohortManifest) -> str:
    return hashlib.sha256(cohort_manifest_bytes(manifest)).hexdigest()


def build_cohort_member(
    *,
    revision: object,
    receipt: CommittedLinkageReceipt,
    collection_event: CollectionEventReference,
    time_axis: TimeAxis,
    time_origin: TimeOriginReference | None = None,
    lineage_role: MemberLineageRole,
    denominator_contribution: bool,
    unit_of_analysis: UnitOfAnalysis,
    technical_replicate_of: str | None = None,
) -> CohortMember:
    """Project one exact live linkage revision into a bounded cohort member."""

    run_token = revision.technical.run.token
    if run_token is None:
        raise ValueError("cohort membership requires known run lineage")
    if (
        collection_event.provider_namespace,
        collection_event.subject_token,
        collection_event.collection_token,
    ) != (
        revision.provider_namespace,
        revision.biological.subject_token,
        revision.biological.collection_token,
    ):
        raise ValueError("collection event does not match linkage lineage")
    event_sha256 = collection_event_reference_sha256(collection_event)
    timepoint_id = biological_timepoint_id(collection_event)
    if time_origin is not None:
        if (
            time_origin.provider_namespace != revision.provider_namespace
            or time_origin.kind != time_axis.kind
            or time_origin.axis_definition_sha256 != time_axis.definition_sha256
            or (
                time_axis.kind == TimeAxisKind.SUBJECT_RELATIVE
                and time_origin.subject_token != revision.biological.subject_token
            )
            or (
                time_axis.kind == TimeAxisKind.STUDY_RELATIVE
                and time_origin.subject_token is not None
            )
        ):
            raise ValueError("time origin does not match linkage and axis")
    time_coordinate = collection_time_coordinate(
        collection_event, time_axis, time_origin
    )
    coordinate_sha256 = _domain_sha256(
        b"traceback-cohort-time-coordinate-v1",
        {
            "collection_token": revision.biological.collection_token,
            "collection_event_sha256": event_sha256,
            "biological_timepoint_id": timepoint_id,
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
        collection_event_sha256=event_sha256,
        biological_timepoint_id=timepoint_id,
        time_coordinate=time_coordinate,
        time_coordinate_sha256=coordinate_sha256,
    )


def build_cohort_manifest(
    *,
    provider_authorities: Sequence[ProviderAuthorityReference],
    collection_events: Sequence[CollectionEventReference],
    time_origins: Sequence[TimeOriginReference] = (),
    members: Sequence[CohortMember],
    **values: object,
) -> CohortManifest:
    return CohortManifest.model_validate(
        {
            **values,
            "provider_authorities": tuple(
                sorted(provider_authorities, key=lambda item: item.provider_namespace)
            ),
            "collection_events": tuple(
                sorted(
                    collection_events,
                    key=lambda item: (
                        item.provider_namespace,
                        item.subject_token,
                        item.collection_token,
                    ),
                )
            ),
            "time_origins": tuple(
                sorted(
                    time_origins,
                    key=lambda item: (
                        item.provider_namespace,
                        item.subject_token or "",
                    ),
                )
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
    payload["collection_events"] = [
        collection_event_statement_sha256(event)
        for event in manifest.collection_events
    ]
    payload["time_origins"] = [
        time_origin_statement_sha256(origin) for origin in manifest.time_origins
    ]
    payload["time_axis"].pop("origin_authority_sha256")
    for member in payload["members"]:
        member.pop("collection_event_sha256")
        member.pop("time_coordinate_sha256")
    return _domain_sha256(b"traceback-cohort-substance-v1", payload)


def validate_manifest_history(history: Sequence[CohortManifest]) -> None:
    if type(history) not in {tuple, list}:
        raise TypeError("manifest history requires an exact tuple or list")
    if not history:
        raise ValueError("manifest history cannot be empty")
    for manifest in history:
        _require_trusted_graph(manifest)
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


def _verify_provider_data_authority(
    authority: SignedProviderDataAuthority,
    trust: ProviderTrustSnapshot,
    *,
    manifest_created_at: datetime,
    evaluated_at: datetime,
) -> None:
    payload = authority.payload
    grant = authority.grant.payload
    trust_sha256 = provider_trust_snapshot_sha256(trust)
    if (
        payload.provider_namespace != trust.provider_namespace
        or payload.trust_snapshot_id != trust.snapshot_id
        or payload.trust_snapshot_revision != trust.revision
        or payload.trust_snapshot_sha256 != trust_sha256
    ):
        raise ValueError("provider data authority does not bind trusted snapshot")
    if (
        grant.provider_namespace != payload.provider_namespace
        or grant.issuer_id != payload.issuer_id
        or grant.key_id != payload.key_id
        or grant.principal_id != payload.principal_id
        or grant.role != payload.role
        or grant.trust_snapshot_id != payload.trust_snapshot_id
        or grant.trust_snapshot_revision != payload.trust_snapshot_revision
        or grant.trust_snapshot_sha256 != trust_sha256
    ):
        raise ValueError("provider data grant does not bind authority and trust")
    if not (
        trust.issued_at
        <= grant.issued_at
        <= manifest_created_at
        <= evaluated_at
        < grant.expires_at
        <= trust.expires_at
    ):
        raise ValueError("provider data grant window is outside trusted snapshot")
    if not (
        grant.issued_at
        <= payload.issued_at
        <= manifest_created_at
        <= evaluated_at
        < payload.expires_at
        <= grant.expires_at
        <= trust.expires_at
    ):
        raise ValueError("provider data authority window is outside trusted snapshot")
    issuer = next(
        (
            item
            for item in trust.issuers
            if (item.issuer_id, item.key_id) == (payload.issuer_id, payload.key_id)
        ),
        None,
    )
    if (
        issuer is None
        or issuer.status != IssuerStatus.ACTIVE
        or ProviderRole.LINKER not in issuer.allowed_roles
    ):
        raise ValueError("provider data grant issuer is not trusted")
    if payload.purpose not in grant.allowed_purposes:
        raise ValueError("provider data authority purpose is not explicitly granted")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(issuer.public_key_base64, validate=True)
        )
        public_key.verify(
            base64.b64decode(authority.grant.signature_base64, validate=True),
            provider_data_grant_payload_bytes(grant),
        )
        public_key.verify(
            base64.b64decode(authority.signature_base64, validate=True),
            provider_data_authority_payload_bytes(payload),
        )
    except (ValueError, InvalidSignature):
        raise ValueError("provider data authority signature is invalid") from None


def _validate_manifest_against_linkage_store_in_fence(
    manifest: CohortManifest,
    store: ProviderLinkageStore,
    *,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> None:
    manifest = cohort_manifest_from_bytes(cohort_manifest_bytes(manifest))
    expected_pins = capture_expected_trust_pins(
        expected_trust_snapshot_sha256_by_provider
    )
    snapshot = _PINNED_ACTIVE_SNAPSHOT(store)
    evaluated_at = _PINNED_AUTHORITY_TIME_IN_FENCE(store)
    authorities = {
        item.provider_namespace: item for item in manifest.provider_authorities
    }
    trusted_snapshots = {
        provider: _provider_trust_snapshot(authority)
        for provider, authority in authorities.items()
    }
    if set(trusted_snapshots) != set(authorities):
        raise ValueError("provider trust snapshot set is not exact")
    manifest_events = {
        (event.provider_namespace, event.subject_token, event.collection_token): event
        for event in manifest.collection_events
    }
    manifest_origins = {
        (origin.provider_namespace, origin.subject_token): origin
        for origin in manifest.time_origins
    }
    if set(expected_pins) != set(authorities):
        raise ValueError("provider authority set is not independently pinned")
    for provider, trust in trusted_snapshots.items():
        if (
            trust.provider_namespace != provider
            or provider_trust_snapshot_sha256(trust) != expected_pins[provider]
        ):
            raise ValueError("provider trust snapshot does not match independent pin")
    data_authorities = [
        event.authority for event in manifest.collection_events
    ] + [origin.authority for origin in manifest.time_origins]
    authority_ids = [item.payload.authority_id for item in data_authorities]
    nonces = [item.payload.nonce for item in data_authorities]
    grant_ids = [item.grant.payload.grant_id for item in data_authorities]
    grant_nonces = [item.grant.payload.nonce for item in data_authorities]
    if len(authority_ids) != len(set(authority_ids)) or len(nonces) != len(set(nonces)):
        raise ValueError("provider data authority proofs cannot be reused")
    if len(grant_ids) != len(set(grant_ids)) or len(grant_nonces) != len(
        set(grant_nonces)
    ):
        raise ValueError("provider data grants cannot be reused")
    for authority in data_authorities:
        _verify_provider_data_authority(
            authority,
            trusted_snapshots[authority.payload.provider_namespace],
            manifest_created_at=manifest.created_at,
            evaluated_at=evaluated_at,
        )
    recomputed_pins = _captured_trust_pins_sha256(expected_pins)
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
        if authority.trust_snapshot_sha256 != expected_pins[provider]:
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
        event = manifest_events[
            (member.provider_namespace, member.subject_token, member.collection_token)
        ]
        origin = None
        if manifest.time_axis.kind == TimeAxisKind.SUBJECT_RELATIVE:
            origin = manifest_origins[(member.provider_namespace, member.subject_token)]
        elif manifest.time_axis.kind == TimeAxisKind.STUDY_RELATIVE:
            origin = manifest_origins[(member.provider_namespace, None)]
        if (
            member.collection_event_sha256 != collection_event_reference_sha256(event)
            or member.biological_timepoint_id != biological_timepoint_id(event)
            or member.time_coordinate
            != collection_time_coordinate(event, manifest.time_axis, origin)
        ):
            raise ValueError(
                "member timepoint is not derived from collection authority"
            )

    final_snapshot = _PINNED_ACTIVE_SNAPSHOT(store)
    final_evaluated_at = _PINNED_AUTHORITY_TIME_IN_FENCE(store)
    if final_snapshot != snapshot:
        raise ProviderLinkageStoreUnsafe(
            "live linkage authority changed during cohort validation"
        )
    for authority in data_authorities:
        _verify_provider_data_authority(
            authority,
            trusted_snapshots[authority.payload.provider_namespace],
            manifest_created_at=manifest.created_at,
            evaluated_at=final_evaluated_at,
        )


def validate_manifest_against_linkage_store(
    manifest: CohortManifest,
    store: ProviderLinkageStore,
    *,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> None:
    if type(store) is not ProviderLinkageStore:
        raise TypeError("cohort validation requires the exact live linkage store type")
    for name, pinned in _PINNED_STORE_CALLABLES.items():
        if name in vars(store) or getattr(ProviderLinkageStore, name) is not pinned:
            raise TypeError("live linkage store authority callable was shadowed")
    _PINNED_PROCESS_INTEGRITY_CHECK()
    if not _PINNED_TIME_SOURCE_IDENTITY_CHECK(store):
        raise ProviderLinkageStoreUnsafe(
            "live linkage store authority time source is not pinned"
        )
    with _PINNED_AUTHORITY_READ_FENCE(store):
        _validate_manifest_against_linkage_store_in_fence(
            manifest,
            store,
            expected_trust_snapshot_sha256_by_provider=(
                expected_trust_snapshot_sha256_by_provider
            ),
        )


__all__ = [
    "MAX_TIME_COORDINATE",
    "MIN_TIME_COORDINATE",
    "BiologicalTimepointId",
    "CohortManifest",
    "CohortMember",
    "CollectionEventReference",
    "MeasurementAnchor",
    "MemberLineageRole",
    "PolicyDigests",
    "ProviderAuthorityReference",
    "ProviderDataAuthorityPayload",
    "ProviderDataAuthorityPurpose",
    "ProviderDataGrantPayload",
    "ReanalysisRule",
    "TechnicalReplicateRule",
    "TimeAxis",
    "TimeAxisKind",
    "TimeOriginReference",
    "SignedProviderDataAuthority",
    "SignedProviderDataGrant",
    "biological_timepoint_id",
    "build_cohort_manifest",
    "build_cohort_member",
    "capture_expected_trust_pins",
    "cohort_manifest_bytes",
    "cohort_manifest_from_bytes",
    "cohort_manifest_sha256",
    "collection_event_reference_sha256",
    "collection_time_coordinate",
    "provider_data_authority_payload_bytes",
    "provider_data_grant_payload_bytes",
    "time_origin_authority_sha256",
    "time_origin_statement_sha256",
    "trust_pins_sha256",
    "validate_manifest_against_linkage_store",
    "validate_manifest_history",
]
