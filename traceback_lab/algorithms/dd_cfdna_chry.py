"""Exploratory male-donor/female-recipient chrY fraction with a pinned calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..dd_contract import COUNTING_PROTOCOL, DonorContract, DonorContractError, inspect_contract
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric
from .sex_check import HG38_CHRY_LENGTH, HG38_CHRY_PARS

_EXPECTED_CONTIGS = tuple(f"chr{chrom}" for chrom in range(1, 23)) + ("chrX", "chrY")
_HG38_CHR1_LENGTH = 248_956_422


def _fraction(y_reads: int, eligible_reads: int, contract: DonorContract) -> float | None:
    """Linear response calibrated by matched female and full male-donor mixtures."""

    if eligible_reads <= 0:
        return None
    observed = y_reads * 1_000_000 / eligible_reads
    baseline = contract.female_background_y_per_million
    full = contract.male_full_donor_y_per_million
    if not baseline <= observed <= full:
        return None
    return (observed - baseline) / (full - baseline)


@dataclass
class DonorDerivedChrY(Base):
    family: str = "copy-number"
    requires: Requirements = field(default_factory=Requirements)
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "model": "matched_female_background_to_full_male_linear_response",
        "count": COUNTING_PROTOCOL, "calibration": "input_scoped_sha256",
    })

    def asset_sha256s_for_input(self, root: Path, input_id: str) -> list[str]:
        from ..store import LabStore

        try:
            return [inspect_contract(LabStore(root), input_id).manifest_sha256]
        except (DonorContractError, OSError):
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.derived is not None:
            return Skip("unsupported_input", "donor calibration applies to the full input")
        from ..store import LabStore

        try:
            contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        except DonorContractError as exc:
            return Skip("missing_asset", f"verified transplant calibration unavailable: {exc}")
        if contract.source_id != ctx.source_id or contract.reference_sha256 != ctx.reference_sha256:
            return Skip("reference", "transplant calibration is bound to a different input or reference")
        if contract.calibration_status != "operator_verified":
            return Skip("missing_asset", "synthetic calibration is for mechanics only")
        if contract.manifest_sha256 not in ctx.extras.get("planned_asset_sha256s", ()):
            return Skip("missing_asset", "transplant calibration changed after result identity was planned")
        if (ctx.approx_depth_x is None or not math.isfinite(ctx.approx_depth_x)
                or ctx.approx_depth_x < contract.min_depth_x):
            return Skip("depth", f"calibrated minimum is {contract.min_depth_x:g}x")
        summary, _ = load_summary(ctx)
        if (tuple(summary.contigs[:24]) != _EXPECTED_CONTIGS
                or summary.lengths[0] != _HG38_CHR1_LENGTH
                or summary.lengths[23] != HG38_CHRY_LENGTH):
            return Skip("reference", "calibration requires canonical hg38 chr1-22,X,Y layout")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        if not isinstance(self.preflight(ctx), Ready):
            raise RuntimeError("donor calibration or input changed after preflight")
        summary, _ = load_summary(ctx)
        eligible = summary.eligible()
        starts = summary.column("start")
        y_rows = eligible & (summary.column("contig") == 23)
        for low, high in HG38_CHRY_PARS:
            y_rows &= ~((starts >= low) & (starts < high))
        y_reads = int(np.count_nonzero(y_rows))
        denominator = int(np.count_nonzero(eligible))
        fraction = _fraction(y_reads, denominator, contract)
        if self.asset_sha256s_for_input(ctx.root, ctx.input_id) != [contract.manifest_sha256]:
            raise RuntimeError("donor calibration changed during execution")
        return AlgorithmOutput(metrics=[
            metric("dd_cfdna_fraction", fraction, unit="fraction", n=denominator),
            metric("chry_nonpar_reads", y_reads, unit="reads", n=denominator),
            metric("chry_reads_per_million", y_reads * 1_000_000 / denominator if denominator else None,
                   unit="reads per million", n=denominator),
        ])


DD_CFDNA_CHRY = DonorDerivedChrY("dd-cfdna-chry")
