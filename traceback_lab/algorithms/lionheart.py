"""Opt-in LIONHEART v2.2.0 raw feature extraction from an aligned ONT BAM.

The upstream 10x898 feature matrix is computed by LIONHEART itself, using its
experimental single-end mode and modified mosdepth. Only its standardized
correlation scores are exposed; its paired-end-trained cancer predictor is
never invoked. The Zenodo resource archive is operator-supplied and checked
against record 15747531's published MD5 before computation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

LIONHEART_VERSION = "2.2.0"
RESOURCE_ARCHIVE_MD5 = "7ab368995ad5123ab665042e6ecc4410"
RESOURCE_VERSION = "v003"
MIN_ELIGIBLE_READS = 5_000_000
REQUIRED_FILES = (
    "ATAC.idx_to_cell_type.csv",
    "DNase.idx_to_cell_type.csv",
    "gc_contents_bin_edges.npy",
    "insert_size_bin_edges.npy",
    "outliers/outlier_indices.npz",
    "outliers/zero_coverage_indices.npz",
    "rows_per_chrom_pre_exclusion.txt",
)
REQUIRED_DIRS = (
    "bin_indices_by_chromosome",
    "chromatin_masks/ATAC/sparse_overlaps_by_chromosome",
    "chromatin_masks/DNase/sparse_overlaps_by_chromosome",
)


def _program(variable: str | None, default: str) -> Path | None:
    selected = (os.environ.get(variable) if variable else None) or shutil.which(default)
    if not selected:
        return None
    path = Path(selected).resolve()
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _mosdepth_library_dir(mosdepth: Path) -> Path | None:
    """Upstream requires this CLI argument even when LD_LIBRARY_PATH is set."""
    selected = os.environ.get("TRACEBACK_LIONHEART_LD_LIBRARY_PATH")
    directory = Path(selected) if selected else mosdepth.parent
    return directory.resolve() if directory.is_dir() else None


def _resources(root: Path) -> tuple[Path, Path]:
    directory = Path(os.environ.get("TRACEBACK_LIONHEART_RESOURCES") or
                     root / "lab" / "assets" / "lionheart" / "inference_resources_v003")
    archive = Path(os.environ.get("TRACEBACK_LIONHEART_ARCHIVE") or
                   directory.parent / "inference_resources_v003.tar.gz")
    return directory, archive


def _file_hash(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while block := stream.read(16 << 20):
            digest.update(block)
    return digest.hexdigest()


_PACKAGE_PIN_SCRIPT = """\
import hashlib, importlib.metadata, json
from pathlib import Path
dist = importlib.metadata.distribution('lionheart')
files = sorted((str(p), Path(dist.locate_file(p))) for p in (dist.files or [])
               if p.parts and p.parts[0] == 'lionheart' and p.suffix in ('.py', '.so', '.pyd'))
if not files:
    raise RuntimeError('Lionheart distribution has no recorded package files')
digest = hashlib.sha256()
for name, path in files:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('Lionheart package file missing or linked')
    digest.update(name.encode('utf-8'))
    with path.open('rb') as stream:
        while block := stream.read(16 << 20):
            digest.update(block)
