"""Closed contracts for scientific methods and independent authority records.

Method definitions are immutable scientific identities. Qualification, display,
provider approval, and revocation are separate append-only authority events.
Historical replay never grants current provider eligibility. Current evaluation
requires a caller-trusted authority-head digest and rejects stale snapshots.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
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

MAX_TOOLS = 32
MAX_ASSETS = 64
MAX_METHODS = 128
MAX_METHOD_BINDINGS = 16
MAX_AUTHORITY_RECORDS = 512

_SENSITIVE_TERMS = (
    "donor",
    "sample",
    "patient",
    "run",
    "read",
    "path",
    "sequence",
)


def _reject_sensitive_token(value: str) -> str:
    lowered = value.lower()
    if any(term in lowered for term in _SENSITIVE_TERMS):
        raise ValueError("controlled identifier contains a reserved privacy term")
    return value


def _token(pattern: str) -> Any:
    return Annotated[
        str,
        StringConstraints(min_length=5, max_length=96, pattern=pattern),
        AfterValidator(_reject_sensitive_token),
    ]


RegistryId = _token(r"^registry_[a-z0-9]+(?:_[a-z0-9]+)*$")
MethodId = _token(r"^mth_[a-z0-9]+(?:_[a-z0-9]+)*$")
ToolId = _token(r"^tool_[a-z0-9]+(?:_[a-z0-9]+)*$")
AssetId = _token(r"^asset_[a-z0-9]+(?:_[a-z0-9]+)*$")
QuantityId = _token(r"^qty_[a-z0-9]+(?:_[a-z0-9]+)*$")
UnitId = _token(r"^unit_[a-z0-9]+(?:_[a-z0-9]+)*$")
AuthorityScope = _token(r"^scope_[a-z0-9]+(?:_[a-z0-9]+)*$")
ApprovalRef = _token(r"^approval_[a-z0-9]+(?:_[a-z0-9]+)*$")
QualificationRef = _token(r"^qual_[a-z0-9]+(?:_[a-z0-9]+)*$")
RoleAssignmentRef = _token(r"^role_[a-z0-9]+(?:_[a-z0-9]+)*$")
RevocationRef = _token(r"^revoke_[a-z0-9]+(?:_[a-z0-9]+)*$")
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class RegistryContract(BaseModel):
    """Immutable, closed contract base with finite-number enforcement."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class MethodFamily(StrEnum):
    FRAGMENT_MEASUREMENT = "fragment_measurement"
    CELL_ORIGIN = "cell_origin"
    COPY_NUMBER = "copy_number"
    PROCESSING_SENSITIVITY = "processing_sensitivity"


class QualificationState(StrEnum):
    UNKNOWN = "unknown"
    DEVELOPMENT_UNQUALIFIED = "development_unqualified"
    QUALIFIED = "qualified"


class DisplayRole(StrEnum):
    PROVIDER_PRIMARY = "provider_primary"
    RESEARCH_BASELINE = "research_baseline"
    RESEARCH_CHALLENGER = "research_challenger"
    DISABLED = "disabled"


class RevocationTarget(StrEnum):
    QUALIFICATION = "qualification"
    DISPLAY_ROLE = "display_role"


class MethodReference(RegistryContract):
    method_id: MethodId
    version: Version


class ToolRegistration(RegistryContract):
    tool_id: ToolId
    version: Version
    artifact_sha256: Sha256


class AssetRegistration(RegistryContract):
    asset_id: AssetId
    version: Version
    content_sha256: Sha256


class ToolReference(RegistryContract):
    tool_id: ToolId
    version: Version
    artifact_sha256: Sha256


class AssetReference(RegistryContract):
    asset_id: AssetId
    version: Version
    content_sha256: Sha256


