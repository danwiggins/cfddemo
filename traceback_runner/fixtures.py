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



# --- Cell origin (signal CO3): planted U/M fragments over a 3-marker mini-atlas ---

CELL_ORIGIN_CONTRIBUTORS = ("TypeA", "TypeB")
# Marker start, end and atlas U fractions (TypeA, TypeB) on chr1.
CELL_ORIGIN_MARKERS = (
    (2_000, 2_100, (0.9, 0.1)),
    (5_000, 5_100, (0.1, 0.9)),
    (8_000, 8_100, (0.5, 0.5)),
)
_CELL_ORIGIN_CONTIG = ("chr1", 12_000)


@dataclass(frozen=True)
class CellOriginFixture:
    """Generated cell-origin inputs; synthetic, never real data or numbers."""

    fasta_path: Path
    bam_path: Path
    index_path: Path
    loyfer_dir: Path
    mixture: tuple[float, float]
    planted: tuple[tuple[int, int], ...]  # (U, M) reads per marker
    other_reads: int
    excluded_reads: int


def _cpg_only_sequence(generator: random.Random, length: int, *, cpg_every: int | None) -> str:
    """A/G/T sequence whose only C bases are CpG cytosines (every ``cpg_every`` bp)."""

    bases = [generator.choice("AGT") for _ in range(length)]
    if cpg_every is not None:
        for start in range(3, length - 2, cpg_every):
            bases[start], bases[start + 1] = "C", "G"
    return "".join(bases)