print(json.dumps({'version': dist.version, 'sha256': digest.hexdigest()}))
"""


def _package_pin(executable: Path) -> tuple[str, str] | None:
    """Hash the installed package loaded by the CLI's Python environment."""

    try:
        with executable.open("rb") as stream:
            shebang = stream.readline(512).decode("utf-8").strip()
        if not shebang.startswith("#!"):
            return None
        parts = shlex.split(shebang[2:])
        if len(parts) != 1 or not Path(parts[0]).is_file():
            return None
        result = subprocess.run([parts[0], "-c", _PACKAGE_PIN_SCRIPT], capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode != 0:
            return None
        reported = json.loads(result.stdout)
        version, sha256 = reported["version"], reported["sha256"]
        if not isinstance(version, str) or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return None
        return version, sha256
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def _tree_files(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("LIONHEART resource directory is absent or a symlink")
    for relative in REQUIRED_FILES:
        path = directory / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"LIONHEART resource is missing: {relative}")
    for relative in REQUIRED_DIRS:
        path = directory / relative
        if not path.is_dir() or path.is_symlink() or not any(path.iterdir()):
            raise ValueError(f"LIONHEART resource directory is missing: {relative}")
    files = sorted(directory.rglob("*"))
    if not files:
        raise ValueError("LIONHEART resource directory is empty")
    hashes = {}
    for path in files:
        if path.is_symlink():
            raise ValueError("LIONHEART resources may not contain symlinks")
        if path.is_file():
            hashes[path.relative_to(directory).as_posix()] = _file_hash(path)
        elif not path.is_dir():
            raise ValueError("LIONHEART resources contain a non-regular file")
    return hashes


def _manifest_digest(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, sha256 in sorted(hashes.items()):
        digest.update(name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256))
    return digest.hexdigest()


def resource_tree_digest(directory: Path) -> str:
    """Content identity for every extracted resource, without trusting mtimes."""

    return _manifest_digest(_tree_files(directory))


def validated_resource_digest(directory: Path, archive: Path) -> str:
    """Match every consumed file byte against the published, verified archive."""

    if archive.is_symlink() or _file_hash(archive, "md5") != RESOURCE_ARCHIVE_MD5:
        raise ValueError("resource archive does not match Zenodo record 15747531")
    extracted = _tree_files(directory)
    archived: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("LIONHEART archive contains an unsafe path")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("LIONHEART archive contains a non-regular entry")
            normalized = name.as_posix()
            if normalized in archived:
                raise ValueError("LIONHEART archive contains a duplicate entry")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("LIONHEART archive member is unreadable")
            digest = hashlib.sha256()
            with stream:
                while block := stream.read(16 << 20):
                    digest.update(block)
            archived[normalized] = digest.hexdigest()
    roots = [name.split("/")[:-1] for name in archived if name.endswith("/ATAC.idx_to_cell_type.csv")]
    if "ATAC.idx_to_cell_type.csv" in archived:
        roots.append([])
    if len(roots) != 1:
        raise ValueError("LIONHEART archive has no unique resource root")
    prefix = "/".join(roots[0])
    prefix = f"{prefix}/" if prefix else ""
    relative = {}
    for name, sha256 in archived.items():
        if not name.startswith(prefix) or not name[len(prefix):]:
            raise ValueError("LIONHEART archive has files outside its resource root")
        relative[name[len(prefix):]] = sha256
    if relative != extracted:
        raise ValueError("extracted LIONHEART resources differ from the verified archive")
    return _manifest_digest(extracted)


def _labels(directory: Path) -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []
    for assay in ("ATAC", "DNase"):  # Upstream concatenates features in this order.
        index_file = directory / f"{assay}.idx_to_cell_type.csv"
        if index_file.stat().st_size > 1_000_000:
            raise ValueError(f"LIONHEART {assay} index is implausibly large")
        with index_file.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) > 898:
            raise ValueError(f"LIONHEART {assay} index has too many rows")
        indexed = {}
        for row in rows:
            index = int(row["idx"])
            name = row["cell_type"].strip()
            if index in indexed or not name:
                raise ValueError(f"LIONHEART {assay} cell-type index is malformed")
            indexed[index] = name
        if set(indexed) != set(range(len(indexed))):
            raise ValueError(f"LIONHEART {assay} cell-type indexes are not consecutive")
        labels.extend((assay, indexed[index]) for index in range(len(indexed)))
    if len(labels) != 898:
        raise ValueError(f"LIONHEART resource labels: expected 898, got {len(labels)}")
    return labels


