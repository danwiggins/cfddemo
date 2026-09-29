"""Pure, fail-closed longitudinal measurement compatibility contracts.

Compatibility is a scientific identity decision. It does not authorize
execution, establish qualification, select a provider primary, or mutate any
record. Every decision is bound to exact E01 method and authority identities,
immutable result/bundle digests, and one canonical compatibility policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    MethodDefinition,
    MethodFamily,
    MethodReference,
    QuantityId,
    RegistryContract,
    Sha256,
    UnitId,
    Version,
    canonical_contract_bytes,
    method_definition_sha256,
)

MAX_POLICY_RULES = 64
MAX_ALLOWED_IDENTITIES = 64
MAX_SELECTION_RECORDS = 64
MAX_MISMATCH_KEYS = 32
MAX_MISSING_FIELDS = 16

_RESERVED_PRIVACY_TERMS = {
    "donor",
    "sample",
    "patient",
    "run",
    "read",
    "path",
    "sequence",
}


def _reject_private_token(value: str) -> str:
    segments = set(re.split(r"[^a-z0-9]+", value.lower()))
    if segments & _RESERVED_PRIVACY_TERMS:
        raise ValueError("controlled identifier contains a reserved privacy term")
    if "/" in value or "\\" in value or "://" in value:
        raise ValueError("controlled identifier cannot contain a path or URI")
    return value


def _token(prefix: str) -> Any:
    return Annotated[
        str,
        StringConstraints(
            min_length=len(prefix) + 2,
            max_length=96,
            pattern=rf"^{prefix}[a-z0-9]+(?:_[a-z0-9]+)*$",
        ),
        AfterValidator(_reject_private_token),
    ]


ResultId = _token("result_")
BundleId = _token("bundle_")
PolicyId = _token("policy_")
ResultSchemaId = _token("schema_")
SemanticsId = _token("sem_")
MissingField = Annotated[
    str,
    StringConstraints(
        min_length=10,
        max_length=64,
        pattern=(
            r"^(left|right)\.(reference_asset|grid_asset|atlas_asset|"
            r"panel_asset|normalization_semantics_id|coordinate_semantics_id|"
            r"denominator_semantics_id)$"
        ),
    ),
]


class CompatibilityContract(RegistryContract):
    """Closed immutable contract with strict Python validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
        strict=True,
    )


