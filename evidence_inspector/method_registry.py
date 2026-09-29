"""Closed, canonical contracts for versioned research methods and authority.

The registry is pure data.  It does not load plugins, inspect local paths, infer
provider defaults, or execute a registered tool.  Provider eligibility is
replayed from explicit role, qualification, authority scope, and time fields.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)


def _privacy_safe_identifier(value: str) -> str:
    uppercase = value.upper()
    if len(value) >= 12 and set(uppercase) <= set("ACGTN"):
        raise ValueError("identifier cannot contain sequence-like content")
    lowered = value.lower()
    if lowered == "donor" or lowered.startswith(("donor-", "donor_", "donor:")):
        raise ValueError("identifier cannot contain donor identity")
    return value


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
    AfterValidator(_privacy_safe_identifier),
]
Version = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=32,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

MAX_TOOLS = 32
MAX_ASSETS = 64
MAX_METHODS = 128
MAX_METHOD_BINDINGS = 16


class RegistryContract(BaseModel):
    """Immutable contract base that rejects unknown fields and non-finite numbers."""

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


class MethodReference(RegistryContract):
    method_id: Identifier
    version: Version


class ToolRegistration(RegistryContract):
    tool_id: Identifier
    version: Version
    artifact_sha256: Sha256


class AssetRegistration(RegistryContract):
    asset_id: Identifier
    version: Version
    content_sha256: Sha256


class ToolReference(RegistryContract):
    tool_id: Identifier
    version: Version
    artifact_sha256: Sha256


class AssetReference(RegistryContract):
    asset_id: Identifier
    version: Version
    content_sha256: Sha256


def _require_utc_second(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be an aware UTC timestamp")
    if value.microsecond != 0:
        raise ValueError(f"{label} must use whole-second precision")


class MethodRegistration(RegistryContract):
    schema_version: Literal["traceback.method-registration.v1"] = (
        "traceback.method-registration.v1"
    )
    method_id: Identifier
    version: Version
    family: MethodFamily
    quantity_id: Identifier
    unit: Identifier
    parameter_schema_sha256: Sha256
    tools: tuple[ToolReference, ...] = Field(
        min_length=1, max_length=MAX_METHOD_BINDINGS
    )
    assets: tuple[AssetReference, ...] = Field(
        min_length=1, max_length=MAX_METHOD_BINDINGS
    )
    qualification_state: QualificationState
    display_role: DisplayRole
    authority_scope: Identifier | None = None
    approval_ref: Identifier | None = None
    effective_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def method_ref(self) -> MethodReference:
        return MethodReference(method_id=self.method_id, version=self.version)

    @model_validator(mode="after")
    def coherent_registration(self) -> MethodRegistration:
        tool_keys = [(item.tool_id, item.version) for item in self.tools]
        if tool_keys != sorted(tool_keys) or len(tool_keys) != len(set(tool_keys)):
            raise ValueError("tool references must be uniquely sorted")
        asset_keys = [(item.asset_id, item.version) for item in self.assets]
        if asset_keys != sorted(asset_keys) or len(asset_keys) != len(set(asset_keys)):
            raise ValueError("asset references must be uniquely sorted")

        authority = (self.authority_scope, self.approval_ref, self.effective_at)
        if self.display_role == DisplayRole.PROVIDER_PRIMARY:
            if any(item is None for item in authority):
                raise ValueError(
                    "provider_primary requires authority_scope, approval_ref, and effective_at"
                )
        elif any(item is not None for item in authority):
            raise ValueError(
                "provider authority fields are allowed only for provider_primary"
            )

        if self.effective_at is not None:
            _require_utc_second(self.effective_at, "effective_at")
        if self.revoked_at is not None:
            _require_utc_second(self.revoked_at, "revoked_at")
            if self.effective_at is not None and self.revoked_at < self.effective_at:
                raise ValueError("revoked_at cannot precede effective_at")
        return self


class MethodRegistry(RegistryContract):
    schema_version: Literal["traceback.method-registry.v1"] = (
        "traceback.method-registry.v1"
    )
    registry_id: Identifier
    registry_version: int = Field(ge=1, le=1_000_000)
    published_at: datetime
    previous_registry_sha256: Sha256 | None = None
    tools: tuple[ToolRegistration, ...] = Field(min_length=1, max_length=MAX_TOOLS)
    assets: tuple[AssetRegistration, ...] = Field(min_length=1, max_length=MAX_ASSETS)
    methods: tuple[MethodRegistration, ...] = Field(
        min_length=1, max_length=MAX_METHODS
    )

    @model_validator(mode="after")
    def coherent_registry(self) -> MethodRegistry:
        _require_utc_second(self.published_at, "published_at")
        if (self.registry_version == 1) != (self.previous_registry_sha256 is None):
            raise ValueError(
                "only registry version one may omit previous_registry_sha256"
            )

        tool_keys = [(item.tool_id, item.version) for item in self.tools]
        if tool_keys != sorted(tool_keys) or len(tool_keys) != len(set(tool_keys)):
            raise ValueError("tool registrations must be uniquely sorted")
        asset_keys = [(item.asset_id, item.version) for item in self.assets]
        if asset_keys != sorted(asset_keys) or len(asset_keys) != len(set(asset_keys)):
            raise ValueError("asset registrations must be uniquely sorted")
        method_keys = [(item.method_id, item.version) for item in self.methods]
        if method_keys != sorted(method_keys) or len(method_keys) != len(
            set(method_keys)
        ):
            raise ValueError("method registrations must be uniquely sorted")

        quantity_units: dict[tuple[MethodFamily, str], str] = {}
        for method in self.methods:
            quantity_key = (method.family, method.quantity_id)
            existing_unit = quantity_units.setdefault(quantity_key, method.unit)
            if existing_unit != method.unit:
                raise ValueError("one registered quantity cannot use conflicting units")

        tools = {
            (item.tool_id, item.version): item.artifact_sha256 for item in self.tools
        }
        assets = {
            (item.asset_id, item.version): item.content_sha256 for item in self.assets
        }
        for method in self.methods:
            for reference in method.tools:
                if tools.get((reference.tool_id, reference.version)) != (
                    reference.artifact_sha256
                ):
                    raise ValueError(
                        "method references an unknown or mismatched tool version"
                    )
            for reference in method.assets:
                if assets.get((reference.asset_id, reference.version)) != (
                    reference.content_sha256
                ):
                    raise ValueError(
                        "method references an unknown or mismatched asset version"
                    )

        authority_windows: dict[
            tuple[MethodFamily, str, str], list[MethodRegistration]
        ] = {}
        for method in self.methods:
            if method.display_role != DisplayRole.PROVIDER_PRIMARY:
                continue
            assert method.authority_scope is not None
            key = (
                method.family,
                method.quantity_id,
                method.authority_scope,
            )
            authority_windows.setdefault(key, []).append(method)
        for registrations in authority_windows.values():
            ordered = sorted(registrations, key=lambda item: item.effective_at)
            for previous, current in zip(ordered, ordered[1:]):
                assert current.effective_at is not None
                if (
                    previous.revoked_at is None
                    or current.effective_at < previous.revoked_at
                ):
                    raise ValueError(
                        "provider_primary authority windows overlap for one measurement scope"
                    )
        return self


class MethodCapability(RegistryContract):
    schema_version: Literal["traceback.method-capability.v1"] = (
        "traceback.method-capability.v1"
    )
    registry_sha256: Sha256
    method_ref: MethodReference
    authority_scope: Identifier
    as_of: datetime
    qualification_state: QualificationState
    display_role: DisplayRole
    research_available: bool
    provider_available: bool
    effective_approval_ref: Identifier | None

    @model_validator(mode="after")
    def coherent_capability(self) -> MethodCapability:
        _require_utc_second(self.as_of, "as_of")
        if self.provider_available != (self.effective_approval_ref is not None):
            raise ValueError(
                "provider availability requires one effective approval reference"
            )
        if self.provider_available and (
            self.display_role != DisplayRole.PROVIDER_PRIMARY
            or self.qualification_state != QualificationState.QUALIFIED
            or not self.research_available
        ):
            raise ValueError(
                "provider availability requires qualified provider_primary research availability"
            )
        return self


class RegistryIdentityError(ValueError):
    """A requested identity is absent or does not match the closed registry."""


class RegistryTransitionError(ValueError):
    """A registry snapshot cannot follow the prior canonical snapshot."""


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
    """Serialize a registry contract to deterministic, closed UTF-8 JSON."""

    payload = contract.model_dump(mode="json", exclude_none=False)
    _reject_nonfinite(payload)
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_registry_bytes(registry: MethodRegistry) -> bytes:
    """Serialize a registry to deterministic, closed UTF-8 JSON."""

    return canonical_contract_bytes(registry)


def registry_sha256(registry: MethodRegistry) -> str:
    """Digest exact canonical registry bytes."""

    return hashlib.sha256(canonical_registry_bytes(registry)).hexdigest()


def registry_from_canonical_bytes(content: bytes) -> MethodRegistry:
    """Load one registry only when its bytes and schema are exactly canonical."""

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON token: {token}")

    try:
        payload = json.loads(content, parse_constant=reject_constant)
        registry = MethodRegistry.model_validate(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise RegistryIdentityError("registry JSON is invalid") from exc
    if canonical_registry_bytes(registry) != content:
        raise RegistryIdentityError("registry JSON is not canonical")
    return registry


def capability_from_canonical_bytes(content: bytes) -> MethodCapability:
    """Load one capability only when its bytes and schema are exactly canonical."""

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON token: {token}")

    try:
        payload = json.loads(content, parse_constant=reject_constant)
        capability = MethodCapability.model_validate(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise RegistryIdentityError("capability JSON is invalid") from exc
    if canonical_contract_bytes(capability) != content:
        raise RegistryIdentityError("capability JSON is not canonical")
    return capability


def _registration_map(
    registry: MethodRegistry,
) -> dict[tuple[str, str], MethodRegistration]:
    return {(item.method_id, item.version): item for item in registry.methods}


def resolve_method_capability(
    registry: MethodRegistry,
    method_ref: MethodReference,
    *,
    authority_scope: str,
    as_of: datetime,
) -> MethodCapability:
    """Replay research and provider availability without inferring a default."""

    _require_utc_second(as_of, "as_of")
    registration = _registration_map(registry).get(
        (method_ref.method_id, method_ref.version)
    )
    if registration is None:
        raise RegistryIdentityError("method ID and version are not registered")

    revoked = registration.revoked_at is not None and as_of >= registration.revoked_at
    research_available = (
        registration.display_role != DisplayRole.DISABLED and not revoked
    )
    effective_authority = (
        registration.display_role == DisplayRole.PROVIDER_PRIMARY
        and registration.authority_scope == authority_scope
        and registration.effective_at is not None
        and registration.effective_at <= as_of
        and not revoked
    )
    provider_available = (
        effective_authority
        and registration.qualification_state == QualificationState.QUALIFIED
    )
    return MethodCapability(
        registry_sha256=registry_sha256(registry),
        method_ref=method_ref,
        authority_scope=authority_scope,
        as_of=as_of,
        qualification_state=registration.qualification_state,
        display_role=registration.display_role,
        research_available=research_available,
        provider_available=provider_available,
        effective_approval_ref=(
            registration.approval_ref if provider_available else None
        ),
    )


def effective_provider_primary(
    registry: MethodRegistry,
    *,
    family: MethodFamily,
    quantity_id: str,
    unit: str,
    authority_scope: str,
    as_of: datetime,
) -> MethodReference | None:
    """Return the sole effective qualified primary, or no default."""

    matches: list[MethodReference] = []
    for registration in registry.methods:
        if (
            registration.family != family
            or registration.quantity_id != quantity_id
            or registration.unit != unit
        ):
            continue
        capability = resolve_method_capability(
            registry,
            registration.method_ref,
            authority_scope=authority_scope,
            as_of=as_of,
        )
        if capability.provider_available:
            matches.append(registration.method_ref)
    if len(matches) > 1:
        raise RegistryIdentityError("multiple effective provider_primary methods")
    return matches[0] if matches else None


def replay_method_capability(
    registry: MethodRegistry,
    capability: MethodCapability,
) -> MethodCapability:
    """Recompute a capability and reject digest or semantic tampering."""

    if capability.registry_sha256 != registry_sha256(registry):
        raise RegistryIdentityError("capability registry digest mismatch")
    expected = resolve_method_capability(
        registry,
        capability.method_ref,
        authority_scope=capability.authority_scope,
        as_of=capability.as_of,
    )
    if capability != expected:
        raise RegistryIdentityError("capability does not match semantic replay")
    return capability


def replay_registry_transition(
    previous: MethodRegistry,
    current: MethodRegistry,
) -> MethodRegistry:
    """Validate the append-only registry chain and monotonic revocation semantics."""

    if current.registry_id != previous.registry_id:
        raise RegistryTransitionError("registry ID cannot change")
    if current.registry_version != previous.registry_version + 1:
        raise RegistryTransitionError("registry version must advance by exactly one")
    if current.previous_registry_sha256 != registry_sha256(previous):
        raise RegistryTransitionError("previous registry digest mismatch")
    if current.published_at <= previous.published_at:
        raise RegistryTransitionError("published_at must increase")

    previous_tools = {(item.tool_id, item.version): item for item in previous.tools}
    current_tools = {(item.tool_id, item.version): item for item in current.tools}
    previous_assets = {(item.asset_id, item.version): item for item in previous.assets}
    current_assets = {(item.asset_id, item.version): item for item in current.assets}
    if any(current_tools.get(key) != value for key, value in previous_tools.items()):
        raise RegistryTransitionError(
            "tool registrations are immutable and append-only"
        )
    if any(current_assets.get(key) != value for key, value in previous_assets.items()):
        raise RegistryTransitionError(
            "asset registrations are immutable and append-only"
        )

    previous_methods = _registration_map(previous)
    current_methods = _registration_map(current)
    for key, old in previous_methods.items():
        new = current_methods.get(key)
        if new is None:
            raise RegistryTransitionError("method registrations are append-only")
        old_without_revocation = old.model_copy(update={"revoked_at": None})
        new_without_revocation = new.model_copy(update={"revoked_at": None})
        if old_without_revocation != new_without_revocation:
            raise RegistryTransitionError(
                "method identity, qualification, display role, and authority are immutable"
            )
        if old.revoked_at is not None and new.revoked_at != old.revoked_at:
            raise RegistryTransitionError(
                "method revocation cannot change or be removed"
            )
        if old.revoked_at is None and new.revoked_at is not None:
            if not previous.published_at <= new.revoked_at <= current.published_at:
                raise RegistryTransitionError(
                    "new revocation must occur between registry publications"
                )
    return current
