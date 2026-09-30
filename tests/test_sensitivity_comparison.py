"""Synthetic adversarial tests for E11 sensitivity/downsampling contracts."""

from __future__ import annotations

import hashlib
import json
from typing import ClassVar

import pytest
from pydantic import ValidationError

import evidence_inspector.sensitivity_comparison as sensitivity
from evidence_inspector.cell_origin_explorer import (
    build_cell_origin_explorer_artifact,
    canonical_cell_origin_explorer_bytes,
)
from evidence_inspector.cell_origin_models import (
    BootstrapInformationStatus,
    NnlsRowScale,
)
from evidence_inspector.method_registry import MethodReference, method_definition_sha256
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.sensitivity_comparison import (
    MAX_MEMBERSHIP_DIGESTS,
    CellOriginRunParameters,
    EdgeInclusionPolicy,
    FailureCode,
    RegisteredParameterSet,
    RegisteredRunKey,
    ReplicateSeed,
    RunAttrition,
    RunStatus,
    SamplingEstimate,
    SensitivityAvailability,
    SensitivityComparisonArtifact,
    SensitivityContractError,
    SensitivityRunBinding,
    SensitivityRunOutcome,
    SensitivitySource,
    SensitivityStudyBundle,
    StudyAttrition,
    SubsetLevel,
    SubsetMembershipCommitment,
    WholeMoleculeSubsetFamily,
    build_sensitivity_comparison_artifact,
    build_sensitivity_comparison_view,
    canonical_sensitivity_comparison_bytes,
    register_sensitivity_study,
    sensitivity_bundle_sha256,
    sensitivity_comparison_from_canonical_bytes,
    sensitivity_result_sha256,
    whole_molecule_membership_sha256,
)
from tests.test_cell_origin_explorer import _request as cell_origin_request
from traceback_runner.serialization import canonical_json_bytes


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _source() -> SensitivitySource:
    explorer = build_cell_origin_explorer_artifact(cell_origin_request())
    record = explorer.request.result_view_request.sources[0].record
    capability = record.current_capability
    catalog_identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": record.bundle_sha256,
                "method_definition_sha256": record.method_definition_sha256,
            }
        )
    ).hexdigest()
    qualification = capability.qualification_state
    return SensitivitySource(
        catalog_ref=CatalogResultRef(
            result_id=f"result_{catalog_identity[:40]}",
            bundle_sha256=record.bundle_sha256,
            bundle_record_id="bundle-record.synthetic.v1",
            bundle_manifest_sha256="9" * 64,
            workflow_release_id="workflow.synthetic.v1",
            method_ref=record.method.method_ref,
            method_definition_sha256=record.method_definition_sha256,
            registry_sha256=capability.registry_sha256,
            registry_version=capability.registry_version,
            authority_head_sha256=capability.authority_head_sha256,
            authority_revision=capability.authority_revision,
            authority_scope=capability.authority_scope,
            capability_as_of=capability.as_of,
            qualification_state=(
                CatalogQualificationState(qualification.value)
                if qualification is not None
                else CatalogQualificationState.UNKNOWN
            ),
            display_role=capability.display_role,
            research_inspectable=capability.research_inspectable,
            current_provider_eligible=capability.current_provider_eligible,
        ),
        explorer_artifact=explorer,
        explorer_sha256=hashlib.sha256(
            canonical_cell_origin_explorer_bytes(explorer)
        ).hexdigest(),
    )


def _family() -> WholeMoleculeSubsetFamily:
    molecule_digests = tuple(
        hashlib.sha256(f"molecule:{index}".encode()).hexdigest() for index in range(8)
    )
    full_membership = whole_molecule_membership_sha256(tuple(sorted(molecule_digests)))
    return WholeMoleculeSubsetFamily(
        family_id="subset-family.synthetic",
        source_molecule_count=8,
        edge_inclusion_policy=EdgeInclusionPolicy.MOLECULE_MIDPOINT_HALF_OPEN,
        levels=(
            SubsetLevel(
                subset_id="subset.half",
                fraction_ppm=500_000,
                target_molecule_count=4,
            ),
            SubsetLevel(
                subset_id="subset.full",
                fraction_ppm=1_000_000,
                target_molecule_count=8,
            ),
        ),
        replicates=(
            ReplicateSeed(replicate_id="replicate.a", seed=7),
            ReplicateSeed(replicate_id="replicate.b", seed=11),
        ),
        membership_commitments=(
            SubsetMembershipCommitment(
                subset_id="subset.full",
                replicate_id="replicate.a",
                membership_count=8,
                membership_sha256=full_membership,
            ),
            SubsetMembershipCommitment(
                subset_id="subset.full",
                replicate_id="replicate.b",
                membership_count=8,
                membership_sha256=full_membership,
            ),
            SubsetMembershipCommitment(
                subset_id="subset.half",
                replicate_id="replicate.a",
                membership_count=4,
                membership_sha256=whole_molecule_membership_sha256(
                    tuple(sorted(molecule_digests[:4]))
                ),
            ),
            SubsetMembershipCommitment(
                subset_id="subset.half",
                replicate_id="replicate.b",
                membership_count=4,
                membership_sha256=whole_molecule_membership_sha256(
                    tuple(sorted(molecule_digests[index] for index in (0, 2, 4, 6)))
                ),
            ),
        ),
    )


