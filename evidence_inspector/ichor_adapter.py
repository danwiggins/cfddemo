"""Pure preparation and parsing for a pinned ichorCNA development adapter.

This module does not execute R or an OCI runtime.  It prepares one canonical
argv boundary and validates the files emitted by the pinned upstream script.
Private development input is authorized; qualification and product release are
always false in this contract.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import stat
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

ICHOR_COMMIT = "5bfc03ed854f0e93fe5b624c97c1290fa0053837"
HMMCOPY_COMMIT = "3b5efcebea919cafed5b85ac5922f67e8127ce71"
HMMCOPY_UTILS_COMMIT = "29a8d1d18dfd301600d5d91832e5fe231935058c"
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_ROWS = 1_000_000

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CommitSha1 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _canonical_sha256(value: BaseModel | Sequence[BaseModel]) -> str:
    if isinstance(value, BaseModel):
        payload: object = value.model_dump(mode="json")
    else:
        payload = [item.model_dump(mode="json") for item in value]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_sha256(value: BaseModel | Sequence[BaseModel]) -> str:
    """Return the canonical contract digest used by adapter bindings."""

    return _canonical_sha256(value)


class ArtifactIdentity(StrictModel):
    artifact_id: Identifier
    content_sha256: Sha256
    content_size_bytes: int = Field(gt=0)


class ExternalComponentBinding(StrictModel):
    component_id: Identifier
    version_label: Identifier
    source_repository_url: str = Field(min_length=1, max_length=2048)
    source_commit_sha1: CommitSha1
    source_tree_or_archive_sha256: Sha256
    invoked_files: tuple[ArtifactIdentity, ...] = Field(min_length=1)
    declared_license: str = Field(min_length=1, max_length=128)
    license_evidence: ArtifactIdentity
    modified: bool
    modification_manifest_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def modification_identity(self) -> ExternalComponentBinding:
        if self.modified != (self.modification_manifest_sha256 is not None):
            raise ValueError("modified components require exactly one modification manifest")
        return self


class RuntimeBinding(StrictModel):
    target: Literal["preparation_only", "local_r", "oci"]
    operating_system: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    r_version: str | None = Field(default=None, max_length=64)
    package_lock_sha256: Sha256 | None = None
    components: tuple[ExternalComponentBinding, ...] = Field(min_length=2)
    ichor_script_path: str = "/runtime/ichorCNA/scripts/runIchorCNA.R"
    ichor_script_sha256: Sha256
    oci_manifest_digest: str | None = None

    @model_validator(mode="after")
    def exact_runtime(self) -> RuntimeBinding:
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("runtime component IDs must be unique")
        component_commits = {
            item.component_id: item.source_commit_sha1 for item in self.components
        }
        if component_commits.get("ichorCNA") != ICHOR_COMMIT:
            raise ValueError("ichorCNA component is not pinned to the adapter revision")
        if component_commits.get("HMMcopy") != HMMCOPY_COMMIT:
            raise ValueError("HMMcopy component is not pinned to the adapter revision")
        ichor_component = next(
            item for item in self.components if item.component_id == "ichorCNA"
        )
        if self.ichor_script_sha256 not in {
            item.content_sha256 for item in ichor_component.invoked_files
        }:
            raise ValueError("ichor script digest is not bound to the ichor component")
        if self.target in {"local_r", "oci"} and (
            self.r_version is None or self.package_lock_sha256 is None
        ):
            raise ValueError("executable targets require exact R and package-lock identities")
        if self.target == "oci":
            if self.oci_manifest_digest is None or not self.oci_manifest_digest.startswith(
                "sha256:"
            ):
                raise ValueError("OCI target requires a manifest digest, never a tag")
        elif self.oci_manifest_digest is not None:
            raise ValueError("non-OCI target cannot claim an OCI manifest")
        path = PurePosixPath(self.ichor_script_path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("ichor script path must be absolute and safe")
        return self


class CanonicalBin(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def increasing(self) -> CanonicalBin:
        if self.end <= self.start:
            raise ValueError("canonical bin end must exceed start")
        return self


class CanonicalGrid(StrictModel):
    coordinate_system: Literal["zero_based_half_open"] = "zero_based_half_open"
    bins: tuple[CanonicalBin, ...] = Field(min_length=1)
    bin_definition_sha256: Sha256

    @model_validator(mode="after")
    def validate_grid(self) -> CanonicalGrid:
        keys = [(row.contig, row.start, row.end) for row in self.bins]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("canonical bins must be unique and sorted")
        by_contig: dict[str, int] = {}
        for row in self.bins:
            previous_end = by_contig.get(row.contig)
            if previous_end is not None and row.start < previous_end:
                raise ValueError("canonical bins cannot overlap")
            by_contig[row.contig] = row.end
        if _canonical_sha256(self.bins) != self.bin_definition_sha256:
            raise ValueError("canonical bin-definition digest mismatch")
        return self


class ReferenceFastaBinding(StrictModel):
    role: Literal["reference_fasta"] = "reference_fasta"
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["fasta"] = "fasta"
    coordinate_semantics: Literal["sequence"] = "sequence"
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)


class WigGridBinding(StrictModel):
    role: Literal["raw_counts_wig", "gc_wig", "map_wig"]
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["wig_fixed_step"] = "wig_fixed_step"
    native_coordinates: Literal["one_based_fixed_step"] = "one_based_fixed_step"
    span_bp: int = Field(gt=0)
    step_bp: int = Field(gt=0)
    bin_size_bp: int = Field(gt=0)
    canonical_bin_definition_sha256: Sha256
    canonical_conversion: Literal["start1_to_zero_based_half_open"] = (
        "start1_to_zero_based_half_open"
    )
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def fixed_width(self) -> WigGridBinding:
        if not self.span_bp == self.step_bp == self.bin_size_bp:
            raise ValueError("v1 WIG binding requires span, step, and bin size to agree")
        return self


class ExclusionBedBinding(StrictModel):
    role: Literal["exclusion_bed"] = "exclusion_bed"
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["bed"] = "bed"
    native_coordinates: Literal["zero_based_half_open"] = "zero_based_half_open"
    interval_set_sha256: Sha256
    canonical_conversion: Literal["identity"] = "identity"
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)


class PanelOfNormalsBinding(StrictModel):
    role: Literal["panel_of_normals"] = "panel_of_normals"
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["rdata_granges"] = "rdata_granges"
    native_coordinates: Literal["one_based_closed"] = "one_based_closed"
    bin_size_bp: int = Field(gt=0)
    canonical_bin_definition_sha256: Sha256
    canonical_conversion: Literal["one_based_closed_to_zero_based_half_open"] = (
        "one_based_closed_to_zero_based_half_open"
    )
    donor_authorization_id: Identifier
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)


class CnvAssetSet(StrictModel):
    reference: ReferenceFastaBinding
    raw_counts: WigGridBinding
    gc: WigGridBinding
    mappability: WigGridBinding | None
    exclusion: ExclusionBedBinding
    panel_of_normals: PanelOfNormalsBinding | None
    canonical_grid: CanonicalGrid

    @model_validator(mode="after")
    def compatible_assets(self) -> CnvAssetSet:
        binned: list[WigGridBinding | PanelOfNormalsBinding] = [
            self.raw_counts,
            self.gc,
        ]
        if self.mappability is not None:
            binned.append(self.mappability)
        if self.panel_of_normals is not None:
            binned.append(self.panel_of_normals)
        all_assets: Iterable[object] = (
            self.reference,
            *binned,
            self.exclusion,
        )
        for asset in all_assets:
            if asset.assembly != self.reference.assembly:  # type: ignore[attr-defined]
                raise ValueError("CNV assets must use one assembly")
            if asset.contig_dictionary_sha256 != (  # type: ignore[attr-defined]
                self.reference.contig_dictionary_sha256
            ):
                raise ValueError("CNV assets must use equivalent contig dictionaries")
        for asset in binned:
            if asset.canonical_bin_definition_sha256 != (
                self.canonical_grid.bin_definition_sha256
            ):
                raise ValueError("binned asset is not on the canonical grid")
            if asset.bin_size_bp != self.raw_counts.bin_size_bp:
                raise ValueError("binned asset sizes disagree")
        return self


class CountingPolicy(StrictModel):
    count_unit: Literal["eligible_primary_alignment_start"] = (
        "eligible_primary_alignment_start"
    )
    minimum_mapping_quality: int = Field(ge=0, le=255)
    exclude_unmapped: bool
    exclude_secondary: bool
    exclude_supplementary: bool
    exclude_qc_failure: bool
    exclude_duplicate: bool
    contigs: tuple[Identifier, ...] = Field(min_length=1)
    terminal_bin_policy: Literal["emit_partial", "exclude_partial"]

    @model_validator(mode="after")
    def unique_contigs(self) -> CountingPolicy:
        if len(self.contigs) != len(set(self.contigs)):
            raise ValueError("counting-policy contigs must be unique")
        return self


class RawWigLineage(StrictModel):
    source_bam_sha256: Sha256
    source_bai_sha256: Sha256
    counter_implementation_sha256: Sha256
    counting_policy_sha256: Sha256
    wig_content_sha256: Sha256
    canonical_bin_definition_sha256: Sha256


class IchorParameterSet(StrictModel):
    chromosomes: tuple[int, ...] = tuple(range(1, 23))
    normal_fraction_starts: tuple[FiniteFloat, ...]
    ploidy_starts: tuple[FiniteFloat, ...]
    max_copy_number: int = Field(ge=2, le=25)
    include_subclonal_states: bool
    lambda_policy: Literal["automatic", "explicit"]
    lambda_values: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat] | None
    minimum_map_score: FiniteFloat = Field(ge=0, le=1)
    centromere_flank_bp: int = Field(ge=0)
    transition_probability: FiniteFloat = Field(gt=0, lt=1)
    transition_strength: FiniteFloat = Field(gt=0)
    minimum_segment_bins: int = Field(gt=0)
    altered_fraction_threshold: FiniteFloat = Field(ge=0, le=1)

    @model_validator(mode="after")
    def coherent_parameters(self) -> IchorParameterSet:
        if (
            not self.chromosomes
            or len(self.chromosomes) != len(set(self.chromosomes))
            or self.chromosomes != tuple(sorted(self.chromosomes))
        ):
            raise ValueError("chromosomes must be sorted, unique, and non-empty")
        if any(not 1 <= value <= 22 for value in self.chromosomes):
            raise ValueError("v1 adapter supports autosomes 1 through 22")
        if any(not 0 <= value <= 1 for value in self.normal_fraction_starts):
            raise ValueError("normal-fraction starts must be within zero and one")
        if not self.normal_fraction_starts or not self.ploidy_starts:
            raise ValueError("normal and ploidy starts cannot be empty")
        if self.normal_fraction_starts != tuple(sorted(set(self.normal_fraction_starts))):
            raise ValueError("normal-fraction starts must be sorted and unique")
        if self.ploidy_starts != tuple(sorted(set(self.ploidy_starts))):
            raise ValueError("ploidy starts must be sorted and unique")
        if self.lambda_policy == "explicit" and self.lambda_values is None:
            raise ValueError("explicit lambda policy requires four values")
        if self.lambda_policy == "automatic" and self.lambda_values is not None:
            raise ValueError("automatic lambda policy cannot include values")
        if self.lambda_values is not None and any(value <= 0 for value in self.lambda_values):
            raise ValueError("lambda values must be positive")
        return self


class CnvRunRequest(StrictModel):
    schema_version: Literal["traceback.ichor-request.v1"] = (
        "traceback.ichor-request.v1"
    )
    sample_id: Identifier
    input_bam: ArtifactIdentity
    input_bai: ArtifactIdentity
    runtime: RuntimeBinding
    assets: CnvAssetSet
    counting_policy: CountingPolicy
    raw_wig_lineage: RawWigLineage
    read_count_source: Literal[
        "hmmcopy_utils_readcounter", "precomputed_bound_wig"
    ]
    pon_mode: Literal["none_development", "protocol_matched_frozen"]
    parameters: IchorParameterSet
    development_input_authorized: Literal[True] = True
    product_release_authorized: Literal[False] = False
    qualification_established: Literal[False] = False

    @model_validator(mode="after")
    def bind_request(self) -> CnvRunRequest:
        if self.raw_wig_lineage.source_bam_sha256 != self.input_bam.content_sha256:
            raise ValueError("raw-WIG lineage does not bind the input BAM")
        if self.raw_wig_lineage.source_bai_sha256 != self.input_bai.content_sha256:
            raise ValueError("raw-WIG lineage does not bind the input BAI")
        if self.raw_wig_lineage.wig_content_sha256 != (
            self.assets.raw_counts.identity.content_sha256
        ):
            raise ValueError("raw-WIG lineage does not bind the WIG asset")
        if self.raw_wig_lineage.counting_policy_sha256 != _canonical_sha256(
            self.counting_policy
        ):
            raise ValueError("raw-WIG lineage does not bind the counting policy")
        if self.raw_wig_lineage.canonical_bin_definition_sha256 != (
            self.assets.canonical_grid.bin_definition_sha256
        ):
            raise ValueError("raw-WIG lineage does not bind the canonical grid")
        if self.read_count_source == "hmmcopy_utils_readcounter":
            utilities = next(
                (
                    item
                    for item in self.runtime.components
                    if item.component_id == "hmmcopy_utils"
                ),
                None,
            )
            if utilities is None or utilities.source_commit_sha1 != HMMCOPY_UTILS_COMMIT:
                raise ValueError("hmmcopy_utils counter source is not pinned")
            if self.raw_wig_lineage.counter_implementation_sha256 not in {
                item.content_sha256 for item in utilities.invoked_files
            }:
                raise ValueError("counter lineage is not bound to hmmcopy_utils")
        has_pon = self.assets.panel_of_normals is not None
        if has_pon != (self.pon_mode == "protocol_matched_frozen"):
            raise ValueError("PoN mode and PoN asset presence disagree")
        return self


class OutputAvailability(StrEnum):
    DIRECTLY_EMITTED = "directly_emitted"
    REQUIRED_INPUT = "required_input"
    OPAQUE_RDATA = "opaque_rdata"
    NOT_EMITTED_BY_PINNED_UPSTREAM = "not_emitted_by_pinned_upstream"


class OutputCapability(StrictModel):
    role: Identifier
    availability: OutputAvailability
    relative_path: str | None = None

    @model_validator(mode="after")
    def safe_path(self) -> OutputCapability:
        if self.relative_path is not None:
            path = PurePosixPath(self.relative_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("output path must be safe and relative")
        emitted = self.availability in {
            OutputAvailability.DIRECTLY_EMITTED,
            OutputAvailability.OPAQUE_RDATA,
        }
        if emitted != (self.relative_path is not None):
            raise ValueError("only emitted output capabilities have paths")
        return self


class PreparedIchorRun(StrictModel):
    schema_version: Literal["traceback.prepared-ichor-run.v1"] = (
        "traceback.prepared-ichor-run.v1"
    )
    request: CnvRunRequest
    request_sha256: Sha256
    argv: tuple[str, ...] = Field(min_length=2)
    environment: tuple[tuple[str, str], ...] = ()
    output_capabilities: tuple[OutputCapability, ...]
    network: Literal["none"] = "none"
    pull_policy: Literal["never"] = "never"
    read_only_root_filesystem: Literal[True] = True
    development_input_authorized: Literal[True] = True
    product_release_authorized: Literal[False] = False
    qualification_established: Literal[False] = False

    @model_validator(mode="after")
    def bind_prepared(self) -> PreparedIchorRun:
        if self.request_sha256 != _canonical_sha256(self.request):
            raise ValueError("prepared request digest mismatch")
        if any(not item or "\x00" in item for item in self.argv):
            raise ValueError("argv must contain non-empty NUL-free arguments")
        if self.environment != tuple(sorted(self.environment)):
            raise ValueError("environment must be deterministically sorted")
        roles = [item.role for item in self.output_capabilities]
        if len(roles) != len(set(roles)):
            raise ValueError("output capability roles must be unique")
        return self


def _r_vector(values: Sequence[int | float]) -> str:
    return "c(" + ",".join(format(value, ".15g") for value in values) + ")"


def _output_capabilities(sample_id: str) -> tuple[OutputCapability, ...]:
    return (
        OutputCapability(role="raw_counts", availability="required_input"),
        OutputCapability(
            role="combined_corrected_depth",
            availability="directly_emitted",
            relative_path=f"{sample_id}.correctedDepth.txt",
        ),
        OutputCapability(
            role="segments_detailed",
            availability="directly_emitted",
            relative_path=f"{sample_id}.seg.txt",
        ),
        OutputCapability(
            role="segments_raw",
            availability="directly_emitted",
            relative_path=f"{sample_id}.seg",
        ),
        OutputCapability(
            role="bin_level_cna",
            availability="directly_emitted",
            relative_path=f"{sample_id}.cna.seg",
        ),
        OutputCapability(
            role="parameters_and_candidates",
            availability="directly_emitted",
            relative_path=f"{sample_id}.params.txt",
        ),
        OutputCapability(
            role="r_workspace",
            availability="opaque_rdata",
            relative_path=f"{sample_id}.RData",
        ),
        OutputCapability(
            role="gc_only_corrected",
            availability="not_emitted_by_pinned_upstream",
        ),
        OutputCapability(
            role="map_only_corrected",
            availability="not_emitted_by_pinned_upstream",
        ),
        OutputCapability(
            role="pon_residual",
            availability="not_emitted_by_pinned_upstream",
        ),
        OutputCapability(
            role="validity_mask",
            availability="not_emitted_by_pinned_upstream",
        ),
    )


def prepare_ichor_run(request: CnvRunRequest) -> PreparedIchorRun:
    """Create a deterministic argv plan without executing external software."""

    params = request.parameters
    argv = [
        "Rscript",
        request.runtime.ichor_script_path,
        "--id",
        request.sample_id,
        "--WIG",
        "/input/counts.wig",
        "--gcWig",
        "/assets/gc.wig",
        "--centromere",
        "/assets/exclusions.bed",
        "--normal",
        _r_vector(params.normal_fraction_starts),
        "--ploidy",
        _r_vector(params.ploidy_starts),
        "--maxCN",
        str(params.max_copy_number),
        "--estimateScPrevalence",
        "TRUE" if params.include_subclonal_states else "FALSE",
        "--scStates",
        "c(1,3)" if params.include_subclonal_states else "NULL",
        "--lambda",
        "NULL" if params.lambda_values is None else _r_vector(params.lambda_values),
        "--minMapScore",
        format(params.minimum_map_score, ".15g"),
        "--rmCentromereFlankLength",
        str(params.centromere_flank_bp),
        "--txnE",
        format(params.transition_probability, ".15g"),
        "--txnStrength",
        format(params.transition_strength, ".15g"),
        "--minSegmentBins",
        str(params.minimum_segment_bins),
        "--altFracThreshold",
        format(params.altered_fraction_threshold, ".15g"),
        "--chrs",
        _r_vector(params.chromosomes),
        "--chrTrain",
        _r_vector(params.chromosomes),
        "--chrNormalize",
        _r_vector(params.chromosomes),
        "--genomeBuild",
        request.assets.reference.assembly,
        "--genomeStyle",
        "UCSC",
        "--includeHOMD",
        "FALSE",
        "--outDir",
        "/attempt",
    ]
    if request.assets.mappability is not None:
        argv.extend(("--mapWig", "/assets/map.wig"))
    if request.assets.panel_of_normals is not None:
        argv.extend(("--normalPanel", "/assets/pon.rds"))
    return PreparedIchorRun(
        request=request,
        request_sha256=_canonical_sha256(request),
        argv=tuple(argv),
        output_capabilities=_output_capabilities(request.sample_id),
    )


class OutputArtifact(StrictModel):
    role: Identifier
    relative_path: str
    content_sha256: Sha256
    content_size_bytes: int = Field(gt=0)


class CorrectedBin(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    corrected_log2: FiniteFloat

    @model_validator(mode="after")
    def increasing(self) -> CorrectedBin:
        if self.end <= self.start:
            raise ValueError("corrected bin end must exceed start")
        return self


class CnaSegment(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    bin_count: int = Field(gt=0)
    median_log2: FiniteFloat
    copy_number: int = Field(ge=0)
    call: str = Field(min_length=1, max_length=64)
    subclone_status: bool

    @model_validator(mode="after")
    def increasing(self) -> CnaSegment:
        if self.end <= self.start:
            raise ValueError("segment end must exceed start")
        return self


class CandidateSolution(StrictModel):
    candidate_id: Identifier
    initial_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    initial_ploidy: FiniteFloat = Field(gt=0)
    estimated_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    model_fraction: FiniteFloat = Field(ge=0, le=1)
    estimated_ploidy: FiniteFloat = Field(gt=0)
    bic: FiniteFloat
    fraction_genome_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    fraction_cna_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    log_likelihood: FiniteFloat


class SelectedSolution(StrictModel):
    sample_id: Identifier
    model_fraction: FiniteFloat = Field(ge=0, le=1)
    ploidy: FiniteFloat = Field(gt=0)
    matched_candidate_id: Identifier | None


class CnvDevelopmentResult(StrictModel):
    schema_version: Literal["traceback.ichor-development-result.v1"] = (
        "traceback.ichor-development-result.v1"
    )
    status: Literal["complete", "insufficient_information"]
    qualification_status: Literal["development_unqualified"] = (
        "development_unqualified"
    )
    development_input_authorized: Literal[True] = True
    product_release_authorized: Literal[False] = False
    request_sha256: Sha256
    pon_mode: Literal["none_development", "protocol_matched_frozen"]
    corrected_bins: tuple[CorrectedBin, ...]
    segments: tuple[CnaSegment, ...]
    candidates: tuple[CandidateSolution, ...]
    selected_solution: SelectedSolution
    identifiability: Literal[
        "identifiable_within_development_model",
        "competitive_solutions",
        "insufficient_altered_structure",
        "not_assessed",
    ]
    output_capabilities: tuple[OutputCapability, ...]
    artifacts: tuple[OutputArtifact, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def coherent_result(self) -> CnvDevelopmentResult:
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        selected = self.selected_solution.matched_candidate_id
        if selected is not None and selected not in ids:
            raise ValueError("selected candidate is not present")
        artifact_roles = [item.role for item in self.artifacts]
        if artifact_roles != sorted(artifact_roles) or len(artifact_roles) != len(
            set(artifact_roles)
        ):
            raise ValueError("output artifacts must be uniquely sorted by role")
        if self.status == "insufficient_information" and self.identifiability != (
            "insufficient_altered_structure"
        ):
            raise ValueError("insufficient result must retain its identifiability reason")
        return self


class IchorOutputError(ValueError):
    """Pinned upstream output is missing, unsafe, or semantically inconsistent."""


def _safe_output(path: Path) -> tuple[str, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise IchorOutputError(f"missing required output: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise IchorOutputError(f"output must be a regular non-symlink: {path.name}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_OUTPUT_BYTES:
        raise IchorOutputError(f"output size is invalid: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), metadata.st_size


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    if not rows or len(rows) > MAX_ROWS:
        raise IchorOutputError(f"output row count is invalid: {path.name}")
    return rows


def _finite(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise IchorOutputError(f"{label} is not numeric") from exc
    if not math.isfinite(parsed):
        raise IchorOutputError(f"{label} must be finite")
    return parsed


def _integer(value: str, label: str) -> int:
    parsed = _finite(value, label)
    if not parsed.is_integer():
        raise IchorOutputError(f"{label} must be an integer")
    return int(parsed)


def _optional_fraction(value: str, label: str) -> float | None:
    if value.strip().upper() in {"NA", "NAN"}:
        return None
    parsed = _finite(value, label)
    if not 0 <= parsed <= 1:
        raise IchorOutputError(f"{label} must be within zero and one")
    return parsed


def _parse_corrected(path: Path, grid: CanonicalGrid) -> tuple[CorrectedBin, ...]:
    expected = {(row.contig, row.start, row.end) for row in grid.bins}
    result: list[CorrectedBin] = []
    for row in _rows(path):
        try:
            contig = row["chr"]
            start = _integer(row["start"], "corrected start") - 1
            end = _integer(row["end"], "corrected end")
            value = _finite(row["log2_TNratio_corrected"], "corrected log2")
        except KeyError as exc:
            raise IchorOutputError("corrected-depth columns are invalid") from exc
        if (contig, start, end) not in expected:
            raise IchorOutputError("corrected-depth row is outside the canonical grid")
        result.append(CorrectedBin(contig=contig, start=start, end=end, corrected_log2=value))
    keys = [(row.contig, row.start, row.end) for row in result]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise IchorOutputError("corrected-depth rows must be unique and sorted")
    return tuple(result)


def _parse_segments(path: Path, sample_id: str) -> tuple[CnaSegment, ...]:
    result: list[CnaSegment] = []
    for row in _rows(path):
        try:
            if row["ID"] != sample_id:
                raise IchorOutputError("segment sample ID mismatch")
            subclone = row["subclone.status"].upper()
            if subclone not in {"TRUE", "FALSE"}:
                raise IchorOutputError("segment subclone status is invalid")
            result.append(
                CnaSegment(
                    contig=row["chrom"],
                    start=_integer(row["start"], "segment start") - 1,
                    end=_integer(row["end"], "segment end"),
                    bin_count=_integer(row["num.mark"], "segment bin count"),
                    median_log2=_finite(row["seg.median.logR"], "segment median"),
                    copy_number=_integer(row["copy.number"], "segment copy number"),
                    call=row["call"],
                    subclone_status=subclone == "TRUE",
                )
            )
        except KeyError as exc:
            raise IchorOutputError("segment columns are invalid") from exc
    keys = [(row.contig, row.start, row.end) for row in result]
    if keys != sorted(keys):
        raise IchorOutputError("segments must be sorted")
    previous: dict[str, int] = {}
    for row in result:
        if row.start < previous.get(row.contig, 0):
            raise IchorOutputError("segments cannot overlap")
        previous[row.contig] = row.end
    return tuple(result)


def _parse_init(value: str) -> tuple[float, float]:
    if not value.startswith("n") or "-p" not in value:
        raise IchorOutputError("candidate init field is invalid")
    normal, ploidy = value[1:].split("-p", 1)
    return _finite(normal, "initial normal"), _finite(ploidy, "initial ploidy")


def _parse_params(
    path: Path,
    sample_id: str,
) -> tuple[SelectedSolution, tuple[CandidateSolution, ...]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3:
        raise IchorOutputError("parameter output is truncated")
    selected_header = lines[0].split("\t")
    selected_values = lines[1].split("\t")
    if selected_header != ["Sample", "Tumor Fraction", "Ploidy"] or len(
        selected_values
    ) != 3:
        raise IchorOutputError("selected parameter summary is invalid")
    if selected_values[0] != sample_id:
        raise IchorOutputError("selected parameter sample ID mismatch")
    selected_fraction = _finite(selected_values[1], "selected model fraction")
    selected_ploidy = _finite(selected_values[2], "selected ploidy")
    candidate_header = [
        "init",
        "n_est",
        "phi_est",
        "BIC",
        "Frac_genome_subclonal",
        "Frac_CNA_subclonal",
        "loglik",
    ]
    header_index = next(
        (index for index, line in enumerate(lines) if line.split("\t") == candidate_header),
        None,
    )
    if header_index is None:
        raise IchorOutputError("candidate table is absent")
    candidates: list[CandidateSolution] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        values = line.split("\t")
        if len(values) != len(candidate_header):
            raise IchorOutputError("candidate row has the wrong number of fields")
        row = dict(zip(candidate_header, values, strict=True))
        initial_normal, initial_ploidy = _parse_init(row["init"])
        estimated_normal = _finite(row["n_est"], "estimated normal")
        candidates.append(
            CandidateSolution(
                candidate_id=row["init"].replace("-", "."),
                initial_normal_fraction=initial_normal,
                initial_ploidy=initial_ploidy,
                estimated_normal_fraction=estimated_normal,
                model_fraction=1 - estimated_normal,
                estimated_ploidy=_finite(row["phi_est"], "estimated ploidy"),
                bic=_finite(row["BIC"], "candidate BIC"),
                fraction_genome_subclonal=_optional_fraction(
                    row["Frac_genome_subclonal"], "subclonal genome fraction"
                ),
                fraction_cna_subclonal=_optional_fraction(
                    row["Frac_CNA_subclonal"], "subclonal CNA fraction"
                ),
                log_likelihood=_finite(row["loglik"], "candidate log likelihood"),
            )
        )
    if not candidates:
        raise IchorOutputError("candidate table is empty")
    matched = [
        item.candidate_id
        for item in candidates
        if math.isclose(item.model_fraction, selected_fraction, abs_tol=5e-4)
        and math.isclose(item.estimated_ploidy, selected_ploidy, abs_tol=5e-4)
    ]
    return (
        SelectedSolution(
            sample_id=sample_id,
            model_fraction=selected_fraction,
            ploidy=selected_ploidy,
            matched_candidate_id=matched[0] if len(matched) == 1 else None,
        ),
        tuple(candidates),
    )


def validate_ichor_outputs(
    prepared: PreparedIchorRun,
    output_directory: Path,
) -> CnvDevelopmentResult:
    """Parse only files the pinned upstream emits; never execute or deserialize RData."""

    try:
        output_metadata = output_directory.lstat()
    except OSError as exc:
        raise IchorOutputError("output directory is absent") from exc
    if not stat.S_ISDIR(output_metadata.st_mode) or output_directory.is_symlink():
        raise IchorOutputError("output directory is absent")
    emitted = [
        item
        for item in prepared.output_capabilities
        if item.relative_path is not None
    ]
    allowed = {item.relative_path for item in emitted}
    observed = {
        path.name
        for path in output_directory.iterdir()
        if path.name in allowed
    }
    missing = sorted(allowed - observed)
    if missing:
        raise IchorOutputError(f"missing required outputs: {missing}")

    artifacts: list[OutputArtifact] = []
    role_paths = {item.role: item.relative_path for item in emitted}
    for item in emitted:
        assert item.relative_path is not None
        path = output_directory / item.relative_path
        digest, size = _safe_output(path)
        artifacts.append(
            OutputArtifact(
                role=item.role,
                relative_path=item.relative_path,
                content_sha256=digest,
                content_size_bytes=size,
            )
        )

    try:
        corrected = _parse_corrected(
            output_directory / str(role_paths["combined_corrected_depth"]),
            prepared.request.assets.canonical_grid,
        )
        segments = _parse_segments(
            output_directory / str(role_paths["segments_detailed"]),
            prepared.request.sample_id,
        )
        selected, candidates = _parse_params(
            output_directory / str(role_paths["parameters_and_candidates"]),
            prepared.request.sample_id,
        )
    except ValidationError as exc:
        raise IchorOutputError("upstream output violates the adapter contract") from exc
    grid_bins = prepared.request.assets.canonical_grid.bins
    for segment in segments:
        covered = [
            row
            for row in grid_bins
            if row.contig == segment.contig
            and row.start >= segment.start
            and row.end <= segment.end
        ]
        if (
            not covered
            or covered[0].start != segment.start
            or covered[-1].end != segment.end
            or len(covered) != segment.bin_count
        ):
            raise IchorOutputError("segment does not align with the canonical bin grid")
    neutral_calls = {"NEUT", "NEUTRAL"}
    altered = [item for item in segments if item.call.upper() not in neutral_calls]
    altered_bins = sum(item.bin_count for item in altered)
    total_bins = sum(item.bin_count for item in segments)
    largest_altered = max((item.bin_count for item in altered), default=0)
    altered_fraction = altered_bins / total_bins if total_bins else 0
    insufficient_structure = (
        selected.model_fraction == 0
        and largest_altered <= prepared.request.parameters.minimum_segment_bins
        and altered_fraction <= prepared.request.parameters.altered_fraction_threshold
    )
    competitive = selected.matched_candidate_id is None
    if insufficient_structure:
        status: Literal["complete", "insufficient_information"] = (
            "insufficient_information"
        )
        identifiability = "insufficient_altered_structure"
    else:
        status = "complete"
        identifiability = (
            "competitive_solutions"
            if competitive
            else "not_assessed"
        )
    limitations = [
        "Pinned ichorCNA development output; not analytically qualified.",
        "Model fraction is conditional on copy-state, ploidy, and parameter assumptions.",
        "Separate GC-only, map-only, and PoN-residual stages are not emitted upstream.",
        "RData is retained by digest but is not deserialized by this parser.",
        "Raw .seg and bin-level .cna.seg files are retained by digest; v1 parses .seg.txt.",
    ]
    if prepared.request.pon_mode == "none_development":
        limitations.append("No protocol-matched panel of normals was supplied.")
    return CnvDevelopmentResult(
        status=status,
        request_sha256=prepared.request_sha256,
        pon_mode=prepared.request.pon_mode,
        corrected_bins=corrected,
        segments=segments,
        candidates=candidates,
        selected_solution=selected,
        identifiability=identifiability,
        output_capabilities=prepared.output_capabilities,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.role)),
        limitations=tuple(limitations),
    )


__all__ = [
    "CanonicalBin",
    "CanonicalGrid",
    "CnvAssetSet",
    "CnvDevelopmentResult",
    "CnvRunRequest",
    "CountingPolicy",
    "ExclusionBedBinding",
    "ExternalComponentBinding",
    "HMMCOPY_COMMIT",
    "HMMCOPY_UTILS_COMMIT",
    "ICHOR_COMMIT",
    "IchorOutputError",
    "IchorParameterSet",
    "PanelOfNormalsBinding",
    "PreparedIchorRun",
    "RawWigLineage",
    "ReferenceFastaBinding",
    "RuntimeBinding",
    "WigGridBinding",
    "prepare_ichor_run",
    "contract_sha256",
    "validate_ichor_outputs",
]
