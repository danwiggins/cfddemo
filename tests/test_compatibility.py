"""Offline contract tests for the pure E05 compatibility engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from evidence_inspector.compatibility import (
    AllowedMethodDefinition,
    CompatibilityContractError,
    CompatibilityMismatchKey,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityPolicyReference,
    CompatibilityRequest,
    CompatibilitySelection,
    CompatibilitySelectionRequest,
    ExecutionState,
    InformationState,
    MeasurementCompatibilityKey,
    MeasurementCompatibilityPolicy,
    RemediationCode,
    ResultSchemaReference,
    TrustState,
    VerifiedMeasurementRecord,
    canonical_compatibility_bytes,
    compatibility_contract_from_canonical_bytes,
    compatibility_policy_sha256,
    decide_compatibility,
    replay_compatibility_decision,
    replay_compatibility_selection,
    select_compatible_records,
)
from evidence_inspector.method_registry import (
    AssetReference,
    CurrentMethodCapability,
    DisplayRole,
    MethodDefinition,
    MethodFamily,
    QualificationState,
    ToolReference,
    method_definition_sha256,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
HEAD_SHA256 = "a" * 64
REGISTRY_SHA256 = "b" * 64


def _asset(name: str, digest: str) -> AssetReference:
    return AssetReference(
        asset_id=f"asset_{name}",
        version="1.0.0",
        content_sha256=digest * 64,
    )


ASSETS = tuple(
    sorted(
        (
            _asset("atlas_a", "1"),
            _asset("atlas_b", "2"),
            _asset("grid_a", "3"),
            _asset("grid_b", "4"),
            _asset("panel_a", "5"),
            _asset("panel_b", "6"),
            _asset("reference_a", "7"),
            _asset("reference_b", "8"),
        ),
        key=lambda item: (item.asset_id, item.version),
    )
)
ASSET_BY_ID = {item.asset_id: item for item in ASSETS}


def _method(
    *,
    method_id: str = "mth_fragment_alpha",
    version: str = "1.0.0",
    family: MethodFamily = MethodFamily.FRAGMENT_MEASUREMENT,
    quantity_id: str = "qty_fragment_fraction",
    unit: str = "unit_fraction",
    parameter_digest: str = "9" * 64,
) -> MethodDefinition:
    return MethodDefinition(
        method_id=method_id,
        version=version,
        family=family,
        quantity_id=quantity_id,
        unit=unit,
        parameter_schema_sha256=parameter_digest,
        tools=(
            ToolReference(
                tool_id="tool_measurement_alpha",
                version="1.0.0",
                artifact_sha256="c" * 64,
            ),
        ),
        assets=ASSETS,
    )


def _capability(
    method: MethodDefinition,
    *,
    registry_sha256: str = REGISTRY_SHA256,
    registry_version: int = 2,
    authority_head_sha256: str = HEAD_SHA256,
    authority_revision: int = 4,
    qualification_state: QualificationState = (
        QualificationState.DEVELOPMENT_UNQUALIFIED
    ),
) -> CurrentMethodCapability:
    return CurrentMethodCapability(
        registry_sha256=registry_sha256,
        registry_version=registry_version,
        authority_head_sha256=authority_head_sha256,
        authority_revision=authority_revision,
        method_definition_sha256=method_definition_sha256(method),
        method_ref=method.method_ref,
        authority_scope="scope_research_alpha",
        as_of=NOW,
        qualification_state=qualification_state,
        display_role=DisplayRole.RESEARCH_BASELINE,
        research_inspectable=True,
        current_provider_eligible=False,
        effective_approval_ref=None,
    )


def _key(
    method: MethodDefinition,
    *,
    result_schema: ResultSchemaReference | None = None,
    reference_asset: AssetReference | None = ASSET_BY_ID["asset_reference_a"],
    grid_asset: AssetReference | None = ASSET_BY_ID["asset_grid_a"],
    atlas_asset: AssetReference | None = ASSET_BY_ID["asset_atlas_a"],
    panel_asset: AssetReference | None = ASSET_BY_ID["asset_panel_a"],
    normalization: str | None = "sem_normalization_alpha",
    coordinate: str | None = "sem_coordinate_alpha",
    denominator: str | None = "sem_denominator_alpha",
    policy_ref: CompatibilityPolicyReference | None = None,
) -> MeasurementCompatibilityKey:
    return MeasurementCompatibilityKey(
        measurement_family=method.family,
        quantity_id=method.quantity_id,
        unit=method.unit,
        result_schema=result_schema
        or ResultSchemaReference(
            schema_id="schema_measurement_alpha", version="1.0.0"
        ),
        reference_asset=reference_asset,
        grid_asset=grid_asset,
        atlas_asset=atlas_asset,
        panel_asset=panel_asset,
        normalization_semantics_id=normalization,
        coordinate_semantics_id=coordinate,
        denominator_semantics_id=denominator,
        registered_policy=policy_ref
        or CompatibilityPolicyReference(
            policy_id="policy_longitudinal_alpha", version="1.0.0"
        ),
    )


def _record(
    suffix: str,
    *,
    method: MethodDefinition | None = None,
    key: MeasurementCompatibilityKey | None = None,
    execution_state: ExecutionState = ExecutionState.COMPLETE,
    information_state: InformationState = InformationState.SUFFICIENT,
    trust_state: TrustState = TrustState.VERIFIED,
    registry_sha256: str = REGISTRY_SHA256,
    registry_version: int = 2,
    authority_head_sha256: str = HEAD_SHA256,
    authority_revision: int = 4,
    qualification_state: QualificationState = (
        QualificationState.DEVELOPMENT_UNQUALIFIED
    ),
) -> VerifiedMeasurementRecord:
    exact_method = method or _method()
    return VerifiedMeasurementRecord(
        result_id=f"result_{suffix}",
        result_sha256=("d" if suffix == "alpha" else "e") * 64,
        bundle_id=f"bundle_{suffix}",
        bundle_sha256=("f" if suffix == "alpha" else "0") * 64,
        method=exact_method,
        method_definition_sha256=method_definition_sha256(exact_method),
        current_capability=_capability(
            exact_method,
            registry_sha256=registry_sha256,
            registry_version=registry_version,
            authority_head_sha256=authority_head_sha256,
            authority_revision=authority_revision,
            qualification_state=qualification_state,
        ),
        execution_state=execution_state,
        information_state=information_state,
        trust_state=trust_state,
        compatibility_key=key or _key(exact_method),
    )


def _policy_for(
    *records: VerifiedMeasurementRecord,
    policy_id: str = "policy_longitudinal_alpha",
    version: str = "1.0.0",
    delta_allowed: bool = True,
    shared_axis_allowed: bool = True,
) -> CompatibilityPolicy:
    grouped: dict[tuple[str, str, str], list[VerifiedMeasurementRecord]] = {}
    for record in records:
        key = record.compatibility_key
        grouped.setdefault(
            (key.measurement_family.value, key.quantity_id, key.unit), []
        ).append(record)
    rules = []
    for group_key in sorted(grouped):
        grouped_records = grouped[group_key]
        first = grouped_records[0]
        methods = tuple(
            sorted(
                {
                    AllowedMethodDefinition(
                        method_ref=item.method.method_ref,
                        method_definition_sha256=(
                            item.method_definition_sha256
                        ),
                    )
                    for item in grouped_records
                },
                key=lambda item: item.sort_key,
            )
        )
        schemas = tuple(
            sorted(
                {item.compatibility_key.result_schema for item in grouped_records},
                key=lambda item: (item.schema_id, item.version),
            )
        )
        rules.append(
            MeasurementCompatibilityPolicy(
                measurement_family=first.compatibility_key.measurement_family,
                quantity_id=first.compatibility_key.quantity_id,
                unit=first.compatibility_key.unit,
                allowed_method_definitions=methods,
                allowed_result_schemas=schemas,
                delta_allowed_when_comparable=delta_allowed,
                shared_axis_allowed_when_comparable=shared_axis_allowed,
            )
        )
    return CompatibilityPolicy(
        policy_id=policy_id,
        version=version,
        registry_sha256=REGISTRY_SHA256,
        registry_version=2,
        authority_head_sha256=HEAD_SHA256,
        authority_revision=4,
        measurement_policies=tuple(rules),
    )


def _request(
    left: VerifiedMeasurementRecord,
    right: VerifiedMeasurementRecord,
    *,
    policy: CompatibilityPolicy | None = None,
    trusted_policy_sha256: str | None = None,
    trusted_authority_head_sha256: str = HEAD_SHA256,
) -> CompatibilityRequest:
    exact_policy = policy or _policy_for(left, right)
    return CompatibilityRequest(
        left=left,
        right=right,
        policy=exact_policy,
        trusted_policy_sha256=trusted_policy_sha256
        or compatibility_policy_sha256(exact_policy),
        trusted_authority_head_sha256=trusted_authority_head_sha256,
    )


def test_exact_identity_is_comparable_and_policy_controls_render_permissions() -> None:
    left = _record("alpha")
    right = _record("beta")
    policy = _policy_for(
        left,
        right,
        delta_allowed=True,
        shared_axis_allowed=False,
    )

    decision = decide_compatibility(_request(left, right, policy=policy))

    assert decision.outcome == CompatibilityOutcome.COMPARABLE
    assert decision.mismatch_keys == ()
    assert decision.missing_fields == ()
    assert decision.delta_allowed
    assert not decision.shared_axis_allowed
    assert decision.remediation_code == RemediationCode.NONE


def test_swapped_sides_produce_identical_canonical_decision_and_digest() -> None:
    left = _record("alpha")
    right = _record("beta")
    policy = _policy_for(left, right)

    forward = decide_compatibility(_request(left, right, policy=policy))
    reverse = decide_compatibility(_request(right, left, policy=policy))

    assert reverse == forward
    assert reverse.decision_sha256 == forward.decision_sha256
    assert forward.binding.left.result_id == "result_alpha"
    assert forward.binding.right.result_id == "result_beta"


def test_distinct_records_may_have_identical_result_bytes() -> None:
    left = _record("alpha")
    right = _record("beta").model_copy(
        update={"result_sha256": left.result_sha256}
    )

    decision = decide_compatibility(_request(left, right))

    assert decision.outcome == CompatibilityOutcome.COMPARABLE
    assert decision.binding.left.result_sha256 == (
        decision.binding.right.result_sha256
    )


def test_policy_digest_is_deterministic_and_binds_policy_changes() -> None:
    left = _record("alpha")
    right = _record("beta")
    policy = _policy_for(left, right)
    repeated = _policy_for(left, right)
    changed = _policy_for(left, right, delta_allowed=False)

    assert compatibility_policy_sha256(repeated) == (
        compatibility_policy_sha256(policy)
    )
    assert compatibility_policy_sha256(changed) != (
        compatibility_policy_sha256(policy)
    )


def test_policy_cannot_weaken_required_compatibility_dimensions() -> None:
    policy = _policy_for(_record("alpha"), _record("beta"))
    payload = policy.measurement_policies[0].model_dump(mode="json")
    payload["required_dimensions"] = payload["required_dimensions"][:-1]

    with pytest.raises(ValidationError, match="every nullable"):
        MeasurementCompatibilityPolicy.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("mutation", "expected_outcome", "expected_keys"),
    [
        (
            "measurement_family",
            CompatibilityOutcome.INCOMPATIBLE,
            {
                CompatibilityMismatchKey.MEASUREMENT_FAMILY,
                CompatibilityMismatchKey.METHOD_DEFINITION,
                CompatibilityMismatchKey.METHOD_ID,
            },
        ),
        (
            "quantity_id",
            CompatibilityOutcome.DIFFERENT_QUANTITY,
            {
                CompatibilityMismatchKey.QUANTITY_ID,
                CompatibilityMismatchKey.METHOD_DEFINITION,
                CompatibilityMismatchKey.METHOD_ID,
            },
        ),
        (
            "unit",
            CompatibilityOutcome.INCOMPATIBLE,
            {
                CompatibilityMismatchKey.UNIT,
                CompatibilityMismatchKey.METHOD_DEFINITION,
                CompatibilityMismatchKey.METHOD_ID,
            },
        ),
        (
            "result_schema_id",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.RESULT_SCHEMA_ID},
        ),
        (
            "result_schema_version",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.RESULT_SCHEMA_VERSION},
        ),
        (
            "reference_asset",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.REFERENCE_ASSET},
        ),
        (
            "grid_asset",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.GRID_ASSET},
        ),
        (
            "atlas_asset",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.ATLAS_ASSET},
        ),
        (
            "panel_asset",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.PANEL_ASSET},
        ),
        (
            "normalization_semantics_id",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.NORMALIZATION_SEMANTICS},
        ),
        (
            "coordinate_semantics_id",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.COORDINATE_SEMANTICS},
        ),
        (
            "denominator_semantics_id",
            CompatibilityOutcome.INCOMPATIBLE,
            {CompatibilityMismatchKey.DENOMINATOR_SEMANTICS},
        ),
        (
            "registered_policy_id",
            CompatibilityOutcome.UNKNOWN,
            {CompatibilityMismatchKey.REGISTERED_POLICY_ID},
        ),
        (
            "registered_policy_version",
            CompatibilityOutcome.UNKNOWN,
            {CompatibilityMismatchKey.REGISTERED_POLICY_VERSION},
        ),
        (
            "method_id",
            CompatibilityOutcome.INCOMPATIBLE,
            {
                CompatibilityMismatchKey.METHOD_DEFINITION,
                CompatibilityMismatchKey.METHOD_ID,
            },
        ),
        (
            "method_version",
            CompatibilityOutcome.INCOMPATIBLE,
            {
                CompatibilityMismatchKey.METHOD_DEFINITION,
                CompatibilityMismatchKey.METHOD_VERSION,
            },
        ),
    ],
)
def test_every_compatibility_dimension_fails_closed_with_exact_sorted_keys(
    mutation: str,
    expected_outcome: CompatibilityOutcome,
    expected_keys: set[CompatibilityMismatchKey],
) -> None:
    left = _record("alpha")
    method = left.method
    key = _key(method)
    if mutation == "measurement_family":
        method = _method(
            method_id="mth_copy_alpha",
            family=MethodFamily.COPY_NUMBER,
        )
        key = _key(method)
    elif mutation == "quantity_id":
        method = _method(
            method_id="mth_fragment_quantity_beta",
            quantity_id="qty_fragment_count",
        )
        key = _key(method)
    elif mutation == "unit":
        method = _method(
            method_id="mth_fragment_unit_beta",
            unit="unit_count",
        )
        key = _key(method)
    elif mutation == "result_schema_id":
        key = _key(
            method,
            result_schema=ResultSchemaReference(
                schema_id="schema_measurement_beta", version="1.0.0"
            ),
        )
    elif mutation == "result_schema_version":
        key = _key(
            method,
            result_schema=ResultSchemaReference(
                schema_id="schema_measurement_alpha", version="2.0.0"
            ),
        )
    elif mutation == "reference_asset":
        key = _key(method, reference_asset=ASSET_BY_ID["asset_reference_b"])
    elif mutation == "grid_asset":
        key = _key(method, grid_asset=ASSET_BY_ID["asset_grid_b"])
    elif mutation == "atlas_asset":
        key = _key(method, atlas_asset=ASSET_BY_ID["asset_atlas_b"])
    elif mutation == "panel_asset":
        key = _key(method, panel_asset=ASSET_BY_ID["asset_panel_b"])
    elif mutation == "normalization_semantics_id":
        key = _key(method, normalization="sem_normalization_beta")
    elif mutation == "coordinate_semantics_id":
        key = _key(method, coordinate="sem_coordinate_beta")
    elif mutation == "denominator_semantics_id":
        key = _key(method, denominator="sem_denominator_beta")
    elif mutation == "registered_policy_id":
        key = _key(
            method,
            policy_ref=CompatibilityPolicyReference(
                policy_id="policy_longitudinal_beta", version="1.0.0"
            ),
        )
    elif mutation == "registered_policy_version":
        key = _key(
            method,
            policy_ref=CompatibilityPolicyReference(
                policy_id="policy_longitudinal_alpha", version="2.0.0"
            ),
        )
    elif mutation == "method_id":
        method = _method(method_id="mth_fragment_beta")
        key = _key(method)
    elif mutation == "method_version":
        method = _method(version="2.0.0")
        key = _key(method)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    right = _record("beta", method=method, key=key)
    policy = _policy_for(left, right)

    decision = decide_compatibility(_request(left, right, policy=policy))

    assert decision.outcome == expected_outcome
    assert set(decision.mismatch_keys) == expected_keys
    assert decision.mismatch_keys == tuple(sorted(decision.mismatch_keys, key=str))
    assert not decision.delta_allowed
    assert not decision.shared_axis_allowed


def test_missing_required_data_is_unknown_with_exact_missing_field() -> None:
    left = _record("alpha")
    method = left.method
    right = _record("beta", method=method, key=_key(method, atlas_asset=None))

    decision = decide_compatibility(_request(left, right))

    assert decision.outcome == CompatibilityOutcome.UNKNOWN
    assert decision.missing_fields == ("right.atlas_asset",)
    assert decision.remediation_code == RemediationCode.PROVIDE_REQUIRED_METADATA
    assert not decision.delta_allowed
    assert not decision.shared_axis_allowed


@pytest.mark.parametrize(
    ("record_kwargs", "remediation"),
    [
        (
            {"execution_state": ExecutionState.FAILED},
            RemediationCode.RESOLVE_EXECUTION,
        ),
        (
            {"execution_state": ExecutionState.NOT_RUN},
            RemediationCode.RESOLVE_EXECUTION,
        ),
        (
            {"information_state": InformationState.INSUFFICIENT},
            RemediationCode.RESOLVE_INFORMATION,
        ),
        (
            {"information_state": InformationState.UNKNOWN},
            RemediationCode.RESOLVE_INFORMATION,
        ),
        (
            {"trust_state": TrustState.UNVERIFIED},
            RemediationCode.VERIFY_RESULT,
        ),
        (
            {"trust_state": TrustState.UNKNOWN},
            RemediationCode.VERIFY_RESULT,
        ),
        (
            {"trust_state": TrustState.REVOKED},
            RemediationCode.REPLACE_REVOKED_RESULT,
        ),
    ],
)
def test_failed_insufficient_revoked_and_unverified_inputs_are_never_comparable(
    record_kwargs: dict[str, object], remediation: RemediationCode
) -> None:
    left = _record("alpha")
    right = _record("beta", **record_kwargs)  # type: ignore[arg-type]

    decision = decide_compatibility(_request(left, right))

    assert decision.outcome == CompatibilityOutcome.UNKNOWN
    assert decision.remediation_code == remediation
    assert not decision.delta_allowed
    assert not decision.shared_axis_allowed


def test_qualification_is_bound_but_does_not_become_compatibility() -> None:
    left = _record(
        "alpha", qualification_state=QualificationState.DEVELOPMENT_UNQUALIFIED
    )
    right = _record("beta", qualification_state=QualificationState.UNKNOWN)

    decision = decide_compatibility(_request(left, right))

    assert decision.outcome == CompatibilityOutcome.COMPARABLE
    assert decision.binding.left.capability_sha256 != (
        decision.binding.right.capability_sha256
    )


def test_stale_authority_and_policy_fail_closed() -> None:
    left = _record("alpha")
    stale_authority = _record(
        "beta", authority_head_sha256="1" * 64, authority_revision=3
    )
    authority_decision = decide_compatibility(_request(left, stale_authority))
    assert authority_decision.outcome == CompatibilityOutcome.UNKNOWN
    assert authority_decision.remediation_code == RemediationCode.REFRESH_AUTHORITY

    stale_registry = _record(
        "beta", registry_sha256="3" * 64, registry_version=3
    )
    registry_decision = decide_compatibility(_request(left, stale_registry))
    assert registry_decision.outcome == CompatibilityOutcome.UNKNOWN
    assert registry_decision.remediation_code == RemediationCode.REFRESH_AUTHORITY

    right = _record("beta")
    policy = _policy_for(left, right)
    policy_decision = decide_compatibility(
        _request(left, right, policy=policy, trusted_policy_sha256="2" * 64)
    )
    assert policy_decision.outcome == CompatibilityOutcome.UNKNOWN
    assert policy_decision.remediation_code == RemediationCode.REFRESH_POLICY


def test_unknown_method_and_result_schema_are_not_inferred() -> None:
    left = _record("alpha")
    other_method = _method(method_id="mth_fragment_beta")
    unknown_method = _record("beta", method=other_method, key=_key(other_method))
    method_policy = _policy_for(left)
    method_decision = decide_compatibility(
        _request(left, unknown_method, policy=method_policy)
    )
    assert method_decision.outcome == CompatibilityOutcome.UNKNOWN
    assert method_decision.remediation_code == RemediationCode.REGISTER_METHOD

    unknown_schema = _record(
        "beta",
        key=_key(
            left.method,
            result_schema=ResultSchemaReference(
                schema_id="schema_measurement_unknown", version="1.0.0"
            ),
        ),
    )
    schema_decision = decide_compatibility(
        _request(left, unknown_schema, policy=method_policy)
    )
    assert schema_decision.outcome == CompatibilityOutcome.UNKNOWN
    assert schema_decision.remediation_code == (
        RemediationCode.REGISTER_RESULT_SCHEMA
    )


def test_policy_binds_exact_method_definition_not_only_reused_reference() -> None:
    trusted_left = _record("alpha")
    trusted_right = _record("beta")
    policy = _policy_for(trusted_left, trusted_right)
    forged_method = _method(parameter_digest="8" * 64)
    forged_left = _record(
        "alpha", method=forged_method, key=_key(forged_method)
    )
    forged_right = _record(
        "beta", method=forged_method, key=_key(forged_method)
    )

    decision = decide_compatibility(
        _request(forged_left, forged_right, policy=policy)
    )

    assert decision.outcome == CompatibilityOutcome.UNKNOWN
    assert decision.remediation_code == RemediationCode.REGISTER_METHOD


def test_decision_exact_json_round_trip_replay_and_tamper_rejection() -> None:
    request = _request(_record("alpha"), _record("beta"))
    decision = decide_compatibility(request)
    encoded = canonical_compatibility_bytes(decision)

    reloaded = compatibility_contract_from_canonical_bytes(
        type(decision), encoded
    )

    assert reloaded == decision
    assert replay_compatibility_decision(request, reloaded) == decision
    rendered = encoded.decode("utf-8")
    for forbidden in (
        "donor",
        "sample_id",
        "run_id",
        "read_id",
        "sequence",
        "local_path",
        "/Users/",
    ):
        assert forbidden not in rendered
    payload = json.loads(encoded)
    payload["delta_allowed"] = False
    with pytest.raises(ValidationError, match="decision digest"):
        type(decision).model_validate_json(json.dumps(payload))


def test_all_public_input_and_output_contracts_round_trip_canonical_json() -> None:
    left = _record("alpha")
    right = _record("beta")
    policy = _policy_for(left, right)
    request = _request(left, right, policy=policy)
    decision = decide_compatibility(request)
    selection_request = CompatibilitySelectionRequest(
        anchor_result_id=left.result_id,
        records=(left, right),
        policy=policy,
        trusted_policy_sha256=compatibility_policy_sha256(policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )
    selection = select_compatible_records(selection_request)

    for contract in (
        left,
        left.compatibility_key,
        policy,
        request,
        decision,
        selection_request,
        selection,
    ):
        encoded = canonical_compatibility_bytes(contract)
        assert compatibility_contract_from_canonical_bytes(
            type(contract), encoded
        ) == contract


def test_selector_requires_explicit_anchor_without_mutation_or_primary_inference(
) -> None:
    alpha = _record("alpha")
    beta = _record("beta")
    changed_key = _key(alpha.method, normalization="sem_normalization_beta")
    gamma = _record("gamma", key=changed_key)
    policy = _policy_for(alpha, beta, gamma)
    records = (alpha, beta, gamma)
    request = CompatibilitySelectionRequest(
        anchor_result_id=alpha.result_id,
        records=records,
        policy=policy,
        trusted_policy_sha256=compatibility_policy_sha256(policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )

    selection = select_compatible_records(request)

    assert request.records == records
    assert selection.anchor_result_id == "result_alpha"
    assert selection.selected_result_ids == ("result_alpha", "result_beta")
    assert [item.decision_sha256 for item in selection.decisions] == sorted(
        item.decision_sha256 for item in selection.decisions
    )
    assert len(selection.selection_sha256) == 64
    assert replay_compatibility_selection(request, selection) == selection


def test_selection_replay_rejects_stale_membership_and_invalid_inclusion() -> None:
    alpha = _record("alpha")
    beta = _record("beta")
    gamma = _record(
        "gamma",
        key=_key(alpha.method, normalization="sem_normalization_beta"),
    )
    policy = _policy_for(alpha, beta, gamma)
    request = CompatibilitySelectionRequest(
        anchor_result_id=alpha.result_id,
        records=(alpha, beta, gamma),
        policy=policy,
        trusted_policy_sha256=compatibility_policy_sha256(policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )
    selection = select_compatible_records(request)
    payload = selection.model_dump(mode="json")
    payload["selected_result_ids"] = [
        "result_alpha",
        "result_beta",
        "result_gamma",
    ]
    with pytest.raises(ValidationError, match="comparable decision outcomes"):
        CompatibilitySelection.model_validate_json(json.dumps(payload))

    delta = _record("delta")
    stale_policy = _policy_for(alpha, beta, delta)
    stale_request = CompatibilitySelectionRequest(
        anchor_result_id=alpha.result_id,
        records=(alpha, beta, delta),
        policy=stale_policy,
        trusted_policy_sha256=compatibility_policy_sha256(stale_policy),
        trusted_authority_head_sha256=HEAD_SHA256,
    )
    stale_selection = select_compatible_records(stale_request)
    with pytest.raises(CompatibilityContractError, match="canonical replay"):
        replay_compatibility_selection(request, stale_selection)


@pytest.mark.parametrize(
    "reserved_term",
    [
        "donor",
        "donor123",
        "donorabc",
        "sample",
        "sample123",
        "sampleabc",
        "patient123",
        "patientabc",
        "run123",
        "runabc",
        "read123",
        "readabc",
        "sequence123",
        "sequenceabc",
        "local_path123",
        "local_pathabc",
    ],
)
def test_reserved_private_identifiers_are_rejected(reserved_term: str) -> None:
    record = _record("alpha")
    payload = record.model_dump(mode="json")
    payload["result_id"] = f"result_{reserved_term}_alpha"
    with pytest.raises(ValidationError, match="reserved privacy"):
        VerifiedMeasurementRecord.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "safe_lexeme",
    [
        "runtime",
        "runner",
        "readout",
        "readiness",
        "pathology",
        "sampled",
        "sequencer",
    ],
)
def test_safe_domain_lexemes_round_trip_canonical_json(
    safe_lexeme: str,
) -> None:
    record = _record(safe_lexeme)
    encoded = canonical_compatibility_bytes(record)

    assert compatibility_contract_from_canonical_bytes(
        VerifiedMeasurementRecord, encoded
    ) == record


def test_privacy_bounds_closed_models_and_canonical_input() -> None:
    record = _record("alpha")
    payload = record.model_dump(mode="json")
    payload["compatibility_key"]["normalization_semantics_id"] = (
        "sem_sequence_alpha"
    )
    with pytest.raises(ValidationError, match="reserved privacy"):
        VerifiedMeasurementRecord.model_validate_json(json.dumps(payload))

    payload = record.model_dump(mode="json")
    payload["extensions"] = {"local_path": "/private/data"}
    with pytest.raises(ValidationError, match="extra"):
        VerifiedMeasurementRecord.model_validate_json(json.dumps(payload))

    policy = _policy_for(record, _record("beta"))
    rule = policy.measurement_policies[0]
    oversized = rule.model_dump(mode="json")
    oversized["allowed_method_definitions"] = (
        oversized["allowed_method_definitions"] * 65
    )
    with pytest.raises(ValidationError, match="64 items"):
        MeasurementCompatibilityPolicy.model_validate_json(
            json.dumps(oversized)
        )

    canonical = canonical_compatibility_bytes(record)
    pretty = json.dumps(json.loads(canonical), indent=2, sort_keys=True).encode()
    with pytest.raises(CompatibilityContractError, match="not canonical"):
        compatibility_contract_from_canonical_bytes(
            VerifiedMeasurementRecord, pretty
        )


def test_same_human_label_cannot_replace_exact_identity() -> None:
    left = _record("alpha")
    alternate = _method(method_id="mth_fragment_same_label")
    right = _record("beta", method=alternate, key=_key(alternate))

    decision = decide_compatibility(_request(left, right))

    assert decision.outcome == CompatibilityOutcome.INCOMPATIBLE
    assert CompatibilityMismatchKey.METHOD_ID in decision.mismatch_keys