class CompatibilityOutcome(StrEnum):
    COMPARABLE = "comparable"
    DIFFERENT_QUANTITY = "different_quantity"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class ExecutionState(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"


class InformationState(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class TrustState(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class CompatibilityMismatchKey(StrEnum):
    ATLAS_ASSET = "atlas_asset"
    COORDINATE_SEMANTICS = "coordinate_semantics_id"
    DENOMINATOR_SEMANTICS = "denominator_semantics_id"
    GRID_ASSET = "grid_asset"
    MEASUREMENT_FAMILY = "measurement_family"
    METHOD_DEFINITION = "method_definition_sha256"
    METHOD_ID = "method_id"
    METHOD_VERSION = "method_version"
    NORMALIZATION_SEMANTICS = "normalization_semantics_id"
    PANEL_ASSET = "panel_asset"
    QUANTITY_ID = "quantity_id"
    REFERENCE_ASSET = "reference_asset"
    REGISTERED_POLICY_ID = "registered_policy_id"
    REGISTERED_POLICY_VERSION = "registered_policy_version"
    RESULT_SCHEMA_ID = "result_schema_id"
    RESULT_SCHEMA_VERSION = "result_schema_version"
    UNIT = "unit"


class RemediationCode(StrEnum):
    NONE = "none"
    ALIGN_MEASUREMENT_CONTRACT = "align_measurement_contract"
    PROVIDE_REQUIRED_METADATA = "provide_required_metadata"
    REFRESH_AUTHORITY = "refresh_authority"
    REFRESH_POLICY = "refresh_policy"
    REGISTER_METHOD = "register_method"
    REGISTER_RESULT_SCHEMA = "register_result_schema"
    REPLACE_REVOKED_RESULT = "replace_revoked_result"
    RESOLVE_EXECUTION = "resolve_execution"
    RESOLVE_INFORMATION = "resolve_information"
    SEPARATE_QUANTITY = "separate_quantity"
    VERIFY_RESULT = "verify_result"


class CompatibilityPolicyReference(CompatibilityContract):
    policy_id: PolicyId
    version: Version


class ResultSchemaReference(CompatibilityContract):
    schema_id: ResultSchemaId
    version: Version


class MeasurementCompatibilityKey(CompatibilityContract):
    """Versioned scientific identity used only for compatibility decisions."""

    schema_version: Literal["traceback.measurement-compatibility-key.v1"] = (
        "traceback.measurement-compatibility-key.v1"
    )
    measurement_family: MethodFamily
    quantity_id: QuantityId
    unit: UnitId
    result_schema: ResultSchemaReference
    reference_asset: AssetReference | None
    grid_asset: AssetReference | None
    atlas_asset: AssetReference | None
    panel_asset: AssetReference | None
    normalization_semantics_id: SemanticsId | None
    coordinate_semantics_id: SemanticsId | None
    denominator_semantics_id: SemanticsId | None
    registered_policy: CompatibilityPolicyReference


class VerifiedMeasurementRecord(CompatibilityContract):
    """Immutable result identity plus independent state and E01 authority."""

    schema_version: Literal["traceback.verified-measurement-record.v1"] = (
        "traceback.verified-measurement-record.v1"
    )
    result_id: ResultId
    result_sha256: Sha256
    bundle_id: BundleId
    bundle_sha256: Sha256
    method: MethodDefinition
    method_definition_sha256: Sha256
    current_capability: CurrentMethodCapability
    execution_state: ExecutionState
    information_state: InformationState
    trust_state: TrustState
    compatibility_key: MeasurementCompatibilityKey

    @model_validator(mode="after")
    def bind_method_and_assets(self) -> VerifiedMeasurementRecord:
        if self.method_definition_sha256 != method_definition_sha256(self.method):
            raise ValueError("method definition digest does not match exact method")
        if self.current_capability.method_ref != self.method.method_ref:
            raise ValueError("authority capability method does not match exact method")
        if (
            self.current_capability.method_definition_sha256
            != self.method_definition_sha256
        ):
            raise ValueError("authority capability method digest does not match")
        key = self.compatibility_key
        if (
            key.measurement_family != self.method.family
            or key.quantity_id != self.method.quantity_id
            or key.unit != self.method.unit
        ):
            raise ValueError("compatibility key does not match exact method quantity")
        registered_assets = set(self.method.assets)
        for asset in (
            key.reference_asset,
            key.grid_asset,
            key.atlas_asset,
            key.panel_asset,
        ):
            if asset is not None and asset not in registered_assets:
                raise ValueError(
                    "compatibility key asset is not bound by the exact method"
                )
        return self


_OPTIONAL_DIMENSIONS = (
    CompatibilityMismatchKey.ATLAS_ASSET,
    CompatibilityMismatchKey.COORDINATE_SEMANTICS,
    CompatibilityMismatchKey.DENOMINATOR_SEMANTICS,
    CompatibilityMismatchKey.GRID_ASSET,
    CompatibilityMismatchKey.NORMALIZATION_SEMANTICS,
    CompatibilityMismatchKey.PANEL_ASSET,
    CompatibilityMismatchKey.REFERENCE_ASSET,
)


class MeasurementCompatibilityPolicy(CompatibilityContract):
    schema_version: Literal["traceback.measurement-compatibility-policy.v1"] = (
        "traceback.measurement-compatibility-policy.v1"
    )
    measurement_family: MethodFamily
    quantity_id: QuantityId
    unit: UnitId
    allowed_methods: tuple[MethodReference, ...] = Field(
        min_length=1, max_length=MAX_ALLOWED_IDENTITIES
    )
    allowed_result_schemas: tuple[ResultSchemaReference, ...] = Field(
        min_length=1, max_length=MAX_ALLOWED_IDENTITIES
    )
    required_dimensions: tuple[CompatibilityMismatchKey, ...] = Field(
        default=_OPTIONAL_DIMENSIONS,
        min_length=1,
        max_length=len(_OPTIONAL_DIMENSIONS),
    )
    delta_allowed_when_comparable: bool
    shared_axis_allowed_when_comparable: bool

    @model_validator(mode="after")
    def deterministic_policy(self) -> MeasurementCompatibilityPolicy:
        method_keys = [
            (item.method_id, item.version) for item in self.allowed_methods
        ]
        if method_keys != sorted(method_keys) or len(method_keys) != len(
            set(method_keys)
        ):
            raise ValueError("allowed method references must be uniquely sorted")
        schema_keys = [
            (item.schema_id, item.version)
            for item in self.allowed_result_schemas
        ]
        if schema_keys != sorted(schema_keys) or len(schema_keys) != len(
            set(schema_keys)
        ):
            raise ValueError("allowed result schemas must be uniquely sorted")
        if self.required_dimensions != _OPTIONAL_DIMENSIONS:
            raise ValueError(
                "policy must require every nullable compatibility dimension"
            )
        return self


class CompatibilityPolicy(CompatibilityContract):
    schema_version: Literal["traceback.compatibility-policy.v1"] = (
        "traceback.compatibility-policy.v1"
    )
    policy_id: PolicyId
    version: Version
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0, le=10_000_000)
    measurement_policies: tuple[MeasurementCompatibilityPolicy, ...] = Field(
        min_length=1, max_length=MAX_POLICY_RULES
    )

    @property
    def policy_ref(self) -> CompatibilityPolicyReference:
        return CompatibilityPolicyReference(
            policy_id=self.policy_id,
            version=self.version,
        )

    @model_validator(mode="after")
    def deterministic_rules(self) -> CompatibilityPolicy:
        keys = [
            (item.measurement_family.value, item.quantity_id, item.unit)
            for item in self.measurement_policies
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("measurement policies must be uniquely sorted")
        return self


class CompatibilityRequest(CompatibilityContract):
    schema_version: Literal["traceback.compatibility-request.v1"] = (
        "traceback.compatibility-request.v1"
    )
    left: VerifiedMeasurementRecord
    right: VerifiedMeasurementRecord
    policy: CompatibilityPolicy
    trusted_policy_sha256: Sha256
    trusted_authority_head_sha256: Sha256

    @model_validator(mode="after")
    def distinct_results(self) -> CompatibilityRequest:
        if self.left.result_id == self.right.result_id:
            raise ValueError("compatibility requires two distinct immutable results")
        return self


class BoundMeasurementIdentity(CompatibilityContract):
    result_id: ResultId
    result_sha256: Sha256
    bundle_id: BundleId
    bundle_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    capability_sha256: Sha256
    compatibility_key_sha256: Sha256


class CompatibilityReplayBinding(CompatibilityContract):
    left: BoundMeasurementIdentity
    right: BoundMeasurementIdentity
    authority_head_sha256: Sha256
    policy_sha256: Sha256
    trusted_policy_sha256: Sha256


class CompatibilityDecision(CompatibilityContract):
    schema_version: Literal["traceback.compatibility-decision.v1"] = (
        "traceback.compatibility-decision.v1"
    )
    binding: CompatibilityReplayBinding
    outcome: CompatibilityOutcome
    mismatch_keys: tuple[CompatibilityMismatchKey, ...] = Field(
        max_length=MAX_MISMATCH_KEYS
    )
    missing_fields: tuple[MissingField, ...] = Field(
        max_length=MAX_MISSING_FIELDS
    )
    delta_allowed: bool
    shared_axis_allowed: bool
    remediation_code: RemediationCode
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_decision(self) -> CompatibilityDecision:
        if tuple(self.mismatch_keys) != tuple(
            sorted(self.mismatch_keys, key=str)
        ) or len(self.mismatch_keys) != len(set(self.mismatch_keys)):
            raise ValueError("mismatch keys must be uniquely sorted")
        if tuple(self.missing_fields) != tuple(sorted(self.missing_fields)) or len(
            self.missing_fields
        ) != len(set(self.missing_fields)):
            raise ValueError("missing fields must be uniquely sorted")
        if self.outcome != CompatibilityOutcome.COMPARABLE and (
            self.delta_allowed or self.shared_axis_allowed
        ):
            raise ValueError(
                "non-comparable outcomes cannot allow delta or shared axis"
            )
        if self.outcome == CompatibilityOutcome.UNKNOWN and (
            self.delta_allowed or self.shared_axis_allowed
        ):
            raise ValueError("unknown compatibility must fail closed")
        if self.outcome == CompatibilityOutcome.COMPARABLE:
            if self.mismatch_keys or self.missing_fields:
                raise ValueError("comparable decisions cannot contain mismatches")
            if self.remediation_code != RemediationCode.NONE:
                raise ValueError("comparable decisions cannot require remediation")
        elif self.remediation_code == RemediationCode.NONE:
            raise ValueError("non-comparable decisions require remediation")
        if self.decision_sha256 != _contract_digest(
            self, exclude={"decision_sha256"}
        ):
            raise ValueError("decision digest does not match canonical decision")
        return self


class CompatibilitySelectionRequest(CompatibilityContract):
    schema_version: Literal["traceback.compatibility-selection-request.v1"] = (
        "traceback.compatibility-selection-request.v1"
    )
    anchor_result_id: ResultId
    records: tuple[VerifiedMeasurementRecord, ...] = Field(
        min_length=2, max_length=MAX_SELECTION_RECORDS
    )
    policy: CompatibilityPolicy
    trusted_policy_sha256: Sha256
    trusted_authority_head_sha256: Sha256

    @model_validator(mode="after")
    def deterministic_records(self) -> CompatibilitySelectionRequest:
        ids = [item.result_id for item in self.records]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("selection records must be uniquely sorted")
        if self.anchor_result_id not in ids:
            raise ValueError("selector requires an explicit existing anchor")
        return self


class CompatibilitySelection(CompatibilityContract):
    schema_version: Literal["traceback.compatibility-selection.v1"] = (
        "traceback.compatibility-selection.v1"
    )
    anchor_result_id: ResultId
    selected_result_ids: tuple[ResultId, ...] = Field(
        max_length=MAX_SELECTION_RECORDS
    )
    decisions: tuple[CompatibilityDecision, ...] = Field(
        min_length=1, max_length=MAX_SELECTION_RECORDS - 1
    )
    selection_sha256: Sha256

    @model_validator(mode="after")
    def validate_selection(self) -> CompatibilitySelection:
        if self.selected_result_ids != tuple(sorted(self.selected_result_ids)) or len(
            self.selected_result_ids
        ) != len(set(self.selected_result_ids)):
            raise ValueError("selected result IDs must be uniquely sorted")
        digests = [item.decision_sha256 for item in self.decisions]
        if digests != sorted(digests) or len(digests) != len(set(digests)):
            raise ValueError("selection decisions must be uniquely sorted")
        if self.selection_sha256 != _contract_digest(
            self, exclude={"selection_sha256"}
        ):
            raise ValueError("selection digest does not match canonical selection")
        return self


class CompatibilityContractError(ValueError):
    """Canonical compatibility input or replay failed closed."""


CompatibilityT = TypeVar("CompatibilityT", bound=CompatibilityContract)


def canonical_compatibility_bytes(contract: CompatibilityContract) -> bytes:
    return canonical_contract_bytes(contract)


def compatibility_contract_from_canonical_bytes(
    model: type[CompatibilityT], content: bytes
) -> CompatibilityT:
    """Validate exact canonical JSON bytes, rejecting JSON normalization drift."""

    try:
        contract = model.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise CompatibilityContractError(
            "compatibility contract JSON is invalid"
        ) from exc
    if canonical_compatibility_bytes(contract) != content:
        raise CompatibilityContractError(
            "compatibility contract JSON is not canonical"
        )
    return contract


def _contract_digest(
    contract: CompatibilityContract, *, exclude: set[str] | None = None
) -> str:
    payload = contract.model_dump(mode="json", exclude=exclude or set())
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compatibility_policy_sha256(policy: CompatibilityPolicy) -> str:
    return _contract_digest(policy)


def compatibility_key_sha256(key: MeasurementCompatibilityKey) -> str:
    return _contract_digest(key)


def _capability_sha256(capability: CurrentMethodCapability) -> str:
    return hashlib.sha256(canonical_contract_bytes(capability)).hexdigest()


def _bound_identity(record: VerifiedMeasurementRecord) -> BoundMeasurementIdentity:
    return BoundMeasurementIdentity(
        result_id=record.result_id,
        result_sha256=record.result_sha256,
        bundle_id=record.bundle_id,
        bundle_sha256=record.bundle_sha256,
        method_ref=record.method.method_ref,
        method_definition_sha256=record.method_definition_sha256,
        capability_sha256=_capability_sha256(record.current_capability),
        compatibility_key_sha256=compatibility_key_sha256(
            record.compatibility_key
        ),
    )


def _ordered_records(
    left: VerifiedMeasurementRecord, right: VerifiedMeasurementRecord
) -> tuple[VerifiedMeasurementRecord, VerifiedMeasurementRecord]:
    return tuple(  # type: ignore[return-value]
        sorted(
            (left, right),
            key=lambda item: (
                item.result_id,
                item.result_sha256,
                item.bundle_sha256,
            ),
        )
    )


def _policy_rule(
    policy: CompatibilityPolicy, record: VerifiedMeasurementRecord
) -> MeasurementCompatibilityPolicy | None:
    key = record.compatibility_key
    for rule in policy.measurement_policies:
        if (
            rule.measurement_family == key.measurement_family
            and rule.quantity_id == key.quantity_id
            and rule.unit == key.unit
        ):
            return rule
    return None


_OPTIONAL_FIELD_BY_DIMENSION = {
    CompatibilityMismatchKey.REFERENCE_ASSET: "reference_asset",
    CompatibilityMismatchKey.GRID_ASSET: "grid_asset",
    CompatibilityMismatchKey.ATLAS_ASSET: "atlas_asset",
    CompatibilityMismatchKey.PANEL_ASSET: "panel_asset",
    CompatibilityMismatchKey.NORMALIZATION_SEMANTICS: (
        "normalization_semantics_id"
    ),
    CompatibilityMismatchKey.COORDINATE_SEMANTICS: "coordinate_semantics_id",
    CompatibilityMismatchKey.DENOMINATOR_SEMANTICS: (
        "denominator_semantics_id"
    ),
}


def _missing_fields(
    left: VerifiedMeasurementRecord,
    right: VerifiedMeasurementRecord,
    rules: tuple[MeasurementCompatibilityPolicy | None, ...],
) -> tuple[str, ...]:
    missing: set[str] = set()
    for side, record, rule in zip(
        ("left", "right"), (left, right), rules, strict=True
    ):
        if rule is None:
            continue
        for dimension in rule.required_dimensions:
            field = _OPTIONAL_FIELD_BY_DIMENSION[dimension]
            if getattr(record.compatibility_key, field) is None:
                missing.add(f"{side}.{field}")
    return tuple(sorted(missing))


def _mismatch_keys(
    left: VerifiedMeasurementRecord,
    right: VerifiedMeasurementRecord,
    policy: CompatibilityPolicy,
) -> tuple[CompatibilityMismatchKey, ...]:
    left_key = left.compatibility_key
    right_key = right.compatibility_key
    mismatches: set[CompatibilityMismatchKey] = set()
    comparisons = (
        (
            CompatibilityMismatchKey.MEASUREMENT_FAMILY,
            left_key.measurement_family,
            right_key.measurement_family,
        ),
        (
            CompatibilityMismatchKey.QUANTITY_ID,
            left_key.quantity_id,
            right_key.quantity_id,
        ),
        (CompatibilityMismatchKey.UNIT, left_key.unit, right_key.unit),
        (
            CompatibilityMismatchKey.RESULT_SCHEMA_ID,
            left_key.result_schema.schema_id,
            right_key.result_schema.schema_id,
        ),
        (
            CompatibilityMismatchKey.RESULT_SCHEMA_VERSION,
            left_key.result_schema.version,
            right_key.result_schema.version,
        ),
        (
            CompatibilityMismatchKey.REFERENCE_ASSET,
            left_key.reference_asset,
            right_key.reference_asset,
        ),
        (
            CompatibilityMismatchKey.GRID_ASSET,
            left_key.grid_asset,
            right_key.grid_asset,
        ),
        (
            CompatibilityMismatchKey.ATLAS_ASSET,
            left_key.atlas_asset,
            right_key.atlas_asset,
        ),
        (
            CompatibilityMismatchKey.PANEL_ASSET,
            left_key.panel_asset,
            right_key.panel_asset,
        ),
        (
            CompatibilityMismatchKey.NORMALIZATION_SEMANTICS,
            left_key.normalization_semantics_id,
            right_key.normalization_semantics_id,
        ),
        (
            CompatibilityMismatchKey.COORDINATE_SEMANTICS,
            left_key.coordinate_semantics_id,
            right_key.coordinate_semantics_id,
        ),
        (
            CompatibilityMismatchKey.DENOMINATOR_SEMANTICS,
            left_key.denominator_semantics_id,
            right_key.denominator_semantics_id,
        ),
        (
            CompatibilityMismatchKey.REGISTERED_POLICY_ID,
            left_key.registered_policy.policy_id,
            right_key.registered_policy.policy_id,
        ),
        (
            CompatibilityMismatchKey.REGISTERED_POLICY_VERSION,
            left_key.registered_policy.version,
            right_key.registered_policy.version,
        ),
        (
            CompatibilityMismatchKey.METHOD_ID,
            left.method.method_id,
            right.method.method_id,
        ),
        (
            CompatibilityMismatchKey.METHOD_VERSION,
            left.method.version,
            right.method.version,
        ),
        (
            CompatibilityMismatchKey.METHOD_DEFINITION,
            left.method_definition_sha256,
            right.method_definition_sha256,
        ),
    )
    for mismatch, left_value, right_value in comparisons:
        if left_value != right_value:
            mismatches.add(mismatch)
    for key in (left_key, right_key):
        if key.registered_policy.policy_id != policy.policy_id:
            mismatches.add(CompatibilityMismatchKey.REGISTERED_POLICY_ID)
        if key.registered_policy.version != policy.version:
            mismatches.add(CompatibilityMismatchKey.REGISTERED_POLICY_VERSION)
    return tuple(sorted(mismatches, key=str))


def _decision(
    request: CompatibilityRequest,
    *,
    outcome: CompatibilityOutcome,
    mismatch_keys: tuple[CompatibilityMismatchKey, ...],
    missing_fields: tuple[str, ...] = (),
    delta_allowed: bool = False,
    shared_axis_allowed: bool = False,
    remediation_code: RemediationCode,
) -> CompatibilityDecision:
    left, right = _ordered_records(request.left, request.right)
    policy_sha256 = compatibility_policy_sha256(request.policy)
    payload: dict[str, Any] = {
        "schema_version": "traceback.compatibility-decision.v1",
        "binding": CompatibilityReplayBinding(
            left=_bound_identity(left),
            right=_bound_identity(right),
            authority_head_sha256=request.trusted_authority_head_sha256,
            policy_sha256=policy_sha256,
            trusted_policy_sha256=request.trusted_policy_sha256,
        ),
        "outcome": outcome,
        "mismatch_keys": mismatch_keys,
        "missing_fields": missing_fields,
        "delta_allowed": delta_allowed,
        "shared_axis_allowed": shared_axis_allowed,
        "remediation_code": remediation_code,
    }
    digest_payload = CompatibilityDecision.model_construct(
        **payload, decision_sha256="0" * 64
    )
    return CompatibilityDecision(
        **payload,
        decision_sha256=_contract_digest(
            digest_payload, exclude={"decision_sha256"}
        ),
    )


def decide_compatibility(request: CompatibilityRequest) -> CompatibilityDecision:
    """Return one deterministic decision without selecting or mutating records."""

    left, right = _ordered_records(request.left, request.right)
    rules = (_policy_rule(request.policy, left), _policy_rule(request.policy, right))
    mismatches = _mismatch_keys(left, right, request.policy)
    missing = _missing_fields(left, right, rules)
    if missing:
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            missing_fields=missing,
            remediation_code=RemediationCode.PROVIDE_REQUIRED_METADATA,
        )

    actual_policy_sha256 = compatibility_policy_sha256(request.policy)
    if request.trusted_policy_sha256 != actual_policy_sha256 or any(
        record.compatibility_key.registered_policy != request.policy.policy_ref
        for record in (left, right)
    ):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.REFRESH_POLICY,
        )
    if (
        request.policy.authority_head_sha256
        != request.trusted_authority_head_sha256
        or any(
            record.current_capability.authority_head_sha256
            != request.trusted_authority_head_sha256
            or record.current_capability.authority_revision
            != request.policy.authority_revision
            for record in (left, right)
        )
    ):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.REFRESH_AUTHORITY,
        )

    if any(record.trust_state == TrustState.REVOKED for record in (left, right)):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.REPLACE_REVOKED_RESULT,
        )
    if any(
        record.execution_state != ExecutionState.COMPLETE
        for record in (left, right)
    ):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.RESOLVE_EXECUTION,
        )
    if any(
        record.information_state != InformationState.SUFFICIENT
        for record in (left, right)
    ):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.RESOLVE_INFORMATION,
        )
    if any(record.trust_state != TrustState.VERIFIED for record in (left, right)):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.VERIFY_RESULT,
        )

    if any(rule is None for rule in rules):
        return _decision(
            request,
            outcome=CompatibilityOutcome.UNKNOWN,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.REGISTER_METHOD,
        )
    assert rules[0] is not None and rules[1] is not None
    for record, rule in zip((left, right), rules, strict=True):
        if record.method.method_ref not in rule.allowed_methods:
            return _decision(
                request,
                outcome=CompatibilityOutcome.UNKNOWN,
                mismatch_keys=mismatches,
                remediation_code=RemediationCode.REGISTER_METHOD,
            )
        if record.compatibility_key.result_schema not in rule.allowed_result_schemas:
            return _decision(
                request,
                outcome=CompatibilityOutcome.UNKNOWN,
                mismatch_keys=mismatches,
                remediation_code=RemediationCode.REGISTER_RESULT_SCHEMA,
            )

    if left.compatibility_key.quantity_id != right.compatibility_key.quantity_id:
        return _decision(
            request,
            outcome=CompatibilityOutcome.DIFFERENT_QUANTITY,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.SEPARATE_QUANTITY,
        )
    if mismatches:
        return _decision(
            request,
            outcome=CompatibilityOutcome.INCOMPATIBLE,
            mismatch_keys=mismatches,
            remediation_code=RemediationCode.ALIGN_MEASUREMENT_CONTRACT,
        )
    return _decision(
        request,
        outcome=CompatibilityOutcome.COMPARABLE,
        mismatch_keys=(),
        delta_allowed=rules[0].delta_allowed_when_comparable,
        shared_axis_allowed=rules[0].shared_axis_allowed_when_comparable,
        remediation_code=RemediationCode.NONE,
    )


