"""Tests for algorithmic fragment-length regeneration."""

from dataclasses import dataclass

import pytest

from evidence_inspector.fragmentomics import (
    chart_rows,
    collect_reference_spans,
    reference_span_from_record,
    summarize_spans,
)
@dataclass
class Record:
    cigartuples: list[tuple[int, int]] | None
    mapping_quality: int = 60
    is_unmapped: bool = False
    is_secondary: bool = False
    is_supplementary: bool = False


def test_reference_span_uses_reference_consuming_cigar_operations() -> None:
    # 45S + 100M + 3I + 20M + 2D + 5H => 122 reference bases.
    record = Record([(4, 45), (0, 100), (1, 3), (0, 20), (2, 2), (5, 5)])
    assert reference_span_from_record(record) == 122


@pytest.mark.parametrize(
    "record",
    [
        Record([(0, 100)], is_unmapped=True),
        Record([(0, 100)], is_secondary=True),
        Record([(0, 100)], is_supplementary=True),
        Record([(0, 100)], mapping_quality=19),
        Record(None),
        Record([(4, 45), (1, 10)]),
    ],
)
def test_reference_span_excludes_ineligible_records(record: Record) -> None:
    assert reference_span_from_record(record) is None


def test_collection_is_bounded_and_records_exclusions() -> None:
    records = [
        Record([(0, 100)]),
        Record([(0, 120)], is_unmapped=True),
        Record([(4, 45), (0, 167)]),
        Record([(0, 200)]),
    ]
    spans, counts = collect_reference_spans(records, maximum_spans=2)
    assert spans == (100, 167)
    assert counts == {
        "inspected_records": 3,
        "accepted_spans": 2,
        "ineligible_or_unmapped": 1,
    }


def test_span_summary_and_chart_buckets_are_deterministic() -> None:
    summary = summarize_spans([100, 102, 151, 167, 167, 1_001])
    assert summary.mode_bp == 167
    assert summary.median_bp == 159
    assert summary.fraction_100_150 == pytest.approx(2 / 6)
    assert summary.fraction_gt_1000 == pytest.approx(1 / 6)

    rows = chart_rows(summary, bin_width=5, minimum_bp=100, maximum_bp=170)
    assert rows[0] == {"length_bp": 100, "read_count": 2}
    assert next(row for row in rows if row["length_bp"] == 165)["read_count"] == 2


def test_chart_rows_revalidates_mapping_input() -> None:
    with pytest.raises(ValueError):
        chart_rows({"bins": []})