class Lionheart(Base):
    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        lionheart = _program("TRACEBACK_LIONHEART_BIN", "lionheart")
        mosdepth = _program("TRACEBACK_LIONHEART_MOSDEPTH_BIN", "mosdepth")
        samtools = _program(None, "samtools")
        unpigz = _program(None, "unpigz")
        library_dir = _mosdepth_library_dir(mosdepth) if mosdepth else None
        package = _package_pin(lionheart) if lionheart else None
        return (
            ToolPin("lionheart-cli", _file_hash(lionheart) if lionheart else "missing"),
            ToolPin("lionheart-package", f"{package[0]}+{package[1]}" if package else "missing"),
            ToolPin("lionheart-mosdepth", _file_hash(mosdepth) if mosdepth else "missing"),
            ToolPin("lionheart-library-directory", hashlib.sha256(str(library_dir).encode()).hexdigest()
                    if library_dir else "missing"),
            ToolPin("lionheart-samtools", _file_hash(samtools) if samtools else "missing"),
            ToolPin("lionheart-unpigz", _file_hash(unpigz) if unpigz else "missing"),
        )

    @tools.setter
    def tools(self, _value: Any) -> None:
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        directory, archive = _resources(root)
        try:
            return [validated_resource_digest(directory, archive)]
        except (OSError, ValueError, tarfile.TarError):
            return []  # preflight supplies the actionable skip reason

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "LIONHEART needs hg38 resources; not in the synthetic example")
        if ctx.derived:
            return Skip("derived_not_supported", "LIONHEART raw scores are extracted only from the full input")
        if ctx.reference_id.lower() not in {"hg38", "grch38"}:
            return Skip("reference", "LIONHEART v2.2.0 requires an hg38-aligned BAM")
        if ctx.eligible_reads is None:
            return Skip("depth", "LIONHEART needs a measured eligible-read count before its 5M gate")
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if os.environ.get("TRACEBACK_LIONHEART_ENABLE_ONT") != "1":
            return Skip("platform_unverified", "ONT single-end feature extraction is experimental; set TRACEBACK_LIONHEART_ENABLE_ONT=1 to opt in")
        executable = _program("TRACEBACK_LIONHEART_BIN", "lionheart")
        mosdepth = _program("TRACEBACK_LIONHEART_MOSDEPTH_BIN", "mosdepth")
        if executable is None or mosdepth is None or _program(None, "samtools") is None or _program(None, "unpigz") is None:
            return Skip("missing_toolchain", "Install LIONHEART v2.2.0, modified mosdepth, samtools, and unpigz")
        if _mosdepth_library_dir(mosdepth) is None:
            return Skip("missing_toolchain", "LIONHEART mosdepth library directory is missing")
        package = _package_pin(executable)
        if package is None or package[0] != LIONHEART_VERSION:
            return Skip("missing_toolchain", "LIONHEART v2.2.0 installed package files must be available for provenance")
        try:
            version = subprocess.run([str(executable), "--version"], capture_output=True, text=True,
                                     timeout=30, check=False)
            help_page = subprocess.run([str(mosdepth), "--help"], capture_output=True, text=True,
                                       timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return Skip("missing_toolchain", "LIONHEART or modified mosdepth could not be started")
        reported = version.stdout.strip()
        if version.returncode != 0 or reported not in {
            f"lionheart {LIONHEART_VERSION}", f"lionheart, version {LIONHEART_VERSION}",
        }:
            return Skip("missing_toolchain", f"LIONHEART {LIONHEART_VERSION} is required")
        if "--single-end-length" not in help_page.stdout + help_page.stderr:
            return Skip("missing_toolchain", "modified mosdepth with --single-end-length is required")
        directory, archive = _resources(ctx.root)
        try:
            validated_resource_digest(directory, archive)
            _labels(directory)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
            return Skip("missing_asset", "LIONHEART v003 resources are absent or changed; install the verified Zenodo archive")
        bam = ctx.input_dir / "aligned.bam"
        if not bam.is_file() or bam.is_symlink():
            return Skip("missing_input", "LIONHEART needs the selected input's aligned BAM")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        executable = _program("TRACEBACK_LIONHEART_BIN", "lionheart")
        mosdepth = _program("TRACEBACK_LIONHEART_MOSDEPTH_BIN", "mosdepth")
        directory, archive = _resources(ctx.root)
        if executable is None or mosdepth is None:
            raise RuntimeError("LIONHEART toolchain or resource archive changed since preflight")
        library_dir = _mosdepth_library_dir(mosdepth)
        if library_dir is None:
            raise RuntimeError("LIONHEART mosdepth library directory changed since preflight")
        planned_tools = ctx.extras.get("tool_versions")
        if not isinstance(planned_tools, dict) or planned_tools != {pin.name: pin.version for pin in self.tools}:
            raise RuntimeError("LIONHEART toolchain changed since execution planning")
        expected = ctx.extras.get("asset_sha256s")
        if not isinstance(expected, list) or len(expected) != 1 or not isinstance(expected[0], str):
            raise RuntimeError("LIONHEART planned resource identity is missing")
        work = out.parent / "lionheart-extraction"
        work.mkdir()
        snapshot = work / "resources"
        shutil.copytree(directory, snapshot)
        if validated_resource_digest(snapshot, archive) != expected[0]:
            raise RuntimeError("LIONHEART resources changed since execution planning")
        command = [str(executable), "extract_features", "--bam_file", str(ctx.input_dir / "aligned.bam"),
                   "--resources_dir", str(snapshot), "--out_dir", str(work),
                   "--mosdepth_path", str(mosdepth), "--ld_library_path", str(library_dir),
                   "--n_jobs", "1", "--single_end_mode"]
        with (out.parent / "lionheart.log").open("wb") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"LIONHEART feature extraction failed (exit {completed.returncode})")
        if resource_tree_digest(snapshot) != expected[0]:
            raise RuntimeError("LIONHEART resource snapshot changed during extraction")
        matrix_path = work / "dataset" / "feature_dataset.npy"
        if matrix_path.stat().st_size > 1_000_000:
            raise ValueError("LIONHEART feature matrix is implausibly large")
        matrix = np.load(matrix_path, allow_pickle=False)
        labels = _labels(snapshot)
        if matrix.shape != (10, len(labels)) or not np.issubdtype(matrix.dtype, np.floating):
            raise ValueError("LIONHEART feature matrix has an unexpected shape or dtype")
        scores = [float(value) if np.isfinite(value) else None for value in matrix[0]]
        count = sum(value is not None for value in scores)
        if not count:
            raise ValueError("LIONHEART produced no finite cell-type scores")
        rows = [{"assay": assay, "cell_type": name, "score": score}
                for (assay, name), score in zip(labels, scores)]
        return AlgorithmOutput(
            metrics=[metric("cell_types_scored", count, unit="profiles", n=len(labels))],
            panels=[{"panel_id": "cell-type-scores", "kind": "lionheart-scores",
                     "data_ref": "artifacts/lionheart_scores.json"}],
            artifacts={"lionheart_scores.json": json.dumps({
                "kind": "lionheart-scores.v1", "method_version": LIONHEART_VERSION,
                "resource_version": RESOURCE_VERSION, "input_mode": "experimental_single_end_ont",
                "score_kind": "sample_standardized_pearson_r", "scores": rows,
            }, separators=(",", ":")).encode("utf-8")},
        )


LIONHEART = Lionheart(
    "lionheart", version="1", family="fragmentomics", consumes="full_bam", cost="slow",
    requires=Requirements(min_eligible_reads=MIN_ELIGIBLE_READS),
    parameters={"upstream_version": LIONHEART_VERSION, "resources": RESOURCE_VERSION,
                "feature_set": 0, "single_end_mode": True},
)