def _require_utc_second(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be an aware UTC timestamp")
    if value.microsecond != 0:
        raise ValueError(f"{label} must use whole-second precision")


class MethodDefinition(RegistryContract):
    """Immutable scientific identity without qualification or display authority."""

    schema_version: Literal["traceback.method-definition.v1"] = (
        "traceback.method-definition.v1"
    )
    method_id: MethodId
    version: Version
    family: MethodFamily
    quantity_id: QuantityId
    unit: UnitId
    parameter_schema_sha256: Sha256
    tools: tuple[ToolReference, ...] = Field(
        min_length=1, max_length=MAX_METHOD_BINDINGS
    )
    assets: tuple[AssetReference, ...] = Field(
        min_length=1, max_length=MAX_METHOD_BINDINGS
    )

    @property
    def method_ref(self) -> MethodReference:
        return MethodReference(method_id=self.method_id, version=self.version)

    @model_validator(mode="after")
    def deterministic_bindings(self) -> MethodDefinition:
        tool_keys = [(item.tool_id, item.version) for item in self.tools]
        if tool_keys != sorted(tool_keys) or len(tool_keys) != len(set(tool_keys)):
            raise ValueError("tool references must be uniquely sorted")
        asset_keys = [(item.asset_id, item.version) for item in self.assets]
        if asset_keys != sorted(asset_keys) or len(asset_keys) != len(set(asset_keys)):
            raise ValueError("asset references must be uniquely sorted")
        return self


class QualificationRecord(RegistryContract):
    schema_version: Literal["traceback.qualification-record.v1"] = (
        "traceback.qualification-record.v1"
    )
    record_ref: QualificationRef
    method_ref: MethodReference
    state: QualificationState
    effective_at: datetime
    approval_ref: ApprovalRef

    @model_validator(mode="after")
    def valid_time(self) -> QualificationRecord:
        _require_utc_second(self.effective_at, "qualification effective_at")
        return self


class DisplayRoleAssignment(RegistryContract):
    schema_version: Literal["traceback.display-role-assignment.v1"] = (
        "traceback.display-role-assignment.v1"
    )
    assignment_ref: RoleAssignmentRef
    method_ref: MethodReference
    display_role: DisplayRole
    authority_scope: AuthorityScope
    effective_at: datetime
    approval_ref: ApprovalRef | None = None

    @model_validator(mode="after")
    def coherent_assignment(self) -> DisplayRoleAssignment:
        _require_utc_second(self.effective_at, "display role effective_at")
        if self.display_role == DisplayRole.PROVIDER_PRIMARY:
            if self.approval_ref is None:
                raise ValueError("provider_primary requires an approval_ref")
        elif self.approval_ref is not None:
            raise ValueError("approval_ref is allowed only for provider_primary")
        return self


class AuthorityRevocation(RegistryContract):
    schema_version: Literal["traceback.authority-revocation.v1"] = (
        "traceback.authority-revocation.v1"
    )
    revocation_ref: RevocationRef
    target: RevocationTarget
    qualification_record_ref: QualificationRef | None = None
    display_role_assignment_ref: RoleAssignmentRef | None = None
    effective_at: datetime
    approval_ref: ApprovalRef

    @model_validator(mode="after")
    def coherent_target(self) -> AuthorityRevocation:
        _require_utc_second(self.effective_at, "revocation effective_at")
        qualification_target = self.qualification_record_ref is not None
        role_target = self.display_role_assignment_ref is not None
        if self.target == RevocationTarget.QUALIFICATION:
            if not qualification_target or role_target:
                raise ValueError(
                    "qualification revocation requires its exact record ref"
                )
        elif not role_target or qualification_target:
            raise ValueError(
                "display-role revocation requires its exact assignment ref"
            )
        return self


class MethodRegistry(RegistryContract):
    schema_version: Literal["traceback.method-registry.v1"] = (
        "traceback.method-registry.v1"
    )
    registry_id: RegistryId
    registry_version: int = Field(ge=1, le=1_000_000)
    authority_revision: int = Field(ge=0, le=10_000_000)
    published_at: datetime
    previous_registry_sha256: Sha256 | None = None
    tools: tuple[ToolRegistration, ...] = Field(min_length=1, max_length=MAX_TOOLS)
    assets: tuple[AssetRegistration, ...] = Field(min_length=1, max_length=MAX_ASSETS)
    method_definitions: tuple[MethodDefinition, ...] = Field(
        min_length=1, max_length=MAX_METHODS
    )
    qualification_records: tuple[QualificationRecord, ...] = Field(
        default=(), max_length=MAX_AUTHORITY_RECORDS
    )
    display_role_assignments: tuple[DisplayRoleAssignment, ...] = Field(
        default=(), max_length=MAX_AUTHORITY_RECORDS
    )
    revocations: tuple[AuthorityRevocation, ...] = Field(
        default=(), max_length=MAX_AUTHORITY_RECORDS
    )

    @model_validator(mode="after")
    def coherent_registry(self) -> MethodRegistry:
        _require_utc_second(self.published_at, "published_at")
        if (self.registry_version == 1) != (self.previous_registry_sha256 is None):
            raise ValueError(
                "only registry version one may omit previous_registry_sha256"
            )
        authority_events = (
            len(self.qualification_records)
            + len(self.display_role_assignments)
            + len(self.revocations)
        )
        if self.authority_revision != authority_events:
            raise ValueError(
                "authority_revision must equal the append-only event count"
            )
        if self.registry_version == 1 and any(
            item.effective_at < self.published_at
            for item in (
                *self.qualification_records,
                *self.display_role_assignments,
                *self.revocations,
            )
        ):
            raise ValueError(
                "initial authority records cannot predate registry publication"
            )

        self._validate_sorted_unique()
        definitions = {
            (item.method_id, item.version): item for item in self.method_definitions
        }
        tools = {
            (item.tool_id, item.version): item.artifact_sha256 for item in self.tools
        }
        assets = {
            (item.asset_id, item.version): item.content_sha256 for item in self.assets
        }
        quantity_units: dict[tuple[MethodFamily, str], str] = {}
        for definition in self.method_definitions:
            quantity_key = (definition.family, definition.quantity_id)
            known_unit = quantity_units.setdefault(quantity_key, definition.unit)
            if known_unit != definition.unit:
                raise ValueError("one registered quantity cannot use conflicting units")
            for reference in definition.tools:
                if tools.get((reference.tool_id, reference.version)) != (
                    reference.artifact_sha256
                ):
                    raise ValueError(
                        "method definition references an unknown or mismatched tool version"
                    )
            for reference in definition.assets:
                if assets.get((reference.asset_id, reference.version)) != (
                    reference.content_sha256
                ):
                    raise ValueError(
                        "method definition references an unknown or mismatched asset version"
                    )

        for record in self.qualification_records:
            if _method_key(record.method_ref) not in definitions:
                raise ValueError("qualification references an unknown method version")
        for assignment in self.display_role_assignments:
            if _method_key(assignment.method_ref) not in definitions:
                raise ValueError("display role references an unknown method version")

        qualification_by_ref = {
            item.record_ref: item for item in self.qualification_records
        }
        role_by_ref = {
            item.assignment_ref: item for item in self.display_role_assignments
        }
        revoked_at: dict[tuple[RevocationTarget, str], datetime] = {}
        for revocation in self.revocations:
            if revocation.target == RevocationTarget.QUALIFICATION:
                assert revocation.qualification_record_ref is not None
                target_ref = revocation.qualification_record_ref
                target = qualification_by_ref.get(target_ref)
            else:
                assert revocation.display_role_assignment_ref is not None
                target_ref = revocation.display_role_assignment_ref
                target = role_by_ref.get(target_ref)
            if target is None:
                raise ValueError("revocation references an unknown authority record")
            key = (revocation.target, target_ref)
            if key in revoked_at:
                raise ValueError("one authority record cannot be revoked twice")
            if revocation.effective_at < target.effective_at:
                raise ValueError("revocation cannot precede its authority record")
            revoked_at[key] = revocation.effective_at

        self._validate_lifecycle_windows(revoked_at, definitions)
        return self

    def _validate_sorted_unique(self) -> None:
        collections = (
            (
                "tool registrations",
                self.tools,
                lambda item: (item.tool_id, item.version),
            ),
            (
                "asset registrations",
                self.assets,
                lambda item: (item.asset_id, item.version),
            ),
            (
                "method definitions",
                self.method_definitions,
                lambda item: (item.method_id, item.version),
            ),
            (
                "qualification records",
                self.qualification_records,
                lambda item: item.record_ref,
            ),
            (
                "display role assignments",
                self.display_role_assignments,
                lambda item: item.assignment_ref,
            ),
            ("revocations", self.revocations, lambda item: item.revocation_ref),
        )
        for label, values, key_function in collections:
            keys = [key_function(item) for item in values]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError(f"{label} must be uniquely sorted")

    def _validate_lifecycle_windows(
        self,
        revoked_at: Mapping[tuple[RevocationTarget, str], datetime],
        definitions: Mapping[tuple[str, str], MethodDefinition],
    ) -> None:
        qualification_groups: dict[tuple[str, str], list[QualificationRecord]] = {}
        for record in self.qualification_records:
            qualification_groups.setdefault(_method_key(record.method_ref), []).append(
                record
            )
        for records in qualification_groups.values():
            ordered = sorted(records, key=lambda item: item.effective_at)
            _require_nonoverlap(
                ordered,
                lambda item: revoked_at.get(
                    (RevocationTarget.QUALIFICATION, item.record_ref)
                ),
                "qualification authority windows overlap",
            )

        role_groups: dict[tuple[str, str, str], list[DisplayRoleAssignment]] = {}
        provider_groups: dict[
            tuple[MethodFamily, str, str], list[DisplayRoleAssignment]
        ] = {}
        for assignment in self.display_role_assignments:
            method_key = _method_key(assignment.method_ref)
            role_groups.setdefault(
                (*method_key, assignment.authority_scope), []
            ).append(assignment)
            if assignment.display_role == DisplayRole.PROVIDER_PRIMARY:
                definition = definitions[method_key]
                provider_groups.setdefault(
                    (
                        definition.family,
                        definition.quantity_id,
                        assignment.authority_scope,
                    ),
                    [],
                ).append(assignment)
        for assignments in role_groups.values():
            ordered = sorted(assignments, key=lambda item: item.effective_at)
            _require_nonoverlap(
                ordered,
                lambda item: revoked_at.get(
                    (RevocationTarget.DISPLAY_ROLE, item.assignment_ref)
                ),
                "display-role authority windows overlap",
            )
        for assignments in provider_groups.values():
            ordered = sorted(assignments, key=lambda item: item.effective_at)
            _require_nonoverlap(
                ordered,
                lambda item: revoked_at.get(
                    (RevocationTarget.DISPLAY_ROLE, item.assignment_ref)
                ),
                "provider_primary authority windows overlap for one measurement scope",
            )


class AuthorityHead(RegistryContract):
    schema_version: Literal["traceback.method-authority-head.v1"] = (
        "traceback.method-authority-head.v1"
    )
    registry_id: RegistryId
    registry_version: int = Field(ge=1, le=1_000_000)
    registry_sha256: Sha256
    authority_revision: int = Field(ge=0, le=10_000_000)
    issued_at: datetime

    @model_validator(mode="after")
    def valid_time(self) -> AuthorityHead:
        _require_utc_second(self.issued_at, "authority head issued_at")
        return self


class HistoricalMethodCapability(RegistryContract):
    schema_version: Literal["traceback.historical-method-capability.v1"] = (
        "traceback.historical-method-capability.v1"
    )
    registry_sha256: Sha256
    registry_version: int = Field(ge=1)
    method_definition_sha256: Sha256
    method_ref: MethodReference
    authority_scope: AuthorityScope
    as_of: datetime
    qualification_state: QualificationState | None
    display_role: DisplayRole | None
    research_inspectable: bool
    historical_provider_designated: bool
    current_provider_eligible: Literal[False] = False

    @model_validator(mode="after")
    def valid_time(self) -> HistoricalMethodCapability:
        _require_utc_second(self.as_of, "historical capability as_of")
        return self


class CurrentMethodCapability(RegistryContract):
    schema_version: Literal["traceback.current-method-capability.v1"] = (
        "traceback.current-method-capability.v1"
    )
    registry_sha256: Sha256
    registry_version: int = Field(ge=1)
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0)
    method_definition_sha256: Sha256
    method_ref: MethodReference
    authority_scope: AuthorityScope
    as_of: datetime
    qualification_state: QualificationState | None
    display_role: DisplayRole | None
    research_inspectable: bool
    current_provider_eligible: bool
    effective_approval_ref: ApprovalRef | None

    @model_validator(mode="after")
    def coherent_capability(self) -> CurrentMethodCapability:
        _require_utc_second(self.as_of, "current capability as_of")
        if self.current_provider_eligible != (self.effective_approval_ref is not None):
            raise ValueError("provider eligibility requires one effective approval")
        if self.current_provider_eligible and (
            self.qualification_state != QualificationState.QUALIFIED
            or self.display_role != DisplayRole.PROVIDER_PRIMARY
        ):
            raise ValueError(
                "provider eligibility requires qualified provider_primary authority"
            )
        return self


