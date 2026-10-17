"""Appendix A QC formulas against small synthetic, hand-calculated cases."""

from pathlib import Path

import pysam
import pytest

from traceback_lab.algorithms.bam_qc import BAM_QC, gc_bias_slope
from traceback_lab.read_summary import ReadSummary, write_summary
from traceback_lab.registry import InputContext


def _bam(path: Path, rows: list[tuple[int, int, int, int, int | None]], length: int) -> ReadSummary:
    """Rows are (start, length, flag, MAPQ, uniform Phred quality)."""
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chr1", "LN": length}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for index, (start, span, flag, mapq, quality) in enumerate(sorted(rows, key=lambda row: row[0])):
            record = pysam.AlignedSegment(bam.header)
            record.query_name = f"synthetic-qc-{index}"
            record.query_sequence = "A" * span
            record.flag = flag
            record.reference_id = 0
            record.reference_start = start
            record.mapping_quality = mapq
            record.cigarstring = f"{span}M"
            record.query_qualities = None if quality is None else [quality] * span
            bam.write(record)
    pysam.index(str(path))
    write_summary(path, path.parent / "columns")
    return ReadSummary.open(path.parent / "columns")


def test_qc_primary_denominators_and_per_read_quality(tmp_path: Path) -> None:
    # Four primary records, three mapped, two mapped at MAPQ >=20.
    # Qualities average over reads (10,30,20), not their unequal lengths.
    _bam(tmp_path / "aligned.bam", [
        (0, 10, 0, 60, 10), (20, 100, 0, 20, 30),
        (140, 20, 0, 19, None), (170, 10, 0x100, 60, 50),
        (190, 10, 0x800, 60, 40), (220, 30, 0x4, 0, 20),
    ], 1000)
    context = InputContext(
        root=tmp_path, source_id="a" * 64, input_id="b" * 64,
        reference_id="toy", reference_sha256="c" * 64, input_dir=tmp_path,
        extras={"upstream": {"read-summary": str(tmp_path)}},
    )
    metrics = {row["metric_id"]: row for row in BAM_QC.run(context, tmp_path).metrics}
    expected = {
        "total_reads": (4, 4), "mapped_fraction": (3 / 4, 4),
        "mapq_ge20_fraction": (2 / 3, 3), "supplementary_fraction": (1 / 3, 3),
        "mean_read_q": (20, 3), "genome_ge1x_fraction": (110 / 1000, 1000),
    }
    for name, (value, denominator) in expected.items():
        assert metrics[name]["value"] == pytest.approx(value), name
        assert metrics[name]["n"] == denominator, name
    assert metrics["duplicate_fraction"]["value"] is None  # unmarked is not a measured zero
    assert metrics["gc_bias_slope"]["value"] is None  # no registered FASTA


def test_gc_slope_uses_eligible_positive_bins_and_acgt_fraction(tmp_path: Path) -> None:
    # GC = .3,.5,.7 with counts = 1,2,4: log2(count/median)=-1,0,1;
    # therefore the hand-calculated OLS slope is 5 log2/GC.
    # The remaining bins are out-of-range GC, mostly N, and zero coverage.
    sequences = [
        "G" * 300_000 + "A" * 700_000,
        "C" * 500_000 + "T" * 500_000,
        "G" * 700_000 + "A" * 300_000,
        "G" * 200_000 + "A" * 800_000,
        "G" * 200_000 + "A" * 200_000 + "N" * 600_000,
        "G" * 400_000 + "A" * 600_000,
    ]
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\n" + "\n".join(sequences) + "\n", encoding="ascii")
    pysam.faidx(str(fasta))
    rows = [(window * 1_000_000 + index * 20, 10, 0, 60, 30)
            for window, count in enumerate([1, 2, 4, 16, 16, 0]) for index in range(count)]
    rows.extend([(500, 10, 0x400, 60, 30), (600, 10, 0, 19, 30)])
    summary = _bam(tmp_path / "aligned.bam", rows, 6_000_000)
    slope, bins = gc_bias_slope(summary, summary.eligible(), str(fasta))
    assert bins == 3
    assert slope == pytest.approx(5.0)

