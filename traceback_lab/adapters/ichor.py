"""Lab-owned copy-number adapter over the existing readCounter + ichorCNA toolchain (D14).

The signed ``CopyNumberAnalysis._measure`` reads prior-stage files and renews
a worker lease inside readCounter and ichorCNA, so it cannot run without a
job; the frozen signed contract also cannot take a panel of normals.  This
adapter composes only public lower-level functions (``write_counting_bam``,
``readcounter_arguments`` via the signed parameters, ``run_contained``,
``run_isolated_r`` and the ``ichor_adapter`` parsers, grid, driver arguments
and output validation), runs with no lease, and takes a counting BAM directly
so synthetic spikes and downsampled inputs reuse it.

Parameters are the **Lab CN contract** ``traceback.lab.copy-number-parameters.v1``:
the signed ``CopyNumberParametersV1`` plus ``pon_policy`` (``none`` |
``lab_loo_v1``) and the PoN asset sha256.  A PoN switches the adapter's
``protocol_matched_frozen`` mode.

A parity test keeps ``map_result`` equal to the signed ``build_measurement``
(tumour fraction, ploidy, segments, bins) in ``none`` mode.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from traceback_runner.serialization import canonical_json_bytes

CONTRACT = "traceback.lab.copy-number-parameters.v1"
SAMPLE_ID = "sample"
PonPolicy = Literal["none", "lab_loo_v1"]


class CopyNumberError(RuntimeError):
    """The adapter failed; the message is safe to show (no paths)."""


@dataclass(frozen=True)
class IchorAssets:
    gc_wig: Path
    map_wig: Path
    centromere: Path


@dataclass(frozen=True)
class LabCopyNumberParameters:
    signed: Any  # CopyNumberParametersV1
    pon_policy: PonPolicy = "none"
    pon_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.pon_policy == "none":
            if self.pon_sha256 is not None:
                raise ValueError("no-PoN parameters cannot carry a PoN digest")
        elif self.pon_policy == "lab_loo_v1":
            if not isinstance(self.pon_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.pon_sha256) is None:
                raise ValueError("lab_loo_v1 requires a lowercase PoN sha256")
        else:
            raise ValueError("unknown PoN policy")

    def contract(self) -> dict[str, Any]:
        return {
            "schema": CONTRACT,
            "signed": self.signed.model_dump(mode="json"),
            "pon_policy": self.pon_policy,
            "pon_sha256": self.pon_sha256,
        }

    @property
    def pon_mode(self) -> Literal["none_development", "protocol_matched_frozen"]:
        return "none_development" if self.pon_policy == "none" else "protocol_matched_frozen"


def default_parameters(*, min_counted_reads: int | None = None) -> LabCopyNumberParameters:
    from traceback_runner.copy_number_method import default_parameters as signed_defaults

    signed = signed_defaults()
    if min_counted_reads is not None:
        signed = signed.model_copy(update={"min_counted_reads": min_counted_reads})
    return LabCopyNumberParameters(signed=signed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def stage_verified_pon(source: Path, destination: Path, expected_sha256: str) -> None:
    """Copy a selected PoN into contained scratch and verify the bytes R will read."""

    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise CopyNumberError("selected PoN digest is invalid")
    try:
        if source.is_symlink() or not source.is_file():
            raise CopyNumberError("selected PoN asset is not a regular file")
        shutil.copyfile(source, destination)
        if _sha256_file(destination) != expected_sha256:
            raise CopyNumberError("selected PoN asset failed digest verification")
    except OSError as exc:
        raise CopyNumberError("selected PoN asset could not be read") from exc


def write_counting(source_bam: Path, destination: Path, parameters: LabCopyNumberParameters) -> tuple[int, Any, int]:
    """The stripped counting BAM (position, MAPQ, CIGAR only) via the signed helper."""

    from traceback_runner.copy_number import write_counting_bam

    signed = parameters.signed
    return write_counting_bam(source_bam, destination, signed.counting_contigs, min_mapq=signed.min_mapq)


def read_counter(toolchain: Any, parameters: LabCopyNumberParameters, counting_bam: Path, output: Path) -> None:
    from traceback_runner.contained_process import ContainedProcessError, run_contained
    from traceback_runner.copy_number_method import ARGV_COUNTING_BAM

    signed = parameters.signed
    argv = [
        str(toolchain.readcounter),
        *(str(counting_bam) if item == ARGV_COUNTING_BAM else item for item in signed.readcounter_arguments),
    ]
    try:
        result = run_contained(
            argv,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            cwd=counting_bam.parent,
            timeout_seconds=signed.readcounter_timeout_seconds,
            should_abort=None,
            stdout_path=output,
            stdout_limit_bytes=64 * 1024 * 1024,
        )
    except ContainedProcessError as exc:
        raise CopyNumberError("readCounter could not start") from exc
    if not result.succeeded or result.stdout_truncated:
        raise CopyNumberError(
            "readCounter "
            + (
                "timed out"
                if result.outcome == "timeout"
                else "wrote too much output"
                if result.stdout_truncated
                else f"exited with status {result.returncode}"
            )
        )


def analysis_grid(
    parameters: LabCopyNumberParameters, assets: IchorAssets, counts_wig: Path, counted_reads: int
) -> tuple[Any, str]:
    """The masked canonical grid of the counted contigs and the run digest
    (the signed ``_grid`` logic, without a method definition)."""

    from evidence_inspector.ichor_adapter import (
        IchorOutputError,
        masked_canonical_grid,
        parse_centromere_table,
        parse_fixed_step_wig,
    )

    signed = parameters.signed
    try:
        gc = parse_fixed_step_wig(assets.gc_wig, "gc_wig")
        mappability = parse_fixed_step_wig(assets.map_wig, "map_wig")
        intervals = parse_centromere_table(assets.centromere)
        counts = parse_fixed_step_wig(counts_wig, "raw_counts_wig")
    except IchorOutputError as exc:
        raise CopyNumberError(f"an ichorCNA input does not parse: {exc}") from exc
    wanted = set(signed.counting_contigs)
    keep = [index for index, row in enumerate(gc.grid.bins) if row.contig in wanted]
    bins = [gc.grid.bins[index] for index in keep]
    if (
        gc.bin_size_bp != signed.bin_size_bp
        or tuple(mappability.grid.bins) != tuple(gc.grid.bins)
        or tuple(counts.grid.bins) != tuple(bins)
    ):
        raise CopyNumberError("readCounter's bins, the GC wig's and the mappability wig's are not one grid")
    total = sum(counts.values)
    if total != counted_reads:
        raise CopyNumberError(f"readCounter binned {int(total)} reads; the pre-filter counted {counted_reads}")
    order = tuple(contig for contig in signed.counting_contigs if any(row.contig == contig for row in bins))
    try:
        grid = masked_canonical_grid(
            bins,
            contig_order=order,
            intervals=intervals,
            map_values=[mappability.values[index] for index in keep],
            parameters=signed.ichor,
            centromere_sha256=_sha256_file(assets.centromere),
            map_sha256=_sha256_file(assets.map_wig),
        )
    except ValueError as exc:
        raise CopyNumberError(f"the analysis grid is invalid: {exc}") from exc
    run_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "traceback.lab.copy-number-run.v1",
                "contract": parameters.contract(),
                "read_counts_sha256": _sha256_file(counts_wig),
                "grid_sha256": grid.bin_definition_sha256,
            }
        )
    ).hexdigest()
    return grid, run_sha256


def run_ichor(
    toolchain: Any,
    parameters: LabCopyNumberParameters,
    assets: IchorAssets,
    counts_wig: Path,
    grid: Any,
    run_sha256: str,
    work: Path,
    *,
    pon: Path | None = None,
) -> Any:
    """ichorCNA through ``run_isolated_r``, validated by the adapter's replay."""

    from evidence_inspector.ichor_adapter import (
        IchorOutputError,
        IchorPaths,
        ichor_driver_arguments,
        validate_local_ichor_outputs,
    )
    from traceback_runner.r_isolation import RInvocation, RIsolationError, run_isolated_r

    if (pon is None) != (parameters.pon_policy == "none"):
        raise CopyNumberError("a panel of normals is given exactly when pon_policy is not none")
    signed = parameters.signed
    staging = work / "ichor-staging"
    staging.mkdir(mode=0o700)
    staged = {
        "counts": staging / "counts.wig",
        "gc": staging / "gc.wig",
        "map": staging / "map.wig",
        "centromere": staging / "centromere.tsv",
    }
    shutil.copyfile(counts_wig, staged["counts"])
    shutil.copyfile(assets.gc_wig, staged["gc"])
    shutil.copyfile(assets.map_wig, staged["map"])
    shutil.copyfile(assets.centromere, staged["centromere"])
    staged_pon = None
    if pon is not None:
        staged_pon = staging / "pon.rds"
        stage_verified_pon(pon, staged_pon, parameters.pon_sha256 or "")
    out_dir = staging / "out"
    arguments = ichor_driver_arguments(
        signed.ichor,
        sample_id=SAMPLE_ID,
        genome_build=signed.genome_build,
        paths=IchorPaths(
            counts_wig=str(staged["counts"]),
            gc_wig=str(staged["gc"]),
            map_wig=str(staged["map"]),
            centromere=str(staged["centromere"]),
            panel_of_normals=str(staged_pon) if staged_pon is not None else None,
            out_dir=str(out_dir),
        ),
    )
    try:
        run = run_isolated_r(
            RInvocation(
                rscript=toolchain.rscript,
                script=toolchain.driver,
                args=arguments,
                library_paths=(toolchain.r_library,),
                work_dir=staging,
                timeout_seconds=signed.ichor_timeout_seconds,
                seed=signed.r_seed,
                rscript_sha256=toolchain.installed.rscript_sha256,
                script_sha256=toolchain.installed.driver_sha256,
            ),
            should_abort=None,
        )
    except RIsolationError as exc:
        raise CopyNumberError("the ichorCNA toolchain changed since it was verified") from exc
    if run.libpaths_mismatch:
        raise CopyNumberError("the ichorCNA toolchain's R library paths are not the declared ones")
    if not run.succeeded:
        raise CopyNumberError(
            "ichorCNA timed out" if run.process.outcome == "timeout" else f"ichorCNA exited with status {run.process.returncode}"
        )
    try:
        return validate_local_ichor_outputs(
            out_dir,
            sample_id=SAMPLE_ID,
            grid=grid,
            parameters=signed.ichor,
            pon_mode=parameters.pon_mode,
            run_sha256=run_sha256,
        )
    except IchorOutputError as exc:
        raise CopyNumberError(f"the output failed validation: {exc}") from exc


