"""Griffin TF-site nucleosome profile through the pinned upstream workflows.

Only compatible paired-end hg38 WGS inputs can run the original code. Native
ONT single-end BAMs skip explicitly; no surrogate reads or site-density
approximation is substituted for Griffin.
"""

from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters.griffin_run import (
    GRIFFIN_COMMIT, MIN_ELIGIBLE_AUTOSOMAL_PAIRS, GriffinRun, _require_paired_input,
    count_d21_pairs, planned_runtime_sha256, run_upstream,
)
from ..griffin_assets import GriffinAssetError, inspect_griffin_assets, registration_available
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, metric


_SAFE_BAM_VALIDATION_MESSAGES = frozenset({
    "pinned Griffin requires unanimous @RG PL:ILLUMINA; native ONT single-end BAM is incompatible",
    "pinned Griffin requires paired-end positive-TLEN WGS reads; native ONT single-end BAM is incompatible",
    "Griffin requires unanimously Illumina-declared read groups",
    "Griffin requires canonical hg38 autosomes",
})


def _public_provenance(manifest: dict[str, Any], raw_sha256: str,
                       runtime_sha256: str) -> bytes:
    """Publish a path-free receipt; the full runtime record stays in the attempt."""
    fields = (
        "status", "input_id", "upstream_commit", "reference_id", "platform",
        "eligible_autosomal_pairs", "minimum_eligible_autosomal_pairs",
        "eligibility_source", "eligibility_predicate", "snakemake_version",
        "registration_sha256", "gc_bias_sha256", "output_sha256", "profile_count",
    )
    public = {key: manifest[key] for key in fields if key in manifest}
    public["full_provenance_sha256"] = raw_sha256
    public["runtime_identity_sha256"] = runtime_sha256
    return json.dumps(public, sort_keys=True, separators=(",", ":")).encode()


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

    def identity_tools(self, *, worker_pythonpath: str | None = None) -> tuple[ToolPin, ...]:
        return (*self.tools, ToolPin("Griffin runtime", planned_runtime_sha256(
            worker_pythonpath=worker_pythonpath,
        )))

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
        snakemake = shutil.which("snakemake")
        if snakemake is None or shutil.which("bedtools") is None or shutil.which("samtools") is None:
            return Skip("missing_toolchain", "Griffin needs Snakemake, bedtools and samtools in PATH")
        if importlib.util.find_spec("pyBigWig") is None or importlib.util.find_spec("pybedtools") is None:
            return Skip("missing_toolchain", "Griffin needs pyBigWig and pybedtools in the Lab Python environment")
        try:
            compat_dir = Path(__file__).parents[1] / "adapters" / "griffin_compat"
            version = subprocess.run(
                [sys.executable, snakemake, "--version"], capture_output=True, text=True,
                check=True, timeout=10,
                env={**os.environ, "PYTHONPATH": str(compat_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")},
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return Skip("missing_toolchain", "Griffin's Snakemake is unavailable to the Lab Python interpreter")
        if version != "5.19.2":
            return Skip("missing_toolchain", "Griffin requires tested Snakemake 5.19.2")
        if not registration_available(ctx.root):
            return Skip("missing_asset", "Griffin assets are not registered or registration is unsafe")
        try:
            profile_reads = _require_paired_input(ctx.input_dir / "aligned.bam")
            pairs = count_d21_pairs(ctx.input_dir / "aligned.bam")
        except OSError:
            return Skip("input_unavailable", "Griffin could not read aligned BAM; inspect the Lab input and retry")
        except ValueError as exc:
            message = str(exc)
            if message == "Griffin requires canonical hg38 autosomes":
                return Skip("reference", message)
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
        planned_assets = ctx.extras.get("planned_asset_sha256s")
        if planned_assets is not None and planned_assets != [assets.sha256]:
            raise RuntimeError("Griffin registration changed after execution identity was set")
        planned_tools = ctx.extras.get("planned_tool_identities")
        actual_tools = [tool.identity() for tool in self.identity_tools()]
        if planned_tools is not None and planned_tools != actual_tools:
            raise RuntimeError("Griffin runtime changed after execution identity was set")
        pairs = count_d21_pairs(ctx.input_dir / "aligned.bam")
        if pairs < MIN_ELIGIBLE_AUTOSOMAL_PAIRS:
            raise RuntimeError("Griffin pair count fell below the required 5M after preflight")
        profiles = run_upstream(GriffinRun(
            input_id=ctx.input_id, bam=ctx.input_dir / "aligned.bam",
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
                "griffin_provenance.json": _public_provenance(
                    provenance, hashlib.sha256(provenance_bytes).hexdigest(),
                    actual_tools[-1]["version"],
                ),
            },
        )


GRIFFIN = Griffin("griffin")
