"""Synthetic aligned-BAM builder for the Lab algorithm tests (no real data)."""

from __future__ import annotations

import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pysam

from traceback_lab.read_summary import write_summary
from traceback_lab.registry import InputContext

SHA = "0" * 64


@dataclass
class Read:
    name: str
    contig: str
    start: int
    sequence: str
    flag: int = 0
    mapq: int = 60
    cigar: list[tuple[int, int]] | None = None  # default: one M over the sequence
    mm: str | None = None
    ml: Sequence[int] = ()
    mn: int | None = None
    tags: dict[str, object] = field(default_factory=dict)


def write_bam(
    path: Path, contigs: Sequence[tuple[str, int]], reads: Iterable[Read], *, platform: str | None = None,
) -> Path:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": name, "LN": length} for name, length in contigs]}
    if platform is not None:
        header["RG"] = [{"ID": "synthetic", "PL": platform}]
    names = [name for name, _ in contigs]
    unsorted = path.with_suffix(".unsorted.bam")
    with pysam.AlignmentFile(str(unsorted), "wb", header=header) as out:
        for read in reads:
            record = pysam.AlignedSegment(out.header)
            record.query_name = read.name
            record.query_sequence = read.sequence
            if read.flag & 0x4:
                record.flag = read.flag
                record.reference_id = -1
                record.reference_start = -1
            else:
                record.flag = read.flag
                record.reference_id = names.index(read.contig)
                record.reference_start = read.start
                record.mapping_quality = read.mapq
                record.cigartuples = read.cigar or [(0, len(read.sequence))]
            record.query_qualities = pysam.qualitystring_to_array("5" * len(read.sequence))
            if read.mm is not None:
                record.set_tag("MM", read.mm, value_type="Z")
                record.set_tag("ML", array.array("B", list(read.ml)))
                record.set_tag("MN", read.mn if read.mn is not None else len(read.sequence), value_type="i")
            for key, value in read.tags.items():
                record.set_tag(key, value)
            if platform is not None and "RG" not in read.tags:
                record.set_tag("RG", "synthetic")
            out.write(record)
    pysam.sort("-o", str(path), str(unsorted))
    pysam.index(str(path))
    unsorted.unlink()
    return path


def context_for(
    base: Path,
    contigs: Sequence[tuple[str, int]],
    reads: Iterable[Read],
    *,
    derived: dict[str, object] | None = None,
    fasta: Path | None = None,
    eligible_reads: int | None = None,
) -> InputContext:
    """An InputContext over a fresh aligned BAM and its read summary."""

    input_dir = base / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    bam = input_dir / "aligned.bam"
    if not bam.exists():
        write_bam(bam, contigs, reads)
        write_summary(bam, base / "read-summary" / "columns")
    extras: dict[str, object] = {"upstream": {"read-summary": str(base / "read-summary")}}
    if fasta is not None:
        extras["reference_fasta"] = str(fasta)
    return InputContext(
        root=base,
        source_id=SHA,
        input_id=SHA,
        reference_id="synth",
        reference_sha256=SHA,
        input_dir=input_dir,
        eligible_reads=eligible_reads,
        derived=derived,
        extras=extras,
    )


def values(output) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {item["metric_id"]: item["value"] for item in output.metrics}
