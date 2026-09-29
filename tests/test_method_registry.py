"""Exact contract tests for the versioned method registry."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from evidence_inspector.method_registry import (
    AssetReference,
    AssetRegistration,
    DisplayRole,
    MethodFamily,
    MethodReference,
    MethodRegistration,
    MethodRegistry,
    QualificationState,
    RegistryIdentityError,
    RegistryTransitionError,
    ToolReference,
    ToolRegistration,
    canonical_registry_bytes,
    effective_provider_primary,
    registry_from_canonical_bytes,
    registry_sha256,
    replay_method_capability,
    replay_registry_transition,
    resolve_method_capability,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
T3 = datetime(2026, 4, 1, tzinfo=UTC)


def _tool() -> ToolRegistration:
    return ToolRegistration(
        tool_id="fragment-counter", version="1.0.0", artifact_sha256="a" * 64
    )


def _asset() -> AssetRegistration:
    return AssetRegistration(
        asset_id="fragment-policy", version="1.0.0", content_sha256="b" * 64
    )


def _method(
    method_id: str,
    version: str,
    role: DisplayRole,
    *,
    qualification: QualificationState = QualificationState.DEVELOPMENT_UNQUALIFIED,
    effective_at: datetime | None = None,
    revoked_at: datetime | None = None,
    authority_scope: str | None = None,
    approval_ref: str | None = None,
) -> MethodRegistration:
    return MethodRegistration(
        method_id=method_id,
        version=version,
        family=MethodFamily.FRAGMENT_MEASUREMENT,
        quantity_id="raw_query_length",
        unit="base_pairs",
        parameter_schema_sha256="c" * 64,
        tools=(
            ToolReference(
                tool_id="fragment-counter",
                version="1.0.0",
                artifact_sha256="a" * 64,
            ),
        ),
        assets=(
            AssetReference(
                asset_id="fragment-policy",
                version="1.0.0",
                content_sha256="b" * 64,
            ),
        ),
        qualification_state=qualification,
        display_role=role,
        effective_at=effective_at,
        revoked_at=revoked_at,
        authority_scope=authority_scope,
        approval_ref=approval_ref,
    )


def _registry(
    *methods: MethodRegistration,
    version: int = 1,
    published_at: datetime = T0,
    previous: str | None = None,
    tools: tuple[ToolRegistration, ...] | None = None,
    assets: tuple[AssetRegistration, ...] | None = None,
) -> MethodRegistry:
    return MethodRegistry(
        registry_id="traceback-methods",
        registry_version=version,
        published_at=published_at,
        previous_registry_sha256=previous,
        tools=tools or (_tool(),),
        assets=assets or (_asset(),),
        methods=tuple(sorted(methods, key=lambda item: (item.method_id, item.version))),
    )


def _baseline() -> MethodRegistration:
    return _method(
        "fragment-raw-query-length",
        "1.0.0",
        DisplayRole.RESEARCH_BASELINE,
    )


def _primary(
    method_id: str,
    version: str,
    effective_at: datetime,
    revoked_at: datetime | None = None,
    *,
    qualification: QualificationState = QualificationState.QUALIFIED,
) -> MethodRegistration:
    return _method(
        method_id,
        version,
        DisplayRole.PROVIDER_PRIMARY,
        qualification=qualification,
        effective_at=effective_at,
        revoked_at=revoked_at,
        authority_scope="provider-west",
        approval_ref=f"approval-{method_id}-{version}",
    )


def test_closed_models_canonical_digest_and_json_round_trip() -> None:
    registry = _registry(_baseline())

    encoded = canonical_registry_bytes(registry)

    assert encoded == canonical_registry_bytes(registry)
    assert registry_sha256(registry) == registry_sha256(registry)
    assert registry_from_canonical_bytes(encoded) == registry
    assert json.loads(encoded)["schema_version"] == "traceback.method-registry.v1"

    indented = json.dumps(registry.model_dump(mode="json"), indent=2).encode()
    with pytest.raises(RegistryIdentityError, match="not canonical"):
        registry_from_canonical_bytes(indented)

    payload = registry.model_dump(mode="python")
    payload["extensions"] = {"anything": True}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MethodRegistry.model_validate(payload)


@pytest.mark.parametrize("missing", ("authority_scope", "approval_ref", "effective_at"))
def test_provider_primary_requires_explicit_effective_authority(missing: str) -> None:
    payload = _primary("provider-fragment", "1.0.0", T0).model_dump(mode="python")
    payload[missing] = None

    with pytest.raises(ValidationError, match="provider_primary requires"):
        MethodRegistration.model_validate(payload)


def test_qualification_role_and_provider_capability_are_independent() -> None:
    unqualified_primary = _primary(
        "provider-fragment",
        "1.0.0",
        T0,
        qualification=QualificationState.DEVELOPMENT_UNQUALIFIED,
    )
    qualified_baseline = _method(
        "qualified-research",
        "1.0.0",
        DisplayRole.RESEARCH_BASELINE,
        qualification=QualificationState.QUALIFIED,
    )
    registry = _registry(unqualified_primary, qualified_baseline)

    primary_capability = resolve_method_capability(
        registry,
        unqualified_primary.method_ref,
        authority_scope="provider-west",
        as_of=T1,
    )
    research_capability = resolve_method_capability(
        registry,
        qualified_baseline.method_ref,
        authority_scope="provider-west",
        as_of=T1,
    )

    assert primary_capability.research_available is True
    assert primary_capability.provider_available is False
    assert primary_capability.effective_approval_ref is None
    assert research_capability.research_available is True
    assert research_capability.provider_available is False
    assert (
        effective_provider_primary(
            registry,
            family=MethodFamily.FRAGMENT_MEASUREMENT,
            quantity_id="raw_query_length",
            unit="base_pairs",
            authority_scope="provider-west",
            as_of=T1,
        )
        is None
    )


def test_revocation_and_effective_time_switch_provider_default_exactly() -> None:
    first = _primary("provider-fragment-a", "1.0.0", T0, T2)
    second = _primary("provider-fragment-b", "1.0.0", T2)
    registry = _registry(first, second)

    assert (
        effective_provider_primary(
            registry,
            family=MethodFamily.FRAGMENT_MEASUREMENT,
            quantity_id="raw_query_length",
            unit="base_pairs",
            authority_scope="provider-west",
            as_of=T1,
        )
        == first.method_ref
    )
    assert (
        effective_provider_primary(
            registry,
            family=MethodFamily.FRAGMENT_MEASUREMENT,
            quantity_id="raw_query_length",
            unit="base_pairs",
            authority_scope="provider-west",
            as_of=T2,
        )
        == second.method_ref
    )

    revoked = resolve_method_capability(
        registry,
        first.method_ref,
        authority_scope="provider-west",
        as_of=T2,
    )
    assert revoked.research_available is False
    assert revoked.provider_available is False
    assert revoked.effective_approval_ref is None


def test_future_wrong_scope_and_disabled_methods_never_become_defaults() -> None:
    future = _primary("provider-fragment", "1.0.0", T2)
    disabled = _method(
        "disabled-fragment",
        "1.0.0",
        DisplayRole.DISABLED,
        qualification=QualificationState.QUALIFIED,
    )
    registry = _registry(disabled, future)

    future_capability = resolve_method_capability(
        registry,
        future.method_ref,
        authority_scope="provider-west",
        as_of=T1,
    )
    wrong_scope = resolve_method_capability(
        registry,
        future.method_ref,
        authority_scope="provider-east",
        as_of=T3,
    )
    disabled_capability = resolve_method_capability(
        registry,
        disabled.method_ref,
        authority_scope="provider-west",
        as_of=T3,
    )

    assert future_capability.provider_available is False
    assert wrong_scope.provider_available is False
    assert disabled_capability.research_available is False
    assert disabled_capability.provider_available is False


def test_overlapping_provider_authority_for_same_measurement_fails_closed() -> None:
    with pytest.raises(ValidationError, match="authority windows overlap"):
        _registry(
            _primary("provider-fragment-a", "1.0.0", T0),
            _primary("provider-fragment-b", "1.0.0", T1),
        )


def test_one_quantity_cannot_split_duplicate_authority_by_unit() -> None:
    second = _primary("provider-fragment-b", "1.0.0", T1)
    payload = second.model_dump(mode="python")
    payload["unit"] = "nucleotides"
    conflicting_unit = MethodRegistration.model_validate(payload)

    with pytest.raises(ValidationError, match="conflicting units"):
        _registry(
            _primary("provider-fragment-a", "1.0.0", T0),
            conflicting_unit,
        )


def test_capability_digest_and_semantics_replay_exactly() -> None:
    primary = _primary("provider-fragment", "1.0.0", T0)
    registry = _registry(primary)
    capability = resolve_method_capability(
        registry,
        primary.method_ref,
        authority_scope="provider-west",
        as_of=T1,
    )

    assert replay_method_capability(registry, capability) == capability

    changed_digest = capability.model_copy(update={"registry_sha256": "f" * 64})
    with pytest.raises(RegistryIdentityError, match="digest mismatch"):
        replay_method_capability(registry, changed_digest)

    changed_availability = capability.model_copy(update={"research_available": False})
    with pytest.raises(RegistryIdentityError, match="semantic replay"):
        replay_method_capability(registry, changed_availability)


@pytest.mark.parametrize("mismatch", ("method", "version"))
def test_unknown_method_or_version_fails_closed(mismatch: str) -> None:
    registry = _registry(_baseline())
    reference = MethodReference(
        method_id=("unknown-method" if mismatch == "method" else _baseline().method_id),
        version=("9.9.9" if mismatch == "version" else "1.0.0"),
    )

    with pytest.raises(RegistryIdentityError, match="not registered"):
        resolve_method_capability(
            registry,
            reference,
            authority_scope="provider-west",
            as_of=T1,
        )


@pytest.mark.parametrize("kind", ("unknown_asset", "asset_digest", "tool_version"))
def test_unknown_tool_and_asset_combinations_fail_closed(kind: str) -> None:
    payload = _baseline().model_dump(mode="python")
    if kind == "unknown_asset":
        payload["assets"][0]["asset_id"] = "not-registered"
    elif kind == "asset_digest":
        payload["assets"][0]["content_sha256"] = "d" * 64
    else:
        payload["tools"][0]["version"] = "9.9.9"
    method = MethodRegistration.model_validate(payload)

    with pytest.raises(ValidationError, match="unknown or mismatched"):
        _registry(method)


@pytest.mark.parametrize(
    "mutation",
    (
        "version_skip",
        "wrong_digest",
        "older_timestamp",
        "removed_method",
        "tampered_identity",
        "cleared_revocation",
        "backdated_revocation",
    ),
)
def test_registry_transition_rejects_invalid_changes_and_tampering(
    mutation: str,
) -> None:
    primary = _primary("provider-fragment", "1.0.0", T0)
    previous = _registry(_baseline(), primary)
    current_methods = list(previous.methods)
    version = 2
    published_at = T2
    previous_digest = registry_sha256(previous)

    if mutation == "version_skip":
        version = 3
    elif mutation == "wrong_digest":
        previous_digest = "f" * 64
    elif mutation == "older_timestamp":
        published_at = T0
    elif mutation == "removed_method":
        current_methods = [primary]
    elif mutation == "tampered_identity":
        payload = previous.methods[0].model_dump(mode="python")
        payload["parameter_schema_sha256"] = "e" * 64
        current_methods[0] = MethodRegistration.model_validate(payload)
    elif mutation == "cleared_revocation":
        previous = _registry(
            _baseline().model_copy(update={"revoked_at": T1}),
            primary,
        )
        previous_digest = registry_sha256(previous)
        current_methods = [_baseline(), primary]
    else:
        current_methods[0] = _baseline().model_copy(
            update={"revoked_at": datetime(2025, 12, 1, tzinfo=UTC)}
        )

    current = _registry(
        *current_methods,
        version=version,
        published_at=published_at,
        previous=previous_digest,
    )

    with pytest.raises(RegistryTransitionError):
        replay_registry_transition(previous, current)


def test_registry_transition_replays_append_and_monotonic_revocation() -> None:
    previous = _registry(_baseline())
    revoked = _baseline().model_copy(update={"revoked_at": T1})
    challenger = _method(
        "fragment-aligned-reference-span",
        "1.0.0",
        DisplayRole.RESEARCH_CHALLENGER,
    )
    current = _registry(
        challenger,
        revoked,
        version=2,
        published_at=T2,
        previous=registry_sha256(previous),
    )

    assert replay_registry_transition(previous, current) == current
    assert registry_from_canonical_bytes(canonical_registry_bytes(current)) == current


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("donor_id", "donor-123"),
        ("local_path", "/private/specimen.json"),
        ("read_id", "read-123"),
        ("sequence", "ACGTACGTACGTACGT"),
        ("input_sha256", "d" * 64),
        ("extensions", {"unsafe": True}),
    ),
)
def test_privacy_forbidden_fields_are_rejected(field: str, value: object) -> None:
    payload = _baseline().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError, match="extra_forbidden"):
        MethodRegistration.model_validate(payload)


@pytest.mark.parametrize("method_id", ("donor-123", "ACGTACGTACGTACGT"))
def test_privacy_sensitive_identifier_values_are_rejected(method_id: str) -> None:
    payload = _baseline().model_dump(mode="python")
    payload["method_id"] = method_id

    with pytest.raises(ValidationError, match="donor identity|sequence-like"):
        MethodRegistration.model_validate(payload)


def test_canonical_registry_contains_only_allowlisted_contract_keys() -> None:
    payload = json.loads(canonical_registry_bytes(_registry(_baseline())))
    rendered = json.dumps(payload, sort_keys=True)
    forbidden = (
        "donor_id",
        "local_path",
        "read_id",
        "sequence",
        "input_sha256",
        "extensions",
    )

    assert all(token not in rendered for token in forbidden)
