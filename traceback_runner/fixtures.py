"""Runtime-only synthetic fixtures for runner conformance tests.

The generated files are artificial and must not be used to claim real-data,
scientific, protocol, Dorado, chemistry, or hardware qualification.

``create_local_golden_path_inputs`` generates the tiny FASTA and small BAM the
golden-path acceptance run uses in CI in place of a real local BAM.
"""

from __future__ import annotations

import array
import hashlib
import json
import random
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pysam

from .contracts import (
    FragmentMeasurementPolicy,
    HistogramBin,
    ReferenceContig,
    RegisteredReference,
)

SYNTHETIC_MODIFIED_BASE_MODEL = "synthetic-mod-model.v1"


class SyntheticBamKind(StrEnum):
    ORDINARY = "ordinary"
    VALID_MODBAM = "valid_modbam"
    MISSING_MOD_TAGS = "missing_mod_tags"
    WRONG_REFERENCE = "wrong_reference"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class SyntheticBamFixture:
    """Local locators for a temporary synthetic fixture; never export this."""

    bam_path: Path
    index_path: Path | None
    kind: SyntheticBamKind
    reference_id: str
    registered_reference: RegisteredReference
    expected_modified_base_model: str | None


def synthetic_registered_reference(*, wrong: bool = False) -> RegisteredReference:
    """Return exact synthetic @SQ provenance used by generated BAMs."""

    reference_id = "wrong-synthetic-reference.v1" if wrong else "synthetic-reference.v1"
    contigs = (
        ReferenceContig(name="synthetic_chr1", length=10_000, md5=hashlib.md5(b"C" * 10_000).hexdigest()),
        ReferenceContig(name="synthetic_alt", length=2_000, md5=hashlib.md5(b"G" * 2_000).hexdigest()),
    )
    asset_digest = hashlib.sha256("|".join(f"{item.name}:{item.length}:{item.md5}" for item in contigs).encode()).hexdigest()
    return RegisteredReference(
        reference_id=reference_id,
        assembly=reference_id,
        asset_sha256=asset_digest,
        contigs=contigs,
    )


def synthetic_fragment_policy() -> FragmentMeasurementPolicy:
    """Return the locked, explicitly unapproved synthetic measurement policy."""

    return FragmentMeasurementPolicy(
        definition_id="aligned-reference-span.synthetic.v1",
        reference_id="synthetic-reference.v1",
        contigs=("synthetic_chr1",),
        min_mapping_quality=20,
        bins=(
            HistogramBin(lower_inclusive=0, upper_exclusive=100),
            HistogramBin(lower_inclusive=100, upper_exclusive=200),
            HistogramBin(lower_inclusive=200, upper_exclusive=500),
            HistogramBin(lower_inclusive=500, upper_exclusive=None),
        ),
    )


def _segment(
    header: pysam.AlignmentHeader,
    *,
    name: str,
    start: int,
    flag: int = 0,
    mapq: int = 60,
    contig: str = "synthetic_chr1",
    cigar: tuple[tuple[int, int], ...] = ((0, 100),),
    modification_tags: bool = False,
) -> pysam.AlignedSegment:
    segment = pysam.AlignedSegment(header)
    segment.query_name = name
    segment.query_sequence = "C" * 100
    segment.flag = flag
    if flag & 4:
        segment.reference_id = -1
        segment.reference_start = -1
        segment.mapping_quality = 0
        segment.cigartuples = None
    else:
        segment.reference_id = header.get_tid(contig)
        segment.reference_start = start
        segment.mapping_quality = mapq
        segment.cigartuples = list(cigar)
    segment.query_qualities = pysam.qualitystring_to_array("I" * 100)
    if modification_tags:
        segment.set_tag("MM", "C+m,0;")
        segment.set_tag("ML", array.array("B", [200]))
        segment.set_tag("MN", 100)
    return segment