def _parameter_sets(source: SensitivitySource) -> tuple[RegisteredParameterSet, ...]:
    record = source.record
    atlas = record.compatibility_key.atlas_asset
    assert atlas is not None
    baseline = CellOriginRunParameters(
        minimum_cpgs=4,
        unmethylated_max_exclusive=0.251,
        methylated_min_inclusive=0.75,
        nnls_row_scale=NnlsRowScale.REFERENCE_COUNT,
        solver_tolerance=1e-12,
        max_iterations=10_000,
    )
    conservative = baseline.model_copy(update={"minimum_cpgs": 5})
    return (
        RegisteredParameterSet(
            parameter_id="parameter.baseline",
            method=record.method,
            method_definition_sha256=record.method_definition_sha256,
            atlas_asset=atlas,
            parameters=baseline,
            parameters_sha256=_digest(baseline),
        ),
        RegisteredParameterSet(
            parameter_id="parameter.conservative",
            method=record.method,
            method_definition_sha256=record.method_definition_sha256,
            atlas_asset=atlas,
            parameters=conservative,
            parameters_sha256=_digest(conservative),
        ),
    )


def _complete_estimates(offset: float = 0.0) -> tuple[SamplingEstimate, ...]:
    immune = 1.0 / 3.0 + offset
    liver = 1.0 - immune
    return (
        SamplingEstimate(
            contributor_id="immune",
            estimate_fraction=immune,
            uncertainty_status=BootstrapInformationStatus.AVAILABLE,
            lower_fraction=max(0.0, immune - 0.05),
            upper_fraction=min(1.0, immune + 0.05),
        ),
        SamplingEstimate(
            contributor_id="liver",
            estimate_fraction=liver,
            uncertainty_status=BootstrapInformationStatus.INSUFFICIENT_INFORMATION,
        ),
    )


def _study_bundle(
    edge_policy: EdgeInclusionPolicy = EdgeInclusionPolicy.MOLECULE_MIDPOINT_HALF_OPEN,
) -> SensitivityStudyBundle:
    source = _source()
    family = _family().model_copy(update={"edge_inclusion_policy": edge_policy})
    parameters = _parameter_sets(source)
    registration = register_sensitivity_study(
        registration_id=f"registration.synthetic.{edge_policy.value}",
        source=source,
        subset_family=family,
        parameter_sets=parameters,
    )
    level_by_id = {item.subset_id: item for item in family.levels}
    replicate_by_id = {item.replicate_id: item for item in family.replicates}
    parameter_by_id = {item.parameter_id: item for item in parameters}
    receipt_by_key = {item.sort_key: item for item in registration.subset_receipts}
    outcomes = []
    for index, key in enumerate(registration.run_grid):
        level = level_by_id[key.subset_id]
        replicate = replicate_by_id[key.replicate_id]
        parameter = parameter_by_id[key.parameter_id]
        subset_digest = receipt_by_key[(key.subset_id, key.replicate_id)].subset_sha256
        attrition = RunAttrition(
            source_molecules=family.source_molecule_count,
            target_molecules=level.target_molecule_count,
            selected_molecules=level.target_molecule_count,
            accepted_molecules=level.target_molecule_count - 1,
            excluded_by_edge_policy=1,
            excluded_by_method=0,
        )
        if index == 2:
            outcomes.append(
                SensitivityRunOutcome(
                    key=key,
                    status=RunStatus.FAILED,
                    attrition=attrition,
                    subset_sha256=subset_digest,
                    failure_code=FailureCode.SOLVER_FAILED,
                )
            )
            continue
        if index == 5:
            outcomes.append(
                SensitivityRunOutcome(
                    key=key,
                    status=RunStatus.INSUFFICIENT_INFORMATION,
                    attrition=attrition,
                    subset_sha256=subset_digest,
                    failure_code=FailureCode.INSUFFICIENT_MOLECULES,
                )
            )
            continue
        estimates = _complete_estimates(offset=index * 0.001)
        atlas = parameter.atlas_asset
        result_id = f"result.sensitivity.{index}"
        bundle_id = f"bundle.sensitivity.{index}"
        result_digest = sensitivity_result_sha256(
            result_id=result_id,
            key=key,
            attrition=attrition,
            subset_sha256=subset_digest,
            parameters_sha256=parameter.parameters_sha256,
            estimates=estimates,
        )
        bundle_digest = sensitivity_bundle_sha256(
            bundle_id=bundle_id,
            result_sha256=result_digest,
            method_ref=parameter.method.method_ref,
            method_definition_sha256=parameter.method_definition_sha256,
            atlas_id=atlas.asset_id,
            atlas_sha256=atlas.content_sha256,
            filter_sha256=registration.source_filter_sha256,
            seed=replicate.seed,
            subset_sha256=subset_digest,
            parameters_sha256=parameter.parameters_sha256,
        )
        outcomes.append(
            SensitivityRunOutcome(
                key=key,
                status=RunStatus.COMPLETE,
                attrition=attrition,
                subset_sha256=subset_digest,
                binding=SensitivityRunBinding(
                    result_id=result_id,
                    result_sha256=result_digest,
                    bundle_id=bundle_id,
                    bundle_sha256=bundle_digest,
                    method_ref=parameter.method.method_ref,
                    method_definition_sha256=parameter.method_definition_sha256,
                    atlas_id=atlas.asset_id,
                    atlas_sha256=atlas.content_sha256,
                    filter_sha256=registration.source_filter_sha256,
                    seed=replicate.seed,
                    subset_sha256=subset_digest,
                    parameters_sha256=parameter.parameters_sha256,
                ),
                estimates=estimates,
            )
        )
    return SensitivityStudyBundle(
        source=source,
        registration=registration,
        outcomes=tuple(outcomes),
        attrition=StudyAttrition(
            expected_replicates=8,
            completed_replicates=6,
            insufficient_replicates=1,
            failed_replicates=1,
        ),
    )


