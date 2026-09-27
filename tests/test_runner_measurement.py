"""Synthetic-only tests for BAM preflight and complete span measurement."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceback_runner.measurement import (
    MeasurementUnavailableError,
    ScanCompletion,
    canonical_measurement_bytes,
    chart_data,
    finalize_measurement,
    scan_aligned_reference_spans,
    scan_records,
)
from traceback_runner.preflight import (
    BamPreflightPolicy,
    validate_bam_snapshot,
)
from traceback_runner.contracts import (
    FragmentMeasurementPolicy,
    HistogramBin,
    PreflightOutcome,
    ReferenceContig,
    RegisteredReference,
)
from traceback_runner.fixtures import (
    SyntheticBamKind,
    create_synthetic_bam,
    synthetic_fragment_policy,
)
from traceback_runner.snapshots import capture_snapshot

CONTIG_MD5 = "0" * 32
OTHER_MD5 = "1" * 32


def _reference() -> RegisteredReference:
    return RegisteredReference(
        reference_id="synthetic-reference-v1",
        assembly="synthetic-assembly-v1",
        asset_sha256="2" * 64,
        contigs=(ReferenceContig(name="chrSynthetic1", length=10_000, md5=CONTIG_MD5),),
    )


def _preflight_policy(*, model: str | None = "synthetic-mod-model-v1") -> BamPreflightPolicy:
    return BamPreflightPolicy(
        policy_id="synthetic-preflight-v1",
        modified_base_model_id=model,
    )


def _measurement_policy() -> FragmentMeasurementPolicy:
    return FragmentMeasurementPolicy(
        definition_id="synthetic-reference-span-v1",
        reference_id="synthetic-reference-v1",
        contigs=("chrSynthetic1",),
        min_mapping_quality=20,
        bins=(
            HistogramBin(lower_inclusive=0, upper_exclusive=100),
            HistogramBin(lower_inclusive=100, upper_exclusive=150),
            HistogramBin(lower_inclusive=150, upper_exclusive=200),
            HistogramBin(lower_inclusive=200, upper_exclusive=None),
        ),
    )


def _header(
    *,
    sort_order: str = "coordinate",
    length: int = 10_000,
    md5: str = CONTIG_MD5,
    assembly: str = "synthetic-assembly-v1",
    model: str | None = "synthetic-mod-model-v1",
) -> dict[str, object]:
    header: dict[str, object] = {
        "HD": {"VN": "1.6", "SO": sort_order},
        "SQ": [
            {
                "SN": "chrSynthetic1",
                "LN": length,
                "M5": md5,
                "AS": assembly,
            }
        ],
    }
    if model is not None:
        header["PG"] = [
            {
                "ID": "synthetic-basecaller",
                "PN": "fixture-generator",
                "DS": f"traceback.modified_base_model={model}",
            }
        ]
    return header


def _segment(
    pysam: object,
    *,
    name: str,
    start: int,
    cigar: list[tuple[int, int]] | None = None,
    flag: int = 0,
    mapq: int = 60,
    tags: bool | str = False,
) -> object:
    record = pysam.AlignedSegment()
    record.query_name = name
    actual_cigar = cigar if cigar is not None else [(0, 20)]
    query_length = sum(
        length for operation, length in actual_cigar if operation in {0, 1, 4, 7, 8}
    )
    record.query_sequence = "C" * query_length
    record.flag = flag
    if flag & 4:
        record.reference_id = -1
        record.reference_start = -1
    else:
        record.reference_id = 0
        record.reference_start = start
    record.mapping_quality = mapq
    record.cigartuples = actual_cigar
    record.query_qualities = pysam.qualitystring_to_array("I" * query_length)
    if tags:
        record.set_tag("MM", "C+m,0;")
        if tags != "partial":
            record.set_tag("ML", [200])
            record.set_tag("MN", query_length)
    return record


def _write_bam(
    tmp_path: Path,
    records: list[dict[str, object]],
    *,
    header: dict[str, object] | None = None,
    stem: str = "sealed-snapshot",
    index: bool = True,
) -> tuple[Path, Path]:
    import pysam

    bam_path = tmp_path / f"{stem}.bam"
    with pysam.AlignmentFile(bam_path, "wb", header=header or _header()) as bam:
        for values in records:
            bam.write(_segment(pysam, **values))
    index_path = tmp_path / f"{stem}.bam.bai"
    if index:
        pysam.index(str(bam_path), str(index_path))
    return bam_path, index_path


def test_preflight_passes_exact_reference_and_valid_modification_tags(
    tmp_path: Path,
) -> None:
    bam_path, index_path = _write_bam(
        tmp_path,
        [{"name": "private-read-id", "start": 10, "tags": True}],
    )

    result = validate_bam_snapshot(
        bam_path, index_path, _reference(), _preflight_policy()
    )

    assert result.fragment_measurement_eligible is True
    assert result.future_methylation_eligible is True
    assert all(check.outcome == PreflightOutcome.PASS for check in result.checks)
    serialized = result.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "private-read-id" not in serialized


@pytest.mark.parametrize(
    ("header", "expected_code"),
    [
        (_header(length=9_999), "TBX-BAM-002"),
        (_header(md5=OTHER_MD5), "TBX-BAM-002"),
        (_header(assembly="other-assembly"), "TBX-BAM-002"),
        (_header(sort_order="unknown"), "TBX-BAM-001"),
    ],
)
def test_preflight_blocks_wrong_or_contradictory_header(
    tmp_path: Path, header: dict[str, object], expected_code: str
) -> None:
    bam_path, index_path = _write_bam(
        tmp_path,
        [{"name": "read", "start": 10}],
        header=header,
        stem=expected_code.lower(),
    )
    result = validate_bam_snapshot(
        bam_path, index_path, _reference(), _preflight_policy()
    )
    assert result.fragment_measurement_eligible is False
    assert any(
        check.code == expected_code and check.outcome == PreflightOutcome.BLOCKED
        for check in result.checks
    )


def test_preflight_proves_record_order_not_only_header_claim(tmp_path: Path) -> None:
    bam_path, index_path = _write_bam(
        tmp_path,
        [
            {"name": "later", "start": 100},
            {"name": "earlier", "start": 10},
        ],
        index=False,
    )
    result = validate_bam_snapshot(
        bam_path, index_path, _reference(), _preflight_policy()
    )
    assert result.fragment_measurement_eligible is False
    assert any(
        check.outcome == PreflightOutcome.BLOCKED and "sort order" in check.problem
        for check in result.checks
    )


def test_ordinary_bam_and_missing_tags_remain_fragment_eligible(tmp_path: Path) -> None:
    bam_path, index_path = _write_bam(
        tmp_path,
        [{"name": "ordinary", "start": 10}],
        header=_header(model=None),
    )
    result = validate_bam_snapshot(
        bam_path, index_path, _reference(), _preflight_policy()
    )
    assert result.fragment_measurement_eligible is True
    assert result.future_methylation_eligible is False
    assert any(
        check.outcome == PreflightOutcome.PARTIAL for check in result.checks
    )


def test_contradictory_tags_are_ineligible_but_mixed_presence_is_allowed(
    tmp_path: Path,
) -> None:
    partial_bam, partial_index = _write_bam(
        tmp_path,
        [{"name": "partial", "start": 10, "tags": "partial"}],
        stem="partial-tags",
    )
    partial = validate_bam_snapshot(
        partial_bam, partial_index, _reference(), _preflight_policy()
    )
    assert partial.fragment_measurement_eligible is True
    assert partial.future_methylation_eligible is False
    assert any(check.code == "TBX-MOD-002" for check in partial.checks)

    mixed_bam, mixed_index = _write_bam(
        tmp_path,
        [
            {"name": "tagged", "start": 10, "tags": True},
            {"name": "untagged", "start": 100},
        ],
        stem="mixed-tags",
    )
    mixed = validate_bam_snapshot(
        mixed_bam, mixed_index, _reference(), _preflight_policy()
    )
    assert mixed.fragment_measurement_eligible is True
    assert mixed.future_methylation_eligible is True


def test_preflight_blocks_missing_index_and_truncated_bam(tmp_path: Path) -> None:
    bam_path, missing_index = _write_bam(
        tmp_path,
        [{"name": "read", "start": 10}],
        index=False,
    )
    missing = validate_bam_snapshot(
        bam_path, missing_index, _reference(), _preflight_policy()
    )
    assert missing.fragment_measurement_eligible is False

    truncated_path = tmp_path / "truncated.bam"
    truncated_path.write_bytes(bam_path.read_bytes()[:-20])
    truncated = validate_bam_snapshot(
        truncated_path, missing_index, _reference(), _preflight_policy()
    )
    assert truncated.fragment_measurement_eligible is False
    assert truncated.checks[0].code == "TBX-BAM-001"


def test_preflight_rejects_index_from_different_snapshot(tmp_path: Path) -> None:
    bam_path, _ = _write_bam(
        tmp_path,
        [
            {"name": "one", "start": 10},
            {"name": "two", "start": 100},
        ],
        stem="two-records",
    )
    _, wrong_index = _write_bam(
        tmp_path,
        [{"name": "one", "start": 10}],
        stem="one-record",
    )
    result = validate_bam_snapshot(
        bam_path, wrong_index, _reference(), _preflight_policy()
    )
    assert result.fragment_measurement_eligible is False
    assert any(
        check.outcome == PreflightOutcome.BLOCKED and "index" in check.problem
        for check in result.checks
    )


@dataclass
class FakeRecord:
    cigartuples: list[tuple[int, int]] | None = None
    mapping_quality: int = 60
    reference_name: str = "chrSynthetic1"
    is_unmapped: bool = False
    is_secondary: bool = False
    is_supplementary: bool = False
    is_qcfail: bool = False
    is_duplicate: bool = False
    is_paired: bool = False


def test_scan_executes_cigar_filters_pairing_bins_and_reconciliation() -> None:
    eligible = FakeRecord(
        cigartuples=[
            (4, 5),
            (0, 20),
            (1, 4),
            (2, 3),
            (3, 7),
            (7, 10),
            (8, 11),
            (5, 2),
        ]
    )
    paired_primary = FakeRecord(cigartuples=[(0, 200)], is_paired=True)
    records = [
        eligible,
        paired_primary,
        FakeRecord(cigartuples=[(0, 20)], is_unmapped=True),
        FakeRecord(cigartuples=[(0, 20)], is_secondary=True),
        FakeRecord(cigartuples=[(0, 20)], is_supplementary=True),
        FakeRecord(cigartuples=[(0, 20)], is_qcfail=True),
        FakeRecord(cigartuples=[(0, 20)], is_duplicate=True),
        FakeRecord(cigartuples=[(0, 20)], reference_name="chrOther"),
        FakeRecord(cigartuples=[(0, 20)], mapping_quality=19),
        FakeRecord(cigartuples=None),
        FakeRecord(cigartuples=[(4, 20)]),
    ]

    result = scan_records(records, _measurement_policy())

    assert result.completion == ScanCompletion.COMPLETE
    assert result.publishable is True
    assert result.records_scanned == 11
    assert result.eligible_alignments == 2
    assert result.exclusions.total == 9
    assert chart_data(result) == (
        {"lower_inclusive": 0, "upper_exclusive": 100, "count": 1},
        {"lower_inclusive": 100, "upper_exclusive": 150, "count": 0},
        {"lower_inclusive": 150, "upper_exclusive": 200, "count": 0},
        {"lower_inclusive": 200, "upper_exclusive": None, "count": 1},
    )
    assert [row.count for row in result.histogram] == [
        1,
        0,
        0,
        1,
    ]


def test_complete_bam_scan_is_deterministic_and_contains_no_private_values(
    tmp_path: Path,
) -> None:
    bam_path, _ = _write_bam(
        tmp_path,
        [
            {"name": "private-one", "start": 10, "cigar": [(0, 100)]},
            {
                "name": "private-two",
                "start": 200,
                "cigar": [(4, 2), (0, 80), (2, 5), (7, 20)],
                "flag": 1,
            },
            {"name": "private-low-mapq", "start": 500, "mapq": 19},
        ],
    )
    first = scan_aligned_reference_spans(bam_path, _measurement_policy())
    second = scan_aligned_reference_spans(bam_path, _measurement_policy())
    first_bytes = canonical_measurement_bytes(first)
    assert first_bytes == canonical_measurement_bytes(second)
    payload = json.loads(first_bytes)
    assert payload["eligible_alignments"] == 2
    assert payload["completion"] == "complete"
    assert finalize_measurement(first).schema_version == "traceback.fragment-measurement.v1"
    assert str(tmp_path).encode() not in first_bytes
    assert b"private-one" not in first_bytes
    assert sum(row["count"] for row in chart_data(first)) == 2


def test_shared_synthetic_fixture_runs_through_preflight_and_measurement(
    tmp_path: Path,
) -> None:
    fixture = create_synthetic_bam(tmp_path, SyntheticBamKind.VALID_MODBAM)
    assert fixture.index_path is not None
    preflight = validate_bam_snapshot(
        fixture.bam_path,
        fixture.index_path,
        fixture.registered_reference,
        BamPreflightPolicy(
            policy_id="synthetic-preflight-v1",
            modified_base_model_id=fixture.expected_modified_base_model,
        ),
    )
    assert preflight.outcome == PreflightOutcome.PASS
    scan = scan_aligned_reference_spans(
        fixture.bam_path, synthetic_fragment_policy()
    )
    measurement = finalize_measurement(scan)
    assert measurement.records_scanned == 8
    assert measurement.eligible_alignments == 1
    assert measurement.exclusions.total == 7
    assert measurement.exclusions.unregistered_contig == 1


def test_measurement_reads_runner_owned_sealed_snapshot_paths(tmp_path: Path) -> None:
    delivery = tmp_path / "mutable-delivery"
    fixture = create_synthetic_bam(delivery, SyntheticBamKind.VALID_MODBAM)
    assert fixture.index_path is not None
    snapshot = capture_snapshot(
        delivery,
        (fixture.bam_path.name, fixture.index_path.name),
        tmp_path / "runner-snapshots",
        snapshot_id="synthetic-sealed-input",
    )
    bam_path = snapshot.path / fixture.bam_path.name
    index_path = snapshot.path / fixture.index_path.name
    assert bam_path.stat().st_mode & 0o222 == 0
    assert index_path.stat().st_mode & 0o222 == 0

    report = validate_bam_snapshot(
        bam_path,
        index_path,
        fixture.registered_reference,
        BamPreflightPolicy(
            policy_id="synthetic-preflight-v1",
            modified_base_model_id=fixture.expected_modified_base_model,
        ),
    )
    assert report.fragment_measurement_eligible is True
    measurement = finalize_measurement(
        scan_aligned_reference_spans(bam_path, synthetic_fragment_policy())
    )
    assert measurement.eligible_alignments == 1


@pytest.mark.parametrize(
    "scan",
    [
        scan_records([], _measurement_policy()),
        scan_records(
            [FakeRecord(cigartuples=[(0, 100)]), FakeRecord(cigartuples=[(0, 120)])],
            _measurement_policy(),
            maximum_records=1,
        ),
        scan_records(
            [FakeRecord(cigartuples=[(0, 100)])],
            _measurement_policy(),
            should_interrupt=lambda _: True,
        ),
    ],
)
def test_zero_capped_and_interrupted_scans_cannot_publish(scan: object) -> None:
    assert scan.publishable is False
    with pytest.raises(MeasurementUnavailableError):
        canonical_measurement_bytes(scan)
    with pytest.raises(MeasurementUnavailableError):
        chart_data(scan)
