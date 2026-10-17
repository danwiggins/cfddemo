"""Lab MM/ML extraction semantics on real synthetic pysam BAM records."""

from __future__ import annotations

import array
import csv
import hashlib
import os
import subprocess
from pathlib import Path

import pysam
import pytest

from evidence_inspector.cell_origin_prefilter import read_bed_regions
from traceback_lab.adapters.mmml_extract import extract_cpg_calls
from traceback_lab.adapters import mmml_extract


def _write_input(tmp_path: Path) -> tuple[Path, Path]:
    sequence = "A" * 100 + "ACGTACGTAA" + "A" * 90 + "TTCGAACGTT" + "A" * 100
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\n" + sequence + "\n")
    pysam.faidx(str(fasta))
    bam = tmp_path / "aligned.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": len(sequence)}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as writer:
        def add(name: str, start: int, bases: str, *, mm: str | None = None,
                ml: list[int] | None = None, mn: int | None = None, reverse: bool = False,
                cigar: str | None = None) -> None:
            read = pysam.AlignedSegment(writer.header)
            read.query_name = name
            read.query_sequence = bases
            read.flag = 16 if reverse else 0
            read.reference_id = 0
            read.reference_start = start
            read.mapping_quality = 60
            read.cigarstring = cigar or f"{len(bases)}M"
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
        add("two-groups", 100, "ACGTACGTAA", mm="C+h?,0;C+m?,1;", ml=[250, 250], mn=10)
        add("competing", 100, "ACGTACGTAA", mm="C+h?,0;C+m?,0;", ml=[240, 10], mn=10)
        add("unsupported", 100, "ACGTACGTAA", mm="C+a?,0;C+m?,0;", ml=[240, 5], mn=10)
        add("query-mismatch", 100, "ACATACGTAA", mm="C+m?,0;", ml=[250], mn=10)
        add("low-confidence", 100, "ACGTACGTAA", mm="C+m?,0;", ml=[100], mn=10)
        add("missing-ml", 100, "ACGTACGTAA", mm="C+m?,0;", mn=10)
        add("bad-mn", 100, "ACGTACGTAA", mm="C+m?,0;", ml=[250], mn=11)
        add("no-mm", 100, "ACGTACGTAA")
        add("reverse", 200, "TTCGAACGTT", mm="C+m?,0;", ml=[250], mn=10, reverse=True)
        add("outside-marker", 200, "TTCGAACGTT", mm="C+m?,0;", ml=[250], mn=10)
        add("insertion", 100, "AACGAAAAAA", mm="C+m?,0;", ml=[250], cigar="2M2I6M")
        add("soft-clip", 100, "CGAAAAAAAA", mm="C+m?,0;", ml=[250], cigar="2S8M")
        add("non-reference-cg", 110, "ACGAAAAAAA", mm="C+m?,0;", ml=[250])
    return bam, fasta


def test_skip_modes_reverse_mapping_and_unknown_calls(tmp_path: Path) -> None:
    bam, fasta = _write_input(tmp_path)
    result = extract_cpg_calls(bam, fasta, maximum_calls=100, filter_threshold=0.9)
    got = sorted((call.position0, call.strand.value, call.state.value) for call in result.calls)
    assert got == [
        (101, "+", "methylated"),  # 5hmC contributes to the UXM M state
        (101, "+", "methylated"),
        (101, "+", "methylated"),  # query CA still aligns over reference CG
        (101, "+", "methylated"),
        (101, "+", "unmethylated"),  # dot-mode implicit canonical, never from ?
        (105, "+", "methylated"),
        (105, "+", "methylated"),
        (105, "+", "methylated"),
        (202, "+", "methylated"),
        (206, "-", "methylated"),
    ]
    assert result.records_scanned == 16
    assert result.records_with_mm == 15
    assert result.invalid_mm == 2  # absent ML and mismatched MN never become canonical
    assert result.low_confidence == 1
    assert result.dropped_unaligned == 2
    assert result.non_reference_cpg == 1
    assert result.unsupported_modification == 1
    assert len({call.fragment_digest for call in result.calls}) == 8