def _resealed_complete_estimates_payload(
    estimates: tuple[SamplingEstimate, ...],
) -> dict[str, object]:
    bundle = _study_bundle()
    outcome = next(
        item for item in bundle.outcomes if item.status == RunStatus.COMPLETE
    )
    binding = outcome.binding
    assert binding is not None
    result_digest = sensitivity_result_sha256(
        result_id=binding.result_id,
        key=outcome.key,
        attrition=outcome.attrition,
        subset_sha256=outcome.subset_sha256,
        parameters_sha256=binding.parameters_sha256,
        estimates=estimates,
    )
    bundle_digest = sensitivity_bundle_sha256(
        bundle_id=binding.bundle_id,
        result_sha256=result_digest,
        method_ref=binding.method_ref,
        method_definition_sha256=binding.method_definition_sha256,
        atlas_id=binding.atlas_id,
        atlas_sha256=binding.atlas_sha256,
        filter_sha256=binding.filter_sha256,
        seed=binding.seed,
        subset_sha256=binding.subset_sha256,
        parameters_sha256=binding.parameters_sha256,
    )
    payload = bundle.model_dump(mode="json")
    changed = next(
        item
        for item in payload["outcomes"]
        if (
            item["key"]["subset_id"],
            item["key"]["replicate_id"],
            item["key"]["parameter_id"],
        )
        == outcome.key.sort_key
    )
    changed["estimates"] = [item.model_dump(mode="json") for item in estimates]
    changed["binding"]["result_sha256"] = result_digest
    changed["binding"]["bundle_sha256"] = bundle_digest
    return payload


def test_builds_full_registered_grid_without_cherry_picking() -> None:
    bundle = _study_bundle()
    artifact = build_sensitivity_comparison_artifact(bundle)

    assert len(bundle.registration.run_grid) == 8
    assert len(artifact.view.run_rows) == 8
    assert [row.key for row in artifact.view.run_rows] == list(
        bundle.registration.run_grid
    )
    assert artifact.view.attrition.failed_replicates == 1
    assert artifact.view.attrition.insufficient_replicates == 1
    assert artifact.view.edge_inclusion_policy == (
        EdgeInclusionPolicy.MOLECULE_MIDPOINT_HALF_OPEN
    )
    assert artifact.view.compatibility.outcome == ("comparable_within_registered_grid")
    assert not artifact.view.compatibility.longitudinal_compatibility_inferred


def test_sampling_uncertainty_and_method_sensitivity_are_distinct() -> None:
    view = build_sensitivity_comparison_artifact(_study_bundle()).view

    complete = next(row for row in view.run_rows if row.status == RunStatus.COMPLETE)
    assert complete.estimates[0].uncertainty_kind == "sampling_bootstrap"
    assert complete.estimates[1].lower_fraction is None
    assert all(
        row.interpretation == "registered_parameter_range_not_sampling_interval"
        for row in view.method_sensitivity_rows
    )
    assert any(
        row.availability == SensitivityAvailability.PARTIAL
        for row in view.method_sensitivity_rows
    )


def test_numeric_zero_is_not_missing_and_unavailable_has_no_bounds() -> None:
    zero = SamplingEstimate(
        contributor_id="immune",
        estimate_fraction=0.0,
        uncertainty_status=BootstrapInformationStatus.INSUFFICIENT_INFORMATION,
    )
    assert zero.estimate_fraction == 0.0
    assert zero.lower_fraction is None
    with pytest.raises(ValidationError, match="only available"):
        SamplingEstimate(
            contributor_id="immune",
            estimate_fraction=0.0,
            uncertainty_status=BootstrapInformationStatus.INSUFFICIENT_INFORMATION,
            lower_fraction=0.0,
            upper_fraction=0.1,
        )


def _normalized_estimates(offset: float) -> tuple[SamplingEstimate, ...]:
    return (
        SamplingEstimate(
            contributor_id="immune",
            estimate_fraction=0.5,
            uncertainty_status="not_run",
        ),
        SamplingEstimate(
            contributor_id="liver",
            estimate_fraction=0.5 + offset,
            uncertainty_status="not_run",
        ),
    )


