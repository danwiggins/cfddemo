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
import re
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

# Pin reconciliation (signal-methods Q2, CN1).  The installed ichorCNA is the
# bioconda package r-ichorcna 0.5.1, build r44hdfd78af_1, from the committed
# toolchain lock.  Its recipe builds GitHub tag v0.5.1 (this commit; source
# archive SHA-256 below) with two bioconda patches, so the component is
# *modified*.  The earlier pin 5bfc03ed (ichorCNA 0.3.2, 2019) was 90 commits
# behind v0.5.1 and is not what any toolchain installs.  v0.5.1 ships no
# runIchorCNA.R script; the toolchain's driver calls ichorCNA::run_ichorCNA().
ICHOR_COMMIT = "e5a6b30c061efc20d12c241eefb2dc7ed911cb6c"
ICHOR_VERSION = "0.5.1"
ICHOR_SOURCE_ARCHIVE_SHA256 = (
    "9c28f86efaee172d80390091edcaf8414ec3ca23c4272300284a4c3fac3ac683"
)
ICHOR_BIOCONDA_BUILD = "r44hdfd78af_1"
ICHOR_BIOCONDA_PATCH_SHA256 = (
    (
        "0001-Set-chrX-median-counts-to-NA-if-gender-is-unknown.patch",
        "8d773bfa023fabaea04586581f79013095c121a816fe11bf62697462ca99356d",
    ),
    (
        "0002-Adjust-uses-of-counts-so-that-they-can-take-account-.patch",
        "87b85eb156faa4c1293a28ad2281b1e88bbabc5721c85c1ebffbac684dbc4560",
    ),
)
HMMCOPY_COMMIT = "3b5efcebea919cafed5b85ac5922f67e8127ce71"
HMMCOPY_UTILS_COMMIT = "29a8d1d18dfd301600d5d91832e5fe231935058c"
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_ROWS = 1_000_000
# Driver flags the adapter fixes for every run (not locked parameters): the
# HMM's clonal states start at copy number 1 unless HOMD is included, and the
# subclonal states, when enabled, are these copy numbers.
INCLUDE_HOMD = False
SUBCLONAL_COPY_STATES: tuple[int, ...] = (1, 3)
# ichorCNA 0.5.1 outputHMM() writes a segment's call as
# names[copy.number + 1] over this vector, then "HLAMP2", "HLAMP3", ...
_UPSTREAM_CALLS = ("HOMD", "HETD", "NEUT", "GAIN", "AMP", "HLAMP")

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


CanonicalDigestValue = BaseModel | str | int | float | bool | None


def _canonical_sha256(
    value: BaseModel | Sequence[CanonicalDigestValue],
) -> str:
    if isinstance(value, BaseModel):
        payload: object = value.model_dump(mode="json")
    else:
        payload = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_sha256(value: BaseModel | Sequence[CanonicalDigestValue]) -> str:
    """Return the canonical contract digest used by adapter bindings."""

    return _canonical_sha256(value)


