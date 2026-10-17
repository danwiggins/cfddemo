"""Build immutable, protocol-matched full and leave-one-out ichorCNA panels.

The caller holds the Lab writer lock and supplies validated full aligned inputs.
No cohort generation is selected here: consumers publish a generation only after
all member copy-number recomputations succeed (D17/D35).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import ichor
from .cohort_contracts import CohortGeneration, CohortPon
from .identity import SHA256_PATTERN
from .problems import LabProblem
from .store import LabStore, open_regular_no_follow

BUILDER_SOURCE_SHA256 = "64d67f0c6526d5de1fe914c83a5ab5dccf63140f23b0ad9f4260f9a282642e05"
BUILDER_SOURCE_SCRIPT = Path(__file__).parent / "pon_scripts" / "createPanelOfNormals.R"
BUILDER_SHA256 = "785e48ab18c15bb4c1168d8ac941843ea31e9cc1ba93c6437280b00671dffb5b"
BUILDER_SCRIPT = Path(__file__).parent / "pon_scripts" / "buildPanelLab.R"
VALIDATOR_SCRIPT = Path(__file__).parent / "pon_scripts" / "validatePanelOfNormals.R"
MIN_PON_N = 10


@dataclass(frozen=True)
class PonInput:
    source_id: str
    input_id: str
    reference_sha256: str
    aligner_identity: str
    bam_path: Path


@dataclass(frozen=True)
class PonBuild:
    source_ids: tuple[str, ...]
    full_asset_sha256: str
    loo_asset_sha256s: dict[str, str]
    reference_sha256: str
    aligner_identity: str
    grid_sha256: str
    counting_parameters_sha256: str
    toolchain_lock_sha256: str
    builder_source_sha256: str
    builder_sha256: str
    gc_sha256: str
    map_sha256: str
    centromere_sha256: str

    @property
    def pon_n(self) -> int:
        return len(self.source_ids)

    def provenance(self) -> CohortPon:
        return CohortPon(
            source_ids=self.source_ids, reference_sha256=self.reference_sha256,
            aligner_identity=self.aligner_identity, grid_sha256=self.grid_sha256,
            counting_parameters_sha256=self.counting_parameters_sha256,
            toolchain_lock_sha256=self.toolchain_lock_sha256,
            builder_source_sha256=self.builder_source_sha256, builder_sha256=self.builder_sha256,
            gc_sha256=self.gc_sha256, map_sha256=self.map_sha256,
            centromere_sha256=self.centromere_sha256,
        )


@dataclass(frozen=True)
class PonSelection:
    cohort_id: str
    cohort_version: str
    pon_policy: str
    pon_sha256: str
    path: Path
    pon_n: int
    reference_sha256: str
    aligner_identity: str
    grid_sha256: str
    counting_parameters_sha256: str
    toolchain_lock_sha256: str
    gc_sha256: str
    map_sha256: str
    centromere_sha256: str
    panel_source_ids: tuple[str, ...]
    target_excluded: bool


def _problem(cause: str, fix: str) -> LabProblem:
    return LabProblem("TBX-LAB-020", cause=cause, fix=fix, data={"field": "panel of normals"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _run_r(toolchain: Any, script: Path, script_sha256: str, args: tuple[str, ...], work: Path) -> None:
    from traceback_runner.r_isolation import RInvocation, RIsolationError, run_isolated_r

    try:
        result = run_isolated_r(
            RInvocation(
                rscript=toolchain.rscript,
                script=script,
                args=args,
                library_paths=(toolchain.r_library,),
                work_dir=work,
                timeout_seconds=toolchain_timeout(),
                rscript_sha256=toolchain.installed.rscript_sha256,
                script_sha256=script_sha256,
            ),
            should_abort=None,
        )
    except RIsolationError as exc:
        raise _problem("the pinned PoN R process could not start", "Repair the ichor toolchain and retry cohort build") from exc
    if result.libpaths_mismatch or not result.succeeded:
        raise _problem("the PoN R process failed or timed out", "Inspect the toolchain and panel inputs, then retry cohort build")


def toolchain_timeout() -> int:
    return ichor.default_parameters().signed.ichor_timeout_seconds


def _count_wig(
    item: PonInput, work: Path, toolchain: Any, assets: ichor.IchorAssets,
    parameters: ichor.LabCopyNumberParameters,
) -> tuple[Path, str] | None:
    from .algorithms.copy_number import MIN_ELIGIBLE_READS

    try:
        with os.fdopen(open_regular_no_follow(item.bam_path), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("aligned input is not a regular file")
    except OSError as exc:
        raise _problem("a PoN aligned input is missing or unsafe", "Restore the full aligned input") from exc
    work.mkdir(mode=0o700)
    counting = work / "counting.bam"
    _, _, counted = ichor.write_counting(item.bam_path, counting, parameters)
    if counted < MIN_ELIGIBLE_READS:
        return None
    wig = work / f"{item.source_id}.wig"
    ichor.read_counter(toolchain, parameters, counting, wig)
    counting.unlink()
    grid, _ = ichor.analysis_grid(parameters, assets, wig, counted)
    return wig, grid.bin_definition_sha256


def _create_panel(
    toolchain: Any, work: Path, wig_paths: tuple[Path, ...],
    assets: ichor.IchorAssets, label: str,
) -> Path:
    panel_dir = work / label
    panel_dir.mkdir(mode=0o700)
    filelist = panel_dir / "filelist.txt"
    filelist.write_text("".join(f"{path}\n" for path in wig_paths), encoding="utf-8")
    prefix = panel_dir / "panel"
    _run_r(
        toolchain, BUILDER_SCRIPT, BUILDER_SHA256,
        (
            "--filelist", str(filelist), "--gcWig", str(assets.gc_wig),
            "--mapWig", str(assets.map_wig), "--centromere", str(assets.centromere),
            "--outfile", str(prefix),
        ),
        panel_dir,
    )
    panel = panel_dir / "panel_median.rds"
    if not panel.is_file() or panel.is_symlink():
        raise _problem("the PoN builder did not produce a regular RDS panel", "Inspect the toolchain and retry cohort build")
    return panel


def _validate_panel(toolchain: Any, panel: Path, source_ids: tuple[str, ...], full: Path | None) -> None:
    _run_r(
        toolchain, VALIDATOR_SCRIPT, _sha256(VALIDATOR_SCRIPT),
        (str(panel), str(full) if full is not None else "-", *source_ids),
        panel.parent,
    )


def _publish_panel(parent: Path, source: Path) -> str:
    try:
        with os.fdopen(open_regular_no_follow(source), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("panel output is not a regular file")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise _problem("the PoN output is missing or unsafe", "Retry the cohort build") from exc
    destination = parent / f"{digest}.rds"
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        try:
            with os.fdopen(open_regular_no_follow(destination), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("panel asset is not a regular file")
                existing = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise _problem("an existing PoN asset is unsafe", "Restore the immutable panel asset before retrying") from exc
        if existing != digest:
            raise _problem("an existing PoN asset has changed", "Restore the immutable panel asset before retrying")
    descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return digest


def build_panels(store: LabStore, inputs: tuple[PonInput, ...]) -> PonBuild:
    """Build full and LOO panels; leave selection to the cohort transaction.

    Every output is content-addressed. A failed call can leave unused complete
    blobs, but cannot change a previously selected cohort generation.
    """

    if len(inputs) < MIN_PON_N or len({item.source_id for item in inputs}) != len(inputs):
        raise _problem("the PoN needs at least ten distinct sources", "Admit ten independent protocol-matched samples")
    for item in inputs:
        if (not SHA256_PATTERN.fullmatch(item.source_id)
                or not SHA256_PATTERN.fullmatch(item.input_id)
                or not SHA256_PATTERN.fullmatch(item.reference_sha256)
                or not item.aligner_identity):
            raise _problem("the PoN input identity is invalid", "Restore the intact Lab input manifest")
        if item.bam_path != store.inputs_dir / item.input_id / "aligned.bam":
            raise _problem("the PoN input path is outside its stored full input", "Use the admitted Lab full input")
    ordered = tuple(sorted(inputs, key=lambda item: item.source_id))
    references = {item.reference_sha256 for item in ordered}
    aligners = {item.aligner_identity for item in ordered}
    if len(references) != 1 or len(aligners) != 1:
        raise _problem("the PoN inputs do not share reference and aligner identity", "Use one protocol-matched cohort")
    if (_sha256(BUILDER_SOURCE_SCRIPT) != BUILDER_SOURCE_SHA256
            or _sha256(BUILDER_SCRIPT) != BUILDER_SHA256):
        raise _problem("a pinned PoN builder script changed", "Restore the packaged builder scripts")
    from traceback_runner.references import ReferenceProblem
    from traceback_runner.toolchain import ToolchainProblem, resolve_copy_number_toolchain

    try:
        toolchain = resolve_copy_number_toolchain()
        assets = ichor.resolve_assets(store.root, toolchain)
    except (OSError, ValueError, RuntimeError, ToolchainProblem, ReferenceProblem) as exc:
        raise _problem("the ichor toolchain or registered assets are unavailable", "Install ichor and register its method assets") from exc
    parameters = ichor.default_parameters()
    from traceback_runner.copy_number_method import parameters_sha256 as signed_parameters_sha256

    counting_parameters_sha256 = signed_parameters_sha256(parameters.signed)
    asset_hashes = ichor.asset_sha256s(assets)
    parent = store.lab / "assets" / "pon"
    if (store.lab.is_symlink() or parent.parent.is_symlink() or parent.is_symlink()):
        raise _problem("the PoN asset directory is unsafe", "Restore the regular Lab asset directory")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=parent) as temporary:
        work = Path(temporary)
        staged_assets = ichor.IchorAssets(
            gc_wig=work / "gc.wig", map_wig=work / "map.wig", centromere=work / "centromere.tsv",
        )
        for source, target in (
            (assets.gc_wig, staged_assets.gc_wig),
            (assets.map_wig, staged_assets.map_wig),
            (assets.centromere, staged_assets.centromere),
        ):
            shutil.copyfile(source, target)
        if ichor.asset_sha256s(staged_assets) != asset_hashes:
            raise _problem("a registered PoN reference asset changed during copying", "Restore the registered method assets")
        wig_by_source: dict[str, Path] = {}
        grid_sha256: str | None = None
        for item in ordered:
            counted = _count_wig(item, work / item.source_id, toolchain, staged_assets, parameters)
            if counted is None:
                continue
            wig, grid = counted
            if grid_sha256 is not None and grid != grid_sha256:
                raise _problem("the PoN inputs do not share one analysis grid", "Rebuild inputs on one reference and bin grid")
            grid_sha256 = grid
            wig_by_source[item.source_id] = wig
        source_ids = tuple(wig_by_source)
        if len(source_ids) < MIN_PON_N or grid_sha256 is None:
            raise _problem("fewer than ten PoN inputs pass the copy-number depth gate", "Admit more full-depth healthy samples")
        full = _create_panel(toolchain, work, tuple(wig_by_source.values()), staged_assets, "full")
        _validate_panel(toolchain, full, source_ids, None)
        loo: dict[str, Path] = {}
        for excluded in source_ids:
            included = tuple(source_id for source_id in source_ids if source_id != excluded)
            panel = _create_panel(
                toolchain, work, tuple(wig_by_source[source_id] for source_id in included),
                staged_assets, f"loo-{excluded}",
            )
            _validate_panel(toolchain, panel, included, full)
            loo[excluded] = panel
        full_digest = _publish_panel(parent, full)
        loo_digests = {source_id: _publish_panel(parent, panel) for source_id, panel in loo.items()}
    return PonBuild(
        source_ids=source_ids, full_asset_sha256=full_digest, loo_asset_sha256s=loo_digests,
        reference_sha256=ordered[0].reference_sha256,
        aligner_identity=ordered[0].aligner_identity,
        grid_sha256=grid_sha256, counting_parameters_sha256=counting_parameters_sha256,
        toolchain_lock_sha256=toolchain.identity.lock_sha256,
        builder_source_sha256=BUILDER_SOURCE_SHA256, builder_sha256=BUILDER_SHA256,
        gc_sha256=asset_hashes["gc_wig"], map_sha256=asset_hashes["map_wig"],
        centromere_sha256=asset_hashes["centromere"],
    )


def binding_for(
    store: LabStore, generation: CohortGeneration, source_id: str, *,
    reference_sha256: str, aligner_identity: str, toolchain_lock_sha256: str,
    counting_parameters_sha256: str, gc_sha256: str, map_sha256: str, centromere_sha256: str,
    grid_sha256: str | None = None,
) -> PonSelection:
    """Select from one pinned generation, checking protocol and immutable bytes.

    A caller may defer the grid check until readCounter has produced the exact
    grid, but must call ``require_grid(selection, actual)`` before ichorCNA.
    """

    full = generation.pon_asset_sha256
    if full is None:
        raise _problem("the selected cohort has no built PoN", "Build a verified panel before selecting this cohort")
    provenance = generation.pon_build
    if provenance is None or generation.pon_n < MIN_PON_N:
        raise _problem("the selected cohort has no qualified PoN build record", "Rebuild the cohort with a verified panel")
    if (provenance.reference_sha256 != reference_sha256
            or provenance.aligner_identity != aligner_identity
            or provenance.builder_source_sha256 != BUILDER_SOURCE_SHA256
            or provenance.builder_sha256 != BUILDER_SHA256
            or provenance.toolchain_lock_sha256 != toolchain_lock_sha256
            or provenance.counting_parameters_sha256 != counting_parameters_sha256
            or provenance.gc_sha256 != gc_sha256
            or provenance.map_sha256 != map_sha256
            or provenance.centromere_sha256 != centromere_sha256
            or (grid_sha256 is not None and provenance.grid_sha256 != grid_sha256)):
        raise _problem("the selected PoN does not match this copy-number protocol", "Use the matching reference and toolchain or rebuild the cohort")
    member = next((item for item in generation.members if item.source_id == source_id), None)
    digest = member.pon_asset_sha256 if member and member.pon_asset_sha256 else full
    target_excluded = source_id in provenance.source_ids
    if target_excluded and (member is None or member.pon_asset_sha256 is None):
        raise _problem("the selected cohort is missing a member's leave-one-out panel", "Restore the immutable cohort generation")
    path = store.lab / "assets" / "pon" / f"{digest}.rds"
    try:
        with os.fdopen(open_regular_no_follow(path), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("panel asset is not a regular file")
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise _problem("the selected PoN asset is missing or unsafe", "Restore the immutable panel asset") from exc
    if actual != digest:
        raise _problem("the selected PoN asset changed", "Restore the immutable panel asset")
    return PonSelection(
        generation.cohort_id, generation.version_id, "lab_loo_v1", digest, path,
        generation.pon_n, provenance.reference_sha256, provenance.aligner_identity,
        provenance.grid_sha256,
        provenance.counting_parameters_sha256, provenance.toolchain_lock_sha256,
        provenance.gc_sha256, provenance.map_sha256, provenance.centromere_sha256,
        tuple(item for item in provenance.source_ids if item != source_id), target_excluded,
    )


def require_grid(selection: PonSelection, actual_grid_sha256: str) -> None:
    """Check the deferred readCounter grid before the PoN enters ichorCNA."""

    if selection.grid_sha256 != actual_grid_sha256:
        raise _problem("the selected PoN grid differs from the current readCounter grid", "Use the matching inputs or rebuild the cohort")


__all__ = ["BUILDER_SOURCE_SHA256", "BUILDER_SHA256", "MIN_PON_N", "PonBuild", "PonInput", "PonSelection", "binding_for", "build_panels", "require_grid"]
