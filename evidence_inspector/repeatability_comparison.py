"""Fail-closed, descriptive repeatability comparisons for Epic D07.

The evaluator replays the exact D03 decision against current linkage authority,
then permits numeric output only inside one exact, current repeatability envelope.
It never attributes a difference, applies a correction, or assigns clinical meaning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, TypeVar
from urllib.parse import unquote

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticSerializationError, TzInfo

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
    LongitudinalDecisionReplayError,
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
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from traceback_runner.signing import (
    DevelopmentTrustDocument,
    KeyPurpose,
    PublicTrustedKey,
    SignatureEnvelope,
    SigningError,
    TrustedKey,
    TrustNamespace,
    TrustStore,
    verify_signature,
)

MAX_FACTORS = 4
MAX_RESULT_TRUST_KEYS = 32
MAX_CONTRACT_PRIMITIVE_LENGTH = 4_096
MAX_CONTRACT_INTEGER_ABSOLUTE = (1 << 63) - 1
MAX_CONTRACT_TUPLE_LENGTH = 32
MAX_CONTRACT_GRAPH_NODES = 4_096
MAX_CONTRACT_GRAPH_DEPTH = 64
_PINNED_AUTHORITY_READ_FENCE = ProviderLinkageStore.authority_read_fence


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
ResultSigningKeyId = Annotated[
    str,
    StringConstraints(pattern=r"^dev-result-[0-9a-f]{24}$"),
]


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


class MeasurementEvidenceReceiptClaim(CompatibilityContract):
    """Domain-separated authority claim signed for one exact receipt identity."""

    schema_version: Literal["traceback.comparison-measurement-receipt-claim.v1"]
    receipt_id: MeasurementReceiptId
    evidence_sha256: Sha256


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
    result_trust_sha256: Sha256 | None
    measurement_signing_key_ids: tuple[ResultSigningKeyId, ...] = Field(max_length=2)
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
        if (available and any(item is None for item in numeric)) or (
            not available and any(item is not None for item in numeric)
        ):
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
            self.result_trust_sha256,
        )
        if available and any(item is None for item in evidence_identities):
            raise ValueError("available comparison requires exact evidence identities")
        if available != (len(self.factor_transition_sha256s) == MAX_FACTORS):
            raise ValueError("available comparison requires every factor transition")
        if self.measurement_signing_key_ids != tuple(
            sorted(set(self.measurement_signing_key_ids))
        ):
            raise ValueError("signing key identities must be uniquely sorted")
        if available and not self.measurement_signing_key_ids:
            raise ValueError(
                "available comparison requires exact signing key identities"
            )
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


_TRUSTED_CONTRACT_TYPES = frozenset(
    (
        ComparisonObservation,
        DevelopmentTrustDocument,
        FactorEnvelope,
        MeasurementCondition,
        MeasurementDenominator,
        MeasurementEvidencePayload,
        MeasurementEvidenceReceipt,
        MeasurementEvidenceReceiptClaim,
        MethodReference,
        PublicTrustedKey,
        RepeatabilityComparison,
        RepeatabilityEnvelope,
        SignatureEnvelope,
    )
)
_TRUSTED_ENUM_TYPES = frozenset(
    (
        ComparisonAvailability,
        KeyPurpose,
        ObservationState,
        RepeatabilityClassification,
        RepeatabilityFactor,
        RepeatabilityReason,
        TrustNamespace,
    )
)


def _contract_graph_is_trusted(root: object) -> bool:
    """Reject caller-controlled nested objects without invoking their hooks."""

    stack = [(root, 0)]
    seen: set[int] = set()
    visited_nodes = 0
    while stack:
        value, depth = stack.pop()
        visited_nodes += 1
        if visited_nodes > MAX_CONTRACT_GRAPH_NODES or depth > MAX_CONTRACT_GRAPH_DEPTH:
            return False
        if value is None or type(value) in {bool, float}:
            continue
        if type(value) in {str, bytes}:
            if len(value) > MAX_CONTRACT_PRIMITIVE_LENGTH:
                return False
            continue
        if type(value) is int:
            if (
                not -MAX_CONTRACT_INTEGER_ABSOLUTE
                <= value
                <= MAX_CONTRACT_INTEGER_ABSOLUTE
            ):
                return False
            continue
        value_type = type(value)
        if value_type in _TRUSTED_ENUM_TYPES or (value_type is tuple and not value):
            continue
        if type(value) is datetime:
            timezone = object.__getattribute__(value, "tzinfo")
            if timezone is UTC:
                continue
            if type(timezone) is TzInfo and timezone.utcoffset(None) == timedelta(0):
                continue
            return False
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        if value_type in _TRUSTED_CONTRACT_TYPES:
            try:
                state = object.__getattribute__(value, "__dict__")
            except (AttributeError, TypeError):
                return False
            fields = vars(value_type).get("__pydantic_fields__")
            if (
                type(state) is not dict
                or type(fields) is not dict
                or len(state) != len(fields)
                or any(type(key) is not str for key in state)
                or state.keys() != fields.keys()
            ):
                return False
            stack.extend((state[name], depth + 1) for name in fields)
            continue
        if type(value) is tuple:
            if len(value) > MAX_CONTRACT_TUPLE_LENGTH:
                return False
            stack.extend((item, depth + 1) for item in value)
            continue
        return False
    return True


_CONTRACT_CODECS = MappingProxyType(
    {
        model: (model.__pydantic_serializer__, model.__pydantic_validator__)
        for model in (
            ComparisonObservation,
            DevelopmentTrustDocument,
            FactorEnvelope,
            MeasurementEvidencePayload,
            MeasurementEvidenceReceipt,
            MeasurementEvidenceReceiptClaim,
            RepeatabilityComparison,
            RepeatabilityEnvelope,
        )
    }
)

_COLLECTION_BOUNDS = MappingProxyType(
    {
        ComparisonObservation: (("evidence.conditions", MAX_FACTORS, MAX_FACTORS),),
        DevelopmentTrustDocument: (("keys", 1, MAX_RESULT_TRUST_KEYS),),
        MeasurementEvidencePayload: (("conditions", MAX_FACTORS, MAX_FACTORS),),
        RepeatabilityComparison: (
            ("reason_codes", 1, 4),
            ("measurement_signing_key_ids", 0, 2),
            ("factor_transition_sha256s", 0, MAX_FACTORS),
        ),
        RepeatabilityEnvelope: (("factor_envelopes", MAX_FACTORS, MAX_FACTORS),),
    }
)


def _preflight_collection_bounds(contract: object, expected_type: type[object]) -> None:
    if type(contract) is not expected_type:
        raise TypeError("repeatability contract type is invalid")
    for dotted_path, minimum, maximum in _COLLECTION_BOUNDS.get(expected_type, ()):
        current = contract
        for name in dotted_path.split("."):
            state = object.__getattribute__(current, "__dict__")
            if type(state) is not dict or name not in state:
                raise TypeError("repeatability contract collection is invalid")
            current = state[name]
        if type(current) is not tuple or not minimum <= len(current) <= maximum:
            raise ValueError(
                f"repeatability contract {dotted_path} exceeds its exact bound"
            )


def _exact_contract_bytes(
    contract: object,
    expected_type: type[object],
    serializer: object,
) -> bytes:
    _preflight_collection_bounds(contract, expected_type)
    if not _contract_graph_is_trusted(contract):
        raise TypeError("repeatability contract graph is invalid")
    payload = serializer.to_python(  # type: ignore[attr-defined]
        contract,
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


def _replay_contract(
    model: type[ContractT],
    contract: object,
    _codecs: MappingProxyType = _CONTRACT_CODECS,
) -> ContractT:
    try:
        serializer, validator = _codecs[model]
    except KeyError:
        raise TypeError("repeatability contract type is unsupported") from None
    encoded = _exact_contract_bytes(contract, model, serializer)
    replayed = validator.validate_json(encoded)  # type: ignore[attr-defined]
    if (
        type(replayed) is not model
        or _exact_contract_bytes(replayed, model, serializer) != encoded
    ):
        raise ValueError("contract does not replay canonically")
    return replayed


def measurement_evidence_receipt_signing_bytes(
    receipt_id: str,
    evidence_sha256: str,
) -> bytes:
    claim = MeasurementEvidenceReceiptClaim(
        schema_version="traceback.comparison-measurement-receipt-claim.v1",
        receipt_id=receipt_id,
        evidence_sha256=evidence_sha256,
    )
    return _exact_contract_bytes(
        claim,
        MeasurementEvidenceReceiptClaim,
        _CONTRACT_CODECS[MeasurementEvidenceReceiptClaim][0],
    )


def _bounded_result_trust_document(
    document: object,
) -> DevelopmentTrustDocument:
    _preflight_collection_bounds(document, DevelopmentTrustDocument)
    return _replay_contract(DevelopmentTrustDocument, document)


def result_trust_document_sha256(document: DevelopmentTrustDocument) -> str:
    replayed = _bounded_result_trust_document(document)
    key_ids = tuple(item.key_id for item in replayed.keys)
    if key_ids != tuple(sorted(set(key_ids))):
        raise ValueError("result trust keys must be uniquely sorted")
    return hashlib.sha256(
        _exact_contract_bytes(
            replayed,
            DevelopmentTrustDocument,
            _CONTRACT_CODECS[DevelopmentTrustDocument][0],
        )
    ).hexdigest()


def _result_trust_store(document: DevelopmentTrustDocument) -> TrustStore:
    keys: list[TrustedKey] = []
    for key in document.keys:
        try:
            public_key_bytes = base64.b64decode(key.public_key_base64, validate=True)
        except ValueError as error:
            raise SigningError("invalid result trust public key encoding") from error
        keys.append(
            TrustedKey(
                key_id=key.key_id,
                purpose=key.purpose,
                public_key_bytes=public_key_bytes,
                namespace=key.namespace,
                revoked=key.revoked,
            )
        )
    return TrustStore(tuple(keys))


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be an exact SHA-256 digest")
    return value


def measurement_evidence_payload_sha256(
    evidence: MeasurementEvidencePayload,
) -> str:
    replayed = _replay_contract(MeasurementEvidencePayload, evidence)
    return hashlib.sha256(
        _exact_contract_bytes(
            replayed,
            MeasurementEvidencePayload,
            _CONTRACT_CODECS[MeasurementEvidencePayload][0],
        )
    ).hexdigest()


def measurement_evidence_receipt_sha256(
    receipt: MeasurementEvidenceReceipt,
) -> str:
    replayed = _replay_contract(MeasurementEvidenceReceipt, receipt)
    return hashlib.sha256(
        _exact_contract_bytes(
            replayed,
            MeasurementEvidenceReceipt,
            _CONTRACT_CODECS[MeasurementEvidenceReceipt][0],
        )
    ).hexdigest()


def repeatability_envelope_sha256(envelope: RepeatabilityEnvelope) -> str:
    replayed = _replay_contract(RepeatabilityEnvelope, envelope)
    return hashlib.sha256(
        _exact_contract_bytes(
            replayed,
            RepeatabilityEnvelope,
            _CONTRACT_CODECS[RepeatabilityEnvelope][0],
        )
    ).hexdigest()


def repeatability_comparison_sha256(comparison: RepeatabilityComparison) -> str:
    replayed = _replay_contract(RepeatabilityComparison, comparison)
    return hashlib.sha256(
        _exact_contract_bytes(
            replayed,
            RepeatabilityComparison,
            _CONTRACT_CODECS[RepeatabilityComparison][0],
        )
    ).hexdigest()


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
    anchor_observation: ComparisonObservation | None,
    member_observation: ComparisonObservation | None,
    result_trust_sha256: str,
    measurement_signing_key_ids: tuple[str, ...],
    available: bool,
    factor_transition_sha256s: tuple[str, ...] = (),
) -> RepeatabilityComparison:
    numeric = available
    if numeric and (anchor_observation is None or member_observation is None):
        raise ValueError("available comparison requires exact observations")
    anchor_evidence = anchor_observation.evidence if anchor_observation else None
    member_evidence = member_observation.evidence if member_observation else None
    assert not numeric or (anchor_evidence is not None and member_evidence is not None)
    anchor_value = anchor_evidence.value if numeric and anchor_evidence else None
    member_value = member_evidence.value if numeric and member_evidence else None
    assert not numeric or (anchor_value is not None and member_value is not None)
    try:
        if anchor_evidence is None or member_evidence is None:
            raise ValueError("measurement evidence is unavailable")
        anchor_evidence_sha256 = measurement_evidence_payload_sha256(anchor_evidence)
        member_evidence_sha256 = measurement_evidence_payload_sha256(member_evidence)
        assert anchor_observation is not None and member_observation is not None
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
        result_trust_sha256=result_trust_sha256,
        measurement_signing_key_ids=measurement_signing_key_ids,
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
            anchor_evidence.uncertainty_lower if numeric and anchor_evidence else None
        ),
        anchor_uncertainty_upper=(
            anchor_evidence.uncertainty_upper if numeric and anchor_evidence else None
        ),
        member_uncertainty_lower=(
            member_evidence.uncertainty_lower if numeric and member_evidence else None
        ),
        member_uncertainty_upper=(
            member_evidence.uncertainty_upper if numeric and member_evidence else None
        ),
        anchor_denominator_count=(
            anchor_evidence.denominator.included_count
            if numeric and anchor_evidence and anchor_evidence.denominator
            else None
        ),
        member_denominator_count=(
            member_evidence.denominator.included_count
            if numeric and member_evidence and member_evidence.denominator
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


def _require_held_authority_fence(linkage_store: object) -> None:
    """Require that this thread already holds the store's authority fence."""

    if type(linkage_store) is not ProviderLinkageStore:
        raise LongitudinalDecisionReplayError(
            "repeatability publication requires an exact live authority fence"
        )
    state = object.__getattribute__(linkage_store, "__dict__")
    connection = state.get("_connection") if type(state) is dict else None
    fence_thread = (
        state.get("_authority_fence_thread") if type(state) is dict else None
    )
    # authority_read_fence marks its holder (process, thread) for exactly its
    # body while it holds one BEGIN IMMEDIATE transaction.  Another store
    # transaction (such as fenced_active_snapshot), another thread's fence, and
    # a forked child that inherited the mark do not match.
    if (
        type(fence_thread) is not tuple
        or fence_thread != (os.getpid(), threading.get_ident())
        or type(connection) is not sqlite3.Connection
        or not connection.in_transaction
    ):
        raise LongitudinalDecisionReplayError(
            "repeatability comparison requires the held live authority fence"
        )


