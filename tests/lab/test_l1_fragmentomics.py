"""L1.2: end-motifs and delfi-ratio on planted synthetic BAMs and a seeded reference."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pysam
import pytest

from traceback_lab.algorithms.delfi_ratio import BIN, DELFI_RATIO
from traceback_lab.algorithms.end_motifs import END_MOTIFS, MOTIFS, kmer_codes, motif_diversity, revcomp_codes
from traceback_lab.registry import Ready, Skip

from .bam_fixtures import Read, context_for, values

_COMP = str.maketrans("ACGTN", "TGCAN")


def _revcomp(text: str) -> str:
    return text.translate(_COMP)[::-1]


def _fasta(path: Path, sequences: dict[str, str]) -> Path:
    with path.open("w") as stream:
        for name, sequence in sequences.items():
            stream.write(f">{name}\n{sequence}\n")
    pysam.faidx(str(path))
    return path


# ------------------------------------------------------------------ end-motifs
def test_kmer_and_revcomp_codes_cover_every_motif() -> None:
    encoded = np.frombuffer("".join(MOTIFS).encode(), dtype=np.uint8)
    from traceback_lab.algorithms.end_motifs import _CODE

    codes = kmer_codes(_CODE[encoded], np.arange(0, len(MOTIFS) * 4, 4))
    assert codes.tolist() == list(range(256))
    rc = revcomp_codes(codes)
    assert [MOTIFS[c] for c in rc] == [_revcomp(m) for m in MOTIFS]
    assert kmer_codes(_CODE[np.frombuffer(b"ACNTACGT", dtype=np.uint8)], np.array([0, 4, 6])).tolist() == [-1, 27, -1]


def test_end_motifs_read_both_fragment_ends_from_the_reference(tmp_path: Path) -> None:
    rng = random.Random(3)
    sequence = list("".join(rng.choices("ACGT", k=3000)))
    sequence[500:504] = list("CCCA")
    sequence[1200:1204] = list("NNNN")
    chr1 = "".join(sequence)
    fasta = _fasta(tmp_path / "ref.fa", {"chr1": chr1})
    reads = [
        Read("f", "chr1", 100, "A" * 170),
        Read("r", "chr1", 300, "A" * 150, flag=16),
        Read("c", "chr1", 500, "A" * 160),
        Read("n", "chr1", 1200, "A" * 170),  # 5' motif holds N: only its other end counts
        Read("low", "chr1", 2000, "A" * 170, mapq=3),
    ]
    ctx = context_for(tmp_path, [("chr1", 3000)], reads, fasta=fasta, eligible_reads=600_000)
    assert isinstance(END_MOTIFS.preflight(ctx), Ready)
    out = END_MOTIFS.run(ctx, tmp_path / "o")
    expected: dict[str, int] = {}
    for start, span in ((100, 170), (300, 150), (500, 160), (1200, 170)):
        for motif in (chr1[start : start + 4], _revcomp(chr1[start + span - 4 : start + span])):
            if "N" not in motif:
                expected[motif] = expected.get(motif, 0) + 1
    import json

    panel = json.loads(out.artifacts["motif_freq.json"])
    got = {motif: count for motif, count in zip(panel["motif"], panel["count"]) if count}
    assert got == expected
    counts = np.array(panel["count"])
    got_values = values(out)
    assert got_values["mds"] == pytest.approx(motif_diversity(counts))
    assert got_values["ccca_fraction"] == pytest.approx(expected.get("CCCA", 0) / sum(expected.values()))


def test_motif_diversity_bounds() -> None:
    assert motif_diversity(np.ones(256)) == pytest.approx(1.0)
    one = np.zeros(256)
    one[5] = 10
    assert motif_diversity(one) == pytest.approx(0.0)
    assert motif_diversity(np.zeros(256)) is None


def test_end_motifs_skip_paths(tmp_path: Path) -> None:
    fasta = _fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 250})
    shallow = context_for(tmp_path / "a", [("chr1", 1000)], [Read("a", "chr1", 0, "A" * 100)], fasta=fasta, eligible_reads=10)
    decision = END_MOTIFS.preflight(shallow)
    assert isinstance(decision, Skip) and decision.reason_code == "depth"
    no_fasta = context_for(tmp_path / "b", [("chr1", 1000)], [Read("a", "chr1", 0, "A" * 100)], eligible_reads=600_000)
    decision = END_MOTIFS.preflight(no_fasta)
    assert isinstance(decision, Skip) and decision.reason_code == "reference"


# ----------------------------------------------------------------- delfi-ratio
def _delfi_reads() -> list[Read]:
    plan = {
        ("chr1", 0): (4, 2),
        ("chr1", 1): (3, 3),
        ("chr1", 2): (5, 0),
        ("chr2", 0): (2, 1),
        ("chr2", 1): (1, 2),
    }
    reads: list[Read] = []
    serial = 0
    for (contig, b), (short, long_) in plan.items():
        for span, count in ((125, short), (180, long_), (90, 1), (300, 1)):
            for _ in range(count):
                reads.append(Read(f"d{serial}", contig, b * BIN + 1000 * serial, "A" * span))
                serial += 1
    reads.append(Read("x", "chrX", 1000, "A" * 125))
    return reads


def test_delfi_ratio_track_and_sd(tmp_path: Path) -> None:
    contigs = [("chr1", 12_000_000), ("chr2", 7_000_000), ("chrX", 6_000_000)]
    ctx = context_for(tmp_path, contigs, _delfi_reads(), eligible_reads=2_000_000)
    assert isinstance(DELFI_RATIO.preflight(ctx), Ready)
    out = DELFI_RATIO.run(ctx, tmp_path / "o")
    import json

    track = json.loads(out.artifacts["ratio_track.json"])["bins"]
    assert [(row["contig"], row["start"], row["short"], row["long"]) for row in track] == [
        ("chr1", 0, 4, 2), ("chr1", BIN, 3, 3), ("chr1", 2 * BIN, 5, 0), ("chr2", 0, 2, 1), ("chr2", BIN, 1, 2)
    ]
    assert track[2]["ratio"] is None and track[2]["centred"] is None
    centred = [row["centred"] for row in track if row["centred"] is not None]
    assert centred == pytest.approx([0.5, -0.5, 0.5, -1.0])
    assert values(out)["ratio_sd"] == pytest.approx(float(np.std(centred, ddof=1)))


def test_delfi_ratio_needs_a_million_eligible_reads(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, [("chr1", 6_000_000)], [Read("a", "chr1", 0, "A" * 125)], eligible_reads=999_999)
    decision = DELFI_RATIO.preflight(ctx)
    assert isinstance(decision, Skip) and decision.reason_code == "depth"


def test_every_l1_2_metric_has_a_citation_checked_library_entry() -> None:
    from traceback_lab.interpretation.engine import load_library

    library = load_library()
    for algorithm_id, metric_ids in {"end-motifs": ["mds", "ccca_fraction"], "delfi-ratio": ["ratio_sd"]}.items():
        assert library.headline(algorithm_id) is not None
        for metric_id in metric_ids:
            entry = library.entry(algorithm_id, metric_id)
            assert entry is not None and entry.citation_verified
            assert "LEGAL" in entry["flags"] and "method-patents" in entry["license_ids"]