def replay_compatibility_decision(
    request: CompatibilityRequest, decision: CompatibilityDecision
) -> CompatibilityDecision:
    expected = decide_compatibility(request)
    if decision != expected:
        raise CompatibilityContractError(
            "compatibility decision does not match canonical replay"
        )
    return decision


def select_compatible_records(
    request: CompatibilitySelectionRequest,
) -> CompatibilitySelection:
    """Select only records comparable to an explicit anchor; infer nothing."""

    by_id = {item.result_id: item for item in request.records}
    anchor = by_id[request.anchor_result_id]
    decisions = []
    selected = [anchor.result_id]
    for record in request.records:
        if record.result_id == anchor.result_id:
            continue
        decision = decide_compatibility(
            CompatibilityRequest(
                left=anchor,
                right=record,
                policy=request.policy,
                trusted_policy_sha256=request.trusted_policy_sha256,
                trusted_authority_head_sha256=(
                    request.trusted_authority_head_sha256
                ),
            )
        )
        decisions.append(decision)
        if decision.outcome == CompatibilityOutcome.COMPARABLE:
            selected.append(record.result_id)
    decisions_tuple = tuple(sorted(decisions, key=lambda item: item.decision_sha256))
    payload: dict[str, Any] = {
        "schema_version": "traceback.compatibility-selection.v1",
        "anchor_result_id": anchor.result_id,
        "selected_result_ids": tuple(sorted(selected)),
        "decisions": decisions_tuple,
    }
    digest_payload = CompatibilitySelection.model_construct(
        **payload, selection_sha256="0" * 64
    )
    return CompatibilitySelection(
        **payload,
        selection_sha256=_contract_digest(
            digest_payload, exclude={"selection_sha256"}
        ),
    )


__all__ = [
    "CompatibilityContractError",
    "CompatibilityDecision",
    "CompatibilityMismatchKey",
    "CompatibilityOutcome",
    "CompatibilityPolicy",
    "CompatibilityPolicyReference",
    "CompatibilityRequest",
    "CompatibilitySelection",
    "CompatibilitySelectionRequest",
    "ExecutionState",
    "InformationState",
    "MeasurementCompatibilityKey",
    "MeasurementCompatibilityPolicy",
    "RemediationCode",
    "ResultSchemaReference",
    "TrustState",
    "VerifiedMeasurementRecord",
    "canonical_compatibility_bytes",
    "compatibility_contract_from_canonical_bytes",
    "compatibility_key_sha256",
    "compatibility_policy_sha256",
    "decide_compatibility",
    "replay_compatibility_decision",
    "select_compatible_records",
]
