"""Deterministic D09 cohort denominators and missingness summaries.

This is a protected, synthetic/local comparison-population contract.  It binds
an immutable D05 manifest to fenced D06 record status (registered path) or to
supplied E04 references and E06 ledgers (protected v1 builder).  It does not expose provider-local linkage
tokens, infer missing linkage, calculate a scientific delta, or claim that an
Epic D qualification gate passed.
"""

from __future__ import annotations

import hashlib
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


class RegisteredCohortDenominatorSummary(RegistryContract):
    """D09 population bound to live D05 registry and D06 catalog state."""

    schema_version: Literal["traceback.registered-cohort-denominator-summary.v3"] = (
        "traceback.registered-cohort-denominator-summary.v3"
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
        if self.summary_sha256 != _sha256_exact(
            placeholder, RegisteredCohortDenominatorSummary
        ):
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


def _registered_row(
    *,
    ordinal: int,
    member: CohortMember,
    status: CohortMemberRecordStatus,
    disposition_policy: CohortDispositionPolicy,
) -> CohortSummaryRow:
    """Derive one row from fenced D05 lineage/policy and D06 record status only."""

    catalog: CatalogResultRef | None = None
    required_reason = _policy_required_reason(
        member=member,
        member_sha256=status.member_sha256,
        disposition_policy=disposition_policy,
    )
    if required_reason is not None:
        disposition = MemberDisposition.EXCLUDED
        reason = required_reason
    elif status.availability is CohortRecordAvailability.MISSING:
        disposition = MemberDisposition.UNAVAILABLE
        reason = MemberDispositionReason.NO_VERIFIED_CATALOG_RESULT
    elif status.availability is CohortRecordAvailability.WITHHELD:
        if status.withheld_reason is not CohortRecordWithheldReason.RESULT_KEY_REVOKED:
            raise ValueError("cohort withheld reason is unsupported")
        disposition = MemberDisposition.UNAVAILABLE
        reason = MemberDispositionReason.RESULT_WITHHELD_KEY_REVOKED
    else:
        if status.binding is None:
            raise ValueError("available cohort status has no exact binding")
        # D06 indexes only complete, development-signature-verified results
        # whose method equals the manifest measurement definition, and it
        # re-verifies the bundle against live trust inside the fence.
        catalog = status.binding.result
        disposition = MemberDisposition.UNAVAILABLE
        if catalog.qualification_state is not CatalogQualificationState.QUALIFIED:
            reason = MemberDispositionReason.QUALIFICATION_UNAVAILABLE
        elif not catalog.current_provider_eligible:
            reason = MemberDispositionReason.PROVIDER_ELIGIBILITY_UNAVAILABLE
        else:
            disposition = MemberDisposition.INCLUDED
            reason = MemberDispositionReason.INCLUDED_BY_POLICY
    return CohortSummaryRow(
        ordinal=ordinal,
        member_sha256=status.member_sha256,
        lineage_role=member.lineage_role,
        denominator_contribution=member.denominator_contribution,
        disposition=disposition,
        reason=reason,
        catalog_result_sha256=(
            _sha256_exact(catalog, CatalogResultRef) if catalog is not None else None
        ),
        result_id=catalog.result_id if catalog is not None else None,
        result_source_sha256=None,
        denominator_ledger_sha256=None,
    )


def _capture_registered_inputs(
    registry: CohortRegistry,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
) -> tuple[CohortDenominatorPolicy, CohortDispositionPolicy]:
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
    try:
        policy_content = _exact_bytes(
            policy, CohortDenominatorPolicy, max_bytes=64 * 1024
        )
        replayed_policy = CohortDenominatorPolicy.model_validate_json(policy_content)
        replayed_disposition_policy = _replay_disposition_policy(disposition_policy)
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    return replayed_policy, replayed_disposition_policy


def _derive_registered_in_fence(
    *,
    history: RegisteredCohortHistory,
    status: CohortManifestRecordStatus,
    selector_id: str,
    cohort_version: int,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
) -> tuple[RegisteredCohortDenominatorSummary, CohortDenominatorSummary, CohortManifest]:
    """Derive the protected rows and their aggregate v3 summary in one fence.

    The registered v3 summary and the protected row-bearing population both
    come from this single derivation, so they cannot disagree.
    """

    manifest = _validate_record_status(history, status)
    _validate_policy_binds_manifest(policy, manifest)
    _validate_disposition_policy(
        policy=policy,
        manifest=manifest,
        disposition_policy=disposition_policy,
    )
    rows = tuple(
        _registered_row(
            ordinal=ordinal,
            member=member,
            status=member_status,
            disposition_policy=disposition_policy,
        )
        for ordinal, (member, member_status) in enumerate(
            zip(manifest.members, status.members, strict=True)
        )
    )
    population = _summary_from_rows(manifest=manifest, policy=policy, rows=rows)
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
        "population": _project_population(population),
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
        summary_sha256=_sha256_exact(placeholder, RegisteredCohortDenominatorSummary),
    )
    registered = RegisteredCohortDenominatorSummary.model_validate_json(
        registered_cohort_denominator_summary_bytes(result)
    )
    return registered, population, manifest


