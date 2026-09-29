"""Deterministic, synthetic-only layer contracts for a CNA explorer.

The explorer composes the whole-chromosome dosage-QC and segmented-CNA
contracts without treating them as the same measurement.  It performs no
native execution, network access, clinical interpretation, or inference.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from traceback_runner.serialization import canonical_json_bytes

from .copy_number_qc import (
    AUTOSOMES,
    DosageQcAnalysisResult,
    DosageQcInsufficientResultBundle,
)
from .ichor_adapter import CnvDevelopmentResult

MAX_BINS = 1_000_000
MAX_SEGMENTS = 100_000
MAX_CANDIDATES = 10_000
MAX_ASSETS = 10_000
MAX_REASONS = 128

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
UnavailabilityReason = Annotated[
    str,
    StringConstraints(
        max_length=96,
        pattern=(
            r"^(dosage_qc|segmented_cna):(?:"
            r"execution_(?:incomplete|unknown)|"
            r"trust_(?:revoked|unknown)|"
            r"qualification_(?:development_unqualified|unknown)|"
            r"research_inspection_not_authorized|"
            r"upstream_insufficient_information)$"
        ),
    ),
]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
IchorCall = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:HOMD|HETD|NEUT|GAIN|AMP|HLAMP(?:[2-9]|1[0-9]|2[0-5])?)$"
    ),
]

_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s=:(\[{'\"\\])" r"(?:/[^\s,;)\]}'\"]+|[A-Za-z]:[\\/][^\s,;)\]}'\"]+)"
)
_SEQUENCE = re.compile(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", re.IGNORECASE)
_SECRET = re.compile(
    r"(?:AWS_SECRET_ACCESS_KEY|PRIVATE_KEY|PASSWORD|SECRET|TOKEN)\s*=",
    re.IGNORECASE,
)
_RAW_IDENTIFIER = re.compile(
    r"\b(?:donor|patient|read|sample|query)[_-]?id\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "donor_id",
        "patient_id",
        "read_id",
        "read_ids",
        "query_name",
        "sample_id",
        "sequence",
        "local_path",
        "path",
        "secret",
    }
)
_LIMITATIONS = (
    "Dosage QC and segmented CNA are distinct measurements and are never merged.",
    "Missing values and unavailable inputs are never displayed as zero.",
    "Candidate and model values are upstream development outputs, not tumor estimates.",
    "No diagnostic, clinical, or product-release interpretation is authorized.",
)


class CnaExplorerError(ValueError):
    """Explorer input or replay does not satisfy the closed contract."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
        strict=True,
    )


class CnaSource(StrEnum):
    DOSAGE_QC = "dosage_qc"
    SEGMENTED_CNA = "segmented_cna"


