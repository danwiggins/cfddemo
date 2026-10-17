"""Griffin TF-site nucleosome profile through the pinned upstream workflows.

Only compatible paired-end hg38 WGS inputs can run the original code. Native
ONT single-end BAMs skip explicitly; no surrogate reads or site-density
approximation is substituted for Griffin.
"""

from __future__ import annotations

import json
import hashlib
import importlib.util
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.griffin_run import (
    GRIFFIN_COMMIT, MIN_ELIGIBLE_READS, GriffinRun, _require_paired_input,
    count_d21_pairs, run_upstream,
)
from ..griffin_assets import GriffinAssetError, inspect_griffin_assets
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric


@dataclass
class Griffin(Base):
    family: str = "fragmentomics"
    consumes: str = "full_bam"
    cost: str = "slow"
    requires: Requirements = field(default_factory=lambda: Requirements(
        min_eligible_reads=MIN_ELIGIBLE_READS, platforms=frozenset({"illumina"}),
    ))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "method": "upstream-Griffin-v0.2", "reference": "hg38",
        "primary_metric": "GC_corrected_central_coverage",
        "minimum_d21_eligible": MIN_ELIGIBLE_READS,
    })
    tools: tuple[ToolPin, ...] = (
        ToolPin("Griffin", GRIFFIN_COMMIT, "method-patents"),
        ToolPin("Snakemake", "5.19.2"),
    )

    def asset_sha256s(self, root: Path) -> list[str]:
        try:
            return [inspect_griffin_assets(root).sha256]
        except GriffinAssetError:
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.platform != "illumina":
            return Skip("platform", "Griffin requires an Illumina-declared paired-end BAM")
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.example:
            return Skip("example_reference", "Griffin requires a genuine hg38 WGS reference and TF-site assets")
        if ctx.derived is not None:
            return Skip("derived_input", "Griffin requires the full aligned input; derived BAM support is not validated")
        if ctx.eligible_reads is None:
            return Skip("depth", "Griffin needs a D21 eligible-read count to enforce the 5M gate")
        if ctx.reference_id != "hg38" or not ctx.extras.get("reference_fasta"):
            return Skip("reference", "Griffin requires the registered hg38 FASTA")
        try:
            _require_paired_input(ctx.input_dir / "aligned.bam")
            pairs = count_d21_pairs(ctx.input_dir / "aligned.bam")
        except (OSError, ValueError) as exc:
            return Skip("unsupported_platform", str(exc))
        if pairs < MIN_ELIGIBLE_READS:
            return Skip("depth", f"Griffin needs {MIN_ELIGIBLE_READS:,} eligible autosomal pairs; this input has {pairs:,}")
        try:
            inspect_griffin_assets(ctx.root)
        except GriffinAssetError as exc:
            return Skip("missing_asset", str(exc))
        if shutil.which("snakemake") is None or shutil.which("bedtools") is None or shutil.which("samtools") is None:
            return Skip("missing_toolchain", "Griffin needs Snakemake, bedtools and samtools in PATH")
        if importlib.util.find_spec("pyBigWig") is None or importlib.util.find_spec("pybedtools") is None:
            return Skip("missing_toolchain", "Griffin needs pyBigWig and pybedtools in the Lab Python environment")
        try:
            version = subprocess.run(
                ["snakemake", "--version"], capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return Skip("missing_toolchain", "Griffin's Snakemake version could not be checked")
        if version != "5.19.2":
            return Skip("missing_toolchain", "Griffin requires tested Snakemake 5.19.2")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        if ctx.platform != "illumina":
            raise RuntimeError("Griffin cannot run on a non-Illumina input")
        assets = inspect_griffin_assets(ctx.root)
        pairs = count_d21_pairs(ctx.input_dir / "aligned.bam")
        if pairs < MIN_ELIGIBLE_READS:
            raise RuntimeError("Griffin pair count fell below the required 5M after preflight")
        profiles = run_upstream(GriffinRun(
            input_id=ctx.input_id, bam=ctx.input_dir / "aligned.bam",
            reference_fasta=Path(ctx.extras["reference_fasta"]),
            reference_id=ctx.reference_id, reference_sha256=ctx.reference_sha256,
            eligible_reads=pairs, upstream_root=assets.upstream_root,
            ref_assets=assets.ref_assets, site_lists=assets.site_lists,
            attempt_dir=out.parent / "griffin-attempt",
            registration_sha256=assets.sha256,
        ))
        primary = next((profile for profile in profiles if profile.site_name == assets.primary_site), None)
        if primary is None:
            raise RuntimeError("registered primary TF site list is absent from Griffin output")
        sample = "lab_" + ctx.input_id
        corrected = out.parent / "griffin-attempt" / "griffin_nucleosome_profiling" / "results" / sample / f"{sample}.GC_corrected.coverage.tsv"
        corrected_bytes = corrected.read_bytes()
        provenance_bytes = (out.parent / "griffin-attempt" / "provenance.json").read_bytes()
        provenance = json.loads(provenance_bytes)
        if (provenance.get("status") != "complete" or provenance.get("input_id") != ctx.input_id or
                provenance.get("upstream_commit") != GRIFFIN_COMMIT or
                provenance.get("platform") != "illumina" or
                provenance.get("registration_sha256") != assets.sha256 or
                provenance.get("output_sha256") != hashlib.sha256(corrected_bytes).hexdigest()):
            raise RuntimeError("Griffin output is not bound to this input and the pinned upstream run")
        panel = {
            "primary_site": assets.primary_site,
            "profiles": [
                {
                    "site_name": profile.site_name, "site_count": profile.site_count,
                    "bin_start_bp": list(profile.offsets_bp), "gc_corrected_coverage": list(profile.coverage),
                    "central_coverage": profile.central_coverage, "amplitude": profile.amplitude,
                    "smoothing": profile.smoothing, "cna_normalization": profile.cna_normalization,
                }
                for profile in profiles
            ],
        }
        return AlgorithmOutput(
            metrics=[
                metric("central_coverage", primary.central_coverage, unit="relative", n=primary.site_count),
                metric("site_count", primary.site_count, unit="sites", n=primary.site_count),
            ],
            panels=[{"panel_id": "tf-site-profile", "kind": "density", "data_ref": "artifacts/griffin_profile.json"}],
            artifacts={
                "griffin_profile.json": json.dumps(panel, separators=(",", ":")).encode(),
                "griffin_corrected_coverage.tsv": corrected_bytes,
                "griffin_provenance.json": provenance_bytes,
            },
        )


GRIFFIN = Griffin("griffin")
