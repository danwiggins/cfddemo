"""Deterministic, bounded evidence checks over prepared aggregate artifacts."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import (
    Case,
    RangeClassification,
    ReadLengthSummaryValues,
    ReferenceComparisonRow,
    ReferenceRangeComparisonValues,
    SourceReviewValues,
    ToolName,
    ToolProvenance,
    ToolResult,
    ToolStatus,
    VerificationLevel,
    canonical_json_bytes,
    resolve_sources,
    sha256_bytes,
)
from .preparation import (
    LENGTHS_FILE_NAME,
    PreparationManifest,
)

CHECK_TOOL_VERSION = "1"
MAX_LENGTH_ARTIFACT_BYTES = 2_097_152
MAX_TABLE_BYTES = 1_048_576
MAX_TABLE_ROWS = 100


class CheckInputError(ValueError):
    """Raised when immutable input evidence is malformed or mismatched."""


def _result_id(tool: ToolName, digest: str) -> str:
    return f"result.{tool.value}.{digest[:16]}"


def load_prepared_lengths(
    artifact_path: str | Path,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_LENGTH_ARTIFACT_BYTES,
) -> tuple[tuple[int, ...], str]:
    """Load and validate a compact derived-length artifact."""

    path = Path(artifact_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CheckInputError(
            f"prepared length artifact is unavailable ({type(exc).__name__})"
        ) from None
    if size > max_bytes:
        raise CheckInputError("prepared length artifact exceeds the 2 MiB cap")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CheckInputError(
            f"prepared length artifact is unavailable ({type(exc).__name__})"
        ) from None
    if len(content) > max_bytes:
        raise CheckInputError("prepared length artifact exceeds the 2 MiB cap")
    digest = sha256_bytes(content)
    if expected_sha256 is not None and digest != expected_sha256:
        raise CheckInputError("prepared length artifact digest mismatch")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        raise CheckInputError("prepared length artifact is not valid JSON") from None
    if not isinstance(payload, list):
        raise CheckInputError("prepared length artifact must be a JSON array")
    if content != canonical_json_bytes(payload):
        raise CheckInputError("prepared length artifact must use canonical compact JSON")
    lengths: list[int] = []
    for value in payload:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > 1_000_000
        ):
            raise CheckInputError(
                "prepared length artifact contains an invalid query length"
            )
        lengths.append(value)
    if not lengths:
        raise CheckInputError("prepared length artifact contains no valid reads")
    return tuple(lengths), digest


def summarize_read_lengths(
    lengths: Sequence[int],
) -> ReadLengthSummaryValues:
    """Compute the exact query-length contract over accepted positive lengths."""

    if not lengths:
        raise CheckInputError("read-length summary requires at least one value")
    clean: list[int] = []
    for value in lengths:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > 1_000_000
        ):
            raise CheckInputError("read lengths must be positive integer base pairs")
        clean.append(value)

    counts = Counter(clean)
    max_count = max(counts.values())
    mode = min(length for length, count in counts.items() if count == max_count)
    valid_count = len(clean)
    overflow_count = sum(count for length, count in counts.items() if length > 1000)
    bins = tuple(
        {"length_bp": length, "count": counts[length]}
        for length in sorted(counts)
        if length <= 1000
    )
    short_count = sum(
        count for length, count in counts.items() if 100 <= length <= 150
    )
    return ReadLengthSummaryValues(
        bins=bins,
        overflow_count=overflow_count,
        valid_read_count=valid_count,
        mode_bp=mode,
        median_bp=float(statistics.median(clean)),
        fraction_100_150=short_count / valid_count,
        fraction_gt_1000=overflow_count / valid_count,
    )


def _load_preparation_manifest(
    manifest: PreparationManifest | str | Path,
) -> PreparationManifest:
    if isinstance(manifest, PreparationManifest):
        return manifest
    try:
        return PreparationManifest.model_validate_json(Path(manifest).read_bytes())
    except OSError as exc:
        raise CheckInputError(
            f"preparation manifest is unavailable ({type(exc).__name__})"
        ) from None
    except ValueError as exc:
        raise CheckInputError(f"preparation manifest is invalid: {exc}") from None


def read_length_summary(
    artifact_path: str | Path,
    *,
    artifact_id: str,
    expected_sha256: str | None = None,
    preparation_manifest: PreparationManifest | str | Path | None = None,
    source_ids: Sequence[str] = (),
    result_id: str | None = None,
) -> ToolResult:
    """Verify a prepared artifact and return a typed read-length tool result."""

    started = time.monotonic()
    manifest = (
        _load_preparation_manifest(preparation_manifest)
        if preparation_manifest is not None
        else None
    )
    if manifest is not None:
        if manifest.artifact.id != artifact_id:
            raise CheckInputError("artifact ID does not match preparation manifest")
        if expected_sha256 is not None and (
            expected_sha256 != manifest.artifact.sha256
        ):
            raise CheckInputError("expected digest disagrees with preparation manifest")
        expected_sha256 = manifest.artifact.sha256

    lengths, digest = load_prepared_lengths(
        artifact_path,
        expected_sha256=expected_sha256,
    )
    if manifest is not None:
        if len(lengths) != manifest.accepted_count:
            raise CheckInputError("artifact count does not match preparation manifest")
        if Path(artifact_path).name != LENGTHS_FILE_NAME:
            raise CheckInputError("artifact filename does not match preparation schema")
    values = summarize_read_lengths(lengths)
    scanned_complete = (
        False if manifest is None else manifest.scanned_complete_input
    )
    verification = (
        VerificationLevel.RECOMPUTED
        if scanned_complete
        else VerificationLevel.SAMPLED_RECOMPUTED
    )
    exclusions = {} if manifest is None else manifest.exclusions
    limitations = [
        (
            "Query-sequence length is not aligned reference span or a corrected "
            "biological fragment length."
        ),
        (
            "fraction_100_150 uses all accepted reads as its denominator and is "
            "not the paper's count(100-150)/count(100-220) ratio."
        ),
    ]
    if manifest is None or not manifest.scanned_complete_input:
        limitations.append(
            "The deterministic prefix subset is nonrepresentative of a population."
        )
    if manifest is not None and manifest.partial_collection:
        limitations.append(
            "The registered inputs are a partial collection, not the complete run."
        )
    if manifest is not None and manifest.trimming.status.value == "unknown":
        limitations.append(
            "Unknown trimming blocks comparison to corrected biological lengths."
        )
    if manifest is not None and manifest.sample_linkage.status.value != "verified":
        limitations.append(
            "Unverified sample linkage blocks report-sample reproduction claims."
        )

    return ToolResult(
        id=result_id or _result_id(ToolName.READ_LENGTH_SUMMARY, digest),
        tool=ToolName.READ_LENGTH_SUMMARY,
        status=ToolStatus.OK,
        values=values.model_dump(mode="json"),
        units={
            "overflow_count": "reads",
            "valid_read_count": "reads",
            "mode_bp": "bp",
            "median_bp": "bp",
            "fraction_100_150": "fraction",
            "fraction_gt_1000": "fraction",
        },
        definitions={
            "bins": "integer query-sequence length counts from 1 through 1000 bp",
            "overflow_count": "accepted query-sequence lengths strictly above 1000 bp",
            "valid_read_count": "accepted unique positive-length primary reads",
            "mode_bp": "lowest query-sequence length among tied highest counts",
            "median_bp": "median accepted query-sequence length",
            "fraction_100_150": (
                "count of accepted query-sequence lengths from 100 through 150 "
                "bp inclusive divided by all accepted reads"
            ),
            "fraction_gt_1000": (
                "count of accepted query-sequence lengths strictly above 1000 "
                "bp divided by all accepted reads"
            ),
        },
        denominator=(
            f"accepted unique positive-length primary reads (n={values.valid_read_count})"
        ),
        filters=(
            "primary records only",
            "first eligible record per read ID across ordered inputs",
            "positive query-sequence length at most 1000000 bp",
        ),
        source_ids=tuple(source_ids),
        verification_level=verification,
        provenance=ToolProvenance(
            artifact_ids=(artifact_id,),
            artifact_digests={artifact_id: digest},
            sample_rule=(
                "validated prepared artifact"
                if manifest is None
                else manifest.sample_rule
            ),
            inspected_count=(
                len(lengths) if manifest is None else manifest.inspected_count
            ),
            accepted_count=len(lengths),
            exclusions=exclusions,
            scanned_complete_input=scanned_complete,
            stop_reason=(
                "preparation manifest not supplied"
                if manifest is None
                else manifest.stop_reason
            ),
            elapsed_ms=round((time.monotonic() - started) * 1000),
            tool_version=CHECK_TOOL_VERSION,
            parameters={
                "histogram_min_bp": 1,
                "histogram_max_bp": 1000,
                "short_fraction_min_bp": 100,
                "short_fraction_max_bp": 150,
                "serialized_artifact_cap_bytes": MAX_LENGTH_ARTIFACT_BYTES,
            },
        ),
        limitations=tuple(limitations),
    )


def _read_rows(
    source: str | Path | Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], str, bool]:
    if not isinstance(source, (str, Path)):
        rows = [dict(row) for row in source]
        try:
            content = canonical_json_bytes(rows)
        except ValueError:
            raise CheckInputError(
                "table artifact values must be finite fractions"
            ) from None
        if len(content) > MAX_TABLE_BYTES:
            raise CheckInputError("table artifact exceeds the 1 MiB cap")
        return rows, sha256_bytes(content), False

    path = Path(source)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CheckInputError(
            f"table artifact is unavailable ({type(exc).__name__})"
        ) from None
    if len(content) > MAX_TABLE_BYTES:
        raise CheckInputError("table artifact exceeds the 1 MiB cap")
    if path.suffix.lower() == ".csv":
        try:
            decoded = content.decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(decoded)))
        except (UnicodeDecodeError, csv.Error):
            raise CheckInputError("table artifact is not valid UTF-8 CSV") from None
        return rows, sha256_bytes(content), True
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        raise CheckInputError("table artifact is not valid JSON") from None
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        raise CheckInputError("JSON table artifact must be an array of objects")
    return payload, sha256_bytes(content), False


def _exact_fields(
    row: Mapping[str, Any], expected: set[str], table: str
) -> None:
    if set(row) != expected:
        raise CheckInputError(
            f"{table} row fields must be exactly {sorted(expected)}"
        )


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value) is None
    ):
        raise CheckInputError(f"{field} must be a valid identifier")
    return value


def _fraction(value: Any, field: str, *, csv_value: bool) -> float:
    if isinstance(value, bool):
        raise CheckInputError(f"{field} must be a finite fraction")
    if isinstance(value, str) and "%" in value:
        raise CheckInputError(f"{field} must use fractions, not percent strings")
    if csv_value and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise CheckInputError(f"{field} must be a finite fraction") from None
    elif isinstance(value, str):
        raise CheckInputError(f"{field} must be numeric, not a string")
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CheckInputError(f"{field} must be a finite fraction")
    result = float(value)
    if not 0 <= result <= 1:
        raise CheckInputError(f"{field} must be between 0 and 1")
    return result


def _boolean(value: Any, field: str, *, csv_value: bool) -> bool:
    if isinstance(value, bool):
        return value
    if csv_value and isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise CheckInputError(f"{field} must be boolean")


def _validate_hierarchy(
    samples: Mapping[str, tuple[str | None, bool, float]]
) -> None:
    parent_ids = {parent for parent, _, _ in samples.values() if parent is not None}
    missing = parent_ids - set(samples)
    if missing:
        raise CheckInputError(f"sample table has missing parent IDs: {sorted(missing)}")
    for start in samples:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                raise CheckInputError("sample hierarchy contains a cycle")
            seen.add(current)
            current = samples[current][0]
    children: Counter[str] = Counter(
        parent for parent, _, _ in samples.values() if parent is not None
    )
    for cell_id, (_, is_leaf, _) in samples.items():
        if is_leaf and children[cell_id]:
            raise CheckInputError(f"leaf cell type has children: {cell_id}")
        if not is_leaf and not children[cell_id]:
            raise CheckInputError(f"non-leaf cell type has no children: {cell_id}")


def compare_reference_ranges(
    sample: str | Path | Sequence[Mapping[str, Any]],
    reference: str | Path | Sequence[Mapping[str, Any]],
    *,
    sample_artifact_id: str,
    reference_artifact_id: str,
    known_source_ids: Sequence[str],
    expected_sample_sha256: str | None = None,
    expected_reference_sha256: str | None = None,
    partial_table: bool = False,
    result_id: str | None = None,
) -> ToolResult:
    """Compare canonical sample fractions to inclusive observed reference ranges."""

    started = time.monotonic()
    sample_rows, sample_digest, sample_csv = _read_rows(sample)
    reference_rows, reference_digest, reference_csv = _read_rows(reference)
    if expected_sample_sha256 and sample_digest != expected_sample_sha256:
        raise CheckInputError("sample table artifact digest mismatch")
    if expected_reference_sha256 and reference_digest != expected_reference_sha256:
        raise CheckInputError("reference table artifact digest mismatch")
    if not sample_rows or not reference_rows:
        raise CheckInputError("sample and reference tables must not be empty")
    if len(sample_rows) > MAX_TABLE_ROWS or len(reference_rows) > MAX_TABLE_ROWS:
        raise CheckInputError("table artifacts are limited to 100 rows")

    sample_fields = {"cell_type_id", "parent_id", "is_leaf", "fraction"}
    reference_fields = {
        "cell_type_id",
        "min_fraction",
        "max_fraction",
        "cohort_id",
        "source_id",
        "assay",
    }
    samples: dict[str, tuple[str | None, bool, float]] = {}
    for row in sample_rows:
        _exact_fields(row, sample_fields, "sample")
        cell_id = _identifier(row["cell_type_id"], "cell_type_id")
        if cell_id in samples:
            raise CheckInputError(f"duplicate sample cell_type_id: {cell_id}")
        parent_raw = row["parent_id"]
        if sample_csv and parent_raw == "":
            parent_raw = None
        parent = (
            None
            if parent_raw is None
            else _identifier(parent_raw, "parent_id")
        )
        if parent == cell_id:
            raise CheckInputError("a cell type cannot be its own parent")
        samples[cell_id] = (
            parent,
            _boolean(row["is_leaf"], "is_leaf", csv_value=sample_csv),
            _fraction(row["fraction"], "fraction", csv_value=sample_csv),
        )
    _validate_hierarchy(samples)

    known_sources = set(known_source_ids)
    if len(known_sources) != len(tuple(known_source_ids)):
        raise CheckInputError("known_source_ids must be unique")
    references: dict[str, tuple[float, float, str]] = {}
    for row in reference_rows:
        _exact_fields(row, reference_fields, "reference")
        cell_id = _identifier(row["cell_type_id"], "cell_type_id")
        if cell_id in references:
            raise CheckInputError(f"duplicate reference cell_type_id: {cell_id}")
        minimum = _fraction(
            row["min_fraction"], "min_fraction", csv_value=reference_csv
        )
        maximum = _fraction(
            row["max_fraction"], "max_fraction", csv_value=reference_csv
        )
        if minimum > maximum:
            raise CheckInputError("reference range minimum cannot exceed maximum")
        _identifier(row["cohort_id"], "cohort_id")
        source_id = _identifier(row["source_id"], "source_id")
        if source_id not in known_sources:
            raise CheckInputError(f"unknown reference source ID: {source_id}")
        if not isinstance(row["assay"], str) or not row["assay"].strip():
            raise CheckInputError("assay must be nonempty text")
        references[cell_id] = (minimum, maximum, source_id)

    if set(samples) != set(references):
        missing_reference = sorted(set(samples) - set(references))
        missing_sample = sorted(set(references) - set(samples))
        raise CheckInputError(
            "sample/reference cell_type_id sets must match; "
            f"without_reference={missing_reference}, without_sample={missing_sample}"
        )

    rows: list[ReferenceComparisonRow] = []
    cited_sources: set[str] = set()
    for cell_id in sorted(samples):
        fraction = samples[cell_id][2]
        minimum, maximum, source_id = references[cell_id]
        classification = (
            RangeClassification.BELOW
            if fraction < minimum
            else RangeClassification.ABOVE
            if fraction > maximum
            else RangeClassification.WITHIN
        )
        cited_sources.add(source_id)
        rows.append(
            ReferenceComparisonRow(
                cell_type_id=cell_id,
                fraction=fraction,
                min_fraction=minimum,
                max_fraction=maximum,
                classification=classification,
                source_ids=(source_id,),
            )
        )
    values = ReferenceRangeComparisonValues(
        rows=tuple(rows),
        partial_table=partial_table,
    )
    combined_digest = sha256_bytes(
        canonical_json_bytes(
            {
                sample_artifact_id: sample_digest,
                reference_artifact_id: reference_digest,
            }
        )
    )
    return ToolResult(
        id=result_id
        or _result_id(ToolName.COMPARE_REFERENCE_RANGES, combined_digest),
        tool=ToolName.COMPARE_REFERENCE_RANGES,
        status=ToolStatus.OK,
        values=values.model_dump(mode="json"),
        units={"rows": "canonical fractions from 0 to 1"},
        definitions={
            "rows": (
                "independent inclusive comparisons of supplied sample fractions "
                "to supplied observed cohort bounds"
            )
        },
        denominator=None,
        filters=(
            "no parent-child aggregation",
            "inclusive minimum and maximum bounds",
            "matched cell_type_id rows only",
        ),
        source_ids=tuple(sorted(cited_sources)),
        verification_level=VerificationLevel.RECOMPUTED,
        provenance=ToolProvenance(
            artifact_ids=(sample_artifact_id, reference_artifact_id),
            artifact_digests={
                sample_artifact_id: sample_digest,
                reference_artifact_id: reference_digest,
            },
            sample_rule="all validated supplied table rows; no aggregation",
            inspected_count=len(rows),
            accepted_count=len(rows),
            exclusions={},
            scanned_complete_input=not partial_table,
            stop_reason=(
                "complete validated tables"
                if not partial_table
                else "source transcription marked partial"
            ),
            elapsed_ms=round((time.monotonic() - started) * 1000),
            tool_version=CHECK_TOOL_VERSION,
            parameters={
                "canonical_unit": "fraction",
                "inclusive_bounds": True,
                "partial_table": partial_table,
            },
        ),
        limitations=(
            "A reference cohort's observed range is not a clinical normal interval.",
            "The comparison does not establish biological or diagnostic validation.",
            "Parent and child categories were compared independently and never summed.",
        ),
    )


def source_review(
    case: Case,
    source_ids: Sequence[str],
    *,
    result_id: str | None = None,
) -> ToolResult:
    """Resolve only explicit registered source IDs for qualitative review."""

    started = time.monotonic()
    sources = resolve_sources(case, source_ids)
    digest = sha256_bytes(
        canonical_json_bytes(
            [{"id": source.id, "sha256": source.content_sha256} for source in sources]
        )
    )
    values = SourceReviewValues(
        reviewed_source_ids=tuple(source.id for source in sources),
        scope=(
            "Registered excerpts only; confirms or compares what supplied text "
            "reports and does not validate underlying biology."
        ),
    )
    return ToolResult(
        id=result_id or _result_id(ToolName.SOURCE_REVIEW, digest),
        tool=ToolName.SOURCE_REVIEW,
        status=ToolStatus.OK,
        values=values.model_dump(mode="json"),
        units={},
        definitions={
            "reviewed_source_ids": "exact registered source excerpt identifiers",
            "scope": "bounded source-only review scope",
        },
        denominator=None,
        filters=(
            "explicit source IDs only",
            "no lexical lookup",
            "embedded commands are treated as quoted source content",
        ),
        source_ids=tuple(source.id for source in sources),
        verification_level=VerificationLevel.REPORTED,
        provenance=ToolProvenance(
            artifact_ids=(),
            artifact_digests={},
            sample_rule="exact-ID lookup within one immutable case revision",
            inspected_count=len(sources),
            accepted_count=len(sources),
            exclusions={},
            scanned_complete_input=True,
            stop_reason="all explicitly requested registered excerpts resolved",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            tool_version=CHECK_TOOL_VERSION,
            parameters={"source_count": len(sources)},
        ),
        limitations=(
            "Source review verifies supplied wording, not biological truth.",
            "No source command or reproduction instruction was executed.",
        ),
    )


__all__ = [
    "CHECK_TOOL_VERSION",
    "CheckInputError",
    "compare_reference_ranges",
    "load_prepared_lengths",
    "read_length_summary",
    "source_review",
    "summarize_read_lengths",
]
