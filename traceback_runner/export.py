"""Closed export adapters and claims-controlled rendering for development records.

Synthetic records render the fixed synthetic report.  Local records
(``unapproved_local``) render the fixed local report, whose banner states that
the record is unqualified, local, not for clinical use, and signed with a
development key only.
"""

from __future__ import annotations

import html
import re
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from traceback_runner.contracts import (
    AnyFragmentMeasurement,
    ApprovalState,
    ExportRunProvenance,
    FragmentMeasurement,
    FragmentMeasurementV2,
    canonical_json_bytes,
    validate_fragment_measurement,
)
from traceback_runner.measurement_schemas import (
    BundleMeasurementSchema,
    measurement_schema,
    schema_version_of,
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


SYNTHETIC_LIMITATIONS_TEMPLATE = "synthetic-fragment-length-research-use.v1"
LOCAL_LIMITATIONS_TEMPLATE = "local-fragment-length-research-use.v1"
# How preflight matched the input header to the registered reference:
# ``registered_digests`` when every header line carried matching M5/AS values,
# ``name_and_length_only`` when they were absent (the D1 WARN path).
ReferenceMatch = Literal["registered_digests", "name_and_length_only"]


class ExportLimitationsV2(_ExportModel):
    """Limitations carried by ``traceback.result-bundle.v3`` records.

    A separate class, never a widened v1.  The local template states: the
    method is unqualified, trust is development-only, no E0 protocol approval
    exists, and (when ``reference_match`` is ``name_and_length_only``) the
    reference was matched by contig name and length only.
    """

    schema_version: Literal["traceback.limitations.v2"] = "traceback.limitations.v2"
    template_id: Literal[
        "synthetic-fragment-length-research-use.v1",
        "local-fragment-length-research-use.v1",
    ]
    reference_match: ReferenceMatch

    @model_validator(mode="after")
    def synthetic_matches_digests(self) -> ExportLimitationsV2:
        if (
            self.template_id == SYNTHETIC_LIMITATIONS_TEMPLATE
            and self.reference_match != "registered_digests"
        ):
            raise ValueError("synthetic records match the reference by digest")
        return self


AnyExportLimitations = ExportLimitations | ExportLimitationsV2


def limitations_for_measurement(
    measurement: AnyFragmentMeasurement, reference_match: ReferenceMatch
) -> ExportLimitationsV2:
    """Return the v2 limitations whose template matches the approval label."""

    template = (
        LOCAL_LIMITATIONS_TEMPLATE
        if measurement.approval_state == ApprovalState.UNAPPROVED_LOCAL
        else SYNTHETIC_LIMITATIONS_TEMPLATE
    )
    return ExportLimitationsV2(template_id=template, reference_match=reference_match)


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


def _v4_schema(measurement: object) -> BundleMeasurementSchema | None:
    """The registered v4 schema a non-fragment measurement names, if any."""

    if isinstance(measurement, (FragmentMeasurement, FragmentMeasurementV2)):
        return None
    return measurement_schema(schema_version_of(measurement))


def _validate_v4_measurement(value: object, spec: BundleMeasurementSchema) -> BaseModel:
    """Re-validate a registered v4 measurement through its exact contract class."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    parsed = spec.measurement_model.model_validate(value)
    if type(parsed) is not spec.measurement_model or (
        parsed.approval_state != ApprovalState.UNAPPROVED_LOCAL  # type: ignore[attr-defined]
    ):
        raise ExportBoundaryError("a v4 measurement is an unapproved_local record")
    return parsed


def validate_measurement(
    value: AnyFragmentMeasurement | BaseModel | Mapping[str, object],
) -> AnyFragmentMeasurement | BaseModel:
    """Parse the exact measurement contract its schema names; enforce export-safe text.

    Fragment-length schemas parse through their fixed contracts; a schema
    registered for result-bundle v4 parses through its registered contract.
    """

    spec = _v4_schema(value)
    try:
        parsed: AnyFragmentMeasurement | BaseModel = (
            validate_fragment_measurement(value)
            if spec is None
            else _validate_v4_measurement(value, spec)
        )
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


def measurement_bytes(measurement: AnyFragmentMeasurement) -> bytes:
    return canonical_json_bytes(measurement)


def chart_for_measurement(
    measurement: AnyFragmentMeasurement | BaseModel, measurement_sha256: str
) -> FragmentLengthChart | BaseModel:
    """Derive the chart losslessly from the validated measurement, per schema.

    Fragment-length measurements derive the fixed fragment-length chart; a v4
    schema derives its own registered chart contract.
    """

    spec = _v4_schema(measurement)
    if spec is not None:
        chart = spec.build_chart(measurement, measurement_sha256)
        if type(chart) is not spec.chart_model:
            raise ExportBoundaryError("chart is not the registered chart contract")
        return chart
    if not isinstance(measurement, (FragmentMeasurement, FragmentMeasurementV2)):
        raise ExportBoundaryError("no chart is registered for this measurement schema")
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


def render_report(measurement: AnyFragmentMeasurement) -> bytes:
    """Render a fixed, escaped synthetic research-use report."""

    if measurement.approval_state != ApprovalState.UNAPPROVED_SYNTHETIC:
        raise ExportBoundaryError("the synthetic report renders only synthetic records")
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


LOCAL_REPORT_TEMPLATE_ID = "report-local.html"
LOCAL_REPORT_BANNER = (
    "Unqualified. Local development record. Not for clinical use. "
    "Development signing key only."
)


def _revalidated_limitations_v2(limitations: object) -> ExportLimitationsV2:
    """Re-parse supplied limitations through the v2 schema literal."""

    if type(limitations) is not ExportLimitationsV2:
        raise ExportBoundaryError("local records require v2 limitations")
    try:
        return ExportLimitationsV2.model_validate(limitations.model_dump(mode="json"))
    except Exception as exc:
        raise ExportBoundaryError("local record limitations are invalid") from exc


def render_local_report(
    measurement: AnyFragmentMeasurement, limitations: ExportLimitationsV2
) -> bytes:
    """Render the fixed, escaped ``report-local.html`` template for a local record."""

    limitations = _revalidated_limitations_v2(limitations)
    if (
        measurement.approval_state != ApprovalState.UNAPPROVED_LOCAL
        or limitations.template_id != LOCAL_LIMITATIONS_TEMPLATE
    ):
        raise ExportBoundaryError("the local report renders only local records")
    definition = html.escape(measurement.definition_id, quote=True)
    reference = html.escape(measurement.reference_id, quote=True)
    reference_item = (
        "<li>The reference was matched by contig name and length only; the input "
        "header carried no sequence digests to compare.</li>"
        if limitations.reference_match == "name_and_length_only"
        else ""
    )
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<title>Traceback local fragment-length record</title>"
        f"<p role=\"note\"><strong>{LOCAL_REPORT_BANNER}</strong></p>"
        "<h1>Local fragment-length research record</h1>"
        f"<p>Definition: <code>{definition}</code>; reference: <code>{reference}</code>.</p>"
        f"<p>Records scanned: {measurement.records_scanned}; "
        f"eligible alignments: {measurement.eligible_alignments}; "
        f"excluded alignments: {measurement.exclusions.total}; unit: bp.</p>"
        "<h2>Limitations</h2><ul>"
        "<li>The measurement method is unqualified for this input.</li>"
        "<li>The record is signed with a development key only; it carries no "
        "production trust.</li>"
        "<li>No laboratory protocol has E0 approval for this input.</li>"
        f"{reference_item}"
        "<li>The record reports aligned reference spans only. It gives no health "
        "interpretation, and no data was uploaded.</li></ul></html>"
    )
    if _FORBIDDEN_CLAIM.search(body):
        raise ExportBoundaryError("report template contains prohibited claim language")
    return body.encode("utf-8")


def _render_v4_report(
    spec: BundleMeasurementSchema, measurement: BaseModel, limitations: BaseModel
) -> bytes:
    """Render a v4 record through its registered template, then check the claims."""

    if type(limitations) is not spec.limitations_model:
        raise ExportBoundaryError("v4 records carry their registered limitations")
    body = spec.render_report(measurement, limitations)
    if type(body) is not bytes:
        raise ExportBoundaryError("a report renders to bytes")
    text = body.decode("utf-8")
    # Every local record states the banner; no template may make a claim.
    if html.escape(LOCAL_REPORT_BANNER, quote=True) not in text:
        raise ExportBoundaryError("a v4 report must carry the local record banner")
    if _FORBIDDEN_CLAIM.search(text):
        raise ExportBoundaryError("report template contains prohibited claim language")
    return body


def render_bundle_report(
    measurement: AnyFragmentMeasurement | BaseModel,
    limitations: AnyExportLimitations | BaseModel,
) -> bytes:
    """Select the one approved report template for a record's schema and label.

    Fragment-length records select by approval label (synthetic or local); a
    v4 record renders through the template its measurement schema registered.
    """

    spec = _v4_schema(measurement)
    if spec is not None:
        return _render_v4_report(spec, measurement, limitations)
    if not isinstance(measurement, (FragmentMeasurement, FragmentMeasurementV2)):
        raise ExportBoundaryError("no report is registered for this measurement schema")
    if measurement.approval_state == ApprovalState.UNAPPROVED_LOCAL:
        if type(limitations) is not ExportLimitationsV2:
            raise ExportBoundaryError("local records require v2 limitations")
        return render_local_report(measurement, limitations)
    return render_report(measurement)


__all__ = [
    "AnyExportLimitations", "ChartRow", "ExportBoundaryError", "ExportLimitations",
    "ExportLimitationsV2", "FragmentLengthChart", "LOCAL_LIMITATIONS_TEMPLATE",
    "LOCAL_REPORT_BANNER", "LOCAL_REPORT_TEMPLATE_ID", "ReferenceMatch",
    "SYNTHETIC_LIMITATIONS_TEMPLATE", "chart_for_measurement",
    "limitations_for_measurement", "measurement_bytes", "render_bundle_report",
    "render_local_report", "render_report", "validate_measurement",
    "validate_provenance",
]
