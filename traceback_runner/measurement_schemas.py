"""Registration point for the measurement schemas that ``result-bundle.v4`` carries.

A v4 bundle carries exactly one measurement.  Its measurement and chart paths,
its contracts, its chart and report derivation, its byte bounds and its catalog
binding are all chosen by that measurement's schema, through the one
:class:`BundleMeasurementSchema` registered for it here.  So is the local
records site's view of it (:class:`RecordViewBinding`, signal SH5).

The fragment-length schemas are not registered here and never travel in v4:
they keep the fixed v1-v3 rules, paths and renderers, so every existing record
verifies byte for byte.  Each new analysis (cell origin, copy number) registers
its own schema from the module that defines its contract; nothing registers
by default.

Threat model: in-process code mutation is out of scope.  Registration is a
module-import-time act by trusted code; the registry validates shape, not
intent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from .contracts import FRAGMENT_MEASUREMENT_MODELS
from .serialization import canonical_model_from_bytes

# ``measurements/<stem>.json`` and ``charts/<stem>.json``; the stem names the
# analysis and its contract version, for example ``cell-origin.v1``.
PATH_STEM = r"[a-z0-9]+(?:-[a-z0-9]+)*\.v[1-9][0-9]*"
_PATH_STEM = re.compile(rf"^{PATH_STEM}$")
_SCHEMA_VERSION = re.compile(r"^traceback\.[a-z0-9]+(?:-[a-z0-9]+)*\.v[1-9][0-9]*$")
FRAGMENT_PATH_STEM = "fragment-length.v1"
MAX_MEASUREMENT_FILE_BYTES = 16 * 1024 * 1024
_REQUIRED_MEASUREMENT_FIELDS = frozenset({"schema_version", "approval_state", "reference_id"})

#: Every analysis the local records site names (signal SH5).  Fragment length
#: keeps its v1-v3 rules and its own view; each v4 view names one of the others.
ANALYSES = ("fragment", "cell_origin", "copy_number")
FRAGMENT_VIEW_SCHEMA = "traceback.local-record-view.v1"
_VIEW_SCHEMA_VERSION = re.compile(
    r"^traceback\.local-[a-z0-9]+(?:-[a-z0-9]+)*-view\.v[1-9][0-9]*$"
)


class MeasurementSchemaError(ValueError):
    """A schema registration is malformed or conflicts with an existing one."""


@dataclass(frozen=True)
class LocalCatalogBinding:
    """How ``traceback catalog import`` binds one v4 measurement schema.

    ``authority`` opens (or creates) the local method authority for a
    registered reference: ``(root, registered_reference) -> LocalMethodAuthority``.
    ``denominator`` builds the E06 ``DenominatorLedger`` from a verified bundle.

    With ``method_slug`` set, the record binds the hash-keyed method store
    ``ROOT/method-authority/<reference>/<method_slug>/<definition sha256>``
    named by its own manifest (signal SH1); ``authority`` is then not called.
    The store is opened and validated, never created, by the import.
    """

    result_schema_id: str
    result_schema_version: str
    accessible_label: str
    normalization_semantics_id: str
    coordinate_semantics_id: str
    denominator_semantics_id: str
    authority: Callable[[Any, Any], Any]
    denominator: Callable[[Any], Any]
    method_slug: str | None = None


@dataclass(frozen=True)
class RecordViewBinding:
    """How the local records site shows one v4 measurement schema (signal SH5).

    ``build_body(measurement)`` returns the analysis-specific part of the view
    (a pydantic model); the site wraps it in the common envelope (banner,
    identity, states, preflight).  ``key_count(measurement)`` is the one count
    the catalog table shows, in ``key_count_unit``.  No estimate (a mixture
    fraction, a tumour fraction) is ever taken from here into the catalog row.
    Both callables must be pure functions of the verified measurement.
    """

    analysis: str
    view_schema_version: str
    key_count_unit: str
    key_count: Callable[[Any], int]
    build_body: Callable[[Any], BaseModel]


@dataclass(frozen=True)
class BundleMeasurementSchema:
    """Everything a v4 bundle needs to know about one measurement schema.

    ``build_limitations(measurement, reference_match)`` and
    ``render_report(measurement, limitations)`` must be pure functions of their
    inputs: verification re-derives the chart, limitations and report and
    compares bytes.
    """

    schema_version: str
    path_stem: str
    measurement_model: type[BaseModel]
    chart_model: type[BaseModel]
    limitations_model: type[BaseModel]
    build_chart: Callable[[Any, str], BaseModel]
    build_limitations: Callable[[Any, str], BaseModel]
    render_report: Callable[[Any, Any], bytes]
    catalog: LocalCatalogBinding
    record_view: RecordViewBinding | None = None
    max_measurement_bytes: int = MAX_MEASUREMENT_FILE_BYTES
    max_chart_bytes: int = MAX_MEASUREMENT_FILE_BYTES

    @property
    def measurement_path(self) -> str:
        return f"measurements/{self.path_stem}.json"

    @property
    def chart_path(self) -> str:
        return f"charts/{self.path_stem}.json"

    def parse_measurement(self, content: bytes) -> BaseModel:
        """Parse exact canonical bytes that must name this schema."""

        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("content is not valid UTF-8 JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != self.schema_version:
            raise ValueError("measurement does not carry this schema version")
        return canonical_model_from_bytes(self.measurement_model, content)


def _literal_default(model: type[BaseModel], field: str) -> object:
    info = model.model_fields.get(field)
    return None if info is None else info.default


def _validate(spec: BundleMeasurementSchema) -> None:
    if not isinstance(spec, BundleMeasurementSchema):
        raise MeasurementSchemaError("registration takes a BundleMeasurementSchema")
    if not _SCHEMA_VERSION.fullmatch(spec.schema_version):
        raise MeasurementSchemaError("measurement schema version is malformed")
    if spec.schema_version in FRAGMENT_MEASUREMENT_MODELS:
        raise MeasurementSchemaError(
            "fragment-length measurements keep their v1-v3 bundle rules"
        )
    if not _PATH_STEM.fullmatch(spec.path_stem) or spec.path_stem == FRAGMENT_PATH_STEM:
        raise MeasurementSchemaError("measurement path stem is malformed or reserved")
    for model in (spec.measurement_model, spec.chart_model, spec.limitations_model):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise MeasurementSchemaError("contracts must be pydantic models")
    if not _REQUIRED_MEASUREMENT_FIELDS <= set(spec.measurement_model.model_fields):
        raise MeasurementSchemaError(
            "a v4 measurement carries schema_version, approval_state and reference_id"
        )
    if _literal_default(spec.measurement_model, "schema_version") != spec.schema_version:
        raise MeasurementSchemaError("measurement model does not default to its schema")
    if "reference_match" not in spec.limitations_model.model_fields:
        raise MeasurementSchemaError("v4 limitations state how the reference was matched")
    for limit in (spec.max_measurement_bytes, spec.max_chart_bytes):
        if type(limit) is not int or not 0 < limit <= MAX_MEASUREMENT_FILE_BYTES:
            raise MeasurementSchemaError("measurement and chart byte bounds are 1..16 MiB")
    binding = spec.catalog
    if not isinstance(binding, LocalCatalogBinding):
        raise MeasurementSchemaError("a v4 schema carries its local catalog binding")
    if binding.result_schema_id == "schema_fragment_measurement":
        raise MeasurementSchemaError("catalog result schema ID is reserved")
    # The catalog's own identifier rules, checked now: a binding that failed
    # them at import time would leave a catalog row without its explorer view.
    from pydantic import TypeAdapter

    from evidence_inspector.compatibility import ResultSchemaReference, SemanticsId
    from evidence_inspector.result_view import AccessibleLabel

    try:
        ResultSchemaReference(
            schema_id=binding.result_schema_id, version=binding.result_schema_version
        )
        semantics = TypeAdapter(SemanticsId)
        for value in (
            binding.normalization_semantics_id,
            binding.coordinate_semantics_id,
            binding.denominator_semantics_id,
        ):
            semantics.validate_python(value)
        TypeAdapter(AccessibleLabel).validate_python(binding.accessible_label)
        # The explorer's public-text boundary, which persistence applies to the
        # artifact these strings land in.
        from .web.contracts import validate_public_projection

        validate_public_projection(
            {
                "accessible_label": binding.accessible_label,
                "result_schema_id": binding.result_schema_id,
                "result_schema_version": binding.result_schema_version,
                "normalization_semantics_id": binding.normalization_semantics_id,
                "coordinate_semantics_id": binding.coordinate_semantics_id,
                "denominator_semantics_id": binding.denominator_semantics_id,
            }
        )
    except ValueError as exc:
        raise MeasurementSchemaError("catalog binding identifiers are malformed") from exc
    if not callable(binding.authority) or not callable(binding.denominator):
        raise MeasurementSchemaError("catalog binding needs authority and denominator")
    if binding.method_slug is not None:
        from .local_authority import validate_method_slug

        try:
            validate_method_slug(binding.method_slug)
        except (TypeError, ValueError) as exc:
            raise MeasurementSchemaError("catalog binding method slug is malformed") from exc
    if spec.record_view is not None:
        _validate_record_view(spec.record_view)


def _validate_record_view(view: RecordViewBinding) -> None:
    if not isinstance(view, RecordViewBinding):
        raise MeasurementSchemaError("a record view is a RecordViewBinding")
    if view.analysis not in ANALYSES or view.analysis == "fragment":
        raise MeasurementSchemaError("a v4 record view names a non-fragment analysis")
    if (
        type(view.view_schema_version) is not str
        or not _VIEW_SCHEMA_VERSION.fullmatch(view.view_schema_version)
        or view.view_schema_version == FRAGMENT_VIEW_SCHEMA
    ):
        raise MeasurementSchemaError("record view schema version is malformed or reserved")
    from .web.contracts import validate_public_text

    try:
        if type(view.key_count_unit) is not str or not 0 < len(view.key_count_unit) <= 64:
            raise ValueError("key count unit length")
        validate_public_text(view.key_count_unit)
    except ValueError as exc:
        raise MeasurementSchemaError("record view key count unit is not public text") from exc
    if not callable(view.key_count) or not callable(view.build_body):
        raise MeasurementSchemaError("a record view needs key_count and build_body")


_REGISTRY: dict[str, BundleMeasurementSchema] = {}


def register_measurement_schema(spec: BundleMeasurementSchema) -> BundleMeasurementSchema:
    """Register one v4 measurement schema; idempotent for the identical spec."""

    _validate(spec)
    existing = _REGISTRY.get(spec.schema_version)
    if existing is not None:
        if existing is spec:
            return spec
        raise MeasurementSchemaError("measurement schema is already registered")
    for other in _REGISTRY.values():
        if other.path_stem == spec.path_stem:
            raise MeasurementSchemaError("measurement path stem is already registered")
        if other.catalog.result_schema_id == spec.catalog.result_schema_id:
            raise MeasurementSchemaError("catalog result schema ID is already registered")
        if (
            other.record_view is not None
            and spec.record_view is not None
            and other.record_view.view_schema_version == spec.record_view.view_schema_version
        ):
            raise MeasurementSchemaError("record view schema version is already registered")
    _REGISTRY[spec.schema_version] = spec
    return spec


def registered_measurement_schemas() -> Mapping[str, BundleMeasurementSchema]:
    """A read-only view of every registered v4 schema, keyed by schema version."""

    return MappingProxyType(dict(sorted(_REGISTRY.items())))


def measurement_schema(schema_version: object) -> BundleMeasurementSchema | None:
    """The registered v4 schema for ``schema_version``, or ``None``."""

    if type(schema_version) is not str:
        return None
    return _REGISTRY.get(schema_version)


def measurement_schema_for_path(relative: str) -> BundleMeasurementSchema | None:
    """The registered schema whose measurement or chart lives at ``relative``."""

    for spec in _REGISTRY.values():
        if relative in (spec.measurement_path, spec.chart_path):
            return spec
    return None


def schema_version_of(value: object) -> object:
    """The ``schema_version`` a measurement model or mapping names (unvalidated)."""

    if isinstance(value, BaseModel):
        return getattr(value, "schema_version", None)
    if isinstance(value, Mapping):
        return value.get("schema_version")
    return None


__all__ = [
    "ANALYSES",
    "FRAGMENT_PATH_STEM",
    "FRAGMENT_VIEW_SCHEMA",
    "MAX_MEASUREMENT_FILE_BYTES",
    "PATH_STEM",
    "BundleMeasurementSchema",
    "LocalCatalogBinding",
    "MeasurementSchemaError",
    "RecordViewBinding",
    "measurement_schema",
    "measurement_schema_for_path",
    "register_measurement_schema",
    "registered_measurement_schemas",
    "schema_version_of",
]
