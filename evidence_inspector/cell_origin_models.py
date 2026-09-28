"""Strict contracts for cell-origin methylation evidence.

Local input contracts use irreversible fragment digests. ``CellOriginResult``
is the model-safe publication boundary: it contains aggregates and provenance,
never genomic calls, fragment digests, paths, or read identifiers.

Loyfer fragment-level UXM and Katsman MethAtlas CpG-average deconvolution are
represented as different method definitions. A result cannot silently combine
the atlas, feature definition, or observation unit from those methods.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Chromosome = Annotated[
    str,
    StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2]|X|Y|M)$"),
]
CanonicalFraction = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
NonNegativeFinite = Annotated[
    float,
    Field(ge=0.0, allow_inf_nan=False),
]
Version = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+:-]*$",
    ),
]
SourceIdentity = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s]+$",
    ),
]

UXM_UNMETHYLATED_MAX_EXCLUSIVE = 0.251
UXM_METHYLATED_MIN_INCLUSIVE = 0.75
UXM_MINIMUM_CPGS = 4


class StrictModel(BaseModel):
    """Immutable, non-coercive base model with a closed schema."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class Strand(StrEnum):
    PLUS = "+"
    MINUS = "-"


class CpgCallState(StrEnum):
    UNMETHYLATED = "unmethylated"
    METHYLATED = "methylated"


class UxmState(StrEnum):
    U = "U"
    X = "X"
    M = "M"


class CellOriginMethodFamily(StrEnum):
    LOYFER_FRAGMENT_UXM = "loyfer_fragment_uxm"
    KATSMAN_METHATLAS_CPG_NNLS = "katsman_methatlas_cpg_nnls"


class AtlasKind(StrEnum):
    LOYFER = "loyfer"
    METHATLAS = "methatlas"


class FeatureDefinition(StrEnum):
    FRAGMENT_U_FRACTION = "fragment_u_fraction"
    CPG_AVERAGE_METHYLATION = "cpg_average_methylation"


class ObservationUnit(StrEnum):
    CLASSIFIED_FRAGMENT = "classified_fragment"
    CPG = "cpg"


class SolverKind(StrEnum):
    NNLS = "nnls"


class NnlsRowScale(StrEnum):
    """Explicit row scaling applied before solving the NNLS system.

    ``REFERENCE_COUNT`` reproduces the inspected reference implementation by
    multiplying both the atlas row and observation by the classified-fragment
    count. ``SQRT_COUNT`` names the historical local transform descriptively;
    it does not assert a validated variance model.
    """

    REFERENCE_COUNT = "reference_count"
    SQRT_COUNT = "sqrt_count"
    UNWEIGHTED = "unweighted"


class ModProbabilityPolicy(StrEnum):
    """How canonical and modified cytosine states were selected."""

    HARD_CALL_COLLAPSED_M_H = "hard_call_collapsed_m_h"
    PRECALL_COMBINED_M_H = "precall_combined_m_h"


class ModkitSourceSchema(StrEnum):
    """Declared generic schemas plus one pinned native Modkit schema."""

    GENERIC_HARD_CALL_CPG_V2 = "traceback.generic-hard-call-cpg.v2"
    GENERIC_CMH_PROBABILITIES_V1 = "traceback.generic-cmh-probabilities.v1"
    MODKIT_EXTRACT_FULL_064 = "modkit.extract-full.v0.6.4"


class ReferenceContextValidationScope(StrEnum):
    CENTERED_CPG_DYAD = "centered_cpg_dyad"


class RangeClassification(StrEnum):
    BELOW = "below"
    WITHIN = "within"
    ABOVE = "above"


class VerificationLevel(StrEnum):
    REPORTED = "reported"
    RECOMPUTED = "recomputed"
    SAMPLED_RECOMPUTED = "sampled_recomputed"


class ReferenceRangeKind(StrEnum):
    OBSERVED_COHORT_RANGE = "observed_cohort_range"


class MethodDefinition(StrictModel):
    """A complete method identity; incompatible components are rejected."""

    family: CellOriginMethodFamily
    atlas_kind: AtlasKind
    feature_definition: FeatureDefinition
    observation_unit: ObservationUnit
    solver: Literal[SolverKind.NNLS] = SolverKind.NNLS

    @model_validator(mode="after")
    def validate_method_components(self) -> MethodDefinition:
        expected = {
            CellOriginMethodFamily.LOYFER_FRAGMENT_UXM: (
                AtlasKind.LOYFER,
                FeatureDefinition.FRAGMENT_U_FRACTION,
                ObservationUnit.CLASSIFIED_FRAGMENT,
            ),
            CellOriginMethodFamily.KATSMAN_METHATLAS_CPG_NNLS: (
                AtlasKind.METHATLAS,
                FeatureDefinition.CPG_AVERAGE_METHYLATION,
                ObservationUnit.CPG,
            ),
        }[self.family]
        observed = (
            self.atlas_kind,
            self.feature_definition,
            self.observation_unit,
        )
        if observed != expected:
            raise ValueError(
                "method family, atlas, feature definition, and observation "
                "unit are not scientifically compatible"
            )
        return self


