"""NIPTeR trisomy z-scores with explicit, unavailable fetal fraction.

NIPTeR 1.0.2 does not estimate fetal fraction.  Its z-scores are emitted only
against an operator-attested, SHA-pinned prenatal control group on a matching
reference and protocol.  This adapter makes no trisomy call or clinical cutoff.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pysam
from traceback_runner.contained_process import ContainedProcessError, run_contained

from ..fetal_contract import FetalContractError, inspect_contract
from ..file_access import open_regular_no_follow
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric

R_CODE = r'''suppressPackageStartupMessages(library(NIPTeR))
args <- commandArgs(trailingOnly=TRUE)
if (length(args) != 3L || as.character(packageVersion("NIPTeR")) != "1.0.2") stop("NIPTeR version mismatch")
if (getRversion() < "4.4.0") stop("R 4.4.0 or newer is required to read control RDS")
control <- readRDS(args[[2L]])
if (!inherits(control, "NIPTControlGroup") || is.null(control$samples) ||
    length(control$samples) != as.integer(args[[3L]])) stop("invalid NIPTeR control group")
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
cat("END\n")
'''
SCRIPT_SHA256 = hashlib.sha256(R_CODE.encode()).hexdigest()
_CONTIGS = tuple(f"chr{chrom}" for chrom in range(1, 23)) + ("chrX", "chrY")
# UCSC hg38 primary chromosome sizes, in _CONTIGS order.
_HG38_LENGTHS = (
    248_956_422, 242_193_529, 198_295_559, 190_214_555, 181_538_259, 170_805_979,
    159_345_973, 145_138_636, 138_394_717, 133_797_422, 135_086_622, 133_275_309,
    114_364_328, 107_043_718, 101_991_189, 90_338_345, 83_257_441, 80_373_285,
    58_617_616, 64_444_167, 46_709_983, 50_818_468, 156_040_895, 57_227_415,
)
MAX_PACKAGE_ENTRIES = 10000
MAX_PACKAGE_BYTES = 256 << 20


@dataclass(frozen=True)
class _RResult:
    outcome: str
    returncode: int | None
    stdout: bytes
    stdout_truncated: bool


def _run_bounded_r(argv: list[str], *, cwd: Path, timeout_seconds: int,
                   log_limit_bytes: int = 4096, contained: bool = False) -> _RResult:
    """Bound both pipes; contain pre-worker probes, inherit the worker group in run.

    The Lab pipeline kills the worker group on outer timeout or interruption.
    A nested contained process during extraction would escape that group kill.
    """

    if contained:
        try:
            result = run_contained(argv, cwd=cwd, env=dict(os.environ),
                                   timeout_seconds=timeout_seconds, log_limit_bytes=log_limit_bytes)
        except ContainedProcessError as exc:
            raise RuntimeError("R process could not start") from exc
        return _RResult(result.outcome, result.returncode, result.stdout, result.stdout_truncated)

    def drain(stream: Any, output: bytearray, truncated: list[bool]) -> None:
        try:
            while block := stream.read(8192):
                room = log_limit_bytes - len(output)
                output.extend(block[:room])
                if len(block) > room:
                    truncated[0] = True
        finally:
            stream.close()

    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=dict(os.environ), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError("R process could not start") from exc
    stdout, stderr = bytearray(), bytearray()
    stdout_truncated, stderr_truncated = [False], [False]
    assert process.stdout is not None and process.stderr is not None
    readers = [threading.Thread(target=drain, args=(process.stdout, stdout, stdout_truncated), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, stderr, stderr_truncated), daemon=True)]
    try:
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_seconds)
            outcome = "exited"
        except subprocess.TimeoutExpired:
            outcome = "timeout"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        return _RResult("pipe_unclosed", None, bytes(stdout), True)
    return _RResult(outcome, process.returncode if outcome == "exited" else None,
                    bytes(stdout), stdout_truncated[0])


def _compatible_bam_layout(path: Path) -> bool:
    try:
        with pysam.AlignmentFile(str(path), "rb") as bam:
            return (tuple(bam.references[:24]) == _CONTIGS
                    and bam.header.get("HD", {}).get("SO") == "coordinate"
                    and tuple(bam.lengths[:24]) == _HG38_LENGTHS)
    except (OSError, ValueError, IndexError):
        return False


def _stage_control(path: Path, expected_sha256: str, out: Path) -> Path:
    """Copy a verified control snapshot for R, closing the check-to-open gap."""

    staged: Path | None = None
    completed = False
    try:
        source_fd = open_regular_no_follow(path)
        try:
            before = os.fstat(source_fd)
            if not 1 <= before.st_size <= (2 << 30):
                raise RuntimeError("registered NIPTeR control size changed")
            output_fd, name = tempfile.mkstemp(prefix="nipter-control-", suffix=".rds", dir=out)
            staged = Path(name)
            digest = hashlib.sha256()
            with os.fdopen(output_fd, "wb") as output:
                while block := os.read(source_fd, 1 << 20):
                    digest.update(block)
                    output.write(block)
            after = os.fstat(source_fd)
            def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

            if identity(before) != identity(after) or digest.hexdigest() != expected_sha256:
                raise RuntimeError("registered NIPTeR control changed during snapshot")
            completed = True
            return staged
        finally:
            os.close(source_fd)
    except OSError as exc:
        raise RuntimeError("registered NIPTeR control could not be snapshotted") from exc
    finally:
        if staged is not None and not completed:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


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


def _nipter_package_sha(rscript: Path, *, contained: bool = True) -> str | None:
    packages = ("NIPTeR", "sets", "Rsamtools", "S4Vectors")
    try:
        result = _run_bounded_r(
            [str(rscript), "--vanilla", "-e",
             "for (p in c('NIPTeR','sets','Rsamtools','S4Vectors')) { "
             "cat(as.character(packageVersion(p)), system.file(package=p), sep='\\t'); "
             "cat('\\n') }"],
            cwd=Path.cwd().resolve(), timeout_seconds=20,
            log_limit_bytes=4096,
            contained=contained,
        )
    except RuntimeError:
        return None
    if result.outcome != "exited" or result.stdout_truncated:
        return None
    lines = [line.split("\t") for line in result.stdout.decode("utf-8", errors="replace").splitlines()]
    if (result.returncode != 0 or len(lines) != len(packages)
            or any(len(line) != 2 for line in lines) or lines[0][0] != "1.0.2"):
        return None
    try:
        digest = hashlib.sha256()
        total = 0
        files = 0
        entries = 0

        def frame(value: bytes) -> None:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        for name, (version, directory) in zip(packages, lines):
            package = Path(directory)
            if not version or not package.is_absolute() or package.is_symlink() or not package.is_dir():
                return None
            frame(name.encode())
            frame(version.encode())
            pending = [package]
            regular_files: list[Path] = []
            while pending:
                with os.scandir(pending.pop()) as children:
                    for child in children:
                        entries += 1
                        if entries > MAX_PACKAGE_ENTRIES:
                            return None
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(child.path))
                        elif stat.S_ISREG(info.st_mode):
                            regular_files.append(Path(child.path))
                        else:
                            return None
            for path in sorted(regular_files, key=lambda item: item.relative_to(package).as_posix()):
                fd = open_regular_no_follow(path)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        return None
                    total += before.st_size
                    files += 1
                    if total > MAX_PACKAGE_BYTES or files > MAX_PACKAGE_ENTRIES:
                        return None
                    frame(path.relative_to(package).as_posix().encode())
                    digest.update(before.st_size.to_bytes(8, "big"))
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        while block := stream.read(1 << 20):
                            digest.update(block)
                    after = os.fstat(fd)
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                            before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                                   after.st_mtime_ns, after.st_ctime_ns):
                        return None
                finally:
                    os.close(fd)
        return digest.hexdigest() if files else None
    except OSError:
        return None


def _parse_z_output(output: str) -> dict[int, float]:
    if not output.endswith("END\n"):
        raise RuntimeError("NIPTeR returned an incomplete z-score table")
    lines = output.splitlines()
    if len(lines) != 4 or lines[-1] != "END":
        raise RuntimeError("NIPTeR returned an unexpected z-score table")
    lines = lines[:3]
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
    tools: tuple[ToolPin, ...] = (ToolPin("NIPTeR", "1.0.2", "nipter-lgpl"),)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "trisomy_statistic": "NIPTeR standard z-score", "control": "attested matched NIPTControlGroup",
        "fetal_fraction_model": "unavailable", "r_script_sha256": SCRIPT_SHA256,
    })

    def asset_sha256s_for_input(self, root: Path, input_id: str,
                               *, contained: bool = True) -> list[str]:
        from ..store import LabStore

        try:
            contract = inspect_contract(LabStore(root), input_id)
            runtime = _rscript()
            package_sha = _nipter_package_sha(runtime[0], contained=contained) if runtime else None
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
        if not (ctx.input_dir / "aligned.bam").is_file():
            return Skip("upstream", "aligned prenatal input BAM is unavailable")
        if not _compatible_bam_layout(ctx.input_dir / "aligned.bam"):
            return Skip("reference", "NIPTeR requires canonical hg38 chr1-22,X,Y BAM layout")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        if ctx.derived is not None:
            raise RuntimeError("NIPTeR requires the full prenatal input")
        if contract.source_id != ctx.source_id or contract.reference_sha256 != ctx.reference_sha256:
            raise RuntimeError("prenatal assertion or reference changed after preflight")
        runtime = _rscript()
        if runtime is None:
            raise RuntimeError("NIPTeR toolchain changed after preflight")
        package_sha = _nipter_package_sha(runtime[0], contained=False)
        if package_sha is None:
            raise RuntimeError("NIPTeR package changed after preflight")
        expected = {contract.manifest_sha256, contract.control_sha256, SCRIPT_SHA256, runtime[1], package_sha}
        if not expected.issubset(set(ctx.extras.get("planned_asset_sha256s", ()))):
            raise RuntimeError("prenatal control or Rscript changed after preflight")
        if not _compatible_bam_layout(ctx.input_dir / "aligned.bam"):
            raise RuntimeError("NIPTeR BAM reference layout changed after preflight")
        out.mkdir(parents=True, exist_ok=True)
        script = out / "nipter-z.R"
        script.write_bytes(R_CODE.encode("utf-8"))
        try:
            with tempfile.TemporaryDirectory(prefix="nipter-private-", dir=out.parent) as private:
                control_snapshot = _stage_control(contract.control_path, contract.control_sha256, Path(private))
                completed = _run_bounded_r(
                    [str(runtime[0]), "--vanilla", str(script), str(ctx.input_dir / "aligned.bam"),
                     str(control_snapshot), str(contract.control_n)],
                    cwd=out, timeout_seconds=900, log_limit_bytes=4096,
                )
        except (RuntimeError, OSError) as exc:
            raise RuntimeError("NIPTeR control or process unavailable; inspect the registered toolchain") from exc
        finally:
            script.unlink(missing_ok=True)
        if completed.outcome != "exited" or completed.returncode != 0 or completed.stdout_truncated:
            raise RuntimeError("NIPTeR trisomy calculation failed; inspect the registered control and toolchain")
        values = _parse_z_output(completed.stdout.decode("utf-8", errors="replace"))
        if self.asset_sha256s_for_input(ctx.root, ctx.input_id, contained=False) != list(ctx.extras["planned_asset_sha256s"]):
            raise RuntimeError("prenatal control or Rscript changed during execution")
        return AlgorithmOutput(metrics=[
            metric("fetal_fraction", None, unit="fraction"),
            *(metric(f"trisomy_{chrom}_z", values[chrom], unit="z-score", n=contract.control_n)
              for chrom in (13, 18, 21)),
        ])


FETAL_FRACTION = FetalFraction("fetal-fraction")