def create_synthetic_bam(directory: Path, kind: SyntheticBamKind = SyntheticBamKind.VALID_MODBAM) -> SyntheticBamFixture:
    """Create a coordinate-sorted synthetic BAM and index under ``directory``."""

    directory.mkdir(parents=True, exist_ok=True)
    registered_reference = synthetic_registered_reference(wrong=kind == SyntheticBamKind.WRONG_REFERENCE)
    reference_id = registered_reference.reference_id
    header_dict: dict[str, Any] = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [
            {"SN": item.name, "LN": item.length, "AS": registered_reference.assembly, "M5": item.md5}
            for item in registered_reference.contigs
        ],
        "PG": [{"ID": "traceback-synthetic", "PN": "fixture-generator", "VN": "1"}],
        "CO": [f"synthetic-only;reference={reference_id};not-qualified"],
    }
    if kind in {SyntheticBamKind.VALID_MODBAM, SyntheticBamKind.MISSING_MOD_TAGS}:
        header_dict["PG"].append({
            "ID": "synthetic-mod-model", "PN": "not-dorado", "VN": "0",
            "DS": f"traceback.modified_base_model={SYNTHETIC_MODIFIED_BASE_MODEL}",
        })
    bam_path = directory / f"{kind.value}.bam"
    header = pysam.AlignmentHeader.from_dict(header_dict)
    with pysam.AlignmentFile(bam_path, "wb", header=header) as output:
        rows = (
            _segment(header, name="synthetic-eligible-1", start=10, modification_tags=kind == SyntheticBamKind.VALID_MODBAM),
            _segment(header, name="synthetic-low-mapq", start=200, mapq=10),
            _segment(header, name="synthetic-duplicate", start=300, flag=1024),
            _segment(header, name="synthetic-secondary", start=400, flag=256),
            _segment(header, name="synthetic-supplementary", start=500, flag=2048),
            _segment(header, name="synthetic-qcfail", start=600, flag=512),
            _segment(header, name="synthetic-alt", start=10, contig="synthetic_alt"),
            _segment(header, name="synthetic-unmapped", start=0, flag=4),
        )
        for row in rows:
            output.write(row)
    pysam.index(str(bam_path))
    index_path = Path(f"{bam_path}.bai")
    if kind == SyntheticBamKind.CORRUPT:
        data = bam_path.read_bytes()
        bam_path.write_bytes(data[: max(1, len(data) // 2)])
        index_path.unlink()
        index_path = None
    expected_model = SYNTHETIC_MODIFIED_BASE_MODEL if kind in {SyntheticBamKind.VALID_MODBAM, SyntheticBamKind.MISSING_MOD_TAGS} else None
    return SyntheticBamFixture(
        bam_path=bam_path,
        index_path=index_path,
        kind=kind,
        reference_id=reference_id,
        registered_reference=registered_reference,
        expected_modified_base_model=expected_model,
    )


def create_synthetic_minknow_run(directory: Path, *, completed: bool = True) -> Path:
    """Create fake run metadata and POD5 inventory markers, not signal data."""

    root = directory / "synthetic-minknow-run"
    pod5 = root / "pod5"
    pod5.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": "traceback.synthetic-minknow-fixture.v1",
        "synthetic_only": True,
        "protocol_run_id": "synthetic-run-0001",
        "sample_id": "synthetic-sample",
        "completed": completed,
        "warning": "Metadata fixture only; contains no POD5 signal and qualifies nothing.",
    }
    (root / "sample_sheet.json").write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    (root / "final_summary.json").write_text(json.dumps({"completed": completed}, sort_keys=True), encoding="utf-8")
    (pod5 / "inventory.json").write_text(json.dumps({"files": [], "synthetic_only": True}, sort_keys=True), encoding="utf-8")
    return root


SYNTHETIC_DORADO_MODBASE_MODEL = "synthetic_modbase_model.v1"


def create_unaligned_ont_bam(
    path: Path,
    *,
    reads: int = 50,
    contigs: dict[str, str] | None = None,
    read_group: str = "synthetic-run_synthetic-basecall",
    seed: int = 20261003,
) -> Path:
    """Write an unaligned BAM shaped like MinKNOW/Dorado output (no ``@SQ``).

    Every read is unmapped with valid ``MM``/``ML``/``MN`` tags and an ``RG``
    tag; the ``@RG DS`` declares ``basecall_model=`` and ``modbase_models=``
    the way Dorado does. With ``contigs`` (name to sequence), each read is an
    exact forward substring named ``synthetic-<contig>-<start>-<n>`` so a
    test aligner can place it; otherwise sequences are random. Synthetic
    only: no real run, model, chemistry or sample.
    """

    if reads < 1:
        raise ValueError("generate at least one read")
    path.parent.mkdir(parents=True, exist_ok=True)
    generator = random.Random(seed)
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6", "SO": "unknown"},
            "RG": [
                {
                    "ID": read_group,
                    "DS": "runid=synthetic-run basecall_model=synthetic_basecall_model.v1 "
                    f"modbase_models={SYNTHETIC_DORADO_MODBASE_MODEL}",
                }
            ],
            "PG": [{"ID": "synthetic-basecaller", "PN": "not-minknow", "VN": "0"}],
        }
    )
    with pysam.AlignmentFile(str(path), "wb", header=header) as output:
        written = 0
        while written < reads:
            length = generator.randint(300, 700)
            if contigs:
                name = generator.choice(sorted(contigs))
                start = generator.randint(0, len(contigs[name]) - length - 1)
                sequence = contigs[name][start : start + length]
                query_name = f"synthetic-{name}-{start}-{written:05d}"
            else:
                sequence = "".join(generator.choice("ACGT") for _ in range(length))
                query_name = f"synthetic-unaligned-{written:05d}"
            if "C" not in sequence:
                continue
            segment = pysam.AlignedSegment(header)
            segment.query_name = query_name
            segment.flag = 4
            segment.reference_id = -1
            segment.reference_start = -1
            segment.mapping_quality = 0
            segment.query_sequence = sequence
            segment.query_qualities = pysam.qualitystring_to_array("I" * length)
            segment.set_tag("MM", "C+m?,0;")
            segment.set_tag("ML", array.array("B", [200]))
            segment.set_tag("MN", length)
            segment.set_tag("RG", read_group)
            output.write(segment)
            written += 1
    return path