@pytest.mark.parametrize("offset", [0.9e-9, -0.9e-9])
def test_complete_fraction_sum_accepts_upstream_tolerance_boundary(
    offset: float,
) -> None:
    payload = _resealed_complete_estimates_payload(_normalized_estimates(offset))
    SensitivityStudyBundle.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "estimates",
    [
        _normalized_estimates(1.1e-9),
        _normalized_estimates(-1.1e-9),
        (
            SamplingEstimate(
                contributor_id="immune",
                estimate_fraction=0.9,
                uncertainty_status="not_run",
            ),
            SamplingEstimate(
                contributor_id="liver",
                estimate_fraction=0.9,
                uncertainty_status="not_run",
            ),
        ),
        (
            SamplingEstimate(
                contributor_id="immune",
                estimate_fraction=0.2,
                uncertainty_status="not_run",
            ),
            SamplingEstimate(
                contributor_id="liver",
                estimate_fraction=0.2,
                uncertainty_status="not_run",
            ),
        ),
    ],
)
def test_complete_fraction_sum_rejects_resealed_nonnormalized_evidence(
    estimates: tuple[SamplingEstimate, ...],
) -> None:
    payload = _resealed_complete_estimates_payload(estimates)
    with pytest.raises(ValidationError, match="canonical cell fractions must sum"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_registration_rejects_caller_selected_grid_omission() -> None:
    bundle = _study_bundle()
    payload = bundle.registration.model_dump(mode="json")
    payload["run_grid"].pop()
    with pytest.raises(ValidationError, match="full preregistered Cartesian grid"):
        type(bundle.registration).model_validate_json(json.dumps(payload))


def test_registration_grid_is_exact_cartesian_without_duplicate_keys() -> None:
    registration = _study_bundle().registration
    keys = tuple(item.sort_key for item in registration.run_grid)
    expected_count = (
        len(registration.subset_family.levels)
        * len(registration.subset_family.replicates)
        * len(registration.parameter_sets)
    )
    assert len(keys) == expected_count
    assert len(keys) == len(set(keys))


def test_bundle_rejects_silent_failed_or_omitted_replicate() -> None:
    payload = _study_bundle().model_dump(mode="json")
    payload["outcomes"].pop()
    with pytest.raises(ValidationError, match="every registered grid cell"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_failed_run_cannot_carry_best_looking_numeric_result() -> None:
    payload = _study_bundle().model_dump(mode="json")
    failed = next(item for item in payload["outcomes"] if item["status"] == "failed")
    failed["estimates"] = [
        SamplingEstimate(
            contributor_id="immune",
            estimate_fraction=0.9,
            uncertainty_status="not_run",
        ).model_dump(mode="json")
    ]
    with pytest.raises(ValidationError, match="failed run"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_insufficient_run_cannot_carry_numeric_result() -> None:
    payload = _study_bundle().model_dump(mode="json")
    insufficient = next(
        item
        for item in payload["outcomes"]
        if item["status"] == "insufficient_information"
    )
    insufficient["estimates"] = [
        SamplingEstimate(
            contributor_id="immune",
            estimate_fraction=1.0,
            uncertainty_status="not_run",
        ).model_dump(mode="json")
    ]
    with pytest.raises(ValidationError, match="insufficient run"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("seed", 99, "bind exact result evidence"),
        ("subset_sha256", "f" * 64, "subset receipt"),
        ("filter_sha256", "e" * 64, "bind exact result evidence"),
        ("atlas_sha256", "d" * 64, "bind exact result evidence"),
        ("parameters_sha256", "c" * 64, "bind exact numeric evidence"),
    ],
)
def test_run_bindings_fail_closed_on_identity_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _study_bundle().model_dump(mode="json")
    complete = next(
        item for item in payload["outcomes"] if item["status"] == "complete"
    )
    complete["binding"][field] = value
    with pytest.raises(ValidationError, match=message):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_parameter_runs_share_exact_seeded_whole_molecule_subset() -> None:
    payload = _study_bundle().model_dump(mode="json")
    first = payload["outcomes"][0]
    peer = next(
        item
        for item in payload["outcomes"][1:]
        if item["key"]["subset_id"] == first["key"]["subset_id"]
        and item["key"]["replicate_id"] == first["key"]["replicate_id"]
    )
    peer["subset_sha256"] = "f" * 64
    if peer["binding"] is not None:
        peer["binding"]["subset_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="bind exact numeric evidence"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_all_peer_subset_receipts_cannot_drift_from_registration() -> None:
    payload = _study_bundle().model_dump(mode="json")
    for outcome in payload["outcomes"]:
        outcome["subset_sha256"] = "f" * 64
        if outcome["binding"] is not None:
            outcome["binding"]["subset_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="bind exact|preregistered subset"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_complete_estimates_are_bound_to_fixed_result_and_bundle_identities() -> None:
    payload = _study_bundle().model_dump(mode="json")
    complete = [item for item in payload["outcomes"] if item["status"] == "complete"]
    complete[0]["estimates"], complete[1]["estimates"] = (
        complete[1]["estimates"],
        complete[0]["estimates"],
    )
    with pytest.raises(ValidationError, match="result digest.*numeric evidence"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_complete_runs_require_unique_result_and_bundle_identities() -> None:
    bundle = _study_bundle()
    complete = [item for item in bundle.outcomes if item.status == RunStatus.COMPLETE]
    first, second = complete[:2]
    assert first.binding is not None and second.binding is not None
    result_digest = sensitivity_result_sha256(
        result_id=first.binding.result_id,
        key=second.key,
        attrition=second.attrition,
        subset_sha256=second.subset_sha256,
        parameters_sha256=second.binding.parameters_sha256,
        estimates=second.estimates,
    )
    bundle_digest = sensitivity_bundle_sha256(
        bundle_id=second.binding.bundle_id,
        result_sha256=result_digest,
        method_ref=second.binding.method_ref,
        method_definition_sha256=second.binding.method_definition_sha256,
        atlas_id=second.binding.atlas_id,
        atlas_sha256=second.binding.atlas_sha256,
        filter_sha256=second.binding.filter_sha256,
        seed=second.binding.seed,
        subset_sha256=second.binding.subset_sha256,
        parameters_sha256=second.binding.parameters_sha256,
    )
    payload = bundle.model_dump(mode="json")
    changed = next(
        item
        for item in payload["outcomes"]
        if (
            item["key"]["subset_id"],
            item["key"]["replicate_id"],
            item["key"]["parameter_id"],
        )
        == second.key.sort_key
    )
    changed["binding"]["result_id"] = first.binding.result_id
    changed["binding"]["result_sha256"] = result_digest
    changed["binding"]["bundle_sha256"] = bundle_digest
    with pytest.raises(ValidationError, match="result_id identities must be unique"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_attrition_and_failed_replicate_counts_reconcile() -> None:
    payload = _study_bundle().model_dump(mode="json")
    payload["outcomes"][0]["attrition"]["accepted_molecules"] -= 1
    with pytest.raises(ValidationError, match="attrition"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))

    payload = _study_bundle().model_dump(mode="json")
    payload["attrition"]["failed_replicates"] = 0
    payload["attrition"]["completed_replicates"] = 7
    with pytest.raises(ValidationError, match="exact run outcomes"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_subset_registration_requires_full_level_and_exact_rounding() -> None:
    payload = _family().model_dump(mode="json")
    payload["levels"].pop()
    with pytest.raises(ValidationError, match="full-molecule"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))

    payload = _family().model_dump(mode="json")
    payload["levels"][0]["target_molecule_count"] = 3
    with pytest.raises(ValidationError, match="registered fraction"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))


def test_subset_levels_reject_duplicate_ids_and_duplicate_fractions() -> None:
    payload = _family().model_dump(mode="json")
    payload["levels"][1]["subset_id"] = payload["levels"][0]["subset_id"]
    with pytest.raises(ValidationError, match="subset IDs must be unique"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))

    payload = _family().model_dump(mode="json")
    payload["levels"][0]["subset_id"] = "subset.a"
    payload["levels"][1]["subset_id"] = "subset.b"
    payload["levels"][1]["fraction_ppm"] = 500_000
    for commitment in payload["membership_commitments"]:
        commitment["subset_id"] = (
            "subset.a" if commitment["subset_id"] == "subset.half" else "subset.b"
        )
    with pytest.raises(ValidationError, match="subset fractions must be unique"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))


def test_subset_levels_reject_colliding_effective_counts_after_rounding() -> None:
    payload = _family().model_dump(mode="json")
    near_half = dict(payload["levels"][0])
    near_half["subset_id"] = "subset.nearhalf"
    near_half["fraction_ppm"] = 500_001
    payload["levels"].insert(1, near_half)
    with pytest.raises(ValidationError, match="unique effective target counts"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))


def test_membership_commitments_are_complete_canonical_and_preregistered() -> None:
    payload = _family().model_dump(mode="json")
    payload["membership_commitments"].pop()
    with pytest.raises(ValidationError, match="full subset-replicate grid"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))

    study_payload = _study_bundle().model_dump(mode="json")
    commitments = study_payload["registration"]["subset_family"][
        "membership_commitments"
    ]
    half_commitment = next(
        item for item in commitments if item["subset_id"] == "subset.half"
    )
    half_commitment["membership_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="preregistered context"):
        SensitivityStudyBundle.model_validate_json(json.dumps(study_payload))


def test_full_subset_membership_is_seed_invariant() -> None:
    payload = _family().model_dump(mode="json")
    full = [
        item
        for item in payload["membership_commitments"]
        if item["subset_id"] == "subset.full"
    ]
    full[1]["membership_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="identical across replicate seeds"):
        WholeMoleculeSubsetFamily.model_validate_json(json.dumps(payload))


def test_membership_digest_requires_unique_canonical_order() -> None:
    first = "0" * 64
    second = "f" * 64
    with pytest.raises(ValueError, match="uniquely sorted"):
        whole_molecule_membership_sha256((second, first))
    with pytest.raises(ValueError, match="uniquely sorted"):
        whole_molecule_membership_sha256((first, first))
    with pytest.raises(ValueError, match="SHA-256 digests only"):
        whole_molecule_membership_sha256(("raw-molecule-id",))


def test_parameter_payloads_must_be_unique_despite_distinct_ids() -> None:
    source = _source()
    parameters = _parameter_sets(source)
    duplicate = parameters[1].model_copy(update={"parameter_id": "parameter.zzz"})
    with pytest.raises(ValidationError, match="unique parameter payloads"):
        register_sensitivity_study(
            registration_id="registration.synthetic",
            source=source,
            subset_family=_family(),
            parameter_sets=(*parameters, duplicate),
        )


def test_subset_family_denominator_must_match_exact_source() -> None:
    bundle = _study_bundle()
    family_payload = bundle.registration.subset_family.model_dump(mode="json")
    family_payload["source_molecule_count"] = 10
    family_payload["levels"][0]["target_molecule_count"] = 5
    family_payload["levels"][1]["target_molecule_count"] = 10
    for commitment in family_payload["membership_commitments"]:
        commitment["membership_count"] = (
            5 if commitment["subset_id"] == "subset.half" else 10
        )
    family = WholeMoleculeSubsetFamily.model_validate_json(json.dumps(family_payload))
    registration = register_sensitivity_study(
        registration_id=bundle.registration.registration_id,
        source=bundle.source,
        subset_family=family,
        parameter_sets=bundle.registration.parameter_sets,
    )
    with pytest.raises(ValidationError, match="molecule denominator"):
        SensitivityStudyBundle(
            source=bundle.source,
            registration=registration,
            outcomes=bundle.outcomes,
            attrition=bundle.attrition,
        )


def test_source_rejects_e04_identity_drift() -> None:
    payload = _source().model_dump(mode="json")
    payload["catalog_ref"]["bundle_sha256"] = "f" * 64
    method_digest = payload["catalog_ref"]["method_definition_sha256"]
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_sha256": "f" * 64,
                "method_definition_sha256": method_digest,
            }
        )
    ).hexdigest()
    payload["catalog_ref"]["result_id"] = f"result_{identity[:40]}"
    with pytest.raises(ValidationError, match="catalog identity"):
        SensitivitySource.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_as_of", "2099-01-01T00:00:00Z"),
        ("qualification_state", "qualified"),
        ("display_role", "provider_primary"),
        ("current_provider_eligible", True),
    ],
)
def test_source_rejects_e04_authority_state_overstatement(
    field: str,
    value: object,
) -> None:
    payload = _source().model_dump(mode="json")
    payload["catalog_ref"][field] = value
    with pytest.raises(ValidationError, match="catalog identity"):
        SensitivitySource.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "workflow_release_id",
    ["Alice Example", "source/private.json", "read123"],
)
def test_source_rejects_unsafe_e04_presentation_identity(
    workflow_release_id: str,
) -> None:
    payload = _source().model_dump(mode="json")
    payload["catalog_ref"]["workflow_release_id"] = workflow_release_id
    with pytest.raises(ValidationError, match="workflow release ID"):
        SensitivitySource.model_validate_json(json.dumps(payload))


