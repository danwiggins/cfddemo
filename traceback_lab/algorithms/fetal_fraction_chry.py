"""Exploratory male-fetus chrY fraction under an operator-verified calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..fetal_chry_contract import (
    COUNTING_PROTOCOL, FetalChrYContract, FetalChrYContractError, inspect_contract,
)
from ..read_summary import CHUNK_ROWS, FLAG_EXCLUDE, MIN_MAPQ
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric
from .sex_check import HG38_CHRY_LENGTH, HG38_CHRY_PARS

_CONTIGS = tuple(f"chr{chrom}" for chrom in range(1, 23)) + ("chrX", "chrY")
_HG38_CHR1_LENGTH = 248_956_422
_F1_MIN_MAPQ = 20
_F1_FLAG_EXCLUDE = 0x4 | 0x100 | 0x200 | 0x400 | 0x800
_F1_PARS = ((10_000, 2_781_479), (56_887_902, 57_217_415))
_UNAVAILABLE = Skip("missing_asset", "Verified fetal chrY calibration is unavailable for this input")


def _protocol_matches() -> bool:
    """Refuse old calibrations if shared D21 or hg38 counting policy changes."""

    return (MIN_MAPQ == _F1_MIN_MAPQ and FLAG_EXCLUDE == _F1_FLAG_EXCLUDE
            and HG38_CHRY_PARS == _F1_PARS and HG38_CHRY_LENGTH == 57_227_415)


def _counts(summary: Any) -> tuple[int, int]:
    """D21 denominator and non-PAR chrY numerator in bounded summary slices."""

    flags = summary.column("flag")
    mapqs = summary.column("mapq")
    contigs = summary.column("contig")
    starts = summary.column("start")
    eligible_count = y_count = 0
    for offset in range(0, summary.rows, CHUNK_ROWS):
        end = min(offset + CHUNK_ROWS, summary.rows)
        chrom = contigs[offset:end]
        eligible = (((flags[offset:end] & FLAG_EXCLUDE) == 0)
                    & (mapqs[offset:end] >= MIN_MAPQ)
                    & (chrom >= 0) & (chrom < 24))
        eligible_count += int(np.count_nonzero(eligible))
        y_rows = eligible & (chrom == 23)
        position = starts[offset:end]
        for low, high in HG38_CHRY_PARS:
            y_rows &= ~((position >= low) & (position < high))
        y_count += int(np.count_nonzero(y_rows))
    return y_count, eligible_count


def _fraction(y_count: int, denominator: int, contract: FetalChrYContract) -> float | None:
    if denominator <= 0:
        return None
    observed = y_count * 1_000_000 / denominator
    background = contract.female_background_y_per_million
    full = contract.full_male_y_per_million
    if not background <= observed <= full:
        return None
    estimate = (observed - background) / (full - background)
    if not contract.validated_min_fraction <= estimate <= contract.validated_max_fraction:
        return None
    return estimate


@dataclass
class FetalFractionChrY(Base):
    family: str = "copy-number"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=2_000_000))
    parameters: dict[str, Any] = field(default_factory=lambda: {
        "model": "matched_female_background_to_full_male_linear_response",
        "count": COUNTING_PROTOCOL, "calibration": "input_scoped_sha256",
    })

    def asset_sha256s_for_input(self, root: Path, input_id: str) -> list[str]:
        from ..store import LabStore

        try:
            return [inspect_contract(LabStore(root), input_id).manifest_sha256]
        except (FetalChrYContractError, OSError):
            return []

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if ctx.eligible_reads is None:
            return Skip("depth", "fetal chrY fraction needs at least 2,000,000 eligible reads")
        if ctx.derived is not None:
            return Skip("unsupported_input", "fetal chrY calibration applies to the full input")
        if not _protocol_matches():
            return Skip("unsupported_input", "fetal chrY counting protocol has changed")
        from ..store import LabStore

        try:
            contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        except FetalChrYContractError:
            return _UNAVAILABLE
        if contract.source_id != ctx.source_id:
            return _UNAVAILABLE
        if contract.reference_sha256 != ctx.reference_sha256:
            return _UNAVAILABLE
        if contract.calibration_status != "operator_verified":
            return _UNAVAILABLE
        if list(ctx.extras.get("planned_asset_sha256s", ())) != [contract.manifest_sha256]:
            return _UNAVAILABLE
        if (ctx.approx_depth_x is None or not math.isfinite(ctx.approx_depth_x)
                or ctx.approx_depth_x < contract.min_depth_x):
            return _UNAVAILABLE
        summary, _ = load_summary(ctx)
        if (tuple(summary.contigs[:24]) != _CONTIGS or summary.lengths[0] != _HG38_CHR1_LENGTH
                or summary.lengths[23] != HG38_CHRY_LENGTH):
            return Skip("reference", "fetal calibration requires canonical hg38 chr1-22,X,Y layout")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        from ..store import LabStore

        if not isinstance(self.preflight(ctx), Ready):
            raise RuntimeError("fetal calibration or input changed after preflight")
        contract = inspect_contract(LabStore(ctx.root), ctx.input_id)
        summary, _ = load_summary(ctx)
        y_count, denominator = _counts(summary)
        if denominator != ctx.eligible_reads:
            raise RuntimeError("fetal read count changed after preflight")
        estimate = _fraction(y_count, denominator, contract)
        if self.asset_sha256s_for_input(ctx.root, ctx.input_id) != [contract.manifest_sha256]:
            raise RuntimeError("fetal calibration changed during execution")
        return AlgorithmOutput(metrics=[
            metric("fetal_fraction", estimate, unit="fraction", n=denominator),
            metric("fetal_chry_nonpar_reads", y_count, unit="reads", n=denominator),
            metric("fetal_chry_reads_per_million", y_count * 1_000_000 / denominator if denominator else None,
                   unit="reads per million", n=denominator),
        ])


FETAL_FRACTION_CHRY = FetalFractionChrY("fetal-fraction-chry")