@dataclass(frozen=True)
class CopyNumberResult:
    tumor_fraction: float | None
    ploidy: float
    identifiable: bool
    selection_resolved: bool
    pon_mode: str
    bins: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    counted_reads: int
    records_scanned: int


def map_result(result: Any, *, counted_reads: int, records_scanned: int) -> CopyNumberResult:
    """The one Lab mapper from a validated ichorCNA result to Lab fields."""

    masks = {(item.bin.contig, item.bin.start, item.bin.end): item.reason for item in result.canonical_grid.masks}
    bins = [
        {
            "chr": status.contig,
            "start": status.start,
            "end": status.end,
            "log2": status.corrected_log2,
            "mask": masks.get((status.contig, status.start, status.end)),
        }
        for status in result.bin_statuses
    ]
    segments = [
        {
            "chr": segment.contig,
            "start": segment.start,
            "end": segment.end,
            "n_bins": segment.native_span_bin_count,
            "median_log2": segment.median_log2,
            "copy_number": segment.copy_number,
            "call": segment.call,
        }
        for segment in result.segments
    ]
    selected = result.selected_solution
    identifiable = result.status == "complete"
    return CopyNumberResult(
        tumor_fraction=selected.model_fraction if identifiable else None,
        ploidy=selected.ploidy,
        identifiable=identifiable,
        selection_resolved=selected.selection_resolution == "resolved_unique_rounded_match",
        pon_mode=result.pon_mode,
        bins=bins,
        segments=segments,
        counted_reads=counted_reads,
        records_scanned=records_scanned,
    )


