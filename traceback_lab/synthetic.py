"""Seeded synthetic reference and unaligned MinKNOW-like chunks.

Used by the integration tests and by ``lab run --example``.  Nothing here is
real data: sequences are drawn from a seeded RNG, read names are
``synthetic-<n>``, and the ``@RG`` line names a synthetic run.

Each read is a fragment sampled from the reference (forward or reverse
strand), with a planted size mode near 167 bp, ``MM``/``ML`` 5mC calls on every
C (``C+m?``, so unlisted Cs are missing, not canonical), ``MN`` equal to the
sequence length, and the ``@RG`` tag of the synthetic run.
"""

from __future__ import annotations

import array
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pysam

EXAMPLE_CONTIGS: tuple[str, ...] = (
    *(f"chr{number}" for number in range(1, 23)),
    "chrX",
    "chrY",
    "chrM",
)
SYNTHETIC_MODEL = "dna_r10.4.1_e8.2_400bps_sup@v5.0.0"
SYNTHETIC_MODBASE = "dna_r10.4.1_e8.2_400bps_sup@v5.0.0_5mCG_5hmCG@v1"
_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")


def rg_line(run: str = "synthetic-run", model: str = SYNTHETIC_MODEL) -> dict[str, str]:
    return {
        "ID": f"{run}_{model}",
        "DS": f"runid={run} basecall_model={model} modbase_models={SYNTHETIC_MODBASE}",
        "PL": "ONT",
        "LB": "synthetic",
        "PU": "SYNTH0001",
    }


@dataclass(frozen=True)
class SyntheticReference:
    fasta: Path
    contigs: tuple[tuple[str, int], ...]
    sequences: dict[str, str]


def write_reference(
    path: Path,
    *,
    seed: int = 7,
    contigs: Sequence[str] = EXAMPLE_CONTIGS,
    length: int = 40_000,
    chrm_length: int = 16_569,
) -> SyntheticReference:
    """Write a seeded random FASTA (and its .fai) with GC near 0.41."""

    rng = random.Random(seed)
    sequences: dict[str, str] = {}
    with path.open("w", encoding="ascii") as stream:
        for name in contigs:
            size = chrm_length if name == "chrM" else length
            bases = "".join(rng.choices("ACGT", weights=(29.5, 20.5, 20.5, 29.5), k=size))
            sequences[name] = bases
            stream.write(f">{name}\n")
            for start in range(0, size, 60):
                stream.write(bases[start : start + 60] + "\n")
    pysam.faidx(str(path))
    return SyntheticReference(
        fasta=path,
        contigs=tuple((name, len(seq)) for name, seq in sequences.items()),
        sequences=sequences,
    )


def _revcomp(sequence: str) -> str:
    return sequence.translate(_COMPLEMENT)[::-1]


def _mm_ml(sequence: str, rng: random.Random, methylated: float) -> tuple[str, list[int]]:
    """``C+m?`` with an explicit call on every C (skip 0 each)."""

    positions = [index for index, base in enumerate(sequence) if base == "C"]
    if not positions:
        return "C+m?;", []
    probabilities = []
    for _ in positions:
        if rng.random() < methylated:
            probabilities.append(rng.randint(200, 255))
        else:
            probabilities.append(rng.randint(0, 40))
    return "C+m?," + ",".join("0" for _ in positions) + ";", probabilities


def write_chunks(
    directory: Path,
    reference: SyntheticReference,
    *,
    chunks: int = 3,
    reads_per_chunk: int = 2_000,
    seed: int = 11,
    mode_bp: int = 167,
    methylated: float = 0.7,
    stem: str = "SYNTH0001_pass_synthetic",
    rg: dict[str, str] | None = None,
    chry_weight: float = 1.0,
    with_tags: bool = True,
) -> list[Path]:
    """Write ``chunks`` unaligned BAMs ``<stem>_<n>.bam`` (no @SQ)."""

    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    group = rg or rg_line()
    header = {
        "HD": {"VN": "1.6", "SO": "unknown"},
        "RG": [group],
        "PG": [{"ID": "basecaller", "PN": "synthetic", "VN": "0"}],
    }
    names = [name for name, _ in reference.contigs]
    weights = [
        length * (chry_weight if name == "chrY" else 1.0) for name, length in reference.contigs
    ]
    paths: list[Path] = []
    serial = 0
    for chunk in range(chunks):
        path = directory / f"{stem}_{chunk}.bam"
        with pysam.AlignmentFile(str(path), "wb", header=header) as out:
            for _ in range(reads_per_chunk):
                contig = rng.choices(names, weights=weights, k=1)[0]
                sequence = reference.sequences[contig]
                size = max(60, min(len(sequence) - 1, int(rng.gauss(mode_bp, 18))))
                start = rng.randrange(0, len(sequence) - size)
                fragment = sequence[start : start + size]
                if rng.random() < 0.5:
                    fragment = _revcomp(fragment)
                record = pysam.AlignedSegment(out.header)
                record.query_name = f"synthetic-{seed}-{serial}"
                serial += 1
                record.query_sequence = fragment
                record.flag = 4
                record.query_qualities = array.array("B", [20] * len(fragment))
                if with_tags:
                    mm, ml = _mm_ml(fragment, rng, methylated)
                    record.set_tag("MM", mm, value_type="Z")
                    record.set_tag("ML", array.array("B", ml))
                    record.set_tag("MN", len(fragment), value_type="i")
                record.set_tag("RG", group["ID"], value_type="Z")
                out.write(record)
        paths.append(path)
    return paths


__all__ = [
    "EXAMPLE_CONTIGS",
    "SYNTHETIC_MODEL",
    "SyntheticReference",
    "rg_line",
    "write_chunks",
    "write_reference",
]
