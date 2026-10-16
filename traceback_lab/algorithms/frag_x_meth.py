"""Joint aligned-fragment span and per-molecule CpG 5mC fraction.

Only D21-eligible calls with a C+m channel enter this descriptive analysis.
The D28 store has one row per CpG site call, ordered by ``read_row``.  A
derived input supplies the same D33 row mask to calls and read summary.  Each
read with at least four distinct site calls contributes one panel observation,
regardless of how many CpGs it contains.

The panel is a 2-D count distribution.  The scalar is a separate descriptive
difference of per-read mean fractions (long minus short), not a classifier or
an interpretation of any individual panel cell.  No reference range is known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..meth_calls import DTYPE, HAS_M, argmax_state
from ..read_summary import ReadSummary
from ..registry import AlgorithmOutput, InputContext, Requirements
from ._base import metric
from ._meth import MethBase, load_calls

MIN_CPGS = 4
# Half-open bins, except the final methylation bin includes 1.0.
SIZE_EDGES_BP = (1, 100, 151, 221, 301, 501, 1001)
METH_EDGES = (0.0, 0.25, 0.5, 0.75, 1.0)
SHORT_BP = (100, 150)
LONG_BP = (151, 220)

_SITE_DTYPE = np.dtype(
    [("read_row", "<u4"), ("contig", "<i2"), ("ref_pos0", "<i4"), ("strand", "<i1")]
)


def _distinct_sites(rows: np.ndarray) -> np.ndarray:
    """Keep the first call at each (read, contig, CpG, strand) site.

    The producer normally emits each site once. Identical duplicates are
    collapsed, including across chunk boundaries. Conflicting probabilities
    or channel declarations fail as a malformed intermediate.
    """

    if rows.size < 2:
        return rows
    keys = np.empty(rows.size, dtype=_SITE_DTYPE)
    for name in _SITE_DTYPE.names:
        keys[name] = rows[name]
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    representative = first[inverse]
    for name in ("p_m", "p_h", "p_canonical", "channels"):
        if np.any(rows[name] != rows[name][representative]):
            raise ValueError("conflicting duplicate CpG site calls in meth-calls")
    return rows[np.sort(first)]


@dataclass
class FragXMeth(MethBase):
    # The 1M availability gate is the approved L2 catalog row, not a clinical cutoff.
    requires: Requirements = field(
        default_factory=lambda: Requirements(mm_tags=True, min_eligible_reads=1_000_000)
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "span": "aligned_reference_end_minus_start_bp",
            "channel": "C+m",
            "call": "argmax_5mc_over_canonical_5mc_5hmc",
            "min_distinct_cpg_sites": MIN_CPGS,
            "size_edges_bp": list(SIZE_EDGES_BP),
            "methylation_edges": list(METH_EDGES),
            "short_bp_inclusive": list(SHORT_BP),
            "long_bp_inclusive": list(LONG_BP),
            "delta": "mean_long_minus_mean_short",
            "duplicate_site": "identical_collapse_conflicts_rejected",
        }
    )

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        calls, mask = load_calls(ctx)
        # load_calls validated the upstream summary and built the derived mask.
        # Reopen just its metadata here; deriving a second whole-input mask
        # would needlessly copy an array proportional to total read count.
        summary = ReadSummary.open(
            Path(ctx.extras["upstream"]["read-summary"]) / "columns"
        )
        starts = summary.column("start")
        ends = summary.column("end")
        panel_counts = np.zeros(
            (len(SIZE_EDGES_BP), len(METH_EDGES) - 1), dtype=np.int64
        )
        n_short = n_long = 0
        sum_short = sum_long = 0.0
        n_panel = 0

        def add(rows: np.ndarray) -> None:
            nonlocal n_short, n_long, sum_short, sum_long, n_panel
            rows = _distinct_sites(rows)
            if rows.size == 0:
                return
            read_rows = rows["read_row"]
            if np.any(read_rows >= summary.rows):
                raise ValueError("meth-calls read_row lies outside read-summary")
            boundaries = np.flatnonzero(np.r_[True, read_rows[1:] != read_rows[:-1]])
            total = np.diff(np.r_[boundaries, rows.size])
            methylated = np.add.reduceat(
                (argmax_state(rows) == 1).astype(np.int64), boundaries
            )
            keep = total >= MIN_CPGS
            if not np.any(keep):
                return
            ids = read_rows[boundaries[keep]]
            span = ends[ids].astype(np.int64) - starts[ids].astype(np.int64)
            fraction = methylated[keep] / total[keep]
            positive = span > 0
            span = span[positive]
            fraction = fraction[positive]
            n_panel += int(span.size)
            size_bin = np.searchsorted(SIZE_EDGES_BP, span, side="right") - 1
            meth_bin = np.minimum(
                np.searchsorted(METH_EDGES, fraction, side="right") - 1,
                len(METH_EDGES) - 2,
            )
            np.add.at(panel_counts, (size_bin, meth_bin), 1)
            short = (span >= SHORT_BP[0]) & (span <= SHORT_BP[1])
            long_ = (span >= LONG_BP[0]) & (span <= LONG_BP[1])
            n_short += int(short.sum())
            n_long += int(long_.sum())
            sum_short += float(fraction[short].sum())
            sum_long += float(fraction[long_].sum())

        carry = np.empty(0, dtype=DTYPE)
        previous_row = -1
        for chunk in calls.chunks(mask):
            chunk = chunk[(chunk["channels"] & HAS_M) != 0]
            if chunk.size == 0:
                continue
            if int(chunk["read_row"][0]) < previous_row or np.any(
                chunk["read_row"][1:] < chunk["read_row"][:-1]
            ):
                raise ValueError(
                    "meth-calls read_row values are not in read-summary order"
                )
            previous_row = int(chunk["read_row"][-1])
            if carry.size:
                if carry["read_row"][-1] == chunk["read_row"][0]:
                    chunk = np.concatenate((carry, chunk))
                else:
                    add(carry)
                carry = carry[:0]
            last_start = int(
                np.searchsorted(chunk["read_row"], chunk["read_row"][-1], side="left")
            )
            add(chunk[:last_start])
            carry = np.array(chunk[last_start:], copy=True)
        add(carry)

        delta = sum_long / n_long - sum_short / n_short if n_short and n_long else None
        panel = {
            "size_edges_bp": list(SIZE_EDGES_BP),
            "size_final_upper_bp": None,
            "methylation_edges": list(METH_EDGES),
            "counts": panel_counts.tolist(),
            "n": n_panel,
            "short_n": n_short,
            "long_n": n_long,
            "short_mean": sum_short / n_short if n_short else None,
            "long_mean": sum_long / n_long if n_long else None,
            "unit": "molecules",
            "min_distinct_cpg_sites": MIN_CPGS,
        }
        return AlgorithmOutput(
            metrics=[
                metric(
                    "short_fragment_hypomethylation_delta",
                    delta,
                    unit="fraction",
                    n=n_short + n_long,
                )
            ],
            panels=[
                {
                    "panel_id": "size-methylation",
                    "kind": "table",
                    "data_ref": "artifacts/size_methylation.json",
                }
            ],
            artifacts={
                "size_methylation.json": json.dumps(
                    panel, separators=(",", ":")
                ).encode()
            },
        )


FRAG_X_METH = FragXMeth("frag-x-meth")
