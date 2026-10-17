"""Run the pinned upstream Griffin workflows for one Lab aligned BAM.

The output belongs to a fresh per-input attempt directory. This module does
not publish a Lab algorithm result: runner identity and registered TF-site
asset support must be added before that is safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Mapping

import yaml

from .griffin import GriffinFormatError, GriffinProfile, read_corrected_profiles

GRIFFIN_COMMIT = "b624c7a2ea7b8758b6eebd424066c815bfeece61"
SNAKEMAKE_VERSION = "5.19.2"
MIN_ELIGIBLE_AUTOSOMAL_PAIRS = 5_000_000
D21_PAIR_PREDICATE = {
    "version": "griffin-d21-autosomal-pairs-v1",
    "contigs": "canonical chr1–chr22 or 1–22",
    "counted_record": "first mate only; primary, mapped, proper pair, same-contig mapped mate",
    "excluded": "secondary, supplementary, duplicate, QC-fail",
    "minimum_mapq": 20,
}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./-]+$")
_REF_FILES = (
    "hg38.standard.chrom.sizes", "k100.Umap.MultiTrackMappability.bw",
    "k100_minus_exclusion_lists.mappable_regions.hg38.bed",
    "encode_unified_GRCh38_exclusion_list.bed", "hg38_centromeres.bed",
    "hg38_gaps.bed", "hg38_fix_patches.bed", "hg38_alternative_haplotypes.bed",
)


@dataclass(frozen=True)
class GriffinRun:
    input_id: str
    bam: Path
    reference_fasta: Path
    reference_id: str
    reference_sha256: str
    eligible_autosomal_pairs: int
    upstream_root: Path
    ref_assets: Path
    site_lists: Mapping[str, Path]
    attempt_dir: Path
    snakemake: str = "snakemake"
    cores: int = 1


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        h.update(path.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(bytes.fromhex(_digest(path)))
    return h.hexdigest()


def _safe_path(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    # Griffin's own Snakemake shell rules interpolate config paths without
    # quoting. Reject paths its shell cannot consume safely.
    if not _SAFE_TOKEN.fullmatch(str(resolved)):
        raise ValueError(f"Griffin upstream requires shell-safe POSIX paths: {resolved}")
    return resolved


def _tool_path(command: str, *, path: str | None = None) -> Path:
    found = shutil.which(command, path=path)
    if found is None:
        raise ValueError(f"Griffin workflow tool is unavailable: {command}")
    return _safe_path(Path(found))


def _runtime_identity(snakemake: Path, python: Path, samtools: Path, bedtools: Path,
                      workflow_path: str) -> dict:
    packages = {}
    for name in ("numpy", "pandas", "scipy", "pysam", "PyYAML", "pybedtools", "pyBigWig", "matplotlib"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "unavailable"
    executables = {"snakemake": snakemake, "python": python,
                   "samtools": samtools, "bedtools": bedtools}
    return {
        "executables": {name: {"path": str(path), "sha256": _digest(path)}
                        for name, path in executables.items()},
        "python_version": sys.version.split()[0],
        "packages": packages,
        "pyyaml": yaml.__version__,
        "environment": {**{key: os.environ.get(key, "") for key in
                           ("PYTHONPATH", "LD_LIBRARY_PATH", "CONDA_PREFIX")},
                        "PATH": workflow_path},
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _load_default(upstream: Path, workflow: str) -> dict:
    path = upstream / "snakemakes" / workflow / "config" / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _ref_options(reference: Path, assets: Path, scripts: Path) -> dict:
    return {
        "reference_genome": str(reference),
        "chrom_sizes": str(assets / "hg38.standard.chrom.sizes"),
        "chrom_sizes_path": str(assets / "hg38.standard.chrom.sizes"),
        "mappability_bw": str(assets / "k100.Umap.MultiTrackMappability.bw"),
        "mappable_regions": str(assets / "k100_minus_exclusion_lists.mappable_regions.hg38.bed"),
        "genome_GC_frequency": str(assets / "genome_GC_frequency"),
        "encode_exclude": str(assets / "encode_unified_GRCh38_exclusion_list.bed"),
        "centromeres": str(assets / "hg38_centromeres.bed"),
        "gaps": str(assets / "hg38_gaps.bed"),
        "patches": str(assets / "hg38_fix_patches.bed"),
        "alternative_haplotypes": str(assets / "hg38_alternative_haplotypes.bed"),
        "griffin_scripts_dir": str(scripts),
    }


def _require_paired_input(bam: Path) -> None:
    """Upstream needs Illumina pairs with positive TLEN across the full BAM."""

    import pysam

    primary = paired = usable = 0
    with pysam.AlignmentFile(str(bam), "rb") as alignments:
        groups = alignments.header.to_dict().get("RG", [])
        if not groups or any(group.get("PL", "").upper() != "ILLUMINA" for group in groups):
            raise ValueError(
                "pinned Griffin requires unanimous @RG PL:ILLUMINA; "
                "native ONT single-end BAM is incompatible"
            )
        for read in alignments.fetch(until_eof=True):
            if read.is_unmapped or read.is_secondary or read.is_supplementary:
                continue
            primary += 1
            paired += bool(read.is_paired)
            usable += bool(
                read.is_paired and not read.is_reverse and 100 <= read.template_length <= 200
                and read.mapping_quality >= 20 and not read.is_duplicate and not read.is_qcfail
            )
    if primary < 100 or paired / primary < 0.95 or usable == 0:
        raise ValueError(
            "pinned Griffin requires paired-end positive-TLEN WGS reads; "
            "native ONT single-end BAM is incompatible"
        )


def _require_reference_header(bam: Path, reference: Path) -> None:
    """Require the BAM coordinate space to match the registered FASTA index."""

    import pysam

    with pysam.FastaFile(str(reference)) as fasta:
        expected = dict(zip(fasta.references, fasta.lengths))
    with pysam.AlignmentFile(str(bam), "rb") as alignments:
        sq = alignments.header.to_dict().get("SQ", [])
    observed = {entry["SN"]: entry["LN"] for entry in sq}
    if not expected or len(observed) != len(sq) or observed != expected:
        raise ValueError("BAM @SQ does not match the registered reference FASTA")


def _require_chrom_sizes(reference: Path, chrom_sizes: Path) -> None:
    """Every coverage-space contig must match the registered FASTA index."""

    import pysam

    with pysam.FastaFile(str(reference)) as fasta:
        expected = dict(zip(fasta.references, fasta.lengths))
    seen: set[str] = set()
    with chrom_sizes.open("r", encoding="ascii") as stream:
        for line in stream:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 2 or not fields[0] or fields[0] in seen:
                raise ValueError("Griffin chromosome sizes are malformed")
            try:
                length = int(fields[1])
            except ValueError as exc:
                raise ValueError("Griffin chromosome sizes are malformed") from exc
            if length <= 0 or expected.get(fields[0]) != length:
                raise ValueError("Griffin chromosome sizes differ from the registered FASTA")
            seen.add(fields[0])
    if not seen:
        raise ValueError("Griffin chromosome sizes are empty")


def count_d21_pairs(bam: Path) -> int:
    """Count one eligible autosomal molecule per proper Illumina pair."""

    import pysam

    with pysam.AlignmentFile(str(bam), "rb") as alignments:
        groups = alignments.header.to_dict().get("RG", [])
        if not groups or any(group.get("PL", "").upper() != "ILLUMINA" for group in groups):
            raise ValueError("Griffin requires unanimously Illumina-declared read groups")
        autosomes = {
            index for index, name in enumerate(alignments.references)
            if re.fullmatch(r"(?:chr)?(?:[1-9]|1[0-9]|2[0-2])", name)
        }
        if not autosomes:
            raise ValueError("Griffin requires canonical hg38 autosomes")
        count = 0
        for read in alignments.fetch(until_eof=True):
            if (read.is_paired and read.is_proper_pair and read.is_read1 and
                    not read.is_unmapped and not read.mate_is_unmapped and
                    not read.is_secondary and not read.is_supplementary and
                    not read.is_duplicate and not read.is_qcfail and
                    read.mapping_quality >= 20 and
                    read.reference_id == read.next_reference_id and
                    read.reference_id in autosomes):
                count += 1
        return count


def run_upstream(config: GriffinRun) -> tuple[GriffinProfile, ...]:
    """Execute upstream GC correction and profiling, then validate its TSV.

    Caller supplies the exact filtered site lists and hg38 assets. No files
    are downloaded or inferred. A fresh attempt directory is mandatory; on
    any failure its partial outputs remain for inspection without becoming a
    successful Lab result.
    """

    if config.eligible_autosomal_pairs < MIN_ELIGIBLE_AUTOSOMAL_PAIRS:
        raise ValueError("Griffin requires at least 5,000,000 caller-supplied eligible autosomal pairs")
    if config.reference_id != "hg38":
        raise ValueError("upstream Griffin is validated for hg38 only")
    if not re.fullmatch(r"[a-z0-9]{12,64}", config.input_id):
        raise ValueError("input_id must be a Lab hex identifier")
    if not config.site_lists or config.cores < 1:
        raise ValueError("Griffin needs TF-site lists and at least one core")
    if os.name != "posix":
        raise RuntimeError("the pinned Griffin workflow requires a POSIX runtime")
    upstream = _safe_path(config.upstream_root)
    commit = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if commit != GRIFFIN_COMMIT:
        raise ValueError("Griffin checkout is not the pinned upstream commit")
    dirty = subprocess.run(
        ["git", "-C", str(upstream), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise ValueError("Griffin checkout has uncommitted changes")
    bam = _safe_path(config.bam)
    reference = _safe_path(config.reference_fasta)
    assets = _safe_path(config.ref_assets)
    scripts_source = _safe_path(upstream / "scripts")
    if _digest(reference) != config.reference_sha256:
        raise ValueError("reference FASTA differs from the registered Lab reference")
    bam_index_candidates = (bam.with_suffix(bam.suffix + ".bai"), bam.with_suffix(".bai"))
    bam_indexes = tuple(_safe_path(path) for path in bam_index_candidates if path.is_file())
    if not bam_indexes:
        raise ValueError("the aligned BAM needs an index for Griffin")
    reference_index = reference.with_suffix(reference.suffix + ".fai")
    if not reference_index.is_file():
        raise ValueError("the registered reference FASTA needs an index for Griffin")
    reference_index = _safe_path(reference_index)
    _require_paired_input(bam)
    _require_reference_header(bam, reference)
    measured_pairs = count_d21_pairs(bam)
    if measured_pairs != config.eligible_autosomal_pairs:
        raise ValueError("caller-supplied eligible autosomal pair count differs from the BAM")
    for filename in _REF_FILES:
        _safe_path(assets / filename)
    _require_chrom_sizes(reference, assets / "hg38.standard.chrom.sizes")
    frequency = _safe_path(assets / "genome_GC_frequency")
    if not frequency.is_dir():
        raise ValueError("Griffin genome GC-frequency reference is absent")
    prefix = "k100_minus_exclusion_lists.mappable_regions.hg38."
    frequency_files = [frequency / f"{prefix}{length}bp.GC_frequency.txt" for length in range(15, 501)]
    if any(not path.is_file() for path in frequency_files):
        raise ValueError("Griffin genome GC-frequency reference is incomplete (15–500 bp)")
    site_paths: dict[str, str] = {}
    for name, path in config.site_lists.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("site-list names must be shell-safe")
        site_paths[name] = str(_safe_path(path))
    sample = "lab_" + config.input_id
    attempt = config.attempt_dir
    if not attempt.is_absolute():
        raise ValueError("Griffin attempt directory must be absolute")
    if attempt.exists():
        raise FileExistsError("Griffin attempt directory already exists")
    _safe_path(attempt.parent)
    if not _SAFE_TOKEN.fullmatch(str(attempt.absolute())):
        raise ValueError("Griffin attempt needs a shell-safe POSIX path")
    fingerprints = {
        "bam_sha256": _digest(bam),
        "bam_indexes_sha256": {path.name: _digest(path) for path in bam_indexes},
        "reference_sha256": config.reference_sha256,
        "reference_index_sha256": _digest(reference_index),
        "site_lists_sha256": {name: _digest(Path(path)) for name, path in site_paths.items()},
        "assets_sha256": {name: _digest(assets / name) for name in _REF_FILES},
        "gc_frequency_sha256": {path.name: _digest(path) for path in frequency_files},
    }
    snakemake = _tool_path(config.snakemake)
    python = Path(sys.executable).parent / "python"
    if not python.is_file() or not _SAFE_TOKEN.fullmatch(str(python.absolute())):
        raise ValueError("Griffin shell scripts need a shell-safe Lab Python launcher")
    workflow_path = str(python.parent) + os.pathsep + os.environ.get("PATH", "")
    samtools = _tool_path("samtools", path=workflow_path)
    bedtools = _tool_path("bedtools", path=workflow_path)
    snakemake_version = subprocess.run(
        [str(snakemake), "--version"], check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if snakemake_version != SNAKEMAKE_VERSION:
        raise ValueError(f"Griffin requires pinned Snakemake {SNAKEMAKE_VERSION}")
    runtime = _runtime_identity(snakemake, python, samtools, bedtools, workflow_path)
    runtime.update({"snakemake_version": snakemake_version, "cores": config.cores})
    attempt.mkdir()
    scripts = attempt / "upstream_scripts"
    shutil.copytree(scripts_source, scripts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    scripts = _safe_path(scripts)
    fingerprints["upstream_scripts_sha256"] = _tree_digest(scripts)
    workflows = (
        "griffin_GC_and_mappability_correction", "griffin_nucleosome_profiling"
    )
    snakefiles: list[Path] = []
    upstream_configs: dict[str, str] = {}
    for workflow in workflows:
        source = upstream / "snakemakes" / workflow
        destination = attempt / workflow
        (destination / "config").mkdir(parents=True)
        snakefile = destination / f"{workflow}.snakefile"
        shutil.copy2(source / snakefile.name, snakefile)
        snakefiles.append(snakefile)
        upstream_configs[workflow] = _digest(source / "config" / "config.yaml")
    fingerprints["upstream_configs_sha256"] = upstream_configs
    fingerprints["snakefiles_sha256"] = {
        str(path.relative_to(attempt)): _digest(path) for path in snakefiles
    }
    gc_dir = attempt / workflows[0]
    profile_dir = attempt / workflows[1]
    gc_config = _load_default(upstream, workflows[0])
    gc_config.update(_ref_options(reference, assets, scripts))
    gc_config.update({"out_dir": "results", "mappability_correction": False})
    _write_yaml(gc_dir / "config" / "config.yaml", gc_config)
    _write_yaml(gc_dir / "config" / "samples.yaml", {"samples": {sample: str(bam)}})
    profile_config = _load_default(upstream, workflows[1])
    profile_config.update(_ref_options(reference, assets, scripts))
    profile_config.update({
        "tmp_dir": "tmp", "results_dir": "results",
        "sites_yaml": "config/sites.yaml", "samples_yaml": "config/samples.GC.yaml",
    })
    _write_yaml(profile_dir / "config" / "config.yaml", profile_config)
    _write_yaml(profile_dir / "config" / "sites.yaml", {"site_lists": site_paths})
    # The second workflow consumes the GC-bias file produced by the first.
    gc_bias = gc_dir / "results" / "GC_bias" / f"{sample}.GC_bias.txt"
    _write_yaml(profile_dir / "config" / "samples.GC.yaml", {
        "samples": {sample: {"bam": str(bam), "GC_bias": str(gc_bias)}}
    })
    generated_configs = (
        gc_dir / "config" / "config.yaml", gc_dir / "config" / "samples.yaml",
        profile_dir / "config" / "config.yaml", profile_dir / "config" / "sites.yaml",
        profile_dir / "config" / "samples.GC.yaml",
    )
    fingerprints["generated_configs_sha256"] = {
        str(path.relative_to(attempt)): _digest(path) for path in generated_configs
    }
    (attempt / "provenance.json").write_text(json.dumps({
        "status": "attempt", "input_id": config.input_id,
        "upstream_commit": commit, "reference_id": config.reference_id,
        "sample": sample, "eligible_autosomal_pairs": config.eligible_autosomal_pairs,
        "minimum_eligible_autosomal_pairs": MIN_ELIGIBLE_AUTOSOMAL_PAIRS,
        "eligibility_source": "bam_verified", "eligibility_predicate": D21_PAIR_PREDICATE,
        "snakemake_version": snakemake_version, "runtime": runtime,
        "fingerprints": fingerprints,
    }, sort_keys=True), encoding="utf-8")
    for directory, snakefile in (
        (gc_dir, workflows[0] + ".snakefile"),
        (profile_dir, workflows[1] + ".snakefile"),
    ):
        with (directory / "workflow.log").open("wb") as log:
            subprocess.run(
                [str(snakemake), "-s", snakefile, "--cores", str(config.cores)],
                cwd=directory, stdout=log, stderr=subprocess.STDOUT, check=True,
                env={**os.environ, "PATH": workflow_path,
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
    if (_tool_path("samtools", path=workflow_path) != samtools
            or _tool_path("bedtools", path=workflow_path) != bedtools
            or _runtime_identity(snakemake, python, samtools, bedtools, workflow_path) != {
        key: value for key, value in runtime.items() if key not in ("snakemake_version", "cores")
    }):
        raise RuntimeError("Griffin workflow runtime changed during computation")
    if _digest(bam) != fingerprints["bam_sha256"] or _digest(reference) != config.reference_sha256:
        raise RuntimeError("BAM or reference changed during Griffin computation")
    if (_tree_digest(scripts) != fingerprints["upstream_scripts_sha256"]
            or {str(path.relative_to(attempt)): _digest(path) for path in snakefiles}
            != fingerprints["snakefiles_sha256"]
            or {str(path.relative_to(attempt)): _digest(path) for path in generated_configs}
            != fingerprints["generated_configs_sha256"]):
        raise RuntimeError("Griffin workflow source or generated configuration changed during computation")
    if (subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True,
                       capture_output=True, text=True).stdout.strip() != commit
            or subprocess.run(["git", "-C", str(upstream), "status", "--porcelain"], check=True,
                              capture_output=True, text=True).stdout.strip()):
        raise RuntimeError("pinned Griffin checkout changed during computation")
    if (tuple(_safe_path(path) for path in bam_index_candidates if path.is_file()) != bam_indexes
            or {path.name: _digest(path) for path in bam_indexes} != fingerprints["bam_indexes_sha256"]
            or not reference_index.is_file()
            or _safe_path(reference.with_suffix(reference.suffix + ".fai")) != reference_index
            or _digest(reference_index) != fingerprints["reference_index_sha256"]):
        raise RuntimeError("BAM or reference index changed during Griffin computation")
    if any(_digest(Path(path)) != fingerprints["site_lists_sha256"][name] for name, path in site_paths.items()):
        raise RuntimeError("TF-site list changed during Griffin computation")
    if any(_digest(assets / name) != fingerprints["assets_sha256"][name] for name in _REF_FILES):
        raise RuntimeError("Griffin reference asset changed during computation")
    if {path.name: _digest(path) for path in frequency_files} != fingerprints["gc_frequency_sha256"]:
        raise RuntimeError("Griffin genome GC frequencies changed during computation")
    output = profile_dir / "results" / sample / f"{sample}.GC_corrected.coverage.tsv"
    profiles = read_corrected_profiles(output, expected_sample=sample)
    if {profile.site_name for profile in profiles} != set(site_paths):
        raise GriffinFormatError("Griffin output site lists do not match the requested site lists")
    manifest = {
        "status": "complete", "input_id": config.input_id,
        "upstream_commit": commit, "reference_id": config.reference_id,
        "sample": sample, "eligible_autosomal_pairs": config.eligible_autosomal_pairs,
        "minimum_eligible_autosomal_pairs": MIN_ELIGIBLE_AUTOSOMAL_PAIRS,
        "eligibility_source": "bam_verified", "eligibility_predicate": D21_PAIR_PREDICATE,
        "snakemake_version": snakemake_version, "runtime": runtime,
        "fingerprints": fingerprints,
        "gc_bias_sha256": _digest(gc_bias), "output_sha256": _digest(output),
        "profile_count": len(profiles),
    }
    (attempt / "provenance.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return profiles