def test_study_rejects_parameter_method_outside_source_compatibility() -> None:
    bundle = _study_bundle()
    parameter_sets = list(bundle.registration.parameter_sets)
    original = parameter_sets[0]
    changed_method = original.method.model_copy(
        update={"method_id": "mth_cell_origin_beta"}
    )
    parameter_sets[0] = RegisteredParameterSet(
        parameter_id=original.parameter_id,
        method=changed_method,
        method_definition_sha256=method_definition_sha256(changed_method),
        atlas_asset=original.atlas_asset,
        parameters=original.parameters,
        parameters_sha256=original.parameters_sha256,
    )
    changed_registration = register_sensitivity_study(
        registration_id=bundle.registration.registration_id,
        source=bundle.source,
        subset_family=bundle.registration.subset_family,
        parameter_sets=tuple(parameter_sets),
    )

    with pytest.raises(ValidationError, match="incompatible.*source method or atlas"):
        SensitivityStudyBundle(
            source=bundle.source,
            registration=changed_registration,
            outcomes=bundle.outcomes,
            attrition=bundle.attrition,
        )


def test_canonical_artifact_replays_and_rejects_view_tampering() -> None:
    artifact = build_sensitivity_comparison_artifact(_study_bundle())
    encoded = canonical_sensitivity_comparison_bytes(artifact)
    assert sensitivity_comparison_from_canonical_bytes(encoded) == artifact

    payload = json.loads(encoded)
    payload["view"]["run_rows"].pop()
    tampered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(SensitivityContractError, match="invalid"):
        sensitivity_comparison_from_canonical_bytes(tampered)