LOYFER_UXM_METHOD = MethodDefinition(
    family=CellOriginMethodFamily.LOYFER_FRAGMENT_UXM,
    atlas_kind=AtlasKind.LOYFER,
    feature_definition=FeatureDefinition.FRAGMENT_U_FRACTION,
    observation_unit=ObservationUnit.CLASSIFIED_FRAGMENT,
)

KATSMAN_METHATLAS_METHOD = MethodDefinition(
    family=CellOriginMethodFamily.KATSMAN_METHATLAS_CPG_NNLS,
    atlas_kind=AtlasKind.METHATLAS,
    feature_definition=FeatureDefinition.CPG_AVERAGE_METHYLATION,
    observation_unit=ObservationUnit.CPG,
)


class UxmThresholds(StrictModel):
    """Fixed Loyfer fragment UXM thresholds."""

    minimum_cpgs: Literal[4] = UXM_MINIMUM_CPGS
    unmethylated_max_exclusive: Literal[0.251] = (
        UXM_UNMETHYLATED_MAX_EXCLUSIVE
    )
    methylated_min_inclusive: Literal[0.75] = (
        UXM_METHYLATED_MIN_INCLUSIVE
    )


def classify_uxm(methylation_fraction: float, cpg_count: int) -> UxmState:
    """Classify one marker-overlapping fragment using exact UXM boundaries."""

    if isinstance(cpg_count, bool) or not isinstance(cpg_count, int):
        raise TypeError("cpg_count must be an integer")
    if cpg_count < UXM_MINIMUM_CPGS:
        raise ValueError("UXM classification requires at least 4 CpGs")
    if (
        isinstance(methylation_fraction, bool)
        or not isinstance(methylation_fraction, (int, float))
        or not math.isfinite(methylation_fraction)
        or not 0.0 <= methylation_fraction <= 1.0
    ):
        raise ValueError("methylation_fraction must be finite and within [0, 1]")
    if methylation_fraction < UXM_UNMETHYLATED_MAX_EXCLUSIVE:
        return UxmState.U
    if methylation_fraction >= UXM_METHYLATED_MIN_INCLUSIVE:
        return UxmState.M
    return UxmState.X


class GenomicMarker(StrictModel):
    """Zero-based, half-open marker interval from a registered atlas."""

    marker_id: Identifier
    chromosome: Chromosome
    start0: int = Field(ge=0)
    end0: int = Field(gt=0)
    target_cell_type_id: Identifier
    atlas_id: Identifier
    source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @field_validator("source_ids")
    @classmethod
    def unique_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> GenomicMarker:
        if self.end0 <= self.start0:
            raise ValueError("marker interval must be non-empty and half-open")
        return self


class ModkitCpgCall(StrictModel):
    """Local-only, modkit-style single-fragment CpG call."""

    fragment_digest: Sha256
    chromosome: Chromosome
    position0: int = Field(ge=0)
    strand: Strand
    modification_code: Literal["m"] = "m"
    modified_probability: CanonicalFraction
    state: CpgCallState


class ModkitCpgCallV2(StrictModel):
    """Reference-validated local CpG call with explicit coordinate identity.

    ``original_position0`` is the reference position supplied by the generic
    adapter. ``canonical_cpg_position0`` is always the reference-forward C of
    the validated CpG dyad. The two strand fields are intentionally distinct.
    """

    schema_version: Literal["cell-origin-cpg-call.v2"] = (
        "cell-origin-cpg-call.v2"
    )
    fragment_digest: Sha256
    chromosome: Chromosome
    original_position0: int = Field(ge=0)
    canonical_cpg_position0: int = Field(ge=0)
    modification_strand: Strand
    reference_mod_strand: Strand
    selected_state_probability: CanonicalFraction
    state: CpgCallState
    policy: ModProbabilityPolicy

    @model_validator(mode="after")
    def validate_canonical_coordinate(self) -> ModkitCpgCallV2:
        if self.reference_mod_strand == Strand.PLUS:
            expected = self.original_position0
        else:
            if self.original_position0 == 0:
                raise ValueError("minus-strand CpG position cannot underflow")
            expected = self.original_position0 - 1
        if self.canonical_cpg_position0 != expected:
            raise ValueError(
                "canonical CpG position does not match reference modification strand"
            )
        return self