def _compare_repeatability(
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
    result_trust_document: DevelopmentTrustDocument,
    expected_result_trust_sha256: str,
    expected_envelope_sha256: str,
    expected_evidence_sha256: str,
    expected_protocol_sha256: str,
    expected_repeatability_authority_sha256: str,
    fence_held: bool,
) -> RepeatabilityComparison:
    if fence_held:
        _require_held_authority_fence(linkage_store)
    if not _contract_graph_is_trusted(evaluated_at):
        raise ValueError(
            "evaluation timestamp must be an exact trusted timezone-aware UTC value"
        )
    expected_result_trust_sha256 = _require_sha256(
        expected_result_trust_sha256, "expected result trust"
    )
    expected_envelope_sha256 = _require_sha256(
        expected_envelope_sha256, "expected repeatability envelope"
    )
    expected_evidence_sha256 = _require_sha256(
        expected_evidence_sha256, "expected repeatability evidence"
    )
    expected_protocol_sha256 = _require_sha256(
        expected_protocol_sha256, "expected repeatability protocol"
    )
    expected_repeatability_authority_sha256 = _require_sha256(
        expected_repeatability_authority_sha256,
        "expected repeatability authority",
    )
    result_trust_document = _bounded_result_trust_document(result_trust_document)
    result_trust_sha256 = result_trust_document_sha256(result_trust_document)
    if result_trust_sha256 != expected_result_trust_sha256:
        raise ValueError("result trust document does not match the independent pin")
    result_trust_store = _result_trust_store(result_trust_document)
    invalid_observation = False
    try:
        anchor_observation = _replay_contract(ComparisonObservation, anchor_observation)
        member_observation = _replay_contract(ComparisonObservation, member_observation)
    except (
        AttributeError,
        PydanticSerializationError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        invalid_observation = True
        anchor_observation = None
        member_observation = None
    invalid_envelope = False
    if envelope is not None:
        try:
            envelope = _replay_contract(RepeatabilityEnvelope, envelope)
        except (
            AttributeError,
            PydanticSerializationError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            invalid_envelope = True
            envelope = None
    decision = replay_longitudinal_member_decision(
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
        "result_trust_sha256": result_trust_sha256,
        "measurement_signing_key_ids": (),
    }
    if invalid_observation:
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH},
            available=False,
        )
    assert anchor_observation is not None and member_observation is not None
    try:
        for observation in (anchor_observation, member_observation):
            verify_signature(
                measurement_evidence_receipt_signing_bytes(
                    observation.receipt.receipt_id,
                    observation.receipt.evidence_sha256,
                ),
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
    common["measurement_signing_key_ids"] = tuple(
        sorted(
            {
                anchor_observation.receipt.signature.key_id,
                member_observation.receipt.signature.key_id,
            }
        )
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
    if decision.outcome not in {
        LongitudinalOutcome.EQUIVALENT,
        LongitudinalOutcome.QUALIFIED_COMPATIBLE,
    }:
        classification, reason = {
            LongitudinalOutcome.REQUIRES_REANALYSIS: (
                RepeatabilityClassification.REQUIRES_REANALYSIS,
                RepeatabilityReason.D03_REANALYSIS_REQUIRED,
            ),
            LongitudinalOutcome.REGISTERED_BRIDGE: (
                RepeatabilityClassification.REGISTERED_BRIDGE,
                RepeatabilityReason.D03_REGISTERED_BRIDGE,
            ),
            LongitudinalOutcome.INCOMPATIBLE: (
                RepeatabilityClassification.INCOMPATIBLE,
                RepeatabilityReason.D03_INCOMPATIBLE,
            ),
            LongitudinalOutcome.UNKNOWN: (
                RepeatabilityClassification.UNKNOWN,
                RepeatabilityReason.D03_UNKNOWN,
            ),
        }[decision.outcome]
        return _result(
            **common,
            classification=classification,
            reasons={reason},
            available=False,
        )
    if invalid_envelope:
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_IDENTITY_MISMATCH},
            available=False,
        )
    if envelope is None:
        return _result(
            **common,
            classification=RepeatabilityClassification.EVIDENCE_UNAVAILABLE,
            reasons={RepeatabilityReason.EVIDENCE_MISSING},
            available=False,
        )
    evidence_digest = repeatability_envelope_sha256(envelope)
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
            hashlib.sha256(
                _exact_contract_bytes(
                    factor_envelope,
                    FactorEnvelope,
                    _CONTRACT_CODECS[FactorEnvelope][0],
                )
            ).hexdigest()
        )
    if (
        type(linkage_store) is not ProviderLinkageStore
        or ProviderLinkageStore.authority_read_fence is not _PINNED_AUTHORITY_READ_FENCE
    ):
        raise LongitudinalDecisionReplayError(
            "repeatability publication requires an exact live authority fence"
        )
    final_fence: AbstractContextManager[object]
    if fence_held:
        _require_held_authority_fence(linkage_store)
        final_fence = nullcontext()
    else:
        final_fence = _PINNED_AUTHORITY_READ_FENCE(linkage_store)
    with final_fence:
        decision = replay_longitudinal_member_decision(
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
        common["decision"] = decision
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
    result_trust_document: DevelopmentTrustDocument,
    expected_result_trust_sha256: str,
    expected_envelope_sha256: str,
    expected_evidence_sha256: str,
    expected_protocol_sha256: str,
    expected_repeatability_authority_sha256: str,
) -> RepeatabilityComparison:
    """Produce a descriptive delta only after exact D03 and evidence replay.

    The final live D03 replay and artifact construction run inside one
    linkage authority fence that this function acquires.
    """

    return _compare_repeatability(
        anchor,
        member,
        policy,
        decision,
        anchor_observation,
        member_observation,
        envelope,
        evaluated_at=evaluated_at,
        expected_policy_sha256=expected_policy_sha256,
        expected_authority_head_sha256=expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider=(
            expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_store=linkage_store,
        result_trust_document=result_trust_document,
        expected_result_trust_sha256=expected_result_trust_sha256,
        expected_envelope_sha256=expected_envelope_sha256,
        expected_evidence_sha256=expected_evidence_sha256,
        expected_protocol_sha256=expected_protocol_sha256,
        expected_repeatability_authority_sha256=(
            expected_repeatability_authority_sha256
        ),
        fence_held=False,
    )


def compare_repeatability_in_fence(
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
    result_trust_document: DevelopmentTrustDocument,
    expected_result_trust_sha256: str,
    expected_envelope_sha256: str,
    expected_evidence_sha256: str,
    expected_protocol_sha256: str,
    expected_repeatability_authority_sha256: str,
) -> RepeatabilityComparison:
    """Already-fenced variant for callers that hold the linkage authority fence.

    The caller must hold ``ProviderLinkageStore.authority_read_fence`` on
    ``linkage_store`` in this thread for the whole call; both D03 replays and
    construction then run under that one fence instead of a nested one, which
    SQLite cannot open.  Without the held fence it raises before any authority
    read.  The comparison contract and every gate are identical to
    ``compare_repeatability``.
    """

    return _compare_repeatability(
        anchor,
        member,
        policy,
        decision,
        anchor_observation,
        member_observation,
        envelope,
        evaluated_at=evaluated_at,
        expected_policy_sha256=expected_policy_sha256,
        expected_authority_head_sha256=expected_authority_head_sha256,
        expected_linkage_trust_snapshot_sha256_by_provider=(
            expected_linkage_trust_snapshot_sha256_by_provider
        ),
        linkage_store=linkage_store,
        result_trust_document=result_trust_document,
        expected_result_trust_sha256=expected_result_trust_sha256,
        expected_envelope_sha256=expected_envelope_sha256,
        expected_evidence_sha256=expected_evidence_sha256,
        expected_protocol_sha256=expected_protocol_sha256,
        expected_repeatability_authority_sha256=(
            expected_repeatability_authority_sha256
        ),
        fence_held=True,
    )


__all__ = [
    "ALL_REPEATABILITY_FACTORS",
    "MAX_RESULT_TRUST_KEYS",
    "ComparisonAvailability",
    "ComparisonObservation",
    "FactorEnvelope",
    "MeasurementCondition",
    "MeasurementDenominator",
    "MeasurementEvidencePayload",
    "MeasurementEvidenceReceipt",
    "MeasurementEvidenceReceiptClaim",
    "ObservationState",
    "RepeatabilityClassification",
    "RepeatabilityComparison",
    "RepeatabilityEnvelope",
    "RepeatabilityFactor",
    "RepeatabilityReason",
    "ResultSigningKeyId",
    "compare_repeatability",
    "compare_repeatability_in_fence",
    "measurement_evidence_payload_sha256",
    "measurement_evidence_receipt_sha256",
    "measurement_evidence_receipt_signing_bytes",
    "repeatability_comparison_sha256",
    "repeatability_envelope_sha256",
    "result_trust_document_sha256",
]
