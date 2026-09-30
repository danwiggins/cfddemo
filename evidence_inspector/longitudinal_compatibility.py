"""Pinned-anchor longitudinal compatibility contracts for Epic D and E12.

This additive D02 layer does not alter E05 pairwise compatibility. It binds one
complete longitudinal comparison key to an exact verified measurement record
and evaluates every member against one immutable anchor policy. Unknown or
invalid authority never permits deltas or connecting trends.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import unquote

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from evidence_inspector.compatibility import (
    BundleId,
    CompatibilityContract,
    ResultId,
    VerifiedMeasurementRecord,
    compatibility_key_sha256,
)
from evidence_inspector.method_registry import (
    MethodReference,
    QuantityId,
    RegistryContract,
    Sha256,
    UnitId,
    Version,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage import (
    AuthorizedLinkageRevision,
    LinkageRevision,
    ProviderNamespace,
    linkage_revision_sha256,
    provider_trust_snapshot_sha256,
)
from evidence_inspector.provider_linkage_store import (
    MAX_PROVIDER_TRUST_PINS,
    ActiveLinkageSnapshot,
    AuthorityTimeSource,
    CommittedLinkageReceipt,
    ProviderLinkageStore,
    ProviderLinkageStoreError,
    ProviderLinkageStoreUnsafe,
    committed_linkage_receipt_sha256,
    provider_linkage_store_time_source_is_pinned,
    require_provider_linkage_store_process_integrity,
)

_PINNED_VERIFY_CURRENT_RECEIPT = ProviderLinkageStore.verify_current_receipt
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_STORE_CALLABLES = {
    name: getattr(ProviderLinkageStore, name)
    for name in vars(ProviderLinkageStore)
    if callable(getattr(ProviderLinkageStore, name))
}
_PINNED_TIME_SOURCE_IDENTITY_CHECK = provider_linkage_store_time_source_is_pinned
_PINNED_PROCESS_INTEGRITY_CHECK = require_provider_linkage_store_process_integrity
_STORE_BOUNDARY_ERRORS = (
    ProviderLinkageStoreError,
    sqlite3.Error,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    AssertionError,
)
_RLOCK_TYPE = type(threading.RLock())
_SHA256_ADAPTER = TypeAdapter(Sha256)
_TRUST_PINS_ADAPTER = TypeAdapter(dict[ProviderNamespace, Sha256])

MAX_DIMENSIONS = 16
MAX_ALLOWANCES_PER_DIMENSION = 32
MAX_SERIES_MEMBERS = 1_000
MAX_DECISION_EVIDENCE = 32

_RESERVED_PRIVATE_TERMS = {
    "donor",
    "path",
    "patient",
    "read",
    "run",
    "sample",
    "sequence",
}


def _reject_private_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    segments = re.split(r"[^a-z0-9]+", normalized)
    if any(
        segment.startswith(term)
        for segment in segments
        for term in _RESERVED_PRIVATE_TERMS
    ):
        raise ValueError("controlled identifier contains a reserved privacy term")
    return value


def _token(prefix: str):
    return Annotated[
        str,
        StringConstraints(
            min_length=len(prefix) + 2,
            max_length=96,
            pattern=rf"^{prefix}[a-z0-9]+(?:_[a-z0-9]+)*$",
        ),
        AfterValidator(_reject_private_token),
    ]


ComparisonIdentityId = _token("cmpid_")
LongitudinalPolicyId = _token("longpolicy_")
EvidenceRef = _token("evidence_")
BridgeRef = _token("bridge_")
EngineVersion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=32,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    ),
]


class ComparisonDimension(StrEnum):
    ASSAY_PROTOCOL = "assay_protocol"
    PREANALYTICS_POLICY = "preanalytics_policy"
    REFERENCE = "reference"
    CANONICAL_MODEL = "canonical_model"
    MODIFIED_BASE_MODEL = "modified_base_model"
    TRIMMING_POLICY = "trimming_policy"
    MEASUREMENT_DEFINITION = "measurement_definition"
    ATLAS_MARKER_SET = "atlas_marker_set"
    FILTER_QC_POLICY = "filter_qc_policy"
    UNCERTAINTY_METHOD = "uncertainty_method"
    COORDINATE_SEMANTICS = "coordinate_semantics"
    DENOMINATOR_SEMANTICS = "denominator_semantics"
    RESULT_SCHEMA = "result_schema"


ALL_COMPARISON_DIMENSIONS = tuple(ComparisonDimension)


class DimensionValueState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class ComparisonDimensionValue(CompatibilityContract):
    """One exact policy component, or an explicit unknown without a value."""

    dimension: ComparisonDimension
    state: DimensionValueState
    identity_id: ComparisonIdentityId | None = None
    version: Version | None = None
    content_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def state_matches_identity(self) -> ComparisonDimensionValue:
        values = (self.identity_id, self.version, self.content_sha256)
        if self.state == DimensionValueState.KNOWN and not all(
            value is not None for value in values
        ):
            raise ValueError("known comparison dimension requires complete identity")
        if self.state == DimensionValueState.UNKNOWN and any(
            value is not None for value in values
        ):
            raise ValueError("unknown comparison dimension cannot claim identity")
        return self


class LongitudinalComparisonKey(CompatibilityContract):
    """Complete comparison identity for one immutable measurement result."""

    schema_version: Literal["traceback.longitudinal-comparison-key.v1"] = (
        "traceback.longitudinal-comparison-key.v1"
    )
    result_id: ResultId
    result_sha256: Sha256
    bundle_id: BundleId
    bundle_sha256: Sha256
    e05_compatibility_key_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    registry_sha256: Sha256
    registry_version: int = Field(ge=1, le=1_000_000)
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0, le=10_000_000)
    capability_sha256: Sha256
    dimensions: tuple[ComparisonDimensionValue, ...] = Field(
        min_length=len(ALL_COMPARISON_DIMENSIONS),
        max_length=len(ALL_COMPARISON_DIMENSIONS),
    )

    @model_validator(mode="after")
    def complete_ordered_dimensions(self) -> LongitudinalComparisonKey:
        actual = tuple(item.dimension for item in self.dimensions)
        if actual != ALL_COMPARISON_DIMENSIONS:
            raise ValueError("comparison key must contain every dimension in order")
        measurement_definition = self.dimensions[
            ALL_COMPARISON_DIMENSIONS.index(ComparisonDimension.MEASUREMENT_DEFINITION)
        ]
        if (
            measurement_definition.state != DimensionValueState.KNOWN
            or measurement_definition.content_sha256
            != measurement_definition_sha256(
                self.method_ref,
                self.method_definition_sha256,
                self.quantity_id,
                self.unit,
            )
        ):
            raise ValueError(
                "measurement-definition dimension must bind exact method and quantity"
            )
        return self


class LongitudinalRecord(CompatibilityContract):
    """Exact E05 result plus protected, authorized provider-local linkage."""

    schema_version: Literal["traceback.longitudinal-record.v1"] = (
        "traceback.longitudinal-record.v1"
    )
    measurement: VerifiedMeasurementRecord
    comparison_key: LongitudinalComparisonKey
    linkage_revision: LinkageRevision
    authorized_linkage: AuthorizedLinkageRevision | None
    activation_receipt: CommittedLinkageReceipt | None = None

    @model_validator(mode="after")
    def exact_bindings(self) -> LongitudinalRecord:
        key = self.comparison_key
        measurement = self.measurement
        capability = measurement.current_capability
        if (
            key.result_id != measurement.result_id
            or key.result_sha256 != measurement.result_sha256
            or key.bundle_id != measurement.bundle_id
            or key.bundle_sha256 != measurement.bundle_sha256
            or key.e05_compatibility_key_sha256
            != compatibility_key_sha256(measurement.compatibility_key)
        ):
            raise ValueError("longitudinal key does not bind exact E05 result")
        if self.comparison_key.method_ref != self.measurement.method.method_ref:
            raise ValueError("longitudinal key method does not match result")
        if (
            self.comparison_key.method_definition_sha256
            != self.measurement.method_definition_sha256
        ):
            raise ValueError("longitudinal key method digest does not match result")
        if (
            self.comparison_key.quantity_id != self.measurement.method.quantity_id
            or self.comparison_key.unit != self.measurement.method.unit
        ):
            raise ValueError("longitudinal key quantity does not match result")
        if (
            key.registry_sha256 != capability.registry_sha256
            or key.registry_version != capability.registry_version
            or key.authority_head_sha256 != capability.authority_head_sha256
            or key.authority_revision != capability.authority_revision
            or key.capability_sha256 != capability_sha256(capability)
        ):
            raise ValueError("longitudinal key does not bind current E01 capability")
        if self.linkage_revision.technical.measurement_id != provider_measurement_id(
            measurement
        ):
            raise ValueError("provider measurement linkage does not bind E05 result")
        if self.linkage_revision.source_projection_ref != provider_projection_ref(
            measurement
        ):
            raise ValueError("provider projection does not bind E01 authority")
        if (
            self.authorized_linkage is not None
            and self.authorized_linkage.revision != self.linkage_revision
        ):
            raise ValueError("authorized linkage does not bind exact revision")
        if self.activation_receipt is not None:
            receipt = self.activation_receipt
            if self.authorized_linkage is None:
                raise ValueError("activation receipt requires signed linkage proof")
            if (
                receipt.provider_namespace != self.linkage_revision.provider_namespace
                or receipt.linkage_id != self.linkage_revision.linkage_id
                or receipt.revision != self.linkage_revision.revision
                or receipt.linkage_revision_sha256
                != linkage_revision_sha256(self.linkage_revision)
                or receipt.authorized_record_sha256
                != hashlib.sha256(
                    canonical_contract_bytes(self.authorized_linkage)
                ).hexdigest()
            ):
                raise ValueError(
                    "activation receipt does not bind exact signed linkage proof"
                )
        self._validate_overlapping_e05_dimensions()
        return self

    def _validate_overlapping_e05_dimensions(self) -> None:
        values = {item.dimension: item for item in self.comparison_key.dimensions}
        e05 = self.measurement.compatibility_key
        expected = {
            ComparisonDimension.REFERENCE: optional_contract_sha256(
                e05.reference_asset
            ),
            ComparisonDimension.ATLAS_MARKER_SET: composite_contract_sha256(
                e05.atlas_asset,
                e05.grid_asset,
                e05.panel_asset,
            ),
            ComparisonDimension.FILTER_QC_POLICY: optional_text_sha256(
                e05.normalization_semantics_id
            ),
            ComparisonDimension.COORDINATE_SEMANTICS: optional_text_sha256(
                e05.coordinate_semantics_id
            ),
            ComparisonDimension.DENOMINATOR_SEMANTICS: optional_text_sha256(
                e05.denominator_semantics_id
            ),
            ComparisonDimension.RESULT_SCHEMA: hashlib.sha256(
                canonical_contract_bytes(e05.result_schema)
            ).hexdigest(),
        }
        for dimension, expected_digest in expected.items():
            value = values[dimension]
            if expected_digest is None:
                if value.state != DimensionValueState.UNKNOWN:
                    raise ValueError("unknown E05 identity must stay unknown in D02")
            elif (
                value.state != DimensionValueState.KNOWN
                or value.content_sha256 != expected_digest
            ):
                raise ValueError("D02 comparison dimension conflicts with E05 identity")


class LongitudinalOutcome(StrEnum):
    EQUIVALENT = "equivalent"
    QUALIFIED_COMPATIBLE = "qualified_compatible"
    REQUIRES_REANALYSIS = "requires_reanalysis"
    REGISTERED_BRIDGE = "registered_bridge"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class LongitudinalReason(StrEnum):
    EXACT_MATCH = "exact_match"
    QUALIFIED_ENVELOPE = "qualified_envelope"
    REANALYSIS_REQUIRED = "reanalysis_required"
    BRIDGE_AVAILABLE = "bridge_available"
    DISALLOWED_MISMATCH = "disallowed_mismatch"
    MIXED_DISPOSITIONS = "mixed_dispositions"
    UNKNOWN_DIMENSION = "unknown_dimension"
    LINKAGE_AUTHORITY_INVALID = "linkage_authority_invalid"
    POLICY_IDENTITY_INVALID = "policy_identity_invalid"
    ANCHOR_IDENTITY_INVALID = "anchor_identity_invalid"
    RESULT_STATE_INVALID = "result_state_invalid"
    SUBJECT_LINKAGE_MISMATCH = "subject_linkage_mismatch"


_EXACT_REASON_SET_BY_OUTCOME = {
    LongitudinalOutcome.EQUIVALENT: frozenset({LongitudinalReason.EXACT_MATCH}),
    LongitudinalOutcome.QUALIFIED_COMPATIBLE: frozenset(
        {LongitudinalReason.QUALIFIED_ENVELOPE}
    ),
    LongitudinalOutcome.REQUIRES_REANALYSIS: frozenset(
        {LongitudinalReason.REANALYSIS_REQUIRED}
    ),
    LongitudinalOutcome.REGISTERED_BRIDGE: frozenset(
        {LongitudinalReason.BRIDGE_AVAILABLE}
    ),
}
_INCOMPATIBLE_REASON_SETS = {
    frozenset({LongitudinalReason.DISALLOWED_MISMATCH}),
    frozenset({LongitudinalReason.MIXED_DISPOSITIONS}),
    frozenset({LongitudinalReason.SUBJECT_LINKAGE_MISMATCH}),
}
_UNKNOWN_REASON_SET = frozenset(
    {
        LongitudinalReason.UNKNOWN_DIMENSION,
        LongitudinalReason.LINKAGE_AUTHORITY_INVALID,
        LongitudinalReason.POLICY_IDENTITY_INVALID,
        LongitudinalReason.ANCHOR_IDENTITY_INVALID,
        LongitudinalReason.RESULT_STATE_INVALID,
    }
)


class DimensionDecisionDisposition(StrEnum):
    EXACT_MATCH = "exact_match"
    UNKNOWN = "unknown"
    DISALLOWED_MISMATCH = "disallowed_mismatch"
    QUALIFIED_COMPATIBLE = "qualified_compatible"
    REQUIRES_REANALYSIS = "requires_reanalysis"
    REGISTERED_BRIDGE = "registered_bridge"


class LongitudinalNextAction(StrEnum):
    USE_DIRECT_COMPARISON = "use_direct_comparison"
    USE_QUALIFIED_COMPARISON = "use_qualified_comparison"
    REQUEST_REANALYSIS = "request_reanalysis"
    REVIEW_REGISTERED_BRIDGE = "review_registered_bridge"
    START_SEPARATE_SERIES = "start_separate_series"
    RESOLVE_UNKNOWN_INPUTS = "resolve_unknown_inputs"


class LongitudinalDecisionReplayError(ValueError):
    """A stored D03 decision did not replay from exact bound authorities."""


def _next_action_for_outcome(
    outcome: LongitudinalOutcome,
) -> LongitudinalNextAction:
    return {
        LongitudinalOutcome.EQUIVALENT: (
            LongitudinalNextAction.USE_DIRECT_COMPARISON
        ),
        LongitudinalOutcome.QUALIFIED_COMPATIBLE: (
            LongitudinalNextAction.USE_QUALIFIED_COMPARISON
        ),
        LongitudinalOutcome.REQUIRES_REANALYSIS: (
            LongitudinalNextAction.REQUEST_REANALYSIS
        ),
        LongitudinalOutcome.REGISTERED_BRIDGE: (
            LongitudinalNextAction.REVIEW_REGISTERED_BRIDGE
        ),
        LongitudinalOutcome.INCOMPATIBLE: (
            LongitudinalNextAction.START_SEPARATE_SERIES
        ),
        LongitudinalOutcome.UNKNOWN: (
            LongitudinalNextAction.RESOLVE_UNKNOWN_INPUTS
        ),
    }[outcome]


class DimensionDecisionExplanation(CompatibilityContract):
    """Exact machine-readable explanation for one evaluated dimension."""

    dimension: ComparisonDimension
    anchor_state: DimensionValueState
    member_state: DimensionValueState
    anchor_value_sha256: Sha256
    member_value_sha256: Sha256
    disposition: DimensionDecisionDisposition
    evidence_ref: EvidenceRef | None = None
    evidence_sha256: Sha256 | None = None
    bridge_ref: BridgeRef | None = None

    @model_validator(mode="after")
    def coherent_explanation(self) -> DimensionDecisionExplanation:
        unknown = (
            self.anchor_state == DimensionValueState.UNKNOWN
            or self.member_state == DimensionValueState.UNKNOWN
        )
        exact = self.anchor_value_sha256 == self.member_value_sha256
        evidenced = self.disposition in {
            DimensionDecisionDisposition.QUALIFIED_COMPATIBLE,
            DimensionDecisionDisposition.REQUIRES_REANALYSIS,
            DimensionDecisionDisposition.REGISTERED_BRIDGE,
        }
        evidence_complete = (
            self.evidence_ref is not None and self.evidence_sha256 is not None
        )
        evidence_partial = (self.evidence_ref is None) != (
            self.evidence_sha256 is None
        )
        if unknown != (self.disposition == DimensionDecisionDisposition.UNKNOWN):
            raise ValueError("unknown dimension must have unknown disposition")
        if not unknown and exact != (
            self.disposition == DimensionDecisionDisposition.EXACT_MATCH
        ):
            raise ValueError("equal known values must be an exact match")
        if evidence_partial or evidenced != evidence_complete:
            raise ValueError("only registered allowances carry evidence")
        bridge = self.disposition == DimensionDecisionDisposition.REGISTERED_BRIDGE
        if bridge != (self.bridge_ref is not None):
            raise ValueError("only registered bridge explanations carry bridge_ref")
        return self


class DimensionAllowance(CompatibilityContract):
    """One named, evidence-bound transition away from the pinned anchor value."""

    member_value_sha256: Sha256
    outcome: Literal[
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
        LongitudinalOutcome.REQUIRES_REANALYSIS,
        LongitudinalOutcome.REGISTERED_BRIDGE,
    ]
    evidence_ref: EvidenceRef
    evidence_sha256: Sha256
    bridge_ref: BridgeRef | None = None

    @model_validator(mode="after")
    def bridge_is_explicit(self) -> DimensionAllowance:
        bridge = self.outcome == LongitudinalOutcome.REGISTERED_BRIDGE
        if bridge != (self.bridge_ref is not None):
            raise ValueError("only a registered-bridge allowance requires bridge_ref")
        return self


class DimensionAnchorRule(CompatibilityContract):
    dimension: ComparisonDimension
    anchor_value_sha256: Sha256
    allowances: tuple[DimensionAllowance, ...] = Field(
        default=(), max_length=MAX_ALLOWANCES_PER_DIMENSION
    )

    @model_validator(mode="after")
    def uniquely_sorted_allowances(self) -> DimensionAnchorRule:
        keys = [item.member_value_sha256 for item in self.allowances]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("dimension allowances must be uniquely sorted")
        if self.anchor_value_sha256 in keys:
            raise ValueError("anchor value cannot also be a mismatch allowance")
        return self


class LongitudinalAnchorPolicy(CompatibilityContract):
    """One policy pinned to one complete anchor key, never to adjacent pairs."""

    schema_version: Literal["traceback.longitudinal-anchor-policy.v1"]
    policy_id: LongitudinalPolicyId
    version: Version
    engine_version: EngineVersion
    anchor_key_sha256: Sha256
    rules: tuple[DimensionAnchorRule, ...] = Field(
        min_length=len(ALL_COMPARISON_DIMENSIONS),
        max_length=len(ALL_COMPARISON_DIMENSIONS),
    )

    @model_validator(mode="after")
    def complete_ordered_rules(self) -> LongitudinalAnchorPolicy:
        actual = tuple(item.dimension for item in self.rules)
        if actual != ALL_COMPARISON_DIMENSIONS:
            raise ValueError("anchor policy must contain every dimension in order")
        return self


class LongitudinalMemberDecision(CompatibilityContract):
    schema_version: Literal["traceback.longitudinal-member-decision.v2"]
    anchor_result_id: ResultId
    member_result_id: ResultId
    anchor_result_sha256: Sha256
    member_result_sha256: Sha256
    anchor_bundle_sha256: Sha256
    member_bundle_sha256: Sha256
    anchor_record_sha256: Sha256
    member_record_sha256: Sha256
    anchor_linkage_revision_sha256: Sha256
    member_linkage_revision_sha256: Sha256
    anchor_linkage_receipt_sha256: Sha256 | None
    member_linkage_receipt_sha256: Sha256 | None
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0, le=10_000_000)
    anchor_key_sha256: Sha256
    member_key_sha256: Sha256
    policy_id: LongitudinalPolicyId
    policy_version: Version
    policy_sha256: Sha256
    engine_version: EngineVersion
    outcome: LongitudinalOutcome
    reason_codes: tuple[LongitudinalReason, ...] = Field(
        min_length=1, max_length=MAX_DECISION_EVIDENCE
    )
    dimension_explanations: tuple[DimensionDecisionExplanation, ...] = Field(
        min_length=len(ALL_COMPARISON_DIMENSIONS),
        max_length=len(ALL_COMPARISON_DIMENSIONS),
    )
    evaluated_dimensions: tuple[ComparisonDimension, ...] = Field(
        min_length=len(ALL_COMPARISON_DIMENSIONS),
        max_length=len(ALL_COMPARISON_DIMENSIONS),
    )
    mismatch_dimensions: tuple[ComparisonDimension, ...] = Field(
        max_length=len(ALL_COMPARISON_DIMENSIONS)
    )
    unknown_dimensions: tuple[ComparisonDimension, ...] = Field(
        max_length=len(ALL_COMPARISON_DIMENSIONS)
    )
    evidence_refs: tuple[EvidenceRef, ...] = Field(
        max_length=MAX_DECISION_EVIDENCE
    )
    bridge_refs: tuple[BridgeRef, ...] = Field(
        max_length=MAX_DECISION_EVIDENCE
    )
    next_action: LongitudinalNextAction
    bridge_execution_state: Literal["not_executed"]
    delta_allowed: bool
    connecting_trend_allowed: bool

    @model_validator(mode="after")
    def coherent_rendering(self) -> LongitudinalMemberDecision:
        if self.evaluated_dimensions != ALL_COMPARISON_DIMENSIONS:
            raise ValueError("decision must report every evaluated dimension")
        if tuple(item.dimension for item in self.dimension_explanations) != (
            ALL_COMPARISON_DIMENSIONS
        ):
            raise ValueError("decision explanations must cover every dimension")
        for values, label in (
            (self.reason_codes, "reasons"),
            (self.mismatch_dimensions, "mismatches"),
            (self.unknown_dimensions, "unknown dimensions"),
            (self.evidence_refs, "evidence references"),
            (self.bridge_refs, "bridge references"),
        ):
            if values != tuple(sorted(set(values), key=str)):
                raise ValueError(f"decision {label} must be uniquely sorted")
        actual_reasons = frozenset(self.reason_codes)
        if self.outcome == LongitudinalOutcome.UNKNOWN:
            if not actual_reasons or not actual_reasons <= _UNKNOWN_REASON_SET:
                raise ValueError("unknown outcome has invalid reason set")
        elif self.outcome == LongitudinalOutcome.INCOMPATIBLE:
            if actual_reasons not in _INCOMPATIBLE_REASON_SETS:
                raise ValueError("incompatible outcome has invalid reason set")
        elif actual_reasons != _EXACT_REASON_SET_BY_OUTCOME[self.outcome]:
            raise ValueError("outcome has invalid exact reason set")
        eligible = self.outcome in {
            LongitudinalOutcome.EQUIVALENT,
            LongitudinalOutcome.QUALIFIED_COMPATIBLE,
        }
        if eligible and (
            self.anchor_linkage_receipt_sha256 is None
            or self.member_linkage_receipt_sha256 is None
        ):
            raise ValueError("eligible decision requires exact activation receipts")
        if self.delta_allowed != eligible or self.connecting_trend_allowed != eligible:
            raise ValueError("only equivalent or qualified outcomes permit rendering")
        if self.outcome == LongitudinalOutcome.REGISTERED_BRIDGE:
            if not self.bridge_refs:
                raise ValueError("registered bridge decision requires bridge reference")
        elif self.bridge_refs:
            raise ValueError("bridge references are allowed only for bridge decisions")
        if set(self.mismatch_dimensions) & set(self.unknown_dimensions):
            raise ValueError("one dimension cannot be both mismatch and unknown")
        explained_unknowns = tuple(
            sorted(
                (
                    item.dimension
                    for item in self.dimension_explanations
                    if item.disposition == DimensionDecisionDisposition.UNKNOWN
                ),
                key=str,
            )
        )
        explained_mismatches = tuple(
            sorted(
                (
                    item.dimension
                    for item in self.dimension_explanations
                    if item.disposition
                    not in {
                        DimensionDecisionDisposition.EXACT_MATCH,
                        DimensionDecisionDisposition.UNKNOWN,
                    }
                ),
                key=str,
            )
        )
        explained_evidence = tuple(
            sorted(
                {
                    item.evidence_ref
                    for item in self.dimension_explanations
                    if item.evidence_ref is not None
                }
            )
        )
        explained_bridges = tuple(
            sorted(
                {
                    item.bridge_ref
                    for item in self.dimension_explanations
                    if item.bridge_ref is not None
                }
            )
        )
        if self.unknown_dimensions != explained_unknowns:
            raise ValueError("unknown dimensions do not match exact explanations")
        if self.mismatch_dimensions != explained_mismatches:
            raise ValueError("mismatch dimensions do not match exact explanations")
        if self.evidence_refs != explained_evidence:
            raise ValueError("evidence references do not match exact explanations")
        summarized_bridges = (
            explained_bridges
            if self.outcome == LongitudinalOutcome.REGISTERED_BRIDGE
            else ()
        )
        if self.bridge_refs != summarized_bridges:
            raise ValueError("bridge references do not match exact explanations")
        expected_action = _next_action_for_outcome(self.outcome)
        if self.next_action != expected_action:
            raise ValueError("next action does not match compatibility outcome")
        if self.outcome == LongitudinalOutcome.EQUIVALENT:
            if (
                self.reason_codes != (LongitudinalReason.EXACT_MATCH,)
                or self.mismatch_dimensions
                or self.unknown_dimensions
                or self.evidence_refs
                or self.bridge_refs
            ):
                raise ValueError("equivalent decision must be an exact identity match")
        elif self.outcome == LongitudinalOutcome.QUALIFIED_COMPATIBLE:
            if (
                self.reason_codes != (LongitudinalReason.QUALIFIED_ENVELOPE,)
                or not self.mismatch_dimensions
                or self.unknown_dimensions
                or not self.evidence_refs
            ):
                raise ValueError("qualified decision requires evidenced mismatches")
        elif self.outcome in {
            LongitudinalOutcome.REQUIRES_REANALYSIS,
            LongitudinalOutcome.REGISTERED_BRIDGE,
        }:
            if not self.mismatch_dimensions or not self.evidence_refs:
                raise ValueError("remediation decision requires evidenced mismatches")
        elif self.outcome == LongitudinalOutcome.INCOMPATIBLE:
            if not self.mismatch_dimensions and (
                LongitudinalReason.SUBJECT_LINKAGE_MISMATCH not in self.reason_codes
            ):
                raise ValueError("incompatible decision requires mismatch evidence")
        elif not any(
            reason
            in {
                LongitudinalReason.UNKNOWN_DIMENSION,
                LongitudinalReason.LINKAGE_AUTHORITY_INVALID,
                LongitudinalReason.POLICY_IDENTITY_INVALID,
                LongitudinalReason.ANCHOR_IDENTITY_INVALID,
                LongitudinalReason.RESULT_STATE_INVALID,
            }
            for reason in self.reason_codes
        ):
            raise ValueError("unknown decision requires an unknown-state reason")
        return self


class LongitudinalSeriesDecision(CompatibilityContract):
    schema_version: Literal["traceback.longitudinal-series-decision.v2"]
    anchor_result_id: ResultId
    policy_sha256: Sha256
    member_result_ids: tuple[ResultId, ...] = Field(
        min_length=1, max_length=MAX_SERIES_MEMBERS
    )
    decisions: tuple[LongitudinalMemberDecision, ...] = Field(
        min_length=1, max_length=MAX_SERIES_MEMBERS
    )
    decision_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_SERIES_MEMBERS
    )

    @model_validator(mode="after")
    def exact_membership(self) -> LongitudinalSeriesDecision:
        if self.member_result_ids != tuple(sorted(set(self.member_result_ids))):
            raise ValueError("series member IDs must be uniquely sorted")
        decision_ids = tuple(item.member_result_id for item in self.decisions)
        if decision_ids != self.member_result_ids:
            raise ValueError("series decisions must exactly match ordered membership")
        if any(
            item.anchor_result_id != self.anchor_result_id
            or item.policy_sha256 != self.policy_sha256
            for item in self.decisions
        ):
            raise ValueError("series decisions must share one anchor and policy")
        if self.decision_sha256s != tuple(
            longitudinal_member_decision_sha256(item) for item in self.decisions
        ):
            raise ValueError("series decision digests do not match exact decisions")
        return self


def comparison_dimension_value_sha256(value: ComparisonDimensionValue) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def optional_contract_sha256(value: RegistryContract | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def composite_contract_sha256(
    *values: RegistryContract | None,
) -> str | None:
    if all(value is None for value in values):
        return None
    framed = bytearray(b"traceback-longitudinal-composite.v1\0")
    for value in values:
        content = b"" if value is None else canonical_contract_bytes(value)
        framed.extend(len(content).to_bytes(8, "big"))
        framed.extend(content)
    return hashlib.sha256(bytes(framed)).hexdigest()


def optional_text_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(
        b"traceback-longitudinal-text.v1\0" + value.encode("ascii")
    ).hexdigest()


def capability_sha256(capability: RegistryContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(capability)).hexdigest()


def provider_measurement_id(measurement: VerifiedMeasurementRecord) -> str:
    payload = b"\0".join(
        (
            b"traceback-provider-measurement.v1",
            measurement.result_id.encode("ascii"),
            measurement.result_sha256.encode("ascii"),
            measurement.bundle_id.encode("ascii"),
            measurement.bundle_sha256.encode("ascii"),
        )
    )
    return "measurement_" + hashlib.sha256(payload).hexdigest()[:32]


def provider_projection_ref(measurement: VerifiedMeasurementRecord) -> str:
    capability = measurement.current_capability
    payload = b"\0".join(
        (
            b"traceback-provider-projection.v1",
            capability.registry_sha256.encode("ascii"),
            str(capability.registry_version).encode("ascii"),
            capability.authority_head_sha256.encode("ascii"),
            str(capability.authority_revision).encode("ascii"),
            measurement.method_definition_sha256.encode("ascii"),
        )
    )
    return "projection_" + hashlib.sha256(payload).hexdigest()[:32]


def measurement_definition_sha256(
    method_ref: MethodReference,
    method_definition_digest: str,
    quantity_id: str,
    unit: str,
) -> str:
    """Bind the method and quantity fields duplicated outside dimension tuples."""

    payload = b"\0".join(
        (
            b"traceback-longitudinal-measurement-definition.v1",
            method_ref.method_id.encode("ascii"),
            method_ref.version.encode("ascii"),
            method_definition_digest.encode("ascii"),
            quantity_id.encode("ascii"),
            unit.encode("ascii"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def longitudinal_comparison_key_sha256(key: LongitudinalComparisonKey) -> str:
    return hashlib.sha256(canonical_contract_bytes(key)).hexdigest()


def longitudinal_anchor_policy_sha256(policy: LongitudinalAnchorPolicy) -> str:
    return hashlib.sha256(canonical_contract_bytes(policy)).hexdigest()


def longitudinal_record_sha256(record: LongitudinalRecord) -> str:
    return hashlib.sha256(canonical_contract_bytes(record)).hexdigest()


def longitudinal_member_decision_sha256(
    decision: LongitudinalMemberDecision,
) -> str:
    return hashlib.sha256(canonical_contract_bytes(decision)).hexdigest()


_INVALID_INPUT_SHA256 = hashlib.sha256(
    b"traceback-longitudinal-invalid-input.v1"
).hexdigest()
_INVALID_INPUT_RESULT_ID = "result_invalid_input"
_INVALID_INPUT_POLICY_ID = "longpolicy_invalid_input"
_INVALID_INPUT_MEMBER_DECISION = LongitudinalMemberDecision(
    anchor_result_id=_INVALID_INPUT_RESULT_ID,
    member_result_id=_INVALID_INPUT_RESULT_ID,
    anchor_result_sha256=_INVALID_INPUT_SHA256,
    member_result_sha256=_INVALID_INPUT_SHA256,
    anchor_bundle_sha256=_INVALID_INPUT_SHA256,
    member_bundle_sha256=_INVALID_INPUT_SHA256,
    anchor_record_sha256=_INVALID_INPUT_SHA256,
    member_record_sha256=_INVALID_INPUT_SHA256,
    anchor_linkage_revision_sha256=_INVALID_INPUT_SHA256,
    member_linkage_revision_sha256=_INVALID_INPUT_SHA256,
    anchor_linkage_receipt_sha256=None,
    member_linkage_receipt_sha256=None,
    authority_head_sha256=_INVALID_INPUT_SHA256,
    authority_revision=0,
    anchor_key_sha256=_INVALID_INPUT_SHA256,
    member_key_sha256=_INVALID_INPUT_SHA256,
    policy_id=_INVALID_INPUT_POLICY_ID,
    policy_version="0.0.0",
    policy_sha256=_INVALID_INPUT_SHA256,
    engine_version="0.0.0",
    outcome=LongitudinalOutcome.UNKNOWN,
    reason_codes=(
        LongitudinalReason.LINKAGE_AUTHORITY_INVALID,
        LongitudinalReason.RESULT_STATE_INVALID,
    ),
    evaluated_dimensions=ALL_COMPARISON_DIMENSIONS,
    mismatch_dimensions=(),
    unknown_dimensions=tuple(sorted(ALL_COMPARISON_DIMENSIONS, key=str)),
    evidence_refs=(),
    bridge_refs=(),
    delta_allowed=False,
    connecting_trend_allowed=False,
)
_INVALID_INPUT_SERIES_DECISION = LongitudinalSeriesDecision(
    anchor_result_id=_INVALID_INPUT_RESULT_ID,
    policy_sha256=_INVALID_INPUT_SHA256,
    member_result_ids=(_INVALID_INPUT_RESULT_ID,),
    decisions=(_INVALID_INPUT_MEMBER_DECISION,),
    decision_sha256s=(
        longitudinal_member_decision_sha256(_INVALID_INPUT_MEMBER_DECISION),
    ),
)


def _replay_record(record: object) -> LongitudinalRecord:
    if type(record) is not LongitudinalRecord:
        raise TypeError("longitudinal record type is invalid")
    encoded = _strict_validation_bytes(record)
    replayed = LongitudinalRecord.model_validate_json(encoded)
    if canonical_contract_bytes(replayed) != encoded:
        raise ValueError("longitudinal record is not canonical")
    return replayed


def _replay_policy(policy: object) -> LongitudinalAnchorPolicy:
    if type(policy) is not LongitudinalAnchorPolicy:
        raise TypeError("longitudinal policy type is invalid")
    encoded = _strict_validation_bytes(policy)
    replayed = LongitudinalAnchorPolicy.model_validate_json(encoded)
    if canonical_contract_bytes(replayed) != encoded:
        raise ValueError("longitudinal policy is not canonical")
    return replayed


def _strict_validation_bytes(contract: object) -> bytes:
    payload = contract.model_dump(  # type: ignore[attr-defined]
        mode="json",
        exclude_none=False,
        warnings="error",
    )
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _trust_pins_sha256(pins: dict[str, str]) -> str:
    encoded = json.dumps(
        sorted(pins.items()),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(b"traceback-linkage-trust-pins-v1\0" + encoded).hexdigest()


def _validate_store_input(
    store: object,
) -> tuple[ProviderLinkageStore | None, ActiveLinkageSnapshot | None]:
    if store is None:
        return None, None
    try:
        _PINNED_PROCESS_INTEGRITY_CHECK()
    except ProviderLinkageStoreError:
        raise ValueError("linkage store process integrity check failed") from None
    try:
        invalid_callable_boundary = (
            type(store) is not ProviderLinkageStore
            or ProviderLinkageStore.verify_current_receipt
            is not _PINNED_VERIFY_CURRENT_RECEIPT
            or ProviderLinkageStore.active_snapshot is not _PINNED_ACTIVE_SNAPSHOT
            or any(
                getattr(ProviderLinkageStore, name, None) is not pinned
                for name, pinned in _PINNED_STORE_CALLABLES.items()
            )
            or any(name in ProviderLinkageStore.__dict__ for name in vars(store))
            or not _PINNED_TIME_SOURCE_IDENTITY_CHECK(store)
        )
    except _STORE_BOUNDARY_ERRORS:
        raise ValueError("linkage store authority input is invalid") from None
    if invalid_callable_boundary:
        raise ValueError("linkage store authority input is invalid")
    state = vars(store)
    required = {
        "_time_source",
        "_connection",
        "_database_fd",
        "_database_identity",
        "_lock",
        "_root_fd",
        "_root_identity",
        "_sqlite_database_fd",
        "_storage_identity",
        "_store_epoch_sha256",
        "_store_id",
        "_trust_pins",
        "_trust_pins_digest",
    }
    if (
        not required <= set(state)
        or type(state["_lock"]) is not _RLOCK_TYPE
        or type(state["_connection"]) is not sqlite3.Connection
        or any(
            type(state[name]) is not int or state[name] < 0
            for name in ("_root_fd", "_database_fd", "_sqlite_database_fd")
        )
        or any(
            type(state[name]) is not tuple
            or len(state[name]) != 2
            or any(type(value) is not int for value in state[name])
            for name in ("_root_identity", "_database_identity")
        )
        or type(state["_trust_pins"]) is not dict
        or type(state["_time_source"]) is not AuthorityTimeSource
        or any(
            type(state[name]) is not str
            for name in (
                "_storage_identity",
                "_store_epoch_sha256",
                "_store_id",
                "_trust_pins_digest",
            )
        )
    ):
        raise ValueError("linkage store internal authority state is invalid")
    try:
        snapshot = _PINNED_ACTIVE_SNAPSHOT(store)
    except _STORE_BOUNDARY_ERRORS:
        raise ValueError("linkage store live authority state is invalid") from None
    try:
        authority_changed = (
            any(
                getattr(ProviderLinkageStore, name, None) is not pinned
                for name, pinned in _PINNED_STORE_CALLABLES.items()
            )
            or any(name in ProviderLinkageStore.__dict__ for name in vars(store))
            or not _PINNED_TIME_SOURCE_IDENTITY_CHECK(store)
        )
        _PINNED_PROCESS_INTEGRITY_CHECK()
    except ProviderLinkageStoreUnsafe as error:
        if str(error) == "linkage store process integrity check failed":
            raise ValueError("linkage store process integrity check failed") from None
        raise ValueError("linkage store authority changed during snapshot") from None
    except _STORE_BOUNDARY_ERRORS:
        raise ValueError("linkage store authority changed during snapshot") from None
    if authority_changed:
        raise ValueError("linkage store authority changed during snapshot")
    return store, snapshot


def _validate_member_inputs(
    anchor: object,
    member: object,
    policy: object,
    expected_policy_sha256: object,
    expected_authority_head_sha256: object,
    expected_linkage_trust_snapshot_sha256_by_provider: object,
    linkage_store: object,
) -> (
    tuple[
        LongitudinalRecord,
        LongitudinalRecord,
        LongitudinalAnchorPolicy,
        str,
        str,
        dict[str, str],
        ProviderLinkageStore | None,
        ActiveLinkageSnapshot | None,
    ]
    | None
):
    try:
        store, snapshot = _validate_store_input(linkage_store)
        if (
            type(expected_linkage_trust_snapshot_sha256_by_provider) is not dict
            or not 1
            <= len(expected_linkage_trust_snapshot_sha256_by_provider)
            <= MAX_PROVIDER_TRUST_PINS
        ):
            raise TypeError("linkage trust pins must be an exact dictionary")
        replayed_anchor = _replay_record(anchor)
        replayed_member = _replay_record(member)
        pins = _TRUST_PINS_ADAPTER.validate_python(
            expected_linkage_trust_snapshot_sha256_by_provider
        )
        required_providers = {
            replayed_anchor.linkage_revision.provider_namespace,
            replayed_member.linkage_revision.provider_namespace,
        }
        if set(pins) != required_providers:
            raise ValueError("linkage trust pins do not match required providers")
        if snapshot is not None and snapshot.trust_pins_sha256 != _trust_pins_sha256(
            pins
        ):
            raise ValueError("live linkage store trust pins do not match exact input")
        return (
            replayed_anchor,
            replayed_member,
            _replay_policy(policy),
            _SHA256_ADAPTER.validate_python(expected_policy_sha256),
            _SHA256_ADAPTER.validate_python(expected_authority_head_sha256),
            pins,
            store,
            snapshot,
        )
    except (
        ValidationError,
        PydanticSerializationError,
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None


def _validate_series_inputs(
    anchor: object,
    members: object,
    policy: object,
    expected_policy_sha256: object,
    expected_authority_head_sha256: object,
    expected_linkage_trust_snapshot_sha256_by_provider: object,
    linkage_store: object,
) -> (
    tuple[
        LongitudinalRecord,
        tuple[LongitudinalRecord, ...],
        LongitudinalAnchorPolicy,
        str,
        str,
        dict[str, str],
        ProviderLinkageStore | None,
        ActiveLinkageSnapshot | None,
    ]
    | None
):
    try:
        store, snapshot = _validate_store_input(linkage_store)
        if type(members) is not tuple or not 1 <= len(members) <= MAX_SERIES_MEMBERS:
            raise TypeError("series members must be one bounded exact tuple")
        if (
            type(expected_linkage_trust_snapshot_sha256_by_provider) is not dict
            or not 1
            <= len(expected_linkage_trust_snapshot_sha256_by_provider)
            <= MAX_PROVIDER_TRUST_PINS
        ):
            raise TypeError("linkage trust pins must be an exact dictionary")
        replayed_anchor = _replay_record(anchor)
        replayed_members = tuple(_replay_record(item) for item in members)
        pins = _TRUST_PINS_ADAPTER.validate_python(
            expected_linkage_trust_snapshot_sha256_by_provider
        )
        required_providers = {
            replayed_anchor.linkage_revision.provider_namespace,
            *(item.linkage_revision.provider_namespace for item in replayed_members),
        }
        if set(pins) != required_providers:
            raise ValueError("linkage trust pins do not match required providers")
        if snapshot is not None and snapshot.trust_pins_sha256 != _trust_pins_sha256(
            pins
        ):
            raise ValueError("live linkage store trust pins do not match exact input")
        return (
            replayed_anchor,
            replayed_members,
            _replay_policy(policy),
            _SHA256_ADAPTER.validate_python(expected_policy_sha256),
            _SHA256_ADAPTER.validate_python(expected_authority_head_sha256),
            pins,
            store,
            snapshot,
        )
    except (
        ValidationError,
        PydanticSerializationError,
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None


def _linkage_authority_invalid(
    record: LongitudinalRecord,
    *,
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, str],
    linkage_snapshot: ActiveLinkageSnapshot | None,
) -> bool:
    authorized = record.authorized_linkage
    receipt = record.activation_receipt
    expected_pins_sha256 = _trust_pins_sha256(
        expected_linkage_trust_snapshot_sha256_by_provider
    )
    expected_trust = expected_linkage_trust_snapshot_sha256_by_provider.get(
        record.linkage_revision.provider_namespace
    )
    invalid = (
        authorized is None
        or receipt is None
        or linkage_snapshot is None
        or expected_trust is None
        or provider_trust_snapshot_sha256(authorized.trust_snapshot) != expected_trust
        or not authorized.authorization.linkage_authorized
        or receipt.trust_pins_sha256 != expected_pins_sha256
        or linkage_snapshot.trust_pins_sha256 != expected_pins_sha256
    )
    if invalid:
        return True
    assert receipt is not None
    return (
        receipt.state_version != linkage_snapshot.state_version
        or receipt.state_head_sha256 != linkage_snapshot.state_head_sha256
        or receipt not in linkage_snapshot.receipts
    )


def _result_state_invalid(
    record: LongitudinalRecord,
    *,
    expected_authority_head_sha256: str,
) -> bool:
    measurement = record.measurement
    capability = measurement.current_capability
    return (
        capability.authority_head_sha256 != expected_authority_head_sha256
        or record.comparison_key.authority_head_sha256 != expected_authority_head_sha256
        or record.comparison_key.authority_revision != capability.authority_revision
        or measurement.execution_state.value != "complete"
        or measurement.information_state.value != "sufficient"
        or measurement.trust_state.value != "verified"
    )


def decide_longitudinal_member(
    anchor: LongitudinalRecord,
    member: LongitudinalRecord,
    policy: LongitudinalAnchorPolicy,
    *,
    expected_policy_sha256: str,
    expected_authority_head_sha256: str,
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, str],
    linkage_store: ProviderLinkageStore | None,
) -> LongitudinalMemberDecision:
    """Evaluate one member against the pinned anchor, never against a neighbor."""

    validated = _validate_member_inputs(
        anchor,
        member,
        policy,
        expected_policy_sha256,
        expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider,
        linkage_store,
    )
    if validated is None:
        return _INVALID_INPUT_MEMBER_DECISION
    (
        anchor,
        member,
        policy,
        expected_policy_sha256,
        expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider,
        linkage_store,
        linkage_snapshot,
    ) = validated
    anchor_key_sha256 = longitudinal_comparison_key_sha256(anchor.comparison_key)
    member_key_sha256 = longitudinal_comparison_key_sha256(member.comparison_key)
    policy_sha256 = longitudinal_anchor_policy_sha256(policy)
    reasons: set[LongitudinalReason] = set()
    explanations: list[DimensionDecisionExplanation] = []
    mismatches: list[ComparisonDimension] = []
    unknowns: list[ComparisonDimension] = []
    evidence: set[str] = set()
    bridges: set[str] = set()
    dispositions: set[LongitudinalOutcome] = set()

    if policy_sha256 != expected_policy_sha256:
        reasons.add(LongitudinalReason.POLICY_IDENTITY_INVALID)
    if policy.anchor_key_sha256 != anchor_key_sha256:
        reasons.add(LongitudinalReason.ANCHOR_IDENTITY_INVALID)
    linkage_invalid = _linkage_authority_invalid(
        anchor,
        expected_linkage_trust_snapshot_sha256_by_provider=(
            expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_snapshot=linkage_snapshot,
    ) or _linkage_authority_invalid(
        member,
        expected_linkage_trust_snapshot_sha256_by_provider=(
            expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_snapshot=linkage_snapshot,
    )
    if linkage_invalid:
        reasons.add(LongitudinalReason.LINKAGE_AUTHORITY_INVALID)
    if _result_state_invalid(
        anchor,
        expected_authority_head_sha256=expected_authority_head_sha256,
    ) or _result_state_invalid(
        member,
        expected_authority_head_sha256=expected_authority_head_sha256,
    ):
        reasons.add(LongitudinalReason.RESULT_STATE_INVALID)
    if (
        anchor.measurement.current_capability.authority_head_sha256
        != member.measurement.current_capability.authority_head_sha256
        or anchor.measurement.current_capability.authority_revision
        != member.measurement.current_capability.authority_revision
    ):
        reasons.add(LongitudinalReason.RESULT_STATE_INVALID)
    if (
        not linkage_invalid
        and anchor.activation_receipt is not None
        and member.activation_receipt is not None
        and (
            anchor.activation_receipt.state_version
            != member.activation_receipt.state_version
            or anchor.activation_receipt.state_head_sha256
            != member.activation_receipt.state_head_sha256
        )
    ):
        reasons.add(LongitudinalReason.LINKAGE_AUTHORITY_INVALID)
    linkage_identity_mismatch = (
        anchor.linkage_revision.provider_namespace
        != member.linkage_revision.provider_namespace
        or anchor.linkage_revision.biological.subject_token
        != member.linkage_revision.biological.subject_token
    )

    for anchor_value, member_value, rule in zip(
        anchor.comparison_key.dimensions,
        member.comparison_key.dimensions,
        policy.rules,
        strict=True,
    ):
        anchor_digest = comparison_dimension_value_sha256(anchor_value)
        member_digest = comparison_dimension_value_sha256(member_value)
        if rule.anchor_value_sha256 != anchor_digest:
            reasons.add(LongitudinalReason.ANCHOR_IDENTITY_INVALID)
        if (
            anchor_value.state == DimensionValueState.UNKNOWN
            or member_value.state == DimensionValueState.UNKNOWN
        ):
            unknowns.append(rule.dimension)
            explanations.append(
                DimensionDecisionExplanation(
                    dimension=rule.dimension,
                    anchor_state=anchor_value.state,
                    member_state=member_value.state,
                    anchor_value_sha256=anchor_digest,
                    member_value_sha256=member_digest,
                    disposition=DimensionDecisionDisposition.UNKNOWN,
                )
            )
            continue
        if anchor_digest == member_digest:
            explanations.append(
                DimensionDecisionExplanation(
                    dimension=rule.dimension,
                    anchor_state=anchor_value.state,
                    member_state=member_value.state,
                    anchor_value_sha256=anchor_digest,
                    member_value_sha256=member_digest,
                    disposition=DimensionDecisionDisposition.EXACT_MATCH,
                )
            )
            continue
        mismatches.append(rule.dimension)
        allowance = next(
            (
                item
                for item in rule.allowances
                if item.member_value_sha256 == member_digest
            ),
            None,
        )
        if allowance is None:
            dispositions.add(LongitudinalOutcome.INCOMPATIBLE)
            explanations.append(
                DimensionDecisionExplanation(
                    dimension=rule.dimension,
                    anchor_state=anchor_value.state,
                    member_state=member_value.state,
                    anchor_value_sha256=anchor_digest,
                    member_value_sha256=member_digest,
                    disposition=(
                        DimensionDecisionDisposition.DISALLOWED_MISMATCH
                    ),
                )
            )
            continue
        dispositions.add(allowance.outcome)
        evidence.add(allowance.evidence_ref)
        if allowance.bridge_ref is not None:
            bridges.add(allowance.bridge_ref)
        explanations.append(
            DimensionDecisionExplanation(
                dimension=rule.dimension,
                anchor_state=anchor_value.state,
                member_state=member_value.state,
                anchor_value_sha256=anchor_digest,
                member_value_sha256=member_digest,
                disposition=DimensionDecisionDisposition(allowance.outcome.value),
                evidence_ref=allowance.evidence_ref,
                evidence_sha256=allowance.evidence_sha256,
                bridge_ref=allowance.bridge_ref,
            )
        )

    if unknowns:
        reasons.add(LongitudinalReason.UNKNOWN_DIMENSION)
    if reasons:
        outcome = LongitudinalOutcome.UNKNOWN
    elif linkage_identity_mismatch:
        outcome = LongitudinalOutcome.INCOMPATIBLE
        reasons.add(LongitudinalReason.SUBJECT_LINKAGE_MISMATCH)
    elif not mismatches:
        outcome = LongitudinalOutcome.EQUIVALENT
        reasons.add(LongitudinalReason.EXACT_MATCH)
    elif dispositions == {LongitudinalOutcome.QUALIFIED_COMPATIBLE}:
        outcome = LongitudinalOutcome.QUALIFIED_COMPATIBLE
        reasons.add(LongitudinalReason.QUALIFIED_ENVELOPE)
    elif dispositions == {LongitudinalOutcome.REQUIRES_REANALYSIS}:
        outcome = LongitudinalOutcome.REQUIRES_REANALYSIS
        reasons.add(LongitudinalReason.REANALYSIS_REQUIRED)
    elif dispositions == {LongitudinalOutcome.REGISTERED_BRIDGE}:
        outcome = LongitudinalOutcome.REGISTERED_BRIDGE
        reasons.add(LongitudinalReason.BRIDGE_AVAILABLE)
    elif dispositions == {LongitudinalOutcome.INCOMPATIBLE}:
        outcome = LongitudinalOutcome.INCOMPATIBLE
        reasons.add(LongitudinalReason.DISALLOWED_MISMATCH)
    else:
        outcome = LongitudinalOutcome.INCOMPATIBLE
        reasons.add(LongitudinalReason.MIXED_DISPOSITIONS)

    eligible = outcome in {
        LongitudinalOutcome.EQUIVALENT,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
    }
    next_action = _next_action_for_outcome(outcome)
    return LongitudinalMemberDecision(
        schema_version="traceback.longitudinal-member-decision.v2",
        anchor_result_id=anchor.measurement.result_id,
        member_result_id=member.measurement.result_id,
        anchor_result_sha256=anchor.measurement.result_sha256,
        member_result_sha256=member.measurement.result_sha256,
        anchor_bundle_sha256=anchor.measurement.bundle_sha256,
        member_bundle_sha256=member.measurement.bundle_sha256,
        anchor_record_sha256=longitudinal_record_sha256(anchor),
        member_record_sha256=longitudinal_record_sha256(member),
        anchor_linkage_revision_sha256=linkage_revision_sha256(anchor.linkage_revision),
        member_linkage_revision_sha256=linkage_revision_sha256(member.linkage_revision),
        anchor_linkage_receipt_sha256=(
            committed_linkage_receipt_sha256(anchor.activation_receipt)
            if anchor.activation_receipt is not None
            else None
        ),
        member_linkage_receipt_sha256=(
            committed_linkage_receipt_sha256(member.activation_receipt)
            if member.activation_receipt is not None
            else None
        ),
        authority_head_sha256=expected_authority_head_sha256,
        authority_revision=anchor.measurement.current_capability.authority_revision,
        anchor_key_sha256=anchor_key_sha256,
        member_key_sha256=member_key_sha256,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_sha256=policy_sha256,
        engine_version=policy.engine_version,
        outcome=outcome,
        reason_codes=tuple(sorted(reasons, key=str)),
        dimension_explanations=tuple(explanations),
        evaluated_dimensions=ALL_COMPARISON_DIMENSIONS,
        mismatch_dimensions=tuple(sorted(mismatches, key=str)),
        unknown_dimensions=tuple(sorted(unknowns, key=str)),
        evidence_refs=tuple(sorted(evidence)),
        bridge_refs=(
            tuple(sorted(bridges))
            if outcome == LongitudinalOutcome.REGISTERED_BRIDGE
            else ()
        ),
        next_action=next_action,
        bridge_execution_state="not_executed",
        delta_allowed=eligible,
        connecting_trend_allowed=eligible,
    )


def decide_longitudinal_series(
    anchor: LongitudinalRecord,
    members: tuple[LongitudinalRecord, ...],
    policy: LongitudinalAnchorPolicy,
    *,
    expected_policy_sha256: str,
    expected_authority_head_sha256: str,
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, str],
    linkage_store: ProviderLinkageStore | None,
) -> LongitudinalSeriesDecision:
    """Evaluate canonical immutable membership against one explicit anchor."""

    validated = _validate_series_inputs(
        anchor,
        members,
        policy,
        expected_policy_sha256,
        expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider,
        linkage_store,
    )
    if validated is None:
        return _INVALID_INPUT_SERIES_DECISION
    (
        anchor,
        members,
        policy,
        expected_policy_sha256,
        expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider,
        linkage_store,
        _linkage_snapshot,
    ) = validated
    member_ids = tuple(item.measurement.result_id for item in members)
    if member_ids != tuple(sorted(set(member_ids))):
        raise ValueError("series members must be uniquely sorted by result ID")
    decisions = tuple(
        decide_longitudinal_member(
            anchor,
            member,
            policy,
            expected_policy_sha256=expected_policy_sha256,
            expected_authority_head_sha256=expected_authority_head_sha256,
            expected_linkage_trust_snapshot_sha256_by_provider=(
                expected_linkage_trust_snapshot_sha256_by_provider
            ),
            linkage_store=linkage_store,
        )
        for member in members
    )
    return LongitudinalSeriesDecision(
        schema_version="traceback.longitudinal-series-decision.v2",
        anchor_result_id=anchor.measurement.result_id,
        policy_sha256=longitudinal_anchor_policy_sha256(policy),
        member_result_ids=member_ids,
        decisions=decisions,
        decision_sha256s=tuple(
            longitudinal_member_decision_sha256(item) for item in decisions
        ),
    )


def replay_longitudinal_member_decision(
    expected: LongitudinalMemberDecision,
    anchor: LongitudinalRecord,
    member: LongitudinalRecord,
    policy: LongitudinalAnchorPolicy,
    *,
    expected_policy_sha256: str,
    expected_authority_head_sha256: str,
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, str],
    linkage_store: ProviderLinkageStore | None,
) -> LongitudinalMemberDecision:
    """Replay an exact D03 decision; never trust a stored decision by itself."""

    actual = decide_longitudinal_member(
        anchor,
        member,
        policy,
        expected_policy_sha256=expected_policy_sha256,
        expected_authority_head_sha256=expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider=(
            expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_store=linkage_store,
    )
    if actual != expected:
        raise LongitudinalDecisionReplayError(
            "stored longitudinal decision does not replay exactly"
        )
    return actual


__all__ = [
    "ALL_COMPARISON_DIMENSIONS",
    "MAX_DECISION_EVIDENCE",
    "ComparisonDimension",
    "ComparisonDimensionValue",
    "DimensionAllowance",
    "DimensionAnchorRule",
    "DimensionDecisionDisposition",
    "DimensionDecisionExplanation",
    "DimensionValueState",
    "LongitudinalAnchorPolicy",
    "LongitudinalComparisonKey",
    "LongitudinalDecisionReplayError",
    "LongitudinalMemberDecision",
    "LongitudinalNextAction",
    "LongitudinalOutcome",
    "LongitudinalPolicyId",
    "LongitudinalReason",
    "LongitudinalRecord",
    "LongitudinalSeriesDecision",
    "capability_sha256",
    "comparison_dimension_value_sha256",
    "composite_contract_sha256",
    "decide_longitudinal_member",
    "decide_longitudinal_series",
    "longitudinal_anchor_policy_sha256",
    "longitudinal_comparison_key_sha256",
    "longitudinal_member_decision_sha256",
    "longitudinal_record_sha256",
    "measurement_definition_sha256",
    "optional_contract_sha256",
    "optional_text_sha256",
    "provider_measurement_id",
    "provider_projection_ref",
    "replay_longitudinal_member_decision",
]
