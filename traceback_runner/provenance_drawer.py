"""Framework-independent provenance and method-difference read model.

The read model is derived only from verified E02 report bundles and exact E05
records/decisions, which already bind E01 method and authority identities.  It
contains aggregate synthetic/development evidence only and grants no authority.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from evidence_inspector.compatibility import (
    CompatibilityDecision,
    CompatibilityOutcome,
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_key_sha256,
    replay_compatibility_decision,
)
from evidence_inspector.copy_number_qc import ReadAccounting, DosageQcScanPolicy
from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    MethodDefinition,
    MethodFamily,
    QualificationState,
    canonical_contract_bytes,
)

from .report_bundles import (
    QUALIFICATION_STATUS,
    RESULT_SCHEMA,
    DevelopmentReportManifest,
    VerifiedDevelopmentReportBundle,
)
from .serialization import canonical_json_bytes, sha256_bytes

MAX_FILTERS = 16
MAX_LIMITATIONS = 8
_RESULT_SCHEMA_REFERENCE = ResultSchemaReference(
    schema_id="schema_copy_number_dosage_qc",
    version="2.0.0",
)
_ALIAS_RESERVED = re.compile(
    r"(?:donor|sample|patient|run|read|path|sequence|[0-9a-f]{32,}|[/\\]|://)",
    re.IGNORECASE,
)


def _validate_alias(value: str) -> str:
    if _ALIAS_RESERVED.search(value):
        raise ValueError("display alias contains a reserved privacy token")
    return value


OpaqueDisplayAlias = Annotated[
    str,
    StringConstraints(
        min_length=10,
        max_length=40,
        pattern=r"^alias_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    ),
    AfterValidator(_validate_alias),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProvenanceDrawerError(ValueError):
    """Source evidence is incomplete, stale, private, or does not replay."""


class DrawerContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
        strict=True,
    )


class DifferenceFieldKey(StrEnum):
    ASSETS = "assets"
    BUNDLE = "bundle"
    CAPABILITY_AUTHORITY = "capability_authority"
    COMPATIBILITY_DECISION = "compatibility_decision"
    COMPATIBILITY_KEY = "compatibility_key"
    DENOMINATOR_COUNTS = "denominator_counts"
    FILTERS = "filters"
    LIMITATIONS = "limitations"
    METHOD_DEFINITION = "method_definition"


class DifferenceState(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


_FIELD_ORDER = tuple(sorted(DifferenceFieldKey, key=str))


class DrawerSource(DrawerContract):
    """Exact verified source; aliases are controlled display tokens only."""

    display_alias: OpaqueDisplayAlias
    bundle: VerifiedDevelopmentReportBundle
    record: VerifiedMeasurementRecord


class ProvenanceDrawerRequest(DrawerContract):
    schema_version: Literal["traceback.provenance-drawer-request.v1"] = (
        "traceback.provenance-drawer-request.v1"
    )
    left: DrawerSource
    right: DrawerSource
    compatibility_request: CompatibilityRequest
    compatibility_decision: CompatibilityDecision

    @model_validator(mode="after")
    def distinct_exact_sources(self) -> ProvenanceDrawerRequest:
        if self.left.display_alias == self.right.display_alias:
            raise ValueError("comparison display aliases must be distinct")
        if self.left.record != self.compatibility_request.left:
            raise ValueError("left source is not the exact compatibility input")
        if self.right.record != self.compatibility_request.right:
            raise ValueError("right source is not the exact compatibility input")
        return self


class ComparisonSideEvidence(DrawerContract):
    schema_version: Literal["traceback.comparison-side-evidence.v1"] = (
        "traceback.comparison-side-evidence.v1"
    )
    display_alias: OpaqueDisplayAlias
    result_id: str
    result_sha256: Sha256
    bundle_id: str
    bundle_sha256: Sha256
    report_id: str
    report_manifest_sha256: Sha256
    report_manifest: DevelopmentReportManifest
    result_schema: ResultSchemaReference
    method: MethodDefinition
    method_definition_sha256: Sha256
    capability: CurrentMethodCapability
    capability_sha256: Sha256
    assets: tuple[AssetReference, ...] = Field(max_length=16)
    compatibility_key: MeasurementCompatibilityKey
    compatibility_key_sha256: Sha256
    denominator_counts: ReadAccounting
    scan_policy: DosageQcScanPolicy
    filters: tuple[str, ...] = Field(min_length=1, max_length=MAX_FILTERS)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=MAX_LIMITATIONS)
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> ComparisonSideEvidence:
        if self.evidence_sha256 != _model_digest(self, exclude={"evidence_sha256"}):
            raise ValueError("side evidence digest does not match canonical evidence")
        return self


class DifferenceField(DrawerContract):
    field: DifferenceFieldKey
    state: DifferenceState
    left_value_sha256: Sha256
    right_value_sha256: Sha256
    left_evidence_sha256: Sha256
    right_evidence_sha256: Sha256
    compatibility_decision_sha256: Sha256

    @model_validator(mode="after")
    def state_matches_values(self) -> DifferenceField:
        expected = (
            DifferenceState.UNCHANGED
            if self.left_value_sha256 == self.right_value_sha256
            else DifferenceState.CHANGED
        )
        if self.state != expected:
            raise ValueError("difference state does not match exact field values")
        return self


class ProvenanceDifferenceReadModel(DrawerContract):
    schema_version: Literal["traceback.provenance-difference-read-model.v1"] = (
        "traceback.provenance-difference-read-model.v1"
    )
    left: ComparisonSideEvidence
    right: ComparisonSideEvidence
    compatibility_decision: CompatibilityDecision
    compatibility_decision_sha256: Sha256
    fields: tuple[DifferenceField, ...] = Field(
        min_length=len(_FIELD_ORDER), max_length=len(_FIELD_ORDER)
    )
    changed_fields: tuple[DifferenceFieldKey, ...]
    unchanged_fields: tuple[DifferenceFieldKey, ...]
    read_model_sha256: Sha256

    @model_validator(mode="after")
    def complete_partition_and_digest(self) -> ProvenanceDifferenceReadModel:
        keys = tuple(item.field for item in self.fields)
        if keys != _FIELD_ORDER:
            raise ValueError("difference fields must be the exact sorted v1 field set")
        changed = tuple(
            item.field for item in self.fields if item.state == DifferenceState.CHANGED
        )
        unchanged = tuple(
            item.field for item in self.fields if item.state == DifferenceState.UNCHANGED
        )
        if self.changed_fields != changed or self.unchanged_fields != unchanged:
            raise ValueError("changed and unchanged fields must exactly partition fields")
        if self.compatibility_decision_sha256 != self.compatibility_decision.decision_sha256:
            raise ValueError("compatibility decision identity does not match")
        if self.read_model_sha256 != _model_digest(self, exclude={"read_model_sha256"}):
            raise ValueError("read-model digest does not match canonical content")
        return self


ModelT = TypeVar("ModelT", bound=BaseModel)


def _model_digest(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    return sha256_bytes(
        canonical_json_bytes(model.model_dump(mode="json", exclude=exclude or set()))
    )


def _bundle_digest(bundle: VerifiedDevelopmentReportBundle) -> str:
    return sha256_bytes(canonical_json_bytes(bundle.manifest))


def _capability_digest(capability: CurrentMethodCapability) -> str:
    return hashlib.sha256(canonical_contract_bytes(capability)).hexdigest()


def _validate_source(source: DrawerSource) -> None:
    record = source.record
    bundle = source.bundle
    if record.execution_state != ExecutionState.COMPLETE:
        raise ProvenanceDrawerError("incomplete execution cannot populate provenance")
    if record.information_state != InformationState.SUFFICIENT:
        raise ProvenanceDrawerError("insufficient information cannot populate provenance")
    if record.trust_state != TrustState.VERIFIED:
        raise ProvenanceDrawerError("unverified or revoked result cannot populate provenance")
    if record.current_capability.qualification_state in {
        None,
        QualificationState.UNKNOWN,
    }:
        raise ProvenanceDrawerError("unknown qualification cannot populate provenance")
    if not record.current_capability.research_inspectable:
        raise ProvenanceDrawerError("disabled capability cannot populate provenance")
    if bundle.result.analysis_status != "complete":
        raise ProvenanceDrawerError("incomplete report bundle cannot populate provenance")
    if bundle.provenance.qualification_status != record.current_capability.qualification_state:
        raise ProvenanceDrawerError("bundle and capability qualification identities differ")
    result_digest = sha256_bytes(canonical_json_bytes(bundle.result))
    if result_digest != record.result_sha256:
        raise ProvenanceDrawerError("record result digest does not match verified bundle")
    if _bundle_digest(bundle) != record.bundle_sha256:
        raise ProvenanceDrawerError("record bundle digest does not match verified manifest")
    if record.compatibility_key.result_schema != _RESULT_SCHEMA_REFERENCE:
        raise ProvenanceDrawerError("result schema identity is unsupported")
    if record.method.family != MethodFamily.COPY_NUMBER:
        raise ProvenanceDrawerError("report bundle requires a copy-number method")
    reference = record.compatibility_key.reference_asset
    if reference is None or (
        reference.content_sha256 != bundle.result.provenance.reference.fasta_sha256
    ):
        raise ProvenanceDrawerError("registered reference asset does not match result")
    if record.compatibility_key.denominator_semantics_id is None:
        raise ProvenanceDrawerError("denominator semantics identity is required")


def _side_evidence(source: DrawerSource) -> ComparisonSideEvidence:
    _validate_source(source)
    bundle = source.bundle
    record = source.record
    payload: dict[str, Any] = {
        "display_alias": source.display_alias,
        "result_id": record.result_id,
        "result_sha256": record.result_sha256,
        "bundle_id": record.bundle_id,
        "bundle_sha256": record.bundle_sha256,
        "report_id": bundle.manifest.report_id,
        "report_manifest_sha256": _bundle_digest(bundle),
        "report_manifest": bundle.manifest,
        "result_schema": record.compatibility_key.result_schema,
        "method": record.method,
        "method_definition_sha256": record.method_definition_sha256,
        "capability": record.current_capability,
        "capability_sha256": _capability_digest(record.current_capability),
        "assets": record.method.assets,
        "compatibility_key": record.compatibility_key,
        "compatibility_key_sha256": compatibility_key_sha256(
            record.compatibility_key
        ),
        "denominator_counts": bundle.result.provenance.read_accounting,
        "scan_policy": bundle.result.provenance.scan_policy,
        "filters": bundle.result.provenance.filters,
        "limitations": bundle.result.limitations,
    }
    draft = ComparisonSideEvidence.model_construct(
        **payload,
        schema_version="traceback.comparison-side-evidence.v1",
        evidence_sha256="0" * 64,
    )
    return ComparisonSideEvidence(
        **payload,
        evidence_sha256=_model_digest(draft, exclude={"evidence_sha256"}),
    )


def _field_value(side: ComparisonSideEvidence, field: DifferenceFieldKey) -> Any:
    if field == DifferenceFieldKey.BUNDLE:
        return {
            "bundle_id": side.bundle_id,
            "bundle_sha256": side.bundle_sha256,
            "report_id": side.report_id,
            "report_manifest_sha256": side.report_manifest_sha256,
            "result_id": side.result_id,
            "result_sha256": side.result_sha256,
            "result_schema": side.result_schema.model_dump(mode="json"),
        }
    if field == DifferenceFieldKey.METHOD_DEFINITION:
        return side.method.model_dump(mode="json")
    if field == DifferenceFieldKey.CAPABILITY_AUTHORITY:
        return side.capability.model_dump(mode="json")
    if field == DifferenceFieldKey.ASSETS:
        return [item.model_dump(mode="json") for item in side.assets]
    if field == DifferenceFieldKey.DENOMINATOR_COUNTS:
        return side.denominator_counts.model_dump(mode="json")
    if field == DifferenceFieldKey.FILTERS:
        return {
            "scan_policy": side.scan_policy.model_dump(mode="json"),
            "filters": side.filters,
        }
    if field == DifferenceFieldKey.COMPATIBILITY_KEY:
        return side.compatibility_key.model_dump(mode="json")
    if field == DifferenceFieldKey.LIMITATIONS:
        return side.limitations
    if field == DifferenceFieldKey.COMPATIBILITY_DECISION:
        return "shared_exact_decision"
    raise AssertionError(f"unhandled difference field: {field}")


def build_provenance_difference_read_model(
    request: ProvenanceDrawerRequest,
) -> ProvenanceDifferenceReadModel:
    """Build a deterministic read model after replaying every source authority."""

    try:
        replay_compatibility_decision(
            request.compatibility_request,
            request.compatibility_decision,
        )
    except Exception as exc:
        raise ProvenanceDrawerError("compatibility decision failed canonical replay") from exc
    if request.compatibility_decision.outcome == CompatibilityOutcome.UNKNOWN:
        raise ProvenanceDrawerError("unknown compatibility fails closed")
    left = _side_evidence(request.left)
    right = _side_evidence(request.right)
    fields = []
    for field in _FIELD_ORDER:
        if field == DifferenceFieldKey.COMPATIBILITY_DECISION:
            left_digest = right_digest = request.compatibility_decision.decision_sha256
        else:
            left_digest = sha256_bytes(canonical_json_bytes(_field_value(left, field)))
            right_digest = sha256_bytes(canonical_json_bytes(_field_value(right, field)))
        fields.append(
            DifferenceField(
                field=field,
                state=(
                    DifferenceState.UNCHANGED
                    if left_digest == right_digest
                    else DifferenceState.CHANGED
                ),
                left_value_sha256=left_digest,
                right_value_sha256=right_digest,
                left_evidence_sha256=left.evidence_sha256,
                right_evidence_sha256=right.evidence_sha256,
                compatibility_decision_sha256=(
                    request.compatibility_decision.decision_sha256
                ),
            )
        )
    changed = tuple(
        item.field for item in fields if item.state == DifferenceState.CHANGED
    )
    unchanged = tuple(
        item.field for item in fields if item.state == DifferenceState.UNCHANGED
    )
    payload: dict[str, Any] = {
        "left": left,
        "right": right,
        "compatibility_decision": request.compatibility_decision,
        "compatibility_decision_sha256": request.compatibility_decision.decision_sha256,
        "fields": tuple(fields),
        "changed_fields": changed,
        "unchanged_fields": unchanged,
    }
    draft = ProvenanceDifferenceReadModel.model_construct(
        **payload,
        schema_version="traceback.provenance-difference-read-model.v1",
        read_model_sha256="0" * 64,
    )
    return ProvenanceDifferenceReadModel(
        **payload,
        read_model_sha256=_model_digest(draft, exclude={"read_model_sha256"}),
    )


def build_synthetic_provenance_fixture(
    *,
    left_bundle: VerifiedDevelopmentReportBundle,
    right_bundle: VerifiedDevelopmentReportBundle,
    compatibility_request: CompatibilityRequest,
    compatibility_decision: CompatibilityDecision,
) -> ProvenanceDifferenceReadModel:
    """Build the deterministic two-panel synthetic fixture used by adapters/tests."""

    return build_provenance_difference_read_model(
        ProvenanceDrawerRequest(
            left=DrawerSource(
                display_alias="alias_alpha_panel",
                bundle=left_bundle,
                record=compatibility_request.left,
            ),
            right=DrawerSource(
                display_alias="alias_beta_panel",
                bundle=right_bundle,
                record=compatibility_request.right,
            ),
            compatibility_request=compatibility_request,
            compatibility_decision=compatibility_decision,
        )
    )


def canonical_provenance_drawer_bytes(model: ProvenanceDifferenceReadModel) -> bytes:
    return canonical_json_bytes(model)


def provenance_drawer_from_canonical_bytes(
    content: bytes,
) -> ProvenanceDifferenceReadModel:
    try:
        model = ProvenanceDifferenceReadModel.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ProvenanceDrawerError("provenance drawer JSON is invalid") from exc
    if canonical_provenance_drawer_bytes(model) != content:
        raise ProvenanceDrawerError("provenance drawer JSON is not canonical")
    return model


def replay_provenance_difference_read_model(
    request: ProvenanceDrawerRequest,
    model: ProvenanceDifferenceReadModel,
) -> ProvenanceDifferenceReadModel:
    expected = build_provenance_difference_read_model(request)
    if model != expected:
        raise ProvenanceDrawerError("provenance drawer does not match source replay")
    return model


__all__ = [
    "ComparisonSideEvidence",
    "DifferenceField",
    "DifferenceFieldKey",
    "DifferenceState",
    "DrawerSource",
    "OpaqueDisplayAlias",
    "ProvenanceDifferenceReadModel",
    "ProvenanceDrawerError",
    "ProvenanceDrawerRequest",
    "build_provenance_difference_read_model",
    "build_synthetic_provenance_fixture",
    "canonical_provenance_drawer_bytes",
    "provenance_drawer_from_canonical_bytes",
    "replay_provenance_difference_read_model",
]
