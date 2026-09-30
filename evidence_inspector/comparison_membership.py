"""Append-only analysis lineage and immutable comparison membership contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.compatibility import ResultId
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalMemberDecision,
    LongitudinalOutcome,
    LongitudinalPolicyId,
    LongitudinalRecord,
    longitudinal_record_sha256,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    Sha256,
    Version,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    AnalysisRecordId,
    AuthorizedLinkageRevision,
    CollectionToken,
    LinkageId,
    ProviderApprovalConsumptionLedger,
    ProviderNamespace,
    SubjectToken,
    UnitOfAnalysis,
    linkage_revision_sha256,
    project_active_linkages,
)

MAX_COMPARISON_MEMBERS = 1_000
MAX_SUPERSESSION_EVENTS = 10_000


def _token(prefix: str):
    return Annotated[
        str,
        StringConstraints(
            min_length=len(prefix) + 2,
            max_length=96,
            pattern=rf"^{prefix}[a-z0-9]+(?:_[a-z0-9]+)*$",
        ),
    ]


ComparisonSetId = _token("comparison_")
MemberId = Annotated[
    str, StringConstraints(pattern=r"^member_[0-9a-f]{32}$")
]
PolicyId = _token("policy_")


class AnalysisSupersessionEvent(RegistryContract):
    """One derived, immutable edge from an old analysis to its reanalysis."""

    schema_version: Literal["traceback.analysis-supersession.v1"] = (
        "traceback.analysis-supersession.v1"
    )
    provider_namespace: ProviderNamespace
    subject_token: SubjectToken
    collection_token: CollectionToken
    predecessor_analysis_id: AnalysisRecordId
    successor_analysis_id: AnalysisRecordId
    successor_linkage_id: LinkageId
    successor_linkage_revision_sha256: Sha256


class AnalysisSupersessionLedger(RegistryContract):
    schema_version: Literal["traceback.analysis-supersession-ledger.v1"] = (
        "traceback.analysis-supersession-ledger.v1"
    )
    events: tuple[AnalysisSupersessionEvent, ...] = Field(
        max_length=MAX_SUPERSESSION_EVENTS
    )
    current_analysis_ids: tuple[AnalysisRecordId, ...] = Field(
        max_length=MAX_SUPERSESSION_EVENTS
    )

    @model_validator(mode="after")
    def canonical_graph(self) -> AnalysisSupersessionLedger:
        event_keys = [
            (item.provider_namespace, item.successor_analysis_id)
            for item in self.events
        ]
        if event_keys != sorted(event_keys) or len(event_keys) != len(set(event_keys)):
            raise ValueError("supersession events must be uniquely sorted")
        if self.current_analysis_ids != tuple(sorted(set(self.current_analysis_ids))):
            raise ValueError("current analysis IDs must be uniquely sorted")
        return self


class MemberDisposition(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNAVAILABLE = "unavailable"


class MemberReason(StrEnum):
    ELIGIBLE = "eligible"
    COMPATIBILITY_BLOCKED = "compatibility_blocked"
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    SUPERSEDED_ANALYSIS = "superseded_analysis"
    TECHNICAL_REPLICATE_EXCLUDED = "technical_replicate_excluded"
    MISSING_MEASUREMENT = "missing_measurement"


class ReplicateRule(StrEnum):
    ONE_ANALYSIS_PER_COLLECTION = "one_analysis_per_collection"
    INCLUDE_TECHNICAL_SEPARATELY = "include_technical_separately"
    LATEST_AUTHORIZED_REANALYSIS = "latest_authorized_reanalysis"


class TimeAxis(StrEnum):
    COLLECTION_ORDINAL = "collection_ordinal"
    NONE = "none"


class ComparisonMember(RegistryContract):
    """Protected membership row with explicit biological and technical indices."""

    schema_version: Literal["traceback.comparison-member.v1"] = (
        "traceback.comparison-member.v1"
    )
    member_id: MemberId
    record: LongitudinalRecord
    decision: LongitudinalMemberDecision
    biological_timepoint_index: int | None = Field(default=None, ge=0, le=1_000_000)
    technical_replicate_index: int = Field(ge=0, le=1_000_000)
    disposition: MemberDisposition
    reason: MemberReason

    @model_validator(mode="after")
    def exact_identity_and_disposition(self) -> ComparisonMember:
        if self.member_id != comparison_member_id(self.record):
            raise ValueError("member ID does not match exact record and linkage")
        if self.decision.member_result_id != self.record.measurement.result_id:
            raise ValueError("member decision does not bind exact result")
        if (
            self.decision.member_result_sha256
            != self.record.measurement.result_sha256
            or self.decision.member_bundle_sha256
            != self.record.measurement.bundle_sha256
            or self.decision.member_record_sha256
            != longitudinal_record_sha256(self.record)
            or self.decision.member_linkage_revision_sha256
            != linkage_revision_sha256(self.record.linkage_revision)
        ):
            raise ValueError("member decision does not bind exact record provenance")
        if self.disposition == MemberDisposition.INCLUDED:
            if self.reason != MemberReason.ELIGIBLE:
                raise ValueError("included member must use eligible reason")
            if not (
                self.decision.delta_allowed
                and self.decision.connecting_trend_allowed
                and self.decision.outcome
                in {
                    LongitudinalOutcome.EQUIVALENT,
                    LongitudinalOutcome.QUALIFIED_COMPATIBLE,
                }
            ):
                raise ValueError("included member requires eligible compatibility")
            if not (
                self.record.authorized_linkage is not None
                and self.record.authorized_linkage.authorization.linkage_authorized
                and self.record.authorized_linkage.authorization.comparison_linkage_eligible
            ):
                raise ValueError("included member requires current linkage authority")
        elif self.reason == MemberReason.ELIGIBLE:
            raise ValueError("eligible reason is reserved for included members")
        if self.disposition == MemberDisposition.UNAVAILABLE:
            if self.reason != MemberReason.MISSING_MEASUREMENT:
                raise ValueError("unavailable member requires missing measurement reason")
        elif self.reason == MemberReason.MISSING_MEASUREMENT:
            raise ValueError("missing measurement reason requires unavailable disposition")
        return self


class PolicyReference(RegistryContract):
    policy_id: PolicyId
    version: Version
    policy_sha256: Sha256


class ComparisonSetDefinition(RegistryContract):
    """Immutable membership and all policies that affect comparison meaning."""

    schema_version: Literal["traceback.comparison-set-definition.v1"] = (
        "traceback.comparison-set-definition.v1"
    )
    provider_namespace: ProviderNamespace
    subject_token: SubjectToken | None
    unit_of_analysis: UnitOfAnalysis
    replicate_rule: ReplicateRule
    time_axis: TimeAxis
    measurement_definition_sha256: Sha256
    anchor_result_id: ResultId
    anchor_policy_id: LongitudinalPolicyId
    anchor_policy_version: Version
    anchor_policy_sha256: Sha256
    missingness_policy: PolicyReference
    denominator_policy: PolicyReference
    authority_snapshot_sha256: Sha256
    supersession_ledger_sha256: Sha256
    supersession_ledger: AnalysisSupersessionLedger
    members: tuple[ComparisonMember, ...] = Field(
        min_length=1, max_length=MAX_COMPARISON_MEMBERS
    )

    @model_validator(mode="after")
    def coherent_membership(self) -> ComparisonSetDefinition:
        member_ids = [item.member_id for item in self.members]
        if member_ids != sorted(member_ids) or len(member_ids) != len(set(member_ids)):
            raise ValueError("comparison members must be uniquely sorted")
        result_ids = [item.record.measurement.result_id for item in self.members]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("comparison results must be unique")
        linkage_revisions = [
            (
                item.record.linkage_revision.linkage_id,
                item.record.linkage_revision.revision,
            )
            for item in self.members
        ]
        if len(linkage_revisions) != len(set(linkage_revisions)):
            raise ValueError("comparison linkage revisions must be unique")
        if self.unit_of_analysis == UnitOfAnalysis.SUBJECT:
            if self.subject_token is None:
                raise ValueError("subject unit requires one protected subject token")
        elif self.subject_token is not None:
            subjects = {
                item.record.linkage_revision.biological.subject_token
                for item in self.members
            }
            if subjects != {self.subject_token}:
                raise ValueError("declared subject does not match every member")
        if any(
            item.record.linkage_revision.provider_namespace
            != self.provider_namespace
            for item in self.members
        ):
            raise ValueError("all members must use the declared provider namespace")
        if any(
            item.record.comparison_key.method_definition_sha256
            != self.measurement_definition_sha256
            for item in self.members
        ):
            raise ValueError("all members must share the declared measurement definition")
        if any(
            item.decision.anchor_result_id != self.anchor_result_id
            or item.decision.policy_id != self.anchor_policy_id
            or item.decision.policy_version != self.anchor_policy_version
            or item.decision.policy_sha256 != self.anchor_policy_sha256
            for item in self.members
        ):
            raise ValueError("all members must share the declared anchor policy")
        included = [
            item for item in self.members if item.disposition == MemberDisposition.INCLUDED
        ]
        if not included:
            raise ValueError("comparison set requires at least one included member")
        if not any(item.record.measurement.result_id == self.anchor_result_id for item in included):
            raise ValueError("anchor result must be an included member")
        trust_digests = {
            item.record.authorized_linkage.authorization.trust_snapshot_sha256
            for item in included
            if item.record.authorized_linkage is not None
        }
        if trust_digests != {self.authority_snapshot_sha256}:
            raise ValueError("included members must share the declared authority snapshot")
        if self.supersession_ledger_sha256 != analysis_supersession_ledger_sha256(
            self.supersession_ledger
        ):
            raise ValueError("analysis supersession ledger digest mismatch")
        if any(
            item.provider_namespace != self.provider_namespace
            for item in self.supersession_ledger.events
        ):
            raise ValueError("supersession ledger provider does not match comparison")
        self._validate_time_axis()
        self._validate_replicates(included)
        return self

    def _validate_time_axis(self) -> None:
        if self.time_axis == TimeAxis.NONE:
            if any(item.biological_timepoint_index is not None for item in self.members):
                raise ValueError("time-axis none forbids biological timepoint indices")
            return
        if any(item.biological_timepoint_index is None for item in self.members):
            raise ValueError("collection ordinal requires every member timepoint index")
        collection_to_index: dict[str, int] = {}
        index_to_collection: dict[int, str] = {}
        for item in self.members:
            collection = item.record.linkage_revision.biological.collection_token
            assert item.biological_timepoint_index is not None
            index = item.biological_timepoint_index
            if collection_to_index.setdefault(collection, index) != index:
                raise ValueError("one collection cannot have multiple timepoint indices")
            if index_to_collection.setdefault(index, collection) != collection:
                raise ValueError("one timepoint index cannot identify multiple collections")

    def _validate_replicates(self, included: list[ComparisonMember]) -> None:
        by_collection: dict[str, list[ComparisonMember]] = {}
        for item in included:
            collection = item.record.linkage_revision.biological.collection_token
            by_collection.setdefault(collection, []).append(item)
        for items in by_collection.values():
            replicate_indices = [item.technical_replicate_index for item in items]
            if len(replicate_indices) != len(set(replicate_indices)):
                raise ValueError("technical replicate indices must be unique per collection")
            if (
                self.replicate_rule == ReplicateRule.ONE_ANALYSIS_PER_COLLECTION
                and len(items) != 1
            ):
                raise ValueError("replicate rule allows one included analysis per collection")
        if self.replicate_rule == ReplicateRule.LATEST_AUTHORIZED_REANALYSIS:
            current = set(self.supersession_ledger.current_analysis_ids)
            if any(
                item.record.linkage_revision.technical.analysis_record_id not in current
                for item in included
            ):
                raise ValueError("latest-reanalysis rule included a superseded analysis")
            for item in self.members:
                analysis_id = item.record.linkage_revision.technical.analysis_record_id
                if item.reason == MemberReason.SUPERSEDED_ANALYSIS:
                    if item.disposition != MemberDisposition.EXCLUDED:
                        raise ValueError("superseded analysis must be excluded")
                    if analysis_id in current:
                        raise ValueError("current analysis cannot be marked superseded")


class ComparisonSetVersion(RegistryContract):
    schema_version: Literal["traceback.comparison-set-version.v1"] = (
        "traceback.comparison-set-version.v1"
    )
    comparison_set_id: ComparisonSetId
    version: int = Field(ge=1, le=1_000_000)
    previous_version_sha256: Sha256 | None
    definition_sha256: Sha256
    definition: ComparisonSetDefinition

    @model_validator(mode="after")
    def exact_definition(self) -> ComparisonSetVersion:
        if self.definition_sha256 != comparison_set_definition_sha256(self.definition):
            raise ValueError("comparison definition digest mismatch")
        if (self.version == 1) != (self.previous_version_sha256 is None):
            raise ValueError("only comparison version one may omit predecessor")
        return self


class ComparisonSetLedger(RegistryContract):
    schema_version: Literal["traceback.comparison-set-ledger.v1"] = (
        "traceback.comparison-set-ledger.v1"
    )
    versions: tuple[ComparisonSetVersion, ...] = Field(
        min_length=1, max_length=1_000
    )

    @model_validator(mode="after")
    def append_only_chain(self) -> ComparisonSetLedger:
        first = self.versions[0]
        for previous, current in pairwise(self.versions):
            if (
                current.comparison_set_id != first.comparison_set_id
                or current.version != previous.version + 1
                or current.previous_version_sha256
                != comparison_set_version_sha256(previous)
            ):
                raise ValueError("comparison version chain is invalid")
            if current.definition_sha256 == previous.definition_sha256:
                raise ValueError("unchanged comparison definition must be idempotent")
        return self


def comparison_member_id(record: LongitudinalRecord) -> str:
    payload = b"\0".join(
        (
            record.measurement.result_id.encode("ascii"),
            record.measurement.result_sha256.encode("ascii"),
            record.linkage_revision.linkage_id.encode("ascii"),
            linkage_revision_sha256(record.linkage_revision).encode("ascii"),
        )
    )
    return "member_" + hashlib.sha256(payload).hexdigest()[:32]


def comparison_set_definition_sha256(definition: ComparisonSetDefinition) -> str:
    return hashlib.sha256(canonical_contract_bytes(definition)).hexdigest()


def analysis_supersession_ledger_sha256(
    ledger: AnalysisSupersessionLedger,
) -> str:
    return hashlib.sha256(canonical_contract_bytes(ledger)).hexdigest()


def comparison_set_version_sha256(version: ComparisonSetVersion) -> str:
    return hashlib.sha256(canonical_contract_bytes(version)).hexdigest()


def build_analysis_supersession_ledger(
    records: Sequence[AuthorizedLinkageRevision],
    *,
    consumption_ledger: ProviderApprovalConsumptionLedger,
    expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
) -> AnalysisSupersessionLedger:
    """Derive a non-branching, acyclic reanalysis graph from authorized linkage."""

    active_revisions = project_active_linkages(
        records,
        consumption_ledger=consumption_ledger,
        expected_trust_snapshot_sha256_by_provider=(
            expected_trust_snapshot_sha256_by_provider
        ),
    )
    active_by_key = {
        (item.provider_namespace, item.linkage_id): item for item in active_revisions
    }
    active_records = [
        record
        for record in records
        if (
            record.revision.provider_namespace,
            record.revision.linkage_id,
        )
        in active_by_key
        and active_by_key[
            (record.revision.provider_namespace, record.revision.linkage_id)
        ].revision
        == record.revision.revision
    ]
    analysis = {
        (record.revision.provider_namespace, record.revision.technical.analysis_record_id): record
        for record in active_records
    }
    events: list[AnalysisSupersessionEvent] = []
    successor_by_predecessor: dict[tuple[str, str], str] = {}
    for record in active_records:
        revision = record.revision
        predecessor = revision.technical.reanalysis_of.token
        if predecessor is None:
            continue
        predecessor_record = analysis.get((revision.provider_namespace, predecessor))
        if predecessor_record is None:
            raise ValueError("reanalysis predecessor is absent from active linkage")
        old = predecessor_record.revision
        if (
            old.biological.subject_token != revision.biological.subject_token
            or old.biological.collection_token != revision.biological.collection_token
        ):
            raise ValueError("reanalysis cannot cross subject or collection identity")
        predecessor_key = (revision.provider_namespace, predecessor)
        known_successor = successor_by_predecessor.setdefault(
            predecessor_key, revision.technical.analysis_record_id
        )
        if known_successor != revision.technical.analysis_record_id:
            raise ValueError("analysis supersession cannot branch")
        events.append(
            AnalysisSupersessionEvent(
                provider_namespace=revision.provider_namespace,
                subject_token=revision.biological.subject_token,
                collection_token=revision.biological.collection_token,
                predecessor_analysis_id=predecessor,
                successor_analysis_id=revision.technical.analysis_record_id,
                successor_linkage_id=revision.linkage_id,
                successor_linkage_revision_sha256=linkage_revision_sha256(revision),
            )
        )

    next_by_analysis = {
        item.predecessor_analysis_id: item.successor_analysis_id for item in events
    }
    for start in next_by_analysis:
        seen: set[str] = set()
        current = start
        while current in next_by_analysis:
            if current in seen:
                raise ValueError("analysis supersession cycle is invalid")
            seen.add(current)
            current = next_by_analysis[current]
    current_ids = tuple(
        sorted(
            analysis_id
            for _, analysis_id in analysis
            if analysis_id not in next_by_analysis
        )
    )
    return AnalysisSupersessionLedger(
        events=tuple(
            sorted(
                events,
                key=lambda item: (
                    item.provider_namespace,
                    item.successor_analysis_id,
                ),
            )
        ),
        current_analysis_ids=current_ids,
    )


__all__ = [
    "AnalysisSupersessionEvent",
    "AnalysisSupersessionLedger",
    "ComparisonMember",
    "ComparisonSetDefinition",
    "ComparisonSetLedger",
    "ComparisonSetVersion",
    "MemberDisposition",
    "MemberReason",
    "PolicyReference",
    "ReplicateRule",
    "TimeAxis",
    "analysis_supersession_ledger_sha256",
    "build_analysis_supersession_ledger",
    "comparison_member_id",
    "comparison_set_definition_sha256",
    "comparison_set_version_sha256",
]