def test_c_minus_dot_sites_follow_pysam_query_coordinates_on_both_orientations() -> None:
    header = pysam.AlignmentHeader.from_dict({"SQ": [{"SN": "chr1", "LN": 1000}]})
    for reverse, expected in (
        (False, [(100, 1, 250), (104, 1, 0)]),
        (True, [(102, 0, 0), (106, 0, 250)]),
    ):
        read = pysam.AlignedSegment(header)
        read.query_name = "c-minus"
        read.query_sequence = "ACGTACGTAA"
        read.flag = 16 if reverse else 0
        read.reference_id = 0
        read.reference_start = 100
        read.cigarstring = "10M"
        read.set_tag("MM", "C-m.,0;")
        read.set_tag("ML", array.array("B", [250]))
        explicit = read.modified_bases[("C", 0 if reverse else 1, "m")]
        assert explicit == [(6 if reverse else 1, 250)]
        sites, dropped = mmml_extract._reference_sites(read)
        assert dropped == 0
        assert [(position, strand, channels["m"]) for position, strand, channels in sites] == expected


def test_extraction_cap_refuses_before_unbounded_growth(tmp_path: Path) -> None:
    bam, fasta = _write_input(tmp_path)
    with pytest.raises(ValueError, match="locked cap"):
        extract_cpg_calls(bam, fasta, maximum_calls=1, filter_threshold=0.9)


def _write_boundary_input(tmp_path: Path) -> tuple[Path, Path]:
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\n" + "A" * 100 + "ACGTACGTAA" + "A" * 100 + "\n")
    pysam.faidx(str(fasta))
    bam = tmp_path / "boundary.bam"
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 210}]}
    with pysam.AlignmentFile(str(bam), "wb", header=header) as writer:
        for name, mm, ml in (
            ("ml22", "C+m?,0;", [22]),
            ("ml23", "C+m?,0;", [23]),
            ("ml204", "C+m?,0;", [204]),
            ("ml205", "C+m?,0;", [205]),
            ("ml232", "C+m?,0;", [232]),
            ("ml233", "C+m?,0;", [233]),
            ("multi-canonical-edge", "C+h?,0;C+m?,0;", [10, 12]),
            ("three-canonical-edge", "C+h?,0;C+m?,0;C+a?,0;", [5, 5, 12]),
            ("three-overmass-valid", "C+h?,0;C+m?,0;C+a?,0;", [85, 85, 85]),
            ("three-raw-over", "C+h?,0;C+m?,0;C+a?,0;", [86, 86, 86]),
            ("two-mod-tie", "C+h?,0;C+m?,0;", [120, 120]),
            ("unsupported-tie", "C+a?,0;C+m?,0;", [120, 120]),
            ("unsupported-last-tie", "C+m?,0;C+a?,0;", [120, 120]),
            ("overlap", "C+h?,0;C+m?,0;", [240, 240]),
        ):
            read = pysam.AlignedSegment(writer.header)
            read.query_name = name
            read.query_sequence = "ACGTACGTAA"
            read.reference_id = 0
            read.reference_start = 100
            read.mapping_quality = 60
            read.cigarstring = "10M"
            read.set_tag("MM", mm)
            read.set_tag("ML", array.array("B", ml))
            writer.write(read)
    return bam, fasta


def test_modkit_probability_bins_and_invalid_overlap(tmp_path: Path) -> None:
    bam, fasta = _write_boundary_input(tmp_path)
    at_eighty = extract_cpg_calls(bam, fasta, maximum_calls=20, filter_threshold=0.8)
    at_locked = extract_cpg_calls(bam, fasta, maximum_calls=20, filter_threshold=0.912109375)
    assert len(at_eighty.calls) == 7  # ML 204 is below 0.8 after bin decoding.
    assert len(at_locked.calls) == 2  # Two-channel canonical 10+12 falls below the locked edge.
    assert at_eighty.invalid_mm == at_locked.invalid_mm == 2  # Raw channel sum > 255.
    assert at_eighty.calls[2].modified_probability == (205 + 0.5) / 256
    assert {call.state.value for call in at_locked.calls} == {"unmethylated", "methylated"}
    assert all(call.modified_probability == 0.912109375 for call in at_locked.calls)


