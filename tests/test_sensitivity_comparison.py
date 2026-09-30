"""Synthetic adversarial tests for E11 sensitivity/downsampling contracts."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from traceback_runner.serialization import canonical_json_bytes

from evidence_inspector.cell_origin_explorer import (
    build_cell_origin_explorer_artifact,
    canonical_cell_origin_explorer_bytes,
)
from evidence_inspector.cell_origin_models import (
    BootstrapInformationStatus,
    NnlsRowScale,
)
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.method_registry import method_definition_sha256
from evidence_inspector.sensitivity_comparison import (
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
    WholeMoleculeSubsetFamily,
    build_sensitivity_comparison_artifact,
    canonical_sensitivity_comparison_bytes,
    register_sensitivity_study,
    sensitivity_comparison_from_canonical_bytes,
)
from tests.test_cell_origin_explorer import _request as cell_origin_request


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


def _study_bundle() -> SensitivityStudyBundle:
    source = _source()
    family = _family()
    parameters = _parameter_sets(source)
    registration = register_sensitivity_study(
        registration_id="registration.synthetic",
        source=source,
        subset_family=family,
        parameter_sets=parameters,
    )
    level_by_id = {item.subset_id: item for item in family.levels}
    replicate_by_id = {item.replicate_id: item for item in family.replicates}
    parameter_by_id = {item.parameter_id: item for item in parameters}
    outcomes = []
    for index, key in enumerate(registration.run_grid):
        level = level_by_id[key.subset_id]
        replicate = replicate_by_id[key.replicate_id]
        parameter = parameter_by_id[key.parameter_id]
        subset_digest = hashlib.sha256(
            f"{key.subset_id}:{key.replicate_id}".encode()
        ).hexdigest()
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
        result_digest = hashlib.sha256(f"result:{key.sort_key}".encode()).hexdigest()
        bundle_digest = hashlib.sha256(f"bundle:{key.sort_key}".encode()).hexdigest()
        atlas = parameter.atlas_asset
        outcomes.append(
            SensitivityRunOutcome(
                key=key,
                status=RunStatus.COMPLETE,
                attrition=attrition,
                subset_sha256=subset_digest,
                binding=SensitivityRunBinding(
                    result_id=f"result.sensitivity.{index}",
                    result_sha256=result_digest,
                    bundle_id=f"bundle.sensitivity.{index}",
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
    assert artifact.view.compatibility.outcome == (
        "comparable_within_registered_grid"
    )
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


def test_registration_rejects_caller_selected_grid_omission() -> None:
    bundle = _study_bundle()
    payload = bundle.registration.model_dump(mode="json")
    payload["run_grid"].pop()
    with pytest.raises(ValidationError, match="full preregistered Cartesian grid"):
        type(bundle.registration).model_validate_json(json.dumps(payload))


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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("seed", 99, "run binding"),
        ("subset_sha256", "f" * 64, "subset receipt"),
        ("filter_sha256", "e" * 64, "run binding"),
        ("atlas_sha256", "d" * 64, "run binding"),
        ("parameters_sha256", "c" * 64, "run binding"),
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
    with pytest.raises(ValidationError, match="exact seeded subset"):
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


def test_subset_family_denominator_must_match_exact_source() -> None:
    bundle = _study_bundle()
    family_payload = bundle.registration.subset_family.model_dump(mode="json")
    family_payload["source_molecule_count"] = 10
    family_payload["levels"][0]["target_molecule_count"] = 5
    family_payload["levels"][1]["target_molecule_count"] = 10
    family = WholeMoleculeSubsetFamily.model_validate_json(
        json.dumps(family_payload)
    )
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