class RegistryIdentityError(ValueError):
    """A registry, method, head, or capability identity failed closed."""


class RegistryTransitionError(ValueError):
    """A registry snapshot cannot follow the prior canonical snapshot."""


def _method_key(reference: MethodReference) -> tuple[str, str]:
    return reference.method_id, reference.version


def _require_nonoverlap(
    records: Sequence[QualificationRecord | DisplayRoleAssignment],
    revoked_at: Callable[[Any], datetime | None],
    message: str,
) -> None:
    for previous, current in zip(records, records[1:]):
        previous_end = revoked_at(previous)
        if previous_end is None or current.effective_at < previous_end:
            raise ValueError(message)


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite number at {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def canonical_contract_bytes(contract: RegistryContract) -> bytes:
    """Serialize any registry contract to deterministic UTF-8 JSON."""

    payload = contract.model_dump(mode="json", exclude_none=False)
    _reject_nonfinite(payload)
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


ContractT = TypeVar("ContractT", bound=RegistryContract)


def contract_from_canonical_bytes(model: type[ContractT], content: bytes) -> ContractT:
    """Load one exact canonical contract and reject unknown or altered bytes."""

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON token: {token}")

    try:
        payload = json.loads(content, parse_constant=reject_constant)
        contract = model.model_validate(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise RegistryIdentityError("contract JSON is invalid") from exc
    if canonical_contract_bytes(contract) != content:
        raise RegistryIdentityError("contract JSON is not canonical")
    return contract


def registry_sha256(registry: MethodRegistry) -> str:
    return hashlib.sha256(canonical_contract_bytes(registry)).hexdigest()


def method_definition_sha256(definition: MethodDefinition) -> str:
    return hashlib.sha256(canonical_contract_bytes(definition)).hexdigest()


def authority_head_sha256(head: AuthorityHead) -> str:
    return hashlib.sha256(canonical_contract_bytes(head)).hexdigest()


def authority_head_for_registry(
    registry: MethodRegistry,
    *,
    issued_at: datetime,
) -> AuthorityHead:
    _require_utc_second(issued_at, "authority head issued_at")
    if issued_at < registry.published_at:
        raise RegistryIdentityError(
            "authority head cannot predate registry publication"
        )
    return AuthorityHead(
        registry_id=registry.registry_id,
        registry_version=registry.registry_version,
        registry_sha256=registry_sha256(registry),
        authority_revision=registry.authority_revision,
        issued_at=issued_at,
    )


def _definition(
    registry: MethodRegistry, method_ref: MethodReference
) -> MethodDefinition:
    for definition in registry.method_definitions:
        if definition.method_ref == method_ref:
            return definition
    raise RegistryIdentityError("method ID and version are not registered")


def _revocation_times(
    registry: MethodRegistry,
) -> dict[tuple[RevocationTarget, str], datetime]:
    result: dict[tuple[RevocationTarget, str], datetime] = {}
    for item in registry.revocations:
        target_ref = (
            item.qualification_record_ref
            if item.target == RevocationTarget.QUALIFICATION
            else item.display_role_assignment_ref
        )
        assert target_ref is not None
        result[(item.target, target_ref)] = item.effective_at
    return result


def _active_authority(
    registry: MethodRegistry,
    method_ref: MethodReference,
    authority_scope: str,
    as_of: datetime,
) -> tuple[QualificationRecord | None, DisplayRoleAssignment | None]:
    revoked_at = _revocation_times(registry)
    latest = datetime.max.replace(tzinfo=as_of.tzinfo)
    qualifications = [
        item
        for item in registry.qualification_records
        if item.method_ref == method_ref
        and item.effective_at <= as_of
        and as_of
        < revoked_at.get((RevocationTarget.QUALIFICATION, item.record_ref), latest)
    ]
    assignments = [
        item
        for item in registry.display_role_assignments
        if item.method_ref == method_ref
        and item.authority_scope == authority_scope
        and item.effective_at <= as_of
        and as_of
        < revoked_at.get((RevocationTarget.DISPLAY_ROLE, item.assignment_ref), latest)
    ]
    if len(qualifications) > 1 or len(assignments) > 1:
        raise RegistryIdentityError("authority replay produced overlapping records")
    return (
        qualifications[0] if qualifications else None,
        assignments[0] if assignments else None,
    )


def resolve_historical_capability(
    registry: MethodRegistry,
    method_ref: MethodReference,
    *,
    authority_scope: str,
    as_of: datetime,
) -> HistoricalMethodCapability:
    """Replay a snapshot historically without granting current eligibility."""

    _require_utc_second(as_of, "historical capability as_of")
    definition = _definition(registry, method_ref)
    qualification, assignment = _active_authority(
        registry, method_ref, authority_scope, as_of
    )
    role = assignment.display_role if assignment is not None else None
    qualified = (
        qualification is not None
        and qualification.state == QualificationState.QUALIFIED
    )
    return HistoricalMethodCapability(
        registry_sha256=registry_sha256(registry),
        registry_version=registry.registry_version,
        method_definition_sha256=method_definition_sha256(definition),
        method_ref=method_ref,
        authority_scope=authority_scope,
        as_of=as_of,
        qualification_state=(qualification.state if qualification else None),
        display_role=role,
        research_inspectable=role != DisplayRole.DISABLED,
        historical_provider_designated=(
            qualified and role == DisplayRole.PROVIDER_PRIMARY
        ),
    )


def _validate_current_head(
    registry: MethodRegistry,
    authority_head: AuthorityHead,
    expected_authority_head_sha256: str,
    as_of: datetime,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_authority_head_sha256):
        raise RegistryIdentityError("expected authority-head digest is invalid")
    observed_head_sha256 = authority_head_sha256(authority_head)
    if observed_head_sha256 != expected_authority_head_sha256:
        raise RegistryIdentityError("authority-head digest mismatch")
    if (
        authority_head.registry_id != registry.registry_id
        or authority_head.registry_version != registry.registry_version
        or authority_head.registry_sha256 != registry_sha256(registry)
        or authority_head.authority_revision != registry.authority_revision
    ):
        raise RegistryIdentityError("registry snapshot is stale for the trusted head")
    if as_of < authority_head.issued_at:
        raise RegistryIdentityError(
            "current capability cannot predate its trusted head"
        )
    return observed_head_sha256


def resolve_current_capability(
    registry: MethodRegistry,
    authority_head: AuthorityHead,
    expected_authority_head_sha256: str,
    method_ref: MethodReference,
    *,
    authority_scope: str,
    as_of: datetime,
) -> CurrentMethodCapability:
    """Evaluate current eligibility only against an externally trusted fresh head."""

    _require_utc_second(as_of, "current capability as_of")
    head_sha256 = _validate_current_head(
        registry, authority_head, expected_authority_head_sha256, as_of
    )
    historical = resolve_historical_capability(
        registry,
        method_ref,
        authority_scope=authority_scope,
        as_of=as_of,
    )
    qualification, assignment = _active_authority(
        registry, method_ref, authority_scope, as_of
    )
    provider_eligible = (
        qualification is not None
        and qualification.state == QualificationState.QUALIFIED
        and assignment is not None
        and assignment.display_role == DisplayRole.PROVIDER_PRIMARY
        and assignment.approval_ref is not None
    )
    return CurrentMethodCapability(
        registry_sha256=historical.registry_sha256,
        registry_version=historical.registry_version,
        authority_head_sha256=head_sha256,
        authority_revision=authority_head.authority_revision,
        method_definition_sha256=historical.method_definition_sha256,
        method_ref=method_ref,
        authority_scope=authority_scope,
        as_of=as_of,
        qualification_state=historical.qualification_state,
        display_role=historical.display_role,
        research_inspectable=historical.research_inspectable,
        current_provider_eligible=provider_eligible,
        effective_approval_ref=(
            assignment.approval_ref if provider_eligible and assignment else None
        ),
    )


def effective_provider_primary(
    registry: MethodRegistry,
    authority_head: AuthorityHead,
    expected_authority_head_sha256: str,
    *,
    family: MethodFamily,
    quantity_id: str,
    unit: str,
    authority_scope: str,
    as_of: datetime,
) -> MethodReference | None:
    """Return the sole head-authorized current primary, never an inferred fallback."""

    _validate_current_head(
        registry, authority_head, expected_authority_head_sha256, as_of
    )
    matches: list[MethodReference] = []
    for definition in registry.method_definitions:
        if (
            definition.family != family
            or definition.quantity_id != quantity_id
            or definition.unit != unit
        ):
            continue
        capability = resolve_current_capability(
            registry,
            authority_head,
            expected_authority_head_sha256,
            definition.method_ref,
            authority_scope=authority_scope,
            as_of=as_of,
        )
        if capability.current_provider_eligible:
            matches.append(definition.method_ref)
    if len(matches) > 1:
        raise RegistryIdentityError("multiple current provider_primary methods")
    return matches[0] if matches else None


def replay_current_capability(
    registry: MethodRegistry,
    authority_head: AuthorityHead,
    expected_authority_head_sha256: str,
    capability: CurrentMethodCapability,
) -> CurrentMethodCapability:
    expected = resolve_current_capability(
        registry,
        authority_head,
        expected_authority_head_sha256,
        capability.method_ref,
        authority_scope=capability.authority_scope,
        as_of=capability.as_of,
    )
    if capability != expected:
        raise RegistryIdentityError("current capability does not match semantic replay")
    return capability


def replay_historical_capability(
    registry: MethodRegistry,
    capability: HistoricalMethodCapability,
) -> HistoricalMethodCapability:
    expected = resolve_historical_capability(
        registry,
        capability.method_ref,
        authority_scope=capability.authority_scope,
        as_of=capability.as_of,
    )
    if capability != expected:
        raise RegistryIdentityError(
            "historical capability does not match semantic replay"
        )
    return capability


def _authority_identity(value: Any) -> Any:
    for field in (
        "record_ref",
        "assignment_ref",
        "revocation_ref",
        "tool_id",
        "asset_id",
        "method_id",
    ):
        if hasattr(value, field):
            identity = getattr(value, field)
            version = getattr(value, "version", None)
            return (identity, version) if version is not None else identity
    raise TypeError("unsupported registry identity")


def _append_only(previous: Sequence[Any], current: Sequence[Any], label: str) -> int:
    previous_by_identity = {_authority_identity(item): item for item in previous}
    current_by_identity = {_authority_identity(item): item for item in current}
    if any(
        current_by_identity.get(identity) != item
        for identity, item in previous_by_identity.items()
    ):
        raise RegistryTransitionError(f"{label} are immutable and append-only")
    return len(current_by_identity) - len(previous_by_identity)


def replay_registry_transition(
    previous: MethodRegistry,
    current: MethodRegistry,
) -> MethodRegistry:
    """Replay immutable definitions and append-only, non-backdated authority events."""

    if current.registry_id != previous.registry_id:
        raise RegistryTransitionError("registry ID cannot change")
    if current.registry_version != previous.registry_version + 1:
        raise RegistryTransitionError("registry version must advance by exactly one")
    if current.previous_registry_sha256 != registry_sha256(previous):
        raise RegistryTransitionError("previous registry digest mismatch")
    if current.published_at <= previous.published_at:
        raise RegistryTransitionError("published_at must increase")

    _append_only(previous.tools, current.tools, "tool registrations")
    _append_only(previous.assets, current.assets, "asset registrations")
    _append_only(
        previous.method_definitions,
        current.method_definitions,
        "method definitions",
    )
    new_qualifications = _append_only(
        previous.qualification_records,
        current.qualification_records,
        "qualification records",
    )
    new_assignments = _append_only(
        previous.display_role_assignments,
        current.display_role_assignments,
        "display-role assignments",
    )
    new_revocations = _append_only(
        previous.revocations,
        current.revocations,
        "authority revocations",
    )
    expected_revision = previous.authority_revision + (
        new_qualifications + new_assignments + new_revocations
    )
    if current.authority_revision != expected_revision:
        raise RegistryTransitionError(
            "authority revision does not match appended events"
        )

    previous_qualification_refs = {
        item.record_ref for item in previous.qualification_records
    }
    previous_assignment_refs = {
        item.assignment_ref for item in previous.display_role_assignments
    }
    previous_revocation_refs = {item.revocation_ref for item in previous.revocations}
    for record in current.qualification_records:
        if (
            record.record_ref not in previous_qualification_refs
            and record.effective_at < current.published_at
        ):
            raise RegistryTransitionError(
                "new qualification authority cannot predate publication"
            )
    for assignment in current.display_role_assignments:
        if (
            assignment.assignment_ref not in previous_assignment_refs
            and assignment.effective_at < current.published_at
        ):
            raise RegistryTransitionError(
                "new display authority cannot predate publication"
            )
    for revocation in current.revocations:
        if (
            revocation.revocation_ref not in previous_revocation_refs
            and revocation.effective_at < current.published_at
        ):
            raise RegistryTransitionError("new revocation cannot predate publication")
    return current