def test_canonical_bytes_are_private_and_bounded() -> None:
    encoded = canonical_sensitivity_comparison_bytes(
        build_sensitivity_comparison_artifact(_study_bundle())
    )
    lowered = encoded.lower()
    assert len(encoded) < 8 * 1024 * 1024
    assert not any(
        token in lowered
        for token in (
            b"donor_id",
            b"patient_id",
            b"read_id",
            b"sample_id",
            b"local_path",
            b"/users/",
            b"sequence",
        )
    )


def test_all_controlled_ids_reject_concatenated_private_stems() -> None:
    with pytest.raises(ValidationError, match="reserved privacy term"):
        RegisteredRunKey(
            subset_id="read123",
            replicate_id="replicate.a",
            parameter_id="parameter.baseline",
        )


def test_contracts_are_closed_and_canonical_order_is_enforced() -> None:
    payload = _study_bundle().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))

    payload = _study_bundle().model_dump(mode="json")
    payload["registration"]["parameter_sets"].reverse()
    with pytest.raises(ValidationError, match="canonical order"):
        SensitivityStudyBundle.model_validate_json(json.dumps(payload))


def test_artifact_rejects_oversize_before_json_parse() -> None:
    from evidence_inspector.sensitivity_comparison import MAX_CANONICAL_BYTES

    oversized = b"{" + b" " * MAX_CANONICAL_BYTES
    with pytest.raises(SensitivityContractError, match="byte bound"):
        sensitivity_comparison_from_canonical_bytes(oversized)