class ModkitInputProvenanceV2(StrictModel):
    """Identity and probability policies for a generic CpG adapter."""

    source_schema_id: ModkitSourceSchema
    source_schema_version: Version
    source_tool_id: SourceIdentity
    source_tool_version: SourceIdentity
    source_model_id: SourceIdentity
    source_model_version: SourceIdentity
    policy: ModProbabilityPolicy
    probability_threshold: CanonicalFraction | None
    probability_threshold_source: Literal["adapter_explicit", "source_unknown"]
    tie_policy: Literal["exclude_exact_ties"] = "exclude_exact_ties"
    probability_tie_tolerance: Literal[0.0] = 0.0
    probability_sum_tolerance: Literal[1e-6] = 1e-6
    reference_id: Identifier
    reference_sha256: Sha256
    reference_context_provider_id: Identifier
    reference_context_validation_scope: Literal[
        ReferenceContextValidationScope.CENTERED_CPG_DYAD
    ] = ReferenceContextValidationScope.CENTERED_CPG_DYAD
    coordinate_policy_id: Literal["canonical-reference-forward-cpg-c.v1"] = (
        "canonical-reference-forward-cpg-c.v1"
    )
    duplicate_policy: Literal["exact_observation_only"] = (
        "exact_observation_only"
    )

    @model_validator(mode="after")
    def validate_source_policy(self) -> ModkitInputProvenanceV2:
        if self.policy == ModProbabilityPolicy.HARD_CALL_COLLAPSED_M_H:
            if self.source_schema_id != ModkitSourceSchema.GENERIC_HARD_CALL_CPG_V2:
                raise ValueError(
                    "hard-call policy requires the generic hard-call schema"
                )
            if self.source_schema_version != "2":
                raise ValueError("generic hard-call schema version must be 2")
            if self.probability_threshold is not None:
                raise ValueError("hard-call input cannot claim an adapter threshold")
            if self.probability_threshold_source != "source_unknown":
                raise ValueError("hard-call threshold source must be source_unknown")
        else:
            if self.source_schema_id not in {
                ModkitSourceSchema.GENERIC_CMH_PROBABILITIES_V1,
                ModkitSourceSchema.MODKIT_EXTRACT_FULL_064,
            }:
                raise ValueError(
                    "precall-combined policy requires the generic C/m/h schema "
                    "or pinned native Modkit schema"
                )
            expected_version = (
                "1"
                if self.source_schema_id
                == ModkitSourceSchema.GENERIC_CMH_PROBABILITIES_V1
                else "0.6.4"
            )
            if self.source_schema_version != expected_version:
                raise ValueError("probability schema version does not match its ID")
            if self.probability_threshold is None:
                raise ValueError("probability input requires an explicit threshold")
            if self.probability_threshold_source != "adapter_explicit":
                raise ValueError(
                    "probability-input threshold source must be adapter_explicit"
                )
        return self


class ModkitIngestionLedgerV2(StrictModel):
    """Mutually exclusive row accounting for one successful generic adapter run."""

    policy: ModProbabilityPolicy
    total_rows: int = Field(ge=0)
    source_failed_rows: int = Field(ge=0)
    source_passed_rows: int = Field(ge=0)
    excluded_non_c_rows: int = Field(ge=0)
    candidate_c_rows: int = Field(ge=0)
    hard_call_c_rows: int = Field(ge=0)
    hard_call_m_rows: int = Field(ge=0)
    hard_call_h_rows: int = Field(ge=0)
    probability_input_rows: int = Field(ge=0)
    excluded_probability_tie_rows: int = Field(ge=0)
    excluded_low_confidence_rows: int = Field(ge=0)
    eligible_call_rows: int = Field(ge=0)
    unmethylated_call_rows: int = Field(ge=0)
    methylated_call_rows: int = Field(ge=0)
    reference_plus_call_rows: int = Field(ge=0)
    reference_minus_call_rows: int = Field(ge=0)
    malformed_rows: Literal[0] = 0
    duplicate_rows: Literal[0] = 0

    @model_validator(mode="after")
    def reconcile_stages(self) -> ModkitIngestionLedgerV2:
        if self.total_rows != self.source_failed_rows + self.source_passed_rows:
            raise ValueError("source failed and passed rows must equal total rows")
        if self.source_passed_rows != self.excluded_non_c_rows + self.candidate_c_rows:
            raise ValueError("non-C and candidate C rows must equal source-passed rows")
        terminal = (
            self.excluded_probability_tie_rows
            + self.excluded_low_confidence_rows
            + self.eligible_call_rows
        )
        if self.candidate_c_rows != terminal:
            raise ValueError("candidate C terminal buckets must reconcile")
        if self.eligible_call_rows != (
            self.unmethylated_call_rows + self.methylated_call_rows
        ):
            raise ValueError("call-state counts must equal eligible calls")
        if self.eligible_call_rows != (
            self.reference_plus_call_rows + self.reference_minus_call_rows
        ):
            raise ValueError("reference-strand counts must equal eligible calls")

        hard_rows = (
            self.hard_call_c_rows
            + self.hard_call_m_rows
            + self.hard_call_h_rows
        )
        if self.policy == ModProbabilityPolicy.HARD_CALL_COLLAPSED_M_H:
            if hard_rows != self.candidate_c_rows or self.probability_input_rows != 0:
                raise ValueError(
                    "hard-call source-state rows must partition candidates"
                )
            if self.excluded_probability_tie_rows or self.excluded_low_confidence_rows:
                raise ValueError(
                    "hard-call input cannot claim adapter probability exclusions"
                )
            if self.hard_call_c_rows != self.unmethylated_call_rows:
                raise ValueError(
                    "hard-call C rows must equal unmethylated calls"
                )
            if (
                self.hard_call_m_rows + self.hard_call_h_rows
                != self.methylated_call_rows
            ):
                raise ValueError(
                    "hard-call m and h rows must equal methylated calls"
                )
        elif hard_rows != 0 or self.probability_input_rows != self.candidate_c_rows:
            raise ValueError("probability-input rows must partition candidates")
        return self


