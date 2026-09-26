"""Pure fragment-length algorithms used to regenerate presentation charts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import LengthBin, ReadLengthSummaryValues

# SAM CIGAR operations that consume reference bases: M, D, N, =, X.
REFERENCE_CONSUMING_CIGAR_OPS = frozenset({0, 2, 3, 7, 8})


def reference_span_from_record(
    record: Any,
    *,
    minimum_mapping_quality: int = 20,
) -> int | None:
    """Return aligned reference span, excluding clipping and ineligible records.

    This is the report's updated measurement definition: the genomic footprint
    represented by reference-consuming CIGAR operations. Soft/hard clipping and
    insertions do not consume reference bases and are therefore excluded without
    applying a fixed adapter subtraction.
    """

    if (
        bool(getattr(record, "is_unmapped", False))
        or bool(getattr(record, "is_secondary", False))
        or bool(getattr(record, "is_supplementary", False))
    ):
        return None
    mapping_quality = getattr(record, "mapping_quality", None)
    if (
        isinstance(mapping_quality, bool)
        or not isinstance(mapping_quality, int)
        or mapping_quality < minimum_mapping_quality
    ):
        return None
    cigartuples = getattr(record, "cigartuples", None)
    if not cigartuples:
        return None
    span = sum(
        length
        for operation, length in cigartuples
        if operation in REFERENCE_CONSUMING_CIGAR_OPS
    )
    return span if span > 0 else None


def collect_reference_spans(
    records: Iterable[Any],
    *,
    maximum_spans: int = 100_000,
    maximum_records: int = 1_000_000,
    minimum_mapping_quality: int = 20,
) -> tuple[tuple[int, ...], Mapping[str, int]]:
    """Collect a bounded aligned-span vector and exclusion counts."""

    if maximum_spans < 1 or maximum_records < 1:
        raise ValueError("record and span caps must be positive")
    spans: list[int] = []
    exclusions: Counter[str] = Counter()
    inspected = 0
    for record in records:
        if inspected >= maximum_records or len(spans) >= maximum_spans:
            break
        inspected += 1
        span = reference_span_from_record(
            record,
            minimum_mapping_quality=minimum_mapping_quality,
        )
        if span is None:
            exclusions["ineligible_or_unmapped"] += 1
            continue
        spans.append(span)
    if not spans:
        raise ValueError("no eligible aligned reference spans were found")
    return tuple(spans), {
        "inspected_records": inspected,
        "accepted_spans": len(spans),
        **dict(sorted(exclusions.items())),
    }


def chart_rows(
    values: ReadLengthSummaryValues | Mapping[str, Any],
    *,
    bin_width: int = 5,
    minimum_bp: int = 50,
    maximum_bp: int = 800,
) -> tuple[dict[str, int], ...]:
    """Aggregate exact integer bins into stable chart buckets."""

    if bin_width < 1 or minimum_bp < 1 or maximum_bp < minimum_bp:
        raise ValueError("invalid chart bounds")
    summary = (
        values
        if isinstance(values, ReadLengthSummaryValues)
        else ReadLengthSummaryValues.model_validate(values)
    )
    buckets: Counter[int] = Counter()
    for item in summary.bins:
        if minimum_bp <= item.length_bp <= maximum_bp:
            bucket = minimum_bp + (
                (item.length_bp - minimum_bp) // bin_width
            ) * bin_width
            buckets[bucket] += item.count
    return tuple(
        {"length_bp": bucket, "read_count": buckets.get(bucket, 0)}
        for bucket in range(minimum_bp, maximum_bp + 1, bin_width)
    )


def summarize_spans(spans: Sequence[int]) -> ReadLengthSummaryValues:
    """Summarize aligned spans with the same numerical contract as raw lengths."""

    if not spans:
        raise ValueError("at least one aligned span is required")
    counts = Counter(spans)
    highest = max(counts.values())
    mode = min(length for length, count in counts.items() if count == highest)
    ordered = sorted(spans)
    midpoint = len(ordered) // 2
    median = (
        float(ordered[midpoint])
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    )
    overflow = sum(count for length, count in counts.items() if length > 1_000)
    short = sum(
        count for length, count in counts.items() if 100 <= length <= 150
    )
    return ReadLengthSummaryValues(
        bins=tuple(
            LengthBin(length_bp=length, count=count)
            for length, count in sorted(counts.items())
            if length <= 1_000
        ),
        overflow_count=overflow,
        valid_read_count=len(spans),
        mode_bp=mode,
        median_bp=median,
        fraction_100_150=short / len(spans),
        fraction_gt_1000=overflow / len(spans),
    )


__all__ = [
    "REFERENCE_CONSUMING_CIGAR_OPS",
    "chart_rows",
    "collect_reference_spans",
    "reference_span_from_record",
    "summarize_spans",
]
