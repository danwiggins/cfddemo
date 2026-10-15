"""``sex-check``: chrY/chrX read-density ratio and a sex call (Appendix A).

``chry_chrx_ratio`` = (chrY reads per Mb of chrY non-PAR mappable length) ÷
(chrX reads per Mb of chrX mappable length), over D21-eligible molecules.
chrY reads starting inside a pseudoautosomal region are not counted.

"Mappable length" (Appendix A leaves it open; stated here and in the spec's
audit trail): the non-N bases of the contig in the registered reference FASTA,
minus the PARs for chrY.  Without the FASTA the contig length is used (minus
the PARs for chrY).  The hg38 PARs apply only when chrY has the hg38 length.

``call``: ``male`` if the ratio > 0.3, ``female`` if < 0.05, else
``indeterminate``; null when no chrX molecule is counted.  Zero chrY reads is
a valid ``female`` call, not an error.  A reference without chrX or chrY
skips with reason ``reference``.  FLAG: PRIVACY (library entry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric

HG38_CHRY_LENGTH = 57_227_415
# hg38 chrY pseudoautosomal regions, 0-based half-open.
HG38_CHRY_PARS = ((10_000, 2_781_479), (56_887_902, 57_217_415))
MALE_ABOVE = 0.3
FEMALE_BELOW = 0.05


def call_sex(ratio_value: float | None) -> str | None:
    if ratio_value is None:
        return None
    if ratio_value > MALE_ABOVE:
        return "male"
    if ratio_value < FEMALE_BELOW:
        return "female"
    return "indeterminate"


def _non_n_bases(fasta: str | None, contig: str, length: int, excluded: tuple[tuple[int, int], ...]) -> int:
    """Non-N bases on ``contig`` outside ``excluded`` intervals."""

    if not fasta:
        return length - sum(end - start for start, end in excluded)
    import pysam

    total = 0
    with pysam.FastaFile(fasta) as reference:
        step = 1_000_000
        for start in range(0, length, step):
            end = min(length, start + step)
            sequence = reference.fetch(contig, start, end).upper()
            keep = np.frombuffer(sequence.encode("ascii"), dtype=np.uint8) != ord("N")
            for low, high in excluded:
                a, b = max(low, start), min(high, end)
                if a < b:
                    keep[a - start : b - start] = False
            total += int(keep.sum())
    return total


@dataclass
class SexCheck(Base):
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=500_000))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "male_above": MALE_ABOVE,
            "female_below": FEMALE_BELOW,
            "chry_pars_hg38": [list(par) for par in HG38_CHRY_PARS],
            "mappable": "non-N reference bases",
        }
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        summary, _ = load_summary(ctx)
        if "chrX" not in summary.contigs or "chrY" not in summary.contigs:
            return Skip("reference", "the reference has no chrX or no chrY contig")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, mask = load_summary(ctx)
        x_id, y_id = summary.contigs.index("chrX"), summary.contigs.index("chrY")
        x_len, y_len = summary.lengths[x_id], summary.lengths[y_id]
        pars = HG38_CHRY_PARS if y_len == HG38_CHRY_LENGTH else ()
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        contig = summary.column("contig")
        x_reads = int((eligible & (contig == x_id)).sum())
        y_rows = eligible & (contig == y_id)
        starts = summary.column("start")[y_rows]
        in_par = np.zeros(starts.size, dtype=bool)
        for low, high in pars:
            in_par |= (starts >= low) & (starts < high)
        y_reads = int((~in_par).sum())
        fasta = ctx.extras.get("reference_fasta")
        x_mb = _non_n_bases(fasta, "chrX", x_len, ()) / 1e6
        y_mb = _non_n_bases(fasta, "chrY", y_len, pars) / 1e6
        ratio_value = (y_reads / y_mb) / (x_reads / x_mb) if x_reads and x_mb > 0 and y_mb > 0 else None
        return AlgorithmOutput(
            metrics=[
                metric("chry_chrx_ratio", ratio_value, unit="ratio", n=x_reads + y_reads),
                metric("call", call_sex(ratio_value), unit=None, n=x_reads + y_reads),
            ]
        )


SEX_CHECK = SexCheck("sex-check")