class LocalHeaderDigests(StrEnum):
    """How the generated BAM header carries ``M5``/``AS`` for each ``@SQ``."""

    ABSENT = "absent"  # the D1 WARN path: names and lengths only
    MATCHING = "matching"
    WRONG_M5 = "wrong_m5"  # a present-but-different M5 blocks preflight


@dataclass(frozen=True)
class LocalGoldenPathFixture:
    """Local locators for generated golden-path inputs; never export these."""

    fasta_path: Path
    bam_path: Path
    index_path: Path
    reads: int
    expected_eligible_alignments: int


_GOLDEN_CONTIGS = (("tiny_a", 20_000), ("tiny_b", 6_000))


def create_local_golden_path_inputs(
    directory: Path,
    *,
    reads: int = 1_000,
    header_digests: LocalHeaderDigests = LocalHeaderDigests.ABSENT,
    eligible: bool = True,
    seed: int = 20261002,
) -> LocalGoldenPathFixture:
    """Write a 2-contig FASTA (+ ``.fai``) and a coordinate-sorted, indexed BAM.

    About 85% of reads are eligible primary alignments with MAPQ 60; the rest
    exercise each exclusion (low MAPQ, duplicate, secondary, supplementary,
    QC failure, unmapped).  ``eligible=False`` gives every mapped read MAPQ 5,
    so a run finds no eligible denominator.  Output is deterministic per seed.
    """

    if reads < 10:
        raise ValueError("generate at least 10 reads")
    directory.mkdir(parents=True, exist_ok=True)
    generator = random.Random(seed)
    sequences = {
        name: "".join(generator.choice("ACGT") for _ in range(length))
        for name, length in _GOLDEN_CONTIGS
    }
    fasta = directory / "golden-reference.fa"
    lines: list[str] = []
    for name, sequence in sequences.items():
        lines.append(f">{name} generated golden-path contig")
        lines.extend(sequence[start : start + 60] for start in range(0, len(sequence), 60))
    fasta.write_text("\n".join(lines) + "\n", encoding="ascii")
    pysam.faidx(str(fasta))

    sq: list[dict[str, Any]] = []
    for name, sequence in sequences.items():
        line: dict[str, Any] = {"SN": name, "LN": len(sequence)}
        if header_digests != LocalHeaderDigests.ABSENT:
            digest = hashlib.md5(sequence.encode("ascii")).hexdigest()
            line["M5"] = "0" * 32 if header_digests == LocalHeaderDigests.WRONG_M5 else digest
            line["AS"] = "golden-assembly"
        sq.append(line)
    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": sq}
    )
    exclusion_flags = (256, 1024, 2048, 512)
    placed: list[tuple[int, int, pysam.AlignedSegment]] = []
    unplaced: list[pysam.AlignedSegment] = []
    expected = 0
    for index in range(reads):
        segment = pysam.AlignedSegment(header)
        segment.query_name = f"golden-{index:05d}"
        roll = generator.random()
        if roll < 0.02:
            segment.flag = 4
            segment.query_sequence = "A" * 50
            segment.query_qualities = pysam.qualitystring_to_array("I" * 50)
            unplaced.append(segment)
            continue
        contig = 0 if generator.random() < 0.75 else 1
        span = generator.choice((generator.randint(40, 99), generator.randint(100, 220),
                                 generator.randint(221, 700)))
        deletion = 5 if generator.random() < 0.1 else 0
        start = generator.randint(0, _GOLDEN_CONTIGS[contig][1] - span - 1)
        query_length = span - deletion
        cigar = ([(0, query_length)] if not deletion else
                 [(0, query_length // 2), (2, deletion), (0, query_length - query_length // 2)])
        segment.flag = 0
        mapq = 60
        if roll < 0.06:
            mapq = 10
        elif roll < 0.14:
            segment.flag = exclusion_flags[index % len(exclusion_flags)]
        if not eligible:
            mapq = 5
        segment.reference_id = contig
        segment.reference_start = start
        segment.mapping_quality = mapq
        segment.cigartuples = cigar
        segment.query_sequence = "".join(generator.choice("ACGT") for _ in range(query_length))
        segment.query_qualities = pysam.qualitystring_to_array("I" * query_length)
        if segment.flag == 0 and mapq >= 20:
            expected += 1
        placed.append((contig, start, segment))
    bam = directory / "golden-aligned.bam"
    with pysam.AlignmentFile(str(bam), "wb", header=header) as output:
        for _, _, segment in sorted(placed, key=lambda item: (item[0], item[1], item[2].query_name)):
            output.write(segment)
        for segment in unplaced:
            output.write(segment)
    pysam.index(str(bam))
    return LocalGoldenPathFixture(
        fasta_path=fasta,
        bam_path=bam,
        index_path=Path(f"{bam}.bai"),
        reads=reads,
        expected_eligible_alignments=expected,
    )
