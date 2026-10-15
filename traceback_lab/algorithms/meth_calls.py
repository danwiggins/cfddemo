"""``meth-calls``: the D28 site-level methylation intermediate (hidden in the UI).

Decodes MM/ML once per aligned input into ``artifacts/calls/`` (see
``traceback_lab.meth_calls``).  Its metrics are counts for Trust/QC; the
consumers (global-5mc, global-5hmc, pmd-score, hypometh-reads) read the store
through D19 ``upstream``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..meth_calls import CHUNK_CALLS, write_meth_calls
from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric
from ._records import eligible_record


@dataclass
class MethCallsIntermediate(Base):
    family: str = "methylation"
    consumes: str = "full_bam"
    requires: Requirements = field(default_factory=lambda: Requirements(mm_tags=True))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"chunk_calls": CHUNK_CALLS, "layout": "site-row-v1", "context": "CpG (read sequence)"}
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        if not np.any(eligible & (summary.column("has_mm") == 1)):
            return Skip("missing_tags", "no eligible read carries an MM tag")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        summary, _ = load_summary(ctx)
        names = {summary.contigs[int(i)] for i in summary.canonical_ids()}
        meta = write_meth_calls(
            ctx.input_dir / "aligned.bam",
            out / "calls",
            eligible=lambda record: eligible_record(record, names),
            contig_index={name: index for index, name in enumerate(summary.contigs)},
        )
        return AlgorithmOutput(
            metrics=[
                metric("calls", meta["calls"], unit="calls"),
                metric("records_with_mm", meta["records_with_mm"], unit="reads", n=meta["records_decoded"]),
                metric("invalid_mm", meta["invalid_mm"], unit="reads", n=meta["records_with_mm"]),
                metric("dropped_unaligned", meta["dropped_unaligned"], unit="calls"),
            ]
        )


METH_CALLS = MethCallsIntermediate("meth-calls")