class ModkitInputResultV2(StrictModel):
    """Calls plus a fully reconciled, reference-bound ingestion ledger."""

    schema_version: Literal["cell-origin-modkit-input.v2"] = (
        "cell-origin-modkit-input.v2"
    )
    provenance: ModkitInputProvenanceV2
    ledger: ModkitIngestionLedgerV2
    calls: tuple[ModkitCpgCallV2, ...]

    @model_validator(mode="after")
    def validate_calls(self) -> ModkitInputResultV2:
        if self.ledger.policy != self.provenance.policy:
            raise ValueError("ledger and provenance probability policies must match")
        if len(self.calls) != self.ledger.eligible_call_rows:
            raise ValueError("emitted calls must equal eligible-call ledger rows")
        if any(call.policy != self.provenance.policy for call in self.calls):
            raise ValueError("every call must use the provenance probability policy")

        unmethylated = sum(
            call.state == CpgCallState.UNMETHYLATED for call in self.calls
        )
        if unmethylated != self.ledger.unmethylated_call_rows:
            raise ValueError("unmethylated calls do not match the ledger")
        if len(self.calls) - unmethylated != self.ledger.methylated_call_rows:
            raise ValueError("methylated calls do not match the ledger")
        plus = sum(
            call.reference_mod_strand == Strand.PLUS for call in self.calls
        )
        if plus != self.ledger.reference_plus_call_rows:
            raise ValueError("plus-reference-strand calls do not match the ledger")
        if len(self.calls) - plus != self.ledger.reference_minus_call_rows:
            raise ValueError("minus-reference-strand calls do not match the ledger")

        threshold = self.provenance.probability_threshold
        if self.provenance.policy == ModProbabilityPolicy.PRECALL_COMBINED_M_H:
            if threshold is None:
                raise ValueError("probability input is missing its bound threshold")
            for call in self.calls:
                if call.selected_state_probability == 0.5:
                    raise ValueError("exact probability ties must be excluded")
                if call.selected_state_probability < 0.5:
                    raise ValueError(
                        "selected state probability must exceed 0.5"
                    )
                if call.selected_state_probability < threshold:
                    raise ValueError("call probability is below the bound threshold")

        seen: set[tuple[str, str, int, Strand, Strand]] = set()
        for call in self.calls:
            key = (
                call.fragment_digest,
                call.chromosome,
                call.canonical_cpg_position0,
                call.modification_strand,
                call.reference_mod_strand,
            )
            if key in seen:
                raise ValueError("exact call observations must be unique")
            seen.add(key)
        return self


