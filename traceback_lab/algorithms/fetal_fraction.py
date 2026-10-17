"""NIPTeR trisomy z-scores with explicit, unavailable fetal fraction.

NIPTeR 1.0.2 does not estimate fetal fraction.  Its z-scores are emitted only
against an operator-attested, SHA-pinned prenatal control group on a matching
reference and protocol.  This adapter makes no trisomy call or clinical cutoff.
"""

from __future__ import annotations

import hashlib
import math
import stat
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..fetal_contract import FetalContractError, inspect_contract
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

R_CODE = r'''suppressPackageStartupMessages(library(NIPTeR))
args <- commandArgs(trailingOnly=TRUE)
if (length(args) != 3L || as.character(packageVersion("NIPTeR")) != "1.0.2") stop("NIPTeR version mismatch")
control <- readRDS(args[[2L]])
if (!inherits(control, "NIPTControlGroup") || is.null(control$samples) ||
    length(control$samples) < as.integer(args[[3L]])) stop("invalid NIPTeR control group")
if (length(control$correction_status_autosomal_chromosomes) != 1L ||
    control$correction_status_autosomal_chromosomes != "Uncorrected" ||
    any(vapply(control$samples, function(item) length(item$autosomal_chromosome_reads) != 1L,
               logical(1)))) stop("control needs uncorrected, combined-strand bins")
sample <- bin_bam_sample(bam_filepath=args[[1L]], do_sort=FALSE, separate_strands=FALSE,
                         custom_name="lab-input")
for (chrom in c(13L, 18L, 21L)) {
  result <- calculate_z_score(nipt_sample=sample, nipt_control_group=control,
                               chromo_focus=chrom)
  value <- result$sample_Zscore
  if (length(value) != 1L || !is.finite(value)) stop("invalid NIPTeR z-score")
  cat(chrom, format(value, digits=17, scientific=FALSE, trim=TRUE), sep="\t")
  cat("\n")
}
'''
SCRIPT_SHA256 = hashlib.sha256(R_CODE.encode()).hexdigest()


def _rscript() -> tuple[Path, str] | None:
    path = shutil.which("Rscript")
    if path is None:
        return None
    try:
        target = Path(path).resolve(strict=True)
        if not target.is_file():
            return None
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(1 << 20):
                digest.update(block)
        return target, digest.hexdigest()
    except OSError:
        return None


def _nipter_package_sha(rscript: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(rscript), "--vanilla", "-e",
             "cat(as.character(packageVersion('NIPTeR')), system.file(package='NIPTeR'), sep='\\t')"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = result.stdout.strip().split("\t")
    if result.returncode != 0 or len(fields) != 2 or fields[0] != "1.0.2":
        return None
    try:
        package = Path(fields[1])
        if package.is_symlink() or not package.is_dir():
            return None
        digest = hashlib.sha256()
        total = 0
        files = 0
        for path in sorted(package.rglob("*")):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                return None
            total += info.st_size
            files += 1
            if total > (256 << 20) or files > 10000:
                return None
            digest.update(path.relative_to(package).as_posix().encode() + b"\0")
            with path.open("rb") as stream:
                while block := stream.read(1 << 20):
                    digest.update(block)
            digest.update(b"\0")
        return digest.hexdigest() if files else None
    except OSError:
        return None


def _parse_z_output(output: str) -> dict[int, float]:
    lines = output.splitlines()
    if len(lines) != 3:
        raise RuntimeError("NIPTeR returned an unexpected z-score table")
    values: dict[int, float] = {}
    for expected, line in zip((13, 18, 21), lines):
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] != str(expected):
            raise RuntimeError("NIPTeR returned an unexpected chromosome")
        try:
            value = float(fields[1])
        except ValueError as exc:
            raise RuntimeError("NIPTeR returned a nonnumeric z-score") from exc
        if not math.isfinite(value):
            raise RuntimeError("NIPTeR returned a nonfinite z-score")
        values[expected] = value
    return values


@dataclass
class FetalFraction(Base):
    family: str = "copy-number"
    consumes: str = "full_bam"
    cost: str = "slow"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=2_000_000))
    tools: tuple[ToolPin, ...] = (ToolPin("NIPTeR", "1.0.2", "nipter-lgpl3"),)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "trisomy_statistic": "NIPTeR standard z-score", "control": "attested matched NIPTControlGroup",
        "fetal_fraction_model": "unavailable", "r_script_sha256": SCRIPT_SHA256,
    })

    def asset_sha256s_for_input(self, root: Path, input_id: str) -> list[str]:
        from ..store import LabStore

        try:
            contract = inspect_contract(LabStore(root), input_id)
            runtime = _rscript()
            package_sha = _nipter_package_sha(runtime[0]) if runtime else None
            return [contract.manifest_sha256, contract.control_sha256, SCRIPT_SHA256,
                    *(runtime[1:] if runtime else ()), *((package_sha,) if package_sha else ())]
        except (FetalContractError, OSError):
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.derived is not None:
            return Skip("unsupported_input", "NIPTeR requires the full aligned prenatal input")
        from ..store import LabStore

        try:
            contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        except FetalContractError as exc:
            return Skip("missing_asset", f"verified prenatal control unavailable: {exc}")
        if contract.source_id != ctx.source_id or contract.reference_sha256 != ctx.reference_sha256:
            return Skip("reference", "prenatal assertion or control reference differs from this input")
        runtime = _rscript()
        package_sha = _nipter_package_sha(runtime[0]) if runtime else None
        if runtime is None or package_sha is None:
            return Skip("missing_toolchain", "Rscript with NIPTeR 1.0.2 is required")
        expected = {contract.manifest_sha256, contract.control_sha256, SCRIPT_SHA256, runtime[1], package_sha}
        if not expected.issubset(set(ctx.extras.get("planned_asset_sha256s", ()))):
            return Skip("missing_asset", "prenatal control or Rscript changed after result identity was planned")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        runtime = _rscript()
        if runtime is None:
            raise RuntimeError("NIPTeR toolchain changed after preflight")
        package_sha = _nipter_package_sha(runtime[0])
        if package_sha is None:
            raise RuntimeError("NIPTeR package changed after preflight")
        expected = {contract.manifest_sha256, contract.control_sha256, SCRIPT_SHA256, runtime[1], package_sha}
        if not expected.issubset(set(ctx.extras.get("planned_asset_sha256s", ()))):
            raise RuntimeError("prenatal control or Rscript changed after preflight")
        out.mkdir(parents=True, exist_ok=True)
        script = out / "nipter-z.R"
        script.write_text(R_CODE, encoding="utf-8")
        try:
            completed = subprocess.run(
                [str(runtime[0]), "--vanilla", str(script), str(ctx.input_dir / "aligned.bam"),
                 str(contract.control_path), str(contract.control_n)],
                cwd=out, capture_output=True, text=True, timeout=900, check=False,
            )
        finally:
            script.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise RuntimeError("NIPTeR trisomy calculation failed; inspect the registered control and toolchain")
        values = _parse_z_output(completed.stdout)
        if self.asset_sha256s_for_input(ctx.root, ctx.input_id) != list(ctx.extras["planned_asset_sha256s"]):
            raise RuntimeError("prenatal control or Rscript changed during execution")
        return AlgorithmOutput(metrics=[
            metric("fetal_fraction", None, unit="fraction"),
            *(metric(f"trisomy_{chrom}_z", values[chrom], unit="z-score", n=contract.control_n)
              for chrom in (13, 18, 21)),
        ])


FETAL_FRACTION = FetalFraction("fetal-fraction")
