"""``end-motifs``: 4-mer reference end motifs at both fragment ends (Appendix A).

For each D21-eligible molecule: the reference 4-mer at the 5′ end of the
forward-strand fragment (``ref[start:start+4]``) and the reverse complement of
the 4-mer at the other end (``revcomp(ref[end-4:end])``), so two motifs per
molecule whatever the read's strand.  A motif containing a non-ACGT base, or
falling off the contig, is not counted.

- ``mds`` (motif diversity score) = −Σ f·ln f / ln 256 over the 256 motif
  frequencies (0 ln 0 = 0);
- ``ccca_fraction`` = f(CCCA).

The panel is the 256-motif frequency vector.  Reference bases come from the
registered FASTA, one contig at a time, vectorized over the read summary, so
no read is re-read from the BAM.  ONT read-end basecalling errors do not
enter: the motif is read from the reference at the aligned ends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np

from ..registry import AlgorithmOutput, InputContext, Ready, Requirements, Skip
from ._base import Base, load_summary, metric

K = 4
MOTIFS: tuple[str, ...] = tuple("".join(p) for p in product("ACGT", repeat=K))
_CODE = np.full(256, 255, dtype=np.uint8)
for _index, _base in enumerate(b"ACGT"):
    _CODE[_base] = _index
    _CODE[ord(chr(_base).lower())] = _index


def _encode(sequence: str) -> np.ndarray:
    return _CODE[np.frombuffer(sequence.encode("ascii"), dtype=np.uint8)]


def kmer_codes(encoded: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """4-mer code (0..255, A=0 C=1 G=2 T=3, first base most significant) at
    each start, or -1 when it runs off the contig or holds a non-ACGT base."""

    out = np.full(starts.size, -1, dtype=np.int64)
    ok = (starts >= 0) & (starts + K <= encoded.size)
    idx = starts[ok]
    code = np.zeros(idx.size, dtype=np.int64)
    bad = np.zeros(idx.size, dtype=bool)
    for offset in range(K):
        base = encoded[idx + offset]
        bad |= base == 255
        code = code * 4 + np.where(base == 255, 0, base)
    code[bad] = -1
    out[ok] = code
    return out


def revcomp_codes(codes: np.ndarray) -> np.ndarray:
    """Code of the reverse complement of each 4-mer code (-1 stays -1)."""

    out = np.zeros_like(codes)
    rest = codes.copy()
    for _ in range(K):
        out = out * 4 + (3 - rest % 4)
        rest //= 4
    out[codes < 0] = -1
    return out


def motif_diversity(counts: np.ndarray) -> float | None:
    total = counts.sum()
    if total <= 0:
        return None
    f = counts[counts > 0] / total
    return float(-(f * np.log(f)).sum() / np.log(len(MOTIFS)))


@dataclass
class EndMotifs(Base):
    family: str = "fragmentomics"
    requires: Requirements = field(default_factory=lambda: Requirements(min_eligible_reads=500_000))
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"k": K, "ends": "5prime_forward_and_revcomp_3prime", "source": "reference"}
    )

    def preflight(self, ctx: InputContext) -> Ready | Skip:
        decision = super().preflight(ctx)
        if isinstance(decision, Skip):
            return decision
        if not ctx.extras.get("reference_fasta"):
            return Skip("reference", "the registered reference FASTA is not available")
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        import pysam

        summary, mask = load_summary(ctx)
        eligible = summary.eligible()
        if mask is not None:
            eligible &= mask
        contig = summary.column("contig")
        starts = summary.column("start")
        ends = summary.column("end")
        counts = np.zeros(len(MOTIFS), dtype=np.int64)
        with pysam.FastaFile(str(ctx.extras["reference_fasta"])) as reference:
            for cid in np.unique(contig[eligible]):
                rows = eligible & (contig == cid)
                name = summary.contigs[int(cid)]
                encoded = _encode(reference.fetch(name))
                left = kmer_codes(encoded, starts[rows].astype(np.int64))
                right = revcomp_codes(kmer_codes(encoded, ends[rows].astype(np.int64) - K))
                both = np.concatenate([left, right])
                counts += np.bincount(both[both >= 0], minlength=len(MOTIFS))
        total = int(counts.sum())
        freq = counts / total if total else np.zeros(len(MOTIFS))
        panel = {"motif": list(MOTIFS), "count": [int(v) for v in counts], "freq": [float(v) for v in freq]}
        return AlgorithmOutput(
            metrics=[
                metric("mds", motif_diversity(counts), unit="index", n=total),
                metric("ccca_fraction", float(freq[MOTIFS.index("CCCA")]) if total else None, unit="fraction", n=total),
            ],
            panels=[{"panel_id": "motif-freq", "kind": "density", "data_ref": "artifacts/motif_freq.json"}],
            artifacts={"motif_freq.json": json.dumps(panel, separators=(",", ":")).encode()},
        )


END_MOTIFS = EndMotifs("end-motifs")
