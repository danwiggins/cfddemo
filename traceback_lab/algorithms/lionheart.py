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
import shutil
import subprocess
from pathlib import Path
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


def _program(variable: str, default: str) -> Path | None:
    selected = os.environ.get(variable) or shutil.which(default)
    if not selected:
        return None
    path = Path(selected).resolve()
    return path if path.is_file() and os.access(path, os.X_OK) else None


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


def resource_tree_digest(directory: Path) -> str:
    """Content identity for every extracted resource, without trusting mtimes."""

    digest = hashlib.sha256()
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
    for path in files:
        if path.is_symlink():
            raise ValueError("LIONHEART resources may not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(_file_hash(path)))
        elif not path.is_dir():
            raise ValueError("LIONHEART resources contain a non-regular file")
    return digest.hexdigest()


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
        return (
            ToolPin("lionheart", f"{LIONHEART_VERSION}+{_file_hash(lionheart)[:12] if lionheart else 'missing'}"),
            ToolPin("lionheart-mosdepth", _file_hash(mosdepth)[:12] if mosdepth else "missing"),
        )

    @tools.setter
    def tools(self, _value: Any) -> None:
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        directory, _ = _resources(root)
        try:
            return [resource_tree_digest(directory)]
        except (OSError, ValueError):
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
        if executable is None or mosdepth is None:
            return Skip("missing_toolchain", "Install pinned LIONHEART v2.2.0 and its modified mosdepth")
        try:
            version = subprocess.run([str(executable), "--version"], capture_output=True, text=True,
                                     timeout=10, check=False)
            help_page = subprocess.run([str(mosdepth), "--help"], capture_output=True, text=True,
                                       timeout=10, check=False)
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
            if archive.is_symlink() or _file_hash(archive, "md5") != RESOURCE_ARCHIVE_MD5:
                raise ValueError("resource archive does not match Zenodo record 15747531")
            resource_tree_digest(directory)
            _labels(directory)
        except (OSError, ValueError, KeyError, TypeError):
            return Skip("missing_asset", "LIONHEART v003 resources are absent or changed; install the verified Zenodo archive")
        bam = ctx.input_dir / "aligned.bam"
        if not bam.is_file() or bam.is_symlink():
            return Skip("missing_input", "LIONHEART needs the selected input's aligned BAM")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        executable = _program("TRACEBACK_LIONHEART_BIN", "lionheart")
        mosdepth = _program("TRACEBACK_LIONHEART_MOSDEPTH_BIN", "mosdepth")
        directory, archive = _resources(ctx.root)
        if executable is None or mosdepth is None or _file_hash(archive, "md5") != RESOURCE_ARCHIVE_MD5:
            raise RuntimeError("LIONHEART toolchain or resource archive changed since preflight")
        work = out.parent / "lionheart-extraction"
        work.mkdir()
        command = [str(executable), "extract_features", "--bam_file", str(ctx.input_dir / "aligned.bam"),
                   "--resources_dir", str(directory), "--out_dir", str(work),
                   "--mosdepth_path", str(mosdepth), "--n_jobs", "1", "--single_end_mode"]
        with (out.parent / "lionheart.log").open("wb") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"LIONHEART feature extraction failed (exit {completed.returncode})")
        matrix_path = work / "dataset" / "feature_dataset.npy"
        if matrix_path.stat().st_size > 1_000_000:
            raise ValueError("LIONHEART feature matrix is implausibly large")
        matrix = np.load(matrix_path, allow_pickle=False)
        labels = _labels(directory)
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
