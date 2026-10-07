"""The locked copy-number method (signal methods CN2, CN3).

``mth_copy_number_ichorcna`` (family ``copy_number``) runs ichorCNA 0.5.1
from the pinned optional toolchain (CN1).  Every result is unqualified, local
and not for clinical use.

CN2 registers the three ichorCNA assets the method binds, read from the
installed package itself (``method-asset register --from-toolchain
copy-number``).  The bin size of the wigs is the locked method's: there is no
flag for it, so a registration can never disagree with the method that uses
it.

CN3 locks every parameter (:class:`CopyNumberParametersV1`) and builds the E01
definition.  Everything that can change a number enters the definition, so it
enters the method hash, the job's workflow hash and the record identity:

- ``tools[]``: package-level digests from the committed lock (the lock
  SHA-256, the r-ichorcna package, the Traceback driver, and the in-package
  ``readCounter`` and ``Rscript``).  Installed digests depend on the install
  path and go into each record's provenance, not here;
- ``assets[]``: the registered reference FASTA and the three registered
  ichorCNA assets, by their registration digests;
- ``parameter_schema_sha256``: the SHA-256 of the canonical parameters.

Threat model: in-process code mutation is out of scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_inspector.ichor_adapter import IchorParameterSet
from evidence_inspector.method_registry import (
    AssetReference,
    MethodDefinition,
    MethodFamily,
    ToolReference,
)

from .contracts import RegisteredReference, RunnerContract
from .references import (
    ICHOR_EXTDATA_RELPATH,
    ICHOR_TOOLCHAIN_KINDS,
    AssetKind,
    AssetRegistrationResult,
    RegisteredAsset,
    ichor_toolchain_files,
    register_ichor_toolchain_directory,
)
from .serialization import canonical_json_bytes

METHOD_ID = "mth_copy_number_ichorcna"
METHOD_SLUG = "copy-number-ichorcna"
QUANTITY_ID = "qty_copy_number_ichorcna_model_fraction"
UNIT = "unit_fraction"
ASSET_VERSION = "1.0.0"
PARAMETERS_SCHEMA = "traceback.copy-number-parameters.v1"
# The locked bin size (spec §3.2 and §11): 1 Mb, the bin size of the adapter's
# fixtures and tests.  500 kb is the alternative (Q4).
LOCKED_BIN_SIZE_BP = 1_000_000
AUTOSOMES = tuple(f"chr{number}" for number in range(1, 23))

# Floors and limits.  The counted-read floor is the spec's default (§3.2):
# about 0.1x at about 300 bp, the ichorCNA ultra-low-pass regime.  The
# synthetic fixtures carry far fewer reads, so tests pass their own floor; the
# scientist confirms it at gate G1.
DEFAULT_MIN_COUNTED_READS = 1_000_000
DEFAULT_READCOUNTER_TIMEOUT_SECONDS = 3_600
DEFAULT_ICHOR_TIMEOUT_SECONDS = 3_600
DEFAULT_R_SEED = 20261004

# The published lower limit is fixed text in the definition (spec §3.2).
STATED_LOWER_LIMIT_VALUE = 0.03
STATED_LOWER_LIMIT_BASIS = (
    "About 3% at about 0.1x short-read coverage with a reference panel (PoN) "
    "(Adalsteinsson et al., Nat Commun 2017); not established for this nanopore "
    "protocol."
)

PENDING_SCIENTIST_SIGNOFF = (
    # Spec default, never set from a real sample: confirmed at gate G1.
    "min_counted_reads",
    # Q1: no panel of normals until a protocol-matched one exists.
    "pon_mode",
)

# Placeholders in the locked readCounter argv (the job fills them).
ARGV_COUNTING_BAM = "{counting_bam}"


class CopyNumberParametersV1(RunnerContract):
    """Every locked parameter of the copy-number method (spec §3.2 and §11).

    Canonical JSON of this model is hashed into ``parameter_schema_sha256``.
    """

    schema_version: Literal["traceback.copy-number-parameters.v1"] = PARAMETERS_SCHEMA
    bin_size_bp: int = Field(gt=0)
    genome_build: Literal["hg38"] = "hg38"
    genome_style: Literal["UCSC"] = "UCSC"
    counting_contigs: tuple[str, ...] = Field(min_length=1)
    # The pysam pre-filter writes only eligible primary alignments (the
    # fragment policy's exclusions) into the job's counting BAM; readCounter
    # alone would also count supplementary alignments.
    prefilter_policy: Literal["pysam-eligible-primary-before-readcounter.v1"] = (
        "pysam-eligible-primary-before-readcounter.v1"
    )
    min_mapq: int = Field(ge=0, le=255)
    excluded_alignment_reasons: tuple[str, ...] = Field(min_length=1)
    readcounter_arguments: tuple[str, ...] = Field(min_length=1)
    # readCounter places an alignment in one bin by its start, and emits the
    # partial last bin of each contig; ichorCNA keeps it when its map score
    # passes.
    count_unit: Literal["eligible_primary_alignment_start"] = (
        "eligible_primary_alignment_start"
    )
    terminal_bin_policy: Literal["readcounter_emits_partial_terminal_bin"] = (
        "readcounter_emits_partial_terminal_bin"
    )
    ichor: IchorParameterSet
    pon_mode: Literal["none_development"] = "none_development"
    include_homd: Literal[False] = False
    min_counted_reads: int = Field(ge=1)
    index_floor_basis: Literal["bai_mapped_records_on_counting_contigs_upper_bound"] = (
        "bai_mapped_records_on_counting_contigs_upper_bound"
    )
    readcounter_timeout_seconds: int = Field(ge=1)
    ichor_timeout_seconds: int = Field(ge=1)
    r_seed: int = Field(gt=-(2**31), lt=2**31)
    r_workspace_policy: Literal["never_opened_never_signed"] = "never_opened_never_signed"
    stated_lower_limit_value: Literal[0.03] = STATED_LOWER_LIMIT_VALUE
    stated_lower_limit_basis: Literal[
        "About 3% at about 0.1x short-read coverage with a reference panel (PoN) "
        "(Adalsteinsson et al., Nat Commun 2017); not established for this nanopore "
        "protocol."
    ] = STATED_LOWER_LIMIT_BASIS

    @model_validator(mode="after")
    def coherent(self) -> CopyNumberParametersV1:
        expected = tuple(f"chr{number}" for number in self.ichor.chromosomes)
        if self.counting_contigs != expected:
            raise ValueError("counting contigs must be the modelled chromosomes, in order")
        return self


def readcounter_arguments(bin_size_bp: int, min_mapq: int, contigs: tuple[str, ...]) -> tuple[str, ...]:
    """readCounter's argv after the executable, the BAM as a placeholder."""

    return (
        "--window",
        str(bin_size_bp),
        "--quality",
        str(min_mapq),
        "--chromosome",
        ",".join(contigs),
        ARGV_COUNTING_BAM,
    )


