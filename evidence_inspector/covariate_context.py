"""Synthetic/local D10 covariate context over a future D09 population digest.

This module groups opaque batch, protocol, and preanalytical metadata. It does
not alter measurements, establish comparability, or attribute biological or
clinical meaning. The D09 boundary is digest-only until its live registry
contract is finalized. D03 decisions are either caller-supplied (marked
unverified) or resolved from the protected D03 decision registry, which replays
them against live linkage before D10 uses them.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, TypeVar

from pydantic import Field, StringConstraints, model_validator

from evidence_inspector.longitudinal_compatibility import (
    LongitudinalMemberDecision,
    LongitudinalOutcome,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    RegisteredLongitudinalSeriesDecision,
)
from evidence_inspector.method_registry import RegistryContract, Sha256
from evidence_inspector.safe_ingress import contract_type_graph, exact_model_bytes

MAX_COVARIATE_MEMBERS = 1_000
MAX_GRAPH_DEPTH = 32
MAX_GRAPH_NODES_PER_MEMBER = 64
MAX_GRAPH_NODES = 1_032 + MAX_GRAPH_NODES_PER_MEMBER * MAX_COVARIATE_MEMBERS
MAX_CONTRACT_BYTES_PER_MEMBER = 4 * 1_024
MAX_CONTRACT_BYTES = (
    1 * 1_024 * 1_024 + MAX_CONTRACT_BYTES_PER_MEMBER * MAX_COVARIATE_MEMBERS
)
MAX_SCALAR_LENGTH = 4_096
MAX_TUPLE_LENGTH = MAX_COVARIATE_MEMBERS
MAX_D03_DECISION_DEPTH = 64
MAX_D03_DECISION_NODES = 10_000
MAX_D03_DECISION_BYTES = 4 * 1_024 * 1_024
MAX_D03_COLLECTION_ITEMS = 1_000
MAX_D03_DECISIONS_TOTAL_BYTES = 16 * 1_024 * 1_024

OpaqueCovariateToken = Annotated[
    str, StringConstraints(pattern=r"^covariate_[0-9a-f]{32}$")
]
CovariateGroupId = Annotated[
    str, StringConstraints(pattern=r"^covariate_group_[0-9a-f]{32}$")
]


class CovariateDimension(StrEnum):
    BATCH = "batch"
    PROTOCOL = "protocol"
    PREANALYTICS = "preanalytics"


ALL_COVARIATE_DIMENSIONS = tuple(CovariateDimension)


class CovariateValueState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class CovariateClassification(StrEnum):
    CLEAR = "clear"
    ALIASED = "aliased"
    MISSING_METADATA = "missing_metadata"
    MIXED = "mixed"


class CovariateReason(StrEnum):
    COMPLETE_CONSTANT_CONTEXT = "complete_constant_context"
    COVARIATE_VARIATION_PRESENT = "covariate_variation_present"
    PROTOCOL_TIMEPOINT_ALIASED = "protocol_timepoint_aliased"
    NO_INCLUDED_MEMBERS = "no_included_members"
    BATCH_METADATA_MISSING = "batch_metadata_missing"
    PROTOCOL_METADATA_MISSING = "protocol_metadata_missing"
    PREANALYTICS_METADATA_MISSING = "preanalytics_metadata_missing"


class D09PopulationDigestInput(RegistryContract):
    """Versioned adapter for D09 identities, not proof of live D09 authority."""

    schema_version: Literal["traceback.d09-population-digest-input.v1"] = (
        "traceback.d09-population-digest-input.v1"
    )
    cohort_manifest_sha256: Sha256
    d09_status_sha256: Sha256
    d09_population_sha256: Sha256
    d02_anchor_policy_sha256: Sha256
    included_member_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )
    adapter_mode: Literal["declared_digest_only"] = "declared_digest_only"
    live_d09_registry_verified: Literal[False] = False
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def canonical_members(self) -> D09PopulationDigestInput:
        if self.included_member_sha256s != tuple(
            sorted(set(self.included_member_sha256s))
        ):
            raise ValueError("included member digests must be uniquely sorted")
        return self


class CovariateValue(RegistryContract):
    dimension: CovariateDimension
    state: CovariateValueState
    token: OpaqueCovariateToken | None

    @model_validator(mode="after")
    def known_or_unknown(self) -> CovariateValue:
        if (self.state is CovariateValueState.KNOWN) != (self.token is not None):
            raise ValueError("known covariates require a token; unknowns forbid one")
        return self


class MemberCovariateContext(RegistryContract):
    member_sha256: Sha256
    biological_timepoint_sha256: Sha256
    d03_decision_sha256: Sha256
    d03_outcome: LongitudinalOutcome
    values: tuple[CovariateValue, ...] = Field(
        min_length=len(ALL_COVARIATE_DIMENSIONS),
        max_length=len(ALL_COVARIATE_DIMENSIONS),
    )

    @model_validator(mode="after")
    def complete_dimensions(self) -> MemberCovariateContext:
        if tuple(value.dimension for value in self.values) != ALL_COVARIATE_DIMENSIONS:
            raise ValueError("member covariates must contain every dimension in order")
        return self


class D10CovariateInput(RegistryContract):
    schema_version: Literal["traceback.d10-covariate-input.v1"] = (
        "traceback.d10-covariate-input.v1"
    )
    population: D09PopulationDigestInput
    members: tuple[MemberCovariateContext, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )

    @model_validator(mode="after")
    def exact_population(self) -> D10CovariateInput:
        member_digests = tuple(member.member_sha256 for member in self.members)
        if len(member_digests) != len(set(member_digests)):
            raise ValueError("covariate members must be unique")
        decision_digests = tuple(member.d03_decision_sha256 for member in self.members)
        if len(decision_digests) != len(set(decision_digests)):
            raise ValueError("each member must bind a unique D03 decision digest")
        if tuple(sorted(member_digests)) != self.population.included_member_sha256s:
            raise ValueError("covariates must bind every and only included member")
        token_dimensions: dict[str, CovariateDimension] = {}
        decision_outcomes: dict[str, LongitudinalOutcome] = {}
        for member in self.members:
            prior_outcome = decision_outcomes.setdefault(
                member.d03_decision_sha256, member.d03_outcome
            )
            if prior_outcome is not member.d03_outcome:
                raise ValueError("one D03 decision digest cannot claim two outcomes")
            for value in member.values:
                if value.token is None:
                    continue
                prior = token_dimensions.setdefault(value.token, value.dimension)
                if prior is not value.dimension:
                    raise ValueError(
                        "one covariate token cannot identify two dimensions"
                    )
        return self


class CovariateGroup(RegistryContract):
    group_id: CovariateGroupId
    values: tuple[CovariateValue, ...] = Field(
        min_length=len(ALL_COVARIATE_DIMENSIONS),
        max_length=len(ALL_COVARIATE_DIMENSIONS),
    )
    member_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_COVARIATE_MEMBERS
    )

    @model_validator(mode="after")
    def canonical_group(self) -> CovariateGroup:
        if tuple(value.dimension for value in self.values) != ALL_COVARIATE_DIMENSIONS:
            raise ValueError("covariate group dimensions are invalid")
        if self.member_sha256s != tuple(sorted(set(self.member_sha256s))):
            raise ValueError("covariate group members must be uniquely sorted")
        if self.group_id != _group_id(self.values):
            raise ValueError("covariate group identity is invalid")
        return self


class CovariateContextResult(RegistryContract):
    schema_version: Literal["traceback.covariate-context-result.v1"] = (
        "traceback.covariate-context-result.v1"
    )
    input_sha256: Sha256
    cohort_manifest_sha256: Sha256
    d09_status_sha256: Sha256
    d09_population_sha256: Sha256
    d02_anchor_policy_sha256: Sha256
    included_member_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )
    member_contexts: tuple[MemberCovariateContext, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )
    member_context_sha256s: tuple[Sha256, ...] = Field(max_length=MAX_COVARIATE_MEMBERS)
    groups: tuple[CovariateGroup, ...] = Field(max_length=MAX_COVARIATE_MEMBERS)
    classification: CovariateClassification
    reason_codes: tuple[CovariateReason, ...] = Field(min_length=1, max_length=3)
    live_d09_registry_verified: Literal[False] = False
    d03_authority_verified: Literal[False] = False
    comparison_eligibility_changed: Literal[False] = False
    measurement_values_changed: Literal[False] = False
    silent_correction_applied: Literal[False] = False
    biological_attribution_allowed: Literal[False] = False
    clinical_interpretation_allowed: Literal[False] = False
    synthetic_only: Literal[True] = True
    protected_local_only: Literal[True] = True

    @model_validator(mode="after")
    def exact_result(self) -> CovariateContextResult:
        if self.included_member_sha256s != tuple(
            sorted(set(self.included_member_sha256s))
        ):
            raise ValueError("result members must be uniquely sorted")
        if len(self.member_context_sha256s) != len(self.included_member_sha256s):
            raise ValueError("result must bind every member context")
        if tuple(item.member_sha256 for item in self.member_contexts) != (
            self.included_member_sha256s
        ):
            raise ValueError(
                "result member contexts must use canonical population order"
            )
        if self.member_context_sha256s != tuple(
            member_covariate_context_sha256(item) for item in self.member_contexts
        ):
            raise ValueError("result member-context digests are invalid")
        if tuple(group.group_id for group in self.groups) != tuple(
            sorted(group.group_id for group in self.groups)
        ):
            raise ValueError("covariate groups must use canonical order")
        covered = tuple(
            sorted(member for group in self.groups for member in group.member_sha256s)
        )
        if covered != self.included_member_sha256s:
            raise ValueError("covariate groups must cover every included member once")
        if self.groups != _derive_groups(self.member_contexts):
            raise ValueError("covariate groups do not match exact member contexts")
        reconstructed_input = D10CovariateInput(
            population=D09PopulationDigestInput(
                cohort_manifest_sha256=self.cohort_manifest_sha256,
                d09_status_sha256=self.d09_status_sha256,
                d09_population_sha256=self.d09_population_sha256,
                d02_anchor_policy_sha256=self.d02_anchor_policy_sha256,
                included_member_sha256s=self.included_member_sha256s,
            ),
            members=self.member_contexts,
        )
        if self.input_sha256 != d10_covariate_input_sha256(reconstructed_input):
            raise ValueError("covariate result input identity is invalid")
        derived_classification, derived_reasons = _classify_members(
            self.member_contexts, self.groups
        )
        if (
            self.classification is not derived_classification
            or self.reason_codes != derived_reasons
        ):
            raise ValueError("covariate classification does not match member contexts")
        exact_reason = {
            CovariateClassification.CLEAR: (CovariateReason.COMPLETE_CONSTANT_CONTEXT,),
            CovariateClassification.ALIASED: (
                CovariateReason.PROTOCOL_TIMEPOINT_ALIASED,
            ),
            CovariateClassification.MIXED: (
                CovariateReason.COVARIATE_VARIATION_PRESENT,
            ),
        }.get(self.classification)
        if exact_reason is not None and self.reason_codes != exact_reason:
            raise ValueError("covariate classification reasons are inconsistent")
        if self.classification is CovariateClassification.MISSING_METADATA:
            missing_reasons = {
                CovariateReason.BATCH_METADATA_MISSING,
                CovariateReason.PROTOCOL_METADATA_MISSING,
                CovariateReason.PREANALYTICS_METADATA_MISSING,
            }
            if not self.included_member_sha256s:
                if self.reason_codes != (CovariateReason.NO_INCLUDED_MEMBERS,):
                    raise ValueError("empty population reason is inconsistent")
            else:
                expected_missing = tuple(
                    reason
                    for reason in (
                        CovariateReason.BATCH_METADATA_MISSING,
                        CovariateReason.PROTOCOL_METADATA_MISSING,
                        CovariateReason.PREANALYTICS_METADATA_MISSING,
                    )
                    if reason in set(self.reason_codes)
                )
                if (
                    not set(self.reason_codes) <= missing_reasons
                    or self.reason_codes != expected_missing
                ):
                    raise ValueError("missing-metadata reasons are inconsistent")
        return self


class AggregateCovariateGroup(RegistryContract):
    group_index: int = Field(ge=0, le=MAX_COVARIATE_MEMBERS - 1, strict=True)
    states: tuple[CovariateValueState, ...] = Field(
        min_length=len(ALL_COVARIATE_DIMENSIONS),
        max_length=len(ALL_COVARIATE_DIMENSIONS),
    )
    member_count: int = Field(ge=1, le=MAX_COVARIATE_MEMBERS, strict=True)


class AggregateCovariateSummary(RegistryContract):
    """Aggregate projection with no member, timepoint, or covariate token."""

    schema_version: Literal["traceback.aggregate-covariate-summary.v1"] = (
        "traceback.aggregate-covariate-summary.v1"
    )
    protected_context_sha256: Sha256
    classification: CovariateClassification
    reason_codes: tuple[CovariateReason, ...] = Field(min_length=1, max_length=3)
    included_member_count: int = Field(ge=0, le=MAX_COVARIATE_MEMBERS, strict=True)
    groups: tuple[AggregateCovariateGroup, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )
    d03_authority_verified: Literal[False] = False
    measurement_values_changed: Literal[False] = False
    silent_correction_applied: Literal[False] = False
    biological_attribution_allowed: Literal[False] = False
    clinical_interpretation_allowed: Literal[False] = False
    synthetic_only: Literal[True] = True

    @model_validator(mode="after")
    def exact_aggregate(self) -> AggregateCovariateSummary:
        if tuple(group.group_index for group in self.groups) != tuple(
            range(len(self.groups))
        ):
            raise ValueError("aggregate covariate groups must use canonical indexes")
        if (
            sum(group.member_count for group in self.groups)
            != self.included_member_count
        ):
            raise ValueError("aggregate covariate group counts must reconcile")
        if self.included_member_count == 0 and self.groups:
            raise ValueError("empty aggregate population cannot contain groups")
        missing_reasons = tuple(
            reason
            for index, reason in enumerate(
                (
                    CovariateReason.BATCH_METADATA_MISSING,
                    CovariateReason.PROTOCOL_METADATA_MISSING,
                    CovariateReason.PREANALYTICS_METADATA_MISSING,
                )
            )
            if any(
                group.states[index] is CovariateValueState.UNKNOWN
                for group in self.groups
            )
        )
        if self.included_member_count == 0:
            expected_classification = CovariateClassification.MISSING_METADATA
            expected_reasons = (CovariateReason.NO_INCLUDED_MEMBERS,)
        elif missing_reasons:
            expected_classification = CovariateClassification.MISSING_METADATA
            expected_reasons = missing_reasons
        else:
            expected_classification = self.classification
            expected_reasons = {
                CovariateClassification.CLEAR: (
                    CovariateReason.COMPLETE_CONSTANT_CONTEXT,
                ),
                CovariateClassification.ALIASED: (
                    CovariateReason.PROTOCOL_TIMEPOINT_ALIASED,
                ),
                CovariateClassification.MIXED: (
                    CovariateReason.COVARIATE_VARIATION_PRESENT,
                ),
            }.get(self.classification, ())
            if (
                self.classification is CovariateClassification.CLEAR
                and len(self.groups) != 1
            ):
                raise ValueError("clear aggregate context must contain one group")
            if (
                self.classification
                in {
                    CovariateClassification.ALIASED,
                    CovariateClassification.MIXED,
                }
                and len(self.groups) < 2
            ):
                raise ValueError(
                    "varying aggregate context must contain multiple groups"
                )
        if (
            self.classification is not expected_classification
            or self.reason_codes != expected_reasons
        ):
            raise ValueError("aggregate classification reasons are inconsistent")
        return self


class RegisteredD03SeriesBinding(RegistryContract):
    """Exact D03 registry identity a registered D10 context was derived from."""

    schema_version: Literal["traceback.d10-d03-series-binding.v1"] = (
        "traceback.d10-d03-series-binding.v1"
    )
    registry_id: str = Field(pattern=r"^d03_registry_[0-9a-f]{32}$")
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=10_000, strict=True)
    state_head_sha256: Sha256
    selector_id: str = Field(pattern=r"^d03_series_[0-9a-f]{40}$")
    object_sha256: Sha256
    series_decision_sha256: Sha256
    linkage_snapshot_state_version: int | None = Field(ge=0, le=10_000_000)
    linkage_snapshot_state_head_sha256: Sha256 | None
    linkage_snapshot_sha256: Sha256 | None


class RegisteredCovariateContext(RegistryContract):
    """Protected D10 context whose D03 decisions came from a live registry replay.

    The binding is as-of one linkage snapshot. A consumer re-checks it with
    ``verify_registered_covariate_context`` before relying on it; a stored copy
    is never authority by itself.
    """

    schema_version: Literal["traceback.registered-covariate-context.v1"] = (
        "traceback.registered-covariate-context.v1"
    )
    context: CovariateContextResult
    d03_series: RegisteredD03SeriesBinding
    d03_authority_verified: Literal[True] = True
    live_d09_registry_verified: Literal[False] = False
    synthetic_only: Literal[True] = True
    protected_local_only: Literal[True] = True


ContractT = TypeVar("ContractT", bound=RegistryContract)
_TRUSTED_TYPES, _TRUSTED_ENUMS = contract_type_graph(
    D10CovariateInput,
    CovariateContextResult,
    AggregateCovariateSummary,
)
_REGISTERED_TYPES, _REGISTERED_ENUMS = contract_type_graph(RegisteredCovariateContext)
# The registered wrapper adds one fixed binding around one protected result.
_REGISTERED_EXTRA_NODES = 64
_REGISTERED_EXTRA_BYTES = 4 * 1_024
_D03_TRUSTED_TYPES, _D03_TRUSTED_ENUMS = contract_type_graph(LongitudinalMemberDecision)
_CODECS = MappingProxyType(
    {
        model: (model.__pydantic_serializer__, model.__pydantic_validator__)
        for model in _TRUSTED_TYPES
    }
)


def _exact_bytes(
    value: object, expected_type: type[object], serializer: object
) -> bytes:
    try:
        del serializer
        return exact_model_bytes(
            value,
            expected_type,  # type: ignore[arg-type]
            model_types=_TRUSTED_TYPES,
            enum_types=_TRUSTED_ENUMS,
            max_bytes=MAX_CONTRACT_BYTES,
            max_nodes=MAX_GRAPH_NODES,
            max_depth=MAX_GRAPH_DEPTH,
            max_collection_items=MAX_TUPLE_LENGTH,
            max_string_bytes=MAX_SCALAR_LENGTH,
            max_int_bits=64,
        )
    except (TypeError, ValueError):
        raise TypeError("covariate contract object graph is invalid") from None


def _replay(model: type[ContractT], value: object) -> ContractT:
    serializer, validator = _CODECS[model]
    encoded = _exact_bytes(value, model, serializer)
    replayed = validator.validate_json(encoded)
    if (
        type(replayed) is not model
        or _exact_bytes(replayed, model, serializer) != encoded
    ):
        raise ValueError("covariate contract does not replay canonically")
    return replayed


def _sha256(model: type[ContractT], value: object) -> str:
    serializer = _CODECS[model][0]
    replayed = _replay(model, value)
    return hashlib.sha256(_exact_bytes(replayed, model, serializer)).hexdigest()


def d09_population_digest_input_sha256(value: D09PopulationDigestInput) -> str:
    return _sha256(D09PopulationDigestInput, value)


def member_covariate_context_sha256(value: MemberCovariateContext) -> str:
    return _sha256(MemberCovariateContext, value)


def d10_covariate_input_sha256(value: D10CovariateInput) -> str:
    replayed = _canonical_d10_input(value)
    serializer = _CODECS[D10CovariateInput][0]
    return hashlib.sha256(
        _exact_bytes(replayed, D10CovariateInput, serializer)
    ).hexdigest()


def covariate_context_result_sha256(value: CovariateContextResult) -> str:
    return _sha256(CovariateContextResult, value)


def aggregate_covariate_summary_sha256(
    value: AggregateCovariateSummary,
    *,
    protected_result: CovariateContextResult,
) -> str:
    return hashlib.sha256(
        aggregate_covariate_summary_bytes(value, protected_result=protected_result)
    ).hexdigest()


def aggregate_covariate_summary_bytes(
    value: AggregateCovariateSummary,
    *,
    protected_result: CovariateContextResult,
) -> bytes:
    replayed = _replay(AggregateCovariateSummary, value)
    expected = project_aggregate_covariate_summary(protected_result)
    if replayed != expected:
        raise ValueError("aggregate summary does not match protected context")
    return _exact_bytes(
        replayed,
        AggregateCovariateSummary,
        _CODECS[AggregateCovariateSummary][0],
    )


def project_aggregate_covariate_summary(
    value: CovariateContextResult,
) -> AggregateCovariateSummary:
    """Remove protected row identities and tokens from one exact local result."""

    protected = _replay(CovariateContextResult, value)
    # Protected groups are ordered by token-derived IDs; reorder by exported
    # content only so the aggregate bytes reveal nothing about the tokens.
    exported = sorted(
        (
            tuple(item.state for item in group.values),
            len(group.member_sha256s),
        )
        for group in protected.groups
    )
    summary = AggregateCovariateSummary(
        protected_context_sha256=covariate_context_result_sha256(protected),
        classification=protected.classification,
        reason_codes=protected.reason_codes,
        included_member_count=len(protected.included_member_sha256s),
        groups=tuple(
            AggregateCovariateGroup(
                group_index=index, states=states, member_count=member_count
            )
            for index, (states, member_count) in enumerate(exported)
        ),
    )
    return _replay(AggregateCovariateSummary, summary)


def _group_id(values: tuple[CovariateValue, ...]) -> str:
    payload = tuple(
        (value.dimension.value, value.state.value, value.token) for value in values
    )
    digest = hashlib.sha256(
        b"traceback-covariate-group-v1\0"
        + json.dumps(payload, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return f"covariate_group_{digest[:32]}"


def _derive_groups(
    members: tuple[MemberCovariateContext, ...],
) -> tuple[CovariateGroup, ...]:
    grouped: dict[tuple[tuple[str, str, str | None], ...], list[str]] = {}
    for member in members:
        key = tuple(
            (item.dimension.value, item.state.value, item.token)
            for item in member.values
        )
        grouped.setdefault(key, []).append(member.member_sha256)
    groups: list[CovariateGroup] = []
    for key, member_digests in grouped.items():
        values = tuple(
            CovariateValue(
                dimension=CovariateDimension(dimension),
                state=CovariateValueState(state),
                token=token,
            )
            for dimension, state, token in key
        )
        groups.append(
            CovariateGroup(
                group_id=_group_id(values),
                values=values,
                member_sha256s=tuple(sorted(member_digests)),
            )
        )
    return tuple(sorted(groups, key=lambda group: group.group_id))


def _protocol_is_timepoint_aliased(members: tuple[MemberCovariateContext, ...]) -> bool:
    if len(members) < 2:
        return False
    pairs: set[tuple[str, str]] = set()
    for member in members:
        protocol = member.values[1]
        if protocol.state is not CovariateValueState.KNOWN or protocol.token is None:
            return False
        pairs.add((member.biological_timepoint_sha256, protocol.token))
    timepoints = {timepoint for timepoint, _ in pairs}
    protocols = {protocol for _, protocol in pairs}
    return len(timepoints) > 1 and len(timepoints) == len(protocols) == len(pairs)


def _classify_members(
    members: tuple[MemberCovariateContext, ...],
    groups: tuple[CovariateGroup, ...],
) -> tuple[CovariateClassification, tuple[CovariateReason, ...]]:
    if not members:
        return (
            CovariateClassification.MISSING_METADATA,
            (CovariateReason.NO_INCLUDED_MEMBERS,),
        )
    missing = {
        item.dimension
        for member in members
        for item in member.values
        if item.state is CovariateValueState.UNKNOWN
    }
    if missing:
        missing_reasons = {
            CovariateDimension.BATCH: CovariateReason.BATCH_METADATA_MISSING,
            CovariateDimension.PROTOCOL: CovariateReason.PROTOCOL_METADATA_MISSING,
            CovariateDimension.PREANALYTICS: (
                CovariateReason.PREANALYTICS_METADATA_MISSING
            ),
        }
        return (
            CovariateClassification.MISSING_METADATA,
            tuple(
                missing_reasons[dimension]
                for dimension in ALL_COVARIATE_DIMENSIONS
                if dimension in missing
            ),
        )
    if _protocol_is_timepoint_aliased(members):
        return (
            CovariateClassification.ALIASED,
            (CovariateReason.PROTOCOL_TIMEPOINT_ALIASED,),
        )
    if len(groups) == 1:
        return (
            CovariateClassification.CLEAR,
            (CovariateReason.COMPLETE_CONSTANT_CONTEXT,),
        )
    return (
        CovariateClassification.MIXED,
        (CovariateReason.COVARIATE_VARIATION_PRESENT,),
    )


def _canonical_d10_input(value: object) -> D10CovariateInput:
    replayed = _replay(D10CovariateInput, value)
    canonical_members = tuple(
        sorted(replayed.members, key=lambda item: item.member_sha256)
    )
    if replayed.members != canonical_members:
        replayed = _replay(
            D10CovariateInput,
            D10CovariateInput(
                population=replayed.population, members=canonical_members
            ),
        )
    return replayed


def _d03_decision_bytes(value: object) -> bytes:
    return exact_model_bytes(
        value,
        LongitudinalMemberDecision,
        model_types=_D03_TRUSTED_TYPES,
        enum_types=_D03_TRUSTED_ENUMS,
        max_bytes=MAX_D03_DECISION_BYTES,
        max_nodes=MAX_D03_DECISION_NODES,
        max_depth=MAX_D03_DECISION_DEPTH,
        max_collection_items=MAX_D03_COLLECTION_ITEMS,
        max_string_bytes=MAX_SCALAR_LENGTH,
        max_int_bits=64,
    )


def _d03_decision_sha256(value: object) -> str:
    return hashlib.sha256(_d03_decision_bytes(value)).hexdigest()


def _replay_d03_decision(value: object) -> LongitudinalMemberDecision:
    try:
        encoded = _d03_decision_bytes(value)
        replayed = LongitudinalMemberDecision.__pydantic_validator__.validate_json(
            encoded
        )
        if (
            type(replayed) is not LongitudinalMemberDecision
            or _d03_decision_bytes(replayed) != encoded
        ):
            raise ValueError("D03 member decision is not canonical")
        return replayed
    except (AttributeError, TypeError, ValueError):
        raise TypeError(
            "D03 member decision is not an exact canonical artifact"
        ) from None


def _capture_d03_decisions(
    decisions: tuple[LongitudinalMemberDecision, ...],
) -> tuple[LongitudinalMemberDecision, ...]:
    if type(decisions) is not tuple or len(decisions) > MAX_COVARIATE_MEMBERS:
        raise TypeError("D03 decisions must use one bounded exact tuple")
    captured: list[LongitudinalMemberDecision] = []
    total_bytes = 0
    for decision in decisions:
        replayed = _replay_d03_decision(decision)
        total_bytes += len(_d03_decision_bytes(replayed))
        if total_bytes > MAX_D03_DECISIONS_TOTAL_BYTES:
            raise TypeError("D03 decisions exceed the collection byte budget")
        captured.append(replayed)
    return tuple(captured)


def build_covariate_context(
    value: D10CovariateInput,
    *,
    expected_d09_status_sha256: str,
    expected_d09_population_sha256: str,
    expected_d02_anchor_policy_sha256: str,
    d03_member_decisions: tuple[LongitudinalMemberDecision, ...],
) -> CovariateContextResult:
    """Build descriptive context from exact pinned digest inputs."""

    replayed = _canonical_d10_input(value)
    decisions = _capture_d03_decisions(d03_member_decisions)
    decisions_by_member = {
        decision.member_result_sha256: decision for decision in decisions
    }
    if len(decisions_by_member) != len(decisions):
        raise ValueError("D03 decisions must bind unique members")
    decision_sha256s = tuple(_d03_decision_sha256(decision) for decision in decisions)
    if len(decision_sha256s) != len(set(decision_sha256s)):
        raise ValueError("D03 decision artifacts must be unique")
    population = replayed.population
    if set(decisions_by_member) != set(population.included_member_sha256s):
        raise ValueError("D03 decisions do not cover the exact population")
    if any(
        decision.policy_sha256 != population.d02_anchor_policy_sha256
        for decision in decisions
    ):
        raise ValueError("D03 decision policy does not match D02 anchor policy")
    if any(
        member.d03_decision_sha256
        != _d03_decision_sha256(decisions_by_member[member.member_sha256])
        or member.d03_outcome is not decisions_by_member[member.member_sha256].outcome
        for member in replayed.members
    ):
        raise ValueError("D03 member decision artifact does not match covariate input")
    for actual, expected, label in (
        (population.d09_status_sha256, expected_d09_status_sha256, "D09 status"),
        (
            population.d09_population_sha256,
            expected_d09_population_sha256,
            "D09 population",
        ),
        (
            population.d02_anchor_policy_sha256,
            expected_d02_anchor_policy_sha256,
            "D02 anchor policy",
        ),
    ):
        if type(expected) is not str or len(expected) != 64 or actual != expected:
            raise ValueError(f"{label} authority pin does not match")

    groups = _derive_groups(replayed.members)
    classification, reasons = _classify_members(replayed.members, groups)

    result = CovariateContextResult(
        input_sha256=d10_covariate_input_sha256(replayed),
        cohort_manifest_sha256=population.cohort_manifest_sha256,
        d09_status_sha256=population.d09_status_sha256,
        d09_population_sha256=population.d09_population_sha256,
        d02_anchor_policy_sha256=population.d02_anchor_policy_sha256,
        included_member_sha256s=population.included_member_sha256s,
        member_contexts=replayed.members,
        member_context_sha256s=tuple(
            member_covariate_context_sha256(member) for member in replayed.members
        ),
        groups=groups,
        classification=classification,
        reason_codes=reasons,
    )
    return _replay(CovariateContextResult, result)


def _registered_bytes(value: object) -> bytes:
    try:
        return exact_model_bytes(
            value,
            RegisteredCovariateContext,
            model_types=_REGISTERED_TYPES,
            enum_types=_REGISTERED_ENUMS,
            max_bytes=MAX_CONTRACT_BYTES + _REGISTERED_EXTRA_BYTES,
            max_nodes=MAX_GRAPH_NODES + _REGISTERED_EXTRA_NODES,
            max_depth=MAX_GRAPH_DEPTH,
            max_collection_items=MAX_TUPLE_LENGTH,
            max_string_bytes=MAX_SCALAR_LENGTH,
            max_int_bits=64,
        )
    except (TypeError, ValueError):
        raise TypeError("registered covariate context object graph is invalid") from None


def registered_covariate_context_bytes(value: RegisteredCovariateContext) -> bytes:
    """Return exact canonical bytes for one protected registered D10 context."""

    encoded = _registered_bytes(value)
    replayed = RegisteredCovariateContext.model_validate_json(encoded)
    if _registered_bytes(replayed) != encoded:
        raise ValueError("registered covariate context does not replay canonically")
    return encoded


def _resolve_registered_series(
    decision_registry: LongitudinalDecisionRegistry, series_selector_id: str
) -> RegisteredLongitudinalSeriesDecision:
    if type(decision_registry) is not LongitudinalDecisionRegistry:
        raise TypeError("D10 requires the exact D03 decision registry type")
    # resolve replays the stored series against the live linkage store and raises
    # LongitudinalDecisionRegistryStale rather than return a stale decision.
    return decision_registry.resolve(series_selector_id)


def _population_decisions(
    series: RegisteredLongitudinalSeriesDecision, population: D09PopulationDigestInput
) -> tuple[LongitudinalMemberDecision, ...]:
    by_member = {
        decision.member_result_sha256: decision
        for decision in series.decision.decisions
    }
    missing = set(population.included_member_sha256s) - set(by_member)
    if missing:
        raise ValueError("D03 registry series does not cover the D10 population")
    return tuple(by_member[member] for member in population.included_member_sha256s)


def _series_binding(
    series: RegisteredLongitudinalSeriesDecision,
) -> RegisteredD03SeriesBinding:
    return RegisteredD03SeriesBinding(
        registry_id=series.registry_id,
        registry_epoch_sha256=series.registry_epoch_sha256,
        state_version=series.state_version,
        state_head_sha256=series.state_head_sha256,
        selector_id=series.selector_id,
        object_sha256=series.object_sha256,
        series_decision_sha256=series.decision_sha256,
        linkage_snapshot_state_version=series.decision.linkage_snapshot_state_version,
        linkage_snapshot_state_head_sha256=(
            series.decision.linkage_snapshot_state_head_sha256
        ),
        linkage_snapshot_sha256=series.decision.linkage_snapshot_sha256,
    )


def build_registered_covariate_context(
    value: D10CovariateInput,
    *,
    expected_d09_status_sha256: str,
    expected_d09_population_sha256: str,
    expected_d02_anchor_policy_sha256: str,
    decision_registry: LongitudinalDecisionRegistry,
    series_selector_id: str,
) -> RegisteredCovariateContext:
    """Build D10 context from D03 decisions resolved from the protected registry.

    The caller supplies covariate metadata and declares each member's decision
    digest and outcome, but the decisions themselves come only from a registry
    resolve that replayed against live linkage. Every declared digest and
    outcome must equal the registry's decision for that member.
    """

    replayed = _canonical_d10_input(value)
    series = _resolve_registered_series(decision_registry, series_selector_id)
    if series.decision.policy_sha256 != replayed.population.d02_anchor_policy_sha256:
        raise ValueError("D03 registry series policy does not match D02 anchor policy")
    context = build_covariate_context(
        replayed,
        expected_d09_status_sha256=expected_d09_status_sha256,
        expected_d09_population_sha256=expected_d09_population_sha256,
        expected_d02_anchor_policy_sha256=expected_d02_anchor_policy_sha256,
        d03_member_decisions=_population_decisions(series, replayed.population),
    )
    result = RegisteredCovariateContext(
        context=context, d03_series=_series_binding(series)
    )
    return RegisteredCovariateContext.model_validate_json(
        registered_covariate_context_bytes(result)
    )


def verify_registered_covariate_context(
    value: RegisteredCovariateContext,
    *,
    decision_registry: LongitudinalDecisionRegistry,
) -> RegisteredCovariateContext:
    """Re-resolve the bound series and rebuild the context; never trust it stored.

    Returns a freshly bound context. The registry state head may have advanced
    through unrelated registrations; the registry identity, selector, object,
    series decision, and rebuilt context must all be unchanged.
    """

    if type(value) is not RegisteredCovariateContext:
        raise TypeError("registered covariate context requires the exact type")
    captured = RegisteredCovariateContext.model_validate_json(
        registered_covariate_context_bytes(value)
    )
    binding = captured.d03_series
    series = _resolve_registered_series(decision_registry, binding.selector_id)
    fresh = _series_binding(series)
    if (
        fresh.registry_id,
        fresh.registry_epoch_sha256,
        fresh.object_sha256,
        fresh.series_decision_sha256,
        fresh.linkage_snapshot_sha256,
    ) != (
        binding.registry_id,
        binding.registry_epoch_sha256,
        binding.object_sha256,
        binding.series_decision_sha256,
        binding.linkage_snapshot_sha256,
    ):
        raise ValueError("registered covariate context D03 binding is not current")
    context = captured.context
    rebuilt = build_registered_covariate_context(
        D10CovariateInput(
            population=D09PopulationDigestInput(
                cohort_manifest_sha256=context.cohort_manifest_sha256,
                d09_status_sha256=context.d09_status_sha256,
                d09_population_sha256=context.d09_population_sha256,
                d02_anchor_policy_sha256=context.d02_anchor_policy_sha256,
                included_member_sha256s=context.included_member_sha256s,
            ),
            members=context.member_contexts,
        ),
        expected_d09_status_sha256=context.d09_status_sha256,
        expected_d09_population_sha256=context.d09_population_sha256,
        expected_d02_anchor_policy_sha256=context.d02_anchor_policy_sha256,
        decision_registry=decision_registry,
        series_selector_id=binding.selector_id,
    )
    if rebuilt.context != context:
        raise ValueError("registered covariate context does not rebuild exactly")
    return rebuilt


__all__ = [
    "ALL_COVARIATE_DIMENSIONS",
    "MAX_COVARIATE_MEMBERS",
    "AggregateCovariateGroup",
    "AggregateCovariateSummary",
    "CovariateClassification",
    "CovariateContextResult",
    "CovariateDimension",
    "CovariateGroup",
    "CovariateReason",
    "CovariateValue",
    "CovariateValueState",
    "D09PopulationDigestInput",
    "D10CovariateInput",
    "MemberCovariateContext",
    "RegisteredCovariateContext",
    "RegisteredD03SeriesBinding",
    "aggregate_covariate_summary_bytes",
    "aggregate_covariate_summary_sha256",
    "build_covariate_context",
    "build_registered_covariate_context",
    "covariate_context_result_sha256",
    "d09_population_digest_input_sha256",
    "d10_covariate_input_sha256",
    "member_covariate_context_sha256",
    "project_aggregate_covariate_summary",
    "registered_covariate_context_bytes",
    "verify_registered_covariate_context",
]
