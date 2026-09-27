"""Closed export schema and claims-controlled rendering for synthetic records."""

from __future__ import annotations

import html
import re
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from evidence_inspector.models import canonical_json_bytes


class ExportBoundaryError(ValueError):
    """Input cannot cross the aggregate-record export boundary."""


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
HmacCommitment = Annotated[
    str, StringConstraints(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
]


class ExportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class HistogramBin(ExportModel):
    lower_bp: int = Field(ge=0, le=1_000_000)
    upper_bp: int | None = Field(default=None, ge=1, le=1_000_000)
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> HistogramBin:
        if self.upper_bp is not None and self.upper_bp <= self.lower_bp:
            raise ValueError("histogram bin upper_bp must exceed lower_bp")
        return self


class FragmentLengthMeasurement(ExportModel):
    """Only numeric, aggregate fields admitted to the release-one record."""

    schema_version: Literal["traceback.fragment-length.v1"] = (
        "traceback.fragment-length.v1"
    )
    synthetic_only: Literal[True] = True
    measurement_definition_id: Identifier
    workflow_release_id: Identifier
    reference_id: Identifier
    completion: Literal["complete"]
    unit: Literal["bp"] = "bp"
    minimum_mapq: int = Field(ge=0, le=255)
    total_alignments: int = Field(ge=1)
    eligible_alignments: int = Field(ge=1)
    excluded_alignments: int = Field(ge=0)
    bins: tuple[HistogramBin, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def reconcile(self) -> FragmentLengthMeasurement:
        if self.eligible_alignments + self.excluded_alignments != self.total_alignments:
            raise ValueError("eligible and excluded alignments must reconcile to total")
        if sum(item.count for item in self.bins) != self.eligible_alignments:
            raise ValueError("histogram counts must reconcile to eligible alignments")
        previous_upper: int | None = None
        for index, item in enumerate(self.bins):
            if previous_upper is not None and item.lower_bp != previous_upper:
                raise ValueError("histogram bins must be ordered and contiguous")
            if item.upper_bp is None and index != len(self.bins) - 1:
                raise ValueError("only the final histogram bin may be unbounded")
            previous_upper = item.upper_bp
        _check_export_strings(self.model_dump(mode="json"))
        return self


class ChartRow(ExportModel):
    lower_bp: int = Field(ge=0, le=1_000_000)
    upper_bp: int | None = Field(default=None, ge=1, le=1_000_000)
    count: int = Field(ge=0)


class FragmentLengthChart(ExportModel):
    schema_version: Literal["traceback.fragment-length-chart.v1"] = (
        "traceback.fragment-length-chart.v1"
    )
    measurement_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    unit: Literal["bp"] = "bp"
    rows: tuple[ChartRow, ...] = Field(min_length=1)


class ToolVersion(ExportModel):
    name: Identifier
    version: Identifier


class ExportProvenance(ExportModel):
    schema_version: Literal["traceback.provenance.v1"] = "traceback.provenance.v1"
    synthetic_only: Literal[True] = True
    workflow_release_id: Identifier
    measurement_definition_id: Identifier
    reference_id: Identifier
    input_commitment: HmacCommitment
    tools: tuple[ToolVersion, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def safe_values(self) -> ExportProvenance:
        _check_export_strings(self.model_dump(mode="json"))
        return self


class ExportLimitations(ExportModel):
    schema_version: Literal["traceback.limitations.v1"] = "traceback.limitations.v1"
    template_id: Literal["synthetic-fragment-length-research-use.v1"] = (
        "synthetic-fragment-length-research-use.v1"
    )


_FORBIDDEN_CLAIM = re.compile(
    r"\b(?:cancer(?:-free)?|diagnos(?:e|ed|is|tic)|disease|healthy|normal|"
    r"reassur(?:e|ing)|screen(?:ing)?|treat(?:ment)?|clean)\b",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:\\[^\s]+)")
_RAW_SHA256 = re.compile(r"(?<!hmac-sha256:)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_SEQUENCE = re.compile(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", re.IGNORECASE)
_SENSITIVE_NAME = re.compile(
    r"(?:read[_-]?id|sample[_-]?(?:id|label|name)|secret|token|password|api[_-]?key)",
    re.IGNORECASE,
)


def _check_export_strings(value: object, *, field_name: str = "") -> None:
    """Defense in depth after closed schemas; the allowlist is the primary boundary."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_NAME.search(str(key)):
                raise ExportBoundaryError(f"forbidden export field {key!r}")
            _check_export_strings(nested, field_name=str(key))
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _check_export_strings(nested, field_name=field_name)
    elif isinstance(value, str):
        if _ABSOLUTE_PATH.search(value):
            raise ExportBoundaryError(f"absolute path is forbidden in {field_name!r}")
        if _RAW_SHA256.search(value):
            raise ExportBoundaryError(f"raw SHA-256 is forbidden in {field_name!r}")
        if field_name != "input_commitment" and _SEQUENCE.search(value):
            raise ExportBoundaryError(f"sequence-like text is forbidden in {field_name!r}")
        if _FORBIDDEN_CLAIM.search(value):
            raise ExportBoundaryError(f"clinical claim language is forbidden in {field_name!r}")


def validate_measurement(value: FragmentLengthMeasurement | Mapping[str, object]) -> FragmentLengthMeasurement:
    """Parse an exact allowlisted measurement and reject every unknown field."""

    try:
        return FragmentLengthMeasurement.model_validate(value)
    except ExportBoundaryError:
        raise
    except Exception as exc:
        raise ExportBoundaryError("measurement violates the export allowlist") from exc


def validate_provenance(value: ExportProvenance | Mapping[str, object]) -> ExportProvenance:
    """Parse export-safe provenance, never a local input manifest."""

    try:
        return ExportProvenance.model_validate(value)
    except ExportBoundaryError:
        raise
    except Exception as exc:
        raise ExportBoundaryError("provenance violates the export allowlist") from exc


def measurement_bytes(measurement: FragmentLengthMeasurement) -> bytes:
    return canonical_json_bytes(measurement)


def chart_for_measurement(
    measurement: FragmentLengthMeasurement,
    measurement_sha256: str,
) -> FragmentLengthChart:
    """Derive chart rows from the validated measurement, never caller-authored numbers."""

    return FragmentLengthChart(
        measurement_sha256=measurement_sha256,
        rows=tuple(ChartRow(**item.model_dump()) for item in measurement.bins),
    )


def render_report(measurement: FragmentLengthMeasurement) -> bytes:
    """Render a fixed, escaped synthetic research-use report."""

    definition = html.escape(measurement.measurement_definition_id, quote=True)
    reference = html.escape(measurement.reference_id, quote=True)
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback synthetic fragment-length record</title>"
        "<h1>Synthetic fragment-length research record</h1>"
        "<p>This development record uses synthetic input only. It does not qualify "
        "real data, sequencing hardware, laboratory protocols, or clinical use.</p>"
        f"<p>Definition: <code>{definition}</code>; reference: <code>{reference}</code>.</p>"
        f"<p>Eligible alignments: {measurement.eligible_alignments}; "
        f"excluded alignments: {measurement.excluded_alignments}; unit: bp.</p>"
        "<h2>Limitations</h2><p>The record reports aligned reference spans only. "
        "It provides no health interpretation and no data was uploaded.</p></html>"
    )
    if _FORBIDDEN_CLAIM.search(body):
        # Fixed copy is reviewed alongside this guard; callers cannot supply report prose.
        raise ExportBoundaryError("report template contains prohibited claim language")
    return body.encode("utf-8")


__all__ = [
    "ExportBoundaryError",
    "ExportLimitations",
    "ExportProvenance",
    "FragmentLengthChart",
    "FragmentLengthMeasurement",
    "HistogramBin",
    "ToolVersion",
    "chart_for_measurement",
    "measurement_bytes",
    "render_report",
    "validate_measurement",
    "validate_provenance",
]
