"""Authority, replay, canonicalization, and privacy tests for E01."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from evidence_inspector.method_registry import (
    AssetReference,
    AssetRegistration,
    AuthorityHead,
    AuthorityRevocation,
    CurrentMethodCapability,
    DisplayRole,
    DisplayRoleAssignment,
    HistoricalMethodCapability,
    MethodDefinition,
    MethodFamily,
    MethodReference,
    MethodRegistry,
    QualificationRecord,
    QualificationState,
    RegistryIdentityError,
    RegistryTransitionError,
    RevocationTarget,
    ToolReference,
    ToolRegistration,
    authority_head_for_registry,
    authority_head_sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
    effective_provider_primary,
    registry_sha256,
    replay_current_capability,
    replay_historical_capability,
    replay_registry_transition,
    resolve_current_capability,
    resolve_historical_capability,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
T3 = datetime(2026, 4, 1, tzinfo=UTC)
OVERSIZED_VERSION = f"{'1' * 29}.1.1"


def _tool() -> ToolRegistration:
    return ToolRegistration(
        tool_id="tool_fragment_counter",
        version="1.0.0",
        artifact_sha256="a" * 64,
    )


def _asset() -> AssetRegistration:
    return AssetRegistration(
        asset_id="asset_fragment_policy",
        version="1.0.0",
        content_sha256="b" * 64,
    )


def _definition(
    method_id: str = "mth_fragment_raw_query_length",
    version: str = "1.0.0",
) -> MethodDefinition:
    return MethodDefinition(
        method_id=method_id,
        version=version,
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id="qty_fragment_raw_query_length",
        unit="unit_base_pairs",
        parameter_schema_sha256="c" * 64,
        tools=(
            ToolReference(
                tool_id="tool_fragment_counter",
                version="1.0.0",
                artifact_sha256="a" * 64,
            ),
        ),
        assets=(
            AssetReference(
                asset_id="asset_fragment_policy",
                version="1.0.0",
                content_sha256="b" * 64,
            ),
        ),
    )


def _qualification(
    method_ref: MethodReference,
    *,
    record_ref: str,
    state: QualificationState,
    effective_at: datetime,
) -> QualificationRecord:
    return QualificationRecord(
        record_ref=record_ref,
        method_ref=method_ref,
        state=state,
        effective_at=effective_at,
        approval_ref=f"approval_{record_ref}",
    )


def _role(
    method_ref: MethodReference,
    *,
    assignment_ref: str,
    role: DisplayRole,
    effective_at: datetime,
    scope: str = "scope_provider_west",
) -> DisplayRoleAssignment:
    return DisplayRoleAssignment(
        assignment_ref=assignment_ref,
        method_ref=method_ref,
        display_role=role,
        authority_scope=scope,
        effective_at=effective_at,
        approval_ref=(
            f"approval_{assignment_ref}"
            if role == DisplayRole.PROVIDER_PRIMARY
            else None
        ),
    )


def _revoke_qualification(
    record_ref: str,
    *,
    revocation_ref: str,
    effective_at: datetime,
) -> AuthorityRevocation:
    return AuthorityRevocation(
        revocation_ref=revocation_ref,
        target=RevocationTarget.QUALIFICATION,
        qualification_record_ref=record_ref,
        effective_at=effective_at,
        approval_ref=f"approval_{revocation_ref}",
    )


def _revoke_role(
    assignment_ref: str,
    *,
    revocation_ref: str,
    effective_at: datetime,
) -> AuthorityRevocation:
    return AuthorityRevocation(
        revocation_ref=revocation_ref,
        target=RevocationTarget.DISPLAY_ROLE,
        display_role_assignment_ref=assignment_ref,
        effective_at=effective_at,
        approval_ref=f"approval_{revocation_ref}",
    )


def _registry(
    *definitions: MethodDefinition,
    qualifications: tuple[QualificationRecord, ...] = (),
    roles: tuple[DisplayRoleAssignment, ...] = (),
    revocations: tuple[AuthorityRevocation, ...] = (),
    version: int = 1,
    published_at: datetime = T0,
    previous: str | None = None,
    tools: tuple[ToolRegistration, ...] | None = None,
    assets: tuple[AssetRegistration, ...] | None = None,
) -> MethodRegistry:
    return MethodRegistry(
        registry_id="registry_traceback_methods",
        registry_version=version,
        authority_revision=len(qualifications) + len(roles) + len(revocations),
        published_at=published_at,
        previous_registry_sha256=previous,
        tools=tools or (_tool(),),
        assets=assets or (_asset(),),
        method_definitions=tuple(
            sorted(definitions, key=lambda item: (item.method_id, item.version))
        ),
        qualification_records=tuple(
            sorted(qualifications, key=lambda item: item.record_ref)
        ),
        display_role_assignments=tuple(
            sorted(roles, key=lambda item: item.assignment_ref)
        ),
        revocations=tuple(sorted(revocations, key=lambda item: item.revocation_ref)),
    )


def _active_provider_registry() -> (
    tuple[MethodRegistry, MethodDefinition, QualificationRecord, DisplayRoleAssignment]
):
    definition = _definition()
    qualification = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_primary",
        state=QualificationState.QUALIFIED,
        effective_at=T0,
    )
    role = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T0,
    )
    return (
        _registry(definition, qualifications=(qualification,), roles=(role,)),
        definition,
        qualification,
        role,
    )


def _head(registry: MethodRegistry, issued_at: datetime = T0) -> AuthorityHead:
    return authority_head_for_registry(registry, issued_at=issued_at)


@pytest.mark.parametrize(
    ("model", "contract"),
    (
        (ToolRegistration, _tool()),
        (AssetRegistration, _asset()),
        (
            ToolReference,
            ToolReference(
                tool_id="tool_fragment_counter",
                version="1.0.0",
                artifact_sha256="a" * 64,
            ),
        ),
        (
            AssetReference,
            AssetReference(
                asset_id="asset_fragment_policy",
                version="1.0.0",
                content_sha256="b" * 64,
            ),
        ),
        (MethodReference, _definition().method_ref),
        (MethodDefinition, _definition()),
    ),
    ids=(
        "tool-registration",
        "asset-registration",
        "tool-reference",
        "asset-reference",
        "method-reference",
        "method-definition",
    ),
)
def test_identity_versions_reject_oversized_construction_and_canonical_load(
    model: type[BaseModel], contract: BaseModel
) -> None:
    assert len(OVERSIZED_VERSION) == 33
    payload = contract.model_dump(mode="json", exclude_none=False)
    payload["version"] = OVERSIZED_VERSION

    with pytest.raises(ValidationError, match="at most 32 characters"):
        model.model_validate(payload)

    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with pytest.raises(RegistryIdentityError, match="contract JSON is invalid"):
        contract_from_canonical_bytes(model, encoded)


def test_registry_head_and_capabilities_round_trip_as_exact_canonical_json() -> None:
    registry, definition, _, _ = _active_provider_registry()
    head = _head(registry)
    historical = resolve_historical_capability(
        registry,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    current = resolve_current_capability(
        registry,
        head,
        authority_head_sha256(head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )

    for model, value in (
        (MethodRegistry, registry),
        (AuthorityHead, head),
        (HistoricalMethodCapability, historical),
        (CurrentMethodCapability, current),
    ):
        encoded = canonical_contract_bytes(value)
        assert contract_from_canonical_bytes(model, encoded) == value
        indented = json.dumps(value.model_dump(mode="json"), indent=2).encode()
        with pytest.raises(RegistryIdentityError, match="not canonical"):
            contract_from_canonical_bytes(model, indented)

    assert registry_sha256(registry) == registry_sha256(registry)
    assert current.authority_head_sha256 == authority_head_sha256(head)
    assert current.method_definition_sha256 == historical.method_definition_sha256


def test_current_capability_requires_trusted_head_digest_and_revision() -> None:
    registry, definition, _, _ = _active_provider_registry()
    head = _head(registry)

    with pytest.raises(RegistryIdentityError, match="digest mismatch"):
        resolve_current_capability(
            registry,
            head,
            "f" * 64,
            definition.method_ref,
            authority_scope="scope_provider_west",
            as_of=T1,
        )

    changed_head = head.model_copy(
        update={"authority_revision": head.authority_revision + 1}
    )
    with pytest.raises(RegistryIdentityError, match="stale"):
        resolve_current_capability(
            registry,
            changed_head,
            authority_head_sha256(changed_head),
            definition.method_ref,
            authority_scope="scope_provider_west",
            as_of=T1,
        )


def test_active_v1_is_rejected_against_revoked_v2_trusted_head() -> None:
    active_v1, definition, _, role = _active_provider_registry()
    revoked_v2 = _registry(
        *active_v1.method_definitions,
        qualifications=active_v1.qualification_records,
        roles=active_v1.display_role_assignments,
        revocations=(
            _revoke_role(
                role.assignment_ref,
                revocation_ref="revoke_fragment_primary",
                effective_at=T2,
            ),
        ),
        version=2,
        published_at=T2,
        previous=registry_sha256(active_v1),
    )
    assert replay_registry_transition(active_v1, revoked_v2) == revoked_v2
    trusted_v2_head = _head(revoked_v2, T2)

    with pytest.raises(RegistryIdentityError, match="stale"):
        resolve_current_capability(
            active_v1,
            trusted_v2_head,
            authority_head_sha256(trusted_v2_head),
            definition.method_ref,
            authority_scope="scope_provider_west",
            as_of=T2,
        )

    revoked = resolve_current_capability(
        revoked_v2,
        trusted_v2_head,
        authority_head_sha256(trusted_v2_head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T2,
    )
    historical = resolve_historical_capability(
        active_v1,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    assert revoked.current_provider_eligible is False
    assert revoked.research_inspectable is True
    assert historical.historical_provider_designated is True
    assert historical.current_provider_eligible is False


def test_late_added_provider_cannot_backdate_before_publication() -> None:
    definition = _definition()
    qualification = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_primary",
        state=QualificationState.QUALIFIED,
        effective_at=T0,
    )
    previous = _registry(definition, qualifications=(qualification,))
    backdated_role = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T1,
    )
    current = _registry(
        definition,
        qualifications=(qualification,),
        roles=(backdated_role,),
        version=2,
        published_at=T2,
        previous=registry_sha256(previous),
    )

    with pytest.raises(RegistryTransitionError, match="cannot predate publication"):
        replay_registry_transition(previous, current)


def test_initial_authority_cannot_backdate_before_first_publication() -> None:
    definition = _definition()
    backdated = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T0,
    )

    with pytest.raises(ValidationError, match="cannot predate registry publication"):
        _registry(definition, roles=(backdated,), published_at=T1)


def test_promotion_and_qualification_do_not_change_scientific_method_version() -> None:
    definition = _definition()
    development = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_development",
        state=QualificationState.DEVELOPMENT_UNQUALIFIED,
        effective_at=T0,
    )
    baseline = _role(
        definition.method_ref,
        assignment_ref="role_fragment_baseline",
        role=DisplayRole.RESEARCH_BASELINE,
        effective_at=T0,
    )
    previous = _registry(definition, qualifications=(development,), roles=(baseline,))
    qualified = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_qualified",
        state=QualificationState.QUALIFIED,
        effective_at=T2,
    )
    primary = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T2,
    )
    current = _registry(
        definition,
        qualifications=(development, qualified),
        roles=(baseline, primary),
        revocations=(
            _revoke_qualification(
                development.record_ref,
                revocation_ref="revoke_fragment_development",
                effective_at=T2,
            ),
            _revoke_role(
                baseline.assignment_ref,
                revocation_ref="revoke_fragment_baseline",
                effective_at=T2,
            ),
        ),
        version=2,
        published_at=T2,
        previous=registry_sha256(previous),
    )

    assert replay_registry_transition(previous, current) == current
    assert current.method_definitions == previous.method_definitions
    head = _head(current, T2)
    capability = resolve_current_capability(
        current,
        head,
        authority_head_sha256(head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T2,
    )
    assert capability.qualification_state == QualificationState.QUALIFIED
    assert capability.display_role == DisplayRole.PROVIDER_PRIMARY
    assert capability.current_provider_eligible is True


def test_qualification_and_display_role_are_independent_axes() -> None:
    definition = _definition()
    qualified = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_qualified",
        state=QualificationState.QUALIFIED,
        effective_at=T0,
    )
    baseline = _role(
        definition.method_ref,
        assignment_ref="role_fragment_baseline",
        role=DisplayRole.RESEARCH_BASELINE,
        effective_at=T0,
    )
    registry = _registry(definition, qualifications=(qualified,), roles=(baseline,))
    head = _head(registry)

    capability = resolve_current_capability(
        registry,
        head,
        authority_head_sha256(head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )

    assert capability.qualification_state == QualificationState.QUALIFIED
    assert capability.display_role == DisplayRole.RESEARCH_BASELINE
    assert capability.research_inspectable is True
    assert capability.current_provider_eligible is False


def test_provider_primary_requires_opaque_approval_token() -> None:
    definition = _definition()
    payload = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T0,
    ).model_dump(mode="python")
    payload["approval_ref"] = None
    with pytest.raises(ValidationError, match="requires an approval_ref"):
        DisplayRoleAssignment.model_validate(payload)

    payload["approval_ref"] = "donor-approval"
    with pytest.raises(ValidationError):
        DisplayRoleAssignment.model_validate(payload)


def test_duplicate_provider_authority_for_measurement_scope_fails_closed() -> None:
    first = _definition("mth_fragment_raw_query_length_a")
    second = _definition("mth_fragment_raw_query_length_b")
    qualifications = tuple(
        _qualification(
            item.method_ref,
            record_ref=f"qual_provider_{index}",
            state=QualificationState.QUALIFIED,
            effective_at=T0,
        )
        for index, item in enumerate((first, second), start=1)
    )
    roles = tuple(
        _role(
            item.method_ref,
            assignment_ref=f"role_provider_{index}",
            role=DisplayRole.PROVIDER_PRIMARY,
            effective_at=T0,
        )
        for index, item in enumerate((first, second), start=1)
    )

    with pytest.raises(
        ValidationError, match="provider_primary authority windows overlap"
    ):
        _registry(first, second, qualifications=qualifications, roles=roles)


@pytest.mark.parametrize("mismatch", ("method", "version"))
def test_unknown_method_or_version_fails_closed(mismatch: str) -> None:
    registry = _registry(_definition())
    reference = MethodReference(
        method_id=(
            "mth_unknown_method"
            if mismatch == "method"
            else "mth_fragment_raw_query_length"
        ),
        version=("9.9.9" if mismatch == "version" else "1.0.0"),
    )
    with pytest.raises(RegistryIdentityError, match="not registered"):
        resolve_historical_capability(
            registry,
            reference,
            authority_scope="scope_provider_west",
            as_of=T1,
        )


@pytest.mark.parametrize("kind", ("unknown_asset", "asset_digest", "tool_version"))
def test_unknown_tool_and_asset_combinations_fail_closed(kind: str) -> None:
    payload = _definition().model_dump(mode="python")
    if kind == "unknown_asset":
        payload["assets"][0]["asset_id"] = "asset_unknown"
    elif kind == "asset_digest":
        payload["assets"][0]["content_sha256"] = "d" * 64
    else:
        payload["tools"][0]["version"] = "9.9.9"
    definition = MethodDefinition.model_validate(payload)
    with pytest.raises(ValidationError, match="unknown or mismatched"):
        _registry(definition)


@pytest.mark.parametrize(
    "mutation",
    (
        "version_skip",
        "wrong_digest",
        "older_timestamp",
        "removed_definition",
        "tampered_definition",
        "removed_qualification",
        "tampered_assignment",
    ),
)
def test_registry_transition_rejects_removal_mutation_and_chain_tampering(
    mutation: str,
) -> None:
    previous, definition, qualification, role = _active_provider_registry()
    definitions = list(previous.method_definitions)
    qualifications = list(previous.qualification_records)
    roles = list(previous.display_role_assignments)
    version = 2
    published_at = T2
    previous_digest = registry_sha256(previous)
    if mutation == "version_skip":
        version = 3
    elif mutation == "wrong_digest":
        previous_digest = "f" * 64
    elif mutation == "older_timestamp":
        published_at = T0
    elif mutation == "removed_definition":
        definitions = [_definition("mth_fragment_aligned_span")]
        qualifications = []
        roles = []
    elif mutation == "tampered_definition":
        definitions[0] = definition.model_copy(
            update={"parameter_schema_sha256": "d" * 64}
        )
    elif mutation == "removed_qualification":
        qualifications = []
    else:
        roles[0] = role.model_copy(update={"approval_ref": "approval_changed"})
    current = _registry(
        *definitions,
        qualifications=tuple(qualifications),
        roles=tuple(roles),
        version=version,
        published_at=published_at,
        previous=previous_digest,
    )

    with pytest.raises(RegistryTransitionError):
        replay_registry_transition(previous, current)


def test_current_and_historical_capability_semantic_replay_rejects_tampering() -> None:
    registry, definition, _, _ = _active_provider_registry()
    head = _head(registry)
    head_digest = authority_head_sha256(head)
    current = resolve_current_capability(
        registry,
        head,
        head_digest,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    historical = resolve_historical_capability(
        registry,
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    assert replay_current_capability(registry, head, head_digest, current) == current
    assert replay_historical_capability(registry, historical) == historical

    changed_current = current.model_copy(update={"research_inspectable": False})
    with pytest.raises(RegistryIdentityError, match="semantic replay"):
        replay_current_capability(registry, head, head_digest, changed_current)
    changed_historical = historical.model_copy(
        update={"historical_provider_designated": False}
    )
    with pytest.raises(RegistryIdentityError, match="semantic replay"):
        replay_historical_capability(registry, changed_historical)


def test_effective_provider_primary_uses_only_current_head_authority() -> None:
    registry, definition, _, _ = _active_provider_registry()
    head = _head(registry)
    assert (
        effective_provider_primary(
            registry,
            head,
            authority_head_sha256(head),
            family=MethodFamily.FRAGMENT_MEASUREMENT,
            quantity_id="qty_fragment_raw_query_length",
            unit="unit_base_pairs",
            authority_scope="scope_provider_west",
            as_of=T1,
        )
        == definition.method_ref
    )

    assert (
        effective_provider_primary(
            registry,
            head,
            authority_head_sha256(head),
            family=MethodFamily.FRAGMENT_MEASUREMENT,
            quantity_id="qty_fragment_raw_query_length",
            unit="unit_base_pairs",
            authority_scope="scope_provider_east",
            as_of=T1,
        )
        is None
    )


def _rich_registry() -> MethodRegistry:
    definition = _definition()
    qualification = _qualification(
        definition.method_ref,
        record_ref="qual_fragment_primary",
        state=QualificationState.QUALIFIED,
        effective_at=T0,
    )
    role = _role(
        definition.method_ref,
        assignment_ref="role_fragment_primary",
        role=DisplayRole.PROVIDER_PRIMARY,
        effective_at=T0,
    )
    return _registry(
        definition,
        qualifications=(qualification,),
        roles=(role,),
        revocations=(
            _revoke_qualification(
                qualification.record_ref,
                revocation_ref="revoke_fragment_qualification",
                effective_at=T2,
            ),
            _revoke_role(
                role.assignment_ref,
                revocation_ref="revoke_fragment_role",
                effective_at=T2,
            ),
        ),
    )


def _string_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, str):
        return [path]
    if isinstance(value, dict):
        result: list[tuple[Any, ...]] = []
        for key, item in value.items():
            result.extend(_string_paths(item, (*path, key)))
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            result.extend(_string_paths(item, (*path, index)))
        return result
    return []


def _set_path(value: Any, path: tuple[Any, ...], replacement: str) -> None:
    cursor = value
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement


def _privacy_cases() -> list[tuple[type[BaseModel], dict[str, Any], tuple[Any, ...]]]:
    registry, definition, _, _ = _active_provider_registry()
    head = _head(registry)
    capability = resolve_current_capability(
        registry,
        head,
        authority_head_sha256(head),
        definition.method_ref,
        authority_scope="scope_provider_west",
        as_of=T1,
    )
    values: tuple[tuple[type[BaseModel], BaseModel], ...] = (
        (MethodRegistry, _rich_registry()),
        (AuthorityHead, head),
        (CurrentMethodCapability, capability),
    )
    cases: list[tuple[type[BaseModel], dict[str, Any], tuple[Any, ...]]] = []
    for model, value in values:
        payload = value.model_dump(mode="json")
        for path in _string_paths(payload):
            cases.append((model, payload, path))
    return cases


@pytest.mark.parametrize(
    ("model", "payload", "path"),
    _privacy_cases(),
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
def test_every_serialized_string_slot_rejects_privacy_sentinels(
    model: type[BaseModel],
    payload: dict[str, Any],
    path: tuple[Any, ...],
) -> None:
    changed = copy.deepcopy(payload)
    _set_path(changed, path, "sample_donor_patient_run_read_path_sequence")
    with pytest.raises(ValidationError):
        model.model_validate(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("donor_id", "donor_123"),
        ("local_path", "/private/specimen.json"),
        ("read_id", "read_123"),
        ("sequence", "ACGTACGTACGTACGT"),
        ("input_sha256", "d" * 64),
        ("extensions", {"unsafe": True}),
    ),
)
def test_forbidden_privacy_and_extension_fields_are_rejected(
    field: str, value: object
) -> None:
    payload = _definition().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MethodDefinition.model_validate(payload)


def test_canonical_contract_contains_no_sensitive_or_unscoped_fields() -> None:
    rendered = canonical_contract_bytes(_rich_registry()).decode()
    forbidden = (
        "donor_id",
        "sample_id",
        "patient_id",
        "run_id",
        "read_id",
        "local_path",
        "sequence",
        "input_sha256",
        "extensions",
    )
    assert all(token not in rendered for token in forbidden)


@pytest.mark.parametrize(
    ("version", "accepted"),
    (
        ("1.0.0", True),
        ("1.0.0-local-ref", True),
        ("1.0.0-local-ref.b_2-x", True),
        ("1.0.0-local-", False),
        ("1.0.0-beta", False),
        ("1.0.0-local-a..b", False),
        ("1.0.0-local-Ref", False),
        ("1.0.0-local-" + "a" * 65, False),
        ("1.0-local-ref", False),
    ),
)
def test_method_versions_accept_only_the_local_reference_suffix(
    version: str, accepted: bool
) -> None:
    if accepted:
        assert MethodReference(method_id="mth_fragment_span", version=version).version == version
        assert _definition(version=version).version == version
    else:
        with pytest.raises(ValidationError):
            MethodReference(method_id="mth_fragment_span", version=version)


def test_tool_and_asset_versions_stay_plain_semver() -> None:
    with pytest.raises(ValidationError):
        ToolReference(tool_id="tool_fragment_counter", version="1.0.0-local-ref", artifact_sha256="a" * 64)
    with pytest.raises(ValidationError):
        AssetReference(asset_id="asset_fragment_policy", version="1.0.0-local-ref", content_sha256="b" * 64)