def _project_population(
    population: CohortDenominatorSummary,
) -> CohortPopulationProjection:
    return CohortPopulationProjection(
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


def build_registered_cohort_denominator_summary(
    *,
    registry: CohortRegistry,
    selector_id: str,
    cohort_version: int,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
) -> RegisteredCohortDenominatorSummary:
    """Derive one D09 population from exact live D05/D06 authority state only.

    No caller-authored result, compatibility, ledger, or comparison evidence
    is accepted: every disposition comes from the fenced D05 manifest and
    policy and the fenced D06 record status.
    """

    replayed_policy, replayed_disposition_policy = _capture_registered_inputs(
        registry, record_catalog, policy, disposition_policy
    )
    with _PINNED_RECORD_STATUS_FENCE(
        record_catalog,
        selector_id,
        cohort_version,
        expected_registry=registry,
    ) as (history, status):
        registered, _, _ = _derive_registered_in_fence(
            history=history,
            status=status,
            selector_id=selector_id,
            cohort_version=cohort_version,
            policy=replayed_policy,
            disposition_policy=replayed_disposition_policy,
        )
        return registered


class RegisteredIncludedMember(RegistryContract):
    """Protected identity of one D09-included member; never an export field."""

    ordinal: int = Field(ge=0, le=MAX_COHORT_SUMMARY_MEMBERS, strict=True)
    member: CohortMember
    catalog_result: CatalogResultRef


class RegisteredCohortPopulationMembers(RegistryContract):
    """Protected per-member derivation behind one registered v3 summary.

    The v3 summary's ``population_sha256`` already commits to the row-bearing
    ``CohortDenominatorSummary``.  This contract carries those rows plus the
    exact D05 member and D06 catalog reference of every included row, all from
    the same fenced derivation.  It is protected and local only and is never
    projected into the aggregate v3 summary, selector rows, or export bytes.
    """

    schema_version: Literal["traceback.registered-cohort-population-members.v1"] = (
        "traceback.registered-cohort-population-members.v1"
    )
    summary: RegisteredCohortDenominatorSummary
    population: CohortDenominatorSummary
    included_members: tuple[RegisteredIncludedMember, ...] = Field(
        max_length=MAX_COHORT_SUMMARY_MEMBERS
    )
    protected_local_only: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False
    scientific_qualification_claimed: Literal[False] = False

    @model_validator(mode="after")
    def exact_derivation(self) -> RegisteredCohortPopulationMembers:
        population = self.population
        if (
            population.population_sha256
            != self.summary.population.population_sha256
            or _project_population(population) != self.summary.population
        ):
            raise ValueError("protected population does not match the v3 summary")
        included_rows = tuple(
            row
            for row in population.rows
            if row.disposition is MemberDisposition.INCLUDED
        )
        if len(included_rows) != len(self.included_members):
            raise ValueError("protected included members do not match the rows")
        for row, item in zip(included_rows, self.included_members, strict=True):
            if (
                item.ordinal != row.ordinal
                or row.catalog_result_sha256 is None
                or item.catalog_result.result_id != row.result_id
                or _sha256_exact(item.member, CohortMember) != row.member_sha256
                or _sha256_exact(item.catalog_result, CatalogResultRef)
                != row.catalog_result_sha256
            ):
                raise ValueError("protected included member does not bind its row")
        return self


MAX_POPULATION_MEMBERS_BYTES = 64 * 1024 * 1024
MAX_POPULATION_MEMBERS_NODES = 8_000_000
_POPULATION_MEMBERS_MODEL_TYPES, _POPULATION_MEMBERS_ENUM_TYPES = (
    contract_type_graph(RegisteredCohortPopulationMembers)
)


def registered_cohort_population_members_bytes(
    value: RegisteredCohortPopulationMembers,
) -> bytes:
    """Exact canonical bytes of one protected population; never export bytes."""

    return exact_model_bytes(
        value,
        RegisteredCohortPopulationMembers,
        model_types=_POPULATION_MEMBERS_MODEL_TYPES,
        enum_types=_POPULATION_MEMBERS_ENUM_TYPES,
        max_bytes=MAX_POPULATION_MEMBERS_BYTES,
        max_nodes=MAX_POPULATION_MEMBERS_NODES,
        max_depth=MAX_COHORT_INPUT_DEPTH,
        max_collection_items=MAX_COHORT_SUMMARY_MEMBERS,
        max_string_bytes=4_096,
    )


def build_registered_cohort_population_members(
    *,
    registry: CohortRegistry,
    selector_id: str,
    cohort_version: int,
    record_catalog: CohortRecordCatalog,
    policy: CohortDenominatorPolicy,
    disposition_policy: CohortDispositionPolicy,
) -> RegisteredCohortPopulationMembers:
    """Derive the registered v3 summary together with its protected members.

    Same inputs, fence, and derivation as
    ``build_registered_cohort_denominator_summary``.  It also returns the
    included rows' exact D05 members and D06 catalog references captured in
    that fence.  Protected and local only.
    """

    replayed_policy, replayed_disposition_policy = _capture_registered_inputs(
        registry, record_catalog, policy, disposition_policy
    )
    with _PINNED_RECORD_STATUS_FENCE(
        record_catalog,
        selector_id,
        cohort_version,
        expected_registry=registry,
    ) as (history, status):
        registered, population, manifest = _derive_registered_in_fence(
            history=history,
            status=status,
            selector_id=selector_id,
            cohort_version=cohort_version,
            policy=replayed_policy,
            disposition_policy=replayed_disposition_policy,
        )
        included: list[RegisteredIncludedMember] = []
        for row in population.rows:
            if row.disposition is not MemberDisposition.INCLUDED:
                continue
            binding = status.members[row.ordinal].binding
            if binding is None:
                raise ValueError("included cohort member has no exact binding")
            included.append(
                RegisteredIncludedMember(
                    ordinal=row.ordinal,
                    member=manifest.members[row.ordinal],
                    catalog_result=binding.result,
                )
            )
        result = RegisteredCohortPopulationMembers(
            summary=registered,
            population=population,
            included_members=tuple(included),
        )
        return RegisteredCohortPopulationMembers.model_validate_json(
            registered_cohort_population_members_bytes(result)
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


_DERIVED_EXCLUSION_REASONS = frozenset(
    {
        MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED,
        MemberDispositionReason.REANALYSIS_COLLAPSED,
        MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY,
        MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY,
    }
)


def _policy_required_reason(
    *,
    member: CohortMember,
    member_sha256: str,
    disposition_policy: CohortDispositionPolicy,
) -> MemberDispositionReason | None:
    """Return the exclusion that lineage or policy mandates, in fixed precedence."""

    if member.lineage_role is MemberLineageRole.TECHNICAL_REPLICATE:
        return MemberDispositionReason.TECHNICAL_REPLICATE_COLLAPSED
    if member.lineage_role is MemberLineageRole.REANALYSIS:
        return MemberDispositionReason.REANALYSIS_COLLAPSED
    if member_sha256 in disposition_policy.inclusion.member_sha256s:
        return MemberDispositionReason.EXCLUDED_BY_INCLUSION_POLICY
    if member_sha256 in disposition_policy.exclusion.member_sha256s:
        return MemberDispositionReason.EXCLUDED_BY_EXCLUSION_POLICY
    return None


def _validate_policy_binds_manifest(
    policy: CohortDenominatorPolicy, manifest: CohortManifest
) -> None:
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


def _summary_from_rows(
    *,
    manifest: CohortManifest,
    policy: CohortDenominatorPolicy,
    rows: tuple[CohortSummaryRow, ...],
) -> CohortDenominatorSummary:
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
        "rows": rows,
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
    disposition_policy: CohortDispositionPolicy,
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
        disposition_policy = _replay_disposition_policy(disposition_policy)
        evidence = tuple(_replay_evidence(item) for item in evidence)
    except (TypeError, ValueError):
        raise ValueError("cohort summary input is not canonical") from None
    _validate_policy_binds_manifest(policy, manifest)
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
        required_reason = _policy_required_reason(
            member=member,
            member_sha256=member_sha256,
            disposition_policy=disposition_policy,
        )
        if required_reason is not None:
            # Collapsed lineage and policy selections are derived, never chosen:
            # a selected member cannot be reported included or unavailable.
            if item.reason is not required_reason:
                raise ValueError("member disposition contradicts the exact policy")
        elif item.reason in _DERIVED_EXCLUSION_REASONS:
            raise ValueError("exclusion reason is not supported by the exact policy")
        if item.catalog_result is not None and item.result_source is not None:
            _validate_catalog_binding(item.catalog_result, item.result_source)
        if item.disposition == MemberDisposition.INCLUDED:
            _validate_included(item)
        elif item.disposition == MemberDisposition.UNAVAILABLE:
            if not _unavailability_reason_matches(item):
                raise ValueError(
                    "unavailable reason is not supported by exact evidence"
                )
        rows.append(_row(ordinal=ordinal, member=member, evidence=item))

    return _summary_from_rows(manifest=manifest, policy=policy, rows=tuple(rows))


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
    "RegisteredCohortPopulationMembers",
    "RegisteredIncludedMember",
    "build_cohort_denominator_summary",
    "build_registered_cohort_denominator_summary",
    "build_registered_cohort_population_members",
    "cohort_denominator_policy_sha256",
    "cohort_member_exclusion_set_sha256",
    "cohort_denominator_summary_bytes",
    "cohort_denominator_summary_from_bytes",
    "registered_cohort_denominator_summary_bytes",
    "registered_cohort_denominator_summary_from_bytes",
    "registered_cohort_population_members_bytes",
]
