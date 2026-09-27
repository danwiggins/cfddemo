"""Closed export adapters and claims-controlled rendering for synthetic records."""

from __future__ import annotations

import html
import re
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from traceback_runner.contracts import (
    ExportRunProvenance,
    FragmentMeasurement,
    canonical_json_bytes,
)


class ExportBoundaryError(ValueError):
    """Input cannot cross the aggregate-record export boundary."""


class _ExportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True,
        validate_default=True, allow_inf_nan=False,
    )


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ChartRow(_ExportModel):
    lower_inclusive: int = Field(ge=0)
    upper_exclusive: int | None = Field(default=None, gt=0)
    count: int = Field(ge=0)


class FragmentLengthChart(_ExportModel):
    schema_version: Literal["traceback.fragment-length-chart.v1"] = "traceback.fragment-length-chart.v1"
    measurement_sha256: Sha256
    unit: Literal["bp"] = "bp"
    rows: tuple[ChartRow, ...] = Field(min_length=1)


class ExportLimitations(_ExportModel):
    schema_version: Literal["traceback.limitations.v1"] = "traceback.limitations.v1"
    template_id: Literal["synthetic-fragment-length-research-use.v1"] = "synthetic-fragment-length-research-use.v1"


_FORBIDDEN_CLAIM = re.compile(
    r"\b(?:cancer(?:-free)?|diagnos(?:e|ed|is|tic)|disease|healthy|normal|"
    r"reassur(?:e|ing)|screen(?:ing)?|treat(?:ment)?|clean)\b",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:\\[^\s]+)")
_RAW_SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_SEQUENCE = re.compile(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", re.IGNORECASE)


def _check_export_strings(value: object, *, field_name: str = "") -> None:
    """Defense in depth after strict shared models enforce the real allowlist."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            _check_export_strings(nested, field_name=str(key))
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _check_export_strings(nested, field_name=field_name)
    elif isinstance(value, str):
        if _ABSOLUTE_PATH.search(value):
            raise ExportBoundaryError(f"absolute path is forbidden in {field_name!r}")
        if field_name != "provider_hmac_sha256" and _RAW_SHA256.search(value):
            raise ExportBoundaryError(f"raw SHA-256 is forbidden in {field_name!r}")
        if field_name != "provider_hmac_sha256" and _SEQUENCE.search(value):
            raise ExportBoundaryError(f"sequence-like text is forbidden in {field_name!r}")
        if _FORBIDDEN_CLAIM.search(value):
            raise ExportBoundaryError(f"clinical claim language is forbidden in {field_name!r}")


def validate_measurement(value: FragmentMeasurement | Mapping[str, object]) -> FragmentMeasurement:
    """Parse the shared exact measurement contract and enforce export-safe text."""

    try:
        parsed = FragmentMeasurement.model_validate(value)
        _check_export_strings(parsed.model_dump(mode="json"))
        return parsed
    except ExportBoundaryError:
        raise
    except Exception as exc:
        raise ExportBoundaryError("measurement violates the export allowlist") from exc


def validate_provenance(value: ExportRunProvenance | Mapping[str, object]) -> ExportRunProvenance:
    """Parse shared export provenance, never a local input manifest."""

    try:
        parsed = ExportRunProvenance.model_validate(value)
        _check_export_strings(parsed.model_dump(mode="json"))
        return parsed
    except ExportBoundaryError:
        raise
    except Exception as exc:
        raise ExportBoundaryError("provenance violates the export allowlist") from exc


def measurement_bytes(measurement: FragmentMeasurement) -> bytes:
    return canonical_json_bytes(measurement)


def chart_for_measurement(measurement: FragmentMeasurement, measurement_sha256: str) -> FragmentLengthChart:
    """Derive chart rows losslessly from the validated shared measurement."""

    return FragmentLengthChart(
        measurement_sha256=measurement_sha256,
        rows=tuple(
            ChartRow(
                lower_inclusive=item.bin.lower_inclusive,
                upper_exclusive=item.bin.upper_exclusive,
                count=item.count,
            )
            for item in measurement.histogram
        ),
    )


def render_report(measurement: FragmentMeasurement) -> bytes:
    """Render a fixed, escaped synthetic research-use report."""

    definition = html.escape(measurement.definition_id, quote=True)
    reference = html.escape(measurement.reference_id, quote=True)
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback synthetic fragment-length record</title>"
        "<h1>Synthetic fragment-length research record</h1>"
        "<p>This development record uses synthetic input only. It does not qualify "
        "real data, sequencing hardware, laboratory protocols, or clinical use.</p>"
        f"<p>Definition: <code>{definition}</code>; reference: <code>{reference}</code>.</p>"
        f"<p>Eligible alignments: {measurement.eligible_alignments}; "
        f"excluded alignments: {measurement.exclusions.total}; unit: bp.</p>"
        "<h2>Limitations</h2><p>The record reports aligned reference spans only. "
        "It provides no health interpretation and no data was uploaded.</p></html>"
    )
    if _FORBIDDEN_CLAIM.search(body):
        raise ExportBoundaryError("report template contains prohibited claim language")
    return body.encode("utf-8")


__all__ = [
    "ChartRow", "ExportBoundaryError", "ExportLimitations",
    "FragmentLengthChart", "chart_for_measurement", "measurement_bytes",
    "render_report", "validate_measurement", "validate_provenance",
]