def test_pinned_modkit_probability_boundary_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = os.environ.get("LAB_MODKIT_PARITY_BINARY")
    if not binary:
        pytest.skip("set LAB_MODKIT_PARITY_BINARY to the pinned modkit 0.6.4 binary")
    bam, fasta = _write_boundary_input(tmp_path)
    monkeypatch.setattr(mmml_extract.os, "urandom", lambda size: b"\0" * size)
    names = ("ml22", "ml23", "ml204", "ml205", "ml232", "ml233",
             "multi-canonical-edge", "three-canonical-edge", "three-overmass-valid",
             "three-raw-over", "two-mod-tie", "unsupported-tie",
             "unsupported-last-tie", "overlap")
    digest_to_name = {
        hashlib.sha256(b"\0" * 32 + b"\0" + name.encode()).hexdigest(): name
        for name in names
    }
    for threshold in (0.3, 0.8, 0.912109375):
        output = tmp_path / f"boundary-{threshold}.tsv"
        subprocess.run(
            [binary, "extract", "calls", "--reference", str(fasta), "--cpg",
             "--filter-threshold", str(threshold), "--suppress-progress",
             str(bam), str(output)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        with output.open(newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        if threshold == 0.3:
            assert {(row["read_id"], row["call_code"]) for row in rows
                    if row["fail"] == "false" and row["read_id"] in {
                        "three-overmass-valid", "unsupported-tie", "unsupported-last-tie",
                    }} == {
                        ("three-overmass-valid", "a"),
                        ("unsupported-tie", "m"),
                        ("unsupported-last-tie", "a"),
                    }
        accepted = [row for row in rows
                    if row["fail"] == "false" and row["call_code"] in {"-", "m", "h"}]
        ours = extract_cpg_calls(bam, fasta, maximum_calls=20, filter_threshold=threshold)
        assert {digest_to_name[call.fragment_digest] for call in ours.calls} == {
            row["read_id"] for row in accepted
        }
        for call in ours.calls:
            matching = next(row for row in accepted
                            if row["read_id"] == digest_to_name[call.fragment_digest])
            assert call.modified_probability == pytest.approx(float(matching["call_prob"]))
            assert call.state.value == (
                "unmethylated" if matching["call_code"] == "-" else "methylated"
            )


def test_reverse_marker_boundary_matches_pinned_modkit(tmp_path: Path) -> None:
    binary = os.environ.get("LAB_MODKIT_PARITY_BINARY")
    if not binary:
        pytest.skip("set LAB_MODKIT_PARITY_BINARY to the pinned modkit 0.6.4 binary")
    bam, fasta = _write_input(tmp_path)
    for start, expected in ((206, 0), (207, 1)):
        bed = tmp_path / f"marker-{start}.bed"
        bed.write_text(f"chr1\t{start}\t{start + 1}\n")
        output = tmp_path / f"marker-{start}.tsv"
        subprocess.run(
            [binary, "extract", "calls", "--reference", str(fasta), "--cpg",
             "--include-bed", str(bed), "--mapped-only", "--filter-threshold", "0.9",
             "--suppress-progress", str(bam), str(output)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        with output.open(newline="") as stream:
            accepted = [row for row in csv.DictReader(stream, delimiter="\t")
                        if row["fail"] == "false"]
        ours = extract_cpg_calls(
            bam, fasta, maximum_calls=100, filter_threshold=0.9,
            include_regions=read_bed_regions(bed),
        )
        assert len(ours.calls) == len(accepted) == expected


def test_matches_pinned_modkit_on_synthetic_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = os.environ.get("LAB_MODKIT_PARITY_BINARY")
    if not binary:
        pytest.skip("set LAB_MODKIT_PARITY_BINARY to the pinned modkit 0.6.4 binary")
    bam, fasta = _write_input(tmp_path)
    bed = tmp_path / "markers.bed"
    bed.write_text("chr1\t100\t107\nchr1\t206\t208\n")
    output = tmp_path / "modkit.tsv"
    subprocess.run(
        [binary, "extract", "calls", "--reference", str(fasta), "--cpg",
         "--include-bed", str(bed), "--mapped-only", "--filter-threshold", "0.9", "--suppress-progress",
         str(bam), str(output)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    monkeypatch.setattr(mmml_extract.os, "urandom", lambda size: b"\0" * size)
    ours = extract_cpg_calls(
        bam, fasta, maximum_calls=100, filter_threshold=0.9,
        include_regions=read_bed_regions(bed),
    )
    assert ours.outside_regions == 2
    observed = {
        (call.fragment_digest, call.position0, call.strand.value, call.state.value)
        for call in ours.calls
    }
    expected = set()
    with output.open(newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row["fail"] == "true":
                continue
            if row["call_code"] not in {"-", "m", "h"}:
                continue
            state = "unmethylated" if row["call_code"] == "-" else "methylated"
            position0 = int(row["ref_position"]) - (row["ref_mod_strand"] == "-")
            digest = hashlib.sha256(b"\0" * 32 + b"\0" + row["read_id"].encode()).hexdigest()
            expected.add((digest, position0, row["ref_mod_strand"], state))
    assert len(ours.calls) == len(expected)
    assert observed == expected
