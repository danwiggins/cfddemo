"""Preregistered, deterministic sensitivity and downsampling contracts.

The module consumes verified aggregate identities only. It never selects raw
molecules, executes a cell-origin method, chooses a preferred parameter set, or
permits callers to omit registered grid cells from the comparison view.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from itertools import product
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from traceback_runner.serialization import canonical_json_bytes

from .cell_origin_explorer import (
    CellOriginExplorerArtifact,
    ExplorerStatus,
    canonical_cell_origin_explorer_bytes,
)
from .cell_origin_models import (
    BootstrapInformationStatus,
    CellOriginResult,
    NnlsRowScale,
)
from .compatibility import VerifiedMeasurementRecord
from .method_registry import (
    AssetReference,
    MethodDefinition,
    MethodFamily,
    MethodReference,
    method_definition_sha256,
)
from .result_catalog import CatalogResultRef
from .result_view import CompatibilityContract, result_filters_sha256

MAX_SUBSET_LEVELS = 16
MAX_REPLICATES = 32
MAX_PARAMETER_SETS = 32
MAX_RUNS = 4_096
MAX_CONTRIBUTORS = 512
MAX_CANONICAL_BYTES = 8 * 1024 * 1024

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

_PRIVATE_PREFIX = re.compile(
    r"^(?:donor|patient|read|sample|sequence|path)(?:[_:.-]?[a-z0-9].*)?$",
    re.IGNORECASE,
)
_SAFE_PRIVATE_LEXEMES = {
    "pathology",
    "readiness",
    "readout",
    "sampled",
    "sequencer",
}


def _safe_id(value: str, *, field: str = "controlled ID") -> str:
    for segment in re.split(r"[^a-z0-9]+", value.lower()):
        if not segment or segment in _SAFE_PRIVATE_LEXEMES:
            continue
        if _PRIVATE_PREFIX.fullmatch(segment):
            raise ValueError(f"{field} contains a reserved privacy term")
    return value


def _require_safe_token(value: str, *, field: str) -> None:
    if re.fullmatch(r"^[a-z][a-z0-9]*(?:[_.:-][a-z0-9]+)*$", value) is None:
        raise ValueError(f"{field} is not a controlled identifier")
    _safe_id(value, field=field)


SafeId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[_.:-][a-z0-9]+)*$",
    ),
    AfterValidator(_safe_id),
]


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def whole_molecule_membership_sha256(
    molecule_sha256s: tuple[Sha256, ...],
) -> str:
    """Commit to canonical private membership without retaining molecule IDs."""
    if not molecule_sha256s:
        raise ValueError("whole-molecule membership cannot be empty")
    if any(
        re.fullmatch(r"[0-9a-f]{64}", item) is None
        for item in molecule_sha256s
    ):
        raise ValueError("whole-molecule membership accepts SHA-256 digests only")
    if molecule_sha256s != tuple(sorted(set(molecule_sha256s))):
        raise ValueError("whole-molecule digests must be uniquely sorted")
    return _digest(
        {
            "schema_version": "traceback.whole-molecule-membership.v1",
            "molecule_sha256s": molecule_sha256s,
        }
    )


class SensitivityContractError(ValueError):
    """A sensitivity contract or canonical replay failed closed."""


class EdgeInclusionPolicy(StrEnum):
    MOLECULE_MIDPOINT_HALF_OPEN = "molecule_midpoint_half_open"
    ANY_ALIGNED_BASE_HALF_OPEN = "any_aligned_base_half_open"
    FULLY_CONTAINED_HALF_OPEN = "fully_contained_half_open"


class RunStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class FailureCode(StrEnum):
    EXTRACTION_FAILED = "extraction_failed"
    SOLVER_FAILED = "solver_failed"
    BOOTSTRAP_FAILED = "bootstrap_failed"
    INSUFFICIENT_MOLECULES = "insufficient_molecules"


class SensitivityAvailability(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class SensitivityCompatibility(CompatibilityContract):
    comparison_scope: Literal["registered_sensitivity_grid"] = (
        "registered_sensitivity_grid"
    )
    outcome: Literal["comparable_within_registered_grid"] = (
        "comparable_within_registered_grid"
    )
    parameter_ids: tuple[SafeId, ...] = Field(
        min_length=1,
        max_length=MAX_PARAMETER_SETS,
    )
    quantity_id: SafeId
    unit: SafeId
    atlas_sha256: Sha256
    denominator_difference: Literal["preregistered_whole_molecule_subset"] = (
        "preregistered_whole_molecule_subset"
    )
    longitudinal_compatibility_inferred: Literal[False] = False

    @model_validator(mode="after")
    def canonical_parameters(self) -> SensitivityCompatibility:
        if self.parameter_ids != tuple(sorted(set(self.parameter_ids))):
            raise ValueError("compatibility parameter IDs must be uniquely sorted")
        return self


class SensitivitySource(CompatibilityContract):
    schema_version: Literal["traceback.sensitivity-source.v1"] = (
        "traceback.sensitivity-source.v1"
    )
    catalog_ref: CatalogResultRef
    explorer_artifact: CellOriginExplorerArtifact
    explorer_sha256: Sha256

    @model_validator(mode="after")
    def exact_source(self) -> SensitivitySource:
        encoded = canonical_cell_origin_explorer_bytes(self.explorer_artifact)
        if hashlib.sha256(encoded).hexdigest() != self.explorer_sha256:
            raise ValueError("explorer artifact digest does not match")
        if self.explorer_artifact.view.status != ExplorerStatus.READY:
            raise ValueError("sensitivity source must be a ready E08 view")
        replay_source = self.explorer_artifact.request.source
        if replay_source is None:
            raise ValueError("ready E08 source lacks replay evidence")
        view_source = self.explorer_artifact.request.result_view_request.sources[0]
        record = view_source.record
        capability = record.current_capability
        catalog = self.catalog_ref
        _require_safe_token(
            catalog.bundle_record_id,
            field="catalog bundle record ID",
        )
        _require_safe_token(
            catalog.workflow_release_id,
            field="catalog workflow release ID",
        )
        if (
            catalog.bundle_sha256 != record.bundle_sha256
            or catalog.method_ref != record.method.method_ref
            or catalog.method_definition_sha256 != record.method_definition_sha256
            or catalog.registry_sha256 != capability.registry_sha256
            or catalog.registry_version != capability.registry_version
            or catalog.authority_head_sha256 != capability.authority_head_sha256
            or catalog.authority_revision != capability.authority_revision
            or catalog.authority_scope != capability.authority_scope
            or catalog.capability_as_of != capability.as_of
            or catalog.qualification_state.value
            != (
                capability.qualification_state.value
                if capability.qualification_state is not None
                else "unknown"
            )
            or (
                catalog.display_role.value
                if catalog.display_role is not None
                else None
            )
            != (
                capability.display_role.value
                if capability.display_role is not None
                else None
            )
            or catalog.research_inspectable != capability.research_inspectable
            or catalog.current_provider_eligible
            != capability.current_provider_eligible
        ):
            raise ValueError("E04 catalog identity does not match E08/E05 source")
        return self

    @property
    def record(self) -> VerifiedMeasurementRecord:
        return self.explorer_artifact.request.result_view_request.sources[0].record

    @property
    def source_result(self) -> CellOriginResult:
        source = self.explorer_artifact.request.source
        assert source is not None
        return source.result


class SubsetLevel(CompatibilityContract):
    subset_id: SafeId
    fraction_ppm: int = Field(ge=1, le=1_000_000)
    target_molecule_count: int = Field(ge=1, le=10**15)

    @model_validator(mode="after")
    def safe_identity(self) -> SubsetLevel:
        _safe_id(self.subset_id, field="subset ID")
        return self


class ReplicateSeed(CompatibilityContract):
    replicate_id: SafeId
    seed: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def safe_identity(self) -> ReplicateSeed:
        _safe_id(self.replicate_id, field="replicate ID")
        return self


class SubsetMembershipCommitment(CompatibilityContract):
    subset_id: SafeId
    replicate_id: SafeId
    membership_encoding: Literal[
        "sorted-unique-whole-molecule-digests-sha256.v1"
    ] = "sorted-unique-whole-molecule-digests-sha256.v1"
    membership_count: int = Field(ge=1, le=10**15)
    membership_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.subset_id, self.replicate_id


class WholeMoleculeSubsetFamily(CompatibilityContract):
    schema_version: Literal["traceback.whole-molecule-subset-family.v1"] = (
        "traceback.whole-molecule-subset-family.v1"
    )
    family_id: SafeId
    source_molecule_count: int = Field(ge=1, le=10**15)
    selection_unit: Literal["whole_molecule"] = "whole_molecule"
    selection_algorithm: Literal["sha256-seeded-whole-molecule-rank.v1"] = (
        "sha256-seeded-whole-molecule-rank.v1"
    )
    nested_subsets: Literal[True] = True
    edge_inclusion_policy: EdgeInclusionPolicy
    levels: tuple[SubsetLevel, ...] = Field(
        min_length=1,
        max_length=MAX_SUBSET_LEVELS,
    )
    replicates: tuple[ReplicateSeed, ...] = Field(
        min_length=1,
        max_length=MAX_REPLICATES,
    )
    membership_commitments: tuple[SubsetMembershipCommitment, ...] = Field(
        min_length=1,
        max_length=MAX_SUBSET_LEVELS * MAX_REPLICATES,
    )

    @model_validator(mode="after")
    def canonical_family(self) -> WholeMoleculeSubsetFamily:
        _safe_id(self.family_id, field="subset family ID")
        level_keys = [(item.fraction_ppm, item.subset_id) for item in self.levels]
        if level_keys != sorted(level_keys):
            raise ValueError("subset levels must use canonical order")
        if len({item.subset_id for item in self.levels}) != len(self.levels):
            raise ValueError("subset IDs must be unique")
        if len({item.fraction_ppm for item in self.levels}) != len(self.levels):
            raise ValueError("subset fractions must be unique")
        if self.levels[-1].fraction_ppm != 1_000_000:
            raise ValueError("subset family must include the full-molecule level")
        for level in self.levels:
            expected = max(
                1,
                self.source_molecule_count * level.fraction_ppm // 1_000_000,
            )
            if level.target_molecule_count != expected:
                raise ValueError("subset target does not match registered fraction")
        target_counts = [item.target_molecule_count for item in self.levels]
        if len(target_counts) != len(set(target_counts)):
            raise ValueError(
                "subset fractions must produce unique effective target counts"
            )
        replicate_keys = [(item.replicate_id, item.seed) for item in self.replicates]
        if replicate_keys != sorted(replicate_keys):
            raise ValueError("replicate seeds must use canonical order")
        if len({item.replicate_id for item in self.replicates}) != len(
            self.replicates
        ) or len({item.seed for item in self.replicates}) != len(self.replicates):
            raise ValueError("replicate IDs and seeds must be unique")
        expected_membership_keys = tuple(
            sorted(
                (
                    (level.subset_id, replicate.replicate_id)
                    for level, replicate in product(self.levels, self.replicates)
                )
            )
        )
        membership_keys = tuple(
            commitment.sort_key for commitment in self.membership_commitments
        )
        if membership_keys != expected_membership_keys:
            raise ValueError(
                "membership commitments must equal the full subset-replicate grid"
            )
        target_by_subset = {
            level.subset_id: level.target_molecule_count for level in self.levels
        }
        for commitment in self.membership_commitments:
            if commitment.membership_count != target_by_subset[commitment.subset_id]:
                raise ValueError(
                    "membership commitment count must equal registered target"
                )
        full_subset_id = self.levels[-1].subset_id
        full_memberships = {
            item.membership_sha256
            for item in self.membership_commitments
            if item.subset_id == full_subset_id
        }
        if len(full_memberships) != 1:
            raise ValueError(
                "full subset membership must be identical across replicate seeds"
            )
        return self


class CellOriginRunParameters(CompatibilityContract):
    minimum_cpgs: int = Field(ge=1, le=100)
    unmethylated_max_exclusive: float = Field(
        gt=0.0,
        lt=1.0,
        allow_inf_nan=False,
    )
    methylated_min_inclusive: float = Field(
        gt=0.0,
        lt=1.0,
        allow_inf_nan=False,
    )
    nnls_row_scale: NnlsRowScale
    solver_tolerance: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    max_iterations: int = Field(ge=1, le=10_000_000)

    @model_validator(mode="after")
    def separated_thresholds(self) -> CellOriginRunParameters:
        if self.unmethylated_max_exclusive >= self.methylated_min_inclusive:
            raise ValueError("UXM thresholds must leave an explicit X interval")
        return self


class RegisteredParameterSet(CompatibilityContract):
    parameter_id: SafeId
    method: MethodDefinition
    method_definition_sha256: Sha256
    atlas_asset: AssetReference
    parameters: CellOriginRunParameters
    parameters_sha256: Sha256

    @model_validator(mode="after")
    def exact_parameter_identity(self) -> RegisteredParameterSet:
        _safe_id(self.parameter_id, field="parameter ID")
        if self.method.family != MethodFamily.CELL_ORIGIN:
            raise ValueError("sensitivity parameter method must be cell_origin")
        if self.method_definition_sha256 != method_definition_sha256(self.method):
            raise ValueError("parameter method digest does not match")
        if self.atlas_asset not in self.method.assets:
            raise ValueError("parameter atlas is not bound by exact method")
        if self.parameters_sha256 != _digest(self.parameters):
            raise ValueError("parameter digest does not match exact parameters")
        return self


class RegisteredRunKey(CompatibilityContract):
    subset_id: SafeId
    replicate_id: SafeId
    parameter_id: SafeId

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return self.subset_id, self.replicate_id, self.parameter_id


class RegisteredSubsetReceipt(CompatibilityContract):
    subset_id: SafeId
    replicate_id: SafeId
    membership_encoding: Literal[
        "sorted-unique-whole-molecule-digests-sha256.v1"
    ] = "sorted-unique-whole-molecule-digests-sha256.v1"
    membership_count: int = Field(ge=1, le=10**15)
    membership_sha256: Sha256
    subset_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.subset_id, self.replicate_id


def _registered_subset_sha256(
    *,
    source_explorer_sha256: str,
    source_result_sha256: str,
    source_bundle_sha256: str,
    source_method_definition_sha256: str,
    source_atlas_sha256: str,
    source_filter_sha256: str,
    subset_family: WholeMoleculeSubsetFamily,
    level: SubsetLevel,
    replicate: ReplicateSeed,
    commitment: SubsetMembershipCommitment,
) -> str:
    return _digest(
        {
            "schema_version": "traceback.registered-subset-receipt.v1",
            "source_explorer_sha256": source_explorer_sha256,
            "source_result_sha256": source_result_sha256,
            "source_bundle_sha256": source_bundle_sha256,
            "source_method_definition_sha256": source_method_definition_sha256,
            "source_atlas_sha256": source_atlas_sha256,
            "source_filter_sha256": source_filter_sha256,
            "family_id": subset_family.family_id,
            "source_molecule_count": subset_family.source_molecule_count,
            "selection_unit": subset_family.selection_unit,
            "selection_algorithm": subset_family.selection_algorithm,
            "nested_subsets": subset_family.nested_subsets,
            "edge_inclusion_policy": subset_family.edge_inclusion_policy,
            "subset_id": level.subset_id,
            "fraction_ppm": level.fraction_ppm,
            "target_molecule_count": level.target_molecule_count,
            "replicate_id": replicate.replicate_id,
            "seed": replicate.seed,
            "membership_encoding": commitment.membership_encoding,
            "membership_count": commitment.membership_count,
            "membership_sha256": commitment.membership_sha256,
        }
    )


class SensitivityRegistration(CompatibilityContract):
    schema_version: Literal["traceback.sensitivity-registration.v1"] = (
        "traceback.sensitivity-registration.v1"
    )
    registration_id: SafeId
    source_explorer_sha256: Sha256
    source_result_sha256: Sha256
    source_bundle_sha256: Sha256
    source_method_definition_sha256: Sha256
    source_atlas_sha256: Sha256
    source_filter_sha256: Sha256
    subset_family: WholeMoleculeSubsetFamily
    parameter_sets: tuple[RegisteredParameterSet, ...] = Field(
        min_length=1,
        max_length=MAX_PARAMETER_SETS,
    )
    run_grid: tuple[RegisteredRunKey, ...] = Field(
        min_length=1,
        max_length=MAX_RUNS,
    )
    subset_receipts: tuple[RegisteredSubsetReceipt, ...] = Field(
        min_length=1,
        max_length=MAX_SUBSET_LEVELS * MAX_REPLICATES,
    )
    registration_sha256: Sha256

    @model_validator(mode="after")
    def closed_grid(self) -> SensitivityRegistration:
        _safe_id(self.registration_id, field="registration ID")
        parameter_ids = [item.parameter_id for item in self.parameter_sets]
        if parameter_ids != sorted(parameter_ids) or len(parameter_ids) != len(
            set(parameter_ids)
        ):
            raise ValueError("parameter sets must use unique canonical order")
        parameter_digests = [item.parameters_sha256 for item in self.parameter_sets]
        if len(parameter_digests) != len(set(parameter_digests)):
            raise ValueError("parameter sets must have unique parameter payloads")
        expected = tuple(
            sorted(
                (
                    RegisteredRunKey(
                        subset_id=level.subset_id,
                        replicate_id=replicate.replicate_id,
                        parameter_id=parameter.parameter_id,
                    )
                    for level, replicate, parameter in product(
                        self.subset_family.levels,
                        self.subset_family.replicates,
                        self.parameter_sets,
                    )
                ),
                key=lambda item: item.sort_key,
            )
        )
        if self.run_grid != expected:
            raise ValueError(
                "run grid must equal the full preregistered Cartesian grid"
            )
        level_by_id = {
            item.subset_id: item for item in self.subset_family.levels
        }
        replicate_by_id = {
            item.replicate_id: item for item in self.subset_family.replicates
        }
        commitment_by_key = {
            item.sort_key: item
            for item in self.subset_family.membership_commitments
        }
        expected_receipts = tuple(
            RegisteredSubsetReceipt(
                subset_id=subset_id,
                replicate_id=replicate_id,
                membership_count=commitment_by_key[
                    (subset_id, replicate_id)
                ].membership_count,
                membership_sha256=commitment_by_key[
                    (subset_id, replicate_id)
                ].membership_sha256,
                subset_sha256=_registered_subset_sha256(
                    source_explorer_sha256=self.source_explorer_sha256,
                    source_result_sha256=self.source_result_sha256,
                    source_bundle_sha256=self.source_bundle_sha256,
                    source_method_definition_sha256=(
                        self.source_method_definition_sha256
                    ),
                    source_atlas_sha256=self.source_atlas_sha256,
                    source_filter_sha256=self.source_filter_sha256,
                    subset_family=self.subset_family,
                    level=level_by_id[subset_id],
                    replicate=replicate_by_id[replicate_id],
                    commitment=commitment_by_key[(subset_id, replicate_id)],
                ),
            )
            for subset_id, replicate_id in sorted(commitment_by_key)
        )
        if self.subset_receipts != expected_receipts:
            raise ValueError("subset receipts do not match preregistered context")
        expected_digest = _model_digest(self, exclude={"registration_sha256"})
        if self.registration_sha256 != expected_digest:
            raise ValueError("registration digest does not match exact run grid")
        return self


def _model_digest(
    contract: CompatibilityContract,
    *,
    exclude: set[str] | None = None,
) -> str:
    return _digest(contract.model_dump(mode="json", exclude=exclude or set()))


def register_sensitivity_study(
    *,
    registration_id: str,
    source: SensitivitySource,
    subset_family: WholeMoleculeSubsetFamily,
    parameter_sets: tuple[RegisteredParameterSet, ...],
) -> SensitivityRegistration:
    source_record = source.record
    source_filter = source.explorer_artifact.request.result_view_request.filters
    source_atlas = source_record.compatibility_key.atlas_asset
    assert source_atlas is not None
    source_filter_sha256 = result_filters_sha256(source_filter)
    run_grid = tuple(
        sorted(
            (
                RegisteredRunKey(
                    subset_id=level.subset_id,
                    replicate_id=replicate.replicate_id,
                    parameter_id=parameter.parameter_id,
                )
                for level, replicate, parameter in product(
                    subset_family.levels,
                    subset_family.replicates,
                    parameter_sets,
                )
            ),
            key=lambda item: item.sort_key,
        )
    )
    level_by_id = {item.subset_id: item for item in subset_family.levels}
    replicate_by_id = {
        item.replicate_id: item for item in subset_family.replicates
    }
    subset_receipts = tuple(
        RegisteredSubsetReceipt(
            subset_id=commitment.subset_id,
            replicate_id=commitment.replicate_id,
            membership_count=commitment.membership_count,
            membership_sha256=commitment.membership_sha256,
            subset_sha256=_registered_subset_sha256(
                source_explorer_sha256=source.explorer_sha256,
                source_result_sha256=source_record.result_sha256,
                source_bundle_sha256=source_record.bundle_sha256,
                source_method_definition_sha256=(
                    source_record.method_definition_sha256
                ),
                source_atlas_sha256=source_atlas.content_sha256,
                source_filter_sha256=source_filter_sha256,
                subset_family=subset_family,
                level=level_by_id[commitment.subset_id],
                replicate=replicate_by_id[commitment.replicate_id],
                commitment=commitment,
            ),
        )
        for commitment in subset_family.membership_commitments
    )
    payload: dict[str, Any] = {
        "registration_id": registration_id,
        "source_explorer_sha256": source.explorer_sha256,
        "source_result_sha256": source_record.result_sha256,
        "source_bundle_sha256": source_record.bundle_sha256,
        "source_method_definition_sha256": source_record.method_definition_sha256,
        "source_atlas_sha256": source_atlas.content_sha256,
        "source_filter_sha256": source_filter_sha256,
        "subset_family": subset_family,
        "parameter_sets": parameter_sets,
        "run_grid": run_grid,
        "subset_receipts": subset_receipts,
    }
    placeholder = SensitivityRegistration.model_construct(
        **payload,
        registration_sha256="0" * 64,
    )
    return SensitivityRegistration(
        **payload,
        registration_sha256=_model_digest(
            placeholder,
            exclude={"registration_sha256"},
        ),
    )


class RunAttrition(CompatibilityContract):
    source_molecules: int = Field(ge=1, le=10**15)
    target_molecules: int = Field(ge=1, le=10**15)
    selected_molecules: int = Field(ge=1, le=10**15)
    accepted_molecules: int = Field(ge=0, le=10**15)
    excluded_by_edge_policy: int = Field(ge=0, le=10**15)
    excluded_by_method: int = Field(ge=0, le=10**15)

    @model_validator(mode="after")
    def reconciled(self) -> RunAttrition:
        if self.selected_molecules != self.target_molecules:
            raise ValueError("whole-molecule selection must meet registered target")
        if self.selected_molecules != (
            self.accepted_molecules
            + self.excluded_by_edge_policy
            + self.excluded_by_method
        ):
            raise ValueError("run attrition does not reconcile selected molecules")
        return self


class SamplingEstimate(CompatibilityContract):
    contributor_id: SafeId
    estimate_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    uncertainty_kind: Literal["sampling_bootstrap"] = "sampling_bootstrap"
    uncertainty_status: BootstrapInformationStatus | Literal["not_run"]
    lower_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    upper_fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def explicit_uncertainty(self) -> SamplingEstimate:
        _safe_id(self.contributor_id, field="contributor ID")
        available = self.uncertainty_status == BootstrapInformationStatus.AVAILABLE
        if available != (
            self.lower_fraction is not None and self.upper_fraction is not None
        ):
            raise ValueError("only available sampling uncertainty may have bounds")
        if available:
            assert self.lower_fraction is not None and self.upper_fraction is not None
            if not self.lower_fraction <= self.estimate_fraction <= self.upper_fraction:
                raise ValueError("sampling interval must contain estimate")
        return self


class SensitivityRunBinding(CompatibilityContract):
    result_id: SafeId
    result_sha256: Sha256
    bundle_id: SafeId
    bundle_sha256: Sha256
    method_ref: MethodReference
    method_definition_sha256: Sha256
    atlas_id: SafeId
    atlas_sha256: Sha256
    filter_sha256: Sha256
    seed: int = Field(ge=0, le=2**63 - 1)
    subset_sha256: Sha256
    parameters_sha256: Sha256


def sensitivity_result_sha256(
    *,
    result_id: str,
    key: RegisteredRunKey,
    attrition: RunAttrition,
    subset_sha256: str,
    parameters_sha256: str,
    estimates: tuple[SamplingEstimate, ...],
) -> str:
    return _digest(
        {
            "schema_version": "traceback.sensitivity-result-evidence.v1",
            "result_id": result_id,
            "key": key.model_dump(mode="json"),
            "attrition": attrition.model_dump(mode="json"),
            "subset_sha256": subset_sha256,
            "parameters_sha256": parameters_sha256,
            "estimates": tuple(
                item.model_dump(mode="json") for item in estimates
            ),
        }
    )


def sensitivity_bundle_sha256(
    *,
    bundle_id: str,
    result_sha256: str,
    method_ref: MethodReference,
    method_definition_sha256: str,
    atlas_id: str,
    atlas_sha256: str,
    filter_sha256: str,
    seed: int,
    subset_sha256: str,
    parameters_sha256: str,
) -> str:
    return _digest(
        {
            "schema_version": "traceback.sensitivity-result-bundle.v1",
            "bundle_id": bundle_id,
            "result_sha256": result_sha256,
            "method_ref": method_ref.model_dump(mode="json"),
            "method_definition_sha256": method_definition_sha256,
            "atlas_id": atlas_id,
            "atlas_sha256": atlas_sha256,
            "filter_sha256": filter_sha256,
            "seed": seed,
            "subset_sha256": subset_sha256,
            "parameters_sha256": parameters_sha256,
        }
    )


class SensitivityRunOutcome(CompatibilityContract):
    key: RegisteredRunKey
    status: RunStatus
    attrition: RunAttrition
    subset_sha256: Sha256
    binding: SensitivityRunBinding | None = None
    estimates: tuple[SamplingEstimate, ...] = Field(
        default=(),
        max_length=MAX_CONTRIBUTORS,
    )
    failure_code: FailureCode | None = None

    @model_validator(mode="after")
    def status_payload(self) -> SensitivityRunOutcome:
        estimate_ids = [item.contributor_id for item in self.estimates]
        if estimate_ids != sorted(estimate_ids) or len(estimate_ids) != len(
            set(estimate_ids)
        ):
            raise ValueError(
                "run estimates must use unique canonical contributor order"
            )
        if self.status == RunStatus.COMPLETE:
            if (
                self.binding is None
                or not self.estimates
                or self.failure_code is not None
            ):
                raise ValueError("complete run requires binding and estimates only")
        elif self.status == RunStatus.FAILED:
            if self.binding is not None or self.estimates or self.failure_code is None:
                raise ValueError(
                    "failed run requires failure code and no numeric result"
                )
        elif self.binding is not None or self.estimates or self.failure_code != (
            FailureCode.INSUFFICIENT_MOLECULES
        ):
            raise ValueError(
                "insufficient run requires its explicit code and no numeric result"
            )
        if (
            self.binding is not None
            and self.binding.subset_sha256 != self.subset_sha256
        ):
            raise ValueError("run binding does not match subset receipt")
        if self.status == RunStatus.COMPLETE:
            assert self.binding is not None
            expected_result = sensitivity_result_sha256(
                result_id=self.binding.result_id,
                key=self.key,
                attrition=self.attrition,
                subset_sha256=self.subset_sha256,
                parameters_sha256=self.binding.parameters_sha256,
                estimates=self.estimates,
            )
            if self.binding.result_sha256 != expected_result:
                raise ValueError("result digest does not bind exact numeric evidence")
            expected_bundle = sensitivity_bundle_sha256(
                bundle_id=self.binding.bundle_id,
                result_sha256=self.binding.result_sha256,
                method_ref=self.binding.method_ref,
                method_definition_sha256=(
                    self.binding.method_definition_sha256
                ),
                atlas_id=self.binding.atlas_id,
                atlas_sha256=self.binding.atlas_sha256,
                filter_sha256=self.binding.filter_sha256,
                seed=self.binding.seed,
                subset_sha256=self.binding.subset_sha256,
                parameters_sha256=self.binding.parameters_sha256,
            )
            if self.binding.bundle_sha256 != expected_bundle:
                raise ValueError("bundle digest does not bind exact result evidence")
        return self


class StudyAttrition(CompatibilityContract):
    expected_replicates: int = Field(ge=1, le=MAX_RUNS)
    completed_replicates: int = Field(ge=0, le=MAX_RUNS)
    insufficient_replicates: int = Field(ge=0, le=MAX_RUNS)
    failed_replicates: int = Field(ge=0, le=MAX_RUNS)

    @model_validator(mode="after")
    def reconciled(self) -> StudyAttrition:
        if self.expected_replicates != (
            self.completed_replicates
            + self.insufficient_replicates
            + self.failed_replicates
        ):
            raise ValueError("study replicate attrition does not reconcile")
        return self


class SensitivityStudyBundle(CompatibilityContract):
    schema_version: Literal["traceback.sensitivity-study-bundle.v1"] = (
        "traceback.sensitivity-study-bundle.v1"
    )
    source: SensitivitySource
    registration: SensitivityRegistration
    outcomes: tuple[SensitivityRunOutcome, ...] = Field(
        min_length=1,
        max_length=MAX_RUNS,
    )
    attrition: StudyAttrition

    @model_validator(mode="after")
    def exact_registered_study(self) -> SensitivityStudyBundle:
        record = self.source.record
        registration = self.registration
        if (
            registration.source_explorer_sha256 != self.source.explorer_sha256
            or registration.source_result_sha256 != record.result_sha256
            or registration.source_bundle_sha256 != record.bundle_sha256
            or registration.source_method_definition_sha256
            != record.method_definition_sha256
            or registration.source_filter_sha256
            != self.source.explorer_artifact.view.binding.filter_sha256
        ):
            raise ValueError("registration does not bind exact source identities")
        outcome_keys = [item.key for item in self.outcomes]
        if tuple(outcome_keys) != registration.run_grid:
            raise ValueError("outcomes must include every registered grid cell exactly")
        level_by_id = {
            item.subset_id: item for item in registration.subset_family.levels
        }
        replicate_by_id = {
            item.replicate_id: item
            for item in registration.subset_family.replicates
        }
        parameter_by_id = {
            item.parameter_id: item for item in registration.parameter_sets
        }
        source_atlas = record.compatibility_key.atlas_asset
        assert source_atlas is not None
        if registration.source_atlas_sha256 != source_atlas.content_sha256:
            raise ValueError("registration does not bind exact source atlas")
        source_denominators = self.source.explorer_artifact.view.fragment_denominators
        assert source_denominators is not None
        if (
            registration.subset_family.source_molecule_count
            != source_denominators.input_fragments
        ):
            raise ValueError(
                "subset family molecule denominator does not match exact source"
            )
        for parameter in registration.parameter_sets:
            if (
                parameter.method != record.method
                or parameter.method_definition_sha256
                != record.method_definition_sha256
                or parameter.atlas_asset != source_atlas
            ):
                raise ValueError(
                    "parameter set is incompatible with exact source method or atlas"
                )
        source_contributors = tuple(
            sorted(
                item.contributor_id
                for item in self.source.explorer_artifact.view.dot_interval_rows
            )
        )
        registered_receipts = {
            item.sort_key: item for item in registration.subset_receipts
        }
        complete_bindings: list[SensitivityRunBinding] = []
        for outcome in self.outcomes:
            level = level_by_id[outcome.key.subset_id]
            replicate = replicate_by_id[outcome.key.replicate_id]
            parameter = parameter_by_id[outcome.key.parameter_id]
            if (
                outcome.attrition.source_molecules
                != registration.subset_family.source_molecule_count
                or outcome.attrition.target_molecules != level.target_molecule_count
            ):
                raise ValueError("run attrition does not match registered subset")
            receipt_key = (outcome.key.subset_id, outcome.key.replicate_id)
            if (
                outcome.subset_sha256
                != registered_receipts[receipt_key].subset_sha256
            ):
                raise ValueError("run does not match preregistered subset receipt")
            if outcome.status == RunStatus.COMPLETE:
                assert outcome.binding is not None
                binding = outcome.binding
                complete_bindings.append(binding)
                if (
                    binding.method_ref != parameter.method.method_ref
                    or binding.method_definition_sha256
                    != parameter.method_definition_sha256
                    or binding.atlas_id != parameter.atlas_asset.asset_id
                    or binding.atlas_sha256 != parameter.atlas_asset.content_sha256
                    or binding.filter_sha256 != registration.source_filter_sha256
                    or binding.seed != replicate.seed
                    or binding.parameters_sha256 != parameter.parameters_sha256
                ):
                    raise ValueError("run binding does not match registration")
                if tuple(item.contributor_id for item in outcome.estimates) != (
                    source_contributors
                ):
                    raise ValueError("run contributors do not match source atlas")
        for field in (
            "result_id",
            "result_sha256",
            "bundle_id",
            "bundle_sha256",
        ):
            identities = [getattr(item, field) for item in complete_bindings]
            if len(identities) != len(set(identities)):
                raise ValueError(f"complete run {field} identities must be unique")
        counts = {
            status: sum(item.status == status for item in self.outcomes)
            for status in RunStatus
        }
        expected_attrition = StudyAttrition(
            expected_replicates=len(self.outcomes),
            completed_replicates=counts[RunStatus.COMPLETE],
            insufficient_replicates=counts[RunStatus.INSUFFICIENT_INFORMATION],
            failed_replicates=counts[RunStatus.FAILED],
        )
        if self.attrition != expected_attrition:
            raise ValueError("study attrition does not match exact run outcomes")
        return self


class RunViewRow(CompatibilityContract):
    key: RegisteredRunKey
    status: RunStatus
    fraction_ppm: int = Field(ge=1, le=1_000_000)
    seed: int = Field(ge=0, le=2**63 - 1)
    subset_sha256: Sha256
    parameters_sha256: Sha256
    attrition: RunAttrition
    estimates: tuple[SamplingEstimate, ...] = Field(max_length=MAX_CONTRIBUTORS)
    failure_code: FailureCode | None = None


class ParameterSensitivityValue(CompatibilityContract):
    parameter_id: SafeId
    run_status: RunStatus
    estimate_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def no_missing_as_zero(self) -> ParameterSensitivityValue:
        if (self.run_status == RunStatus.COMPLETE) != (
            self.estimate_fraction is not None
        ):
            raise ValueError("only complete runs may carry sensitivity values")
        return self


class MethodSensitivityRow(CompatibilityContract):
    subset_id: SafeId
    replicate_id: SafeId
    contributor_id: SafeId
    interpretation: Literal[
        "registered_parameter_range_not_sampling_interval"
    ] = "registered_parameter_range_not_sampling_interval"
    values: tuple[ParameterSensitivityValue, ...] = Field(
        min_length=1,
        max_length=MAX_PARAMETER_SETS,
    )
    availability: SensitivityAvailability
    minimum_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    maximum_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def explicit_method_range(self) -> MethodSensitivityRow:
        ids = [item.parameter_id for item in self.values]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("method sensitivity values must use full canonical grid")
        available = [
            item.estimate_fraction
            for item in self.values
            if item.estimate_fraction is not None
        ]
        expected_availability = (
            SensitivityAvailability.INSUFFICIENT
            if not available
            else SensitivityAvailability.AVAILABLE
            if len(available) == len(self.values)
            else SensitivityAvailability.PARTIAL
        )
        if self.availability != expected_availability:
            raise ValueError("method sensitivity availability is inconsistent")
        expected_bounds = (
            (min(available), max(available)) if available else (None, None)
        )
        if (self.minimum_fraction, self.maximum_fraction) != expected_bounds:
            raise ValueError("method sensitivity range does not match grid values")
        return self


class SensitivityComparisonView(CompatibilityContract):
    schema_version: Literal["traceback.sensitivity-comparison-view.v1"] = (
        "traceback.sensitivity-comparison-view.v1"
    )
    registration_sha256: Sha256
    source_result_sha256: Sha256
    source_bundle_sha256: Sha256
    source_method_definition_sha256: Sha256
    source_atlas_sha256: Sha256
    source_filter_sha256: Sha256
    subset_family_id: SafeId
    edge_inclusion_policy: EdgeInclusionPolicy
    compatibility: SensitivityCompatibility
    run_rows: tuple[RunViewRow, ...] = Field(min_length=1, max_length=MAX_RUNS)
    method_sensitivity_rows: tuple[MethodSensitivityRow, ...] = Field(
        max_length=MAX_SUBSET_LEVELS * MAX_REPLICATES * MAX_CONTRIBUTORS,
    )
    attrition: StudyAttrition
    bundle_sha256: Sha256


def build_sensitivity_comparison_view(
    bundle: SensitivityStudyBundle,
) -> SensitivityComparisonView:
    registration = bundle.registration
    level_by_id = {
        item.subset_id: item for item in registration.subset_family.levels
    }
    replicate_by_id = {
        item.replicate_id: item for item in registration.subset_family.replicates
    }
    parameter_by_id = {
        item.parameter_id: item for item in registration.parameter_sets
    }
    run_rows = tuple(
        RunViewRow(
            key=outcome.key,
            status=outcome.status,
            fraction_ppm=level_by_id[outcome.key.subset_id].fraction_ppm,
            seed=replicate_by_id[outcome.key.replicate_id].seed,
            subset_sha256=outcome.subset_sha256,
            parameters_sha256=parameter_by_id[
                outcome.key.parameter_id
            ].parameters_sha256,
            attrition=outcome.attrition,
            estimates=outcome.estimates,
            failure_code=outcome.failure_code,
        )
        for outcome in bundle.outcomes
    )
    outcome_by_key = {item.key.sort_key: item for item in bundle.outcomes}
    source_contributors = tuple(
        sorted(
            item.contributor_id
            for item in bundle.source.explorer_artifact.view.dot_interval_rows
        )
    )
    sensitivity_rows = []
    for level, replicate, contributor_id in product(
        registration.subset_family.levels,
        registration.subset_family.replicates,
        source_contributors,
    ):
        values = []
        for parameter in registration.parameter_sets:
            outcome = outcome_by_key[
                (level.subset_id, replicate.replicate_id, parameter.parameter_id)
            ]
            estimate = next(
                (
                    item.estimate_fraction
                    for item in outcome.estimates
                    if item.contributor_id == contributor_id
                ),
                None,
            )
            values.append(
                ParameterSensitivityValue(
                    parameter_id=parameter.parameter_id,
                    run_status=outcome.status,
                    estimate_fraction=estimate,
                )
            )
        available = [
            item.estimate_fraction
            for item in values
            if item.estimate_fraction is not None
        ]
        availability = (
            SensitivityAvailability.INSUFFICIENT
            if not available
            else SensitivityAvailability.AVAILABLE
            if len(available) == len(values)
            else SensitivityAvailability.PARTIAL
        )
        sensitivity_rows.append(
            MethodSensitivityRow(
                subset_id=level.subset_id,
                replicate_id=replicate.replicate_id,
                contributor_id=contributor_id,
                values=tuple(values),
                availability=availability,
                minimum_fraction=min(available) if available else None,
                maximum_fraction=max(available) if available else None,
            )
        )
    source_record = bundle.source.record
    atlas = source_record.compatibility_key.atlas_asset
    assert atlas is not None
    return SensitivityComparisonView(
        registration_sha256=registration.registration_sha256,
        source_result_sha256=source_record.result_sha256,
        source_bundle_sha256=source_record.bundle_sha256,
        source_method_definition_sha256=source_record.method_definition_sha256,
        source_atlas_sha256=atlas.content_sha256,
        source_filter_sha256=registration.source_filter_sha256,
        subset_family_id=registration.subset_family.family_id,
        edge_inclusion_policy=registration.subset_family.edge_inclusion_policy,
        compatibility=SensitivityCompatibility(
            parameter_ids=tuple(
                item.parameter_id for item in registration.parameter_sets
            ),
            quantity_id=source_record.method.quantity_id,
            unit=source_record.method.unit,
            atlas_sha256=atlas.content_sha256,
        ),
        run_rows=run_rows,
        method_sensitivity_rows=tuple(sensitivity_rows),
        attrition=bundle.attrition,
        bundle_sha256=_digest(bundle),
    )


class SensitivityComparisonArtifact(CompatibilityContract):
    schema_version: Literal["traceback.sensitivity-comparison-artifact.v1"] = (
        "traceback.sensitivity-comparison-artifact.v1"
    )
    bundle: SensitivityStudyBundle
    view: SensitivityComparisonView

    @model_validator(mode="after")
    def exact_replay(self) -> SensitivityComparisonArtifact:
        if build_sensitivity_comparison_view(self.bundle) != self.view:
            raise ValueError("sensitivity comparison does not replay exactly")
        if len(canonical_json_bytes(self)) > MAX_CANONICAL_BYTES:
            raise ValueError("sensitivity artifact exceeds canonical byte bound")
        return self


def build_sensitivity_comparison_artifact(
    bundle: SensitivityStudyBundle,
) -> SensitivityComparisonArtifact:
    return SensitivityComparisonArtifact(
        bundle=bundle,
        view=build_sensitivity_comparison_view(bundle),
    )


def canonical_sensitivity_comparison_bytes(
    artifact: SensitivityComparisonArtifact,
) -> bytes:
    content = canonical_json_bytes(artifact)
    if len(content) > MAX_CANONICAL_BYTES:
        raise SensitivityContractError(
            "sensitivity artifact exceeds canonical byte bound"
        )
    return content


def sensitivity_comparison_from_canonical_bytes(
    content: bytes,
) -> SensitivityComparisonArtifact:
    if len(content) > MAX_CANONICAL_BYTES:
        raise SensitivityContractError(
            "sensitivity artifact exceeds canonical byte bound"
        )
    try:
        artifact = SensitivityComparisonArtifact.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise SensitivityContractError("sensitivity artifact is invalid") from exc
    if canonical_sensitivity_comparison_bytes(artifact) != content:
        raise SensitivityContractError("sensitivity artifact JSON is not canonical")
    return artifact


__all__ = [
    "CellOriginRunParameters",
    "EdgeInclusionPolicy",
    "FailureCode",
    "MethodSensitivityRow",
    "RegisteredParameterSet",
    "RegisteredRunKey",
    "RegisteredSubsetReceipt",
    "ReplicateSeed",
    "RunAttrition",
    "RunStatus",
    "SamplingEstimate",
    "SensitivityAvailability",
    "SensitivityComparisonArtifact",
    "SensitivityComparisonView",
    "SensitivityCompatibility",
    "SensitivityContractError",
    "SensitivityRegistration",
    "SensitivityRunBinding",
    "SensitivityRunOutcome",
    "SensitivitySource",
    "SensitivityStudyBundle",
    "StudyAttrition",
    "SubsetLevel",
    "SubsetMembershipCommitment",
    "WholeMoleculeSubsetFamily",
    "build_sensitivity_comparison_artifact",
    "build_sensitivity_comparison_view",
    "canonical_sensitivity_comparison_bytes",
    "register_sensitivity_study",
    "sensitivity_bundle_sha256",
    "sensitivity_comparison_from_canonical_bytes",
    "sensitivity_result_sha256",
    "whole_molecule_membership_sha256",
]