class ExecutionState(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class TrustState(StrEnum):
    VERIFIED = "verified"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class QualificationState(StrEnum):
    QUALIFIED = "qualified"
    DEVELOPMENT_UNQUALIFIED = "development_unqualified"
    UNKNOWN = "unknown"


class ExplorerAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ExplorerInputAuthority(_ClosedModel):
    schema_version: Literal["traceback.cna-explorer-input-authority.v1"] = (
        "traceback.cna-explorer-input-authority.v1"
    )
    source: CnaSource
    execution_state: ExecutionState
    trust_state: TrustState
    qualification_state: QualificationState
    research_inspectable: bool

    @property
    def eligible(self) -> bool:
        return (
            self.execution_state == ExecutionState.COMPLETE
            and self.trust_state == TrustState.VERIFIED
            and self.qualification_state == QualificationState.QUALIFIED
            and self.research_inspectable
        )


class ExplorerInputBinding(_ClosedModel):
    source: CnaSource
    result_schema_version: Identifier
    result_sha256: Sha256
    authority_sha256: Sha256


class CoordinateGridLayer(_ClosedModel):
    source: CnaSource
    coordinate_system: Literal["zero_based_half_open"] = "zero_based_half_open"
    contig_order: tuple[Identifier, ...] = Field(min_length=1, max_length=128)
    bin_definition_sha256: Sha256
    bin_count: int = Field(gt=0, le=MAX_BINS)

    @model_validator(mode="after")
    def unique_contigs(self) -> CoordinateGridLayer:
        if len(self.contig_order) != len(set(self.contig_order)):
            raise ValueError("coordinate-grid contigs must be unique")
        return self


class AssetLayer(_ClosedModel):
    source: CnaSource
    role: Identifier
    content_sha256: Sha256


class MethodLayer(_ClosedModel):
    source: CnaSource
    method_id: Literal[
        "sample-internal-whole-chromosome-dosage-qc",
        "ichor-development-adapter",
    ]
    result_schema_version: Literal[
        "copy-number-dosage-qc.v2",
        "traceback.ichor-development-result.v1",
    ]
    embedded_qualification_status: Literal["development_unqualified"] = (
        "development_unqualified"
    )
    authority_execution_state: ExecutionState
    authority_trust_state: TrustState
    authority_qualification_state: QualificationState
    research_inspectable: bool
    product_release_authorized: Literal[False] = False
    diagnostic_interpretation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def source_specific_identity(self) -> MethodLayer:
        expected = {
            CnaSource.DOSAGE_QC: (
                "sample-internal-whole-chromosome-dosage-qc",
                "copy-number-dosage-qc.v2",
            ),
            CnaSource.SEGMENTED_CNA: (
                "ichor-development-adapter",
                "traceback.ichor-development-result.v1",
            ),
        }[self.source]
        if (self.method_id, self.result_schema_version) != expected:
            raise ValueError("method identity does not match its source")
        return self


class BinLayer(_ClosedModel):
    source: CnaSource
    bin_index: int = Field(ge=0, lt=MAX_BINS)
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    status: Literal[
        "included",
        "excluded_terminal_partial_bin",
        "retained",
        "masked_prespecified",
    ]
    accepted_read_start_count: int | None = Field(default=None, ge=0)
    corrected_log2: FiniteFloat | None = None

    @model_validator(mode="after")
    def source_specific_values(self) -> BinLayer:
        if self.end <= self.start:
            raise ValueError("bin end must exceed start")
        dosage = self.source == CnaSource.DOSAGE_QC
        dosage_status = self.status in {"included", "excluded_terminal_partial_bin"}
        if dosage != dosage_status:
            raise ValueError("bin status does not match its source")
        if dosage != (self.accepted_read_start_count is not None):
            raise ValueError("only dosage bins carry accepted-read counts")
        if dosage and self.corrected_log2 is not None:
            raise ValueError("dosage bins cannot carry corrected-depth values")
        if (
            not dosage
            and self.status == "masked_prespecified"
            and self.corrected_log2 is not None
        ):
            raise ValueError("masked segmented bins cannot imply zero or a value")
        return self


class CorrectedDepthLayer(_ClosedModel):
    source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    bin_index: int = Field(ge=0, lt=MAX_BINS)
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    corrected_log2: FiniteFloat | None
    value_state: Literal["observed", "native_missing"]

    @model_validator(mode="after")
    def preserve_missingness(self) -> CorrectedDepthLayer:
        if self.end <= self.start:
            raise ValueError("corrected-depth end must exceed start")
        expected = "observed" if self.corrected_log2 is not None else "native_missing"
        if self.value_state != expected:
            raise ValueError("corrected-depth missingness cannot be rewritten as zero")
        return self


class MaskLayer(_ClosedModel):
    source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    bin_index: int = Field(ge=0, lt=MAX_BINS)
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    reason: Literal["centromere_or_flank", "low_mappability"]
    source_artifact_sha256: Sha256
    source_value: FiniteFloat | None = None


class DosageChromosomeLayer(_ClosedModel):
    source: Literal[CnaSource.DOSAGE_QC] = CnaSource.DOSAGE_QC
    chromosome: Annotated[
        str, StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2])$")
    ]
    ordinal: int = Field(ge=1, le=22)
    accepted_read_count: int = Field(ge=0)
    relative_diploid_dosage: FiniteFloat = Field(ge=0)
    log2_ratio: FiniteFloat
    dosage_direction: Literal[
        "within_visualization_boundary",
        "higher_relative_dosage",
        "lower_relative_dosage",
    ]


class SegmentLayer(_ClosedModel):
    source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    segment_index: int = Field(ge=0, lt=MAX_SEGMENTS)
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    native_span_bin_count: int = Field(gt=0)
    retained_bin_count: int = Field(gt=0)
    median_log2: FiniteFloat
    upstream_copy_number: int = Field(ge=0)
    upstream_call: IchorCall
    subclone_status: bool


