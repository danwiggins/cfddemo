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
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.griffin_run import (
    GRIFFIN_COMMIT, MIN_ELIGIBLE_AUTOSOMAL_PAIRS, GriffinRun, _require_paired_input,
    count_d21_pairs, run_upstream,
)
from ..griffin_assets import GriffinAssetError, inspect_griffin_assets, registration_available
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric
from .cell_origin import derived_name_predicate, write_transient_bam


_SAFE_BAM_VALIDATION_MESSAGES = frozenset({
    "pinned Griffin requires unanimous @RG PL:ILLUMINA; native ONT single-end BAM is incompatible",
    "pinned Griffin requires paired-end positive-TLEN WGS reads; native ONT single-end BAM is incompatible",
    "Griffin requires unanimously Illumina-declared read groups",
    "Griffin requires canonical hg38 autosomes",
})


@dataclass
class Griffin(Base):
    family: str = "fragmentomics"
    consumes: str = "full_bam"
    cost: str = "slow"
    requires: Requirements = field(default_factory=lambda: Requirements(
        min_eligible_reads=MIN_ELIGIBLE_AUTOSOMAL_PAIRS, platforms=frozenset({"illumina"}),
    ))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "method": "upstream-Griffin-v0.2", "reference": "hg38",
        "primary_metric": "GC_corrected_central_coverage",
        "minimum_d21_eligible": MIN_ELIGIBLE_AUTOSOMAL_PAIRS,
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
        if ctx.derived is not None and ctx.derived.get("transform") not in {"downsample", "split"}:
            return Skip("derived_input", "Griffin supports only downsample and split selections")
        if ctx.eligible_reads is None:
            return Skip("depth", "Griffin needs a D21 eligible-read count to enforce the 5M gate")
        if ctx.reference_id != "hg38" or not ctx.extras.get("reference_fasta"):
            return Skip("reference", "Griffin requires the registered hg38 FASTA")
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
        if not registration_available(ctx.root):
            return Skip("missing_asset", "Griffin assets are not registered or registration is unsafe")
        try:
            bam = ctx.input_dir / "aligned.bam"
            if ctx.derived is not None:
                keep_name = derived_name_predicate(dict(ctx.derived))
                pairs = count_d21_pairs(bam, keep_name)
                if pairs < MIN_ELIGIBLE_AUTOSOMAL_PAIRS:
                    return Skip("depth", f"Griffin needs {MIN_ELIGIBLE_AUTOSOMAL_PAIRS:,} eligible autosomal pairs; this selection has {pairs:,}")
                profile_reads = _require_paired_input(bam, keep_name)
            else:
                profile_reads = _require_paired_input(bam)
                pairs = count_d21_pairs(bam)
        except OSError:
            return Skip("input_unavailable", "Griffin could not read aligned BAM; inspect the Lab input and retry")
        except (ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            if message == "Griffin requires canonical hg38 autosomes":
                return Skip("reference", message)
            if ctx.derived is not None and message not in _SAFE_BAM_VALIDATION_MESSAGES:
                return Skip("derived_input", "Griffin requires a valid downsample or split read-name selection")
            return Skip("platform", message if message in _SAFE_BAM_VALIDATION_MESSAGES else
                        "Griffin could not validate the aligned BAM for paired-end Illumina input")
        if profile_reads == 0:
            return Skip("depth", "Griffin's pinned 100–200 bp profile window has no qualifying fragments")
        if pairs < MIN_ELIGIBLE_AUTOSOMAL_PAIRS:
            return Skip("depth", f"Griffin needs {MIN_ELIGIBLE_AUTOSOMAL_PAIRS:,} eligible autosomal pairs; this input has {pairs:,}")
        try:
            inspect_griffin_assets(ctx.root)
        except GriffinAssetError as exc:
            return Skip("missing_asset", str(exc))
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        if ctx.platform != "illumina":
            raise RuntimeError("Griffin cannot run on a non-Illumina input")
        assets = inspect_griffin_assets(ctx.root)
        scratch = (tempfile.TemporaryDirectory(prefix="griffin-derived-", dir=out.parent)
                   if ctx.derived is not None else nullcontext(None))
        with scratch as directory:
            bam = ctx.input_dir / "aligned.bam"
            if ctx.derived is not None:
                bam = Path(directory) / "selected.bam"
                write_transient_bam(ctx.input_dir / "aligned.bam", bam, dict(ctx.derived))
            pairs = count_d21_pairs(bam)
            if pairs < MIN_ELIGIBLE_AUTOSOMAL_PAIRS:
                raise RuntimeError("Griffin pair count fell below the required 5M after preflight")
            profiles = run_upstream(GriffinRun(
                input_id=ctx.input_id, bam=bam,
                reference_fasta=Path(ctx.extras["reference_fasta"]),
                reference_id=ctx.reference_id, reference_sha256=ctx.reference_sha256,
                eligible_autosomal_pairs=pairs, upstream_root=assets.upstream_root,
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
                provenance.get("eligible_autosomal_pairs") != pairs or
                provenance.get("snakemake_version") != "5.19.2" or
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
