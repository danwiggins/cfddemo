"""``modbase-qc``: can the methylation calls be trusted (Appendix A).

- ``mod_codes``: the mod codes declared in the first 10,000 MM tags of
  D21-eligible records (``C+m`` only means no 5hmC channel);
- ``mm_tagged_fraction``: eligible records carrying MM / eligible records
  (from the read summary);
- ``ml_mean``: mean ML/255 over every C+m and C+h value at CpG calls;
- ``ml_below_filter_fraction``: share of CpG calls whose most likely state
  (5mC, 5hmC or canonical, canonical = 1 − Σ mods) has probability < 0.7;
- ``invalid_mm_records``: eligible records with MM whose ``MN`` mismatches or
  whose MM/ML do not parse; they contribute no calls (D28).

ML values are streamed from the aligned BAM (the read summary does not carry
them); a derived input applies its row predicate to each read name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..modbase import InvalidMM, cpg_calls, mm_codes, mm_tag
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric, ratio
from ._records import eligible_record, name_predicate

CODE_SAMPLE = 10_000
FILTER_THRESHOLD = 0.7


@dataclass
class ModbaseQc(Base):
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"code_sample_records": CODE_SAMPLE, "filter_threshold": FILTER_THRESHOLD}
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        if not np.any(eligible & (summary.column("has_mm") == 1)):
            return Skip("missing_tags", "no eligible read carries an MM tag (basecalled without modified bases?)")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        import pysam

        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        n_eligible = int(eligible.sum())
        n_tagged = int((eligible & (summary.column("has_mm") == 1)).sum())
        contigs = {summary.contigs[int(i)] for i in summary.canonical_ids()}
        keep = name_predicate(ctx.derived)
        threshold = float(self.parameters["filter_threshold"])
        codes: list[str] = []
        sampled = 0
        ml_sum = 0
        ml_values = 0
        calls = 0
        low = 0
        invalid = 0
        with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "rb") as bam:
            for record in bam.fetch(until_eof=True):
                if not eligible_record(record, contigs):
                    continue
                if keep is not None and not keep(record.query_name or ""):
                    continue
                mm = mm_tag(record)
                if mm is None:
                    continue
                if sampled < CODE_SAMPLE:
                    sampled += 1
                    for code in mm_codes(mm):
                        if code not in codes:
                            codes.append(code)
                try:
                    for call in cpg_calls(record):
                        calls += 1
                        ml_sum += sum(call.probabilities.values())
                        ml_values += len(call.probabilities)
                        if call.max_probability() < threshold:
                            low += 1
                except InvalidMM:
                    invalid += 1
        ml_mean = ml_sum / (255.0 * ml_values) if ml_values else None
        return AlgorithmOutput(
            metrics=[
                metric("mod_codes", sorted(codes), unit=None, n=sampled),
                metric("mm_tagged_fraction", ratio(n_tagged, n_eligible), unit="fraction", n=n_eligible),
                metric("ml_mean", ml_mean, unit="fraction", n=ml_values),
                metric("ml_below_filter_fraction", ratio(low, calls), unit="fraction", n=calls),
                metric("invalid_mm_records", invalid, unit="reads", n=n_tagged),
            ]
        )


MODBASE_QC = ModbaseQc("modbase-qc")
