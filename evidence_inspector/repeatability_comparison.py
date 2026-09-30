"""Fail-closed, descriptive repeatability comparisons for Epic D07.

The evaluator replays the exact D03 decision against current linkage authority,
then permits numeric output only inside one exact, current repeatability envelope.
It never attributes a difference, applies a correction, or assigns clinical meaning.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeVar
from urllib.parse import unquote

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from evidence_inspector.compatibility import (
    CompatibilityContract,
    ExecutionState,
    InformationState,
    ResultId,
)
from evidence_inspector.longitudinal_compatibility import (
    ComparisonDimension,
    DimensionValueState,
    LongitudinalAnchorPolicy,
    LongitudinalMemberDecision,
    LongitudinalOutcome,
    LongitudinalRecord,
    longitudinal_anchor_policy_sha256,
    longitudinal_member_decision_sha256,
    longitudinal_record_sha256,
    replay_longitudinal_member_decision,
)
from evidence_inspector.method_registry import (
    MethodReference,
    QuantityId,
    Sha256,
    UnitId,
    Version,
    canonical_contract_bytes,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from traceback_runner.signing import (
    KeyPurpose,
    SignatureEnvelope,
    SigningError,
    TrustStore,
    verify_signature,
)

MAX_FACTORS = 4


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
        for term in ("donor", "path", "patient", "read", "run", "sample", "sequence")
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


RepeatabilityEvidenceId = _token("repeatability_")
RepeatabilityProtocolId = _token("protocol_")
RepeatabilityAuthorityId = _token("authority_")
UncertaintyMethodId = _token("uncertainty_")
DenominatorSemanticsId = _token("denominator_")
MeasurementEvidenceId = _token("measurement_evidence_")
MeasurementReceiptId = _token("measurement_receipt_")


class RepeatabilityFactor(StrEnum):
    BETWEEN_DAY = "between_day"
    OPERATOR = "operator"
    LOT = "lot"
    PREANALYTICS = "preanalytics"


ALL_REPEATABILITY_FACTORS = tuple(RepeatabilityFactor)


class FactorEnvelope(CompatibilityContract):
    factor: RepeatabilityFactor
    anchor_condition_sha256: Sha256
    member_condition_sha256: Sha256
    condition_policy_sha256: Sha256
    maximum_absolute_contribution: float = Field(ge=0.0)


class RepeatabilityEnvelope(CompatibilityContract):
    """Exact preapproved technical envelope; factor bounds are explanatory only."""

    schema_version: Literal["traceback.repeatability-envelope.v1"]
    evidence_id: RepeatabilityEvidenceId
    evidence_version: Version
    evidence_sha256: Sha256
    protocol_id: RepeatabilityProtocolId
    protocol_version: Version
    protocol_sha256: Sha256
    authority_id: RepeatabilityAuthorityId
    authority_sha256: Sha256
    valid_from: datetime
    valid_through: datetime
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    uncertainty_method_id: UncertaintyMethodId
    uncertainty_method_sha256: Sha256
    denominator_semantics_id: DenominatorSemanticsId
    denominator_semantics_sha256: Sha256
    factor_envelopes: tuple[FactorEnvelope, ...] = Field(
        min_length=MAX_FACTORS, max_length=MAX_FACTORS
    )
    maximum_absolute_delta: float = Field(ge=0.0)
    combination_rule: Literal["preapproved_combined_absolute_delta.v1"]

    @model_validator(mode="after")
    def complete_current_envelope(self) -> RepeatabilityEnvelope:
        if self.valid_from.tzinfo is None or self.valid_through.tzinfo is None:
            raise ValueError("repeatability validity timestamps must be timezone-aware")
        if self.valid_from.astimezone(UTC) >= self.valid_through.astimezone(UTC):
            raise ValueError("repeatability validity window must be increasing")
        if (
            tuple(item.factor for item in self.factor_envelopes)
            != ALL_REPEATABILITY_FACTORS
        ):
            raise ValueError(
                "repeatability envelope must contain every factor in order"
            )
        return self


class ObservationState(StrEnum):
    AVAILABLE = "available"
    MISSING_DRAW = "missing_draw"


class MeasurementCondition(CompatibilityContract):
    factor: RepeatabilityFactor
    condition_sha256: Sha256
    condition_policy_sha256: Sha256


class MeasurementDenominator(CompatibilityContract):
    total_count: int = Field(ge=1, le=1_000_000_000)
    included_count: int = Field(ge=1, le=1_000_000_000)
    excluded_count: int = Field(ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def reconciled(self) -> MeasurementDenominator:
        if self.included_count + self.excluded_count != self.total_count:
            raise ValueError("measurement denominator counts must reconcile exactly")
        return self


class MeasurementEvidencePayload(CompatibilityContract):
    """Canonical numeric evidence signed by the result authority."""

    schema_version: Literal["traceback.comparison-measurement-evidence.v1"]
    evidence_id: MeasurementEvidenceId
    record_sha256: Sha256
    result_id: ResultId
    result_sha256: Sha256
    bundle_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    uncertainty_method_sha256: Sha256
    denominator_semantics_sha256: Sha256
    conditions: tuple[MeasurementCondition, ...] = Field(
        min_length=MAX_FACTORS, max_length=MAX_FACTORS
    )
    state: ObservationState
    value: float | None
    uncertainty_lower: float | None
    uncertainty_upper: float | None
    denominator: MeasurementDenominator | None

    @model_validator(mode="after")
    def coherent_observation(self) -> MeasurementEvidencePayload:
        if tuple(item.factor for item in self.conditions) != ALL_REPEATABILITY_FACTORS:
            raise ValueError(
                "measurement evidence must contain every condition in order"
            )
        numeric = (
            self.value,
            self.uncertainty_lower,
            self.uncertainty_upper,
            self.denominator,
        )
        if self.state == ObservationState.MISSING_DRAW:
            if any(item is not None for item in numeric):
                raise ValueError("missing draw cannot contain numeric output")
            return self
        if any(item is None for item in numeric):
            raise ValueError(
                "available observation requires value, uncertainty, and denominator"
            )
        assert self.value is not None
        assert self.uncertainty_lower is not None
        assert self.uncertainty_upper is not None
        if not self.uncertainty_lower <= self.value <= self.uncertainty_upper:
            raise ValueError("uncertainty interval must contain the value")
        return self


class MeasurementEvidenceReceipt(CompatibilityContract):
    schema_version: Literal["traceback.comparison-measurement-receipt.v1"]
    receipt_id: MeasurementReceiptId
    evidence_sha256: Sha256
    signature: SignatureEnvelope


class ComparisonObservation(CompatibilityContract):
    """Signed evidence plus an exact receipt; never a caller-authored numeric value."""

    schema_version: Literal["traceback.comparison-observation.v1"]
    evidence: MeasurementEvidencePayload
    receipt: MeasurementEvidenceReceipt

    @model_validator(mode="after")
    def receipt_binds_evidence(self) -> ComparisonObservation:
        if self.receipt.evidence_sha256 != measurement_evidence_payload_sha256(
            self.evidence
        ):
            raise ValueError("measurement receipt does not bind exact evidence")
        return self


class ComparisonAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class RepeatabilityClassification(StrEnum):
    EXACT_SAME_VALUE = "exact_same_value"
    NOISY_WITHIN_ENVELOPE = "noisy_within_envelope"
    OUTSIDE_ENVELOPE = "outside_envelope"
    MISSING_DRAW = "missing_draw"
    FAILED_MEASUREMENT = "failed_measurement"
    INSUFFICIENT_MEASUREMENT = "insufficient_measurement"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"
    REQUIRES_REANALYSIS = "requires_reanalysis"
    REGISTERED_BRIDGE = "registered_bridge"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


class RepeatabilityReason(StrEnum):
    EXACT_SAME_VALUE = "exact_same_value"
    WITHIN_PREAPPROVED_ENVELOPE = "within_preapproved_envelope"
    OUTSIDE_PREAPPROVED_ENVELOPE = "outside_preapproved_envelope"
    MISSING_DRAW = "missing_draw"
    MEASUREMENT_FAILED = "measurement_failed"
    MEASUREMENT_INSUFFICIENT = "measurement_insufficient"
    D03_INCOMPATIBLE = "d03_incompatible"
    D03_UNKNOWN = "d03_unknown"
    D03_REANALYSIS_REQUIRED = "d03_reanalysis_required"
    D03_REGISTERED_BRIDGE = "d03_registered_bridge"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_IDENTITY_MISMATCH = "evidence_identity_mismatch"
    EVIDENCE_STALE = "evidence_stale"
    MEASUREMENT_IDENTITY_MISMATCH = "measurement_identity_mismatch"
    MEASUREMENT_SIGNATURE_INVALID = "measurement_signature_invalid"
    FACTOR_TRANSITION_UNREGISTERED = "factor_transition_unregistered"


class RepeatabilityComparison(CompatibilityContract):
    schema_version: Literal["traceback.repeatability-comparison.v1"]
    anchor_record_sha256: Sha256
    member_record_sha256: Sha256
    anchor_policy_sha256: Sha256
    d03_decision_sha256: Sha256
    repeatability_envelope_sha256: Sha256 | None
    repeatability_evidence_sha256: Sha256 | None
    repeatability_protocol_sha256: Sha256 | None
    repeatability_authority_sha256: Sha256 | None
    anchor_measurement_evidence_sha256: Sha256 | None
    member_measurement_evidence_sha256: Sha256 | None
    anchor_measurement_receipt_sha256: Sha256 | None
    member_measurement_receipt_sha256: Sha256 | None
    factor_transition_sha256s: tuple[Sha256, ...] = Field(max_length=MAX_FACTORS)
    evaluated_at: datetime
    availability: ComparisonAvailability
    classification: RepeatabilityClassification
    reason_codes: tuple[RepeatabilityReason, ...] = Field(min_length=1, max_length=4)
    anchor_value: float | None
    member_value: float | None
    delta: float | None
    anchor_uncertainty_lower: float | None
    anchor_uncertainty_upper: float | None
    member_uncertainty_lower: float | None
    member_uncertainty_upper: float | None
    anchor_denominator_count: int | None
    member_denominator_count: int | None
    maximum_absolute_delta: float | None
    trend_allowed: bool
    interpretation: Literal[
        "descriptive_technical_difference_only_no_causal_or_clinical_meaning"
    ]
    automatic_correction_applied: Literal[False]

    @model_validator(mode="after")
    def suppress_unavailable_numbers(self) -> RepeatabilityComparison:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("comparison evaluation timestamp must be timezone-aware")
        numeric = (
            self.anchor_value,
            self.member_value,
            self.delta,
            self.anchor_uncertainty_lower,
            self.anchor_uncertainty_upper,
            self.member_uncertainty_lower,
            self.member_uncertainty_upper,
            self.anchor_denominator_count,
            self.member_denominator_count,
            self.maximum_absolute_delta,
        )
        available = self.availability == ComparisonAvailability.AVAILABLE
        if available != all(item is not None for item in numeric):
            raise ValueError(
                "numeric comparison fields must be all present or all suppressed"
            )
        if self.trend_allowed != available:
            raise ValueError("only an available comparison permits a trend")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=str)):
            raise ValueError("comparison reasons must be uniquely sorted")
        classification_available = self.classification in {
            RepeatabilityClassification.EXACT_SAME_VALUE,
            RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
        }
        if available != classification_available:
            raise ValueError("comparison availability does not match classification")
        evidence_identities = (
            self.repeatability_envelope_sha256,
            self.repeatability_evidence_sha256,
            self.repeatability_protocol_sha256,
            self.repeatability_authority_sha256,
            self.anchor_measurement_evidence_sha256,
            self.member_measurement_evidence_sha256,
            self.anchor_measurement_receipt_sha256,
            self.member_measurement_receipt_sha256,
        )
        if available and any(item is None for item in evidence_identities):
            raise ValueError("available comparison requires exact evidence identities")
        if available != (len(self.factor_transition_sha256s) == MAX_FACTORS):
            raise ValueError("available comparison requires every factor transition")
        if available:
            assert self.anchor_value is not None
            assert self.member_value is not None
            assert self.delta is not None
            assert self.maximum_absolute_delta is not None
            if self.delta != self.member_value - self.anchor_value:
                raise ValueError("comparison delta does not match exact values")
            exact = self.delta == 0.0
            if exact != (
                self.classification == RepeatabilityClassification.EXACT_SAME_VALUE
            ):
                raise ValueError("same-value classification does not match delta")
            if abs(self.delta) > self.maximum_absolute_delta:
                raise ValueError("available delta exceeds repeatability envelope")
        expected_reason = {
            RepeatabilityClassification.EXACT_SAME_VALUE: RepeatabilityReason.EXACT_SAME_VALUE,
            RepeatabilityClassification.NOISY_WITHIN_ENVELOPE: RepeatabilityReason.WITHIN_PREAPPROVED_ENVELOPE,
            RepeatabilityClassification.OUTSIDE_ENVELOPE: RepeatabilityReason.OUTSIDE_PREAPPROVED_ENVELOPE,
            RepeatabilityClassification.MISSING_DRAW: RepeatabilityReason.MISSING_DRAW,
            RepeatabilityClassification.FAILED_MEASUREMENT: RepeatabilityReason.MEASUREMENT_FAILED,
            RepeatabilityClassification.INSUFFICIENT_MEASUREMENT: RepeatabilityReason.MEASUREMENT_INSUFFICIENT,
            RepeatabilityClassification.INCOMPATIBLE: RepeatabilityReason.D03_INCOMPATIBLE,
            RepeatabilityClassification.UNKNOWN: RepeatabilityReason.D03_UNKNOWN,
            RepeatabilityClassification.REQUIRES_REANALYSIS: RepeatabilityReason.D03_REANALYSIS_REQUIRED,
            RepeatabilityClassification.REGISTERED_BRIDGE: RepeatabilityReason.D03_REGISTERED_BRIDGE,
        }.get(self.classification)
        if expected_reason is not None and self.reason_codes != (expected_reason,):
            raise ValueError("comparison classification has invalid exact reason")
        if (
            self.classification == RepeatabilityClassification.EVIDENCE_UNAVAILABLE
            and not (
                set(self.reason_codes)
                <= {
                    RepeatabilityReason.EVIDENCE_MISSING,
                    RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH,
                    RepeatabilityReason.EVIDENCE_STALE,
                    RepeatabilityReason.MEASUREMENT_IDENTITY_MISMATCH,
                    RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID,
                    RepeatabilityReason.FACTOR_TRANSITION_UNREGISTERED,
                }
            )
        ):
            raise ValueError("evidence-unavailable comparison has invalid reason")
        return self


ContractT = TypeVar("ContractT", bound=CompatibilityContract)


def _replay_contract(model: type[ContractT], contract: ContractT) -> ContractT:
    encoded = canonical_contract_bytes(contract)
    replayed = model.model_validate_json(encoded)
    if replayed != contract or canonical_contract_bytes(replayed) != encoded:
        raise ValueError("contract does not replay canonically")
    return replayed


def measurement_evidence_payload_sha256(
    evidence: MeasurementEvidencePayload,
) -> str:
    replayed = _replay_contract(MeasurementEvidencePayload, evidence)
    return hashlib.sha256(canonical_contract_bytes(replayed)).hexdigest()


def measurement_evidence_receipt_sha256(
    receipt: MeasurementEvidenceReceipt,
) -> str:
    replayed = _replay_contract(MeasurementEvidenceReceipt, receipt)
    return hashlib.sha256(canonical_contract_bytes(replayed)).hexdigest()


def repeatability_envelope_sha256(envelope: RepeatabilityEnvelope) -> str:
    replayed = _replay_contract(RepeatabilityEnvelope, envelope)
    return hashlib.sha256(canonical_contract_bytes(replayed)).hexdigest()


def repeatability_comparison_sha256(comparison: RepeatabilityComparison) -> str:
    replayed = _replay_contract(RepeatabilityComparison, comparison)
    return hashlib.sha256(canonical_contract_bytes(replayed)).hexdigest()


def _result(
    *,
    anchor: LongitudinalRecord,
    member: LongitudinalRecord,
    policy: LongitudinalAnchorPolicy,
    decision: LongitudinalMemberDecision,
    evaluated_at: datetime,
    classification: RepeatabilityClassification,
    reasons: set[RepeatabilityReason],
    envelope: RepeatabilityEnvelope | None,
    anchor_observation: ComparisonObservation,
    member_observation: ComparisonObservation,
    available: bool,
    factor_transition_sha256s: tuple[str, ...] = (),
) -> RepeatabilityComparison:
    numeric = available
    anchor_evidence = anchor_observation.evidence
    member_evidence = member_observation.evidence
    anchor_value = anchor_evidence.value if numeric else None
    member_value = member_evidence.value if numeric else None
    assert not numeric or (anchor_value is not None and member_value is not None)
    try:
        anchor_evidence_sha256 = measurement_evidence_payload_sha256(anchor_evidence)
        member_evidence_sha256 = measurement_evidence_payload_sha256(member_evidence)
        anchor_receipt_sha256 = measurement_evidence_receipt_sha256(
            anchor_observation.receipt
        )
        member_receipt_sha256 = measurement_evidence_receipt_sha256(
            member_observation.receipt
        )
    except (AttributeError, TypeError, ValueError):
        anchor_evidence_sha256 = member_evidence_sha256 = None
        anchor_receipt_sha256 = member_receipt_sha256 = None
    comparison = RepeatabilityComparison(
        schema_version="traceback.repeatability-comparison.v1",
        anchor_record_sha256=longitudinal_record_sha256(anchor),
        member_record_sha256=longitudinal_record_sha256(member),
        anchor_policy_sha256=longitudinal_anchor_policy_sha256(policy),
        d03_decision_sha256=longitudinal_member_decision_sha256(decision),
        repeatability_envelope_sha256=(
            repeatability_envelope_sha256(envelope) if envelope else None
        ),
        repeatability_evidence_sha256=(envelope.evidence_sha256 if envelope else None),
        repeatability_protocol_sha256=(envelope.protocol_sha256 if envelope else None),
        repeatability_authority_sha256=(
            envelope.authority_sha256 if envelope else None
        ),
        anchor_measurement_evidence_sha256=anchor_evidence_sha256,
        member_measurement_evidence_sha256=member_evidence_sha256,
        anchor_measurement_receipt_sha256=anchor_receipt_sha256,
        member_measurement_receipt_sha256=member_receipt_sha256,
        factor_transition_sha256s=factor_transition_sha256s,
        evaluated_at=evaluated_at,
        availability=(
            ComparisonAvailability.AVAILABLE
            if available
            else ComparisonAvailability.UNAVAILABLE
        ),
        classification=classification,
        reason_codes=tuple(sorted(reasons, key=str)),
        anchor_value=anchor_value,
        member_value=member_value,
        delta=(member_value - anchor_value if numeric else None),
        anchor_uncertainty_lower=(
            anchor_evidence.uncertainty_lower if numeric else None
        ),
        anchor_uncertainty_upper=(
            anchor_evidence.uncertainty_upper if numeric else None
        ),
        member_uncertainty_lower=(
            member_evidence.uncertainty_lower if numeric else None
        ),
        member_uncertainty_upper=(
            member_evidence.uncertainty_upper if numeric else None
        ),
        anchor_denominator_count=(
            anchor_evidence.denominator.included_count
            if numeric and anchor_evidence.denominator
            else None
        ),
        member_denominator_count=(
            member_evidence.denominator.included_count
            if numeric and member_evidence.denominator
            else None
        ),
        maximum_absolute_delta=(
            envelope.maximum_absolute_delta if numeric and envelope else None
        ),
        trend_allowed=available,
        interpretation="descriptive_technical_difference_only_no_causal_or_clinical_meaning",
        automatic_correction_applied=False,
    )
    return _replay_contract(RepeatabilityComparison, comparison)


def compare_repeatability(
    anchor: LongitudinalRecord,
    member: LongitudinalRecord,
    policy: LongitudinalAnchorPolicy,
    decision: LongitudinalMemberDecision,
    anchor_observation: ComparisonObservation,
    member_observation: ComparisonObservation,
    envelope: RepeatabilityEnvelope | None,
    *,
    evaluated_at: datetime,
    expected_policy_sha256: str,
    expected_authority_head_sha256: str,
    expected_linkage_trust_snapshot_sha256_by_provider: dict[str, str],
    linkage_store: ProviderLinkageStore | None,
    result_trust_store: TrustStore,
    expected_envelope_sha256: str,
    expected_evidence_sha256: str,
    expected_protocol_sha256: str,
    expected_repeatability_authority_sha256: str,
) -> RepeatabilityComparison:
    """Produce a descriptive delta only after exact D03 and evidence replay."""

    if evaluated_at.tzinfo is None:
        raise ValueError("evaluation timestamp must be timezone-aware")
    replay_longitudinal_member_decision(
        decision,
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
    common = {
        "anchor": anchor,
        "member": member,
        "policy": policy,
        "decision": decision,
        "evaluated_at": evaluated_at,
        "envelope": envelope,
        "anchor_observation": anchor_observation,
        "member_observation": member_observation,
    }
    try:
        anchor_observation = _replay_contract(ComparisonObservation, anchor_observation)
        member_observation = _replay_contract(ComparisonObservation, member_observation)
    except (AttributeError, TypeError, ValueError):
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH},
            available=False,
        )
    common["anchor_observation"] = anchor_observation
    common["member_observation"] = member_observation
    try:
        for observation in (anchor_observation, member_observation):
            verify_signature(
                canonical_contract_bytes(observation.evidence),
                observation.receipt.signature,
                result_trust_store,
                purpose=KeyPurpose.RESULT,
            )
    except SigningError:
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.MEASUREMENT_SIGNATURE_INVALID},
            available=False,
        )
    measurements = (anchor.measurement, member.measurement)
    if any(item.execution_state == ExecutionState.FAILED for item in measurements):
        return _result(
            **common,
            classification=RepeatabilityClassification.FAILED_MEASUREMENT,
            reasons={RepeatabilityReason.MEASUREMENT_FAILED},
            available=False,
        )
    if any(
        item.execution_state != ExecutionState.COMPLETE
        or item.information_state != InformationState.SUFFICIENT
        for item in measurements
    ):
        return _result(
            **common,
            classification=RepeatabilityClassification.INSUFFICIENT_MEASUREMENT,
            reasons={RepeatabilityReason.MEASUREMENT_INSUFFICIENT},
            available=False,
        )
    if (
        anchor_observation.evidence.state == ObservationState.MISSING_DRAW
        or member_observation.evidence.state == ObservationState.MISSING_DRAW
    ):
        return _result(
            **common,
            classification=RepeatabilityClassification.MISSING_DRAW,
            reasons={RepeatabilityReason.MISSING_DRAW},
            available=False,
        )
    if decision.outcome == LongitudinalOutcome.REQUIRES_REANALYSIS:
        return _result(
            **common,
            classification=RepeatabilityClassification.REQUIRES_REANALYSIS,
            reasons={RepeatabilityReason.D03_REANALYSIS_REQUIRED},
            available=False,
        )
    if decision.outcome == LongitudinalOutcome.REGISTERED_BRIDGE:
        return _result(
            **common,
            classification=RepeatabilityClassification.REGISTERED_BRIDGE,
            reasons={RepeatabilityReason.D03_REGISTERED_BRIDGE},
            available=False,
        )
    if decision.outcome == LongitudinalOutcome.INCOMPATIBLE:
        return _result(
            **common,
            classification=RepeatabilityClassification.INCOMPATIBLE,
            reasons={RepeatabilityReason.D03_INCOMPATIBLE},
            available=False,
        )
    if decision.outcome == LongitudinalOutcome.UNKNOWN:
        return _result(
            **common,
            classification=RepeatabilityClassification.UNKNOWN,
            reasons={RepeatabilityReason.D03_UNKNOWN},
            available=False,
        )
    if envelope is None:
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_MISSING},
            available=False,
        )
    try:
        envelope = _replay_contract(RepeatabilityEnvelope, envelope)
        evidence_digest = repeatability_envelope_sha256(envelope)
    except (AttributeError, TypeError, ValueError):
        common["envelope"] = None
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH},
            available=False,
        )
    common["envelope"] = envelope
    if (
        evidence_digest != expected_envelope_sha256
        or envelope.evidence_sha256 != expected_evidence_sha256
        or envelope.protocol_sha256 != expected_protocol_sha256
        or envelope.authority_sha256 != expected_repeatability_authority_sha256
    ):
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH},
            available=False,
        )
    evaluation_utc = evaluated_at.astimezone(UTC)
    if not (
        envelope.valid_from.astimezone(UTC)
        <= evaluation_utc
        <= envelope.valid_through.astimezone(UTC)
    ):
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_STALE},
            available=False,
        )
    for record, observation in (
        (anchor, anchor_observation),
        (member, member_observation),
    ):
        key = record.comparison_key
        evidence = observation.evidence
        dimensions = {item.dimension: item for item in key.dimensions}
        uncertainty = dimensions[ComparisonDimension.UNCERTAINTY_METHOD]
        denominator = dimensions[ComparisonDimension.DENOMINATOR_SEMANTICS]
        if (
            key.method_ref != envelope.method_ref
            or key.method_definition_sha256 != envelope.method_definition_sha256
            or key.quantity_id != envelope.quantity_id
            or key.unit != envelope.unit
            or uncertainty.state != DimensionValueState.KNOWN
            or uncertainty.content_sha256 != envelope.uncertainty_method_sha256
            or denominator.state != DimensionValueState.KNOWN
            or denominator.content_sha256 != envelope.denominator_semantics_sha256
            or evidence.record_sha256 != longitudinal_record_sha256(record)
            or evidence.result_id != record.measurement.result_id
            or evidence.result_sha256 != record.measurement.result_sha256
            or evidence.bundle_sha256 != record.measurement.bundle_sha256
            or evidence.method_ref != key.method_ref
            or evidence.method_definition_sha256 != key.method_definition_sha256
            or evidence.quantity_id != key.quantity_id
            or evidence.unit != key.unit
            or evidence.uncertainty_method_sha256 != envelope.uncertainty_method_sha256
            or evidence.denominator_semantics_sha256
            != envelope.denominator_semantics_sha256
        ):
            return _result(
                **common,
                classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
                reasons={RepeatabilityReason.MEASUREMENT_IDENTITY_MISMATCH},
                available=False,
            )
        preanalytics = dimensions[ComparisonDimension.PREANALYTICS_POLICY]
        preanalytics_condition = evidence.conditions[
            ALL_REPEATABILITY_FACTORS.index(RepeatabilityFactor.PREANALYTICS)
        ]
        if (
            preanalytics.state != DimensionValueState.KNOWN
            or preanalytics.content_sha256
            != preanalytics_condition.condition_policy_sha256
        ):
            return _result(
                **common,
                classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
                reasons={RepeatabilityReason.MEASUREMENT_IDENTITY_MISMATCH},
                available=False,
            )
    transition_sha256s: list[str] = []
    for factor_envelope, anchor_condition, member_condition in zip(
        envelope.factor_envelopes,
        anchor_observation.evidence.conditions,
        member_observation.evidence.conditions,
        strict=True,
    ):
        if (
            factor_envelope.factor != anchor_condition.factor
            or factor_envelope.factor != member_condition.factor
            or factor_envelope.anchor_condition_sha256
            != anchor_condition.condition_sha256
            or factor_envelope.member_condition_sha256
            != member_condition.condition_sha256
            or factor_envelope.condition_policy_sha256
            != anchor_condition.condition_policy_sha256
            or factor_envelope.condition_policy_sha256
            != member_condition.condition_policy_sha256
        ):
            return _result(
                **common,
                classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
                reasons={RepeatabilityReason.FACTOR_TRANSITION_UNREGISTERED},
                available=False,
            )
        transition_sha256s.append(
            hashlib.sha256(canonical_contract_bytes(factor_envelope)).hexdigest()
        )
    assert anchor_observation.evidence.value is not None
    assert member_observation.evidence.value is not None
    delta = member_observation.evidence.value - anchor_observation.evidence.value
    if delta == 0.0:
        return _result(
            **common,
            classification=RepeatabilityClassification.EXACT_SAME_VALUE,
            reasons={RepeatabilityReason.EXACT_SAME_VALUE},
            available=True,
            factor_transition_sha256s=tuple(transition_sha256s),
        )
    if abs(delta) <= envelope.maximum_absolute_delta:
        return _result(
            **common,
            classification=RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
            reasons={RepeatabilityReason.WITHIN_PREAPPROVED_ENVELOPE},
            available=True,
            factor_transition_sha256s=tuple(transition_sha256s),
        )
    return _result(
        **common,
        classification=RepeatabilityClassification.OUTSIDE_ENVELOPE,
        reasons={RepeatabilityReason.OUTSIDE_PREAPPROVED_ENVELOPE},
        available=False,
    )


__all__ = [
    "ALL_REPEATABILITY_FACTORS",
    "ComparisonAvailability",
    "ComparisonObservation",
    "FactorEnvelope",
    "MeasurementCondition",
    "MeasurementDenominator",
    "MeasurementEvidencePayload",
    "MeasurementEvidenceReceipt",
    "ObservationState",
    "RepeatabilityClassification",
    "RepeatabilityComparison",
    "RepeatabilityEnvelope",
    "RepeatabilityFactor",
    "RepeatabilityReason",
    "compare_repeatability",
    "measurement_evidence_payload_sha256",
    "measurement_evidence_receipt_sha256",
    "repeatability_comparison_sha256",
    "repeatability_envelope_sha256",
]
