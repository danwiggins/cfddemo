"""Runtime-only synthetic fixtures for runner conformance tests.

The generated files are artificial and must not be used to claim real-data,
scientific, protocol, Dorado, chemistry, or hardware qualification.
"""

from __future__ import annotations

import array
import hashlib
import json
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
