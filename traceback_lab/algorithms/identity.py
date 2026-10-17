"""Somalier fingerprint for explicit, later pair comparison (SPEC L2).

Only a current input's opaque-named sketch is published. Relatedness is
computed by ``lab identity compare`` from two selected immutable attempts;
there is no automatic scan of donors and no duplicate cutoff.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..identity_sites import IdentitySitesError, inspect_sites
from ..file_access import open_regular_no_follow
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ..store import LabStore
from ._base import Base, metric

MIN_ELIGIBLE = 10_000_000
MAX_SKETCH_BYTES = 16 << 20
_VERSION = re.compile(r"somalier version:\s*([^\s]+)")


def somalier_tool() -> tuple[str, str] | None:
    path = shutil.which("somalier")
    if path is None:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return _probe_somalier(path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@lru_cache(maxsize=8)
def _probe_somalier(path: str, device: int, inode: int, size: int, mtime_ns: int) -> tuple[str, str] | None:
    """Cache one executable incarnation, not a process-lifetime tool selection."""

    try:
        found = subprocess.run([path, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION.search((found.stderr + found.stdout).decode("utf-8", "replace"))
    return (path, match.group(1)) if match else None


def _somalier_env(input_id: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("SOMALIER_")}
    env["SOMALIER_SAMPLE_NAME"] = input_id
    return env


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Identity(Base):
    consumes: str = "full_bam"
    cost: str = "slow"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=MIN_ELIGIBLE))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "sites": "operator-registered hg38 VCF", "sample_name": "full input SHA-256",
        "min_eligible_reads": MIN_ELIGIBLE,
    })

    @property  # type: ignore[override]
    def tools(self) -> tuple[ToolPin, ...]:
        found = somalier_tool()
        return (ToolPin("somalier", found[1] if found else "missing", "somalier-mit"),)

    @tools.setter
    def tools(self, value: tuple[ToolPin, ...]) -> None:
        # Base's generated initializer assigns this field; the live pin wins.
        pass

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return [inspect_sites(LabStore(root)).sha256]
        except IdentitySitesError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.example:
            return Skip("example_reference", "identity needs a registered hg38 reference, not the synthetic example")
        if somalier_tool() is None:
            return Skip("missing_toolchain", "Somalier is not installed")
        try:
            inspect_sites(LabStore(ctx.root))
        except IdentitySitesError:
            return Skip("missing_asset", "register Somalier hg38 sites with lab identity sites add")
        if not ctx.extras.get("reference_fasta"):
            return Skip("reference", "the registered reference FASTA is unavailable")
        if not (ctx.input_dir / "aligned.bam").is_file():
            return Skip("reference", "the aligned BAM is unavailable")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        tool = somalier_tool()
        if tool is None:
            raise RuntimeError("Somalier changed after preflight")
        try:
            sites = inspect_sites(LabStore(ctx.root))
        except IdentitySitesError as exc:
            raise RuntimeError("identity sites changed after preflight") from exc
        fasta = Path(ctx.extras["reference_fasta"])
        if _digest(fasta) != ctx.reference_sha256:
            raise RuntimeError("the registered reference FASTA changed")
        bam = ctx.input_dir / "aligned.bam"
        if ctx.derived:
            from .cell_origin import write_transient_bam

            bam = out / "derived.bam"
            write_transient_bam(ctx.input_dir / "aligned.bam", bam, dict(ctx.derived))
        destination = out / "somalier"
        destination.mkdir()
        process = subprocess.run(
            [tool[0], "extract", "--sites", str(sites.file), "--fasta", str(fasta),
             "--out-dir", str(destination), str(bam)],
            env=_somalier_env(ctx.input_id), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1800, check=False,
        )
        if process.returncode != 0:
            raise RuntimeError("Somalier extraction failed")
        sketch = destination / f"{ctx.input_id}.somalier"
        try:
            descriptor = open_regular_no_follow(sketch)
        except OSError as exc:
            raise RuntimeError("Somalier produced no fingerprint") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_SKETCH_BYTES:
                raise RuntimeError("Somalier fingerprint is unsafe or outside the size limit")
            data = os.read(descriptor, MAX_SKETCH_BYTES + 1)
            after = os.fstat(descriptor)
            if len(data) != info.st_size or (info.st_dev, info.st_ino, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_mtime_ns
            ):
                raise RuntimeError("Somalier fingerprint changed while being read")
        finally:
            os.close(descriptor)
        return AlgorithmOutput(
            metrics=[metric("fingerprint_bytes", len(data), unit="bytes")],
            artifacts={"fingerprint.somalier": data},
        )


IDENTITY = Identity("identity")