class CandidateLayer(_ClosedModel):
    source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    candidate_index: int = Field(ge=0, lt=MAX_CANDIDATES)
    candidate_id: Identifier
    selected: bool
    initial_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    initial_ploidy: FiniteFloat = Field(gt=0)
    estimated_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    upstream_model_fraction: FiniteFloat = Field(ge=0, le=1)
    estimated_ploidy: FiniteFloat = Field(gt=0)
    bic: None = None
    fraction_genome_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    fraction_cna_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    log_likelihood: FiniteFloat


class InsufficiencyLayer(_ClosedModel):
    source: CnaSource
    upstream_status: Literal["complete", "insufficient_information"]
    reasons: tuple[BoundedText, ...] = Field(max_length=MAX_REASONS)
    missing_values_are_zero: Literal[False] = False
    tumor_or_clinical_interpretation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def exact_reason_presence(self) -> InsufficiencyLayer:
        if (self.upstream_status == "complete") == bool(self.reasons):
            raise ValueError("insufficiency reasons must match upstream status")
        return self


class ExplorerLayers(_ClosedModel):
    coordinate_grids: tuple[CoordinateGridLayer, ...] = Field(
        min_length=2, max_length=2
    )
    assets: tuple[AssetLayer, ...] = Field(min_length=1, max_length=MAX_ASSETS)
    methods: tuple[MethodLayer, ...] = Field(min_length=2, max_length=2)
    bins: tuple[BinLayer, ...] = Field(min_length=1, max_length=MAX_BINS)
    corrected_depth: tuple[CorrectedDepthLayer, ...] = Field(max_length=MAX_BINS)
    masks: tuple[MaskLayer, ...] = Field(max_length=MAX_BINS)
    dosage_chromosomes: tuple[DosageChromosomeLayer, ...] = Field(
        min_length=22, max_length=22
    )
    segments: tuple[SegmentLayer, ...] = Field(max_length=MAX_SEGMENTS)
    candidates: tuple[CandidateLayer, ...] = Field(max_length=MAX_CANDIDATES)
    insufficiency: tuple[InsufficiencyLayer, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def reconcile_layers(self) -> ExplorerLayers:
        expected_sources = (CnaSource.DOSAGE_QC, CnaSource.SEGMENTED_CNA)
        if tuple(item.source for item in self.coordinate_grids) != expected_sources:
            raise ValueError("coordinate grids must preserve source order")
        if tuple(item.source for item in self.methods) != expected_sources:
            raise ValueError("methods must preserve source distinction")
        if tuple(item.source for item in self.insufficiency) != expected_sources:
            raise ValueError("insufficiency layers must preserve source distinction")
        keys = [(item.source, item.role, item.content_sha256) for item in self.assets]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("assets must be uniquely sorted")
        for source in expected_sources:
            source_bins = [item for item in self.bins if item.source == source]
            indexes = [item.bin_index for item in source_bins]
            if indexes != list(range(len(indexes))):
                raise ValueError("bin indexes must be contiguous within each source")
            grid = next(item for item in self.coordinate_grids if item.source == source)
            if len(source_bins) != grid.bin_count:
                raise ValueError("grid bin count disagrees with bin layer")
            order = {contig: index for index, contig in enumerate(grid.contig_order)}
            keys = [(item.contig, item.start, item.end) for item in source_bins]
            if any(item[0] not in order for item in keys):
                raise ValueError("bin layer references an undeclared contig")
            if keys != sorted(
                keys, key=lambda item: (order[item[0]], item[1], item[2])
            ):
                raise ValueError("bin layer does not follow its coordinate grid")
            if len(keys) != len(set(keys)):
                raise ValueError("bin coordinates must be unique within each source")
        expected_chromosomes = tuple(f"chr{index}" for index in range(1, 23))
        if (
            tuple(item.chromosome for item in self.dosage_chromosomes)
            != expected_chromosomes
        ):
            raise ValueError("dosage chromosome layer must contain chr1 through chr22")
        if tuple(item.ordinal for item in self.dosage_chromosomes) != tuple(
            range(1, 23)
        ):
            raise ValueError("dosage chromosome ordinals are not canonical")
        segmented = {
            (item.contig, item.start, item.end): item
            for item in self.bins
            if item.source == CnaSource.SEGMENTED_CNA
        }
        corrected = {
            (item.contig, item.start, item.end): item for item in self.corrected_depth
        }
        expected_corrected = {
            key: item for key, item in segmented.items() if item.status == "retained"
        }
        if set(corrected) != set(expected_corrected):
            raise ValueError("corrected-depth layer disagrees with retained bins")
        for key, value in corrected.items():
            if value.corrected_log2 != expected_corrected[key].corrected_log2:
                raise ValueError("corrected-depth values disagree with bin layer")
        masked = {
            (item.contig, item.start, item.end)
            for item in self.bins
            if item.source == CnaSource.SEGMENTED_CNA
            and item.status == "masked_prespecified"
        }
        if {(item.contig, item.start, item.end) for item in self.masks} != masked:
            raise ValueError("mask layer disagrees with segmented bin layer")
        if [item.segment_index for item in self.segments] != list(
            range(len(self.segments))
        ):
            raise ValueError("segment indexes must be contiguous")
        if [item.candidate_index for item in self.candidates] != list(
            range(len(self.candidates))
        ):
            raise ValueError("candidate indexes must be contiguous")
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        if sum(item.selected for item in self.candidates) > 1:
            raise ValueError("at most one candidate may be selected")
        return self


class ExplorerChart(_ClosedModel):
    schema_version: Literal["traceback.cna-explorer-chart.v1"] = (
        "traceback.cna-explorer-chart.v1"
    )
    dosage_chromosomes: tuple[DosageChromosomeLayer, ...] = Field(max_length=22)
    corrected_depth: tuple[CorrectedDepthLayer, ...] = Field(max_length=MAX_BINS)
    segments: tuple[SegmentLayer, ...] = Field(max_length=MAX_SEGMENTS)
    clinical_thresholds_present: Literal[False] = False


class ExplorerTables(_ClosedModel):
    schema_version: Literal["traceback.cna-explorer-tables.v1"] = (
        "traceback.cna-explorer-tables.v1"
    )
    bins: tuple[BinLayer, ...] = Field(max_length=MAX_BINS)
    masks: tuple[MaskLayer, ...] = Field(max_length=MAX_BINS)
    segments: tuple[SegmentLayer, ...] = Field(max_length=MAX_SEGMENTS)
    candidates: tuple[CandidateLayer, ...] = Field(max_length=MAX_CANDIDATES)
    insufficiency: tuple[InsufficiencyLayer, ...] = Field(max_length=2)


class ExplorerProvenance(_ClosedModel):
    generator_id: Literal["traceback.cna-explorer-contract.v1"] = (
        "traceback.cna-explorer-contract.v1"
    )
    input_bindings: tuple[ExplorerInputBinding, ...] = Field(min_length=2, max_length=2)
    synthetic_only: Literal[True] = True
    native_execution_performed: Literal[False] = False
    network_access_performed: Literal[False] = False
    product_release_authorized: Literal[False] = False
    diagnostic_interpretation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def exact_sources(self) -> ExplorerProvenance:
        if tuple(item.source for item in self.input_bindings) != (
            CnaSource.DOSAGE_QC,
            CnaSource.SEGMENTED_CNA,
        ):
            raise ValueError("input bindings must preserve source distinction")
        expected_schemas = (
            "copy-number-dosage-qc.v2",
            "traceback.ichor-development-result.v1",
        )
        if tuple(item.result_schema_version for item in self.input_bindings) != (
            expected_schemas
        ):
            raise ValueError("input schema does not match its source")
        return self


class CnaExplorerSnapshot(_ClosedModel):
    schema_version: Literal["traceback.cna-explorer-snapshot.v1"] = (
        "traceback.cna-explorer-snapshot.v1"
    )
    availability: ExplorerAvailability
    unavailable_reasons: tuple[UnavailabilityReason, ...] = Field(
        max_length=MAX_REASONS
    )
    provenance: ExplorerProvenance
    methods: tuple[MethodLayer, ...] = Field(min_length=2, max_length=2)
    layers: ExplorerLayers | None
    chart: ExplorerChart
    tables: ExplorerTables
    limitations: tuple[
        Literal[
            "Dosage QC and segmented CNA are distinct measurements and are never merged.",
            "Missing values and unavailable inputs are never displayed as zero.",
            "Candidate and model values are upstream development outputs, not tumor estimates.",
            "No diagnostic, clinical, or product-release interpretation is authorized.",
        ],
        ...,
    ] = _LIMITATIONS

    @model_validator(mode="after")
    def reconcile_presentation(self) -> CnaExplorerSnapshot:
        if self.availability == ExplorerAvailability.UNAVAILABLE:
            if not self.unavailable_reasons or self.layers is not None:
                raise ValueError("unavailable explorer requires reasons and no layers")
            if any(
                (
                    self.chart.dosage_chromosomes,
                    self.chart.corrected_depth,
                    self.chart.segments,
                    self.tables.bins,
                    self.tables.masks,
                    self.tables.segments,
                    self.tables.candidates,
                    self.tables.insufficiency,
                )
            ):
                raise ValueError(
                    "unavailable explorer cannot expose chart or table values"
                )
        else:
            if self.unavailable_reasons or self.layers is None:
                raise ValueError(
                    "available explorer requires exact layers and no blockers"
                )
            if self.methods != self.layers.methods:
                raise ValueError("snapshot methods disagree with method layer")
            if self.chart.dosage_chromosomes != self.layers.dosage_chromosomes:
                raise ValueError("dosage chart does not replay from layers")
            if self.chart.corrected_depth != self.layers.corrected_depth:
                raise ValueError("corrected-depth chart does not replay from layers")
            if self.chart.segments != self.layers.segments:
                raise ValueError("segment chart does not replay from layers")
            if self.tables.bins != self.layers.bins:
                raise ValueError("bin table does not replay from layers")
            if self.tables.masks != self.layers.masks:
                raise ValueError("mask table does not replay from layers")
            if self.tables.segments != self.layers.segments:
                raise ValueError("segment table does not replay from layers")
            if self.tables.candidates != self.layers.candidates:
                raise ValueError("candidate table does not replay from layers")
            if self.tables.insufficiency != self.layers.insufficiency:
                raise ValueError("insufficiency table does not replay from layers")
            if any(
                not (
                    item.authority_execution_state == ExecutionState.COMPLETE
                    and item.authority_trust_state == TrustState.VERIFIED
                    and item.authority_qualification_state
                    == QualificationState.QUALIFIED
                    and item.research_inspectable
                )
                for item in self.methods
            ):
                raise ValueError("available explorer has ineligible method authority")
        if self.limitations != _LIMITATIONS:
            raise ValueError("explorer limitations are not the exact allowlist")
        _privacy_check(self.model_dump(mode="json"))
        return self


def _privacy_check(value: Any, *, field_name: str = "") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in _FORBIDDEN_FIELDS:
                raise ValueError(f"privacy-forbidden field: {key}")
            _privacy_check(nested, field_name=str(key))
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _privacy_check(nested, field_name=field_name)
    elif isinstance(value, str):
        digest_field = field_name.endswith(("sha256", "md5"))
        if _ABSOLUTE_PATH.search(value):
            raise ValueError(f"absolute local path forbidden in {field_name}")
        if _SECRET.search(value):
            raise ValueError(f"secret-like text forbidden in {field_name}")
        if _RAW_IDENTIFIER.search(value):
            raise ValueError(f"raw identifier forbidden in {field_name}")
        if not digest_field and _SEQUENCE.search(value):
            raise ValueError(f"sequence-like text forbidden in {field_name}")


def _sha256(value: BaseModel) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _method_layer(source: CnaSource, authority: ExplorerInputAuthority) -> MethodLayer:
    if authority.source != source:
        raise CnaExplorerError("authority source does not match result source")
    if source == CnaSource.DOSAGE_QC:
        method_id = "sample-internal-whole-chromosome-dosage-qc"
        schema = "copy-number-dosage-qc.v2"
    else:
        method_id = "ichor-development-adapter"
        schema = "traceback.ichor-development-result.v1"
    return MethodLayer(
        source=source,
        method_id=method_id,
        result_schema_version=schema,
        authority_execution_state=authority.execution_state,
        authority_trust_state=authority.trust_state,
        authority_qualification_state=authority.qualification_state,
        research_inspectable=authority.research_inspectable,
    )


def _blockers(
    dosage: DosageQcAnalysisResult,
    segmented: CnvDevelopmentResult,
    authorities: tuple[ExplorerInputAuthority, ExplorerInputAuthority],
) -> tuple[str, ...]:
    reasons: list[str] = []
    for authority in authorities:
        if authority.execution_state != ExecutionState.COMPLETE:
            reasons.append(
                f"{authority.source.value}:execution_{authority.execution_state.value}"
            )
        if authority.trust_state != TrustState.VERIFIED:
            reasons.append(
                f"{authority.source.value}:trust_{authority.trust_state.value}"
            )
        if authority.qualification_state != QualificationState.QUALIFIED:
            reasons.append(
                f"{authority.source.value}:qualification_{authority.qualification_state.value}"
            )
        if not authority.research_inspectable:
            reasons.append(
                f"{authority.source.value}:research_inspection_not_authorized"
            )
    if dosage.analysis_status != "complete":
        reasons.append("dosage_qc:upstream_insufficient_information")
    if segmented.status != "complete":
        reasons.append("segmented_cna:upstream_insufficient_information")
    return tuple(reasons)


def _dosage_assets(result: DosageQcAnalysisResult) -> tuple[AssetLayer, ...]:
    return (
        AssetLayer(
            source=CnaSource.DOSAGE_QC,
            role="bin_definition",
            content_sha256=result.provenance.reference.bin_definition_sha256,
        ),
        AssetLayer(
            source=CnaSource.DOSAGE_QC,
            role="input_artifact",
            content_sha256=result.provenance.input_artifact_sha256,
        ),
        AssetLayer(
            source=CnaSource.DOSAGE_QC,
            role="reference_fasta",
            content_sha256=result.provenance.reference.fasta_sha256,
        ),
    )


def _segmented_assets(result: CnvDevelopmentResult) -> tuple[AssetLayer, ...]:
    rows = [
        AssetLayer(
            source=CnaSource.SEGMENTED_CNA,
            role="canonical_grid",
            content_sha256=result.canonical_grid.bin_definition_sha256,
        ),
        AssetLayer(
            source=CnaSource.SEGMENTED_CNA,
            role="request",
            content_sha256=result.request_sha256,
        ),
    ]
    rows.extend(
        AssetLayer(
            source=CnaSource.SEGMENTED_CNA,
            role=f"output.{item.role}",
            content_sha256=item.content_sha256,
        )
        for item in result.artifacts
    )
    rows.extend(
        AssetLayer(
            source=CnaSource.SEGMENTED_CNA,
            role=f"mask_source.{index}",
            content_sha256=item.source_artifact_sha256,
        )
        for index, item in enumerate(result.canonical_grid.masks)
    )
    return tuple(rows)


def _derive_layers(
    dosage: DosageQcAnalysisResult,
    segmented: CnvDevelopmentResult,
    methods: tuple[MethodLayer, MethodLayer],
) -> ExplorerLayers:
    dosage_contigs = tuple(
        name
        for name in (item.name for item in dosage.provenance.reference.contigs)
        if name in AUTOSOMES
    )
    grids = (
        CoordinateGridLayer(
            source=CnaSource.DOSAGE_QC,
            contig_order=dosage_contigs,
            bin_definition_sha256=dosage.provenance.reference.bin_definition_sha256,
            bin_count=len(dosage.bins),
        ),
        CoordinateGridLayer(
            source=CnaSource.SEGMENTED_CNA,
            contig_order=segmented.canonical_grid.contig_order,
            bin_definition_sha256=segmented.canonical_grid.bin_definition_sha256,
            bin_count=len(segmented.canonical_grid.bins),
        ),
    )
    dosage_bins = tuple(
        BinLayer(
            source=CnaSource.DOSAGE_QC,
            bin_index=index,
            contig=item.contig,
            start=item.start,
            end=item.end,
            status=(
                "included"
                if item.included_in_screen
                else "excluded_terminal_partial_bin"
            ),
            accepted_read_start_count=item.accepted_read_start_count,
        )
        for index, item in enumerate(dosage.bins)
    )
    segmented_bins = tuple(
        BinLayer(
            source=CnaSource.SEGMENTED_CNA,
            bin_index=index,
            contig=item.contig,
            start=item.start,
            end=item.end,
            status=item.status,
            corrected_log2=item.corrected_log2,
        )
        for index, item in enumerate(segmented.bin_statuses)
    )
    index_by_key = {
        (item.contig, item.start, item.end): index
        for index, item in enumerate(segmented.bin_statuses)
    }
    corrected = tuple(
        CorrectedDepthLayer(
            bin_index=index_by_key[(item.contig, item.start, item.end)],
            contig=item.contig,
            start=item.start,
            end=item.end,
            corrected_log2=item.corrected_log2,
            value_state=(
                "observed" if item.corrected_log2 is not None else "native_missing"
            ),
        )
        for item in segmented.corrected_bins
    )
    masks = tuple(
        MaskLayer(
            bin_index=index_by_key[(item.bin.contig, item.bin.start, item.bin.end)],
            contig=item.bin.contig,
            start=item.bin.start,
            end=item.bin.end,
            reason=item.reason,
            source_artifact_sha256=item.source_artifact_sha256,
            source_value=item.source_value,
        )
        for item in segmented.canonical_grid.masks
    )
    dosage_chromosomes = tuple(
        DosageChromosomeLayer(
            chromosome=item.chromosome,
            ordinal=item.ordinal,
            accepted_read_count=item.accepted_read_count,
            relative_diploid_dosage=item.relative_diploid_dosage,
            log2_ratio=item.log2_ratio,
            dosage_direction=item.dosage_direction,
        )
        for item in dosage.chromosomes
    )
    segments = tuple(
        SegmentLayer(
            segment_index=index,
            contig=item.contig,
            start=item.start,
            end=item.end,
            native_span_bin_count=item.native_span_bin_count,
            retained_bin_count=item.retained_bin_count,
            median_log2=item.median_log2,
            upstream_copy_number=item.copy_number,
            upstream_call=item.call,
            subclone_status=item.subclone_status,
        )
        for index, item in enumerate(segmented.segments)
    )
    candidates = tuple(
        CandidateLayer(
            candidate_index=index,
            candidate_id=item.candidate_id,
            selected=item.candidate_id
            == segmented.selected_solution.matched_candidate_id,
            initial_normal_fraction=item.initial_normal_fraction,
            initial_ploidy=item.initial_ploidy,
            estimated_normal_fraction=item.estimated_normal_fraction,
            upstream_model_fraction=item.model_fraction,
            estimated_ploidy=item.estimated_ploidy,
            bic=item.bic,
            fraction_genome_subclonal=item.fraction_genome_subclonal,
            fraction_cna_subclonal=item.fraction_cna_subclonal,
            log_likelihood=item.log_likelihood,
        )
        for index, item in enumerate(segmented.candidates)
    )
    dosage_reasons = (
        dosage.reasons if isinstance(dosage, DosageQcInsufficientResultBundle) else ()
    )
    segmented_reasons = (
        (segmented.identifiability,)
        if segmented.status == "insufficient_information"
        else ()
    )
    insufficiency = (
        InsufficiencyLayer(
            source=CnaSource.DOSAGE_QC,
            upstream_status=dosage.analysis_status,
            reasons=dosage_reasons,
        ),
        InsufficiencyLayer(
            source=CnaSource.SEGMENTED_CNA,
            upstream_status=segmented.status,
            reasons=segmented_reasons,
        ),
    )
    assets = tuple(
        sorted(
            (*_dosage_assets(dosage), *_segmented_assets(segmented)),
            key=lambda item: (item.source, item.role, item.content_sha256),
        )
    )
    return ExplorerLayers(
        coordinate_grids=grids,
        assets=assets,
        methods=methods,
        bins=(*dosage_bins, *segmented_bins),
        corrected_depth=corrected,
        masks=masks,
        dosage_chromosomes=dosage_chromosomes,
        segments=segments,
        candidates=candidates,
        insufficiency=insufficiency,
    )


def _empty_chart() -> ExplorerChart:
    return ExplorerChart(dosage_chromosomes=(), corrected_depth=(), segments=())


def _empty_tables() -> ExplorerTables:
    return ExplorerTables(
        bins=(), masks=(), segments=(), candidates=(), insufficiency=()
    )


def build_cna_explorer_snapshot(
    dosage: DosageQcAnalysisResult,
    segmented: CnvDevelopmentResult,
    *,
    dosage_authority: ExplorerInputAuthority,
    segmented_authority: ExplorerInputAuthority,
) -> CnaExplorerSnapshot:
    """Derive a deterministic explorer snapshot from validated upstream models."""

    if dosage_authority.source != CnaSource.DOSAGE_QC:
        raise CnaExplorerError("dosage authority has the wrong source")
    if segmented_authority.source != CnaSource.SEGMENTED_CNA:
        raise CnaExplorerError("segmented authority has the wrong source")
    authorities = (dosage_authority, segmented_authority)
    methods = (
        _method_layer(CnaSource.DOSAGE_QC, dosage_authority),
        _method_layer(CnaSource.SEGMENTED_CNA, segmented_authority),
    )
    provenance = ExplorerProvenance(
        input_bindings=(
            ExplorerInputBinding(
                source=CnaSource.DOSAGE_QC,
                result_schema_version=dosage.schema_version,
                result_sha256=_sha256(dosage),
                authority_sha256=_sha256(dosage_authority),
            ),
            ExplorerInputBinding(
                source=CnaSource.SEGMENTED_CNA,
                result_schema_version=segmented.schema_version,
                result_sha256=_sha256(segmented),
                authority_sha256=_sha256(segmented_authority),
            ),
        )
    )
    blockers = _blockers(dosage, segmented, authorities)
    if blockers:
        return CnaExplorerSnapshot(
            availability=ExplorerAvailability.UNAVAILABLE,
            unavailable_reasons=blockers,
            provenance=provenance,
            methods=methods,
            layers=None,
            chart=_empty_chart(),
            tables=_empty_tables(),
        )
    layers = _derive_layers(dosage, segmented, methods)
    return CnaExplorerSnapshot(
        availability=ExplorerAvailability.AVAILABLE,
        unavailable_reasons=(),
        provenance=provenance,
        methods=methods,
        layers=layers,
        chart=ExplorerChart(
            dosage_chromosomes=layers.dosage_chromosomes,
            corrected_depth=layers.corrected_depth,
            segments=layers.segments,
        ),
        tables=ExplorerTables(
            bins=layers.bins,
            masks=layers.masks,
            segments=layers.segments,
            candidates=layers.candidates,
            insufficiency=layers.insufficiency,
        ),
    )


def replay_cna_explorer_snapshot(
    dosage: DosageQcAnalysisResult,
    segmented: CnvDevelopmentResult,
    snapshot: CnaExplorerSnapshot,
    *,
    dosage_authority: ExplorerInputAuthority,
    segmented_authority: ExplorerInputAuthority,
) -> CnaExplorerSnapshot:
    """Fail closed unless a snapshot is the exact semantic replay of its inputs."""

    expected = build_cna_explorer_snapshot(
        dosage,
        segmented,
        dosage_authority=dosage_authority,
        segmented_authority=segmented_authority,
    )
    if snapshot != expected:
        raise CnaExplorerError("explorer snapshot does not match semantic replay")
    return snapshot


def cna_explorer_snapshot_bytes(snapshot: CnaExplorerSnapshot) -> bytes:
    """Return exact canonical bytes for an already validated snapshot."""

    return canonical_json_bytes(snapshot)


def cna_explorer_snapshot_sha256(snapshot: CnaExplorerSnapshot) -> str:
    """Return the canonical digest of an explorer snapshot."""

    return hashlib.sha256(cna_explorer_snapshot_bytes(snapshot)).hexdigest()


def cna_explorer_snapshot_from_bytes(content: bytes) -> CnaExplorerSnapshot:
    """Load one exact canonical explorer snapshot."""

    try:
        parsed = CnaExplorerSnapshot.model_validate_json(content)
    except Exception as exc:
        raise CnaExplorerError("explorer snapshot is invalid or noncanonical") from exc
    if canonical_json_bytes(parsed) != content:
        raise CnaExplorerError("explorer snapshot is invalid or noncanonical")
    return parsed


__all__ = [
    "CnaExplorerError",
    "CnaExplorerSnapshot",
    "CnaSource",
    "ExecutionState",
    "ExplorerAvailability",
    "ExplorerInputAuthority",
    "QualificationState",
    "TrustState",
    "build_cna_explorer_snapshot",
    "cna_explorer_snapshot_bytes",
    "cna_explorer_snapshot_from_bytes",
    "cna_explorer_snapshot_sha256",
    "replay_cna_explorer_snapshot",
]
