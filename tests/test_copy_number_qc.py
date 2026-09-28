"""Exact accounting and reference-bound fixtures for dosage QC v2."""

from __future__ import annotations

import json
from pathlib import Path

import pysam
import pytest
from pydantic import ValidationError

from evidence_inspector.copy_number import CopyNumberResultBundle
from evidence_inspector.copy_number_qc import (
    AUTOSOMES,
    BinDefinition,
    DosageQcResultBundle,
    ReferenceBinding,
    ReferenceContigBinding,
    bin_definition_sha256,
    compute_dosage_qc,
    fixed_width_bins,
    scan_bam,
    validate_bins,
)

FIXTURES = Path(__file__).parent / "fixtures" / "copy_number"


def _manifest() -> dict[str, object]:
    return json.loads((FIXTURES / "reference_manifest.json").read_text())


def _reference_and_bins() -> tuple[ReferenceBinding, tuple[BinDefinition, ...]]:
    manifest = _manifest()
    lengths = manifest["autosome_lengths"] | manifest["extra_contigs"]
    contigs = tuple(
        ReferenceContigBinding(
            name=name,
            length=length,
            md5=manifest["contig_md5"],
        )
        for name, length in lengths.items()
    )
    bins = fixed_width_bins(contigs, window_size_bp=10)
    reference = ReferenceBinding(
        reference_id=manifest["reference_id"],
        assembly=manifest["assembly"],
        fasta_sha256=manifest["fasta_sha256"],
        contigs=contigs,
        bin_definition_sha256=bin_definition_sha256(bins),
    )
    return reference, bins


def _record(
    header: pysam.AlignmentHeader,
    *,
    name: str,
    contig: str | None,
    start: int = 0,
    flag: int = 0,
    mapq: int = 60,
) -> pysam.AlignedSegment:
    record = pysam.AlignedSegment(header)
    record.query_name = name
    record.query_sequence = "A"
    record.query_qualities = pysam.qualitystring_to_array("I")
    record.flag = flag
    record.mapping_quality = mapq
    if contig is None:
        record.reference_id = -1
        record.reference_start = -1
        record.cigar = ()
    else:
        record.reference_id = header.get_tid(contig)
        record.reference_start = start
        record.cigar = ((0, 1),)
    return record


def _write_fixture_bam(path: Path) -> Path:
    manifest = _manifest()
    lengths = manifest["autosome_lengths"] | manifest["extra_contigs"]
    header = pysam.AlignmentHeader.from_dict(
        {
            "HD": {"VN": "1.6"},
            "SQ": [
                {"SN": name, "LN": length, "M5": manifest["contig_md5"]}
                for name, length in lengths.items()
            ],
        }
    )
    records = [
        _record(header, name=f"chr1-{position}", contig="chr1", start=position)
        for position in (0, 9, 10, 19, 20, 24)
    ]
    for chromosome in AUTOSOMES[1:]:
        records.extend(
            (
                _record(header, name=f"{chromosome}-0", contig=chromosome, start=0),
                _record(header, name=f"{chromosome}-10", contig=chromosome, start=10),
            )
        )
    records.extend(
        (
            _record(header, name="unmapped", contig=None, flag=0x4),
            _record(header, name="secondary", contig="chr2", flag=0x100),
            _record(header, name="supplementary", contig="chr2", flag=0x800),
            _record(header, name="qc-failure", contig="chr2", flag=0x200),
            _record(header, name="duplicate", contig="chr2", flag=0x400),
            _record(header, name="low-mapq", contig="chr2", mapq=19),
            _record(header, name="non-autosomal", contig="chrUn"),
        )
    )
    with pysam.AlignmentFile(path, "wb", header=header) as stream:
        for record in records:
            stream.write(record)
    return path


def test_exact_half_open_edges_and_mutually_exclusive_accounting(tmp_path: Path) -> None:
    reference, bins = _reference_and_bins()
    bam = _write_fixture_bam(tmp_path / "edge.bam")
    scan = scan_bam(
        bam,
        reference=reference,
        bins=bins,
        window_size_bp=10,
    )
    repeated = scan_bam(
        bam,
        reference=reference,
        bins=bins,
        window_size_bp=10,
    )
    assert repeated.model_dump_json() == scan.model_dump_json()

    accounting = scan.accounting
    assert accounting.inspected_alignment_count == 55
    assert accounting.accepted_autosomal_read_count == 48
    assert accounting.excluded_unmapped == 1
    assert accounting.excluded_secondary == 1
    assert accounting.excluded_supplementary == 1
    assert accounting.excluded_qc_failure == 1
    assert accounting.excluded_duplicate == 1
    assert accounting.excluded_below_mapq == 1
    assert accounting.excluded_non_autosomal == 1
    assert accounting.excluded_outside_analyzed_bins == 0
    assert accounting.accepted_autosomal_read_count == sum(
        row.accepted_read_start_count for row in scan.bins
    )
    assert scan.reference_verification.sequence_identity == "md5_verified"

    expected_edges = json.loads((FIXTURES / "bin_edges.json").read_text())["cases"]
    chr1 = [row for row in scan.bins if row.contig == "chr1"]
    for case in expected_edges:
        matching = [
            row
            for row in chr1
            if row.start <= case["position"] < row.end
        ]
        assert len(matching) == 1
        assert matching[0].start == case["expected_start"]
        assert matching[0].end == case["expected_end"]
        assert matching[0].is_terminal_partial_bin is case["terminal_partial"]
    assert [row.accepted_read_start_count for row in chr1] == [2, 2, 2]
    assert chr1[-1].included_in_screen is False
    assert chr1[-1].exclusion_reason == "terminal_partial_bin"