def test_public_object_boundaries_reject_top_level_and_nested_hooks() -> None:
    bundle = _study_bundle()

    class HookedBundle(SensitivityStudyBundle):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    hostile_bundle = HookedBundle.model_construct(**bundle.__dict__)
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(hostile_bundle)
    assert HookedBundle.calls == 0

    class HookedEstimate(SamplingEstimate):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    outcome = next(item for item in bundle.outcomes if item.estimates)
    hostile_estimate = HookedEstimate.model_construct(**outcome.estimates[0].__dict__)
    hostile_outcome = outcome.model_copy(
        update={"estimates": (hostile_estimate, *outcome.estimates[1:])}
    )
    hostile_nested = bundle.model_copy(
        update={
            "outcomes": tuple(
                hostile_outcome if item is outcome else item for item in bundle.outcomes
            )
        }
    )
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(hostile_nested)
    assert HookedEstimate.calls == 0


def test_every_e11_public_model_boundary_rejects_virtual_dispatch() -> None:
    bundle = _study_bundle()

    class HookedSource(SensitivitySource):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    source = HookedSource.model_construct(**bundle.source.__dict__)
    with pytest.raises(SensitivityContractError):
        register_sensitivity_study(
            registration_id="registration.hostile",
            source=source,
            subset_family=bundle.registration.subset_family,
            parameter_sets=bundle.registration.parameter_sets,
        )
    assert HookedSource.calls == 0

    complete = next(item for item in bundle.outcomes if item.binding is not None)
    assert complete.binding is not None

    class HookedKey(RegisteredRunKey):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    key = HookedKey.model_construct(**complete.key.__dict__)
    with pytest.raises(SensitivityContractError):
        sensitivity_result_sha256(
            result_id=complete.binding.result_id,
            key=key,
            attrition=complete.attrition,
            subset_sha256=complete.subset_sha256,
            parameters_sha256=complete.binding.parameters_sha256,
            estimates=complete.estimates,
        )
    assert HookedKey.calls == 0

    class HookedMethodReference(MethodReference):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    method_ref = HookedMethodReference.model_construct(
        **complete.binding.method_ref.__dict__
    )
    with pytest.raises(SensitivityContractError):
        sensitivity_bundle_sha256(
            bundle_id=complete.binding.bundle_id,
            result_sha256=complete.binding.result_sha256,
            method_ref=method_ref,
            method_definition_sha256=complete.binding.method_definition_sha256,
            atlas_id=complete.binding.atlas_id,
            atlas_sha256=complete.binding.atlas_sha256,
            filter_sha256=complete.binding.filter_sha256,
            seed=complete.binding.seed,
            subset_sha256=complete.binding.subset_sha256,
            parameters_sha256=complete.binding.parameters_sha256,
        )
    assert HookedMethodReference.calls == 0

    class HookedBundle(SensitivityStudyBundle):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    hooked_bundle = HookedBundle.model_construct(**bundle.__dict__)
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_view(hooked_bundle)
    assert HookedBundle.calls == 0

    artifact = build_sensitivity_comparison_artifact(bundle)

    class HookedArtifact(SensitivityComparisonArtifact):
        calls: ClassVar[int] = 0

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            type(self).calls += 1
            return super().model_dump(*args, **kwargs)

    hooked_artifact = HookedArtifact.model_construct(**artifact.__dict__)
    with pytest.raises(SensitivityContractError):
        canonical_sensitivity_comparison_bytes(hooked_artifact)
    assert HookedArtifact.calls == 0

    class HookedBytes(bytes):
        pass

    with pytest.raises(SensitivityContractError):
        sensitivity_comparison_from_canonical_bytes(
            HookedBytes(canonical_sensitivity_comparison_bytes(artifact))
        )


def test_e11_rejects_top_level_nested_extra_and_private_state_without_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _study_bundle()
    serializer_calls = 0

    class SerializerGuard:
        def to_python(self, *args: object, **kwargs: object) -> object:
            nonlocal serializer_calls
            serializer_calls += 1
            raise AssertionError("forged state reached pinned serializer")

    class EvilProxy:
        calls = 0

        def __str__(self) -> str:
            type(self).calls += 1
            return "/Users/alice/private.bam"

        def __iter__(self):
            type(self).calls += 1
            raise AssertionError("forged proxy was traversed")

    monkeypatch.setattr(
        SensitivityStudyBundle,
        "__pydantic_serializer__",
        SerializerGuard(),
    )
    proxy = EvilProxy()
    forged_top = bundle.model_copy(update={"hidden_private_path": proxy})
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(forged_top)

    forged_outcome = bundle.outcomes[0].model_copy(
        update={"hidden_private_path": "/Users/alice/private.bam"}
    )
    forged_nested = bundle.model_copy(
        update={"outcomes": (forged_outcome, *bundle.outcomes[1:])}
    )
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(forged_nested)

    cycle: list[object] = []
    cycle.append(cycle)
    forged_private = bundle.model_copy()
    object.__setattr__(forged_private, "__pydantic_private__", {"cycle": cycle})
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(forged_private)

    forged_extra = bundle.model_copy()
    object.__setattr__(forged_extra, "__pydantic_extra__", {"proxy": proxy})
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(forged_extra)
    assert EvilProxy.calls == 0
    assert serializer_calls == 0