class FragmentMarkerObservation(StrictModel):
    """Local-only fragment observation used to derive a UXM state."""

    fragment_digest: Sha256
    marker: GenomicMarker
    cpg_calls: tuple[ModkitCpgCall, ...] = Field(min_length=UXM_MINIMUM_CPGS)
    callable_cpg_count: int = Field(ge=UXM_MINIMUM_CPGS)
    methylated_cpg_count: int = Field(ge=0)
    methylation_fraction: CanonicalFraction
    state: UxmState

    @model_validator(mode="after")
    def validate_observation(self) -> FragmentMarkerObservation:
        if self.callable_cpg_count != len(self.cpg_calls):
            raise ValueError("callable_cpg_count must equal the number of CpG calls")
        methylated = sum(
            call.state == CpgCallState.METHYLATED for call in self.cpg_calls
        )
        if self.methylated_cpg_count != methylated:
            raise ValueError("methylated_cpg_count does not match CpG call states")
        expected_fraction = methylated / self.callable_cpg_count
        if not math.isclose(
            self.methylation_fraction,
            expected_fraction,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("methylation_fraction does not match CpG calls")
        if self.state != classify_uxm(
            self.methylation_fraction, self.callable_cpg_count
        ):
            raise ValueError("state does not match the fixed UXM thresholds")
        loci: set[tuple[str, int, Strand]] = set()
        for call in self.cpg_calls:
            if call.fragment_digest != self.fragment_digest:
                raise ValueError("all CpG calls must belong to the same fragment")
            if call.chromosome != self.marker.chromosome:
                raise ValueError("CpG call chromosome does not match marker")
            if not self.marker.start0 <= call.position0 < self.marker.end0:
                raise ValueError("CpG call lies outside the marker interval")
            locus = (call.chromosome, call.position0, call.strand)
            if locus in loci:
                raise ValueError("CpG loci must be unique within an observation")
            loci.add(locus)
        return self


class MarkerCountRow(StrictModel):
    """Aggregate U/X/M counts for one marker; safe for model input."""

    marker_id: Identifier
    u_count: int = Field(ge=0)
    x_count: int = Field(ge=0)
    m_count: int = Field(ge=0)
    classified_fragment_count: int = Field(ge=1)
    u_fraction: CanonicalFraction

    @model_validator(mode="after")
    def validate_counts(self) -> MarkerCountRow:
        total = self.u_count + self.x_count + self.m_count
        if total != self.classified_fragment_count:
            raise ValueError("U + X + M must equal classified_fragment_count")
        if not math.isclose(
            self.u_fraction,
            self.u_count / total,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("u_fraction must use all classified U/X/M fragments")
        return self


class AtlasUValue(StrictModel):
    cell_type_id: Identifier
    u_fraction: CanonicalFraction


class AtlasUMatrixRow(StrictModel):
    marker_id: Identifier
    values: tuple[AtlasUValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cell_types(self) -> AtlasUMatrixRow:
        ids = [value.cell_type_id for value in self.values]
        if len(set(ids)) != len(ids):
            raise ValueError("atlas row cell_type_id values must be unique")
        return self


class AtlasUMatrix(StrictModel):
    """Loyfer atlas matrix of expected U fractions by marker and cell type."""

    atlas_id: Identifier
    method: MethodDefinition
    cell_type_ids: tuple[Identifier, ...] = Field(min_length=1)
    rows: tuple[AtlasUMatrixRow, ...] = Field(min_length=1)
    source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_matrix(self) -> AtlasUMatrix:
        if self.method != LOYFER_UXM_METHOD:
            raise ValueError("AtlasUMatrix is specifically a Loyfer UXM U-matrix")
        if len(set(self.cell_type_ids)) != len(self.cell_type_ids):
            raise ValueError("cell_type_ids must be unique")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        marker_ids = [row.marker_id for row in self.rows]
        if len(set(marker_ids)) != len(marker_ids):
            raise ValueError("atlas marker_id values must be unique")
        for row in self.rows:
            if tuple(value.cell_type_id for value in row.values) != self.cell_type_ids:
                raise ValueError(
                    "each atlas row must contain the declared cell types in order"
                )
        return self


class CellOriginInputBundle(StrictModel):
    """Validated local-only inputs for fragment UXM deconvolution."""

    schema_version: Identifier
    method: MethodDefinition
    markers: tuple[GenomicMarker, ...] = Field(min_length=1)
    observations: tuple[FragmentMarkerObservation, ...] = Field(min_length=1)
    atlas_u_matrix: AtlasUMatrix

    @model_validator(mode="after")
    def validate_references(self) -> CellOriginInputBundle:
        if self.method != LOYFER_UXM_METHOD:
            raise ValueError("fragment UXM input bundles require the Loyfer method")
        marker_by_id = {marker.marker_id: marker for marker in self.markers}
        if len(marker_by_id) != len(self.markers):
            raise ValueError("marker IDs must be unique")
        if any(marker.atlas_id != self.atlas_u_matrix.atlas_id for marker in self.markers):
            raise ValueError("every marker must belong to the U-matrix atlas")
        matrix_markers = {row.marker_id for row in self.atlas_u_matrix.rows}
        if matrix_markers != set(marker_by_id):
            raise ValueError("marker registry and U-matrix marker sets must match")
        observed_pairs: set[tuple[str, str]] = set()
        for observation in self.observations:
            registered = marker_by_id.get(observation.marker.marker_id)
            if registered is None or registered != observation.marker:
                raise ValueError("observation references an unknown or changed marker")
            key = (observation.fragment_digest, observation.marker.marker_id)
            if key in observed_pairs:
                raise ValueError("fragment-marker observations must be unique")
            observed_pairs.add(key)
        return self


class CellFractionEstimate(StrictModel):
    cell_type_id: Identifier
    raw_nnls_weight: NonNegativeFinite
    fraction: CanonicalFraction


class NnlsDiagnostics(StrictModel):
    converged: bool
    iterations: int = Field(ge=0)
    residual_l2: NonNegativeFinite
    objective_value: NonNegativeFinite


class NnlsDiagnosticsV2(StrictModel):
    """Version-two diagnostics with an explicit estimator row transform."""

    converged: bool
    iterations: int = Field(ge=0)
    residual_l2: NonNegativeFinite
    objective_value: NonNegativeFinite
    row_scale: NnlsRowScale
    solver_tolerance: float = Field(gt=0.0, allow_inf_nan=False)
    max_iterations: int = Field(ge=1)
    solver_implementation_id: Identifier


class DeconvolutionOutput(StrictModel):
    result_id: Identifier
    method: MethodDefinition
    atlas_id: Identifier
    marker_ids: tuple[Identifier, ...] = Field(min_length=1)
    estimates: tuple[CellFractionEstimate, ...] = Field(min_length=1)
    diagnostics: NnlsDiagnostics

    @model_validator(mode="after")
    def validate_output(self) -> DeconvolutionOutput:
        if len(set(self.marker_ids)) != len(self.marker_ids):
            raise ValueError("marker_ids must be unique")
        cell_types = [estimate.cell_type_id for estimate in self.estimates]
        if len(set(cell_types)) != len(cell_types):
            raise ValueError("cell type estimates must be unique")
        total = sum(estimate.fraction for estimate in self.estimates)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("canonical cell fractions must sum to 1")
        return self


class DeconvolutionOutputV2(DeconvolutionOutput):
    """Additive output boundary that never infers identity for v1 results."""

    schema_version: Literal["cell-origin-deconvolution.v2"]
    atlas_sha256: Sha256
    diagnostics: NnlsDiagnosticsV2


class BootstrapInterval(StrictModel):
    cell_type_id: Identifier
    estimate: CanonicalFraction
    lower_fraction: CanonicalFraction
    upper_fraction: CanonicalFraction

    @model_validator(mode="after")
    def validate_interval(self) -> BootstrapInterval:
        if not self.lower_fraction <= self.estimate <= self.upper_fraction:
            raise ValueError("bootstrap interval must contain its estimate")
        return self


class BootstrapResult(StrictModel):
    source_result_id: Identifier
    replicates: int = Field(ge=2)
    random_seed: int = Field(ge=0)
    confidence_level: CanonicalFraction
    intervals: tuple[BootstrapInterval, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cell_types(self) -> BootstrapResult:
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be strictly between 0 and 1")
        ids = [interval.cell_type_id for interval in self.intervals]
        if len(set(ids)) != len(ids):
            raise ValueError("bootstrap cell_type_id values must be unique")
        return self


class BootstrapInformationStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL_INFORMATION = "partial_information"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class BootstrapIntervalV2(StrictModel):
    """One cell-type interval that never represents zero width as precision."""

    cell_type_id: Identifier
    estimate: CanonicalFraction
    information_status: Literal[
        BootstrapInformationStatus.AVAILABLE,
        BootstrapInformationStatus.INSUFFICIENT_INFORMATION,
    ]
    lower_fraction: CanonicalFraction | None = None
    upper_fraction: CanonicalFraction | None = None

    @model_validator(mode="after")
    def validate_information(self) -> BootstrapIntervalV2:
        if self.information_status == BootstrapInformationStatus.AVAILABLE:
            if self.lower_fraction is None or self.upper_fraction is None:
                raise ValueError("available bootstrap interval requires bounds")
            if self.lower_fraction >= self.upper_fraction:
                raise ValueError(
                    "available bootstrap interval must have positive width"
                )
            if not self.lower_fraction <= self.estimate <= self.upper_fraction:
                raise ValueError("bootstrap interval must contain its estimate")
        elif self.lower_fraction is not None or self.upper_fraction is not None:
            raise ValueError(
                "insufficient-information bootstrap interval cannot claim bounds"
            )
        return self


class BootstrapDiagnosticsV2(StrictModel):
    """Complete resample accounting and estimator identity."""

    schema_version: Literal["cell-origin-bootstrap-diagnostics.v2"] = (
        "cell-origin-bootstrap-diagnostics.v2"
    )
    requested_resamples: int = Field(ge=2)
    successful_resamples: int = Field(ge=0)
    failed_resamples: int = Field(ge=0)
    degenerate_resamples: int = Field(ge=0)
    resampling_unit: Literal["classified_fragment_call_within_marker"] = (
        "classified_fragment_call_within_marker"
    )
    method_id: Literal["independent-marker-binomial-bootstrap.v1"] = (
        "independent-marker-binomial-bootstrap.v1"
    )
    preserves_cross_marker_molecule_linkage: Literal[False] = False
    limitation_id: Literal["cross-marker-molecule-linkage-not-preserved"] = (
        "cross-marker-molecule-linkage-not-preserved"
    )
    minimum_tail_observations: Literal[2] = 2
    tail_probability: float = Field(gt=0.0, lt=0.5, allow_inf_nan=False)
    minimum_successful_resamples: int = Field(ge=2)
    maximum_failed_resample_fraction: Literal[0.0] = 0.0
    observed_failed_resample_fraction: CanonicalFraction
    interval_eligibility_met: bool
    nnls_row_scale: NnlsRowScale
    solver_tolerance: float = Field(gt=0.0, allow_inf_nan=False)
    max_iterations: int = Field(ge=1)
    solver_implementation_id: Identifier

    @model_validator(mode="after")
    def reconcile_resamples(self) -> BootstrapDiagnosticsV2:
        if self.requested_resamples != (
            self.successful_resamples
            + self.failed_resamples
            + self.degenerate_resamples
        ):
            raise ValueError("bootstrap resample accounting must reconcile")
        expected_failed_fraction = (
            self.failed_resamples / self.requested_resamples
        )
        if not math.isclose(
            self.observed_failed_resample_fraction,
            expected_failed_fraction,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ValueError("observed bootstrap failure fraction is inconsistent")
        expected_eligibility = (
            self.successful_resamples >= self.minimum_successful_resamples
            and self.observed_failed_resample_fraction
            <= self.maximum_failed_resample_fraction
        )
        if self.interval_eligibility_met != expected_eligibility:
            raise ValueError("bootstrap interval eligibility is inconsistent")
        return self


class BootstrapResultV2(StrictModel):
    """Additive uncertainty result with explicit information availability."""

    schema_version: Literal["cell-origin-bootstrap.v2"] = (
        "cell-origin-bootstrap.v2"
    )
    source_result_id: Identifier
    replicates: int = Field(ge=2)
    random_seed: int = Field(ge=0)
    confidence_level: CanonicalFraction
    information_status: BootstrapInformationStatus
    intervals: tuple[BootstrapIntervalV2, ...] = Field(min_length=1)
    diagnostics: BootstrapDiagnosticsV2

    @model_validator(mode="after")
    def validate_result(self) -> BootstrapResultV2:
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be strictly between 0 and 1")
        if self.replicates != self.diagnostics.requested_resamples:
            raise ValueError("bootstrap diagnostics must match requested replicates")
        expected_tail_probability = (1.0 - self.confidence_level) / 2.0
        if not math.isclose(
            self.diagnostics.tail_probability,
            expected_tail_probability,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ValueError("bootstrap tail probability is inconsistent")
        expected_minimum = math.ceil(
            self.diagnostics.minimum_tail_observations
            / self.diagnostics.tail_probability
        )
        if self.diagnostics.minimum_successful_resamples != expected_minimum:
            raise ValueError("bootstrap tail-resolution threshold is inconsistent")
        ids = [interval.cell_type_id for interval in self.intervals]
        if len(set(ids)) != len(ids):
            raise ValueError("bootstrap cell_type_id values must be unique")
        available = sum(
            interval.information_status == BootstrapInformationStatus.AVAILABLE
            for interval in self.intervals
        )
        if available and not self.diagnostics.interval_eligibility_met:
            raise ValueError(
                "available intervals require eligible successful resamples"
            )
        expected = (
            BootstrapInformationStatus.INSUFFICIENT_INFORMATION
            if available == 0
            else BootstrapInformationStatus.AVAILABLE
            if available == len(self.intervals)
            else BootstrapInformationStatus.PARTIAL_INFORMATION
        )
        if self.information_status != expected:
            raise ValueError("bootstrap information status does not match intervals")
        return self


class ReferenceRangeRow(StrictModel):
    cell_type_id: Identifier
    fraction: CanonicalFraction
    min_fraction: CanonicalFraction
    max_fraction: CanonicalFraction
    classification: RangeClassification
    source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> ReferenceRangeRow:
        if self.min_fraction > self.max_fraction:
            raise ValueError("reference range minimum cannot exceed maximum")
        expected = (
            RangeClassification.BELOW
            if self.fraction < self.min_fraction
            else RangeClassification.ABOVE
            if self.fraction > self.max_fraction
            else RangeClassification.WITHIN
        )
        if self.classification != expected:
            raise ValueError("classification must use inclusive range bounds")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        return self


class RangeComparison(StrictModel):
    reference_kind: Literal[ReferenceRangeKind.OBSERVED_COHORT_RANGE] = (
        ReferenceRangeKind.OBSERVED_COHORT_RANGE
    )
    rows: tuple[ReferenceRangeRow, ...] = Field(min_length=1)
    partial_table: bool

    @model_validator(mode="after")
    def unique_cell_types(self) -> RangeComparison:
        ids = [row.cell_type_id for row in self.rows]
        if len(set(ids)) != len(ids):
            raise ValueError("range comparison cell_type_id values must be unique")
        return self


class DigestArtifact(StrictModel):
    artifact_id: Identifier
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class SoftwareVersion(StrictModel):
    software_id: Identifier
    version: Version


class CellOriginProvenance(StrictModel):
    """Model-safe aggregate provenance with no local path fields."""

    schema_version: Identifier
    input_artifacts: tuple[DigestArtifact, ...] = Field(min_length=1)
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    software_versions: tuple[SoftwareVersion, ...] = Field(min_length=1)
    method: MethodDefinition
    uxm_thresholds: UxmThresholds
    input_fragment_count: int = Field(ge=0)
    marker_overlap_count: int = Field(ge=0)
    classified_fragment_marker_count: int = Field(ge=0)
    excluded_fewer_than_four_cpgs: int = Field(ge=0)
    partial_input: bool
    verification_level: VerificationLevel

    @model_validator(mode="after")
    def validate_provenance(self) -> CellOriginProvenance:
        artifact_ids = [item.artifact_id for item in self.input_artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("input artifact IDs must be unique")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        software_ids = [item.software_id for item in self.software_versions]
        if len(set(software_ids)) != len(software_ids):
            raise ValueError("software IDs must be unique")
        if self.classified_fragment_marker_count > self.marker_overlap_count:
            raise ValueError("classified count cannot exceed marker overlap count")
        if (
            self.classified_fragment_marker_count
            + self.excluded_fewer_than_four_cpgs
            > self.marker_overlap_count
        ):
            raise ValueError("classified and excluded counts exceed marker overlaps")
        return self


class ValidationCheck(StrEnum):
    STRICT_SCHEMA = "strict_schema"
    DIGESTS_VERIFIED = "digests_verified"
    MARKERS_MATCH_ATLAS = "markers_match_atlas"
    UXM_COUNTS_RECONCILED = "uxm_counts_reconciled"
    FRACTIONS_NORMALIZED = "fractions_normalized"
    MODEL_SAFE = "model_safe"


class ValidationRecord(StrictModel):
    check: ValidationCheck
    passed: bool


class ValidationReport(StrictModel):
    records: tuple[ValidationRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_records(self) -> ValidationReport:
        checks = [record.check for record in self.records]
        if len(set(checks)) != len(checks):
            raise ValueError("validation checks must be unique")
        return self

    @property
    def passed(self) -> bool:
        return all(record.passed for record in self.records)


class CellOriginResult(StrictModel):
    """Aggregate publication contract safe to include in a model request."""

    result_id: Identifier
    method: MethodDefinition
    marker_counts: tuple[MarkerCountRow, ...] = Field(min_length=1)
    deconvolution: DeconvolutionOutput
    bootstrap: BootstrapResult | BootstrapResultV2 | None = None
    range_comparison: RangeComparison | None = None
    provenance: CellOriginProvenance
    validation: ValidationReport

    @model_validator(mode="after")
    def validate_result(self) -> CellOriginResult:
        if self.method != LOYFER_UXM_METHOD:
            raise ValueError("cell-origin UXM results require the Loyfer method")
        if self.deconvolution.method != self.method:
            raise ValueError("deconvolution method does not match result method")
        if self.provenance.method != self.method:
            raise ValueError("provenance method does not match result method")
        marker_ids = [row.marker_id for row in self.marker_counts]
        if len(set(marker_ids)) != len(marker_ids):
            raise ValueError("marker count rows must be unique")
        if tuple(marker_ids) != self.deconvolution.marker_ids:
            raise ValueError(
                "marker counts and deconvolution marker order must match"
            )
        estimate_ids = {
            estimate.cell_type_id for estimate in self.deconvolution.estimates
        }
        if self.bootstrap is not None:
            if self.bootstrap.source_result_id != self.deconvolution.result_id:
                raise ValueError("bootstrap must bind to this deconvolution result")
            if {item.cell_type_id for item in self.bootstrap.intervals} != estimate_ids:
                raise ValueError("bootstrap and deconvolution cell types must match")
        if self.range_comparison is not None:
            if {
                item.cell_type_id for item in self.range_comparison.rows
            } != estimate_ids:
                raise ValueError(
                    "range comparison and deconvolution cell types must match"
                )
        if not self.validation.passed:
            raise ValueError("an invalid result cannot cross the publication boundary")
        return self


__all__ = [
    "AtlasKind",
    "AtlasUMatrix",
    "AtlasUMatrixRow",
    "AtlasUValue",
    "BootstrapInterval",
    "BootstrapIntervalV2",
    "BootstrapDiagnosticsV2",
    "BootstrapInformationStatus",
    "BootstrapResult",
    "BootstrapResultV2",
    "CanonicalFraction",
    "CellFractionEstimate",
    "CellOriginInputBundle",
    "CellOriginMethodFamily",
    "CellOriginProvenance",
    "CellOriginResult",
    "CpgCallState",
    "DeconvolutionOutput",
    "DeconvolutionOutputV2",
    "DigestArtifact",
    "FeatureDefinition",
    "FragmentMarkerObservation",
    "GenomicMarker",
    "KATSMAN_METHATLAS_METHOD",
    "LOYFER_UXM_METHOD",
    "MarkerCountRow",
    "MethodDefinition",
    "ModProbabilityPolicy",
    "ModkitCpgCall",
    "ModkitCpgCallV2",
    "ModkitIngestionLedgerV2",
    "ModkitInputProvenanceV2",
    "ModkitInputResultV2",
    "ModkitSourceSchema",
    "NnlsDiagnostics",
    "NnlsDiagnosticsV2",
    "NnlsRowScale",
    "ObservationUnit",
    "RangeClassification",
    "RangeComparison",
    "ReferenceContextValidationScope",
    "ReferenceRangeKind",
    "ReferenceRangeRow",
    "SoftwareVersion",
    "SolverKind",
    "Strand",
    "UXM_METHYLATED_MIN_INCLUSIVE",
    "UXM_MINIMUM_CPGS",
    "UXM_UNMETHYLATED_MAX_EXCLUSIVE",
    "UxmState",
    "UxmThresholds",
    "ValidationCheck",
    "ValidationRecord",
    "ValidationReport",
    "VerificationLevel",
    "classify_uxm",
]
