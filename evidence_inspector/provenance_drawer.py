"""Framework-independent provenance and method-difference read model.

The drawer consumes the E01 method registry, E02 asset authorization, E04
catalog, and E05 compatibility contracts. It emits only aggregate controlled
identities. It never accepts paths, source identifiers, sequence, or inferred
authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import unquote

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from evidence_inspector.compatibility import (
    CompatibilityContract,
    CompatibilityContractError,
    CompatibilityDecision,
    CompatibilityMismatchKey,
    CompatibilityOutcome,
    CompatibilityRequest,
    RemediationCode,
    VerifiedMeasurementRecord,
    replay_compatibility_decision,
)
from evidence_inspector.method_registry import (
    AssetReference as MethodAssetReference,
    AuthorityScope,
    MethodReference,
    Sha256,
    canonical_contract_bytes,
)
from evidence_inspector.result_catalog import CatalogResultRef
from traceback_runner.assets import AssetVerification, IntegrityStatus
from traceback_runner.qualification import (
    AssetAuthorizationDecision,
    AssetLifecycleStatus,
    AuthorityFailure,
    AuthorityStatus,
    QualificationBinding,
    QualificationTrustPolicy,
    ReleaseAuthorityHead,
    ReleaseEvidenceEnvelope,
    verify_release_asset_authorization,
)
from traceback_runner.release_evidence import (
    AssetStatus,
    DigestDomain,
    domain_digest,
)
from traceback_runner.serialization import (
    canonical_json_bytes,
    sha256_bytes,
)
from traceback_runner.signing import (
    DevelopmentTrustDocument,
    development_trust_document_bytes,
    load_development_trust,
)

MAX_ASSETS = 16
MAX_FILTERS = 16
MAX_LIMITATIONS = 16
MAX_DISPLAY_LENGTH = 512
# Derived from the two bounded sides: each signed E02 proof is capped at 48 KiB,
# each filter/limitation at 1 KiB, plus 128 KiB for fixed replay contracts/rows.
MAX_CANONICAL_DRAWER_BYTES = 128 * 1024 + 2 * (
    MAX_ASSETS * 48 * 1024 + MAX_FILTERS * 1024 + MAX_LIMITATIONS * 1024
)

_RESERVED_PRIVACY_TERMS = {
    "donor",
    "patient",
    "sample",
    "run",
    "read",
    "path",
    "sequence",
}
_SAFE_RESERVED_PREFIX_LEXEMES = {
    "pathology",
    "readiness",
    "readout",
    "runner",
    "runtime",
    "sampled",
    "sequencer",
}
_SEQUENCE = re.compile(r"(?i)^[acgturyswkmbdhvn\s._-]{12,}$")


def _reject_private_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    lowered = normalized.lower()
    if (
        "/" in normalized
        or "\\" in normalized
        or "://" in lowered
        or ".." in normalized
        or lowered.startswith(("file:", "data:", "http:", "https:"))
    ):
        raise ValueError("drawer text cannot contain a path or URI")
    if _SEQUENCE.fullmatch(normalized):
        raise ValueError("drawer text cannot contain sequence-like content")
    for segment in re.split(r"[^a-z0-9]+", lowered):
        if segment in _SAFE_RESERVED_PREFIX_LEXEMES:
            continue
        if any(segment.startswith(term) for term in _RESERVED_PRIVACY_TERMS):
            raise ValueError("drawer text contains a reserved privacy term")
    return value


def _controlled_id(prefix: str) -> Any:
    return Annotated[
        str,
        StringConstraints(
            min_length=len(prefix) + 2,
            max_length=96,
            pattern=rf"^{prefix}[a-z0-9]+(?:_[a-z0-9]+)*$",
        ),
        AfterValidator(_reject_private_text),
    ]


DenominatorId = _controlled_id("denom_")
CountId = _controlled_id("count_")
FilterId = _controlled_id("filter_")
LimitationId = _controlled_id("limit_")
SemanticsId = _controlled_id("sem_")
ResultId = Annotated[
    str,
    StringConstraints(pattern=r"^result_[0-9a-f]{40}$"),
]
BundleId = _controlled_id("bundle_")
AssetId = _controlled_id("asset_")
Version = Annotated[
    str,
    StringConstraints(max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$"),
]
DisplayText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_DISPLAY_LENGTH
    ),
    AfterValidator(_reject_private_text),
]


class DrawerError(ValueError):
    """A provenance input, replay, or canonical read model failed closed."""


class CountRole(StrEnum):
    TOTAL = "total"
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"


class ComparisonFieldKey(StrEnum):
    MEASUREMENT = "measurement"
    BUNDLE = "bundle"
    METHOD_DEFINITION = "method_definition"
    CAPABILITY_AUTHORITY = "capability_authority"
    ASSETS = "assets"
    DENOMINATOR = "denominator"
    COUNTS = "counts"
    FILTERS = "filters"
    LIMITATIONS = "limitations"
    COMPATIBILITY_DECISION = "compatibility_decision"


VISIBLE_FIELD_ORDER = tuple(ComparisonFieldKey)


class DifferenceState(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class DenominatorEvidence(CompatibilityContract):
    schema_version: Literal["traceback.drawer-denominator.v1"] = (
        "traceback.drawer-denominator.v1"
    )
    denominator_id: DenominatorId
    semantics_id: SemanticsId
    definition_sha256: Sha256
    total_count: int = Field(strict=True, ge=0, le=10**15)


class CountEvidence(CompatibilityContract):
    schema_version: Literal["traceback.drawer-count.v1"] = (
        "traceback.drawer-count.v1"
    )
    count_id: CountId
    role: CountRole
    value: int = Field(strict=True, ge=0, le=10**15)
    denominator_sha256: Sha256


class FilterEvidence(CompatibilityContract):
    schema_version: Literal["traceback.drawer-filter.v1"] = (
        "traceback.drawer-filter.v1"
    )
    filter_id: FilterId
    version: Version
    definition_sha256: Sha256
    denominator_sha256: Sha256
    input_count: int = Field(strict=True, ge=0, le=10**15)
    retained_count: int = Field(strict=True, ge=0, le=10**15)
    excluded_count: int = Field(strict=True, ge=0, le=10**15)

    @model_validator(mode="after")
    def reconcile_counts(self) -> FilterEvidence:
        if self.input_count != self.retained_count + self.excluded_count:
            raise ValueError("filter counts do not reconcile")
        return self


class LimitationEvidence(CompatibilityContract):
    schema_version: Literal["traceback.drawer-limitation.v1"] = (
        "traceback.drawer-limitation.v1"
    )
    limitation_id: LimitationId
    version: Version
    statement_sha256: Sha256
    method_definition_sha256: Sha256


class MeasurementValue(CompatibilityContract):
    schema_version: Literal["traceback.drawer-measurement-value.v1"] = (
        "traceback.drawer-measurement-value.v1"
    )
    numeric_value: float
    display_value: DisplayText
    quantity_id: str = Field(pattern=r"^qty_[a-z0-9]+(?:_[a-z0-9]+)*$")
    unit: str = Field(pattern=r"^unit_[a-z0-9]+(?:_[a-z0-9]+)*$")

    @model_validator(mode="after")
    def canonical_display(self) -> MeasurementValue:
        unit_label = self.unit.removeprefix("unit_").replace("_", " ")
        if self.display_value != f"{self.numeric_value:.12g} {unit_label}":
            raise ValueError("measurement display does not match exact numeric value")
        return self


class BoundAssetEvidence(CompatibilityContract):
    schema_version: Literal["traceback.drawer-bound-asset.v1"] = (
        "traceback.drawer-bound-asset.v1"
    )
    method_asset: MethodAssetReference
    verification: AssetVerification
    authorization: AssetAuthorizationDecision
    release_envelope: ReleaseEvidenceEnvelope
    trust_document: DevelopmentTrustDocument
    role_policy: QualificationTrustPolicy
    authority_head: ReleaseAuthorityHead
    expected_binding: QualificationBinding

    @model_validator(mode="after")
    def exact_active_asset(self) -> BoundAssetEvidence:
        method = self.method_asset
        verification = self.verification
        authorization = self.authorization
        if (
            verification.asset_id != method.asset_id
            or verification.version != method.version
            or verification.content_sha256 != method.content_sha256
            or authorization.asset_id != method.asset_id
            or authorization.asset_version != method.version
        ):
            raise ValueError("asset proof does not match the exact method asset")
        if (
            not verification.installed
            or verification.integrity_status != IntegrityStatus.VALID
            or verification.authority_status != AuthorityStatus.VERIFIED.value
            or verification.lifecycle_status != AssetLifecycleStatus.ACTIVE.value
            or verification.authority_failure != AuthorityFailure.NONE.value
            or not verification.registration_matches_current_reference
            or verification.registered_reference_sha256 is None
            or verification.registered_reference_sha256
            != verification.current_reference_sha256
        ):
            raise ValueError("asset verification is incomplete, stale, or revoked")
        if (
            authorization.authority_status != AuthorityStatus.VERIFIED
            or authorization.lifecycle_status != AssetLifecycleStatus.ACTIVE
            or authorization.failure != AuthorityFailure.NONE
            or authorization.authorized_reference is None
            or authorization.asset_reference_sha256
            != verification.current_reference_sha256
        ):
            raise ValueError("asset authorization is incomplete, stale, or revoked")
        if authorization.asset_reference_sha256 != domain_digest(
            DigestDomain.ASSET_REFERENCE, authorization.authorized_reference
        ):
            raise ValueError("asset authorization reference digest does not match")
        content = authorization.authorized_reference.content
        if (
            authorization.authorized_reference.lifecycle.status
            != AssetStatus.ACTIVE
            or content.asset_id != method.asset_id
            or content.version != method.version
            or content.content_sha256 != method.content_sha256
            or verification.content_size_bytes != content.content_size_bytes
            or verification.content_sha256 != content.content_sha256
        ):
            raise ValueError("authorized asset reference does not match method asset")
        for controlled in (
            authorization.release_id,
            authorization.release_version,
            authorization.authorized_reference.provenance.source_authority,
            authorization.authorized_reference.provenance.license_id,
        ):
            _reject_private_text(controlled)
        if self.authorization != self.replay_authorization(
            self.authorization.verified_as_of
        ):
            raise ValueError("asset authorization does not replay from signed evidence")
        return self

    def replay_authorization(
        self, evaluated_at: datetime | None
    ) -> AssetAuthorizationDecision:
        if evaluated_at is None:
            raise ValueError("asset authorization has no verification time")
        trust = load_development_trust(
            development_trust_document_bytes(self.trust_document)
        )
        return verify_release_asset_authorization(
            self.release_envelope,
            trust,
            self.role_policy,
            self.authority_head,
            expected_binding=self.expected_binding,
            expected_package_sha256=self.authorization.package_sha256,
            expected_asset_id=self.method_asset.asset_id,
            expected_asset_version=self.method_asset.version,
            expected_asset_reference_sha256=self.authorization.asset_reference_sha256,
            now=evaluated_at,
        )


class SideEvidenceInput(CompatibilityContract):
    schema_version: Literal["traceback.drawer-side-input.v1"] = (
        "traceback.drawer-side-input.v1"
    )
    catalog_result: CatalogResultRef
    measurement: VerifiedMeasurementRecord
    assets: tuple[BoundAssetEvidence, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    denominator: DenominatorEvidence
    counts: tuple[CountEvidence, ...] = Field(min_length=3, max_length=3)
    filters: tuple[FilterEvidence, ...] = Field(
        min_length=1, max_length=MAX_FILTERS
    )
    limitations: tuple[LimitationEvidence, ...] = Field(
        min_length=1, max_length=MAX_LIMITATIONS
    )
    measurement_value: MeasurementValue
    evidence_payload_sha256: Sha256

    @model_validator(mode="after")
    def exact_side_evidence(self) -> SideEvidenceInput:
        catalog = self.catalog_result
        measurement = self.measurement
        capability = measurement.current_capability
        if (
            catalog.result_id != measurement.result_id
            or catalog.bundle_sha256 != measurement.bundle_sha256
            or catalog.method_ref != measurement.method.method_ref
            or catalog.method_definition_sha256
            != measurement.method_definition_sha256
            or catalog.registry_sha256 != capability.registry_sha256
            or catalog.registry_version != capability.registry_version
            or catalog.authority_head_sha256 != capability.authority_head_sha256
            or catalog.authority_revision != capability.authority_revision
            or catalog.authority_scope != capability.authority_scope
            or catalog.capability_as_of != capability.as_of
            or catalog.qualification_state.value
            != capability.qualification_state.value
            or catalog.display_role != capability.display_role
            or catalog.research_inspectable != capability.research_inspectable
            or catalog.current_provider_eligible
            != capability.current_provider_eligible
        ):
            raise ValueError(
                "catalog result does not match exact measurement authority"
            )
        for controlled in (catalog.bundle_record_id, catalog.workflow_release_id):
            _reject_private_text(controlled)

        _validate_side_scientific_evidence(self)
        return self


def _validate_side_scientific_evidence(side: Any) -> None:
    measurement = side.measurement
    expected_assets = tuple(
        (item.asset_id, item.version, item.content_sha256)
        for item in measurement.method.assets
    )
    observed_assets = tuple(
        (
            item.method_asset.asset_id,
            item.method_asset.version,
            item.method_asset.content_sha256,
        )
        for item in side.assets
    )
    if observed_assets != tuple(sorted(observed_assets)):
        raise ValueError("asset proofs must be uniquely sorted")
    if observed_assets != expected_assets:
        raise ValueError("asset proofs must exactly cover method assets")

    denominator_sha256 = _digest(side.denominator)
    semantics = measurement.compatibility_key.denominator_semantics_id
    if semantics is None or side.denominator.semantics_id != semantics:
        raise ValueError("denominator semantics do not match compatibility key")
    roles = tuple(item.role for item in side.counts)
    if roles != tuple(CountRole):
        raise ValueError("counts must contain total, eligible, excluded in order")
    count_ids = tuple(item.count_id for item in side.counts)
    if len(count_ids) != len(set(count_ids)):
        raise ValueError("count IDs must be unique across roles")
    if any(item.denominator_sha256 != denominator_sha256 for item in side.counts):
        raise ValueError("count is not bound to exact denominator")
    values = {item.role: item.value for item in side.counts}
    if (
        values[CountRole.TOTAL] != side.denominator.total_count
        or values[CountRole.TOTAL]
        != values[CountRole.ELIGIBLE] + values[CountRole.EXCLUDED]
    ):
        raise ValueError("side counts do not reconcile to denominator")
    filter_keys = [(item.filter_id, item.version) for item in side.filters]
    if filter_keys != sorted(filter_keys) or len(filter_keys) != len(
        set(filter_keys)
    ):
        raise ValueError("filters must be uniquely sorted")
    if any(
        item.denominator_sha256 != denominator_sha256
        or item.input_count != side.denominator.total_count
        for item in side.filters
    ):
        raise ValueError("filter does not bind the exact denominator")
    limitation_keys = [
        (item.limitation_id, item.version) for item in side.limitations
    ]
    if limitation_keys != sorted(limitation_keys) or len(limitation_keys) != len(
        set(limitation_keys)
    ):
        raise ValueError("limitations must be uniquely sorted")
    if any(
        item.method_definition_sha256 != measurement.method_definition_sha256
        for item in side.limitations
    ):
        raise ValueError("limitation does not bind exact method definition")
    if (
        side.measurement_value.quantity_id != measurement.method.quantity_id
        or side.measurement_value.unit != measurement.method.unit
    ):
        raise ValueError("measurement value does not match method quantity")
    expected_payload_sha256 = _side_evidence_payload_sha256(side)
    if side.evidence_payload_sha256 != expected_payload_sha256:
        raise ValueError("evidence payload digest does not match exact evidence")
    if measurement.result_sha256 != expected_payload_sha256:
        raise ValueError("evidence payload is not bound to verified result")


class SideEvidenceReplay(CompatibilityContract):
    schema_version: Literal["traceback.drawer-side-replay.v1"] = (
        "traceback.drawer-side-replay.v1"
    )
    bundle_manifest_sha256: Sha256
    measurement: VerifiedMeasurementRecord
    assets: tuple[BoundAssetEvidence, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    denominator: DenominatorEvidence
    counts: tuple[CountEvidence, ...] = Field(min_length=3, max_length=3)
    filters: tuple[FilterEvidence, ...] = Field(
        min_length=1, max_length=MAX_FILTERS
    )
    limitations: tuple[LimitationEvidence, ...] = Field(
        min_length=1, max_length=MAX_LIMITATIONS
    )
    measurement_value: MeasurementValue
    evidence_payload_sha256: Sha256

    @model_validator(mode="after")
    def exact_replay_evidence(self) -> SideEvidenceReplay:
        _validate_side_scientific_evidence(self)
        return self


class DrawerBuildRequest(CompatibilityContract):
    schema_version: Literal["traceback.provenance-drawer-request.v1"] = (
        "traceback.provenance-drawer-request.v1"
    )
    evaluated_at: datetime
    left: SideEvidenceInput
    right: SideEvidenceInput
    compatibility_request: CompatibilityRequest
    compatibility_decision: CompatibilityDecision

    @model_validator(mode="after")
    def exact_request(self) -> DrawerBuildRequest:
        _validate_request_replay(self)
        for side in (self.left, self.right):
            if not side.catalog_result.research_inspectable:
                raise ValueError("non-inspectable result cannot render a drawer")
        return self


def _validate_request_replay(request: Any) -> None:
    _require_utc_second(request.evaluated_at)
    if request.left.measurement != request.compatibility_request.left:
        raise ValueError("left evidence does not match compatibility request")
    if request.right.measurement != request.compatibility_request.right:
        raise ValueError("right evidence does not match compatibility request")
    try:
        replay_compatibility_decision(
            request.compatibility_request, request.compatibility_decision
        )
    except CompatibilityContractError as exc:
        raise ValueError("compatibility decision replay failed") from exc
    if request.compatibility_decision.outcome == CompatibilityOutcome.UNKNOWN:
        raise ValueError("unknown compatibility cannot render a drawer")
    for side in (request.left, request.right):
        if side.measurement.current_capability.as_of > request.evaluated_at:
            raise ValueError("method authority is not yet valid")
        for asset in side.assets:
            verified = asset.authorization.verified_as_of
            fresh_until = asset.authorization.fresh_until
            if (
                verified is None
                or fresh_until is None
                or verified > request.evaluated_at
                or request.evaluated_at >= fresh_until
            ):
                raise ValueError("asset authority is stale or not yet valid")
            if asset.authorization != asset.replay_authorization(request.evaluated_at):
                raise ValueError("asset authorization replay failed at evaluated_at")


class DrawerReplayRequest(CompatibilityContract):
    schema_version: Literal["traceback.provenance-drawer-replay.v1"] = (
        "traceback.provenance-drawer-replay.v1"
    )
    evaluated_at: datetime
    left: SideEvidenceReplay
    right: SideEvidenceReplay
    compatibility_request: CompatibilityRequest
    compatibility_decision: CompatibilityDecision

    @model_validator(mode="after")
    def exact_replay(self) -> DrawerReplayRequest:
        _validate_request_replay(self)
        return self


class AssetLineageIdentity(CompatibilityContract):
    asset_id: AssetId
    version: Version
    content_sha256: Sha256
    asset_reference_sha256: Sha256
    release_package_sha256: Sha256
    release_id: str
    release_version: str
    content_size_bytes: int = Field(strict=True, gt=0)
    authorization_sha256: Sha256
    verification_sha256: Sha256
    verified_as_of: datetime
    fresh_until: datetime

    @model_validator(mode="after")
    def exact_freshness_window(self) -> AssetLineageIdentity:
        _require_utc_second(self.verified_as_of)
        _require_utc_second(self.fresh_until)
        if self.fresh_until <= self.verified_as_of:
            raise ValueError("asset freshness window is invalid")
        return self


class DrawerSideIdentity(CompatibilityContract):
    schema_version: Literal["traceback.drawer-side-identity.v1"] = (
        "traceback.drawer-side-identity.v1"
    )
    result_id: ResultId
    result_sha256: Sha256
    bundle_id: BundleId
    bundle_sha256: Sha256
    bundle_manifest_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    capability_sha256: Sha256
    registry_sha256: Sha256
    registry_version: int = Field(strict=True, ge=1)
    authority_head_sha256: Sha256
    authority_revision: int = Field(strict=True, ge=0)
    authority_scope: AuthorityScope
    capability_as_of: datetime
    assets: tuple[AssetLineageIdentity, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    denominator_sha256: Sha256
    count_sha256s: tuple[Sha256, Sha256, Sha256]
    filter_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_FILTERS
    )
    limitation_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_LIMITATIONS
    )
    evidence_payload_sha256: Sha256
    provenance_sha256: Sha256

    @model_validator(mode="after")
    def exact_digest(self) -> DrawerSideIdentity:
        _require_utc_second(self.capability_as_of)
        asset_keys = [(item.asset_id, item.version) for item in self.assets]
        if asset_keys != sorted(asset_keys) or len(asset_keys) != len(
            set(asset_keys)
        ):
            raise ValueError("side assets must be uniquely sorted")
        if self.provenance_sha256 != _digest(
            self, exclude={"provenance_sha256"}
        ):
            raise ValueError("side provenance digest does not match")
        return self


class FieldLineage(CompatibilityContract):
    side_provenance_sha256: Sha256
    bundle_sha256: Sha256
    bundle_manifest_sha256: Sha256
    method_definition_sha256: Sha256
    capability_sha256: Sha256
    registry_sha256: Sha256
    authority_head_sha256: Sha256
    authority_scope: AuthorityScope
    capability_as_of: datetime
    asset_reference_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    asset_verified_as_of: tuple[datetime, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    asset_fresh_until: tuple[datetime, ...] = Field(
        min_length=1, max_length=MAX_ASSETS
    )
    denominator_sha256: Sha256
    count_sha256s: tuple[Sha256, Sha256, Sha256]
    filter_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_FILTERS
    )
    limitation_sha256s: tuple[Sha256, ...] = Field(
        min_length=1, max_length=MAX_LIMITATIONS
    )
    compatibility_decision_sha256: Sha256


class ComparisonFieldValue(CompatibilityContract):
    display_value: DisplayText
    identity_sha256: Sha256


class ComparisonField(CompatibilityContract):
    field: ComparisonFieldKey
    left: ComparisonFieldValue
    right: ComparisonFieldValue
    difference: DifferenceState
    left_lineage: FieldLineage
    right_lineage: FieldLineage

    @model_validator(mode="after")
    def exact_difference(self) -> ComparisonField:
        expected = (
            DifferenceState.UNCHANGED
            if self.left.identity_sha256 == self.right.identity_sha256
            else DifferenceState.CHANGED
        )
        if self.difference != expected:
            raise ValueError("field difference does not match exact identities")
        return self


class ProvenanceDrawer(CompatibilityContract):
    schema_version: Literal["traceback.provenance-drawer.v1"] = (
        "traceback.provenance-drawer.v1"
    )
    evaluated_at: datetime
    left: DrawerSideIdentity
    right: DrawerSideIdentity
    compatibility_outcome: CompatibilityOutcome
    compatibility_decision_sha256: Sha256
    compatibility_mismatch_keys: tuple[CompatibilityMismatchKey, ...] = Field(
        max_length=32
    )
    compatibility_remediation_code: RemediationCode
    replay_request: DrawerReplayRequest
    fields: tuple[ComparisonField, ...] = Field(
        min_length=len(VISIBLE_FIELD_ORDER), max_length=len(VISIBLE_FIELD_ORDER)
    )
    changed_fields: tuple[ComparisonFieldKey, ...] = Field(
        max_length=len(VISIBLE_FIELD_ORDER)
    )
    unchanged_fields: tuple[ComparisonFieldKey, ...] = Field(
        max_length=len(VISIBLE_FIELD_ORDER)
    )
    drawer_sha256: Sha256

    @model_validator(mode="after")
    def exact_complete_drawer(self) -> ProvenanceDrawer:
        _require_utc_second(self.evaluated_at)
        keys = tuple(item.field for item in self.fields)
        if keys != VISIBLE_FIELD_ORDER:
            raise ValueError("drawer fields must use the complete fixed order")
        changed = tuple(
            item.field
            for item in self.fields
            if item.difference == DifferenceState.CHANGED
        )
        unchanged = tuple(
            item.field
            for item in self.fields
            if item.difference == DifferenceState.UNCHANGED
        )
        if self.changed_fields != changed or self.unchanged_fields != unchanged:
            raise ValueError("changed and unchanged fields must exhaust the drawer")
        if self.compatibility_outcome == CompatibilityOutcome.UNKNOWN:
            raise ValueError("unknown compatibility cannot render a drawer")
        if self.compatibility_mismatch_keys != tuple(
            sorted(self.compatibility_mismatch_keys, key=str)
        ) or len(self.compatibility_mismatch_keys) != len(
            set(self.compatibility_mismatch_keys)
        ):
            raise ValueError("compatibility mismatch keys must be uniquely sorted")
        if self.compatibility_outcome == CompatibilityOutcome.COMPARABLE:
            if self.compatibility_mismatch_keys:
                raise ValueError("comparable drawer cannot contain mismatches")
            if self.compatibility_remediation_code != RemediationCode.NONE:
                raise ValueError("comparable drawer cannot require remediation")
        elif self.compatibility_remediation_code == RemediationCode.NONE:
            raise ValueError("non-comparable drawer requires remediation")
        expected = _derive_drawer_payload(self.replay_request)
        for name in (
            "evaluated_at",
            "left",
            "right",
            "compatibility_outcome",
            "compatibility_decision_sha256",
            "compatibility_mismatch_keys",
            "compatibility_remediation_code",
            "fields",
            "changed_fields",
            "unchanged_fields",
        ):
            if getattr(self, name) != expected[name]:
                raise ValueError("drawer does not replay from exact source evidence")
        for field in self.fields:
            _validate_lineage(
                field.left_lineage,
                self.left,
                self.compatibility_decision_sha256,
            )
            _validate_lineage(
                field.right_lineage,
                self.right,
                self.compatibility_decision_sha256,
            )
        if self.drawer_sha256 != _digest(self, exclude={"drawer_sha256"}):
            raise ValueError("drawer digest does not match canonical read model")
        return self


def _require_utc_second(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("evaluated_at must be aware UTC")
    if value.microsecond != 0:
        raise ValueError("evaluated_at must use whole-second precision")


def _digest(value: Any, *, exclude: set[str] | None = None) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude=exclude or set())
    value = _jsonable(value)
    return sha256_bytes(canonical_json_bytes(value))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _side_evidence_payload_sha256(side: SideEvidenceInput) -> str:
    """Digest every caller-visible scientific value in the verified result."""

    return _digest(
        {
            "schema_version": "traceback.drawer-evidence-payload.v1",
            "measurement_value": side.measurement_value,
            "denominator": side.denominator,
            "counts": side.counts,
            "filters": side.filters,
            "limitations": side.limitations,
        }
    )


def _asset_identity(asset: BoundAssetEvidence) -> AssetLineageIdentity:
    method = asset.method_asset
    authorization = asset.authorization
    verification = asset.verification
    assert authorization.verified_as_of is not None
    assert authorization.fresh_until is not None
    return AssetLineageIdentity(
        asset_id=method.asset_id,
        version=method.version,
        content_sha256=method.content_sha256,
        asset_reference_sha256=authorization.asset_reference_sha256,
        release_package_sha256=authorization.package_sha256,
        release_id=authorization.release_id,
        release_version=authorization.release_version,
        content_size_bytes=(
            authorization.authorized_reference.content.content_size_bytes
        ),
        authorization_sha256=_digest(authorization),
        verification_sha256=_digest(verification),
        verified_as_of=authorization.verified_as_of,
        fresh_until=authorization.fresh_until,
    )


def _side_identity(side: SideEvidenceInput | SideEvidenceReplay) -> DrawerSideIdentity:
    measurement = side.measurement
    capability = measurement.current_capability
    payload: dict[str, Any] = {
        "schema_version": "traceback.drawer-side-identity.v1",
        "result_id": measurement.result_id,
        "result_sha256": measurement.result_sha256,
        "bundle_id": measurement.bundle_id,
        "bundle_sha256": measurement.bundle_sha256,
        "bundle_manifest_sha256": (
            side.catalog_result.bundle_manifest_sha256
            if isinstance(side, SideEvidenceInput)
            else side.bundle_manifest_sha256
        ),
        "method_ref": measurement.method.method_ref,
        "method_definition_sha256": measurement.method_definition_sha256,
        "capability_sha256": hashlib.sha256(
            canonical_contract_bytes(capability)
        ).hexdigest(),
        "registry_sha256": capability.registry_sha256,
        "registry_version": capability.registry_version,
        "authority_head_sha256": capability.authority_head_sha256,
        "authority_revision": capability.authority_revision,
        "authority_scope": capability.authority_scope,
        "capability_as_of": capability.as_of,
        "assets": tuple(_asset_identity(item) for item in side.assets),
        "denominator_sha256": _digest(side.denominator),
        "count_sha256s": tuple(_digest(item) for item in side.counts),
        "filter_sha256s": tuple(_digest(item) for item in side.filters),
        "limitation_sha256s": tuple(_digest(item) for item in side.limitations),
        "evidence_payload_sha256": side.evidence_payload_sha256,
    }
    seed = DrawerSideIdentity.model_construct(
        **payload, provenance_sha256="0" * 64
    )
    return DrawerSideIdentity(
        **payload,
        provenance_sha256=_digest(seed, exclude={"provenance_sha256"}),
    )


def _lineage(
    side: DrawerSideIdentity, decision_sha256: str
) -> FieldLineage:
    return FieldLineage(
        side_provenance_sha256=side.provenance_sha256,
        bundle_sha256=side.bundle_sha256,
        bundle_manifest_sha256=side.bundle_manifest_sha256,
        method_definition_sha256=side.method_definition_sha256,
        capability_sha256=side.capability_sha256,
        registry_sha256=side.registry_sha256,
        authority_head_sha256=side.authority_head_sha256,
        authority_scope=side.authority_scope,
        capability_as_of=side.capability_as_of,
        asset_reference_sha256s=tuple(
            item.asset_reference_sha256 for item in side.assets
        ),
        asset_verified_as_of=tuple(item.verified_as_of for item in side.assets),
        asset_fresh_until=tuple(item.fresh_until for item in side.assets),
        denominator_sha256=side.denominator_sha256,
        count_sha256s=side.count_sha256s,
        filter_sha256s=side.filter_sha256s,
        limitation_sha256s=side.limitation_sha256s,
        compatibility_decision_sha256=decision_sha256,
    )


def _validate_lineage(
    lineage: FieldLineage,
    side: DrawerSideIdentity,
    decision_sha256: str,
) -> None:
    if lineage != _lineage(side, decision_sha256):
        raise ValueError("visible field lineage does not resolve exact identities")


def _field_value(display: str, identity: Any) -> ComparisonFieldValue:
    return ComparisonFieldValue(
        display_value=display,
        identity_sha256=(identity if isinstance(identity, str) and re.fullmatch(
            r"[0-9a-f]{64}", identity
        ) else _digest(identity)),
    )


def _field_sources(
    side: SideEvidenceInput | SideEvidenceReplay,
    identity: DrawerSideIdentity,
    decision: CompatibilityDecision,
) -> dict[ComparisonFieldKey, ComparisonFieldValue]:
    method = side.measurement.method
    counts = {item.role: item.value for item in side.counts}
    return {
        ComparisonFieldKey.MEASUREMENT: _field_value(
            side.measurement_value.display_value,
            side.measurement_value,
        ),
        ComparisonFieldKey.BUNDLE: _field_value(
            identity.bundle_id, identity.bundle_sha256
        ),
        ComparisonFieldKey.METHOD_DEFINITION: _field_value(
            f"{method.method_id}@{method.version}",
            identity.method_definition_sha256,
        ),
        ComparisonFieldKey.CAPABILITY_AUTHORITY: _field_value(
            (
                f"registry {identity.registry_version}; "
                f"authority {identity.authority_revision}"
            ),
            {
                "capability_sha256": identity.capability_sha256,
                "registry_sha256": identity.registry_sha256,
                "authority_head_sha256": identity.authority_head_sha256,
            },
        ),
        ComparisonFieldKey.ASSETS: _field_value(
            ", ".join(f"{item.asset_id}@{item.version}" for item in identity.assets),
            identity.assets,
        ),
        ComparisonFieldKey.DENOMINATOR: _field_value(
            side.denominator.denominator_id,
            identity.denominator_sha256,
        ),
        ComparisonFieldKey.COUNTS: _field_value(
            (
                f"total {counts[CountRole.TOTAL]}; "
                f"eligible {counts[CountRole.ELIGIBLE]}; "
                f"excluded {counts[CountRole.EXCLUDED]}"
            ),
            identity.count_sha256s,
        ),
        ComparisonFieldKey.FILTERS: _field_value(
            ", ".join(f"{item.filter_id}@{item.version}" for item in side.filters),
            identity.filter_sha256s,
        ),
        ComparisonFieldKey.LIMITATIONS: _field_value(
            ", ".join(
                f"{item.limitation_id}@{item.version}"
                for item in side.limitations
            ),
            identity.limitation_sha256s,
        ),
        ComparisonFieldKey.COMPATIBILITY_DECISION: _field_value(
            decision.outcome.value,
            decision.decision_sha256,
        ),
    }


def _derive_drawer_payload(
    request: DrawerBuildRequest | DrawerReplayRequest,
) -> dict[str, Any]:
    left = _side_identity(request.left)
    right = _side_identity(request.right)
    decision = request.compatibility_decision
    left_values = _field_sources(request.left, left, decision)
    right_values = _field_sources(request.right, right, decision)
    fields = tuple(
        ComparisonField(
            field=key,
            left=left_values[key],
            right=right_values[key],
            difference=(
                DifferenceState.UNCHANGED
                if left_values[key].identity_sha256
                == right_values[key].identity_sha256
                else DifferenceState.CHANGED
            ),
            left_lineage=_lineage(left, decision.decision_sha256),
            right_lineage=_lineage(right, decision.decision_sha256),
        )
        for key in VISIBLE_FIELD_ORDER
    )
    payload: dict[str, Any] = {
        "schema_version": "traceback.provenance-drawer.v1",
        "evaluated_at": request.evaluated_at,
        "left": left,
        "right": right,
        "compatibility_outcome": decision.outcome,
        "compatibility_decision_sha256": decision.decision_sha256,
        "compatibility_mismatch_keys": decision.mismatch_keys,
        "compatibility_remediation_code": decision.remediation_code,
        "replay_request": request,
        "fields": fields,
        "changed_fields": tuple(
            item.field for item in fields if item.difference == DifferenceState.CHANGED
        ),
        "unchanged_fields": tuple(
            item.field
            for item in fields
            if item.difference == DifferenceState.UNCHANGED
        ),
    }
    return payload


def build_provenance_drawer(request: DrawerBuildRequest) -> ProvenanceDrawer:
    """Build one deterministic read model after exact replay and freshness checks."""

    try:
        request = DrawerBuildRequest.model_validate_json(
            canonical_json_bytes(request)
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise DrawerError("drawer build request is invalid") from exc
    replay_request = DrawerReplayRequest(
        evaluated_at=request.evaluated_at,
        left=_side_replay(request.left),
        right=_side_replay(request.right),
        compatibility_request=request.compatibility_request,
        compatibility_decision=request.compatibility_decision,
    )
    payload = _derive_drawer_payload(replay_request)
    seed = ProvenanceDrawer.model_construct(**payload, drawer_sha256="0" * 64)
    return ProvenanceDrawer(
        **payload,
        drawer_sha256=_digest(seed, exclude={"drawer_sha256"}),
    )


def _side_replay(side: SideEvidenceInput) -> SideEvidenceReplay:
    return SideEvidenceReplay(
        bundle_manifest_sha256=side.catalog_result.bundle_manifest_sha256,
        measurement=side.measurement,
        assets=side.assets,
        denominator=side.denominator,
        counts=side.counts,
        filters=side.filters,
        limitations=side.limitations,
        measurement_value=side.measurement_value,
        evidence_payload_sha256=side.evidence_payload_sha256,
    )


DrawerT = TypeVar("DrawerT", bound=CompatibilityContract)


def canonical_drawer_bytes(value: CompatibilityContract) -> bytes:
    return canonical_json_bytes(value)


def drawer_from_canonical_bytes(
    model: type[DrawerT], content: bytes
) -> DrawerT:
    limit = (
        MAX_CANONICAL_DRAWER_BYTES
        if model is ProvenanceDrawer
        else MAX_CANONICAL_DRAWER_BYTES // 2
    )
    if len(content) > limit:
        raise DrawerError("drawer contract bytes exceed the schema input limit")
    try:
        raw = json.loads(content)
        _validate_untrusted_strings(raw)
        parsed = model.model_validate_json(content)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise DrawerError("drawer contract bytes are invalid or non-canonical") from exc
    if canonical_drawer_bytes(parsed) != content:
        raise DrawerError("drawer contract bytes are invalid or non-canonical")
    return parsed


def _validate_untrusted_strings(value: Any, *, field_name: str | None = None) -> None:
    """Apply privacy checks before typed parsing; only typed digest slots are exempt."""

    if isinstance(value, dict):
        for key, item in value.items():
            _validate_untrusted_strings(item, field_name=key)
    elif isinstance(value, list):
        for item in value:
            _validate_untrusted_strings(item, field_name=field_name)
    elif isinstance(value, str):
        digest_slot = field_name is not None and (
            field_name.endswith("_sha256") or field_name.endswith("_sha256s")
        )
        typed_crypto_slot = field_name in {"signature_base64", "public_key_base64"}
        if not digest_slot and not typed_crypto_slot:
            _reject_private_text(value)


__all__ = [
    "BoundAssetEvidence",
    "ComparisonField",
    "ComparisonFieldKey",
    "CountEvidence",
    "CountRole",
    "DenominatorEvidence",
    "DifferenceState",
    "DrawerBuildRequest",
    "DrawerError",
    "DrawerReplayRequest",
    "DrawerSideIdentity",
    "FieldLineage",
    "FilterEvidence",
    "LimitationEvidence",
    "MeasurementValue",
    "MAX_CANONICAL_DRAWER_BYTES",
    "ProvenanceDrawer",
    "SideEvidenceInput",
    "SideEvidenceReplay",
    "VISIBLE_FIELD_ORDER",
    "build_provenance_drawer",
    "canonical_drawer_bytes",
    "drawer_from_canonical_bytes",
]
