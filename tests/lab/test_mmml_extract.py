"""Lab MM/ML extraction semantics on real synthetic pysam BAM records."""

from __future__ import annotations

import array
from pathlib import Path

import pysam
import pytest

from traceback_lab.adapters.mmml_extract import extract_cpg_calls


def _write_input(tmp_path: Path) -> tuple[Path, Path]:
    sequence = "A" * 100 + "ACGTACGTAA" + "A" * 90 + "TTCGAACGTT" + "A" * 100
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\n" + sequence + "\n")
    pysam.faidx(str(fasta))
    bam = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": len(sequence)}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as writer:
        def add(name: str, start: int, bases: str, *, mm: str | None = None,
                ml: list[int] | None = None, mn: int | None = None, reverse: bool = False) -> None:
            read = pysam.AlignedSegment(writer.header)
            read.query_name = name
            read.query_sequence = bases
            read.flag = 16 if reverse else 0
            read.reference_id = 0
            read.reference_start = start
            read.mapping_quality = 60
            read.cigarstring = f"{len(bases)}M"
            if mm is not None:
                read.set_tag("MM", mm)
            if ml is not None:
                read.set_tag("ML", array.array("B", ml))
            if mn is not None:
                read.set_tag("MN", mn)
            writer.write(read)

        add("dot", 100, "ACGTACGTAA", mm="C+m.,1;", ml=[250], mn=10)
        add("question", 100, "ACGTACGTAA", mm="C+m?,1;", ml=[250], mn=10)
        add("hydroxymethyl", 100, "ACGTACGTAA", mm="C+h?,0;", ml=[250], mn=10)
        add("low-confidence", 100, "ACGTACGTAA", mm="C+m?,0;", ml=[100], mn=10)
        add("missing-ml", 100, "ACGTACGTAA", mm="C+m?,0;", mn=10)
        add("bad-mn", 100, "ACGTACGTAA", mm="C+m?,0;", ml=[250], mn=11)
        add("no-mm", 100, "ACGTACGTAA")
        add("reverse", 200, "TTCGAACGTT", mm="C+m?,0;", ml=[250], mn=10, reverse=True)
    return bam, fasta


def test_skip_modes_reverse_mapping_and_unknown_calls(tmp_path: Path) -> None:
    bam, fasta = _write_input(tmp_path)
    result = extract_cpg_calls(bam, fasta, maximum_calls=100, filter_threshold=0.9)
    got = sorted((call.position0, call.strand.value, call.state.value) for call in result.calls)
    assert got == [
        (101, "+", "methylated"),  # 5hmC contributes to the UXM M state
        (101, "+", "unmethylated"),  # dot-mode implicit canonical, never from ?
        (105, "+", "methylated"),
        (105, "+", "methylated"),
        (206, "-", "methylated"),
    ]
    assert result.records_scanned == 8
    assert result.records_with_mm == 7
    assert result.invalid_mm == 2  # absent ML and mismatched MN never become canonical
    assert result.low_confidence == 1
    assert result.dropped_unaligned == result.non_reference_cpg == 0
    assert len({call.fragment_digest for call in result.calls}) == 4


def test_extraction_cap_refuses_before_unbounded_growth(tmp_path: Path) -> None:
    bam, fasta = _write_input(tmp_path)
    with pytest.raises(ValueError, match="locked cap"):
        extract_cpg_calls(bam, fasta, maximum_calls=1, filter_threshold=0.9)
