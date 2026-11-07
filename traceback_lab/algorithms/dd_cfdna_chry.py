"""Exploratory male-donor/female-recipient chrY fraction with a pinned calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..dd_contract import COUNTING_PROTOCOL, DonorContract, DonorContractError, inspect_contract
from ..derived_calibration import calibrated_input_id
from ..read_summary import CHUNK_ROWS, FLAG_EXCLUDE, MIN_MAPQ
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric
from .sex_check import HG38_CHRY_LENGTH, HG38_CHRY_PARS

_EXPECTED_CONTIGS = tuple(f"chr{chrom}" for chrom in range(1, 23)) + ("chrX", "chrY")
_HG38_CHR1_LENGTH = 248_956_422
_D21_MIN_MAPQ = 20
_D21_FLAG_EXCLUDE = 0x4 | 0x100 | 0x200 | 0x400 | 0x800
_D21_PARS = ((10_000, 2_781_479), (56_887_902, 57_217_415))


def _protocol_matches() -> bool:
    """Reject a pinned calibration if shared counting semantics change."""

    return (MIN_MAPQ == _D21_MIN_MAPQ and FLAG_EXCLUDE == _D21_FLAG_EXCLUDE
            and HG38_CHRY_PARS == _D21_PARS and HG38_CHRY_LENGTH == 57_227_415)


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


def _count_reads(summary: Any, mask: np.ndarray | None = None) -> tuple[int, int]:
    """Count D21 reads in bounded slices of the memmapped summary columns."""

    flags = summary.column("flag")
    mapqs = summary.column("mapq")
    contigs = summary.column("contig")
    starts = summary.column("start")
    eligible_count = y_count = 0
    for offset in range(0, summary.rows, CHUNK_ROWS):
        end = min(offset + CHUNK_ROWS, summary.rows)
        chunk_contigs = contigs[offset:end]
        eligible = (((flags[offset:end] & FLAG_EXCLUDE) == 0)
                    & (mapqs[offset:end] >= MIN_MAPQ)
                    & (chunk_contigs >= 0) & (chunk_contigs < 24))
        if mask is not None:
            eligible &= mask[offset:end]
        eligible_count += int(np.count_nonzero(eligible))
        y_rows = eligible & (chunk_contigs == 23)
        chunk_starts = starts[offset:end]
        for low, high in HG38_CHRY_PARS:
            y_rows &= ~((chunk_starts >= low) & (chunk_starts < high))
        y_count += int(np.count_nonzero(y_rows))
    return y_count, eligible_count


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
            store = LabStore(root)
            parent = calibrated_input_id(store, input_id)
            return [inspect_contract(store, parent).manifest_sha256] if parent is not None else []
        except (DonorContractError, OSError):
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.derived is not None and ctx.eligible_reads is None:
            return Skip("depth", "derived donor read count is unavailable")
        if ctx.derived is not None and ctx.derived.get("transform") not in ("downsample", "split"):
            return Skip("unsupported_input", "donor calibration supports only selected read subsets")
        if not _protocol_matches():
            return Skip("unsupported_input", "donor chrY counting protocol has changed")
        from ..store import LabStore

        store = LabStore(ctx.root)
        parent = calibrated_input_id(store, ctx.input_id, ctx.derived)
        if parent is None or (ctx.derived is None) != (parent == ctx.input_id):
            return Skip("unsupported_input", "derived donor input has no verified calibrated parent")
        try:
            contract = inspect_contract(store, parent)
        except DonorContractError as exc:
            return Skip("missing_asset", f"verified transplant calibration unavailable: {exc}")
        if contract.source_id != ctx.source_id:
            return Skip("unsupported_input", "transplant calibration is bound to a different source")
        if contract.reference_sha256 != ctx.reference_sha256:
            return Skip("reference", "transplant calibration is bound to a different reference")
        if contract.calibration_status != "operator_verified":
            return Skip("missing_asset", "synthetic calibration is for mechanics only")
        if list(ctx.extras.get("planned_asset_sha256s", ())) != [contract.manifest_sha256]:
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

        store = LabStore(ctx.root)
        parent = calibrated_input_id(store, ctx.input_id, ctx.derived)
        if parent is None:
            raise RuntimeError("derived donor binding changed after preflight")
        contract = inspect_contract(store, parent)
        if list(ctx.extras.get("planned_asset_sha256s", ())) != [contract.manifest_sha256]:
            raise RuntimeError("consumed donor calibration differs from planned result identity")
        if not isinstance(self.preflight(ctx), Ready):
            raise RuntimeError("donor calibration or input changed after preflight")
        summary, mask = load_summary(ctx)
        y_reads, denominator = _count_reads(summary, mask)
        if ctx.derived is not None and denominator != ctx.eligible_reads:
            raise RuntimeError("derived donor read selection changed after preflight")
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