def default_parameters() -> CopyNumberParametersV1:
    """The locked parameters (spec §3.2 table plus the §11 amendments)."""

    from evidence_inspector.cell_origin_prefilter import DEFAULT_MIN_MAPQ, PREFILTER_REASONS

    bin_size = locked_bin_size_bp()
    return CopyNumberParametersV1(
        bin_size_bp=bin_size,
        counting_contigs=AUTOSOMES,
        min_mapq=DEFAULT_MIN_MAPQ,
        excluded_alignment_reasons=tuple(reason.value for reason in PREFILTER_REASONS),
        readcounter_arguments=readcounter_arguments(bin_size, DEFAULT_MIN_MAPQ, AUTOSOMES),
        ichor=IchorParameterSet(
            chromosomes=tuple(range(1, 23)),
            normal_fraction_starts=(0.95, 0.99, 0.995, 0.999),
            ploidy_starts=(2.0,),
            max_copy_number=3,
            include_subclonal_states=False,
            lambda_policy="automatic",
            lambda_values=None,
            minimum_map_score=0.9,
            centromere_flank_bp=100_000,
            transition_probability=0.9999,
            transition_strength=10_000,
            minimum_segment_bins=50,
            altered_fraction_threshold=0.05,
        ),
        min_counted_reads=DEFAULT_MIN_COUNTED_READS,
        readcounter_timeout_seconds=DEFAULT_READCOUNTER_TIMEOUT_SECONDS,
        ichor_timeout_seconds=DEFAULT_ICHOR_TIMEOUT_SECONDS,
        r_seed=DEFAULT_R_SEED,
    )


def parameters_sha256(parameters: CopyNumberParametersV1) -> str:
    """``parameter_schema_sha256``: SHA-256 of the canonical parameter JSON."""

    return hashlib.sha256(canonical_json_bytes(parameters)).hexdigest()


def locked_bin_size_bp() -> int:
    """The bin size the locked copy-number method counts and models in."""

    return LOCKED_BIN_SIZE_BP


def toolchain_tag(lock_sha256: str) -> str:
    """The 12-hex tag of a toolchain lock that its asset IDs carry."""

    return lock_sha256[:12]


def copy_number_asset_files(
    lock_sha256: str, *, bin_size_bp: int | None = None
) -> dict[AssetKind, tuple[str, str]]:
    """Each ichorCNA kind's package file name and asset ID for this toolchain."""

    return ichor_toolchain_files(
        locked_bin_size_bp() if bin_size_bp is None else bin_size_bp,
        toolchain_tag(lock_sha256),
    )


def register_toolchain_assets(
    root: Path, *, toolchain: object | None = None
) -> tuple[AssetRegistrationResult, ...]:
    """Register the installed toolchain's gc wig, map wig and centromere table.

    ``toolchain`` is a resolved ``IchorToolchain``; without one the platform's
    toolchain is resolved and verified here (TBX-TOOL-002 when it is missing
    or changed).  The caller holds the workspace mutation lock.
    """

    if toolchain is None:
        from .toolchain import resolve_copy_number_toolchain

        toolchain = resolve_copy_number_toolchain()
    identity = toolchain.identity  # type: ignore[attr-defined]
    return register_ichor_toolchain_directory(
        root,
        toolchain.r_library / ICHOR_EXTDATA_RELPATH,  # type: ignore[attr-defined]
        bin_size_bp=locked_bin_size_bp(),
        toolchain_tag=toolchain_tag(identity.lock_sha256),
    )