def test_public_object_boundaries_preflight_scalars_and_collections() -> None:
    bundle = _study_bundle()
    giant_registration = bundle.registration.model_copy(
        update={"registration_id": "x" * 10_000_000}
    )
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(
            bundle.model_copy(update={"registration": giant_registration})
        )

    outcome = bundle.outcomes[0]
    huge_attrition = outcome.attrition.model_copy(
        update={"selected_molecules": 1 << 100_000}
    )
    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(
            bundle.model_copy(
                update={
                    "outcomes": (
                        outcome.model_copy(update={"attrition": huge_attrition}),
                        *bundle.outcomes[1:],
                    )
                }
            )
        )

    with pytest.raises(SensitivityContractError):
        build_sensitivity_comparison_artifact(
            bundle.model_copy(update={"outcomes": (bundle.outcomes[0],) * 4_097})
        )

    digest = "a" * 64
    with pytest.raises(ValueError, match="digest-count bound"):
        whole_molecule_membership_sha256((digest,) * (MAX_MEMBERSHIP_DIGESTS + 1))


def test_registration_id_is_bounded_before_model_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_calls = 0

    def unexpected_digest(*args: object, **kwargs: object) -> str:
        nonlocal digest_calls
        digest_calls += 1
        raise AssertionError("registration digest must not run")

    monkeypatch.setattr(sensitivity, "_model_digest", unexpected_digest)
    source = _source()
    with pytest.raises(SensitivityContractError, match="registration ID"):
        register_sensitivity_study(
            registration_id="x" * 10_000_000,
            source=source,
            subset_family=_family(),
            parameter_sets=_parameter_sets(source),
        )
    assert digest_calls == 0


@pytest.mark.parametrize(
    "private_id",
    ("patient123", "/Users/alice/private.bam", "Alice Example"),
)
def test_digest_ids_are_privacy_safe_before_digest_entry(
    private_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _study_bundle()
    complete = next(item for item in bundle.outcomes if item.binding is not None)
    binding = complete.binding
    assert binding is not None
    digest_calls = 0

    def unexpected_digest(*args: object, **kwargs: object) -> str:
        nonlocal digest_calls
        digest_calls += 1
        raise AssertionError("invalid identifier reached digest helper")

    monkeypatch.setattr(sensitivity, "_digest", unexpected_digest)
    with pytest.raises(SensitivityContractError):
        sensitivity_result_sha256(
            result_id=private_id,
            key=complete.key,
            attrition=complete.attrition,
            subset_sha256=complete.subset_sha256,
            parameters_sha256=binding.parameters_sha256,
            estimates=complete.estimates,
        )

    bundle_arguments = {
        "bundle_id": binding.bundle_id,
        "result_sha256": binding.result_sha256,
        "method_ref": binding.method_ref,
        "method_definition_sha256": binding.method_definition_sha256,
        "atlas_id": binding.atlas_id,
        "atlas_sha256": binding.atlas_sha256,
        "filter_sha256": binding.filter_sha256,
        "seed": binding.seed,
        "subset_sha256": binding.subset_sha256,
        "parameters_sha256": binding.parameters_sha256,
    }
    with pytest.raises(SensitivityContractError):
        sensitivity_bundle_sha256(**{**bundle_arguments, "bundle_id": private_id})
    with pytest.raises(SensitivityContractError):
        sensitivity_bundle_sha256(**{**bundle_arguments, "atlas_id": private_id})
    assert digest_calls == 0


@pytest.mark.parametrize("policy", tuple(EdgeInclusionPolicy))
def test_every_edge_policy_is_bound_and_replays(policy: EdgeInclusionPolicy) -> None:
    artifact = build_sensitivity_comparison_artifact(_study_bundle(policy))
    canonical = canonical_sensitivity_comparison_bytes(artifact)
    replayed = sensitivity_comparison_from_canonical_bytes(canonical)
    assert replayed == artifact
    assert replayed.bundle.registration.subset_family.edge_inclusion_policy == policy
    assert replayed.view.edge_inclusion_policy == policy
    assert all(
        receipt.subset_sha256
        for receipt in replayed.bundle.registration.subset_receipts
    )


def test_cross_policy_receipt_and_view_tamper_fail_replay() -> None:
    artifact = build_sensitivity_comparison_artifact(_study_bundle())
    alternate = EdgeInclusionPolicy.ANY_ALIGNED_BASE_HALF_OPEN
    with pytest.raises(ValidationError, match="replay exactly"):
        type(artifact)(
            bundle=artifact.bundle,
            view=artifact.view.model_copy(update={"edge_inclusion_policy": alternate}),
        )

    original = artifact.bundle.registration
    changed_family = original.subset_family.model_copy(
        update={"edge_inclusion_policy": alternate}
    )
    with pytest.raises(ValidationError, match="subset receipts"):
        type(original)(
            registration_id=original.registration_id,
            source_explorer_sha256=original.source_explorer_sha256,
            source_result_sha256=original.source_result_sha256,
            source_bundle_sha256=original.source_bundle_sha256,
            source_method_definition_sha256=original.source_method_definition_sha256,
            source_atlas_sha256=original.source_atlas_sha256,
            source_filter_sha256=original.source_filter_sha256,
            subset_family=changed_family,
            parameter_sets=original.parameter_sets,
            run_grid=original.run_grid,
            subset_receipts=original.subset_receipts,
            registration_sha256=original.registration_sha256,
        )
