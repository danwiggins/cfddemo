"""Exploratory ONT tumour/normal deconvolution through a trusted MethylBERT bundle.

The trained checkpoint is operator supplied. No model is distributed with the
Lab, and a missing model, pinned package, modified-base input, or GPU skips.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pysam

from ..assets import AssetError
from ..methylbert_assets import copy_methylbert_bundle, inspect_methylbert_bundle
from ..methylbert_bundle import BundleError, unpack_bundle, verify_bundle
from ..methylbert_runtime import (
    MIN_ELIGIBLE_READS, PINNED_UPSTREAM_COMMIT, RuntimeRefusal, numerical_runtime_identity,
    require_ready, run_worker,
)
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip, ToolPin
from ._base import Base, load_summary, metric
from ._records import eligible_record, name_predicate
from .cell_origin import header_model

_CANONICAL = re.compile(r"^chr(?:[1-9]|1[0-9]|2[0-2]|X|Y)$")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(16 << 20):
            value.update(block)
    return value.hexdigest()


def _sha(text: object) -> str | None:
    return text if isinstance(text, str) and len(text) == 64 and all(c in "0123456789abcdef" for c in text) else None


def _ont_header(path: Path) -> bool:
    with pysam.AlignmentFile(str(path), "rb") as bam:
        groups = bam.header.to_dict().get("RG", [])
        return bool(groups) and all(str(group.get("PL", "")).upper() in {"ONT", "OXFORDNANOPORE"} for group in groups)


def _eligible_tagged(ctx: InputContext) -> tuple[int, int]:
    summary, mask = load_summary(ctx)
    eligible = summary.eligible()
    if mask is not None:
        eligible &= mask
    return int(np.count_nonzero(eligible)), int(np.count_nonzero(eligible & (summary.column("has_mm") == 1)))


def _write_eligible_bam(source: Path, destination: Path, ctx: InputContext) -> int:
    """Give upstream only D21-eligible, selected rows, preserving MM/ML tags."""

    keep_name = name_predicate(ctx.derived)
    kept = 0
    with pysam.AlignmentFile(str(source), "rb") as reader:
        canonical = {name for name in reader.references if _CANONICAL.fullmatch(name)}
        if not canonical:
            canonical = set(reader.references)
        with pysam.AlignmentFile(str(destination), "wb", template=reader) as writer:
            for record in reader.fetch(until_eof=True):
                if not eligible_record(record, canonical):
                    continue
                if keep_name is not None and not keep_name(record.query_name or ""):
                    continue
                writer.write(record)
                kept += 1
    pysam.index(str(destination))
    return kept


def _offline_env(scratch: Path) -> dict[str, str]:
    """No inherited Python search path, proxy, or model-hub network setting."""

    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES")
           if name in os.environ}
    env.update({
        "HOME": str(scratch), "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "HF_HOME": str(scratch / "hf"), "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false", "NO_PROXY": "*",
    })
    return env


@dataclass
class Methylbert(Base):
    family: str = "methylation"
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=MIN_ELIGIBLE_READS))
    consumes: str = "full_bam"
    cost: str = "slow"
    tools: tuple[ToolPin, ...] = (ToolPin("methylbert", PINNED_UPSTREAM_COMMIT, "methylbert-mit"),)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "method": "methylbert tumour/normal deconvolution", "input": "Dorado MM/ML ONT BAM",
        "minimum_eligible_reads": MIN_ELIGIBLE_READS, "worker": "offline-private-v1",
    })

    def identity_assets(self, ctx: InputContext) -> list[str]:
        return [value for key in ("methylbert_bundle_sha256", "methylbert_code_sha256")
                if (value := _sha(ctx.extras.get(key))) is not None]

    def identity_parameters(self, ctx: InputContext) -> dict[str, Any]:
        try:
            model = header_model(ctx.input_dir / "aligned.bam")
        except (OSError, ValueError):
            model = None
        probe_gpu = (_sha(ctx.extras.get("methylbert_bundle_sha256")) is not None
                     and _sha(ctx.extras.get("methylbert_code_sha256")) is not None
                     and type(ctx.extras.get("methylbert_min_classified_reads")) is int)
        return {**self.parameters, "basecall_model": model,
                "bundle_sha256": ctx.extras.get("methylbert_bundle_sha256"),
                "code_sha256": ctx.extras.get("methylbert_code_sha256"),
                "min_classified_reads": ctx.extras.get("methylbert_min_classified_reads"),
                "numerical_runtime": numerical_runtime_identity(probe_gpu=probe_gpu),
                "child_sha256": _digest(Path(__file__).resolve().parents[1] / "methylbert_child.py")}

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        if ctx.example:
            return Skip("example_reference", "MethylBERT needs a real ONT input and a trusted tumour/normal checkpoint")
        if ctx.eligible_reads is None:
            return Skip("depth", "eligible-read depth has not been measured")
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        bundle_sha = _sha(ctx.extras.get("methylbert_bundle_sha256"))
        code_sha = _sha(ctx.extras.get("methylbert_code_sha256"))
        if bundle_sha is None or code_sha is None:
            return Skip("missing_asset", "supply a trusted registered bundle and installed-code SHA-256")
        minimum_support = ctx.extras.get("methylbert_min_classified_reads")
        if type(minimum_support) is not int or not 1 <= minimum_support <= ctx.eligible_reads:
            return Skip("unqualified_support", "supply an independently established minimum classified-read count")
        fasta_text = ctx.extras.get("reference_fasta")
        if not isinstance(fasta_text, str):
            return Skip("reference", "the registered reference FASTA is unavailable")
        bam = ctx.input_dir / "aligned.bam"
        try:
            if not _ont_header(bam):
                return Skip("platform", "every BAM read group must declare ONT")
            if header_model(bam) is None:
                return Skip("missing_tags", "the BAM has no single header-declared modified-base model")
            eligible, tagged = _eligible_tagged(ctx)
        except (OSError, ValueError) as exc:
            return Skip("input_kind", f"the ONT BAM or read summary cannot be inspected: {type(exc).__name__}")
        if eligible != ctx.eligible_reads:
            raise RuntimeError("eligible depth differs from the verified read summary")
        if tagged == 0:
            return Skip("missing_tags", "no eligible read carries MM/Mm methylation tags")
        # Store imports the CLI problem contract; keep it out of worker startup.
        from ..store import LabStore

        try:
            record = inspect_methylbert_bundle(LabStore(ctx.root), bundle_sha)
            bundle = verify_bundle(record.file)
        except (AssetError, BundleError) as exc:
            directory = LabStore(ctx.root).lab / "assets" / "methylbert-bundle" / bundle_sha
            if directory.exists() or directory.is_symlink():
                raise RuntimeError("registered MethylBERT bundle failed integrity verification") from exc
            return Skip("missing_asset", f"the trusted MethylBERT bundle is unavailable: {type(exc).__name__}")
        try:
            import torch
            gpu = bool(torch.cuda.is_available())
        except (ImportError, OSError, RuntimeError):
            gpu = False
        if not gpu:
            return Skip("missing_gpu", "MethylBERT needs a usable CUDA GPU")
        runtime = numerical_runtime_identity(probe_gpu=True)
        if runtime["cuda"] is None or not runtime["cuda"]["available"]:
            return Skip("missing_gpu", "MethylBERT CUDA device could not be identified")
        if any(version is None for version in runtime["packages"].values()):
            return Skip("model_not_ready", "MethylBERT numerical runtime packages are incomplete")
        try:
            require_ready(
                bundle, reference_sha256=ctx.reference_sha256, eligible_reads=eligible,
                platform="ont", gpu_available=gpu, expected_code_sha256=code_sha,
            )
        except RuntimeRefusal as exc:
            return Skip("model_not_ready", str(exc))
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        decision = self.preflight(ctx)
        if isinstance(decision, Skip):
            raise RuntimeError(f"MethylBERT readiness changed after preflight: {decision.reason_code}")
        bundle_sha = str(ctx.extras["methylbert_bundle_sha256"])
        code_sha = str(ctx.extras["methylbert_code_sha256"])
        fasta = Path(str(ctx.extras["reference_fasta"]))
        if _digest(fasta) != ctx.reference_sha256:
            raise RuntimeError("the registered reference FASTA changed since input alignment")
        child = Path(__file__).resolve().parents[1] / "methylbert_child.py"
        planned = ctx.extras.get("planned_parameters", {})
        if _digest(child) != planned.get("child_sha256"):
            raise RuntimeError("MethylBERT child code changed since execution identity was selected")
        if numerical_runtime_identity(probe_gpu=True) != planned.get("numerical_runtime"):
            raise RuntimeError("MethylBERT numerical runtime changed since execution identity was selected")
        with tempfile.TemporaryDirectory(prefix="methylbert-", dir=out.parent) as private_name:
            private = Path(private_name)
            private.chmod(0o700)
            archive = copy_methylbert_bundle(LabStore(ctx.root), bundle_sha, private / "model.tar")
            bundle = unpack_bundle(archive, private / "model")
            bam = private / "eligible.bam"
            if _write_eligible_bam(ctx.input_dir / "aligned.bam", bam, ctx) != ctx.eligible_reads:
                raise RuntimeError("MethylBERT eligible BAM differs from measured read-summary depth")
            request = private / "request.json"
            aggregate_path = private / "aggregate.json"
            request.write_text(json.dumps({
                "bam": str(bam), "reference": str(fasta), "model_dir": str(private / "model"),
                "scratch": str(private), "aggregate": str(aggregate_path),
                "code_sha256": code_sha, "reference_sha256": ctx.reference_sha256,
                "model_id": bundle.model_id,
                "n_mers": bundle.n_mers, "seq_len": bundle.seq_len,
                "eligible_reads": ctx.eligible_reads,
                "min_classified_reads": ctx.extras["methylbert_min_classified_reads"],
                "numerical_runtime": planned["numerical_runtime"],
            }), encoding="utf-8")
            aggregate = run_worker(
                [sys.executable, "-I", "-X", f"pycache_prefix={private / 'pycache'}", str(child), str(request)],
                scratch=private, env=_offline_env(private), aggregate_path=aggregate_path,
                model_id=bundle.model_id, eligible_reads=int(ctx.eligible_reads),
            )
            if aggregate.classified_reads < int(ctx.extras["methylbert_min_classified_reads"]):
                raise RuntimeError("MethylBERT classified-read support is below the qualified floor")
        provenance = {
            "bundle_sha256": bundle_sha, "installed_code_sha256": code_sha,
            "child_sha256": planned["child_sha256"],
            "numerical_runtime": planned["numerical_runtime"],
            "reference_sha256": ctx.reference_sha256, "model_id": bundle.model_id,
            "n_mers": bundle.n_mers, "seq_len": bundle.seq_len,
            "dmrs_sha256": bundle.files.get("dmrs.tsv"),
            "training_table_sha256": bundle.files.get("train_seq.csv"),
            "basecall_model": header_model(ctx.input_dir / "aligned.bam"),
            "eligible_reads": ctx.eligible_reads, "classified_reads": aggregate.classified_reads,
            "min_classified_reads": ctx.extras["methylbert_min_classified_reads"],
        }
        return AlgorithmOutput(
            metrics=[
                metric("tumour_fraction", aggregate.tumour_fraction, unit="fraction", n=aggregate.classified_reads),
                metric("normal_fraction", aggregate.normal_fraction, unit="fraction", n=aggregate.classified_reads),
                metric("classified_reads", aggregate.classified_reads, unit="reads", n=ctx.eligible_reads),
            ],
            artifacts={"methylbert_provenance.json": json.dumps(provenance, separators=(",", ":")).encode()},
        )


METHYLBERT = Methylbert("methylbert")
