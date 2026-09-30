"""Deterministic D09 cohort denominators and missingness summaries.

This is a protected, synthetic/local comparison-population contract.  It binds
an immutable D05 manifest to independently indexed E04/D06 result references
and exact E06 denominator ledgers.  It does not expose provider-local linkage
tokens, infer missing linkage, calculate a scientific delta, or claim that an
Epic D qualification gate passed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MemberLineageRole,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import (
    CohortRegistry,
    RegisteredCohortHistory,
)
from evidence_inspector.compatibility import (
    CompatibilityOutcome,
    ExecutionState,
    InformationState,
    ResultId,
    TrustState,
)
from evidence_inspector.method_registry import (
    QualificationState,
    RegistryContract,
    Sha256,
)
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalAnchorPolicy,
    LongitudinalRecord,
    LongitudinalSeriesDecision,
    longitudinal_anchor_policy_sha256,
    replay_longitudinal_series_decision,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    ComparisonObservation,
    RepeatabilityClassification,
    RepeatabilityComparison,
    RepeatabilityEnvelope,
    compare_repeatability,
    repeatability_comparison_sha256,
)
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.cohort_import import (
    COHORT_RECORD_STATUS_POLICY_SHA256,
    CohortManifestRecordStatus,
    CohortMemberRecordStatus,
    CohortRecordAvailability,
    CohortRecordCatalog,
    CohortRecordWithheldReason,
)
from evidence_inspector.result_view import (
    CountState,
    DenominatorLedger,
    ResultViewSource,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)
from traceback_runner.signing import DevelopmentTrustDocument

MAX_COHORT_SUMMARY_MEMBERS = 100_000
MAX_REGISTERED_SUMMARY_BYTES = 64 * 1024
MAX_REGISTERED_SUMMARY_DEPTH = 32
MAX_REGISTERED_SUMMARY_NODES = 10_000
MAX_COHORT_INPUT_BYTES = 32 * 1024 * 1024
MAX_COHORT_INPUT_DEPTH = 64
MAX_COHORT_INPUT_NODES = 2_000_000
_PINNED_RESOLVE_HISTORY = CohortRegistry.resolve_history
_PINNED_RECORD_STATUS = CohortRecordCatalog.record_status_for_manifest
_PINNED_RECORD_STATUS_FENCE = CohortRecordCatalog.record_status_authority_fence
DenominatorPolicyId = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=96,
        pattern=r"^denominator_[a-z0-9]+(?:_[a-z0-9]+)*$",
    ),
]


def _exact_bytes(
    value: object,
    model: type[BaseModel],
    *,
    max_bytes: int = MAX_COHORT_INPUT_BYTES,
) -> bytes:
    model_types, enum_types = contract_type_graph(model)
    return exact_model_bytes(
        value,
        model,
        model_types=model_types,
        enum_types=enum_types,
        max_bytes=max_bytes,
        max_nodes=MAX_COHORT_INPUT_NODES,
        max_depth=MAX_COHORT_INPUT_DEPTH,
        max_collection_items=MAX_COHORT_SUMMARY_MEMBERS,
        max_string_bytes=4_096,
    )


def _replay_exact(
    value: object, model: type[BaseModel], *, max_bytes: int = MAX_COHORT_INPUT_BYTES
) -> BaseModel:
    return model.model_validate_json(_exact_bytes(value, model, max_bytes=max_bytes))


def _sha256_exact(value: object, model: type[BaseModel]) -> str:
    return hashlib.sha256(_exact_bytes(value, model)).hexdigest()


class DenominatorBasis(StrEnum):
    MANIFEST_CONTRIBUTORS = "manifest_contributors"


class UnavailableUnitRule(StrEnum):
    RETAIN_IN_DECLARED_DENOMINATOR = "retain_in_declared_denominator"


class MissingValueRule(StrEnum):
    EXPLICIT_UNAVAILABLE = "explicit_unavailable"


class InclusionRule(StrEnum):
    ALL_ELIGIBLE_BIOLOGICAL_MEMBERS = "all_eligible_biological_members"


class CohortDenominatorPolicy(RegistryContract):
    """Exact D09 policy identity; policy content is supplied, never inferred."""

    schema_version: Literal["traceback.cohort-denominator-policy.v1"] = (
        "traceback.cohort-denominator-policy.v1"
    )
    policy_id: DenominatorPolicyId
    version: int = Field(ge=1, le=100_000, strict=True)
    definition_sha256: Sha256
    inclusion_sha256: Sha256
    exclusion_sha256: Sha256
    missingness_sha256: Sha256
    basis: Literal[DenominatorBasis.MANIFEST_CONTRIBUTORS] = (
        DenominatorBasis.MANIFEST_CONTRIBUTORS
    )
    unavailable_unit_rule: Literal[
        UnavailableUnitRule.RETAIN_IN_DECLARED_DENOMINATOR
    ] = UnavailableUnitRule.RETAIN_IN_DECLARED_DENOMINATOR
    missing_value_rule: Literal[MissingValueRule.EXPLICIT_UNAVAILABLE] = (
        MissingValueRule.EXPLICIT_UNAVAILABLE
    )
    inclusion_rule: Literal[InclusionRule.ALL_ELIGIBLE_BIOLOGICAL_MEMBERS] = (
        InclusionRule.ALL_ELIGIBLE_BIOLOGICAL_MEMBERS
    )
    require_complete: Literal[True] = True
    require_sufficient: Literal[True] = True
    require_verified: Literal[True] = True
    require_qualified: Literal[True] = True
    require_provider_eligible: Literal[True] = True
    require_comparable: Literal[True] = True


class CohortMemberExclusionSet(RegistryContract):
    """Closed member commitments selected by one immutable manifest policy."""

    schema_version: Literal["traceback.cohort-member-exclusion-set.v1"] = (
        "traceback.cohort-member-exclusion-set.v1"
    )
    member_sha256s: tuple[Sha256, ...] = Field(
        default=(), max_length=MAX_COHORT_SUMMARY_MEMBERS
    )

    @model_validator(mode="after")
    def canonical_membership(self) -> CohortMemberExclusionSet:
        if self.member_sha256s != tuple(sorted(set(self.member_sha256s))):
            raise ValueError("policy member commitments must be uniquely sorted")
        return self


class CohortDispositionPolicy(RegistryContract):
    """Exact inclusion/exclusion policy evidence bound by the D05 manifest."""

    schema_version: Literal["traceback.cohort-disposition-policy.v1"] = (
        "traceback.cohort-disposition-policy.v1"
    )
    inclusion: CohortMemberExclusionSet
    exclusion: CohortMemberExclusionSet
    missingness_sha256: Sha256

    @model_validator(mode="after")
    def disjoint_rule_sets(self) -> CohortDispositionPolicy:
        if set(self.inclusion.member_sha256s) & set(self.exclusion.member_sha256s):
            raise ValueError("inclusion and exclusion policies must be disjoint")
        return self


def cohort_member_exclusion_set_sha256(policy: CohortMemberExclusionSet) -> str:
    return _sha256_exact(policy, CohortMemberExclusionSet)


def _replay_disposition_policy(value: object) -> CohortDispositionPolicy:
    replayed = _replay_exact(value, CohortDispositionPolicy)
    if type(replayed) is not CohortDispositionPolicy:
        raise ValueError("cohort disposition policy is invalid")
    return replayed


def _validate_disposition_policy(
    *,
    policy: CohortDenominatorPolicy,
    manifest: CohortManifest,
    disposition_policy: CohortDispositionPolicy,
) -> None:
    expected = (
        cohort_member_exclusion_set_sha256(disposition_policy.inclusion),
        cohort_member_exclusion_set_sha256(disposition_policy.exclusion),
        disposition_policy.missingness_sha256,
    )
    bound = (
        policy.inclusion_sha256,
        policy.exclusion_sha256,
        policy.missingness_sha256,
    )
    manifest_bound = (
        manifest.policies.inclusion_sha256,
        manifest.policies.exclusion_sha256,
        manifest.policies.missingness_sha256,
    )
    if expected != bound or expected != manifest_bound:
        raise ValueError("disposition policy does not bind manifest policies")
    members = {_sha256_exact(item, CohortMember) for item in manifest.members}
    selected = set(disposition_policy.inclusion.member_sha256s) | set(
        disposition_policy.exclusion.member_sha256s
    )
    if not selected.issubset(members):
        raise ValueError("disposition policy contains a non-member commitment")


class CohortRepeatabilityReplay(RegistryContract):
    """Protected exact D07 replay inputs for one D03 member decision."""

    schema_version: Literal["traceback.cohort-repeatability-replay.v1"] = (
        "traceback.cohort-repeatability-replay.v1"
    )
    member_result_id: ResultId
    expected: RepeatabilityComparison
    anchor_observation: ComparisonObservation
    member_observation: ComparisonObservation
    envelope: RepeatabilityEnvelope | None
    evaluated_at: datetime
    result_trust_document: DevelopmentTrustDocument
    expected_result_trust_sha256: Sha256
    expected_envelope_sha256: Sha256
    expected_evidence_sha256: Sha256
    expected_protocol_sha256: Sha256
    expected_repeatability_authority_sha256: Sha256


class CohortComparisonReplay(RegistryContract):
    """Protected D02/D03/D07 replay request; never crosses the public boundary."""

    schema_version: Literal["traceback.cohort-comparison-replay.v1"] = (
        "traceback.cohort-comparison-replay.v1"
    )
    anchor: LongitudinalRecord
    members: tuple[LongitudinalRecord, ...] = Field(
        min_length=1, max_length=MAX_COHORT_SUMMARY_MEMBERS
    )
    policy: LongitudinalAnchorPolicy
    expected_series: LongitudinalSeriesDecision
    expected_policy_sha256: Sha256
    expected_authority_head_sha256: Sha256
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, Sha256] = Field(
        min_length=1, max_length=64
    )
    repeatability: tuple[CohortRepeatabilityReplay, ...] = Field(
        default=(), max_length=MAX_COHORT_SUMMARY_MEMBERS
    )

    @model_validator(mode="after")
    def exact_replay_membership(self) -> CohortComparisonReplay:
        member_ids = tuple(item.member_result_id for item in self.repeatability)
        if member_ids != self.expected_series.member_result_ids:
            raise ValueError("D07 replay membership must match D03 series order")
        return self


class ComparisonEligibility(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    AVAILABLE = "available"
    MISSING_DRAW = "missing_draw"
    FAILED_MEASUREMENT = "failed_measurement"
    INSUFFICIENT_MEASUREMENT = "insufficient_measurement"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"
    REQUIRES_REANALYSIS = "requires_reanalysis"
    REGISTERED_BRIDGE = "registered_bridge"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    OUTSIDE_ENVELOPE = "outside_envelope"


_COMPARISON_ELIGIBILITY_BY_CLASSIFICATION = {
    RepeatabilityClassification.EXACT_SAME_VALUE: ComparisonEligibility.AVAILABLE,
    RepeatabilityClassification.NOISY_WITHIN_ENVELOPE: ComparisonEligibility.AVAILABLE,
    RepeatabilityClassification.OUTSIDE_ENVELOPE: ComparisonEligibility.OUTSIDE_ENVELOPE,
    RepeatabilityClassification.MISSING_DRAW: ComparisonEligibility.MISSING_DRAW,
    RepeatabilityClassification.FAILED_MEASUREMENT: (
        ComparisonEligibility.FAILED_MEASUREMENT
    ),
    RepeatabilityClassification.INSUFFICIENT_MEASUREMENT: (
        ComparisonEligibility.INSUFFICIENT_MEASUREMENT
    ),
    RepeatabilityClassification.INCOMPATIBLE: ComparisonEligibility.INCOMPATIBLE,
    RepeatabilityClassification.UNKNOWN: ComparisonEligibility.UNKNOWN,
    RepeatabilityClassification.REQUIRES_REANALYSIS: (
        ComparisonEligibility.REQUIRES_REANALYSIS
    ),
    RepeatabilityClassification.REGISTERED_BRIDGE: (
        ComparisonEligibility.REGISTERED_BRIDGE
    ),
    RepeatabilityClassification.EVIDENCE_UNAVAILABLE: (
        ComparisonEligibility.EVIDENCE_UNAVAILABLE
    ),
}


def cohort_denominator_policy_sha256(policy: CohortDenominatorPolicy) -> str:
    return _sha256_exact(policy, CohortDenominatorPolicy)


class MemberDisposition(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNAVAILABLE = "unavailable"


class MemberDispositionReason(StrEnum):
    INCLUDED_BY_POLICY = "included_by_policy"
    EXCLUDED_BY_INCLUSION_POLICY = "excluded_by_inclusion_policy"
    EXCLUDED_BY_EXCLUSION_POLICY = "excluded_by_exclusion_policy"
    TECHNICAL_REPLICATE_COLLAPSED = "technical_replicate_collapsed"
    REANALYSIS_COLLAPSED = "reanalysis_collapsed"
    NO_VERIFIED_CATALOG_RESULT = "no_verified_catalog_result"
    NO_RESULT_VIEW_EVIDENCE = "no_result_view_evidence"
    EXECUTION_FAILED = "execution_failed"
    NOT_RUN = "not_run"
    INFORMATION_INSUFFICIENT = "information_insufficient"
    INFORMATION_UNKNOWN = "information_unknown"
    TRUST_UNVERIFIED = "trust_unverified"
    TRUST_REVOKED = "trust_revoked"
    TRUST_UNKNOWN = "trust_unknown"
    RESULT_WITHHELD_KEY_REVOKED = "result_withheld_key_revoked"
    QUALIFICATION_UNAVAILABLE = "qualification_unavailable"
    PROVIDER_ELIGIBILITY_UNAVAILABLE = "provider_eligibility_unavailable"
    COMPATIBILITY_DIFFERENT_QUANTITY = "compatibility_different_quantity"
    COMPATIBILITY_INCOMPATIBLE = "compatibility_incompatible"
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    DENOMINATOR_MISSING = "denominator_missing"
    DENOMINATOR_WITHHELD = "denominator_withheld"


_EXCLUDED_REASONS = frozenset(
    {
        MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY,
        MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
        MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
        MemberDispositionReason.REANALYSIS_COLLAPSED,
    }
)
_UNAVAILABLE_REASONS = frozenset(
    set(MemberDispositionReason)
    - _EXCLUDED_REASONS
    - {MemberDispositionReason.INCLUDED_BY_POLICY}
)


class CohortMemberEvidence(RegistryContract):
    """Protected build input for one exact manifest member.

    ``member_sha256`` is the only linkage reference.  The resulting summary
    never serializes subject, collection, specimen, provider, or run tokens.
    """

    schema_version: Literal["traceback.cohort-member-evidence.v1"] = (
        "traceback.cohort-member-evidence.v1"
    )
    member_sha256: Sha256
    disposition: MemberDisposition
    reason: MemberDispositionReason
    catalog_result: CatalogResultRef | None = None
    result_source: ResultViewSource | None = None
    record_availability: CohortRecordAvailability | None = None
    record_withheld_reason: CohortRecordWithheldReason | None = None

    @model_validator(mode="after")
    def coherent_disposition(self) -> CohortMemberEvidence:
        if self.disposition == MemberDisposition.INCLUDED:
            if self.reason != MemberDispositionReason.INCLUDED_BY_POLICY:
                raise ValueError("included evidence requires the included reason")
            if self.catalog_result is None or self.result_source is None:
                raise ValueError("included evidence requires catalog and result view")
        elif self.disposition == MemberDisposition.EXCLUDED:
            if self.reason not in _EXCLUDED_REASONS:
                raise ValueError("excluded evidence requires an exclusion reason")
        elif self.reason not in _UNAVAILABLE_REASONS:
            raise ValueError("unavailable evidence requires an unavailable reason")
        if (self.catalog_result is None) != (
            self.result_source is None
        ) and self.disposition != MemberDisposition.UNAVAILABLE:
            raise ValueError("partial evidence can only be unavailable")
        if self.reason is MemberDispositionReason.RESULT_WITHHELD_KEY_REVOKED:
            if (
                self.record_availability is not CohortRecordAvailability.WITHHELD
                or self.record_withheld_reason
                is not CohortRecordWithheldReason.RESULT_KEY_REVOKED
            ):
                raise ValueError("withheld evidence requires its exact D06 reason")
        elif (
            self.record_availability is not None
            or self.record_withheld_reason is not None
        ):
            raise ValueError("D06 withheld state is only valid for withheld evidence")
        return self


class CohortSummaryState(StrEnum):
    NO_INCLUDED_UNITS = "no_included_units"
    ONE_INCLUDED_UNIT = "one_included_unit"
    MULTIPLE_INCLUDED_UNITS = "multiple_included_units"


class CohortSummaryRow(RegistryContract):
    """Privacy-bounded row; exact linkage remains only in the protected manifest."""

    ordinal: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    member_sha256: Sha256
    lineage_role: MemberLineageRole
    denominator_contribution: bool
    disposition: MemberDisposition
    reason: MemberDispositionReason
    catalog_result_sha256: Sha256 | None
    result_id: ResultId | None
    result_source_sha256: Sha256 | None
    denominator_ledger_sha256: Sha256 | None


class CohortDenominatorSummary(RegistryContract):
    schema_version: Literal["traceback.cohort-denominator-summary.v1"] = (
        "traceback.cohort-denominator-summary.v1"
    )
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    denominator_policy_id: DenominatorPolicyId
    denominator_policy_sha256: Sha256
    inclusion_sha256: Sha256
    exclusion_sha256: Sha256
    missingness_sha256: Sha256
    population_id: str = Field(pattern=r"^population_[0-9a-f]{40}$")
    population_sha256: Sha256
    state: CohortSummaryState
    declared_members: int = Field(ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    included_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    excluded_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    unavailable_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    declared_denominator_units: int = Field(
        ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    included_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    excluded_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    unavailable_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    rows: tuple[CohortSummaryRow, ...] = Field(
        min_length=1, max_length=MAX_COHORT_SUMMARY_MEMBERS
    )
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False
    scientific_qualification_claimed: Literal[False] = False

    @model_validator(mode="after")
    def reconcile_counts_and_identity(self) -> CohortDenominatorSummary:
        if self.declared_members != len(self.rows):
            raise ValueError("declared member count does not match rows")
        observed_member_counts = (
            sum(row.disposition == MemberDisposition.INCLUDED for row in self.rows),
            sum(row.disposition == MemberDisposition.EXCLUDED for row in self.rows),
            sum(row.disposition == MemberDisposition.UNAVAILABLE for row in self.rows),
        )
        if observed_member_counts != (
            self.included_members,
            self.excluded_members,
            self.unavailable_members,
        ):
            raise ValueError("member dispositions do not reconcile")
        observed_unit_counts = tuple(
            sum(
                row.denominator_contribution and row.disposition == disposition
                for row in self.rows
            )
            for disposition in MemberDisposition
        )
        if observed_unit_counts != (
            self.included_denominator_units,
            self.excluded_denominator_units,
            self.unavailable_denominator_units,
        ):
            raise ValueError("denominator unit dispositions do not reconcile")
        if self.declared_denominator_units != sum(
            row.denominator_contribution for row in self.rows
        ):
            raise ValueError("declared denominator does not match manifest rows")
        expected_ordinals = list(range(len(self.rows)))
        if [row.ordinal for row in self.rows] != expected_ordinals:
            raise ValueError("summary rows must preserve canonical manifest order")
        member_digests = [row.member_sha256 for row in self.rows]
        if len(member_digests) != len(set(member_digests)):
            raise ValueError("summary member commitments must be unique")
        for row in self.rows:
            if row.lineage_role == MemberLineageRole.TECHNICAL_REPLICATE:
                if (
                    row.denominator_contribution
                    or row.disposition != MemberDisposition.EXCLUDED
                    or row.reason
                    != MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED
                ):
                    raise ValueError(
                        "technical replicates must remain collapsed exclusions"
                    )
            elif row.lineage_role == MemberLineageRole.REANALYSIS:
                if (
                    row.denominator_contribution
                    or row.disposition != MemberDisposition.EXCLUDED
                    or row.reason != MemberDispositionReason.REANALYSIS_COLLAPSED
                ):
                    raise ValueError("reanalyses must remain collapsed exclusions")
            elif row.reason in {
                MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
                MemberDispositionReason.REANALYSIS_COLLAPSED,
            }:
                raise ValueError("collapsed reason does not match member lineage")
        result_ids = [row.result_id for row in self.rows if row.result_id is not None]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("one catalog result cannot represent multiple members")
        expected_state = (
            CohortSummaryState.NO_INCLUDED_UNITS
            if self.included_denominator_units == 0
            else CohortSummaryState.ONE_INCLUDED_UNIT
            if self.included_denominator_units == 1
            else CohortSummaryState.MULTIPLE_INCLUDED_UNITS
        )
        if self.state != expected_state:
            raise ValueError("summary state does not match included denominator")
        payload = self.model_copy(
            update={
                "population_id": "population_" + "0" * 40,
                "population_sha256": "0" * 64,
            }
        )
        expected_sha256 = _sha256_exact(payload, CohortDenominatorSummary)
        if self.population_sha256 != expected_sha256:
            raise ValueError("population digest does not match summary content")
        if self.population_id != f"population_{expected_sha256[:40]}":
            raise ValueError("population ID does not match summary content")
        return self


class CohortPopulationProjection(RegistryContract):
    """Aggregate-only projection; protected member rows never cross this boundary."""

    schema_version: Literal["traceback.cohort-population-projection.v1"] = (
        "traceback.cohort-population-projection.v1"
    )
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    denominator_policy_id: DenominatorPolicyId
    denominator_policy_sha256: Sha256
    inclusion_sha256: Sha256
    exclusion_sha256: Sha256
    missingness_sha256: Sha256
    population_id: str = Field(pattern=r"^population_[0-9a-f]{40}$")
    population_sha256: Sha256
    state: CohortSummaryState
    declared_members: int = Field(ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    included_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    excluded_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    unavailable_members: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    declared_denominator_units: int = Field(
        ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    included_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    excluded_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    unavailable_denominator_units: int = Field(
        ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )

    @model_validator(mode="after")
    def reconcile_aggregate_counts(self) -> CohortPopulationProjection:
        if self.declared_members != (
            self.included_members
            + self.excluded_members
            + self.unavailable_members
        ):
            raise ValueError("projected member counts do not reconcile")
        if self.declared_denominator_units != (
            self.included_denominator_units
            + self.excluded_denominator_units
            + self.unavailable_denominator_units
        ):
            raise ValueError("projected denominator counts do not reconcile")
        expected_state = (
            CohortSummaryState.NO_INCLUDED_UNITS
            if self.included_denominator_units == 0
            else CohortSummaryState.ONE_INCLUDED_UNIT
            if self.included_denominator_units == 1
            else CohortSummaryState.MULTIPLE_INCLUDED_UNITS
        )
        if self.state != expected_state:
            raise ValueError("projected state does not match included denominator")
        return self


class ComparisonEligibilityCount(RegistryContract):
    state: ComparisonEligibility
    count: int = Field(ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)


class RegisteredCohortDenominatorSummary(RegistryContract):
    """D09 population bound to live D05 registry and D06 catalog state."""

    schema_version: Literal["traceback.registered-cohort-denominator-summary.v2"] = (
        "traceback.registered-cohort-denominator-summary.v2"
    )
    registry_id: str = Field(pattern=r"^cohort_registry_[0-9a-f]{32}$")
    registry_epoch_sha256: Sha256
    registry_state_version: int = Field(
        ge=1, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True
    )
    registry_state_head_sha256: Sha256
    selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    linkage_snapshot_sha256: Sha256
    catalog_authority_sha256: Sha256
    record_status_sha256: Sha256
    record_status_policy_sha256: Sha256
    population: CohortPopulationProjection
    comparison_replay_sha256: Sha256 | None = None
    comparison_eligibility: tuple[ComparisonEligibilityCount, ...] = Field(
        default=(), max_length=len(ComparisonEligibility)
    )
    summary_sha256: Sha256
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False
    scientific_qualification_claimed: Literal[False] = False

    @model_validator(mode="after")
    def exact_cross_system_identity(self) -> RegisteredCohortDenominatorSummary:
        if (
            self.population.cohort_version != self.cohort_version
            or self.population.cohort_manifest_sha256
            != self.cohort_manifest_sha256
        ):
            raise ValueError("registered summary population identity is inconsistent")
        states = tuple(item.state for item in self.comparison_eligibility)
        if states != tuple(sorted(set(states), key=lambda item: item.value)):
            raise ValueError("comparison eligibility states must be uniquely sorted")
        if (self.comparison_replay_sha256 is None) != (
            len(self.comparison_eligibility) == 0
        ):
            raise ValueError("comparison replay identity and counts must be paired")
        placeholder = self.model_copy(update={"summary_sha256": "0" * 64})
        if self.summary_sha256 != _sha256_exact(
            placeholder, RegisteredCohortDenominatorSummary
        ):
            raise ValueError("registered summary digest is invalid")
        return self


@dataclass(frozen=True, slots=True)
class RegisteredCohortDenominatorDerivation:
    """Protected live D09 derivation retained only while its authority is fenced."""

    summary: RegisteredCohortDenominatorSummary
    manifest: CohortManifest
    population: CohortDenominatorSummary
    comparison_replay: CohortComparisonReplay | None
    comparison_series: LongitudinalSeriesDecision | None
    repeatability_comparisons: tuple[RepeatabilityComparison, ...]


_REGISTERED_SUMMARY_MODEL_TYPES, _REGISTERED_SUMMARY_ENUM_TYPES = (
    contract_type_graph(RegisteredCohortDenominatorSummary)
)


def _count_states(source: ResultViewSource) -> tuple[CountState, ...]:
    ledger = source.denominator
    return (
        ledger.input_records.state,
        ledger.accepted_records.state,
        ledger.eligible_records.state,
        ledger.displayed_records.state,
        *(item.count.state for item in ledger.attrition),
    )


def _validate_catalog_binding(
    catalog: CatalogResultRef, source: ResultViewSource
) -> None:
    record = source.record
    capability = record.current_capability
    if (
        catalog.result_id,
        catalog.bundle_sha256,
        catalog.method_ref,
        catalog.method_definition_sha256,
        catalog.registry_sha256,
        catalog.registry_version,
        catalog.authority_head_sha256,
        catalog.authority_revision,
        catalog.authority_scope,
        catalog.capability_as_of,
        catalog.qualification_state.value,
        catalog.display_role,
        catalog.research_inspectable,
        catalog.current_provider_eligible,
    ) != (
        record.result_id,
        record.bundle_sha256,
        record.method.method_ref,
        record.method_definition_sha256,
        capability.registry_sha256,
        capability.registry_version,
        capability.authority_head_sha256,
        capability.authority_revision,
        capability.authority_scope,
        capability.as_of,
        (
            capability.qualification_state.value
            if capability.qualification_state is not None
            else CatalogQualificationState.UNKNOWN.value
        ),
        capability.display_role,
        capability.research_inspectable,
        capability.current_provider_eligible,
    ):
        raise ValueError("catalog result does not bind the exact result view source")


def _unavailability_reason_matches(evidence: CohortMemberEvidence) -> bool:
    catalog, source, reason = (
        evidence.catalog_result,
        evidence.result_source,
        evidence.reason,
    )
    if reason == MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT:
        return catalog is None
    if reason == MemberDispositionReason.NO_RESULT_VIEW_EVIDENCE:
        return source is None
    if reason == MemberDispositionReason.RESULT_WITHHELD_KEY_REVOKED:
        return (
            evidence.record_availability is CohortRecordAvailability.WITHHELD
            and evidence.record_withheld_reason
            is CohortRecordWithheldReason.RESULT_KEY_REVOKED
            and catalog is None
            and source is None
        )
    if source is None:
        return False
    record = source.record
    capability = record.current_capability
    states = _count_states(source)
    conditions = {
        MemberDispositionReason.EXECUTION_FAILED: (
            record.execution_state == ExecutionState.FAILED
        ),
        MemberDispositionReason.NOT_RUN: (
            record.execution_state == ExecutionState.NOT_RUN
        ),
        MemberDispositionReason.INFORMATION_INSUFFICIENT: (
            record.information_state == InformationState.INSUFFICIENT
        ),
        MemberDispositionReason.INFORMATION_UNKNOWN: (
            record.information_state == InformationState.UNKNOWN
        ),
        MemberDispositionReason.TRUST_UNVERIFIED: (
            record.trust_state == TrustState.UNVERIFIED
        ),
        MemberDispositionReason.TRUST_REVOKED: (
            record.trust_state == TrustState.REVOKED
        ),
        MemberDispositionReason.TRUST_UNKNOWN: (
            record.trust_state == TrustState.UNKNOWN
        ),
        MemberDispositionReason.QUALIFICATION_UNAVAILABLE: (
            capability.qualification_state != QualificationState.QUALIFIED
        ),
        MemberDispositionReason.PROVIDER_ELIGIBILITY_UNAVAILABLE: (
            not capability.current_provider_eligible
        ),
        MemberDispositionReason.COMPATIBILITY_DIFFERENT_QUANTITY: (
            source.compatibility_decision.outcome
            == CompatibilityOutcome.DIFFERENT_QUANTITY
        ),
        MemberDispositionReason.COMPATIBILITY_INCOMPATIBLE: (
            source.compatibility_decision.outcome == CompatibilityOutcome.INCOMPATIBLE
        ),
        MemberDispositionReason.COMPATIBILITY_UNKNOWN: (
            source.compatibility_decision.outcome == CompatibilityOutcome.UNKNOWN
        ),
        MemberDispositionReason.DENOMINATOR_MISSING: CountState.MISSING in states,
        MemberDispositionReason.DENOMINATOR_WITHHELD: CountState.WITHHELD in states,
    }
    return conditions.get(reason, False)


def _validate_included(evidence: CohortMemberEvidence) -> None:
    assert evidence.catalog_result is not None and evidence.result_source is not None
    catalog, source = evidence.catalog_result, evidence.result_source
    record = source.record
    capability = record.current_capability
    if (
        record.execution_state != ExecutionState.COMPLETE
        or record.information_state != InformationState.SUFFICIENT
        or record.trust_state != TrustState.VERIFIED
        or capability.qualification_state != QualificationState.QUALIFIED
        or not capability.current_provider_eligible
        or source.compatibility_decision.outcome != CompatibilityOutcome.COMPARABLE
        or any(state != CountState.OBSERVED for state in _count_states(source))
        or catalog.qualification_state != CatalogQualificationState.QUALIFIED
        or not catalog.current_provider_eligible
    ):
        raise ValueError("included evidence does not satisfy every eligibility gate")


def _replay_source(source: object) -> ResultViewSource:
    try:
        replayed = _replay_exact(source, ResultViewSource)
        assert type(replayed) is ResultViewSource
        return replayed
    except (AssertionError, TypeError, ValueError):
        raise ValueError("cohort result source is invalid") from None


def _derived_disposition(
    *,
    member: CohortMember,
    status: CohortMemberRecordStatus,
    source: ResultViewSource | None,
    disposition_policy: CohortDispositionPolicy,
) -> CohortMemberEvidence:
    member_sha256 = status.member_sha256
    if member.lineage_role is MemberLineageRole.TECHNICAL_REPLICATE:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.EXCLUDED,
            reason=MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
        )
    if member.lineage_role is MemberLineageRole.REANALYSIS:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.EXCLUDED,
            reason=MemberDispositionReason.REANALYSIS_COLLAPSED,
        )
    if member_sha256 in disposition_policy.inclusion.member_sha256s:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.EXCLUDED,
            reason=MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY,
        )
    if member_sha256 in disposition_policy.exclusion.member_sha256s:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.EXCLUDED,
            reason=MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
        )
    if status.availability is CohortRecordAvailability.MISSING:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT,
        )
    if status.availability is CohortRecordAvailability.WITHHELD:
        if status.withheld_reason is not CohortRecordWithheldReason.RESULT_KEY_REVOKED:
            raise ValueError("cohort withheld reason is unsupported")
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.RESULT_WITHHELD_KEY_REVOKED,
            record_availability=status.availability,
            record_withheld_reason=status.withheld_reason,
        )
    binding = status.binding
    if binding is None:
        raise ValueError("available cohort status has no exact binding")
    if source is None:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.NO_RESULT_VIEW_EVIDENCE,
            catalog_result=binding.result,
        )
    _validate_catalog_binding(binding.result, source)
    record = source.record
    capability = record.current_capability
    states = _count_states(source)
    reason: MemberDispositionReason | None = None
    if record.execution_state is ExecutionState.FAILED:
        reason = MemberDispositionReason.EXECUTION_FAILED
    elif record.execution_state is ExecutionState.NOT_RUN:
        reason = MemberDispositionReason.NOT_RUN
    elif record.information_state is InformationState.INSUFFICIENT:
        reason = MemberDispositionReason.INFORMATION_INSUFFICIENT
    elif record.information_state is InformationState.UNKNOWN:
        reason = MemberDispositionReason.INFORMATION_UNKNOWN
    elif record.trust_state is TrustState.UNVERIFIED:
        reason = MemberDispositionReason.TRUST_UNVERIFIED
    elif record.trust_state is TrustState.REVOKED:
        reason = MemberDispositionReason.TRUST_REVOKED
    elif record.trust_state is TrustState.UNKNOWN:
        reason = MemberDispositionReason.TRUST_UNKNOWN
    elif capability.qualification_state is not QualificationState.QUALIFIED:
        reason = MemberDispositionReason.QUALIFICATION_UNAVAILABLE
    elif not capability.current_provider_eligible:
        reason = MemberDispositionReason.PROVIDER_ELIGIBILITY_UNAVAILABLE
    elif (
        source.compatibility_decision.outcome
        is CompatibilityOutcome.DIFFERENT_QUANTITY
    ):
        reason = MemberDispositionReason.COMPATIBILITY_DIFFERENT_QUANTITY
    elif source.compatibility_decision.outcome is CompatibilityOutcome.INCOMPATIBLE:
        reason = MemberDispositionReason.COMPATIBILITY_INCOMPATIBLE
    elif source.compatibility_decision.outcome is CompatibilityOutcome.UNKNOWN:
        reason = MemberDispositionReason.COMPATIBILITY_UNKNOWN
    elif CountState.MISSING in states:
        reason = MemberDispositionReason.DENOMINATOR_MISSING
    elif CountState.WITHHELD in states:
        reason = MemberDispositionReason.DENOMINATOR_WITHHELD
    if reason is None:
        evidence = CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.INCLUDED,
            reason=MemberDispositionReason.INCLUDED_BY_POLICY,
            catalog_result=binding.result,
            result_source=source,
        )
        _validate_included(evidence)
        return evidence
    return CohortMemberEvidence(
        member_sha256=member_sha256,
        disposition=MemberDisposition.UNAVAILABLE,
        reason=reason,
        catalog_result=binding.result,
        result_source=source,
    )


def _require_all_result_sources_consumed(
    evidence: tuple[CohortMemberEvidence, ...],
    sources_by_result_id: dict[str, ResultViewSource],
) -> None:
    consumed = {
        item.result_source.record.result_id
        for item in evidence
        if item.result_source is not None
    }
    if consumed != set(sources_by_result_id):
        raise ValueError(
            "cohort result source is not consumed by the selected population"
        )


def _validate_record_status(
    history: RegisteredCohortHistory,
    status: CohortManifestRecordStatus,
) -> CohortManifest:
    manifest = history.manifests[-1]
    if (
        status.cohort_id != manifest.cohort_id
        or status.cohort_version != manifest.version
        or status.cohort_manifest_sha256 != history.selected_manifest_sha256
        or status.inclusion_policy_sha256 != manifest.policies.inclusion_sha256
        or status.exclusion_policy_sha256 != manifest.policies.exclusion_sha256
        or status.missingness_policy_sha256 != manifest.policies.missingness_sha256
        or status.record_status_policy_sha256
        != COHORT_RECORD_STATUS_POLICY_SHA256
        or len(status.members) != len(manifest.members)
    ):
        raise ValueError("cohort catalog status does not bind selected manifest")
    for member, member_status in zip(
        manifest.members, status.members, strict=True
    ):
        if (
            member_status.provider_namespace != member.provider_namespace
            or member_status.analysis_record_id != member.analysis_record_id
            or member_status.member_sha256 != _sha256_exact(member, CohortMember)
        ):
            raise ValueError("cohort catalog status member binding is invalid")
    return manifest


def _replay_comparisons(
    *,
    request: object,
    linkage_store: ProviderLinkageStore,
) -> tuple[
    CohortComparisonReplay,
    LongitudinalSeriesDecision,
    tuple[RepeatabilityComparison, ...],
    str,
    tuple[ComparisonEligibilityCount, ...],
]:
    captured = _replay_exact(request, CohortComparisonReplay)
    if type(captured) is not CohortComparisonReplay:
        raise ValueError("cohort comparison replay input is invalid")
    if longitudinal_anchor_policy_sha256(captured.policy) != captured.expected_policy_sha256:
        raise ValueError("D02 anchor policy identity is invalid")
    series = replay_longitudinal_series_decision(
        captured.expected_series,
        captured.anchor,
        captured.members,
        captured.policy,
        expected_policy_sha256=captured.expected_policy_sha256,
        expected_authority_head_sha256=captured.expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider=dict(
            captured.expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_store=linkage_store,
    )
    records = {captured.anchor.measurement.result_id: captured.anchor}
    records.update({item.measurement.result_id: item for item in captured.members})
    if len(records) != len(captured.members) + 1:
        raise ValueError("D03 replay contains duplicate result identities")
    decisions = {item.member_result_id: item for item in series.decisions}
    replay_inputs = {item.member_result_id: item for item in captured.repeatability}
    if len(replay_inputs) != len(captured.repeatability):
        raise ValueError("D07 replay inputs cannot be duplicated")
    if set(replay_inputs) != set(series.member_result_ids):
        raise ValueError("D07 replay must cover every D03 member exactly once")
    counts: dict[ComparisonEligibility, int] = {}
    comparisons: list[RepeatabilityComparison] = []
    for member_result_id in series.member_result_ids:
        replay = replay_inputs[member_result_id]
        actual = compare_repeatability(
            captured.anchor,
            records[member_result_id],
            captured.policy,
            decisions[member_result_id],
            replay.anchor_observation,
            replay.member_observation,
            replay.envelope,
            evaluated_at=replay.evaluated_at,
            expected_policy_sha256=captured.expected_policy_sha256,
            expected_authority_head_sha256=captured.expected_authority_head_sha256,
            expected_linkage_trust_snapshot_sha256_by_provider=dict(
                captured.expected_linkage_trust_snapshot_sha256_by_provider
            ),
            linkage_store=linkage_store,
            result_trust_document=replay.result_trust_document,
            expected_result_trust_sha256=replay.expected_result_trust_sha256,
            expected_envelope_sha256=replay.expected_envelope_sha256,
            expected_evidence_sha256=replay.expected_evidence_sha256,
            expected_protocol_sha256=replay.expected_protocol_sha256,
            expected_repeatability_authority_sha256=(
                replay.expected_repeatability_authority_sha256
            ),
        )
        if (
            actual != replay.expected
            or repeatability_comparison_sha256(actual)
            != repeatability_comparison_sha256(replay.expected)
        ):
            raise ValueError("stored D07 comparison does not replay exactly")
        comparisons.append(actual)
        eligibility = _COMPARISON_ELIGIBILITY_BY_CLASSIFICATION[
            actual.classification
        ]
        if (
            eligibility is ComparisonEligibility.AVAILABLE
        ) != (actual.availability is ComparisonAvailability.AVAILABLE):
            raise ValueError("D07 comparison availability is inconsistent")
        if actual.classification in {
            RepeatabilityClassification.INCOMPATIBLE,
            RepeatabilityClassification.UNKNOWN,
        } and actual.trend_allowed:
            raise ValueError("non-comparable D07 state cannot allow a trend")
        counts[eligibility] = counts.get(eligibility, 0) + 1
    return (
        captured,
        series,
        tuple(comparisons),
        hashlib.sha256(_exact_bytes(captured, CohortComparisonReplay)).hexdigest(),
        tuple(
            ComparisonEligibilityCount(state=state, count=count)
            for state, count in sorted(counts.items(), key=lambda item: item[0].value)
        ),
    )


def _validate_comparison_bindings(
    *,
    request: CohortComparisonReplay,
    series: LongitudinalSeriesDecision,
    comparisons: tuple[RepeatabilityComparison, ...],
    manifest: CohortManifest,
    status: CohortManifestRecordStatus,
    sources_by_result_id: dict[str, ResultViewSource],
) -> None:
    if (
        request.expected_policy_sha256
        != manifest.measurement_anchor.anchor_definition_sha256
        or series.linkage_snapshot_sha256 != status.linkage_snapshot_sha256
    ):
        raise ValueError("D02/D03 authority does not bind the cohort manifest")
    records = {request.anchor.measurement.result_id: request.anchor}
    records.update({item.measurement.result_id: item for item in request.members})
    if set(records) != set(sources_by_result_id):
        raise ValueError("D03 replay membership does not match D06 cohort results")
    for result_id, record in records.items():
        source = sources_by_result_id.get(result_id)
        if source is None:
            raise ValueError("D03 replay result is not an available D06 member result")
        measurement = record.measurement
        if (
            source.record.result_id != measurement.result_id
            or source.record.result_sha256 != measurement.result_sha256
            or source.record.bundle_sha256 != measurement.bundle_sha256
            or source.record.method_definition_sha256
            != measurement.method_definition_sha256
        ):
            raise ValueError("D03 replay does not bind the exact D06 result source")
    if tuple(item.member_record_sha256 for item in comparisons) != tuple(
        item.expected.member_record_sha256 for item in request.repeatability
    ):
        # This branch is intentionally unreachable for canonical replay inputs;
        # keep comparison order and membership explicit at the D09 boundary.
        raise ValueError("D07 replay membership changed")


@contextmanager
def registered_cohort_denominator_authority_fence(
    *,
    registry: CohortRegistry,
    selector_id: str,
    cohort_version: int,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
    result_sources: tuple[ResultViewSource, ...],
    comparison_replay: CohortComparisonReplay | None = None,
    linkage_store: ProviderLinkageStore | None = None,
) -> Iterator[RegisteredCohortDenominatorDerivation]:
    """Yield one protected D09 derivation while all live authority remains fenced."""

    if type(registry) is not CohortRegistry:
        raise TypeError("cohort registry type is invalid")
    if type(record_catalog) is not CohortRecordCatalog:
        raise TypeError("cohort record catalog type is invalid")
    if (
        "resolve_history" in vars(registry)
        or CohortRegistry.__dict__.get("resolve_history") is not _PINNED_RESOLVE_HISTORY
        or "record_status_for_manifest" in vars(record_catalog)
        or CohortRecordCatalog.__dict__.get("record_status_for_manifest")
        is not _PINNED_RECORD_STATUS
        or "record_status_authority_fence" in vars(record_catalog)
        or CohortRecordCatalog.__dict__.get("record_status_authority_fence")
        is not _PINNED_RECORD_STATUS_FENCE
    ):
        raise TypeError("cohort summary authority callable changed")
    if type(policy) is not CohortDenominatorPolicy:
        raise TypeError("cohort denominator policy type is invalid")
    if type(disposition_policy) is not CohortDispositionPolicy:
        raise TypeError("cohort disposition policy type is invalid")
    if type(result_sources) is not tuple:
        raise TypeError("cohort result sources must be one exact tuple")
    if len(result_sources) > MAX_COHORT_SUMMARY_MEMBERS:
        raise ValueError("cohort result source count exceeds its bound")
    if (comparison_replay is None) != (linkage_store is None):
        raise ValueError("comparison replay requires its exact live linkage authority")
    if linkage_store is not None and type(linkage_store) is not ProviderLinkageStore:
        raise TypeError("comparison linkage authority type is invalid")
    try:
        policy_content = _exact_bytes(
            policy, CohortDenominatorPolicy, max_bytes=64 * 1024
        )
        replayed_policy = CohortDenominatorPolicy.model_validate_json(policy_content)
        replayed_disposition_policy = _replay_disposition_policy(disposition_policy)
        replayed_sources = tuple(_replay_source(item) for item in result_sources)
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    sources_by_result_id = {
        item.record.result_id: item for item in replayed_sources
    }
    if len(sources_by_result_id) != len(replayed_sources):
        raise ValueError("cohort result sources cannot be duplicated")
    replayed_comparison: CohortComparisonReplay | None = None
    replayed_series: LongitudinalSeriesDecision | None = None
    replayed_repeatability: tuple[RepeatabilityComparison, ...] = ()
    comparison_sha256: str | None = None
    comparison_eligibility: tuple[ComparisonEligibilityCount, ...] = ()
    if comparison_replay is not None:
        assert linkage_store is not None
        (
            replayed_comparison,
            replayed_series,
            replayed_repeatability,
            comparison_sha256,
            comparison_eligibility,
        ) = _replay_comparisons(
            request=comparison_replay,
            linkage_store=linkage_store,
        )

    with _PINNED_RECORD_STATUS_FENCE(
        record_catalog,
        selector_id,
        cohort_version,
        expected_registry=registry,
        expected_linkage_store=linkage_store,
    ) as (history, status):
        manifest = _validate_record_status(history, status)
        if replayed_comparison is not None:
            assert replayed_series is not None
            _validate_comparison_bindings(
                request=replayed_comparison,
                series=replayed_series,
                comparisons=replayed_repeatability,
                manifest=manifest,
                status=status,
                sources_by_result_id=sources_by_result_id,
            )
        _validate_disposition_policy(
            policy=replayed_policy,
            manifest=manifest,
            disposition_policy=replayed_disposition_policy,
        )
        available_result_ids = {
            item.binding.result.result_id
            for item in status.members
            if item.binding is not None
        }
        if not set(sources_by_result_id).issubset(available_result_ids):
            raise ValueError("cohort result source is not an available catalog result")
        evidence = tuple(
            _derived_disposition(
                member=member,
                status=member_status,
                source=(
                    sources_by_result_id.get(member_status.binding.result.result_id)
                    if member_status.binding is not None
                    else None
                ),
                disposition_policy=replayed_disposition_policy,
            )
            for member, member_status in zip(
                manifest.members, status.members, strict=True
            )
        )
        _require_all_result_sources_consumed(evidence, sources_by_result_id)
        population = build_cohort_denominator_summary(
            manifest=manifest,
            policy=replayed_policy,
            disposition_policy=replayed_disposition_policy,
            evidence=evidence,
        )
        population_values = population.model_dump(
            mode="python",
            exclude={
                "schema_version",
                "rows",
                "synthetic_only",
                "clinical_use_authorized",
                "scientific_qualification_claimed",
            },
        )
        population_projection = CohortPopulationProjection(**population_values)
        payload = {
            "registry_id": history.registry_id,
            "registry_epoch_sha256": history.registry_epoch_sha256,
            "registry_state_version": history.state_version,
            "registry_state_head_sha256": history.state_head_sha256,
            "selector_id": selector_id,
            "cohort_version": cohort_version,
            "cohort_manifest_sha256": history.selected_manifest_sha256,
            "linkage_snapshot_sha256": status.linkage_snapshot_sha256,
            "catalog_authority_sha256": status.catalog_authority_sha256,
            "record_status_sha256": status.status_sha256,
            "record_status_policy_sha256": status.record_status_policy_sha256,
            "population": population_projection,
            "comparison_replay_sha256": comparison_sha256,
            "comparison_eligibility": comparison_eligibility,
        }
        placeholder = RegisteredCohortDenominatorSummary.model_construct(
            **payload,
            summary_sha256="0" * 64,
            synthetic_only=True,
            clinical_use_authorized=False,
            scientific_qualification_claimed=False,
        )
        result = RegisteredCohortDenominatorSummary(
            **payload,
            summary_sha256=_sha256_exact(
                placeholder, RegisteredCohortDenominatorSummary
            ),
        )
        result = RegisteredCohortDenominatorSummary.model_validate_json(
            registered_cohort_denominator_summary_bytes(result)
        )
        yield RegisteredCohortDenominatorDerivation(
            summary=result,
            manifest=manifest,
            population=population,
            comparison_replay=replayed_comparison,
            comparison_series=replayed_series,
            repeatability_comparisons=replayed_repeatability,
        )


def build_registered_cohort_denominator_summary(
    *,
    registry: CohortRegistry,
    selector_id: str,
    cohort_version: int,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
    result_sources: tuple[ResultViewSource, ...],
    comparison_replay: CohortComparisonReplay | None = None,
    linkage_store: ProviderLinkageStore | None = None,
) -> RegisteredCohortDenominatorSummary:
    """Derive one public D09 summary from exact live D05/D06 authority state."""

    with registered_cohort_denominator_authority_fence(
        registry=registry,
        selector_id=selector_id,
        cohort_version=cohort_version,
        record_catalog=record_catalog,
        policy=policy,
        disposition_policy=disposition_policy,
        result_sources=result_sources,
        comparison_replay=comparison_replay,
        linkage_store=linkage_store,
    ) as derivation:
        return derivation.summary


def _row(
    *, ordinal: int, member: object, evidence: CohortMemberEvidence
) -> CohortSummaryRow:
    catalog = evidence.catalog_result
    source = evidence.result_source
    return CohortSummaryRow(
        ordinal=ordinal,
        member_sha256=evidence.member_sha256,
        lineage_role=member.lineage_role,
        denominator_contribution=member.denominator_contribution,
        disposition=evidence.disposition,
        reason=evidence.reason,
        catalog_result_sha256=(
            _sha256_exact(catalog, CatalogResultRef)
            if catalog is not None
            else None
        ),
        result_id=catalog.result_id if catalog is not None else None,
        result_source_sha256=(
            _sha256_exact(source, ResultViewSource) if source is not None else None
        ),
        denominator_ledger_sha256=(
            _sha256_exact(source.denominator, DenominatorLedger)
            if source is not None
            else None
        ),
    )


def _replay_evidence(item: object) -> CohortMemberEvidence:
    try:
        replayed = _replay_exact(item, CohortMemberEvidence)
        assert type(replayed) is CohortMemberEvidence
        return replayed
    except (AssertionError, ValidationError, TypeError, ValueError):
        raise ValueError("cohort member evidence is invalid") from None


def build_cohort_denominator_summary(
    *,
    manifest: CohortManifest,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy | None = None,
    evidence: tuple[CohortMemberEvidence, ...],
) -> CohortDenominatorSummary:
    """Build a canonical summary after exact coverage and eligibility checks."""

    if type(manifest) is not CohortManifest:
        raise TypeError("cohort manifest type is invalid")
    if type(policy) is not CohortDenominatorPolicy:
        raise TypeError("cohort denominator policy type is invalid")
    if type(evidence) is not tuple:
        raise TypeError("cohort evidence must be one exact tuple")
    try:
        manifest = CohortManifest.model_validate_json(
            _exact_bytes(manifest, CohortManifest)
        )
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    if len(evidence) != len(manifest.members):
        raise ValueError("evidence must cover every manifest member exactly once")
    try:
        policy = CohortDenominatorPolicy.model_validate_json(
            _exact_bytes(policy, CohortDenominatorPolicy)
        )
        disposition_policy = (
            _replay_disposition_policy(disposition_policy)
            if disposition_policy is not None
            else None
        )
        evidence = tuple(_replay_evidence(item) for item in evidence)
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    if (
        policy.inclusion_sha256,
        policy.exclusion_sha256,
        policy.missingness_sha256,
    ) != (
        manifest.policies.inclusion_sha256,
        manifest.policies.exclusion_sha256,
        manifest.policies.missingness_sha256,
    ):
        raise ValueError("denominator policy does not bind manifest policies")
    if disposition_policy is not None:
        _validate_disposition_policy(
            policy=policy,
            manifest=manifest,
            disposition_policy=disposition_policy,
        )
    by_digest = {item.member_sha256: item for item in evidence}
    if len(by_digest) != len(evidence):
        raise ValueError("member evidence cannot be duplicated")
    member_digests = tuple(
        _sha256_exact(member, CohortMember) for member in manifest.members
    )
    if set(by_digest) != set(member_digests):
        raise ValueError("member evidence does not match exact manifest membership")

    rows: list[CohortSummaryRow] = []
    for ordinal, (member, member_sha256) in enumerate(
        zip(manifest.members, member_digests, strict=True)
    ):
        item = by_digest[member_sha256]
        if item.catalog_result is not None and item.result_source is not None:
            _validate_catalog_binding(item.catalog_result, item.result_source)
        if item.disposition == MemberDisposition.INCLUDED:
            _validate_included(item)
        elif item.disposition == MemberDisposition.UNAVAILABLE:
            if not _unavailability_reason_matches(item):
                raise ValueError(
                    "unavailable reason is not supported by exact evidence"
                )
        elif item.reason == MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED:
            if member.lineage_role != MemberLineageRole.TECHNICAL_REPLICATE:
                raise ValueError("technical-replicate reason does not match lineage")
        elif (
            item.reason == MemberDispositionReason.REANALYSIS_COLLAPSED
            and member.lineage_role != MemberLineageRole.REANALYSIS
        ):
            raise ValueError("reanalysis reason does not match lineage")
        elif item.reason in {
            MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY,
            MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
        }:
            if disposition_policy is None:
                raise ValueError("policy exclusion requires exact policy evidence")
            expected_members = (
                disposition_policy.inclusion.member_sha256s
                if item.reason
                is MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY
                else disposition_policy.exclusion.member_sha256s
            )
            if item.member_sha256 not in expected_members:
                raise ValueError("policy exclusion is not supported by exact policy")
        rows.append(_row(ordinal=ordinal, member=member, evidence=item))

    included_members = sum(
        row.disposition == MemberDisposition.INCLUDED for row in rows
    )
    excluded_members = sum(
        row.disposition == MemberDisposition.EXCLUDED for row in rows
    )
    unavailable_members = sum(
        row.disposition == MemberDisposition.UNAVAILABLE for row in rows
    )
    included_units = sum(
        row.denominator_contribution and row.disposition == MemberDisposition.INCLUDED
        for row in rows
    )
    excluded_units = sum(
        row.denominator_contribution and row.disposition == MemberDisposition.EXCLUDED
        for row in rows
    )
    unavailable_units = sum(
        row.denominator_contribution
        and row.disposition == MemberDisposition.UNAVAILABLE
        for row in rows
    )
    payload = {
        "cohort_id": manifest.cohort_id,
        "cohort_version": manifest.version,
        "cohort_manifest_sha256": cohort_manifest_sha256(manifest),
        "denominator_policy_id": policy.policy_id,
        "denominator_policy_sha256": cohort_denominator_policy_sha256(policy),
        "inclusion_sha256": policy.inclusion_sha256,
        "exclusion_sha256": policy.exclusion_sha256,
        "missingness_sha256": policy.missingness_sha256,
        "state": (
            CohortSummaryState.NO_INCLUDED_UNITS
            if included_units == 0
            else CohortSummaryState.ONE_INCLUDED_UNIT
            if included_units == 1
            else CohortSummaryState.MULTIPLE_INCLUDED_UNITS
        ),
        "declared_members": len(rows),
        "included_members": included_members,
        "excluded_members": excluded_members,
        "unavailable_members": unavailable_members,
        "declared_denominator_units": sum(row.denominator_contribution for row in rows),
        "included_denominator_units": included_units,
        "excluded_denominator_units": excluded_units,
        "unavailable_denominator_units": unavailable_units,
        "rows": tuple(rows),
    }
    placeholder = CohortDenominatorSummary.model_construct(
        **payload,
        population_id="population_" + "0" * 40,
        population_sha256="0" * 64,
        synthetic_only=True,
        clinical_use_authorized=False,
        scientific_qualification_claimed=False,
    )
    population_sha256 = _sha256_exact(placeholder, CohortDenominatorSummary)
    return CohortDenominatorSummary(
        **payload,
        population_id=f"population_{population_sha256[:40]}",
        population_sha256=population_sha256,
    )


def cohort_denominator_summary_bytes(summary: CohortDenominatorSummary) -> bytes:
    return _exact_bytes(summary, CohortDenominatorSummary)


def cohort_denominator_summary_from_bytes(
    content: bytes,
) -> CohortDenominatorSummary:
    try:
        decoded = bounded_json_loads(
            content,
            max_bytes=MAX_COHORT_INPUT_BYTES,
            max_depth=MAX_COHORT_INPUT_DEPTH,
            max_nodes=MAX_COHORT_INPUT_NODES,
            max_collection_items=MAX_COHORT_SUMMARY_MEMBERS,
            max_string_bytes=4_096,
        )
        summary = CohortDenominatorSummary.model_validate(decoded)
        if cohort_denominator_summary_bytes(summary) != content:
            raise ValueError("cohort denominator summary is not canonical")
        return summary
    except (TypeError, ValueError):
        raise ValueError("cohort denominator summary is not canonical") from None


def registered_cohort_denominator_summary_bytes(
    summary: RegisteredCohortDenominatorSummary,
) -> bytes:
    return exact_model_bytes(
        summary,
        RegisteredCohortDenominatorSummary,
        model_types=_REGISTERED_SUMMARY_MODEL_TYPES,
        enum_types=_REGISTERED_SUMMARY_ENUM_TYPES,
        max_bytes=MAX_REGISTERED_SUMMARY_BYTES,
        max_nodes=MAX_REGISTERED_SUMMARY_NODES,
        max_depth=MAX_REGISTERED_SUMMARY_DEPTH,
        max_collection_items=MAX_COHORT_SUMMARY_MEMBERS,
        max_string_bytes=4_096,
    )


def registered_cohort_denominator_summary_from_bytes(
    content: bytes,
) -> RegisteredCohortDenominatorSummary:
    try:
        decoded = bounded_json_loads(
            content,
            max_bytes=MAX_REGISTERED_SUMMARY_BYTES,
            max_depth=MAX_REGISTERED_SUMMARY_DEPTH,
            max_nodes=MAX_REGISTERED_SUMMARY_NODES,
            max_collection_items=MAX_COHORT_SUMMARY_MEMBERS,
            max_string_bytes=4_096,
        )
        summary = RegisteredCohortDenominatorSummary.model_validate(decoded)
        if registered_cohort_denominator_summary_bytes(summary) != content:
            raise ValueError("registered cohort denominator summary is not canonical")
        return summary
    except (TypeError, ValueError):
        raise ValueError(
            "registered cohort denominator summary is not canonical"
        ) from None


__all__ = [
    "MAX_COHORT_SUMMARY_MEMBERS",
    "MAX_REGISTERED_SUMMARY_BYTES",
    "CohortDenominatorPolicy",
    "CohortDenominatorSummary",
    "CohortDispositionPolicy",
    "CohortMemberExclusionSet",
    "CohortComparisonReplay",
    "CohortRepeatabilityReplay",
    "CohortPopulationProjection",
    "CohortMemberEvidence",
    "CohortSummaryRow",
    "CohortSummaryState",
    "ComparisonEligibility",
    "ComparisonEligibilityCount",
    "DenominatorBasis",
    "InclusionRule",
    "MemberDisposition",
    "MemberDispositionReason",
    "MissingValueRule",
    "UnavailableUnitRule",
    "RegisteredCohortDenominatorDerivation",
    "RegisteredCohortDenominatorSummary",
    "build_cohort_denominator_summary",
    "build_registered_cohort_denominator_summary",
    "cohort_denominator_policy_sha256",
    "cohort_member_exclusion_set_sha256",
    "cohort_denominator_summary_bytes",
    "cohort_denominator_summary_from_bytes",
    "registered_cohort_denominator_summary_bytes",
    "registered_cohort_denominator_summary_from_bytes",
    "registered_cohort_denominator_authority_fence",
]