# The modification manifest an ichorCNA component binding carries for the
# bioconda build (``modified=True``): the ordered patch names and digests.
ICHOR_BIOCONDA_MODIFICATION_MANIFEST_SHA256 = _canonical_sha256(
    [f"{name}:{digest}" for name, digest in ICHOR_BIOCONDA_PATCH_SHA256]
)


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
            raise ValueError(
                "modified components require exactly one modification manifest"
            )
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
            raise ValueError(
                "executable targets require exact R and package-lock identities"
            )
        if self.target == "oci":
            if (
                self.oci_manifest_digest is None
                or not self.oci_manifest_digest.startswith("sha256:")
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


class CanonicalBinMask(StrictModel):
    bin: CanonicalBin
    reason: Literal["centromere_or_flank", "low_mappability"]
    source_artifact_sha256: Sha256
    source_value: FiniteFloat | None = None
    upstream_row_presence: Literal["removed"] = "removed"

    @model_validator(mode="after")
    def reason_specific_evidence(self) -> CanonicalBinMask:
        if self.reason == "centromere_or_flank" and self.source_value is not None:
            raise ValueError("centromere masks cannot claim a map score")
        if self.reason == "low_mappability" and self.source_value is None:
            raise ValueError("low-mappability masks require a bound map score")
        return self


class CanonicalGrid(StrictModel):
    coordinate_system: Literal["zero_based_half_open"] = "zero_based_half_open"
    contig_order: tuple[Identifier, ...] = Field(min_length=1)
    bins: tuple[CanonicalBin, ...] = Field(min_length=1)
    masks: tuple[CanonicalBinMask, ...] = ()
    bin_definition_sha256: Sha256

    @model_validator(mode="after")
    def validate_grid(self) -> CanonicalGrid:
        if len(self.contig_order) != len(set(self.contig_order)):
            raise ValueError("canonical contig order must be unique")
        order = {contig: index for index, contig in enumerate(self.contig_order)}
        if any(row.contig not in order for row in self.bins):
            raise ValueError("canonical bin references an undeclared contig")
        keys = [(row.contig, row.start, row.end) for row in self.bins]
        ordered_keys = sorted(keys, key=lambda row: (order[row[0]], row[1], row[2]))
        if keys != ordered_keys or len(keys) != len(set(keys)):
            raise ValueError("canonical bins must follow declared contig order")
        by_contig: dict[str, int] = {}
        for row in self.bins:
            previous_end = by_contig.get(row.contig)
            if previous_end is not None and row.start < previous_end:
                raise ValueError("canonical bins cannot overlap")
            by_contig[row.contig] = row.end
        if _canonical_sha256(self.bins) != self.bin_definition_sha256:
            raise ValueError("canonical bin-definition digest mismatch")
        known = set(self.bins)
        masked = [item.bin for item in self.masks]
        if any(item not in known for item in masked) or len(masked) != len(set(masked)):
            raise ValueError("canonical masks must uniquely reference grid bins")
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


WigRole = Literal["raw_counts_wig", "gc_wig", "map_wig"]


def check_wig_grid(
    role: WigRole,
    *,
    span_bp: int,
    step_bp: int,
    bin_size_bp: int,
    canonical_values: Sequence[float] | None,
) -> None:
    """The v1 WIG rules shared by :class:`WigGridBinding` and the file parser.

    Span, step and bin size agree.  The mappability WIG carries replayable
    values within zero and one; the GC (and raw-count) WIG binds none.
    """

    if not span_bp == step_bp == bin_size_bp:
        raise ValueError("v1 WIG binding requires span, step, and bin size to agree")
    if role == "map_wig":
        if canonical_values is None:
            raise ValueError("mappability WIG requires replayable canonical values")
        if any(not 0 <= value <= 1 for value in canonical_values):
            raise ValueError("mappability values must be within zero and one")
    elif canonical_values is not None:
        raise ValueError("only the mappability WIG binds canonical values in v1")


class WigGridBinding(StrictModel):
    role: WigRole
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["wig_fixed_step"] = "wig_fixed_step"
    native_coordinates: Literal["one_based_fixed_step"] = "one_based_fixed_step"
    span_bp: int = Field(gt=0)
    step_bp: int = Field(gt=0)
    bin_size_bp: int = Field(gt=0)
    canonical_bin_definition_sha256: Sha256
    canonical_values: tuple[FiniteFloat, ...] | None = None
    canonical_values_sha256: Sha256 | None = None
    canonical_conversion: Literal["start1_to_zero_based_half_open"] = (
        "start1_to_zero_based_half_open"
    )
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def fixed_width(self) -> WigGridBinding:
        has_values = self.canonical_values is not None
        if has_values != (self.canonical_values_sha256 is not None):
            raise ValueError("canonical WIG values require their exact digest")
        check_wig_grid(
            self.role,
            span_bp=self.span_bp,
            step_bp=self.step_bp,
            bin_size_bp=self.bin_size_bp,
            canonical_values=self.canonical_values,
        )
        if (
            self.canonical_values is not None
            and _canonical_sha256(self.canonical_values) != self.canonical_values_sha256
        ):
            raise ValueError("canonical mappability value digest mismatch")
        return self


class CentromereInterval(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def increasing(self) -> CentromereInterval:
        if self.end <= self.start:
            raise ValueError("centromere interval end must exceed start")
        return self


class CentromereTableBinding(StrictModel):
    role: Literal["ichor_centromere_table"] = "ichor_centromere_table"
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    native_format: Literal["ichor_centromere_tsv"] = "ichor_centromere_tsv"
    required_columns: tuple[
        Literal["Chr"], Literal["Start"], Literal["End"], Literal["GapType"]
    ] = ("Chr", "Start", "End", "GapType")
    native_coordinates: Literal["one_based_closed_granges"] = "one_based_closed_granges"
    required_gap_type: Literal["centromere"] = "centromere"
    canonical_intervals: tuple[CentromereInterval, ...] = Field(min_length=1)
    interval_set_sha256: Sha256
    canonical_conversion: Literal["one_based_closed_to_zero_based_half_open"] = (
        "one_based_closed_to_zero_based_half_open"
    )
    source_url: str = Field(min_length=1, max_length=2048)
    declared_license_or_terms: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def bind_intervals(self) -> CentromereTableBinding:
        keys = [(row.contig, row.start, row.end) for row in self.canonical_intervals]
        if len(keys) != len(set(keys)):
            raise ValueError("centromere intervals must be unique")
        if _canonical_sha256(self.canonical_intervals) != self.interval_set_sha256:
            raise ValueError("centromere interval-set digest mismatch")
        return self


class PanelOfNormalsBinding(StrictModel):
    role: Literal["panel_of_normals"] = "panel_of_normals"
    identity: ArtifactIdentity
    assembly: Identifier
    contig_dictionary_sha256: Sha256
    # ichorCNA reads the panel with readRDS() (utils.R at v0.5.1), and
    # createPanelOfNormals saves it with saveRDS(): an .rds GRanges, never
    # an .RData workspace.  The argv passes it as /assets/pon.rds.
    native_format: Literal["rds_granges"] = "rds_granges"
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
    centromere: CentromereTableBinding
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
            self.centromere,
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
        if self.raw_counts.role != "raw_counts_wig":
            raise ValueError("raw-count asset role is invalid")
        if self.gc.role != "gc_wig":
            raise ValueError("GC asset role is invalid")
        if self.mappability is not None and self.mappability.role != "map_wig":
            raise ValueError("mappability asset role is invalid")
        if self.mappability is not None and len(
            self.mappability.canonical_values or ()
        ) != len(self.canonical_grid.bins):
            raise ValueError("mappability values do not cover the canonical grid")
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
        if self.normal_fraction_starts != tuple(
            sorted(set(self.normal_fraction_starts))
        ):
            raise ValueError("normal-fraction starts must be sorted and unique")
        if self.ploidy_starts != tuple(sorted(set(self.ploidy_starts))):
            raise ValueError("ploidy starts must be sorted and unique")
        if self.lambda_policy == "explicit" and self.lambda_values is None:
            raise ValueError("explicit lambda policy requires four values")
        if self.lambda_policy == "automatic" and self.lambda_values is not None:
            raise ValueError("automatic lambda policy cannot include values")
        if self.lambda_values is not None and any(
            value <= 0 for value in self.lambda_values
        ):
            raise ValueError("lambda values must be positive")
        return self


def centromere_mask_bins(
    bins: Sequence[CanonicalBin],
    intervals: Sequence[CentromereInterval],
    flank_bp: int,
) -> set[CanonicalBin]:
    """Bins ichorCNA removes as centromere or flank (the one rule for masks)."""

    return {
        bin_row
        for bin_row in bins
        for interval in intervals
        if bin_row.contig == interval.contig
        and bin_row.start < interval.end + flank_bp
        and bin_row.end > max(0, interval.start - flank_bp)
    }


def low_mappability_mask_bins(
    bins: Sequence[CanonicalBin],
    map_values: Sequence[float] | None,
    minimum_map_score: float,
    centromere_masks: set[CanonicalBin],
) -> set[CanonicalBin]:
    """Bins below the map-score threshold that are not already centromere masks."""

    return {
        bin_row
        for index, bin_row in enumerate(bins)
        if bin_row not in centromere_masks
        and map_values is not None
        and map_values[index] < minimum_map_score
    }


def masked_canonical_grid(
    bins: Sequence[CanonicalBin],
    *,
    contig_order: Sequence[str],
    intervals: Sequence[CentromereInterval],
    map_values: Sequence[float],
    parameters: IchorParameterSet,
    centromere_sha256: str,
    map_sha256: str,
) -> CanonicalGrid:
    """The canonical grid with the masks a bound request would carry.

    For a local run, which binds its inputs by digest without a full
    :class:`CnvRunRequest`: the masks follow the same two rules the request
    validator replays.
    """

    if len(map_values) != len(bins):
        raise ValueError("mappability values do not cover the grid")
    centromere = centromere_mask_bins(bins, intervals, parameters.centromere_flank_bp)
    low = low_mappability_mask_bins(
        bins, map_values, parameters.minimum_map_score, centromere
    )
    masks = tuple(
        CanonicalBinMask(
            bin=bin_row,
            reason="centromere_or_flank",
            source_artifact_sha256=centromere_sha256,
        )
        if bin_row in centromere
        else CanonicalBinMask(
            bin=bin_row,
            reason="low_mappability",
            source_artifact_sha256=map_sha256,
            source_value=map_values[index],
        )
        for index, bin_row in enumerate(bins)
        if bin_row in centromere or bin_row in low
    )
    return CanonicalGrid(
        contig_order=tuple(contig_order),
        bins=tuple(bins),
        masks=masks,
        bin_definition_sha256=_canonical_sha256(tuple(bins)),
    )


class CnvRunRequest(StrictModel):
    schema_version: Literal["traceback.ichor-request.v1"] = "traceback.ichor-request.v1"
    sample_id: Identifier
    input_bam: ArtifactIdentity
    input_bai: ArtifactIdentity
    runtime: RuntimeBinding
    assets: CnvAssetSet
    counting_policy: CountingPolicy
    raw_wig_lineage: RawWigLineage
    read_count_source: Literal["hmmcopy_utils_readcounter", "precomputed_bound_wig"]
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
            if (
                utilities is None
                or utilities.source_commit_sha1 != HMMCOPY_UTILS_COMMIT
            ):
                raise ValueError("hmmcopy_utils counter source is not pinned")
            if self.raw_wig_lineage.counter_implementation_sha256 not in {
                item.content_sha256 for item in utilities.invoked_files
            }:
                raise ValueError("counter lineage is not bound to hmmcopy_utils")
        has_pon = self.assets.panel_of_normals is not None
        if has_pon != (self.pon_mode == "protocol_matched_frozen"):
            raise ValueError("PoN mode and PoN asset presence disagree")
        if not all(
            (
                self.counting_policy.exclude_unmapped,
                self.counting_policy.exclude_secondary,
                self.counting_policy.exclude_supplementary,
                self.counting_policy.exclude_qc_failure,
                self.counting_policy.exclude_duplicate,
            )
        ):
            raise ValueError("v1 eligible-primary counting exclusions are fixed true")
        if self.counting_policy.terminal_bin_policy != "exclude_partial":
            raise ValueError("v1 ichor counting excludes partial terminal bins")
        if self.counting_policy.contigs != self.assets.canonical_grid.contig_order:
            raise ValueError("counting contigs do not match canonical grid order")
        expected_contigs = tuple(
            f"chr{chromosome}" for chromosome in self.parameters.chromosomes
        )
        if expected_contigs != self.counting_policy.contigs:
            raise ValueError("model chromosomes do not match counting contigs")
        centromere_masks = {
            item.bin
            for item in self.assets.canonical_grid.masks
            if item.reason == "centromere_or_flank"
        }
        expected_centromere_masks = centromere_mask_bins(
            self.assets.canonical_grid.bins,
            self.assets.centromere.canonical_intervals,
            self.parameters.centromere_flank_bp,
        )
        if centromere_masks != expected_centromere_masks:
            raise ValueError(
                "centromere masks do not match the bound intervals and flank"
            )
        map_values = (
            self.assets.mappability.canonical_values
            if self.assets.mappability is not None
            else None
        )
        expected_low_masks = low_mappability_mask_bins(
            self.assets.canonical_grid.bins,
            map_values,
            self.parameters.minimum_map_score,
            expected_centromere_masks,
        )
        low_masks = {
            item.bin
            for item in self.assets.canonical_grid.masks
            if item.reason == "low_mappability"
        }
        if low_masks != expected_low_masks:
            raise ValueError(
                "low-mappability masks do not match the bound map values and threshold"
            )
        bin_positions = {
            bin_row: index
            for index, bin_row in enumerate(self.assets.canonical_grid.bins)
        }
        for mask in self.assets.canonical_grid.masks:
            if mask.reason == "centromere_or_flank":
                if mask.source_artifact_sha256 != (
                    self.assets.centromere.identity.content_sha256
                ):
                    raise ValueError("centromere mask is not bound to its source asset")
            else:
                if self.assets.mappability is None:
                    raise ValueError("low-mappability mask requires a map asset")
                if mask.source_artifact_sha256 != (
                    self.assets.mappability.identity.content_sha256
                ):
                    raise ValueError(
                        "low-mappability mask is not bound to its source asset"
                    )
                assert mask.source_value is not None
                expected_value = map_values[bin_positions[mask.bin]]
                if mask.source_value != expected_value:
                    raise ValueError(
                        "low-mappability mask does not match its map value"
                    )
                if not mask.source_value < self.parameters.minimum_map_score:
                    raise ValueError(
                        "low-mappability mask does not satisfy the pinned threshold"
                    )
        return self


class OutputAvailability(StrEnum):
    DIRECTLY_EMITTED = "directly_emitted"
    REQUIRED_INPUT = "required_input"
    OPAQUE_RDATA = "opaque_rdata"
    # Emitted upstream but never opened, hashed or kept (a local run's .RData:
    # it is not byte-stable across runs, so nothing may bind its bytes).
    EMITTED_NOT_READ = "emitted_not_read"
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
            OutputAvailability.EMITTED_NOT_READ,
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
        if self.environment:
            raise ValueError("v1 prepared environment is fixed empty")
        roles = [item.role for item in self.output_capabilities]
        if len(roles) != len(set(roles)):
            raise ValueError("output capability roles must be unique")
        if self.argv != _expected_argv(self.request):
            raise ValueError("prepared argv does not match the bound request")
        if self.output_capabilities != _output_capabilities(self.request.sample_id):
            raise ValueError(
                "prepared output capabilities do not match pinned upstream"
            )
        return self


def _r_vector(values: Sequence[int | float]) -> str:
    return "c(" + ",".join(format(value, ".15g") for value in values) + ")"


def _output_capabilities(
    sample_id: str, *, read_workspace: bool = True
) -> tuple[OutputCapability, ...]:
    """What the pinned upstream emits; a local run never reads the .RData."""

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
            availability="opaque_rdata" if read_workspace else "emitted_not_read",
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


class IchorPaths(StrictModel):
    """Where the driver reads its inputs and writes its outputs."""

    counts_wig: str
    gc_wig: str
    map_wig: str | None
    centromere: str
    panel_of_normals: str | None
    out_dir: str

    @model_validator(mode="after")
    def absolute_paths(self) -> IchorPaths:
        for value in (
            self.counts_wig,
            self.gc_wig,
            self.map_wig,
            self.centromere,
            self.panel_of_normals,
            self.out_dir,
        ):
            if value is None:
                continue
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or chr(0) in value:
                raise ValueError("ichor paths must be absolute and safe")
        return self


# The container layout the prepared (OCI) run binds.
CONTAINER_PATHS = IchorPaths(
    counts_wig="/input/counts.wig",
    gc_wig="/assets/gc.wig",
    map_wig="/assets/map.wig",
    centromere="/assets/centromere.tsv",
    panel_of_normals="/assets/pon.rds",
    out_dir="/attempt",
)


def _expected_argv(request: CnvRunRequest) -> tuple[str, ...]:
    paths = CONTAINER_PATHS.model_copy(
        update={
            "map_wig": CONTAINER_PATHS.map_wig
            if request.assets.mappability is not None
            else None,
            "panel_of_normals": CONTAINER_PATHS.panel_of_normals
            if request.assets.panel_of_normals is not None
            else None,
        }
    )
    return (
        "Rscript",
        request.runtime.ichor_script_path,
        *ichor_driver_arguments(
            request.parameters,
            sample_id=request.sample_id,
            genome_build=request.assets.reference.assembly,
            paths=paths,
        ),
    )


def upstream_call(copy_number: int) -> str:
    """The call the pinned ichorCNA writes for an HMM copy-number state."""

    if copy_number < 0:
        raise ValueError("copy number cannot be negative")
    if copy_number < len(_UPSTREAM_CALLS):
        return _UPSTREAM_CALLS[copy_number]
    return f"HLAMP{copy_number - len(_UPSTREAM_CALLS) + 2}"


def allowed_states(params: IchorParameterSet) -> frozenset[tuple[int, str, bool]]:
    """Every (copy number, call, subclone status) the locked run can emit.

    ichorCNA 0.5.1's HMM states are copy numbers ``0..maxCN`` (``1..maxCN``
    without HOMD), plus the subclonal copy numbers when subclonal states are
    enabled; each segment's call is a fixed function of its copy number.  A
    segment outside this set was not produced under the locked parameters.
    """

    clonal = range(0 if INCLUDE_HOMD else 1, params.max_copy_number + 1)
    subclonal = SUBCLONAL_COPY_STATES if params.include_subclonal_states else ()
    return frozenset(
        {(value, upstream_call(value), False) for value in clonal}
        | {(value, upstream_call(value), True) for value in subclonal}
    )


def ichor_driver_arguments(
    params: IchorParameterSet,
    *,
    sample_id: str,
    genome_build: str,
    paths: IchorPaths,
) -> tuple[str, ...]:
    """The driver's flags after the script path: one argv for every target.

    The prepared (OCI) run passes :data:`CONTAINER_PATHS`; a local run passes
    its private staging paths.  Only the paths differ.
    """

    argv = [
        "--id",
        sample_id,
        "--WIG",
        paths.counts_wig,
        "--gcWig",
        paths.gc_wig,
        "--centromere",
        paths.centromere,
        "--normal",
        _r_vector(params.normal_fraction_starts),
        "--ploidy",
        _r_vector(params.ploidy_starts),
        "--maxCN",
        str(params.max_copy_number),
        "--estimateScPrevalence",
        "TRUE" if params.include_subclonal_states else "FALSE",
        "--scStates",
        _r_vector(SUBCLONAL_COPY_STATES) if params.include_subclonal_states else "NULL",
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
        genome_build,
        "--genomeStyle",
        "UCSC",
        "--includeHOMD",
        "TRUE" if INCLUDE_HOMD else "FALSE",
        "--outDir",
        paths.out_dir,
    ]
    if paths.map_wig is not None:
        argv.extend(("--mapWig", paths.map_wig))
    if paths.panel_of_normals is not None:
        argv.extend(("--normalPanel", paths.panel_of_normals))
    return tuple(argv)


def prepare_ichor_run(request: CnvRunRequest) -> PreparedIchorRun:
    """Create a deterministic argv plan without executing external software."""

    return PreparedIchorRun(
        request=request,
        request_sha256=_canonical_sha256(request),
        argv=_expected_argv(request),
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
    corrected_log2: FiniteFloat | None

    @model_validator(mode="after")
    def increasing(self) -> CorrectedBin:
        if self.end <= self.start:
            raise ValueError("corrected bin end must exceed start")
        return self


class CorrectedBinStatus(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    status: Literal["retained", "masked_prespecified"]
    mask_reason: str | None = None
    corrected_log2: FiniteFloat | None = None

    @model_validator(mode="after")
    def coherent_status(self) -> CorrectedBinStatus:
        if self.end <= self.start:
            raise ValueError("bin-status end must exceed start")
        if self.status == "retained" and self.mask_reason is not None:
            raise ValueError("retained bin cannot have a mask reason")
        if self.status == "masked_prespecified" and (
            self.corrected_log2 is not None or self.mask_reason is None
        ):
            raise ValueError("masked bin requires a reason and no value")
        return self


class CnaSegment(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    native_span_bin_count: int = Field(gt=0)
    retained_bin_count: int = Field(gt=0)
    median_log2: FiniteFloat
    copy_number: int = Field(ge=0)
    call: str = Field(min_length=1, max_length=64)
    subclone_status: bool

    @model_validator(mode="after")
    def increasing(self) -> CnaSegment:
        if self.end <= self.start:
            raise ValueError("segment end must exceed start")
        return self


class CnaBinEvent(StrictModel):
    contig: Identifier
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    event: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def increasing(self) -> CnaBinEvent:
        if self.end <= self.start:
            raise ValueError("CNA-bin end must exceed start")
        return self


class IdentifiabilityEvidence(StrictModel):
    largest_altered_segment: tuple[Identifier, int, int] | None
    largest_altered_segment_retained_overlap: int = Field(ge=0)
    altered_training_bin_count: int = Field(ge=0)
    total_training_bin_count: int = Field(gt=0)
    altered_training_fraction: FiniteFloat = Field(ge=0, le=1)
    minimum_segment_bins: int = Field(gt=0)
    altered_fraction_threshold: FiniteFloat = Field(ge=0, le=1)
    force_zero_condition: bool

    @model_validator(mode="after")
    def arithmetic(self) -> IdentifiabilityEvidence:
        expected_fraction = (
            self.altered_training_bin_count / self.total_training_bin_count
        )
        if not math.isclose(
            self.altered_training_fraction,
            expected_fraction,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("altered training fraction arithmetic is inconsistent")
        expected_force = (
            self.largest_altered_segment_retained_overlap <= self.minimum_segment_bins
            and self.altered_training_fraction <= self.altered_fraction_threshold
        )
        if self.force_zero_condition != expected_force:
            raise ValueError("force-zero evidence arithmetic is inconsistent")
        return self


class CandidateSolution(StrictModel):
    candidate_id: Identifier
    initial_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    initial_ploidy: FiniteFloat = Field(gt=0)
    estimated_normal_fraction: FiniteFloat = Field(ge=0, le=1)
    model_fraction: FiniteFloat = Field(ge=0, le=1)
    estimated_ploidy: FiniteFloat = Field(gt=0)
    bic: None = None
    fraction_genome_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    fraction_cna_subclonal: FiniteFloat | None = Field(default=None, ge=0, le=1)
    log_likelihood: FiniteFloat


class SelectedSolution(StrictModel):
    sample_id: Identifier
    model_fraction: FiniteFloat = Field(ge=0, le=1)
    ploidy: FiniteFloat = Field(gt=0)
    matched_candidate_id: Identifier | None
    selection_resolution: Literal[
        "resolved_unique_rounded_match", "not_resolved_rounded_collision"
    ]


class CnvDevelopmentResult(StrictModel):
    schema_version: Literal["traceback.ichor-development-result.v1"] = (
        "traceback.ichor-development-result.v1"
    )
    status: Literal["complete", "insufficient_information"]
    qualification_status: Literal["development_unqualified"] = "development_unqualified"
    development_input_authorized: Literal[True] = True
    product_release_authorized: Literal[False] = False
    request_sha256: Sha256
    pon_mode: Literal["none_development", "protocol_matched_frozen"]
    canonical_grid: CanonicalGrid
    parameters: IchorParameterSet
    corrected_bins: tuple[CorrectedBin, ...]
    bin_statuses: tuple[CorrectedBinStatus, ...]
    segments: tuple[CnaSegment, ...]
    bin_events: tuple[CnaBinEvent, ...]
    candidates: tuple[CandidateSolution, ...]
    selected_solution: SelectedSolution
    identifiability_evidence: IdentifiabilityEvidence
    identifiability: Literal[
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
        expected_initializations = _expected_candidate_initializations(self.parameters)
        observed_initializations = tuple(
            f"n{format(item.initial_normal_fraction, '.15g')}"
            f"-p{format(item.initial_ploidy, '.15g')}"
            for item in self.candidates
        )
        expected_ids = tuple(
            value.replace("-", ".") for value in expected_initializations
        )
        if observed_initializations != expected_initializations or tuple(ids) != (
            expected_ids
        ):
            raise ValueError("candidates do not match the bound parameter grid")
        selected = self.selected_solution.matched_candidate_id
        if selected is not None and selected not in ids:
            raise ValueError("selected candidate is not present")
        resolved = self.selected_solution.selection_resolution.startswith("resolved")
        if resolved != (selected is not None):
            raise ValueError("selected candidate and resolution disagree")
        matches = _selection_matches(self.selected_solution, self.candidates)
        if not matches:
            raise ValueError("selected summary contradicts every candidate")
        expected_selected = matches[0] if len(matches) == 1 else None
        expected_resolution = (
            "resolved_unique_rounded_match"
            if len(matches) == 1
            else "not_resolved_rounded_collision"
        )
        if selected != expected_selected or (
            self.selected_solution.selection_resolution != expected_resolution
        ):
            raise ValueError("selected solution does not match rounding replay")
        artifact_roles = [item.role for item in self.artifacts]
        if artifact_roles != sorted(artifact_roles) or len(artifact_roles) != len(
            set(artifact_roles)
        ):
            raise ValueError("output artifacts must be uniquely sorted by role")
        sample_id = self.selected_solution.sample_id
        if self.output_capabilities not in (
            _output_capabilities(sample_id),
            _output_capabilities(sample_id, read_workspace=False),
        ):
            raise ValueError("result output capabilities do not match pinned upstream")
        emitted = {
            item.role: item.relative_path
            for item in self.output_capabilities
            if item.relative_path is not None
            and item.availability != OutputAvailability.EMITTED_NOT_READ
        }
        artifacts = {item.role: item.relative_path for item in self.artifacts}
        if artifacts != emitted:
            raise ValueError("result artifacts do not match emitted capability paths")
        status_by_key = {
            (item.contig, item.start, item.end): item for item in self.bin_statuses
        }
        if len(status_by_key) != len(self.bin_statuses):
            raise ValueError("bin statuses must be unique")
        corrected_by_key = {
            (item.contig, item.start, item.end): item for item in self.corrected_bins
        }
        if len(corrected_by_key) != len(self.corrected_bins):
            raise ValueError("corrected bins must be unique")
        expected_keys = {
            (item.contig, item.start, item.end) for item in self.canonical_grid.bins
        }
        if set(status_by_key) != expected_keys:
            raise ValueError("bin statuses do not cover the canonical grid")
        ordered_status_keys = [
            (item.contig, item.start, item.end) for item in self.bin_statuses
        ]
        ordered_grid_keys = [
            (item.contig, item.start, item.end) for item in self.canonical_grid.bins
        ]
        if ordered_status_keys != ordered_grid_keys:
            raise ValueError("bin statuses do not follow canonical grid order")
        masks = {
            (item.bin.contig, item.bin.start, item.bin.end): item.reason
            for item in self.canonical_grid.masks
        }
        for key, status in status_by_key.items():
            expected_mask = masks.get(key)
            if expected_mask is None:
                if status.status != "retained" or status.mask_reason is not None:
                    raise ValueError("bin status disagrees with canonical masks")
            elif (
                status.status != "masked_prespecified"
                or status.mask_reason != expected_mask
            ):
                raise ValueError("bin status disagrees with canonical masks")
        retained = {
            key: item
            for key, item in status_by_key.items()
            if item.status == "retained"
        }
        if set(corrected_by_key) != set(retained):
            raise ValueError("corrected bins disagree with retained statuses")
        for key, corrected in corrected_by_key.items():
            if retained[key].corrected_log2 != corrected.corrected_log2:
                raise ValueError("corrected value disagrees with retained status")
        insufficient = _replay_segment_structure(
            self.canonical_grid,
            self.bin_statuses,
            self.segments,
            self.parameters,
            self.bin_events,
        )
        if self.identifiability_evidence != insufficient:
            raise ValueError("identifiability evidence does not match semantic replay")
        if (
            insufficient.force_zero_condition
            and self.selected_solution.model_fraction != 0
        ):
            raise ValueError(
                "structurally insufficient result has nonzero model fraction"
            )
        expected_status = (
            "insufficient_information"
            if insufficient.force_zero_condition
            else "complete"
        )
        expected_identifiability = (
            "insufficient_altered_structure"
            if insufficient.force_zero_condition
            else "not_assessed"
        )
        if (
            self.status != expected_status
            or self.identifiability != expected_identifiability
        ):
            raise ValueError("status and identifiability do not match semantic replay")
        return self


class IchorOutputError(ValueError):
    """Pinned upstream output is missing, unsafe, or semantically inconsistent."""


def _safe_output(path: Path) -> tuple[str, int]:
    try:
        metadata = path.lstat()
    except OSError:
        raise IchorOutputError(f"missing required output: {path.name}") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise IchorOutputError(f"output must be a regular non-symlink: {path.name}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_OUTPUT_BYTES:
        raise IchorOutputError(f"output size is invalid: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise IchorOutputError(f"output could not be read: {path.name}") from None
    return digest.hexdigest(), metadata.st_size


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
    except (UnicodeError, OSError, csv.Error):
        raise IchorOutputError("output text could not be parsed") from None
    if not rows or len(rows) > MAX_ROWS:
        raise IchorOutputError(f"output row count is invalid: {path.name}")
    return rows


def _finite(value: object, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise IchorOutputError(f"{label} is not numeric") from None
    if not math.isfinite(parsed):
        raise IchorOutputError(f"{label} must be finite")
    return parsed


def _integer(value: str, label: str) -> int:
    parsed = _finite(value, label)
    if not parsed.is_integer():
        raise IchorOutputError(f"{label} must be an integer")
    return int(parsed)


# ichorCNA reads coordinates as R integers (``as.integer``): plain decimal
# digits, at most .Machine$integer.max.
R_INTEGER_MAX = 2**31 - 1
_R_DECIMAL = re.compile(r"[0-9]{1,10}")


# A plain decimal number: a strict subset of what R's ``as.numeric`` reads,
# so a value accepted here is never NA in R (no "_", hex, "NA" or "Inf").
_R_DECIMAL_NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]{1,3})?")
# Values read.delim treats as missing (its default na.strings).
_R_NA_STRINGS = frozenset({"NA"})


def _r_number(value: str, label: str) -> float:
    if not _R_DECIMAL_NUMBER.fullmatch(value):
        raise IchorOutputError(f"{label} is not a plain decimal number")
    return _finite(value, label)


def _r_integer(value: str | None, label: str) -> int:
    if value is None or not _R_DECIMAL.fullmatch(value) or int(value) > R_INTEGER_MAX:
        raise IchorOutputError(f"{label} is not an R integer (plain digits, at most 2^31-1)")
    return int(value)


CENTROMERE_COLUMNS = ("Chr", "Start", "End", "GapType")
CENTROMERE_GAP_TYPE = "centromere"


def parse_centromere_table(path: Path) -> tuple[CentromereInterval, ...]:
    """Parse the headered ichorCNA centromere table into zero-based intervals.

    The one parser of this file: :func:`validate_centromere_table` and
    method-asset registration (``traceback_runner.references``) both call it.
    One-based closed ``Start``/``End`` become zero-based half-open intervals.
    """

    _safe_output(path)
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if tuple(reader.fieldnames or ()) != CENTROMERE_COLUMNS:
                raise IchorOutputError("centromere table columns are invalid")
            intervals: list[CentromereInterval] = []
            for row_number, row in enumerate(reader, start=1):
                if row_number > MAX_ROWS:
                    raise IchorOutputError("centromere table has too many rows")
                if None in row or any(value is None for value in row.values()):
                    raise IchorOutputError("centromere table contains a malformed row")
                if row["GapType"] != CENTROMERE_GAP_TYPE:
                    raise IchorOutputError(
                        "centromere table contains an unsupported gap type"
                    )
                if row["Chr"] in _R_NA_STRINGS:
                    raise IchorOutputError("centromere chromosome is missing (NA)")
                start_one_based = _r_integer(row["Start"], "centromere start")
                if start_one_based <= 0:
                    raise IchorOutputError(
                        "centromere start must be one-based positive"
                    )
                intervals.append(
                    CentromereInterval(
                        contig=row["Chr"],
                        start=start_one_based - 1,
                        end=_r_integer(row["End"], "centromere end"),
                    )
                )
    except IchorOutputError:
        raise
    except (UnicodeError, OSError, csv.Error, KeyError, ValidationError):
        raise IchorOutputError("centromere table could not be parsed") from None
    parsed = tuple(intervals)
    if not parsed:
        raise IchorOutputError("centromere table is empty")
    keys = [(row.contig, row.start, row.end) for row in parsed]
    if len(keys) != len(set(keys)):
        raise IchorOutputError("centromere table repeats an interval")
    return parsed


def validate_centromere_table(
    path: Path,
    binding: CentromereTableBinding,
) -> tuple[CentromereInterval, ...]:
    """Validate the pinned headered table and replay its GRanges conversion."""

    if (
        binding.required_columns != CENTROMERE_COLUMNS
        or binding.required_gap_type != CENTROMERE_GAP_TYPE
    ):
        raise IchorOutputError("centromere binding names an unsupported table layout")
    parsed = parse_centromere_table(path)
    if parsed != binding.canonical_intervals:
        raise IchorOutputError("centromere table does not match its canonical binding")
    return parsed


_WIG_HEADER = re.compile(
    r"fixedStep chrom=(\S+) start=([1-9][0-9]{0,17}) step=([1-9][0-9]{0,17}) "
    r"span=([1-9][0-9]{0,17})"
)


class ParsedFixedStepWig(StrictModel):
    """A fixed-step WIG as HMMcopy reads it, on the canonical zero-based grid."""

    role: WigRole
    bin_size_bp: int = Field(gt=0)
    grid: CanonicalGrid
    values: tuple[FiniteFloat, ...]


def parse_fixed_step_wig(path: Path, role: WigRole) -> ParsedFixedStepWig:
    """Parse one ``fixedStep`` WIG for ``role`` and apply the v1 WIG rules.

    Every header is exactly ``fixedStep chrom=C start=N step=N span=N`` in that
    order (HMMcopy reads the fields by position); every other line is one
    finite value.  Bins follow ``start1_to_zero_based_half_open`` and must form
    a :class:`CanonicalGrid`; :func:`check_wig_grid` applies the role's rules
    (map values within zero and one; GC binds no values).
    """

    _safe_output(path)
    bins: list[CanonicalBin] = []
    values: list[float] = []
    contigs: list[str] = []
    step_size: int | None = None
    span_size: int | None = None
    position: int | None = None
    contig: str | None = None
    block_values = -1  # values in the current block; -1 before any header
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if line_number > MAX_ROWS:
                    raise IchorOutputError("WIG has too many lines")
                line = raw.rstrip("\n")
                if line.startswith("fixedStep"):
                    match = _WIG_HEADER.fullmatch(line)
                    if match is None:
                        raise IchorOutputError(
                            "WIG header is not 'fixedStep chrom= start= step= span=' "
                            "in that order"
                        )
                    contig = match.group(1)
                    start, step, span = (
                        _r_integer(match.group(index), "WIG header value")
                        for index in (2, 3, 4)
                    )
                    if step_size is not None and (step, span) != (step_size, span_size):
                        raise IchorOutputError("WIG blocks use different step or span")
                    step_size, span_size = step, span
                    position = start - 1
                    if contig not in contigs:
                        contigs.append(contig)
                    if block_values == 0:
                        raise IchorOutputError("WIG has a fixedStep block with no values")
                    block_values = 0
                    continue
                if position is None or contig is None or step_size is None:
                    raise IchorOutputError("WIG has a value before any fixedStep header")
                values.append(_r_number(line, "WIG value"))
                block_values += 1
                if position + (span_size or 0) > R_INTEGER_MAX:
                    raise IchorOutputError("WIG bin end exceeds the R integer range")
                bins.append(
                    CanonicalBin(
                        contig=contig, start=position, end=position + (span_size or 0)
                    )
                )
                position += step_size
    except IchorOutputError:
        raise
    except (UnicodeError, OSError, ValueError):
        raise IchorOutputError("WIG could not be parsed") from None
    if step_size is None or span_size is None or not values or block_values == 0:
        raise IchorOutputError("WIG has no fixedStep block with values")
    try:
        grid = CanonicalGrid(
            contig_order=tuple(contigs),
            bins=tuple(bins),
            bin_definition_sha256=_canonical_sha256(bins),
        )
        check_wig_grid(
            role,
            span_bp=span_size,
            step_bp=step_size,
            bin_size_bp=step_size,
            canonical_values=tuple(values) if role == "map_wig" else None,
        )
        return ParsedFixedStepWig(
            role=role, bin_size_bp=step_size, grid=grid, values=tuple(values)
        )
    except ValidationError as exc:
        raise IchorOutputError(
            f"WIG grid is invalid: {exc.errors()[0]['msg']}"
        ) from None
    except ValueError as exc:
        raise IchorOutputError(f"WIG is invalid for {role}: {exc}") from None


def _optional_fraction(value: str, label: str) -> float | None:
    if value.strip().upper() in {"NA", "NAN"}:
        return None
    parsed = _finite(value, label)
    if not 0 <= parsed <= 1:
        raise IchorOutputError(f"{label} must be within zero and one")
    return parsed


def _optional_native_na(value: object, label: str) -> float | None:
    if not isinstance(value, str):
        raise IchorOutputError(f"{label} is missing")
    if value == "NA":
        return None
    return _finite(value, label)


def _exact_native_na(value: object, label: str) -> None:
    if value != "NA":
        raise IchorOutputError(f"{label} must be exact native NA")


def _parse_corrected(
    path: Path,
    grid: CanonicalGrid,
) -> tuple[tuple[CorrectedBin, ...], tuple[CorrectedBinStatus, ...]]:
    expected_order = {
        (row.contig, row.start, row.end): index for index, row in enumerate(grid.bins)
    }
    masks = {
        (item.bin.contig, item.bin.start, item.bin.end): item.reason
        for item in grid.masks
    }
    result: list[CorrectedBin] = []
    observed_keys: list[tuple[str, int, int]] = []
    for row in _rows(path):
        try:
            contig = row["chr"]
            start = _integer(row["start"], "corrected start") - 1
            end = _integer(row["end"], "corrected end")
            raw_value = row["log2_TNratio_corrected"]
        except KeyError:
            raise IchorOutputError("corrected-depth columns are invalid") from None
        key = (contig, start, end)
        if key not in expected_order:
            raise IchorOutputError("corrected-depth row is outside the canonical grid")
        if key in observed_keys:
            raise IchorOutputError("corrected-depth rows must be unique")
        observed_keys.append(key)
        if key in masks:
            raise IchorOutputError("upstream emitted a row for a removed masked bin")
        value = _optional_native_na(raw_value, "corrected log2")
        result.append(
            CorrectedBin(contig=contig, start=start, end=end, corrected_log2=value)
        )
    positions = [expected_order[key] for key in observed_keys]
    if positions != sorted(positions):
        raise IchorOutputError("corrected-depth rows violate declared contig order")
    retained_keys = {(row.contig, row.start, row.end) for row in result}
    expected_retained = set(expected_order) - set(masks)
    missing = expected_retained - retained_keys
    if missing:
        raise IchorOutputError("corrected-depth output has unexplained missing bins")
    retained_values = {
        (item.contig, item.start, item.end): item.corrected_log2 for item in result
    }
    statuses = tuple(
        CorrectedBinStatus(
            contig=row.contig,
            start=row.start,
            end=row.end,
            status=(
                "masked_prespecified"
                if (row.contig, row.start, row.end) in masks
                else "retained"
            ),
            mask_reason=masks.get((row.contig, row.start, row.end)),
            corrected_log2=retained_values.get((row.contig, row.start, row.end)),
        )
        for row in grid.bins
    )
    return tuple(result), statuses


def _parse_segments(
    path: Path,
    sample_id: str,
    grid: CanonicalGrid,
    corrected: Sequence[CorrectedBin],
) -> tuple[CnaSegment, ...]:
    result: list[CnaSegment] = []
    for row in _rows(path):
        try:
            if row["ID"] != sample_id:
                raise IchorOutputError("segment sample ID mismatch")
            subclone = row["subclone.status"].upper()
            if subclone not in {"TRUE", "FALSE"}:
                raise IchorOutputError("segment subclone status is invalid")
            start = _integer(row["start"], "segment start") - 1
            end = _integer(row["end"], "segment end")
            retained_count = sum(
                item.contig == row["chrom"] and item.start >= start and item.end <= end
                for item in corrected
            )
            result.append(
                CnaSegment(
                    contig=row["chrom"],
                    start=start,
                    end=end,
                    native_span_bin_count=_integer(
                        row["num.mark"], "segment native span bin count"
                    ),
                    retained_bin_count=retained_count,
                    median_log2=_finite(row["seg.median.logR"], "segment median"),
                    copy_number=_integer(row["copy.number"], "segment copy number"),
                    call=row["call"],
                    subclone_status=subclone == "TRUE",
                )
            )
        except KeyError:
            raise IchorOutputError("segment columns are invalid") from None
    order = {contig: index for index, contig in enumerate(grid.contig_order)}
    if any(row.contig not in order for row in result):
        raise IchorOutputError("segment references an undeclared contig")
    keys = [(row.contig, row.start, row.end) for row in result]
    if keys != sorted(keys, key=lambda row: (order[row[0]], row[1], row[2])):
        raise IchorOutputError("segments violate declared contig order")
    previous: dict[str, int] = {}
    for row in result:
        if row.start < previous.get(row.contig, 0):
            raise IchorOutputError("segments cannot overlap")
        previous[row.contig] = row.end
    return tuple(result)


def _parse_bin_events(
    path: Path,
    sample_id: str,
    grid: CanonicalGrid,
) -> tuple[CnaBinEvent, ...]:
    expected_order = {
        (row.contig, row.start, row.end): index for index, row in enumerate(grid.bins)
    }
    removed = {(item.bin.contig, item.bin.start, item.bin.end) for item in grid.masks}
    event_column = f"{sample_id}.event"
    result: list[CnaBinEvent] = []
    for row in _rows(path):
        try:
            key = (
                row["chr"],
                _integer(row["start"], "CNA-bin start") - 1,
                _integer(row["end"], "CNA-bin end"),
            )
            raw_event = row[event_column].strip()
        except KeyError:
            raise IchorOutputError("bin-level CNA columns are invalid") from None
        if key not in expected_order or key in removed:
            raise IchorOutputError("bin-level CNA row is outside its analysis grid")
        result.append(
            CnaBinEvent(
                contig=key[0],
                start=key[1],
                end=key[2],
                event=(None if raw_event.upper() in {"NA", "NAN", ""} else raw_event),
            )
        )
    keys = [(row.contig, row.start, row.end) for row in result]
    if len(keys) != len(set(keys)):
        raise IchorOutputError("bin-level CNA rows must be unique")
    if [expected_order[key] for key in keys] != sorted(
        expected_order[key] for key in keys
    ):
        raise IchorOutputError("bin-level CNA rows violate declared contig order")
    expected = set(expected_order) - removed
    if set(keys) != expected:
        raise IchorOutputError("bin-level CNA output has missing rows")
    return tuple(result)


def _parse_init(value: str) -> tuple[float, float]:
    if not value.startswith("n") or "-p" not in value:
        raise IchorOutputError("candidate init field is invalid")
    normal, ploidy = value[1:].split("-p", 1)
    return _finite(normal, "initial normal"), _finite(ploidy, "initial ploidy")


def _expected_candidate_initializations(
    parameters: IchorParameterSet,
) -> tuple[str, ...]:
    return tuple(
        f"n{format(normal, '.15g')}-p{format(ploidy, '.15g')}"
        for normal in parameters.normal_fraction_starts
        for ploidy in parameters.ploidy_starts
        if not (normal == 0.95 and ploidy != 2)
    )


def _significant(value: float, digits: int) -> float:
    if value == 0:
        return 0.0
    places = digits - 1 - math.floor(math.log10(abs(value)))
    return round(value, places)


def _selection_matches(
    selected: SelectedSolution,
    candidates: Sequence[CandidateSolution],
) -> tuple[str, ...]:
    return tuple(
        item.candidate_id
        for item in candidates
        if _significant(item.estimated_normal_fraction, 2)
        == _significant(1 - selected.model_fraction, 2)
        and _significant(item.estimated_ploidy, 4) == _significant(selected.ploidy, 4)
    )


# The candidate table header: the pinned script's, or run_ichorCNA()'s at
# v0.5.1 (starting values in two columns, no BIC).
_CANDIDATE_HEADERS = (
    [
        "init",
        "n_est",
        "phi_est",
        "BIC",
        "Frac_genome_subclonal",
        "Frac_CNA_subclonal",
        "loglik",
    ],
    [
        "n_0",
        "phi_0",
        "n_est",
        "phi_est",
        "Frac_genome_subclonal",
        "Frac_CNA_subclonal",
        "loglik",
    ],
)


def _parse_params(
    path: Path,
    sample_id: str,
    parameters: IchorParameterSet,
) -> tuple[SelectedSolution, tuple[CandidateSolution, ...]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeError, OSError):
        raise IchorOutputError("parameter output could not be parsed") from None
    if len(lines) < 3:
        raise IchorOutputError("parameter output is truncated")
    selected_header = lines[0].split("\t")
    selected_values = lines[1].split("\t")
    if (
        selected_header != ["Sample", "Tumor Fraction", "Ploidy"]
        or len(selected_values) != 3
    ):
        raise IchorOutputError("selected parameter summary is invalid")
    if selected_values[0] != sample_id:
        raise IchorOutputError("selected parameter sample ID mismatch")
    selected_fraction = _finite(selected_values[1], "selected model fraction")
    selected_ploidy = _finite(selected_values[2], "selected ploidy")
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.split("\t") in _CANDIDATE_HEADERS
        ),
        None,
    )
    if header_index is None:
        raise IchorOutputError("candidate table is absent")
    candidate_header = lines[header_index].split("\t")
    candidates: list[CandidateSolution] = []
    observed_initializations: list[str] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        values = line.split("\t")
        if len(values) != len(candidate_header):
            raise IchorOutputError("candidate row has the wrong number of fields")
        row = dict(zip(candidate_header, values, strict=True))
        if "init" not in row:
            # run_ichorCNA() 0.5.1 writes the starting values as two columns
            # and no BIC column (it computes none).
            row["init"] = (
                f"n{format(_finite(row['n_0'], 'initial normal'), '.15g')}"
                f"-p{format(_finite(row['phi_0'], 'initial ploidy'), '.15g')}"
            )
            row["BIC"] = "NA"
        observed_initializations.append(row["init"])
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
                bic=_exact_native_na(row["BIC"], "candidate BIC"),
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
    if tuple(observed_initializations) != _expected_candidate_initializations(
        parameters
    ):
        raise IchorOutputError(
            "candidate initializations do not match the bound request"
        )
    provisional = SelectedSolution(
        sample_id=sample_id,
        model_fraction=selected_fraction,
        ploidy=selected_ploidy,
        matched_candidate_id=None,
        selection_resolution="not_resolved_rounded_collision",
    )
    matched = _selection_matches(provisional, candidates)
    if not matched:
        raise IchorOutputError(
            "selected summary contradicts every rounding-compatible candidate"
        )
    return (
        SelectedSolution(
            sample_id=sample_id,
            model_fraction=selected_fraction,
            ploidy=selected_ploidy,
            matched_candidate_id=matched[0] if len(matched) == 1 else None,
            selection_resolution=(
                "resolved_unique_rounded_match"
                if len(matched) == 1
                else "not_resolved_rounded_collision"
            ),
        ),
        tuple(candidates),
    )


def _replay_segment_structure(
    grid: CanonicalGrid,
    statuses: Sequence[CorrectedBinStatus],
    segments: Sequence[CnaSegment],
    parameters: IchorParameterSet,
    bin_events: Sequence[CnaBinEvent],
) -> IdentifiabilityEvidence:
    grid_bins = grid.bins
    order = {contig: index for index, contig in enumerate(grid.contig_order)}
    segment_keys = [(item.contig, item.start, item.end) for item in segments]
    if any(item.contig not in order for item in segments) or segment_keys != sorted(
        segment_keys, key=lambda item: (order[item[0]], item[1], item[2])
    ):
        raise ValueError("segments violate the canonical grid order")
    previous_end: dict[str, int] = {}
    for segment in segments:
        if segment.start < previous_end.get(segment.contig, 0):
            raise ValueError("segments overlap")
        previous_end[segment.contig] = segment.end
    removed = {(item.bin.contig, item.bin.start, item.bin.end) for item in grid.masks}
    expected_event_keys = [
        (item.contig, item.start, item.end)
        for item in grid_bins
        if (item.contig, item.start, item.end) not in removed
    ]
    event_keys = [(item.contig, item.start, item.end) for item in bin_events]
    if event_keys != expected_event_keys:
        raise ValueError("bin-level CNA events do not match the analysis grid")
    retained_keys = {
        (item.contig, item.start, item.end)
        for item in statuses
        if item.status == "retained"
    }
    coverage = {key: 0 for key in retained_keys}
    # Retained bins without a corrected value (native NA) carry no data to
    # segment: run_ichorCNA() 0.5.1 leaves such bins at a segment's edge out of
    # every segment.  They may be covered at most once; every bin with a value
    # exactly once.
    unvalued = {
        (item.contig, item.start, item.end)
        for item in statuses
        if item.status == "retained" and item.corrected_log2 is None
    }
    neutral_calls = {"NEUT", "NEUTRAL"}
    altered_segments: list[CnaSegment] = []
    for segment in segments:
        span_bins = [
            row
            for row in grid_bins
            if row.contig == segment.contig
            and row.start >= segment.start
            and row.end <= segment.end
        ]
        if (
            not span_bins
            or span_bins[0].start != segment.start
            or span_bins[-1].end != segment.end
            or len(span_bins) != segment.native_span_bin_count
        ):
            raise ValueError("segment native span does not match canonical bins")
        retained_in_segment = [
            row
            for row in span_bins
            if (row.contig, row.start, row.end) in retained_keys
        ]
        if len(retained_in_segment) != segment.retained_bin_count:
            raise ValueError("segment retained count does not match bin statuses")
        for row in retained_in_segment:
            coverage[(row.contig, row.start, row.end)] += 1
        if segment.call.upper() not in neutral_calls:
            altered_segments.append(segment)
    if any(
        value > 1 or (value == 0 and key not in unvalued) for key, value in coverage.items()
    ):
        raise ValueError("segments do not cover every retained bin exactly once")

    largest = (
        max(altered_segments, key=lambda item: item.end - item.start)
        if altered_segments
        else None
    )
    largest_key = (
        (largest.contig, largest.start, largest.end) if largest is not None else None
    )
    max_valid_overlap = largest.retained_bin_count if largest is not None else 0

    training = {f"chr{chromosome}" for chromosome in parameters.chromosomes}
    training_events = [item for item in bin_events if item.contig in training]
    if not training_events:
        raise ValueError("bin-level CNA output has no training-chromosome rows")
    altered_count = sum(
        item.event is not None and item.event.upper() not in neutral_calls
        for item in training_events
    )
    total_count = len(training_events)
    altered_fraction = altered_count / total_count
    return IdentifiabilityEvidence(
        largest_altered_segment=largest_key,
        largest_altered_segment_retained_overlap=max_valid_overlap,
        altered_training_bin_count=altered_count,
        total_training_bin_count=total_count,
        altered_training_fraction=altered_fraction,
        minimum_segment_bins=parameters.minimum_segment_bins,
        altered_fraction_threshold=parameters.altered_fraction_threshold,
        force_zero_condition=(
            max_valid_overlap <= parameters.minimum_segment_bins
            and altered_fraction <= parameters.altered_fraction_threshold
        ),
    )


def validate_ichor_outputs(
    prepared: PreparedIchorRun,
    output_directory: Path,
) -> CnvDevelopmentResult:
    """Parse only files the pinned upstream emits; never execute or deserialize RData."""

    return _validate_output_directory(
        output_directory,
        sample_id=prepared.request.sample_id,
        grid=prepared.request.assets.canonical_grid,
        parameters=prepared.request.parameters,
        pon_mode=prepared.request.pon_mode,
        request_sha256=prepared.request_sha256,
        output_capabilities=prepared.output_capabilities,
    )


def validate_local_ichor_outputs(
    output_directory: Path,
    *,
    sample_id: str,
    grid: CanonicalGrid,
    parameters: IchorParameterSet,
    pon_mode: Literal["none_development", "protocol_matched_frozen"],
    run_sha256: str,
) -> CnvDevelopmentResult:
    """Validate a local run's outputs with the same parsers and replay.

    A local run binds its inputs by digest (``run_sha256``) instead of a full
    :class:`CnvRunRequest`.  Its ``.RData`` is never opened, hashed or kept:
    the workspace is not byte-stable across runs.
    """

    return _validate_output_directory(
        output_directory,
        sample_id=sample_id,
        grid=grid,
        parameters=parameters,
        pon_mode=pon_mode,
        request_sha256=run_sha256,
        output_capabilities=_output_capabilities(sample_id, read_workspace=False),
    )


def _validate_output_directory(
    output_directory: Path,
    *,
    sample_id: str,
    grid: CanonicalGrid,
    parameters: IchorParameterSet,
    pon_mode: Literal["none_development", "protocol_matched_frozen"],
    request_sha256: str,
    output_capabilities: tuple[OutputCapability, ...],
) -> CnvDevelopmentResult:
    read_workspace = all(
        item.availability != OutputAvailability.EMITTED_NOT_READ
        for item in output_capabilities
    )
    try:
        output_metadata = output_directory.lstat()
    except OSError:
        raise IchorOutputError("output directory is absent") from None
    if not stat.S_ISDIR(output_metadata.st_mode) or output_directory.is_symlink():
        raise IchorOutputError("output directory is absent")
    emitted = [
        item
        for item in output_capabilities
        if item.relative_path is not None
        and item.availability != OutputAvailability.EMITTED_NOT_READ
    ]
    allowed = {item.relative_path for item in emitted}
    try:
        observed = {
            path.name for path in output_directory.iterdir() if path.name in allowed
        }
    except OSError:
        raise IchorOutputError("output directory could not be read") from None
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
        corrected, bin_statuses = _parse_corrected(
            output_directory / str(role_paths["combined_corrected_depth"]),
            grid,
        )
        segments = _parse_segments(
            output_directory / str(role_paths["segments_detailed"]),
            sample_id,
            grid,
            corrected,
        )
        bin_events = _parse_bin_events(
            output_directory / str(role_paths["bin_level_cna"]),
            sample_id,
            grid,
        )
        selected, candidates = _parse_params(
            output_directory / str(role_paths["parameters_and_candidates"]),
            sample_id,
            parameters,
        )
    except IchorOutputError:
        raise
    except (ValidationError, ValueError):
        raise IchorOutputError(
            "upstream output violates the adapter contract"
        ) from None
    # One rule for every segment state: copy number, call and subclone status
    # must be a state the locked parameters let the HMM emit (maxCN, HOMD,
    # subclonal states).  A parameter change moves the set, not this check.
    permitted = allowed_states(parameters)
    if any(
        (item.copy_number, item.call, item.subclone_status) not in permitted
        for item in segments
    ):
        raise IchorOutputError(
            "segment state is outside the states the locked parameters permit"
        )
    try:
        identifiability_evidence = _replay_segment_structure(
            grid,
            bin_statuses,
            segments,
            parameters,
            bin_events,
        )
    except ValueError:
        raise IchorOutputError("upstream structural evidence is inconsistent") from None
    if identifiability_evidence.force_zero_condition and selected.model_fraction != 0:
        raise IchorOutputError(
            "pinned force-zero evidence contradicts the selected model fraction"
        )
    if identifiability_evidence.force_zero_condition:
        status: Literal["complete", "insufficient_information"] = (
            "insufficient_information"
        )
        identifiability = "insufficient_altered_structure"
    else:
        status = "complete"
        identifiability = "not_assessed"
    limitations = [
        "Pinned ichorCNA development output; not analytically qualified.",
        "Model fraction is conditional on copy-state, ploidy, and parameter assumptions.",
        "Separate GC-only, map-only, and PoN-residual stages are not emitted upstream.",
        (
            "RData is retained by digest but is not deserialized by this parser."
            if read_workspace
            else "RData is neither read nor retained."
        ),
        "Raw .seg is retained by digest; v1 parses .seg.txt and bin-level .cna.seg.",
    ]
    if any(item.corrected_log2 is None for item in corrected):
        limitations.append(
            "Native retained bins with unavailable corrected log2 values are "
            "represented as null, never as zero or a prespecified mask."
        )
    if any(item.bic is None for item in candidates):
        limitations.append(
            "Native unavailable candidate BIC values are represented as null; "
            "no finite selection score is claimed."
        )
    if pon_mode == "none_development":
        limitations.append("No protocol-matched panel of normals was supplied.")
    try:
        return CnvDevelopmentResult(
            status=status,
            request_sha256=request_sha256,
            pon_mode=pon_mode,
            canonical_grid=grid,
            parameters=parameters,
            corrected_bins=corrected,
            bin_statuses=bin_statuses,
            segments=segments,
            bin_events=bin_events,
            candidates=candidates,
            selected_solution=selected,
            identifiability_evidence=identifiability_evidence,
            identifiability=identifiability,
            output_capabilities=output_capabilities,
            artifacts=tuple(sorted(artifacts, key=lambda item: item.role)),
            limitations=tuple(limitations),
        )
    except (ValidationError, ValueError):
        raise IchorOutputError(
            "upstream output violates the adapter contract"
        ) from None


__all__ = [
    "HMMCOPY_COMMIT",
    "HMMCOPY_UTILS_COMMIT",
    "CENTROMERE_COLUMNS",
    "CENTROMERE_GAP_TYPE",
    "ICHOR_BIOCONDA_BUILD",
    "ICHOR_BIOCONDA_MODIFICATION_MANIFEST_SHA256",
    "ICHOR_BIOCONDA_PATCH_SHA256",
    "ICHOR_COMMIT",
    "ICHOR_SOURCE_ARCHIVE_SHA256",
    "ICHOR_VERSION",
    "CanonicalBin",
    "CanonicalBinMask",
    "CanonicalGrid",
    "CentromereInterval",
    "CentromereTableBinding",
    "CnaBinEvent",
    "CnaSegment",
    "CnvAssetSet",
    "CnvDevelopmentResult",
    "CnvRunRequest",
    "CountingPolicy",
    "ExternalComponentBinding",
    "IchorOutputError",
    "IchorParameterSet",
    "IchorPaths",
    "CONTAINER_PATHS",
    "INCLUDE_HOMD",
    "IdentifiabilityEvidence",
    "PanelOfNormalsBinding",
    "ParsedFixedStepWig",
    "PreparedIchorRun",
    "RawWigLineage",
    "ReferenceFastaBinding",
    "RuntimeBinding",
    "SUBCLONAL_COPY_STATES",
    "WigGridBinding",
    "allowed_states",
    "centromere_mask_bins",
    "check_wig_grid",
    "ichor_driver_arguments",
    "low_mappability_mask_bins",
    "masked_canonical_grid",
    "contract_sha256",
    "parse_centromere_table",
    "parse_fixed_step_wig",
    "prepare_ichor_run",
    "upstream_call",
    "validate_centromere_table",
    "validate_ichor_outputs",
    "validate_local_ichor_outputs",
]