def create_cell_origin_inputs(
    directory: Path,
    *,
    mixture: tuple[float, float] = (0.7, 0.3),
    reads_per_marker: int = 100,
    modification_tags: bool = True,
    mn_tag: bool = True,
    header_model: str | None = None,
    seed: int = 20261005,
) -> CellOriginFixture:
    """Write a chr1 FASTA, a modBAM of planted U/M fragments and three Loyfer files.

    Each marker region holds ten CpGs and no other C.  At marker ``k`` the
    expected U fraction is the mixture's: ``round(reads * sum(w * u))`` reads
    are fully unmethylated (ML 5) and the rest fully methylated (ML 250), so
    NNLS on the mini-atlas recovers ``mixture``.  Ten reads elsewhere on chr1
    overlap no marker, and four marker reads are excluded by the pre-filter
    (duplicate, secondary, QC failure, MAPQ 5).
    """

    directory.mkdir(parents=True, exist_ok=True)
    generator = random.Random(seed)
    name, length = _CELL_ORIGIN_CONTIG
    sequence = list(_cpg_only_sequence(generator, length, cpg_every=None))
    for start, end, _ in CELL_ORIGIN_MARKERS:
        sequence[start:end] = _cpg_only_sequence(generator, end - start, cpg_every=10)
    reference = "".join(sequence)
    fasta = directory / "cell-origin-reference.fa"
    fasta.write_text(
        f">{name} generated cell-origin contig\n"
        + "\n".join(reference[i : i + 60] for i in range(0, length, 60))
        + "\n",
        encoding="ascii",
    )
    pysam.faidx(str(fasta))

    header_dict: dict[str, Any] = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": name, "LN": length}],
    }
    if header_model is not None:
        header_dict["RG"] = [
            {"ID": "rg1", "DS": f"basecall_model=x modbase_models={header_model}"}
        ]
    header = pysam.AlignmentHeader.from_dict(header_dict)

    def segment(
        read: str, start: int, span: int, *, methylated: bool, flag: int = 0, mapq: int = 60
    ) -> pysam.AlignedSegment:
        item = pysam.AlignedSegment(header)
        item.query_name = read
        item.flag = flag
        item.reference_id = 0
        item.reference_start = start
        item.mapping_quality = mapq
        item.cigartuples = [(0, span)]
        query = reference[start : start + span]
        item.query_sequence = query
        item.query_qualities = pysam.qualitystring_to_array("I" * span)
        if modification_tags:
            cytosines = query.count("C")
            item.set_tag("MM", "C+m?" + ",0" * cytosines + ";")
            item.set_tag("ML", array.array("B", [250 if methylated else 5] * cytosines))
            if mn_tag:
                item.set_tag("MN", span)
        if header_model is not None:
            item.set_tag("RG", "rg1")
        return item

    placed: list[pysam.AlignedSegment] = []
    planted: list[tuple[int, int]] = []
    for index, (start, end, u_values) in enumerate(CELL_ORIGIN_MARKERS):
        expected_u = sum(w * u for w, u in zip(mixture, u_values, strict=True))
        unmethylated = round(reads_per_marker * expected_u)
        planted.append((unmethylated, reads_per_marker - unmethylated))
        for read in range(reads_per_marker):
            placed.append(
                segment(
                    f"co-{index}-{read:04d}", start, end - start,
                    methylated=read >= unmethylated,
                )
            )
        if index == 0:
            for flag, mapq, label in (
                (1024, 60, "dup"), (256, 60, "sec"), (512, 60, "qc"), (0, 5, "lowq")
            ):
                placed.append(
                    segment(f"co-x-{label}", start, end - start, methylated=False,
                            flag=flag, mapq=mapq)
                )
    for read in range(10):
        placed.append(segment(f"co-off-{read:02d}", 9_000 + 100 * read, 100, methylated=False))
    bam = directory / "cell-origin.bam"
    with pysam.AlignmentFile(str(bam), "wb", header=header) as output:
        for item in sorted(placed, key=lambda value: (value.reference_start, value.query_name)):
            output.write(item)
    pysam.index(str(bam))

    loyfer = directory / "loyfer"
    loyfer.mkdir(exist_ok=True)
    atlas_rows, marker_rows, region_rows = [], [], []
    for start, end, u_values in CELL_ORIGIN_MARKERS:
        region = f"{name}:{start}-{end}"
        target = CELL_ORIGIN_CONTRIBUTORS[0 if u_values[0] >= u_values[1] else 1]
        atlas_rows.append(
            f"{name}\t{start}\t{end}\t1\t11\t{target}\t{region}\tU\t"
            + "\t".join(str(value) for value in u_values)
        )
        marker_rows.append(
            f"{name}\t{start}\t{end}\t1\t11\t{target}\t{region}\t10CpGs\t100bp\t0.9\t0.1"
            "\t0.8\t0.5\t0.4\t1e-9\tU"
        )
        region_rows.append(f"{name}\t{start}\t{end}")
    (loyfer / "Atlas.U250.l4.hg38.full.tsv").write_text(
        "chr\tstart\tend\tstartCpG\tendCpG\ttarget\tname\tdirection\t"
        + "\t".join(CELL_ORIGIN_CONTRIBUTORS) + "\n" + "\n".join(atlas_rows) + "\n",
        encoding="utf-8",
    )
    (loyfer / "Markers.U250.hg38.tsv").write_text(
        "#chr\tstart\tend\tstartCpG\tendCpG\ttarget\tregion\tlenCpG\tbp\ttg_mean\tbg_mean"
        "\tdelta_means\tdelta_quants\tdelta_maxmin\tttest\tdirection\n"
        + "\n".join(marker_rows) + "\n",
        encoding="utf-8",
    )
    (loyfer / "Regions.U250.l4.hg38.bed").write_text(
        "\n".join(region_rows) + "\n", encoding="utf-8"
    )
    return CellOriginFixture(
        fasta_path=fasta,
        bam_path=bam,
        index_path=Path(f"{bam}.bai"),
        loyfer_dir=loyfer,
        mixture=mixture,
        planted=tuple(planted),
        other_reads=10,
        excluded_reads=4,
    )


# A stand-in for ``modkit extract calls`` (tests only): it reads the
# pre-filtered BAM it is given and emits one native row per CpG call from the
# MM/ML tags.
FAKE_MODKIT_SOURCE = r'''
import sys
import pysam

args = sys.argv[1:]
bam = args[-2]
print("read_id\tref_position\tchrom\tmod_strand\tmodified_primary_base\tfail\tcall_code"
      "\tcall_prob", flush=True)
with pysam.AlignmentFile(bam, "rb", check_sq=False) as reader:
    for record in reader.fetch(until_eof=True):
        pairs = dict(record.get_aligned_pairs(matches_only=True))
        for (base, strand, code), calls in sorted(record.modified_bases.items()):
            for query_position, quality in calls:
                probability = (quality + 0.5) / 256
                call = code if probability >= 0.5 else "-"
                print(f"{record.query_name}\t{pairs[query_position]}\t"
                      f"{record.reference_name}\t+\tC\tfalse\t{call}\t"
                      f"{max(probability, 1 - probability):.4f}")
'''