def test_result_identity_and_cross_level_totals_are_strict(tmp_path: Path) -> None:
    reference, bins = _reference_and_bins()
    scan = scan_bam(
        _write_fixture_bam(tmp_path / "result.bam"),
        reference=reference,
        bins=bins,
        window_size_bp=10,
    )
    result = compute_dosage_qc(
        scan,
        input_artifact_sha256="b" * 64,
        reference=reference,
        window_size_bp=10,
    )

    assert result.schema_version == "copy-number-dosage-qc.v2"
    assert result.identity.intended_use == "exploratory_quality_control"
    assert result.identity.calibration_status == "uncalibrated"
    assert result.identity.diagnostic_interpretation_allowed is False
    assert result.identity.tumor_fraction_estimate_present is False
    assert result.identity.qualification_status == "development_unqualified"
    assert result.chromosomes[0].accepted_read_count == 6
    assert result.chromosomes[0].complete_bin_count == 2
    assert result.chromosomes[0].dosage_direction == "higher_relative_dosage"

    payload = result.model_dump(mode="python")
    payload["bins"][0]["accepted_read_start_count"] += 1
    with pytest.raises(ValueError, match="accepted read count"):
        DosageQcResultBundle.model_validate(payload)

    moved = result.model_dump(mode="python")
    moved["bins"][0]["accepted_read_start_count"] += 1
    chr2_first = next(
        index for index, row in enumerate(moved["bins"]) if row["contig"] == "chr2"
    )
    moved["bins"][chr2_first]["accepted_read_start_count"] -= 1
    with pytest.raises(ValueError, match="chromosome read count"):
        DosageQcResultBundle.model_validate(moved)


def test_reference_and_bin_changes_fail_closed(tmp_path: Path) -> None:
    reference, bins = _reference_and_bins()
    bam = _write_fixture_bam(tmp_path / "reference.bam")

    changed_length = reference.model_dump(mode="python")
    changed_length["contigs"][0]["length"] = 26
    changed_contigs = tuple(
        ReferenceContigBinding.model_validate(row) for row in changed_length["contigs"]
    )
    changed_bins = fixed_width_bins(changed_contigs, window_size_bp=10)
    changed_length["bin_definition_sha256"] = bin_definition_sha256(changed_bins)
    with pytest.raises(ValueError, match="contig length mismatch"):
        scan_bam(
            bam,
            reference=ReferenceBinding.model_validate(changed_length),
            bins=changed_bins,
            window_size_bp=10,
        )

    changed_md5 = reference.model_dump(mode="python")
    changed_md5["contigs"][0]["md5"] = "f" * 32
    with pytest.raises(ValueError, match="MD5 mismatch"):
        scan_bam(
            bam,
            reference=ReferenceBinding.model_validate(changed_md5),
            bins=bins,
            window_size_bp=10,
        )

    changed_digest = reference.model_copy(
        update={"bin_definition_sha256": "f" * 64}
    )
    with pytest.raises(ValueError, match="digest"):
        validate_bins(changed_digest, bins, window_size_bp=10)

    with pytest.raises(ValueError, match="exact ordered reference tiling"):
        validate_bins(reference, tuple(reversed(bins)), window_size_bp=10)

    mixed_names = reference.model_dump(mode="python")
    mixed_names["contigs"][0]["name"] = "1"
    with pytest.raises(ValueError, match="missing autosomes"):
        ReferenceBinding.model_validate(mixed_names)


def test_historical_v1_and_additive_v2_do_not_relabel_each_other(tmp_path: Path) -> None:
    reference, bins = _reference_and_bins()
    scan = scan_bam(
        _write_fixture_bam(tmp_path / "versions.bam"),
        reference=reference,
        bins=bins,
        window_size_bp=10,
    )
    v2 = compute_dosage_qc(
        scan,
        input_artifact_sha256="c" * 64,
        reference=reference,
        window_size_bp=10,
    )

    with pytest.raises(ValidationError):
        CopyNumberResultBundle.model_validate(v2.model_dump(mode="python"))

    v2_payload = v2.model_dump(mode="python")
    v2_payload["schema_version"] = "copy-number-screen.v1"
    with pytest.raises(ValidationError):
        DosageQcResultBundle.model_validate(v2_payload)
