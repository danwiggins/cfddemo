"""Q10 pre-filter before modkit: pure flag/MAPQ rule, region index, BAM writer."""

from __future__ import annotations

from pathlib import Path

import pysam
import pytest

from evidence_inspector.cell_origin_prefilter import (
    PREFILTER_REASONS,
    PrefilterCounts,
    RegionIndex,
    prefilter_exclusion,
    read_bed_regions,
    write_prefiltered_bam,
)
from traceback_runner.measurement import ExclusionReason


@pytest.mark.parametrize(
    ("flag", "mapq", "expected"),
    [
        (0, 60, None),
        (16, 20, None),  # reverse strand, MAPQ exactly at the floor
        (0, 19, ExclusionReason.LOW_MAPPING_QUALITY),
        (0x4, 0, ExclusionReason.UNMAPPED),
        (0x100, 60, ExclusionReason.SECONDARY),
        (0x800, 60, ExclusionReason.SUPPLEMENTARY),
        (0x200, 60, ExclusionReason.QC_FAILURE),
        (0x400, 60, ExclusionReason.DUPLICATE),
        # First matching reason wins, in the fragment policy's order.
        (0x100 | 0x400, 5, ExclusionReason.SECONDARY),
        (0x800 | 0x200, 60, ExclusionReason.SUPPLEMENTARY),
        (0x200 | 0x400, 5, ExclusionReason.QC_FAILURE),
        (0x400, 5, ExclusionReason.DUPLICATE),
    ],
)
def test_prefilter_exclusion_matches_the_fragment_policy(
    flag: int, mapq: int, expected: ExclusionReason | None
) -> None:
    assert prefilter_exclusion(flag, mapq) == expected


def test_prefilter_min_mapq_is_a_parameter() -> None:
    assert prefilter_exclusion(0, 10, min_mapq=10) is None
    assert prefilter_exclusion(0, 10, min_mapq=11) == ExclusionReason.LOW_MAPPING_QUALITY


def test_region_index_overlap_is_half_open_and_merged() -> None:
    regions = RegionIndex.from_intervals(
        [("chr1", 100, 200), ("chr1", 150, 300), ("chr1", 500, 600), ("chr2", 0, 10)]
    )
    assert regions.starts["chr1"] == (100, 500)
    assert regions.ends["chr1"] == (300, 600)
    assert regions.overlaps("chr1", 299, 400)
    assert not regions.overlaps("chr1", 300, 500)  # touches both ends, overlaps none
    assert not regions.overlaps("chr1", 0, 100)
    assert regions.overlaps("chr1", 0, 101)
    assert regions.overlaps("chr1", 550, 551)
    assert not regions.overlaps("chr3", 0, 1000)
    assert not regions.overlaps(None, 0, 1000)


def test_region_index_refuses_empty_intervals() -> None:
    with pytest.raises(ValueError):
        RegionIndex.from_intervals([("chr1", 5, 5)])


def test_read_bed_regions(tmp_path: Path) -> None:
    bed = tmp_path / "regions.bed"
    bed.write_text("#header\nchr1\t10\t20\textra\nchr1\t30\t40\n", encoding="utf-8")
    regions = read_bed_regions(bed)
    assert regions.starts["chr1"] == (10, 30)
    bed.write_text("chr1\t10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="3 columns"):
        read_bed_regions(bed)


def _write_bam(path: Path, records: list[tuple[str, int, int, int]]) -> None:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chr1", "LN": 10_000}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for name, flag, mapq, start in records:
            segment = pysam.AlignedSegment(bam.header)
            segment.query_name = name
            segment.query_sequence = "ACGT" * 25
            segment.query_qualities = pysam.qualitystring_to_array("I" * 100)
            segment.flag = flag
            if flag & 0x4:
                segment.reference_id = -1
                segment.reference_start = -1
            else:
                segment.reference_id = 0
                segment.reference_start = start
                segment.cigar = [(0, 100)]
                segment.mapping_quality = mapq
            bam.write(segment)


def test_write_prefiltered_bam_keeps_only_passing_region_reads(tmp_path: Path) -> None:
    source = tmp_path / "in.bam"
    _write_bam(
        source,
        [
            ("keep-1", 0, 60, 1000),
            ("secondary", 0x100, 60, 1000),
            ("supplementary", 0x800, 60, 1000),
            ("qcfail", 0x200, 60, 1000),
            ("duplicate", 0x400, 60, 1000),
            ("low-mapq", 0, 19, 1000),
            ("outside", 0, 60, 5000),
            ("keep-2", 16, 20, 1950),
            ("unmapped", 0x4, 0, 0),
        ],
    )
    destination = tmp_path / "job" / "filtered.bam"
    destination.parent.mkdir()
    counts = write_prefiltered_bam(
        source, destination, RegionIndex.from_intervals([("chr1", 1050, 2000)])
    )
    with pysam.AlignmentFile(str(destination), "rb") as bam:
        assert [record.query_name for record in bam.fetch()] == ["keep-1", "keep-2"]
    assert Path(str(destination) + ".bai").is_file()
    assert counts.records_scanned == 9
    assert counts.written == 2
    assert counts.outside_regions == 1
    assert {reason: counts.excluded[reason] for reason in PREFILTER_REASONS} == {
        ExclusionReason.UNMAPPED: 1,
        ExclusionReason.SECONDARY: 1,
        ExclusionReason.SUPPLEMENTARY: 1,
        ExclusionReason.QC_FAILURE: 1,
        ExclusionReason.DUPLICATE: 1,
        ExclusionReason.LOW_MAPPING_QUALITY: 1,
    }


def test_prefilter_counts_must_reconcile() -> None:
    with pytest.raises(ValueError, match="every scanned record"):
        PrefilterCounts(
            records_scanned=3,
            written=1,
            outside_regions=0,
            excluded={reason: 0 for reason in PREFILTER_REASONS},
        )
