"""Deterministic D09 cohort denominators and missingness summaries.

This is a protected, synthetic/local comparison-population contract.  It binds
an immutable D05 manifest to independently indexed E04/D06 result references
and exact E06 denominator ledgers.  It does not expose provider-local linkage
tokens, infer missing linkage, calculate a scientific delta, or claim that an
Epic D qualification gate passed.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MemberLineageRole,
    cohort_manifest_bytes,
    cohort_manifest_from_bytes,
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
    RegistryIdentityError,
    Sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.cohort_import import (
    CohortManifestRecordStatus,
    CohortMemberRecordStatus,
    CohortRecordAvailability,
    CohortRecordCatalog,
)
from evidence_inspector.result_view import (
    CountState,
    ResultViewReplayError,
    ResultViewSource,
    canonical_result_view_bytes,
    result_view_contract_from_canonical_bytes,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_COHORT_SUMMARY_MEMBERS = 100_000
MAX_REGISTERED_SUMMARY_BYTES = 64 * 1024
MAX_REGISTERED_SUMMARY_DEPTH = 32
MAX_REGISTERED_SUMMARY_NODES = 10_000
_PINNED_RESOLVE_HISTORY = CohortRegistry.resolve_history
_PINNED_RECORD_STATUS = CohortRecordCatalog.record_status_for_manifest
DenominatorPolicyId = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=96,
        pattern=r"^denominator_[a-z0-9]+(?:_[a-z0-9]+)*$",
    ),
]


def _sha256(value: RegistryContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def _canonical_model_bytes(value: object) -> bytes:
    payload = value.model_dump(  # type: ignore[attr-defined]
        mode="json", exclude_none=False, warnings="error"
    )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


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


_POLICY_MODEL_TYPES, _POLICY_ENUM_TYPES = contract_type_graph(
    CohortDenominatorPolicy
)


def cohort_denominator_policy_sha256(policy: CohortDenominatorPolicy) -> str:
    return _sha256(policy)


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
    QUALIFICATION_UNAVAILABLE = "qualification_unavailable"
    PROVIDER_ELIGIBILITY_UNAVAILABLE = "provider_eligibility_unavailable"
    COMPATIBILITY_NOT_ESTABLISHED = "compatibility_not_established"
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
        expected_sha256 = _sha256(payload)
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
    population: CohortPopulationProjection
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
        placeholder = self.model_copy(update={"summary_sha256": "0" * 64})
        if self.summary_sha256 != _sha256(placeholder):
            raise ValueError("registered summary digest is invalid")
        return self


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
        MemberDispositionReason.COMPATIBILITY_NOT_ESTABLISHED: (
            source.compatibility_decision.outcome != CompatibilityOutcome.COMPARABLE
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
    if type(source) is not ResultViewSource:
        raise TypeError("cohort result source type is invalid")
    try:
        return result_view_contract_from_canonical_bytes(
            ResultViewSource, canonical_result_view_bytes(source)
        )
    except (ResultViewReplayError, TypeError, ValueError):
        raise ValueError("cohort result source is invalid") from None


def _derived_disposition(
    *,
    member: CohortMember,
    status: CohortMemberRecordStatus,
    source: ResultViewSource | None,
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
    if status.availability is CohortRecordAvailability.MISSING:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT,
        )
    if status.availability is CohortRecordAvailability.WITHHELD:
        return CohortMemberEvidence(
            member_sha256=member_sha256,
            disposition=MemberDisposition.UNAVAILABLE,
            reason=MemberDispositionReason.TRUST_REVOKED,
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
    elif source.compatibility_decision.outcome is not CompatibilityOutcome.COMPARABLE:
        reason = MemberDispositionReason.COMPATIBILITY_NOT_ESTABLISHED
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
        or len(status.members) != len(manifest.members)
    ):
        raise ValueError("cohort catalog status does not bind selected manifest")
    for member, member_status in zip(
        manifest.members, status.members, strict=True
    ):
        if (
            member_status.provider_namespace != member.provider_namespace
            or member_status.analysis_record_id != member.analysis_record_id
            or member_status.member_sha256 != _sha256(member)
        ):
            raise ValueError("cohort catalog status member binding is invalid")
    return manifest


def build_registered_cohort_denominator_summary(
    *,
    registry: CohortRegistry,
    selector_id: str,
    cohort_version: int,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    result_sources: tuple[ResultViewSource, ...],
) -> RegisteredCohortDenominatorSummary:
    """Derive one D09 population from exact live D05/D06 authority state."""

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
    ):
        raise TypeError("cohort summary authority callable changed")
    if type(policy) is not CohortDenominatorPolicy:
        raise TypeError("cohort denominator policy type is invalid")
    if type(result_sources) is not tuple:
        raise TypeError("cohort result sources must be one exact tuple")
    if len(result_sources) > MAX_COHORT_SUMMARY_MEMBERS:
        raise ValueError("cohort result source count exceeds its bound")
    try:
        policy_content = exact_model_bytes(
            policy,
            CohortDenominatorPolicy,
            model_types=_POLICY_MODEL_TYPES,
            enum_types=_POLICY_ENUM_TYPES,
            max_bytes=64 * 1024,
        )
        replayed_policy = CohortDenominatorPolicy.model_validate_json(policy_content)
        replayed_sources = tuple(_replay_source(item) for item in result_sources)
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    sources_by_result_id = {
        item.record.result_id: item for item in replayed_sources
    }
    if len(sources_by_result_id) != len(replayed_sources):
        raise ValueError("cohort result sources cannot be duplicated")

    initial_history = _PINNED_RESOLVE_HISTORY(
        registry, selector_id, cohort_version
    )
    initial_status = _PINNED_RECORD_STATUS(
        record_catalog, initial_history.manifests
    )
    manifest = _validate_record_status(initial_history, initial_status)
    available_result_ids = {
        item.binding.result.result_id
        for item in initial_status.members
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
        )
        for member, member_status in zip(
            manifest.members, initial_status.members, strict=True
        )
    )
    _require_all_result_sources_consumed(evidence, sources_by_result_id)
    population = build_cohort_denominator_summary(
        manifest=manifest,
        policy=replayed_policy,
        evidence=evidence,
    )
    population_projection = CohortPopulationProjection(
        **population.model_dump(
            mode="python",
            exclude={
                "schema_version",
                "rows",
                "synthetic_only",
                "clinical_use_authorized",
                "scientific_qualification_claimed",
            },
        )
    )

    final_history = _PINNED_RESOLVE_HISTORY(registry, selector_id, cohort_version)
    final_status = _PINNED_RECORD_STATUS(record_catalog, final_history.manifests)
    if final_history != initial_history or final_status != initial_status:
        raise ValueError("cohort summary authority changed during derivation")
    payload = {
        "registry_id": final_history.registry_id,
        "registry_epoch_sha256": final_history.registry_epoch_sha256,
        "registry_state_version": final_history.state_version,
        "registry_state_head_sha256": final_history.state_head_sha256,
        "selector_id": selector_id,
        "cohort_version": cohort_version,
        "cohort_manifest_sha256": final_history.selected_manifest_sha256,
        "linkage_snapshot_sha256": final_status.linkage_snapshot_sha256,
        "catalog_authority_sha256": final_status.catalog_authority_sha256,
        "record_status_sha256": final_status.status_sha256,
        "population": population_projection,
    }
    placeholder = RegisteredCohortDenominatorSummary.model_construct(
        **payload,
        summary_sha256="0" * 64,
        synthetic_only=True,
        clinical_use_authorized=False,
        scientific_qualification_claimed=False,
    )
    return RegisteredCohortDenominatorSummary(
        **payload,
        summary_sha256=_sha256(placeholder),
    )


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
            hashlib.sha256(_canonical_model_bytes(catalog)).hexdigest()
            if catalog is not None
            else None
        ),
        result_id=catalog.result_id if catalog is not None else None,
        result_source_sha256=_sha256(source) if source is not None else None,
        denominator_ledger_sha256=(
            _sha256(source.denominator) if source is not None else None
        ),
    )


def _replay_evidence(item: object) -> CohortMemberEvidence:
    if type(item) is not CohortMemberEvidence:
        raise TypeError("cohort member evidence type is invalid")
    catalog = item.catalog_result
    source = item.result_source
    try:
        replayed_catalog = (
            CatalogResultRef.model_validate_json(_canonical_model_bytes(catalog))
            if catalog is not None
            else None
        )
        replayed_source = (
            result_view_contract_from_canonical_bytes(
                ResultViewSource, canonical_result_view_bytes(source)
            )
            if source is not None
            else None
        )
        return CohortMemberEvidence(
            member_sha256=item.member_sha256,
            disposition=item.disposition,
            reason=item.reason,
            catalog_result=replayed_catalog,
            result_source=replayed_source,
        )
    except (ValidationError, ResultViewReplayError, TypeError, ValueError):
        raise ValueError("cohort member evidence is invalid") from None


def build_cohort_denominator_summary(
    *,
    manifest: CohortManifest,
    policy: CohortDenominatorPolicy,
    evidence: tuple[CohortMemberEvidence, ...],
) -> CohortDenominatorSummary:
    """Build a canonical summary after exact coverage and eligibility checks."""

    if type(manifest) is not CohortManifest:
        raise TypeError("cohort manifest type is invalid")
    if type(policy) is not CohortDenominatorPolicy:
        raise TypeError("cohort denominator policy type is invalid")
    if type(evidence) is not tuple:
        raise TypeError("cohort evidence must be one exact tuple")
    manifest = cohort_manifest_from_bytes(cohort_manifest_bytes(manifest))
    if len(evidence) != len(manifest.members):
        raise ValueError("evidence must cover every manifest member exactly once")
    try:
        policy = contract_from_canonical_bytes(
            CohortDenominatorPolicy, canonical_contract_bytes(policy)
        )
        evidence = tuple(_replay_evidence(item) for item in evidence)
    except RegistryIdentityError as exc:
        raise ValueError("cohort summary input is not canonical") from exc
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
    by_digest = {item.member_sha256: item for item in evidence}
    if len(by_digest) != len(evidence):
        raise ValueError("member evidence cannot be duplicated")
    member_digests = tuple(_sha256(member) for member in manifest.members)
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
    population_sha256 = _sha256(placeholder)
    return CohortDenominatorSummary(
        **payload,
        population_id=f"population_{population_sha256[:40]}",
        population_sha256=population_sha256,
    )


def cohort_denominator_summary_bytes(summary: CohortDenominatorSummary) -> bytes:
    return canonical_contract_bytes(summary)


def cohort_denominator_summary_from_bytes(
    content: bytes,
) -> CohortDenominatorSummary:
    try:
        return contract_from_canonical_bytes(CohortDenominatorSummary, content)
    except RegistryIdentityError as exc:
        raise ValueError("cohort denominator summary is not canonical") from exc


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
    "CohortPopulationProjection",
    "CohortMemberEvidence",
    "CohortSummaryRow",
    "CohortSummaryState",
    "DenominatorBasis",
    "InclusionRule",
    "MemberDisposition",
    "MemberDispositionReason",
    "MissingValueRule",
    "UnavailableUnitRule",
    "RegisteredCohortDenominatorSummary",
    "build_cohort_denominator_summary",
    "build_registered_cohort_denominator_summary",
    "cohort_denominator_policy_sha256",
    "cohort_denominator_summary_bytes",
    "cohort_denominator_summary_from_bytes",
    "registered_cohort_denominator_summary_bytes",
    "registered_cohort_denominator_summary_from_bytes",
]
