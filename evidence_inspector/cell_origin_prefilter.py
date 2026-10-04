"""Alignment pre-filter applied before modkit (signal-methods spec Q10).

Decision: a pysam pre-filter, not modkit flags.  ``modkit extract calls``
0.6.4 has no MAPQ, duplicate or QC-fail filter; it drops secondary and
supplementary alignments only by default (``--allow-non-primary`` reverses
that) and unmapped ones only with ``--mapped-only``.  The fragment policy's
exclusions (unmapped, secondary, supplementary, QC-fail, duplicate,
MAPQ < 20) are therefore applied here, in the fragment policy's order and
with its reason names, so the denominators of the two records of one BAM
agree.

The writer keeps only passing alignments that overlap a marker region (modkit
``--include-bed`` would drop the others anyway), so the filtered BAM, which
holds read names, stays small.  It is written only under the caller's job
directory.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from traceback_runner.measurement import ExclusionReason

DEFAULT_MIN_MAPQ = 20

FLAG_UNMAPPED = 0x4
FLAG_SECONDARY = 0x100
FLAG_QC_FAILURE = 0x200
FLAG_DUPLICATE = 0x400
FLAG_SUPPLEMENTARY = 0x800

PREFILTER_REASONS = (
    ExclusionReason.UNMAPPED,
    ExclusionReason.SECONDARY,
    ExclusionReason.SUPPLEMENTARY,
    ExclusionReason.QC_FAILURE,
    ExclusionReason.DUPLICATE,
    ExclusionReason.LOW_MAPPING_QUALITY,
)


def prefilter_exclusion(
    flag: int, mapping_quality: int, *, min_mapq: int = DEFAULT_MIN_MAPQ
) -> ExclusionReason | None:
    """Why one alignment is excluded before modkit, or ``None`` to keep it.

    Pure: depends only on the SAM flag and MAPQ.  The first matching reason
    wins, in the fragment policy's order (``traceback_runner.measurement``).
    """

    if flag & FLAG_UNMAPPED:
        return ExclusionReason.UNMAPPED
    if flag & FLAG_SECONDARY:
        return ExclusionReason.SECONDARY
    if flag & FLAG_SUPPLEMENTARY:
        return ExclusionReason.SUPPLEMENTARY
    if flag & FLAG_QC_FAILURE:
        return ExclusionReason.QC_FAILURE
    if flag & FLAG_DUPLICATE:
        return ExclusionReason.DUPLICATE
    if mapping_quality < min_mapq:
        return ExclusionReason.LOW_MAPPING_QUALITY
    return None


@dataclass(frozen=True, slots=True)
class RegionIndex:
    """Merged half-open ``[start, end)`` intervals per contig."""

    starts: Mapping[str, tuple[int, ...]]
    ends: Mapping[str, tuple[int, ...]]

    @classmethod
    def from_intervals(cls, intervals: Iterable[tuple[str, int, int]]) -> RegionIndex:
        by_contig: dict[str, list[tuple[int, int]]] = {}
        for contig, start, end in intervals:
            if start < 0 or end <= start:
                raise ValueError("region intervals must be non-empty and non-negative")
            by_contig.setdefault(contig, []).append((start, end))
        starts: dict[str, tuple[int, ...]] = {}
        ends: dict[str, tuple[int, ...]] = {}
        for contig, items in by_contig.items():
            merged: list[list[int]] = []
            for start, end in sorted(items):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            starts[contig] = tuple(item[0] for item in merged)
            ends[contig] = tuple(item[1] for item in merged)
        return cls(starts=starts, ends=ends)

    def overlaps(self, contig: str | None, start: int, end: int) -> bool:
        """Whether ``[start, end)`` on ``contig`` overlaps any region."""

        if contig is None or end <= start:
            return False
        starts = self.starts.get(contig)
        if not starts:
            return False
        index = bisect_right(starts, end - 1) - 1  # last region starting before end
        return index >= 0 and self.ends[contig][index] > start


def read_bed_regions(path: Path, *, max_rows: int = 1_000_000) -> RegionIndex:
    """Regions from a BED file's first three columns (header lines skipped)."""

    intervals = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number > max_rows:
                raise ValueError("region BED exceeds the row cap")
            if not line.strip() or line.startswith(("#", "track", "browser")):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3:
                raise ValueError("region BED line has fewer than 3 columns")
            intervals.append((fields[0], int(fields[1]), int(fields[2])))
    return RegionIndex.from_intervals(intervals)


@dataclass(frozen=True, slots=True)
class PrefilterCounts:
    records_scanned: int
    written: int
    outside_regions: int
    excluded: Mapping[ExclusionReason, int]

    def __post_init__(self) -> None:
        if self.records_scanned != self.written + self.outside_regions + sum(
            self.excluded.values()
        ):
            raise ValueError("every scanned record must be written, outside or excluded")


def write_prefiltered_bam(
    source: Path,
    destination: Path,
    regions: RegionIndex,
    *,
    min_mapq: int = DEFAULT_MIN_MAPQ,
) -> PrefilterCounts:
    """Copy passing, region-overlapping alignments of a coordinate-sorted BAM.

    Order is preserved, so ``destination`` stays sorted and is indexed.
    """

    import pysam

    excluded: Counter[ExclusionReason] = Counter()
    scanned = written = outside = 0
    with pysam.AlignmentFile(str(source), "rb", check_sq=False) as reader:
        with pysam.AlignmentFile(str(destination), "wb", template=reader) as writer:
            for record in reader.fetch(until_eof=True):
                scanned += 1
                reason = prefilter_exclusion(
                    record.flag, record.mapping_quality, min_mapq=min_mapq
                )
                if reason is not None:
                    excluded[reason] += 1
                    continue
                end = record.reference_end
                if end is None or not regions.overlaps(
                    record.reference_name, record.reference_start, end
                ):
                    outside += 1
                    continue
                writer.write(record)
                written += 1
    pysam.index(str(destination))
    return PrefilterCounts(
        records_scanned=scanned,
        written=written,
        outside_regions=outside,
        excluded={reason: excluded.get(reason, 0) for reason in PREFILTER_REASONS},
    )


def prefilter_summary(counts: PrefilterCounts) -> Sequence[tuple[str, int]]:
    """Stable ``(reason, count)`` rows for provenance or a report."""

    return tuple((reason.value, counts.excluded[reason]) for reason in PREFILTER_REASONS)


__all__ = [
    "DEFAULT_MIN_MAPQ",
    "PREFILTER_REASONS",
    "PrefilterCounts",
    "RegionIndex",
    "prefilter_exclusion",
    "prefilter_summary",
    "read_bed_regions",
    "write_prefiltered_bam",
]