def _package_version(package_url: str) -> str:
    """``4.4.3`` from ``.../r-base-4.4.3-h35b0bb1_11.conda``."""

    return package_url.rsplit("/", 1)[1].rsplit("-", 2)[1]


def _reference_asset(reference: RegisteredReference) -> AssetReference:
    from .local_authority import LOCAL_ASSET_VERSION, _local_asset_id

    # The same asset reference the fragment method binds, so every record of
    # one BAM names the registered FASTA identically.
    return AssetReference(
        asset_id=_local_asset_id(reference.reference_id),
        version=LOCAL_ASSET_VERSION,
        content_sha256=reference.asset_sha256,
    )


def copy_number_method_definition(
    reference: RegisteredReference,
    assets: Mapping[AssetKind, RegisteredAsset],
    pin: Any,
    parameters: CopyNumberParametersV1,
) -> MethodDefinition:
    """The locked E01 definition for one registered reference.

    ``assets`` holds the registration of each ichorCNA kind; ``pin`` is the
    platform's ``IchorPin``.
    """

    from .local_authority import local_method_version

    expected = copy_number_asset_files(pin.lock_sha256, bin_size_bp=parameters.bin_size_bp)
    bound = []
    for kind in ICHOR_TOOLCHAIN_KINDS:
        registered = assets.get(kind)
        if registered is None or registered.kind is not kind:
            raise ValueError("every ichorCNA asset kind must be registered")
        if registered.asset_id != expected[kind][1]:
            raise ValueError("an ichorCNA asset is not the one this toolchain registers")
        bound.append(
            AssetReference(
                asset_id=registered.asset_id,
                version=ASSET_VERSION,
                content_sha256=registered.file_sha256,
            )
        )
    tools = (
        ToolReference(
            tool_id="tool_ichorcna", version=pin.version, artifact_sha256=pin.ichorcna_package_sha256
        ),
        ToolReference(
            tool_id="tool_ichorcna_driver", version=pin.version, artifact_sha256=pin.driver_sha256
        ),
        ToolReference(
            tool_id="tool_ichorcna_lock", version=pin.version, artifact_sha256=pin.lock_sha256
        ),
        ToolReference(
            tool_id="tool_hmmcopy_bin_counter",
            version=_package_version(pin.readcounter.package_url),
            artifact_sha256=pin.readcounter.package_binary_sha256,
        ),
        ToolReference(
            tool_id="tool_rscript",
            version=_package_version(pin.rscript.package_url),
            artifact_sha256=pin.rscript.package_binary_sha256,
        ),
    )
    return MethodDefinition(
        method_id=METHOD_ID,
        version=local_method_version(reference.reference_id),
        family=MethodFamily.COPY_NUMBER,
        quantity_id=QUANTITY_ID,
        unit=UNIT,
        parameter_schema_sha256=parameters_sha256(parameters),
        tools=tuple(sorted(tools, key=lambda item: (item.tool_id, item.version))),
        assets=tuple(
            sorted(
                (_reference_asset(reference), *bound),
                key=lambda item: (item.asset_id, item.version),
            )
        ),
    )


def registered_copy_number_assets(
    root: Path, lock_sha256: str, *, bin_size_bp: int | None = None
) -> dict[AssetKind, RegisteredAsset]:
    """The registration of each ichorCNA kind for this toolchain.

    A missing registration raises TBX-ASSET-004 naming the
    ``--from-toolchain`` command.  Registrations are read, not re-hashed:
    every job hashes its own copy of each file before using it.
    """

    from .references import load_asset

    return {
        kind: load_asset(root, asset_id, kind=kind).registered
        for kind, (_, asset_id) in copy_number_asset_files(
            lock_sha256, bin_size_bp=bin_size_bp
        ).items()
    }


__all__ = [
    "ARGV_COUNTING_BAM",
    "AUTOSOMES",
    "DEFAULT_MIN_COUNTED_READS",
    "LOCKED_BIN_SIZE_BP",
    "METHOD_ID",
    "METHOD_SLUG",
    "PENDING_SCIENTIST_SIGNOFF",
    "QUANTITY_ID",
    "STATED_LOWER_LIMIT_BASIS",
    "STATED_LOWER_LIMIT_VALUE",
    "UNIT",
    "CopyNumberParametersV1",
    "copy_number_asset_files",
    "copy_number_method_definition",
    "default_parameters",
    "locked_bin_size_bp",
    "parameters_sha256",
    "readcounter_arguments",
    "register_toolchain_assets",
    "registered_copy_number_assets",
    "toolchain_tag",
]