def measure(
    counting_bam: Path,
    counted_reads: int,
    records_scanned: int,
    work: Path,
    *,
    toolchain: Any,
    assets: IchorAssets,
    parameters: LabCopyNumberParameters,
    pon: Path | None = None,
    validate_grid: Callable[[str], None] | None = None,
) -> CopyNumberResult:
    """readCounter → grid → ichorCNA → validated → mapped.  ``work`` is scratch."""

    if counted_reads < parameters.signed.min_counted_reads:
        raise CopyNumberError(
            f"{counted_reads:,} counted reads, below the floor of {parameters.signed.min_counted_reads:,}"
        )
    counts_wig = work / "counts.wig"
    read_counter(toolchain, parameters, counting_bam, counts_wig)
    grid, run_sha256 = analysis_grid(parameters, assets, counts_wig, counted_reads)
    if validate_grid is not None:
        validate_grid(grid.bin_definition_sha256)
    result = run_ichor(toolchain, parameters, assets, counts_wig, grid, run_sha256, work, pon=pon)
    return map_result(result, counted_reads=counted_reads, records_scanned=records_scanned)


def resolve_assets(root: Path, toolchain: Any) -> IchorAssets:
    """The registered gc/map/centromere assets for this toolchain lock, re-verified by digest."""

    from traceback_runner.copy_number_method import copy_number_asset_files
    from traceback_runner.references import AssetKind, verify_registered_asset

    files = copy_number_asset_files(toolchain.identity.lock_sha256)
    paths: dict[str, Path] = {}
    for kind in (AssetKind.ICHOR_GC_WIG, AssetKind.ICHOR_MAP_WIG, AssetKind.ICHOR_CENTROMERE):
        _, asset_id = files[kind]
        loaded = verify_registered_asset(root, asset_id, kind=kind)
        paths[kind.value] = Path(loaded.source.file_path)
    return IchorAssets(
        gc_wig=paths[AssetKind.ICHOR_GC_WIG.value],
        map_wig=paths[AssetKind.ICHOR_MAP_WIG.value],
        centromere=paths[AssetKind.ICHOR_CENTROMERE.value],
    )


def asset_sha256s(assets: IchorAssets) -> Mapping[str, str]:
    return {
        "gc_wig": _sha256_file(assets.gc_wig),
        "map_wig": _sha256_file(assets.map_wig),
        "centromere": _sha256_file(assets.centromere),
    }


__all__ = [
    "CONTRACT",
    "CopyNumberError",
    "CopyNumberResult",
    "IchorAssets",
    "LabCopyNumberParameters",
    "analysis_grid",
    "asset_sha256s",
    "default_parameters",
    "map_result",
    "measure",
    "read_counter",
    "resolve_assets",
    "run_ichor",
    "write_counting",
]
