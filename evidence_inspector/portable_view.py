"""Closed E13 accessible-table and portable local-view contracts.

This module adapts already validated E02/E05-E10 contracts into deterministic,
framework-independent exact-value rows.  It performs no rendering, network
access, scientific computation, or clinical interpretation.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import unquote

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from traceback_runner.contracts import ResultBundleManifestV2
from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes

from .cell_origin_explorer import CellOriginExplorerArtifact, ExplorerStatus
from .cna_explorer import CnaExplorerSnapshot, CnaSource, ExplorerAvailability
from .compatibility import CompatibilityOutcome, TrustState
from .fragment_explorer import (
    ExplorerSourceState,
    FragmentExplorerView,
    PanelId,
)
from .provenance_drawer import ProvenanceDrawer
from .result_view import (
    NormalizedResultFilters,
    ResultViewFixture,
    ViewSurfaceState,
    result_filters_sha256,
)

VIEW_PATH = "view.json"
TABLE_PATH = "accessible-table.tsv"
MANIFEST_PATH = "manifest.json"
_FILES = (MANIFEST_PATH, TABLE_PATH, VIEW_PATH)
_CONTENT_FILES = (TABLE_PATH, VIEW_PATH)
MAX_SOURCE_IDENTITIES = 128
MAX_TABLES = 32
MAX_ROWS = 500_000
MAX_CELLS = 32
MAX_LIMITATIONS = 128
MAX_TABLE_BYTES = 64 * 1024 * 1024
MAX_VIEW_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_TOTAL_BYTES = MAX_TABLE_BYTES + MAX_VIEW_BYTES + MAX_MANIFEST_BYTES
SEALED_FILE_MODE = 0o400
SEALED_DIRECTORY_MODE = 0o500

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

_PRIVATE_IDENTIFIER = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:"
    r"(?:donor|patient|read|sample|query)[_-]?id\s*[:=._-]\s*[^\s]+"
    r"|(?:donor|patient|sample)\s*[:=]\s*[^\s]+"
    r")",
    re.IGNORECASE,
)
_BASE64_TOKEN = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def _decoded_private_identifier(value: str) -> bool:
    """Detect private identifiers through bounded URL/base64 nesting."""

    pending = [value]
    seen: set[str] = set()
    for _ in range(5):
        next_round: list[str] = []
        for candidate in pending:
            if candidate in seen:
                continue
            seen.add(candidate)
            if _PRIVATE_IDENTIFIER.search(candidate):
                return True
            decoded_url = unquote(candidate)
            if decoded_url != candidate:
                next_round.append(decoded_url)
            if len(candidate) >= 8 and _BASE64_TOKEN.fullmatch(candidate):
                padded = candidate + "=" * (-len(candidate) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    pass
                else:
                    if decoded and all(char.isprintable() for char in decoded):
                        next_round.append(decoded)
        pending = next_round
        if not pending:
            break
    return False


def _safe_token(value: str) -> str:
    if "/" in value or "\\" in value or "://" in value:
        raise ValueError("controlled token cannot contain a path or URI")
    if _decoded_private_identifier(value):
        raise ValueError("controlled token cannot contain a raw identifier")
    if re.search(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", value, re.IGNORECASE):
        raise ValueError("controlled token cannot contain sequence-like text")
    return value


ControlledToken = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
    AfterValidator(_safe_token),
]
VersionToken = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
    AfterValidator(_safe_token),
]


class PortableViewError(ValueError):
    """Base class for E13 contract and publication failures."""


class PortableViewContractError(PortableViewError):
    """A source or derived artifact violates the E13 closed contract."""


class PortableViewConflictError(PortableViewError):
    """A publication lock or destination already exists."""


class PortableViewStorageError(PortableViewError):
    """Local storage could not durably publish the artifact."""


class PortableViewPermissionError(PortableViewError):
    """Local permissions prevent safe publication."""


class PortableViewTamperError(PortableViewError):
    """Source identity, artifact bytes, or filesystem identity changed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
        strict=True,
    )


class MeasurementKind(StrEnum):
    FRAGMENT = "fragment"
    CELL_ORIGIN = "cell_origin"
    CNA_DOSAGE = "cna_dosage"
    CNA_SEGMENTED = "cna_segmented"


class PortableSurfaceState(StrEnum):
    LOADING = "loading"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"
    SUCCESS = "success"
    STALE = "stale"
    REVOKED = "revoked"


class ExactValueState(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    WITHHELD = "withheld"


class DeltaState(StrEnum):
    AVAILABLE = "available"
    NOT_REQUESTED = "not_requested"
    NOT_ALLOWED_INCOMPATIBLE = "not_allowed_incompatible"
    WITHHELD_STATE = "withheld_state"


class PortableSourceIdentity(_ClosedModel):
    measurement_kind: MeasurementKind
    source_id: ControlledToken
    bundle_id: ControlledToken
    bundle_sha256: Sha256
    bundle_manifest_sha256: Sha256
    result_id: ControlledToken
    result_sha256: Sha256
    method_id: ControlledToken
    method_version: VersionToken
    method_definition_sha256: Sha256
    asset_sha256s: tuple[Sha256, ...] = Field(min_length=1, max_length=256)
    capability_sha256: Sha256
    compatibility_decision_sha256: Sha256
    compatibility_policy_sha256: Sha256
    compatibility_authority_head_sha256: Sha256
    compatibility_outcome: CompatibilityOutcome
    trust_state: TrustState
    source_contract_sha256: Sha256

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.measurement_kind.value,
            self.result_id,
            self.method_id,
            self.method_version,
        )

    @model_validator(mode="after")
    def canonical_assets(self) -> PortableSourceIdentity:
        if self.asset_sha256s != tuple(sorted(set(self.asset_sha256s))):
            raise ValueError("source asset digests must be uniquely sorted")
        return self


class PortableTrustContext(_ClosedModel):
    """Independent expected identities supplied by a trusted local caller."""

    schema_version: Literal["traceback.portable-trust-context.v1"] = (
        "traceback.portable-trust-context.v1"
    )
    expected_source_identities: tuple[PortableSourceIdentity, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )
    expected_bundle_manifest_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )

    @model_validator(mode="after")
    def canonical_expectations(self) -> PortableTrustContext:
        keys = [item.sort_key for item in self.expected_source_identities]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("trusted source identities must be uniquely sorted")
        if self.expected_bundle_manifest_sha256s != tuple(
            sorted(set(self.expected_bundle_manifest_sha256s))
        ):
            raise ValueError("trusted manifest digests must be uniquely sorted")
        if {
            item.bundle_manifest_sha256 for item in self.expected_source_identities
        } != set(self.expected_bundle_manifest_sha256s):
            raise ValueError("trusted source and manifest identities must be exact")
        return self


class PortableVersions(_ClosedModel):
    view_schema: Literal["traceback.portable-local-view.v1"] = (
        "traceback.portable-local-view.v1"
    )
    filter_schema: Literal["traceback.normalized-result-filters.v1"] = (
        "traceback.normalized-result-filters.v1"
    )
    plot_data_schemas: tuple[
        Literal[
            "traceback.fragment-explorer-view.v1",
            "traceback.cell-origin-explorer-view.v1",
            "traceback.cna-explorer-chart.v1",
        ],
        ...,
    ] = (
        "traceback.cell-origin-explorer-view.v1",
        "traceback.cna-explorer-chart.v1",
        "traceback.fragment-explorer-view.v1",
    )
    plot_spec_schemas: tuple[
        Literal[
            "traceback.fragment-explorer-spec.v1",
            "traceback.cell-origin-explorer-spec.v1",
            "traceback.cna-explorer-spec.v1",
        ],
        ...,
    ] = (
        "traceback.cell-origin-explorer-spec.v1",
        "traceback.cna-explorer-spec.v1",
        "traceback.fragment-explorer-spec.v1",
    )
    table_schema: Literal["traceback.portable-exact-table.v1"] = (
        "traceback.portable-exact-table.v1"
    )
    provenance_schema: Literal["traceback.provenance-drawer.v1"] = (
        "traceback.provenance-drawer.v1"
    )
    bundle_schema: Literal["traceback.result-bundle.v2"] = "traceback.result-bundle.v2"

    @model_validator(mode="after")
    def canonical_versions(self) -> PortableVersions:
        required_data = (
            "traceback.cell-origin-explorer-view.v1",
            "traceback.cna-explorer-chart.v1",
            "traceback.fragment-explorer-view.v1",
        )
        required_specs = (
            "traceback.cell-origin-explorer-spec.v1",
            "traceback.cna-explorer-spec.v1",
            "traceback.fragment-explorer-spec.v1",
        )
        if self.plot_data_schemas != required_data:
            raise ValueError("plot-data versions must be the complete required set")
        if self.plot_spec_schemas != required_specs:
            raise ValueError("plot-spec versions must be the complete required set")
        return self


class AccessibilityMetadata(_ClosedModel):
    schema_version: Literal["traceback.portable-accessibility.v1"] = (
        "traceback.portable-accessibility.v1"
    )
    document_language: Literal["en"] = "en"
    table_header_association: Literal["explicit_column_headers"] = (
        "explicit_column_headers"
    )
    keyboard_navigation: Literal["native_table_navigation"] = "native_table_navigation"
    focus_order: tuple[
        Literal["status"],
        Literal["filters"],
        Literal["tables"],
        Literal["provenance"],
    ] = (
        "status",
        "filters",
        "tables",
        "provenance",
    )
    status_announcements: Literal["polite_live_region"] = "polite_live_region"
    zoom_percent_supported: Literal[200] = 200
    reflow_without_two_dimensional_page_scroll: Literal[True] = True
    color_only_encoding: Literal[False] = False
    hover_required: Literal[False] = False
    keyboard_trap_present: Literal[False] = False


class ExactCell(_ClosedModel):
    column_id: ControlledToken
    value_state: ExactValueState
    integer_value: int | None = None
    decimal_value: float | None = Field(default=None, allow_inf_nan=False)
    token_value: ControlledToken | None = None
    sha256_value: Sha256 | None = None
    boolean_value: bool | None = None
    numerator: int | None = None
    denominator: int | None = Field(default=None, gt=0)
    unit_id: ControlledToken

    @model_validator(mode="after")
    def exactly_one_value(self) -> ExactCell:
        values = (
            self.integer_value,
            self.decimal_value,
            self.token_value,
            self.sha256_value,
            self.boolean_value,
        )
        present = sum(value is not None for value in values)
        if self.value_state == ExactValueState.OBSERVED:
            if present != 1:
                raise ValueError("observed cell requires exactly one typed value")
        elif present or self.numerator is not None or self.denominator is not None:
            raise ValueError("missing or withheld cell cannot contain a value")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("exact fraction requires numerator and denominator")
        return self


class ExactTableRow(_ClosedModel):
    row_index: int = Field(ge=0, lt=MAX_ROWS)
    row_key: ControlledToken
    cells: tuple[ExactCell, ...] = Field(min_length=1, max_length=MAX_CELLS)

    @model_validator(mode="after")
    def canonical_cells(self) -> ExactTableRow:
        columns = [item.column_id for item in self.cells]
        if columns != sorted(columns) or len(columns) != len(set(columns)):
            raise ValueError("exact cells must be uniquely sorted by column")
        return self


class ExactMeasurementTable(_ClosedModel):
    schema_version: Literal["traceback.portable-exact-table.v1"] = (
        "traceback.portable-exact-table.v1"
    )
    table_id: ControlledToken
    measurement_kind: MeasurementKind
    source_contract_sha256: Sha256
    source_state: PortableSurfaceState
    rows: tuple[ExactTableRow, ...] = Field(max_length=MAX_ROWS)
    denominator_sha256s: tuple[Sha256, ...] = Field(max_length=128)
    limitation_ids: tuple[ControlledToken, ...] = Field(max_length=MAX_LIMITATIONS)

    @model_validator(mode="after")
    def canonical_table(self) -> ExactMeasurementTable:
        if [item.row_index for item in self.rows] != list(range(len(self.rows))):
            raise ValueError("exact table row indexes must be contiguous")
        row_keys = [item.row_key for item in self.rows]
        if len(row_keys) != len(set(row_keys)):
            raise ValueError("exact table row keys must be unique")
        if self.denominator_sha256s != tuple(sorted(set(self.denominator_sha256s))):
            raise ValueError("denominator digests must be uniquely sorted")
        if self.limitation_ids != tuple(sorted(set(self.limitation_ids))):
            raise ValueError("limitation IDs must be uniquely sorted")
        numerical = self.source_state in {
            PortableSurfaceState.SUCCESS,
            PortableSurfaceState.PARTIAL,
        }
        if not numerical and self.rows:
            raise ValueError("unavailable table state cannot expose exact values")
        return self


class CompatibilitySummary(_ClosedModel):
    decision_sha256s: tuple[Sha256, ...] = Field(max_length=MAX_SOURCE_IDENTITIES)
    policy_sha256s: tuple[Sha256, ...] = Field(max_length=MAX_SOURCE_IDENTITIES)
    authority_head_sha256s: tuple[Sha256, ...] = Field(max_length=MAX_SOURCE_IDENTITIES)
    outcomes: tuple[CompatibilityOutcome, ...] = Field(max_length=8)
    delta_state: DeltaState

    @model_validator(mode="after")
    def canonical_compatibility(self) -> CompatibilitySummary:
        for values in (
            self.decision_sha256s,
            self.policy_sha256s,
            self.authority_head_sha256s,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("compatibility digests must be uniquely sorted")
        if self.outcomes != tuple(sorted(set(self.outcomes), key=str)):
            raise ValueError("compatibility outcomes must be uniquely sorted")
        incompatible = any(
            item != CompatibilityOutcome.COMPARABLE for item in self.outcomes
        )
        if incompatible and self.delta_state != DeltaState.NOT_ALLOWED_INCOMPATIBLE:
            raise ValueError("incompatible inputs must prohibit deltas")
        if not incompatible and self.delta_state == DeltaState.NOT_ALLOWED_INCOMPATIBLE:
            raise ValueError("comparable inputs cannot prohibit deltas as incompatible")
        return self


class PortableViewBuildRequest(_ClosedModel):
    schema_version: Literal["traceback.portable-view-request.v1"] = (
        "traceback.portable-view-request.v1"
    )
    view_id: ControlledToken
    surface_fixture: ResultViewFixture
    filters: NormalizedResultFilters
    fragment_view: FragmentExplorerView | None = None
    cell_origin_artifact: CellOriginExplorerArtifact | None = None
    cna_snapshot: CnaExplorerSnapshot | None = None
    provenance_drawer: ProvenanceDrawer | None = None
    source_identities: tuple[PortableSourceIdentity, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )
    bundle_manifests: tuple[ResultBundleManifestV2, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )

    @model_validator(mode="after")
    def closed_inputs(self) -> PortableViewBuildRequest:
        keys = [item.sort_key for item in self.source_identities]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("source identities must be uniquely sorted")
        manifest_digests = tuple(_digest(item) for item in self.bundle_manifests)
        if manifest_digests != tuple(sorted(set(manifest_digests))):
            raise ValueError("bundle manifests must be uniquely digest-sorted")
        if {item.bundle_manifest_sha256 for item in self.source_identities} - set(
            manifest_digests
        ):
            raise ValueError("source identity has no exact E02 bundle manifest")
        for identity in self.source_identities:
            manifest = next(
                item
                for item in self.bundle_manifests
                if _digest(item) == identity.bundle_manifest_sha256
            )
            if (
                manifest.method.method_id != identity.method_id
                or manifest.method.version != identity.method_version
                or manifest.method.method_definition_sha256
                != identity.method_definition_sha256
            ):
                raise ValueError("bundle method does not match source identity")
        if self.surface_fixture.view is not None:
            if self.surface_fixture.view.normalized_filters != self.filters:
                raise ValueError("portable filters differ from E06 normalized filters")
            bound_results = {item.result_id: item for item in self.source_identities}
            for row in self.surface_fixture.view.rows:
                identity = bound_results.get(row.result_identity.result_id)
                if (
                    identity is None
                    or identity.result_sha256 != row.result_identity.result_sha256
                    or identity.bundle_id != row.result_identity.bundle_id
                    or identity.bundle_sha256 != row.result_identity.bundle_sha256
                    or identity.method_id != row.method_identity.method_ref.method_id
                    or identity.method_version != row.method_identity.method_ref.version
                    or identity.method_definition_sha256
                    != row.method_identity.method_definition_sha256
                    or identity.capability_sha256
                    != row.authority_identity.capability_sha256
                    or identity.compatibility_decision_sha256
                    != row.compatibility_identity.decision_sha256
                    or identity.compatibility_policy_sha256
                    != row.compatibility_identity.policy_sha256
                    or identity.compatibility_authority_head_sha256
                    != row.compatibility_identity.authority_head_sha256
                    or identity.compatibility_outcome
                    != row.compatibility_identity.outcome
                    or identity.trust_state != row.trust_state
                ):
                    raise ValueError("E06 row does not match exact portable identity")
        return self


class PortableLocalView(_ClosedModel):
    schema_version: Literal["traceback.portable-local-view.v1"] = (
        "traceback.portable-local-view.v1"
    )
    view_id: ControlledToken
    surface_state: PortableSurfaceState
    error_code: ControlledToken | None
    versions: PortableVersions
    accessibility: AccessibilityMetadata
    filters: NormalizedResultFilters
    filters_sha256: Sha256
    source_identities: tuple[PortableSourceIdentity, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )
    bundle_manifest_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAX_SOURCE_IDENTITIES
    )
    compatibility: CompatibilitySummary
    provenance_drawer_sha256: Sha256 | None
    tables: tuple[ExactMeasurementTable, ...] = Field(max_length=MAX_TABLES)
    accessible_table_sha256: Sha256
    request_sha256: Sha256
    trust_context_sha256: Sha256
    view_sha256: Sha256
    synthetic_local_only: Literal[True] = True
    product_release_authorized: Literal[False] = False
    diagnostic_interpretation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def exact_view(self) -> PortableLocalView:
        if self.filters_sha256 != result_filters_sha256(self.filters):
            raise ValueError("portable filter digest does not match filters")
        source_keys = [item.sort_key for item in self.source_identities]
        if source_keys != sorted(source_keys) or len(source_keys) != len(
            set(source_keys)
        ):
            raise ValueError("portable sources must be uniquely sorted")
        if self.bundle_manifest_sha256s != tuple(
            sorted(set(self.bundle_manifest_sha256s))
        ):
            raise ValueError("bundle manifest digests must be uniquely sorted")
        table_keys = [
            (item.measurement_kind.value, item.table_id) for item in self.tables
        ]
        if table_keys != sorted(table_keys) or len(table_keys) != len(set(table_keys)):
            raise ValueError("portable exact tables must be uniquely sorted")
        if self.surface_state not in {
            PortableSurfaceState.SUCCESS,
            PortableSurfaceState.PARTIAL,
        } and any(item.rows for item in self.tables):
            raise ValueError("unavailable portable state cannot expose exact values")
        if self.surface_state == PortableSurfaceState.ERROR:
            if self.error_code is None:
                raise ValueError("error state requires a typed error code")
        elif self.error_code is not None:
            raise ValueError("only error state may contain an error code")
        expected_sha256 = _digest(self, exclude={"view_sha256"})
        if self.view_sha256 != expected_sha256:
            raise ValueError("portable view digest does not match canonical content")
        _validate_private_strings(self.model_dump(mode="json"))
        return self


class PortableArtifactFile(_ClosedModel):
    relative_path: Literal["accessible-table.tsv", "view.json"]
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    media_type: Literal[
        "application/json",
        "text/tab-separated-values; charset=utf-8",
    ]


class PortableViewManifest(_ClosedModel):
    schema_version: Literal["traceback.portable-view-manifest.v1"] = (
        "traceback.portable-view-manifest.v1"
    )
    artifact_id: Annotated[
        str, StringConstraints(pattern=r"^portable-view-[0-9a-f]{24}$")
    ]
    files: tuple[PortableArtifactFile, ...] = Field(min_length=2, max_length=2)
    source_identity_sha256: Sha256
    accessible_table_sha256: Sha256
    view_sha256: Sha256

    @model_validator(mode="after")
    def exact_manifest(self) -> PortableViewManifest:
        if tuple(item.relative_path for item in self.files) != _CONTENT_FILES:
            raise ValueError("portable manifest file inventory is not exact")
        files = {item.relative_path: item for item in self.files}
        if (
            files[TABLE_PATH].sha256 != self.accessible_table_sha256
            or files[VIEW_PATH].sha256 != self.view_sha256
        ):
            raise ValueError("portable manifest bindings disagree with files")
        expected_id = f"portable-view-{_digest({'source': self.source_identity_sha256, 'table': self.accessible_table_sha256, 'view': self.view_sha256})[:24]}"
        if self.artifact_id != expected_id:
            raise ValueError("portable artifact ID does not match exact bindings")
        return self


class VerifiedPortableView(_ClosedModel):
    manifest: PortableViewManifest
    view: PortableLocalView
    accessible_table_bytes: bytes


def _digest(value: Any, *, exclude: set[str] | None = None) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude=exclude or set())
    elif isinstance(value, dict):
        value = {key: _jsonable(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        value = [_jsonable(item) for item in value]
    return sha256_bytes(canonical_json_bytes(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _validate_private_strings(value: Any, *, field_name: str = "") -> None:
    forbidden_fields = {
        "alias",
        "aliases",
        "donor_id",
        "patient_id",
        "read_id",
        "sample_id",
        "sequence",
        "path",
        "local_path",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in forbidden_fields:
                raise ValueError(f"privacy-forbidden field: {key}")
            _validate_private_strings(item, field_name=key)
    elif isinstance(value, list):
        for item in value:
            _validate_private_strings(item, field_name=field_name)
    elif isinstance(value, str):
        digest_slot = field_name.endswith(("sha256", "sha256s", "sha256_value"))
        if not digest_slot:
            _safe_token(value)


def _cell(
    column_id: str,
    value: float | str | bool | None,
    unit_id: str,
    *,
    numerator: int | None = None,
    denominator: int | None = None,
    withheld: bool = False,
) -> ExactCell:
    state = (
        ExactValueState.WITHHELD
        if withheld
        else ExactValueState.MISSING
        if value is None
        else ExactValueState.OBSERVED
    )
    values: dict[str, Any] = {}
    if isinstance(value, bool):
        values["boolean_value"] = value
    elif isinstance(value, int):
        values["integer_value"] = value
    elif isinstance(value, float):
        values["decimal_value"] = value
    elif isinstance(value, str) and unit_id == "sha256":
        values["sha256_value"] = value
    elif isinstance(value, str):
        values["token_value"] = value
    return ExactCell(
        column_id=column_id,
        value_state=state,
        unit_id=unit_id,
        numerator=numerator,
        denominator=denominator,
        **values,
    )


def _row(index: int, key: str, cells: Iterable[ExactCell]) -> ExactTableRow:
    return ExactTableRow(
        row_index=index,
        row_key=key,
        cells=tuple(sorted(cells, key=lambda item: item.column_id)),
    )


def _identity_for_result(
    identities: tuple[PortableSourceIdentity, ...], result_id: str
) -> PortableSourceIdentity:
    matches = [item for item in identities if item.result_id == result_id]
    if len(matches) != 1:
        raise PortableViewContractError(
            "source result identity is missing or ambiguous"
        )
    return matches[0]


def _identity_for_kind(
    identities: tuple[PortableSourceIdentity, ...], kind: MeasurementKind
) -> PortableSourceIdentity:
    matches = [item for item in identities if item.measurement_kind == kind]
    if len(matches) != 1:
        raise PortableViewContractError("measurement identity is missing or ambiguous")
    return matches[0]


def _fragment_table(
    view: FragmentExplorerView,
    identities: tuple[PortableSourceIdentity, ...],
) -> ExactMeasurementTable:
    rows = []
    for index, item in enumerate(view.accessible_rows):
        selection = view.state.left if item.panel == PanelId.A else view.state.right
        identity = _identity_for_result(identities, selection.result_id)
        rows.append(
            _row(
                index,
                f"fragment_{item.panel.value}_{index}",
                (
                    _cell("bundle_digest", identity.bundle_sha256, "sha256"),
                    _cell("count", item.count, "records"),
                    _cell(
                        "fraction",
                        item.fraction_numerator / item.fraction_denominator,
                        "fraction",
                        numerator=item.fraction_numerator,
                        denominator=item.fraction_denominator,
                    ),
                    _cell("lower_inclusive", item.lower_inclusive, "base_pairs"),
                    _cell("panel", item.panel.value, "category"),
                    _cell("upper_exclusive", item.upper_exclusive, "base_pairs"),
                ),
            )
        )
    first = _identity_for_result(identities, view.state.left.result_id)
    denominators = tuple(
        sorted(
            {
                _digest(panel.denominator)
                for panel in (view.left, view.right)
                if panel.denominator is not None
            }
        )
    )
    limitations = tuple(
        sorted(
            {
                f"withheld_{panel.withholding_code.value}"
                for panel in (view.left, view.right)
                if panel.withholding_code is not None
            }
        )
    )
    return ExactMeasurementTable(
        table_id="table_fragment_histogram",
        measurement_kind=MeasurementKind.FRAGMENT,
        source_contract_sha256=first.source_contract_sha256,
        source_state=(
            PortableSurfaceState.SUCCESS if rows else PortableSurfaceState.PARTIAL
        ),
        rows=tuple(rows),
        denominator_sha256s=denominators,
        limitation_ids=limitations,
    )


def _cell_origin_table(
    artifact: CellOriginExplorerArtifact,
    identity: PortableSourceIdentity,
) -> ExactMeasurementTable:
    view = artifact.view
    rows = tuple(
        _row(
            index,
            f"cell_origin_{index}",
            (
                _cell("contributor_id", item.contributor_id, "category"),
                _cell("estimate_fraction", item.estimate_fraction, "fraction"),
                _cell("lower_fraction", item.lower_fraction, "fraction"),
                _cell("uncertainty_status", str(item.uncertainty_status), "category"),
                _cell("upper_fraction", item.upper_fraction, "fraction"),
            ),
        )
        for index, item in enumerate(view.exact_table_rows)
    )
    denominator_values = tuple(
        item
        for item in (
            view.marker_support,
            view.fragment_denominators,
            view.atlas_coverage,
        )
        if item is not None
    )
    return ExactMeasurementTable(
        table_id="table_cell_origin_estimates",
        measurement_kind=MeasurementKind.CELL_ORIGIN,
        source_contract_sha256=identity.source_contract_sha256,
        source_state=(
            PortableSurfaceState.SUCCESS
            if view.status == ExplorerStatus.READY
            else PortableSurfaceState.PARTIAL
        ),
        rows=rows,
        denominator_sha256s=tuple(sorted(_digest(item) for item in denominator_values)),
        limitation_ids=tuple(sorted(item.value for item in view.limitations)),
    )


def _cna_tables(
    snapshot: CnaExplorerSnapshot,
    identities: tuple[PortableSourceIdentity, ...],
) -> tuple[ExactMeasurementTable, ...]:
    if (
        snapshot.availability != ExplorerAvailability.AVAILABLE
        or snapshot.layers is None
    ):
        return ()
    dosage_identity = _identity_for_kind(identities, MeasurementKind.CNA_DOSAGE)
    segmented_identity = _identity_for_kind(identities, MeasurementKind.CNA_SEGMENTED)
    by_source = {
        CnaSource.DOSAGE_QC: (MeasurementKind.CNA_DOSAGE, dosage_identity),
        CnaSource.SEGMENTED_CNA: (MeasurementKind.CNA_SEGMENTED, segmented_identity),
    }
    limitation_ids = tuple(
        sorted(f"limitation_{_digest(item)[:24]}" for item in snapshot.limitations)
    )

    def table(
        table_id: str,
        source: CnaSource,
        rows: tuple[ExactTableRow, ...],
    ) -> ExactMeasurementTable:
        kind, identity = by_source[source]
        return ExactMeasurementTable(
            table_id=table_id,
            measurement_kind=kind,
            source_contract_sha256=identity.source_contract_sha256,
            source_state=PortableSurfaceState.SUCCESS,
            rows=rows,
            denominator_sha256s=(),
            limitation_ids=limitation_ids,
        )

    dosage_rows = tuple(
        _row(
            index,
            f"cna_dosage_{index}",
            (
                _cell("accepted_count", item.accepted_read_count, "records"),
                _cell("chromosome", item.chromosome, "category"),
                _cell("direction", item.dosage_direction, "category"),
                _cell("log2_ratio", item.log2_ratio, "log2_ratio"),
                _cell("ordinal", item.ordinal, "ordinal"),
                _cell(
                    "relative_diploid_dosage",
                    item.relative_diploid_dosage,
                    "relative_dosage",
                ),
                _cell("source", item.source.value, "category"),
            ),
        )
        for index, item in enumerate(snapshot.layers.dosage_chromosomes)
    )
    segment_rows = tuple(
        _row(
            index,
            f"cna_segment_{index}",
            (
                _cell("call", item.upstream_call, "category"),
                _cell("contig", item.contig, "category"),
                _cell("copy_number", item.upstream_copy_number, "copies"),
                _cell("end", item.end, "base_pairs"),
                _cell("median_log2", item.median_log2, "log2_ratio"),
                _cell("native_span_bins", item.native_span_bin_count, "bins"),
                _cell("retained_bins", item.retained_bin_count, "bins"),
                _cell("segment_index", item.segment_index, "ordinal"),
                _cell("source", item.source.value, "category"),
                _cell("start", item.start, "base_pairs"),
                _cell("subclone_status", item.subclone_status, "boolean"),
            ),
        )
        for index, item in enumerate(snapshot.layers.segments)
    )
    candidate_rows = tuple(
        _row(
            index,
            f"cna_candidate_{index}",
            (
                _cell("bic", item.bic, "score"),
                _cell("candidate_index", item.candidate_index, "ordinal"),
                _cell("candidate_id", item.candidate_id, "category"),
                _cell(
                    "estimated_normal_fraction",
                    item.estimated_normal_fraction,
                    "fraction",
                ),
                _cell("estimated_ploidy", item.estimated_ploidy, "copies"),
                _cell(
                    "fraction_cna_subclonal",
                    item.fraction_cna_subclonal,
                    "fraction",
                ),
                _cell(
                    "fraction_genome_subclonal",
                    item.fraction_genome_subclonal,
                    "fraction",
                ),
                _cell(
                    "initial_normal_fraction",
                    item.initial_normal_fraction,
                    "fraction",
                ),
                _cell("initial_ploidy", item.initial_ploidy, "copies"),
                _cell("log_likelihood", item.log_likelihood, "score"),
                _cell("model_fraction", item.upstream_model_fraction, "fraction"),
                _cell("selected", item.selected, "boolean"),
                _cell("source", item.source.value, "category"),
            ),
        )
        for index, item in enumerate(snapshot.layers.candidates)
    )
    corrected_rows = tuple(
        _row(
            index,
            f"cna_corrected_{index}",
            (
                _cell("bin_index", item.bin_index, "ordinal"),
                _cell("contig", item.contig, "category"),
                _cell("corrected_log2", item.corrected_log2, "log2_ratio"),
                _cell("end", item.end, "base_pairs"),
                _cell("source", item.source.value, "category"),
                _cell("start", item.start, "base_pairs"),
                _cell("value_state", item.value_state, "category"),
            ),
        )
        for index, item in enumerate(snapshot.layers.corrected_depth)
    )
    tables = [
        table("table_cna_dosage_chromosomes", CnaSource.DOSAGE_QC, dosage_rows),
        table("table_cna_corrected_depth", CnaSource.SEGMENTED_CNA, corrected_rows),
        table("table_cna_model_candidates", CnaSource.SEGMENTED_CNA, candidate_rows),
        table("table_cna_segments", CnaSource.SEGMENTED_CNA, segment_rows),
    ]
    for source in CnaSource:
        source_name = source.value
        bin_rows = tuple(
            _row(
                index,
                f"cna_bin_{source_name}_{index}",
                (
                    _cell(
                        "accepted_read_start_count",
                        item.accepted_read_start_count,
                        "records",
                    ),
                    _cell("bin_index", item.bin_index, "ordinal"),
                    _cell("contig", item.contig, "category"),
                    _cell("corrected_log2", item.corrected_log2, "log2_ratio"),
                    _cell("end", item.end, "base_pairs"),
                    _cell("source", item.source.value, "category"),
                    _cell("start", item.start, "base_pairs"),
                    _cell("status", item.status, "category"),
                ),
            )
            for index, item in enumerate(
                value for value in snapshot.layers.bins if value.source == source
            )
        )
        tables.append(table(f"table_cna_bins_{source_name}", source, bin_rows))

        grid = next(
            item for item in snapshot.layers.coordinate_grids if item.source == source
        )
        grid_rows = tuple(
            _row(
                index,
                f"cna_grid_{source_name}_{index}",
                (
                    _cell("bin_count", grid.bin_count, "bins"),
                    _cell(
                        "bin_definition_sha256",
                        grid.bin_definition_sha256,
                        "sha256",
                    ),
                    _cell("contig", contig, "category"),
                    _cell("contig_ordinal", index, "ordinal"),
                    _cell("coordinate_system", grid.coordinate_system, "category"),
                    _cell("source", grid.source.value, "category"),
                ),
            )
            for index, contig in enumerate(grid.contig_order)
        )
        tables.append(
            table(f"table_cna_coordinate_grid_{source_name}", source, grid_rows)
        )

        asset_rows = tuple(
            _row(
                index,
                f"cna_asset_{source_name}_{index}",
                (
                    _cell("content_sha256", item.content_sha256, "sha256"),
                    _cell("role", item.role, "category"),
                    _cell("source", item.source.value, "category"),
                ),
            )
            for index, item in enumerate(
                value for value in snapshot.layers.assets if value.source == source
            )
        )
        tables.append(table(f"table_cna_assets_{source_name}", source, asset_rows))

        method = next(item for item in snapshot.layers.methods if item.source == source)
        method_rows = (
            _row(
                0,
                f"cna_method_{source_name}",
                (
                    _cell(
                        "authority_execution_state",
                        method.authority_execution_state.value,
                        "category",
                    ),
                    _cell(
                        "authority_qualification_state",
                        method.authority_qualification_state.value,
                        "category",
                    ),
                    _cell(
                        "authority_trust_state",
                        method.authority_trust_state.value,
                        "category",
                    ),
                    _cell(
                        "diagnostic_interpretation_allowed",
                        method.diagnostic_interpretation_allowed,
                        "boolean",
                    ),
                    _cell(
                        "embedded_qualification_status",
                        method.embedded_qualification_status,
                        "category",
                    ),
                    _cell("method_id", method.method_id, "category"),
                    _cell(
                        "product_release_authorized",
                        method.product_release_authorized,
                        "boolean",
                    ),
                    _cell(
                        "research_inspectable", method.research_inspectable, "boolean"
                    ),
                    _cell(
                        "result_schema_version",
                        method.result_schema_version,
                        "version",
                    ),
                    _cell("source", method.source.value, "category"),
                ),
            ),
        )
        tables.append(table(f"table_cna_method_{source_name}", source, method_rows))

        insufficiency = next(
            item for item in snapshot.layers.insufficiency if item.source == source
        )
        reason_digests = tuple(_digest(item) for item in insufficiency.reasons) or (
            None,
        )
        insufficiency_rows = tuple(
            _row(
                index,
                f"cna_insufficiency_{source_name}_{index}",
                (
                    _cell(
                        "missing_values_are_zero",
                        insufficiency.missing_values_are_zero,
                        "boolean",
                    ),
                    _cell("reason_count", len(insufficiency.reasons), "records"),
                    _cell("reason_sha256", reason_sha256, "sha256"),
                    _cell("source", insufficiency.source.value, "category"),
                    _cell(
                        "tumor_or_clinical_interpretation_allowed",
                        insufficiency.tumor_or_clinical_interpretation_allowed,
                        "boolean",
                    ),
                    _cell(
                        "upstream_status",
                        insufficiency.upstream_status,
                        "category",
                    ),
                ),
            )
            for index, reason_sha256 in enumerate(reason_digests)
        )
        tables.append(
            table(
                f"table_cna_insufficiency_{source_name}",
                source,
                insufficiency_rows,
            )
        )

    mask_rows = tuple(
        _row(
            index,
            f"cna_mask_{index}",
            (
                _cell("bin_index", item.bin_index, "ordinal"),
                _cell("contig", item.contig, "category"),
                _cell("end", item.end, "base_pairs"),
                _cell("reason", item.reason, "category"),
                _cell("source", item.source.value, "category"),
                _cell(
                    "source_artifact_sha256",
                    item.source_artifact_sha256,
                    "sha256",
                ),
                _cell("source_value", item.source_value, "log2_ratio"),
                _cell("start", item.start, "base_pairs"),
            ),
        )
        for index, item in enumerate(snapshot.layers.masks)
    )
    tables.append(table("table_cna_masks", CnaSource.SEGMENTED_CNA, mask_rows))
    return tuple(
        sorted(tables, key=lambda item: (item.measurement_kind.value, item.table_id))
    )


def _source_contract_bindings(request: PortableViewBuildRequest) -> None:
    identities = request.source_identities
    if request.fragment_view is not None:
        view = request.fragment_view
        if _digest(view) not in {item.source_contract_sha256 for item in identities}:
            raise PortableViewContractError("fragment view digest is not source-bound")
        for source in view.request.sources:
            identity = _identity_for_result(identities, source.record.result_id)
            if (
                identity.measurement_kind != MeasurementKind.FRAGMENT
                or identity.bundle_id != source.record.bundle_id
                or identity.result_sha256 != source.record.result_sha256
                or identity.bundle_sha256 != source.record.bundle_sha256
                or identity.method_id != source.record.method.method_id
                or identity.method_version != source.record.method.version
                or identity.method_definition_sha256
                != source.record.method_definition_sha256
                or identity.asset_sha256s
                != tuple(
                    sorted(item.content_sha256 for item in source.record.method.assets)
                )
                or identity.capability_sha256
                != _digest(source.record.current_capability)
                or identity.compatibility_decision_sha256
                != view.compatibility.decision_sha256
                or identity.compatibility_policy_sha256
                != view.request.trusted_policy_sha256
                or identity.compatibility_authority_head_sha256
                != view.request.trusted_authority_head_sha256
                or identity.compatibility_outcome != view.compatibility.outcome
                or identity.trust_state != source.record.trust_state
            ):
                raise PortableViewContractError("fragment source binding is inexact")
    if request.cell_origin_artifact is not None:
        artifact = request.cell_origin_artifact
        identity = _identity_for_kind(identities, MeasurementKind.CELL_ORIGIN)
        binding = artifact.view.binding
        if (
            identity.source_contract_sha256 != _digest(artifact)
            or identity.result_id != binding.result_id
            or identity.result_sha256 != binding.result_sha256
            or identity.bundle_id != binding.bundle_id
            or identity.bundle_sha256 != binding.bundle_sha256
            or identity.method_id != binding.method_id
            or identity.method_version != binding.method_version
            or identity.method_definition_sha256 != binding.method_definition_sha256
            or identity.compatibility_authority_head_sha256
            != binding.authority_head_sha256
        ):
            raise PortableViewContractError("cell-origin source binding is inexact")
    if request.cna_snapshot is not None:
        snapshot = request.cna_snapshot
        if _digest(snapshot) not in {
            item.source_contract_sha256 for item in identities
        }:
            raise PortableViewContractError("CNA snapshot digest is not source-bound")
        bindings = {
            item.source: item.result_sha256
            for item in snapshot.provenance.input_bindings
        }
        dosage = _identity_for_kind(identities, MeasurementKind.CNA_DOSAGE)
        segmented = _identity_for_kind(identities, MeasurementKind.CNA_SEGMENTED)
        if (
            dosage.result_sha256 != bindings[CnaSource.DOSAGE_QC]
            or segmented.result_sha256 != bindings[CnaSource.SEGMENTED_CNA]
        ):
            raise PortableViewContractError("CNA result binding is inexact")
        for source, identity in (
            (CnaSource.DOSAGE_QC, dosage),
            (CnaSource.SEGMENTED_CNA, segmented),
        ):
            expected_assets = (
                tuple(
                    sorted(
                        item.content_sha256
                        for item in snapshot.layers.assets
                        if item.source == source
                    )
                )
                if snapshot.layers is not None
                else ()
            )
            method = next(item for item in snapshot.methods if item.source == source)
            if (
                identity.asset_sha256s != expected_assets
                or identity.method_id != method.method_id
            ):
                raise PortableViewContractError(
                    "CNA method or asset binding is inexact"
                )
    if request.provenance_drawer is not None:
        drawer = request.provenance_drawer
        compatibility_request = drawer.replay_request.compatibility_request
        if not any(
            identity.compatibility_decision_sha256
            == drawer.compatibility_decision_sha256
            and identity.compatibility_policy_sha256
            == compatibility_request.trusted_policy_sha256
            and identity.compatibility_authority_head_sha256
            == compatibility_request.trusted_authority_head_sha256
            for identity in identities
        ):
            raise PortableViewContractError(
                "provenance drawer compatibility is not source-bound"
            )


def _trusted_bindings(
    request: PortableViewBuildRequest, trust_context: PortableTrustContext
) -> None:
    if request.source_identities != trust_context.expected_source_identities:
        raise PortableViewTamperError(
            "portable source identities differ from independent trust context"
        )
    manifest_sha256s = tuple(sorted(_digest(item) for item in request.bundle_manifests))
    if manifest_sha256s != trust_context.expected_bundle_manifest_sha256s:
        raise PortableViewTamperError(
            "portable manifests differ from independent trust context"
        )


def _derive_state(request: PortableViewBuildRequest) -> PortableSurfaceState:
    surface = request.surface_fixture.surface_state
    if surface == ViewSurfaceState.LOADING:
        return PortableSurfaceState.LOADING
    if surface == ViewSurfaceState.ERROR:
        return PortableSurfaceState.ERROR
    if surface == ViewSurfaceState.EMPTY:
        return PortableSurfaceState.EMPTY
    if any(
        item.trust_state == TrustState.REVOKED for item in request.source_identities
    ):
        return PortableSurfaceState.REVOKED
    if any(
        item.trust_state != TrustState.VERIFIED for item in request.source_identities
    ):
        return PortableSurfaceState.STALE
    states: list[bool] = []
    if request.fragment_view is not None:
        states.extend(
            panel.source_state == ExplorerSourceState.COMPLETE
            for panel in (request.fragment_view.left, request.fragment_view.right)
        )
    if request.cell_origin_artifact is not None:
        states.append(request.cell_origin_artifact.view.status == ExplorerStatus.READY)
    if request.cna_snapshot is not None:
        states.append(
            request.cna_snapshot.availability == ExplorerAvailability.AVAILABLE
        )
    expected_kinds = {item.measurement_kind for item in request.source_identities}
    present_kinds: set[MeasurementKind] = set()
    if request.fragment_view is not None:
        present_kinds.add(MeasurementKind.FRAGMENT)
    if request.cell_origin_artifact is not None:
        present_kinds.add(MeasurementKind.CELL_ORIGIN)
    if request.cna_snapshot is not None:
        present_kinds.update(
            {MeasurementKind.CNA_DOSAGE, MeasurementKind.CNA_SEGMENTED}
        )
    if expected_kinds != present_kinds or not states or not all(states):
        return PortableSurfaceState.PARTIAL
    return PortableSurfaceState.SUCCESS


def _compatibility(
    identities: tuple[PortableSourceIdentity, ...],
    fragment: FragmentExplorerView | None,
    state: PortableSurfaceState,
) -> CompatibilitySummary:
    outcomes = tuple(
        sorted({item.compatibility_outcome for item in identities}, key=str)
    )
    if any(item != CompatibilityOutcome.COMPARABLE for item in outcomes):
        delta = DeltaState.NOT_ALLOWED_INCOMPATIBLE
    elif state not in {PortableSurfaceState.SUCCESS, PortableSurfaceState.PARTIAL}:
        delta = DeltaState.WITHHELD_STATE
    elif fragment is not None and fragment.delta_rows:
        delta = DeltaState.AVAILABLE
    else:
        delta = DeltaState.NOT_REQUESTED
    return CompatibilitySummary(
        decision_sha256s=tuple(
            sorted({item.compatibility_decision_sha256 for item in identities})
        ),
        policy_sha256s=tuple(
            sorted({item.compatibility_policy_sha256 for item in identities})
        ),
        authority_head_sha256s=tuple(
            sorted({item.compatibility_authority_head_sha256 for item in identities})
        ),
        outcomes=outcomes,
        delta_state=delta,
    )


def _derive_tables(
    request: PortableViewBuildRequest, state: PortableSurfaceState
) -> tuple[ExactMeasurementTable, ...]:
    if state not in {PortableSurfaceState.SUCCESS, PortableSurfaceState.PARTIAL}:
        return ()
    tables: list[ExactMeasurementTable] = []
    if request.fragment_view is not None:
        table = _fragment_table(request.fragment_view, request.source_identities)
        if table.rows:
            tables.append(table)
    if request.cell_origin_artifact is not None:
        identity = _identity_for_kind(
            request.source_identities, MeasurementKind.CELL_ORIGIN
        )
        table = _cell_origin_table(request.cell_origin_artifact, identity)
        if table.rows:
            tables.append(table)
    if request.cna_snapshot is not None:
        tables.extend(_cna_tables(request.cna_snapshot, request.source_identities))
    return tuple(
        sorted(tables, key=lambda item: (item.measurement_kind.value, item.table_id))
    )


def build_portable_view(
    request: PortableViewBuildRequest,
    *,
    trust_context: PortableTrustContext,
) -> tuple[PortableLocalView, bytes]:
    """Build one deterministic local view and its canonical accessible TSV."""

    try:
        request = PortableViewBuildRequest.model_validate_json(
            canonical_json_bytes(request)
        )
        trust_context = PortableTrustContext.model_validate_json(
            canonical_json_bytes(trust_context)
        )
        _trusted_bindings(request, trust_context)
        _source_contract_bindings(request)
    except PortableViewTamperError:
        raise
    except (ValidationError, ValueError, KeyError) as exc:
        raise PortableViewContractError("portable view inputs are invalid") from exc
    state = _derive_state(request)
    tables = _derive_tables(request, state)
    table_bytes = accessible_table_bytes(tables)
    table_sha256 = sha256_bytes(table_bytes)
    provenance_sha256 = (
        _digest(request.provenance_drawer)
        if request.provenance_drawer is not None
        else None
    )
    payload: dict[str, Any] = {
        "view_id": request.view_id,
        "surface_state": state,
        "error_code": (
            request.surface_fixture.error_code.value
            if request.surface_fixture.error_code is not None
            else None
        ),
        "versions": PortableVersions(),
        "accessibility": AccessibilityMetadata(),
        "filters": request.filters,
        "filters_sha256": result_filters_sha256(request.filters),
        "source_identities": request.source_identities,
        "bundle_manifest_sha256s": tuple(
            sorted(_digest(item) for item in request.bundle_manifests)
        ),
        "compatibility": _compatibility(
            request.source_identities, request.fragment_view, state
        ),
        "provenance_drawer_sha256": provenance_sha256,
        "tables": tables,
        "accessible_table_sha256": table_sha256,
        "request_sha256": _digest(request),
        "trust_context_sha256": _digest(trust_context),
    }
    seed = PortableLocalView.model_construct(**payload, view_sha256="0" * 64)
    view = PortableLocalView(
        **payload,
        view_sha256=_digest(seed, exclude={"view_sha256"}),
    )
    return view, table_bytes


_TABLE_HEADER = (
    "table_schema\tmeasurement_kind\ttable_id\tsource_contract_sha256\t"
    "source_state\trow_index\trow_key\tcolumn_id\tvalue_state\tinteger_value\t"
    "decimal_value\ttoken_value\tsha256_value\tboolean_value\tnumerator\t"
    "denominator\tunit_id\n"
)


def _tsv_value(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    return str(value)


def accessible_table_bytes(tables: tuple[ExactMeasurementTable, ...]) -> bytes:
    """Return the canonical long-form exact-value table bytes."""

    lines = [_TABLE_HEADER]
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                values = (
                    table.schema_version,
                    table.measurement_kind.value,
                    table.table_id,
                    table.source_contract_sha256,
                    table.source_state.value,
                    row.row_index,
                    row.row_key,
                    cell.column_id,
                    cell.value_state.value,
                    cell.integer_value,
                    cell.decimal_value,
                    cell.token_value,
                    cell.sha256_value,
                    cell.boolean_value,
                    cell.numerator,
                    cell.denominator,
                    cell.unit_id,
                )
                rendered = tuple(_tsv_value(item) for item in values)
                if any(
                    "\t" in item or "\n" in item or "\r" in item for item in rendered
                ):
                    raise PortableViewContractError(
                        "accessible table cell is not TSV-safe"
                    )
                lines.append("\t".join(rendered) + "\n")
    content = "".join(lines).encode("utf-8")
    if len(content) > MAX_TABLE_BYTES:
        raise PortableViewContractError("accessible table exceeds byte bound")
    return content


def replay_portable_view(
    request: PortableViewBuildRequest,
    expected_view: PortableLocalView,
    expected_table_bytes: bytes,
    *,
    trust_context: PortableTrustContext,
) -> PortableLocalView:
    """Fail closed unless view and table replay byte-identically."""

    actual_view, actual_table = build_portable_view(
        request, trust_context=trust_context
    )
    if actual_view != expected_view or actual_table != expected_table_bytes:
        raise PortableViewTamperError("portable view does not replay byte-identically")
    return actual_view


def _manifest_for(
    view_bytes: bytes, table_bytes: bytes, view: PortableLocalView
) -> PortableViewManifest:
    files = (
        PortableArtifactFile(
            relative_path=TABLE_PATH,
            sha256=sha256_bytes(table_bytes),
            size_bytes=len(table_bytes),
            media_type="text/tab-separated-values; charset=utf-8",
        ),
        PortableArtifactFile(
            relative_path=VIEW_PATH,
            sha256=sha256_bytes(view_bytes),
            size_bytes=len(view_bytes),
            media_type="application/json",
        ),
    )
    source_sha256 = _digest(view.source_identities)
    bindings = {
        "source": source_sha256,
        "table": view.accessible_table_sha256,
        "view": sha256_bytes(view_bytes),
    }
    return PortableViewManifest(
        artifact_id=f"portable-view-{_digest(bindings)[:24]}",
        files=files,
        source_identity_sha256=source_sha256,
        accessible_table_sha256=view.accessible_table_sha256,
        view_sha256=sha256_bytes(view_bytes),
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except PermissionError as exc:
        raise PortableViewPermissionError("portable parent permission denied") from exc
    except OSError as exc:
        raise PortableViewTamperError(
            "portable parent is not a safe directory"
        ) from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PortableViewTamperError("portable parent is not a real directory")
    return descriptor


def _stage_name(parent_fd: int, destination_name: str) -> str:
    for _ in range(32):
        candidate = f".{destination_name}.{secrets.token_hex(8)}"
        try:
            os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            return candidate
        except FileExistsError:
            continue
        except PermissionError as exc:
            raise PortableViewPermissionError(
                "cannot allocate private staging"
            ) from exc
        except OSError as exc:
            raise PortableViewStorageError("cannot allocate private staging") from exc
    raise PortableViewConflictError("could not allocate a unique staging directory")


def _same_directory(path: Path, descriptor: int) -> None:
    try:
        named = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise PortableViewTamperError(
            "portable parent changed during publication"
        ) from exc
    pinned = os.fstat(descriptor)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
        pinned.st_dev,
        pinned.st_ino,
    ):
        raise PortableViewTamperError("portable parent changed during publication")


def _same_directory_at(parent_fd: int, name: str, descriptor: int) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise PortableViewTamperError("portable staging root changed") from exc
    pinned = os.fstat(descriptor)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
        pinned.st_dev,
        pinned.st_ino,
    ):
        raise PortableViewTamperError("portable staging root changed")


def _cleanup(parent_fd: int, stage_name: str | None, stage_fd: int | None) -> None:
    if stage_name is None:
        return
    if stage_fd is not None:
        try:
            os.fchmod(stage_fd, 0o700)
        except OSError:
            pass
        for name in _FILES:
            try:
                os.unlink(name, dir_fd=stage_fd)
            except OSError:
                pass
    try:
        os.rmdir(stage_name, dir_fd=parent_fd)
    except OSError:
        pass


SourceIdentityVerifier = Callable[[], tuple[PortableSourceIdentity, ...]]


@dataclass(frozen=True)
class _PinnedArtifact:
    descriptor: int
    device: int
    inode: int
    mode: int
    size_bytes: int
    sha256: str


def _read_descriptor(descriptor: int, limit: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_sealed_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != SEALED_DIRECTORY_MODE
    ):
        raise PortableViewTamperError("portable directory is not sealed")


def _seal_stage(stage_fd: int) -> None:
    try:
        for name in _FILES:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=stage_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise PortableViewTamperError(
                        "portable artifact cannot be sealed safely"
                    )
                os.fchmod(descriptor, SEALED_FILE_MODE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fchmod(stage_fd, SEALED_DIRECTORY_MODE)
        os.fsync(stage_fd)
        _require_sealed_directory(stage_fd)
    except PortableViewError:
        raise
    except OSError as exc:
        raise PortableViewStorageError("portable artifact sealing failed") from exc


def _pin_artifacts(
    root_fd: int, expected: dict[str, bytes]
) -> dict[str, _PinnedArtifact]:
    pinned: dict[str, _PinnedArtifact] = {}
    current_descriptor: int | None = None
    try:
        for name in _FILES:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_fd,
            )
            current_descriptor = descriptor
            metadata = os.fstat(descriptor)
            expected_bytes = expected[name]
            observed = _read_descriptor(descriptor, len(expected_bytes))
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != SEALED_FILE_MODE
                or metadata.st_size != len(expected_bytes)
                or observed != expected_bytes
            ):
                os.close(descriptor)
                current_descriptor = None
                raise PortableViewTamperError(
                    "artifact is not the expected single-link regular file"
                )
            digest = sha256_bytes(observed)
            if digest != sha256_bytes(expected_bytes):
                os.close(descriptor)
                current_descriptor = None
                raise PortableViewTamperError("artifact digest changed")
            pinned[name] = _PinnedArtifact(
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mode=stat.S_IMODE(metadata.st_mode),
                size_bytes=metadata.st_size,
                sha256=digest,
            )
            current_descriptor = None
            os.fsync(descriptor)
        os.fsync(root_fd)
        return pinned
    except PortableViewError:
        if current_descriptor is not None:
            os.close(current_descriptor)
        for item in pinned.values():
            os.close(item.descriptor)
        raise
    except OSError as exc:
        if current_descriptor is not None:
            try:
                os.close(current_descriptor)
            except OSError:
                pass
        for item in pinned.values():
            os.close(item.descriptor)
        raise PortableViewTamperError(
            "artifacts could not be descriptor-pinned"
        ) from exc


def _pin_existing_artifacts(
    root_fd: int, limits: dict[str, int]
) -> tuple[dict[str, _PinnedArtifact], dict[str, bytes]]:
    pinned: dict[str, _PinnedArtifact] = {}
    content: dict[str, bytes] = {}
    current_descriptor: int | None = None
    try:
        if set(os.listdir(root_fd)) != set(_FILES):
            raise PortableViewTamperError("portable file inventory is not exact")
        for name in _FILES:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_fd,
            )
            current_descriptor = descriptor
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != SEALED_FILE_MODE
                or metadata.st_size > limits[name]
            ):
                os.close(descriptor)
                current_descriptor = None
                raise PortableViewTamperError(
                    "portable file type, link count, or size is invalid"
                )
            observed = _read_descriptor(descriptor, limits[name])
            if len(observed) != metadata.st_size:
                os.close(descriptor)
                current_descriptor = None
                raise PortableViewTamperError("portable file changed while pinned")
            digest = sha256_bytes(observed)
            pinned[name] = _PinnedArtifact(
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mode=stat.S_IMODE(metadata.st_mode),
                size_bytes=metadata.st_size,
                sha256=digest,
            )
            content[name] = observed
            current_descriptor = None
        return pinned, content
    except PortableViewError:
        if current_descriptor is not None:
            os.close(current_descriptor)
        _close_pinned(pinned)
        raise
    except OSError as exc:
        if current_descriptor is not None:
            try:
                os.close(current_descriptor)
            except OSError:
                pass
        _close_pinned(pinned)
        raise PortableViewTamperError(
            "portable files could not be descriptor-pinned"
        ) from exc


def _validate_pinned_names(
    root_fd: int,
    pinned: dict[str, _PinnedArtifact],
    expected: dict[str, bytes],
    *,
    sync: bool,
) -> None:
    named: dict[str, int] = {}
    try:
        _require_sealed_directory(root_fd)
        if set(os.listdir(root_fd)) != set(_FILES):
            raise PortableViewTamperError("artifact inventory changed")
        for name in _FILES:
            item = pinned[name]
            named_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_fd,
            )
            named[name] = named_fd
            metadata = os.fstat(named_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != SEALED_FILE_MODE
                or (metadata.st_dev, metadata.st_ino) != (item.device, item.inode)
                or metadata.st_size != item.size_bytes
            ):
                raise PortableViewTamperError(
                    "artifact name no longer resolves to its sealed pinned inode"
                )

        for name in reversed(_FILES):
            item = pinned[name]
            expected_bytes = expected[name]
            pinned_metadata = os.fstat(item.descriptor)
            named_metadata = os.fstat(named[name])
            for metadata in (pinned_metadata, named_metadata):
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != item.mode
                    or (metadata.st_dev, metadata.st_ino) != (item.device, item.inode)
                    or metadata.st_size != item.size_bytes
                ):
                    raise PortableViewTamperError("sealed artifact stat vector changed")
            pinned_bytes = _read_descriptor(item.descriptor, len(expected_bytes))
            named_bytes = _read_descriptor(named[name], len(expected_bytes))
            if (
                pinned_bytes != expected_bytes
                or named_bytes != expected_bytes
                or sha256_bytes(pinned_bytes) != item.sha256
                or sha256_bytes(named_bytes) != item.sha256
            ):
                raise PortableViewTamperError("sealed artifact digest vector changed")
            if sync:
                os.fsync(item.descriptor)
                os.fsync(named[name])

        if set(os.listdir(root_fd)) != set(_FILES):
            raise PortableViewTamperError("artifact inventory changed")
        for name in reversed(_FILES):
            item = pinned[name]
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != item.mode
                or (metadata.st_dev, metadata.st_ino) != (item.device, item.inode)
                or metadata.st_size != item.size_bytes
            ):
                raise PortableViewTamperError(
                    "final artifact name-to-inode vector changed"
                )
        _require_sealed_directory(root_fd)
        if sync:
            os.fsync(root_fd)
    except PortableViewError:
        raise
    except OSError as exc:
        raise PortableViewTamperError(
            "artifact inventory could not be revalidated"
        ) from exc
    finally:
        for descriptor in named.values():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _close_pinned(pinned: dict[str, _PinnedArtifact]) -> None:
    for item in pinned.values():
        try:
            os.close(item.descriptor)
        except OSError:
            pass


def _quarantine_owned_directory(
    parent_fd: int, name: str, descriptor: int
) -> str | None:
    try:
        _same_directory_at(parent_fd, name, descriptor)
    except PortableViewTamperError:
        return None
    for _ in range(32):
        quarantine_name = f".{name}.invalid.{secrets.token_hex(8)}"
        try:
            rename_directory_exclusive_at(parent_fd, name, quarantine_name)
            _same_directory_at(parent_fd, quarantine_name, descriptor)
            os.fsync(parent_fd)
            return quarantine_name
        except FileExistsError:
            continue
        except (OSError, PortableViewError):
            return None
    return None


def _verify_trusted_view(
    view: PortableLocalView, trust_context: PortableTrustContext
) -> None:
    if view.source_identities != trust_context.expected_source_identities:
        raise PortableViewTamperError("view sources differ from trust context")
    if (
        view.bundle_manifest_sha256s != trust_context.expected_bundle_manifest_sha256s
        or view.trust_context_sha256 != _digest(trust_context)
    ):
        raise PortableViewTamperError("view manifests differ from trust context")


def publish_portable_view(
    destination: str | Path,
    *,
    view: PortableLocalView,
    accessible_table: bytes,
    trust_context: PortableTrustContext,
    source_identity_verifier: SourceIdentityVerifier,
) -> Path:
    """Durably publish after immediate source re-verification, without overwrite."""

    destination = Path(destination)
    if destination.name in {"", ".", ".."} or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,127}", destination.name
    ):
        raise PortableViewContractError("portable destination name is not controlled")
    _verify_trusted_view(view, trust_context)
    if accessible_table_bytes(view.tables) != accessible_table:
        raise PortableViewTamperError("accessible table bytes do not match exact view")
    if sha256_bytes(accessible_table) != view.accessible_table_sha256:
        raise PortableViewTamperError("accessible table digest does not match view")
    view_bytes = canonical_json_bytes(view)
    if len(view_bytes) > MAX_VIEW_BYTES:
        raise PortableViewContractError("portable view exceeds byte bound")
    manifest = _manifest_for(view_bytes, accessible_table, view)
    content = {
        MANIFEST_PATH: canonical_json_bytes(manifest),
        TABLE_PATH: accessible_table,
        VIEW_PATH: view_bytes,
    }
    if sum(map(len, content.values())) > MAX_TOTAL_BYTES:
        raise PortableViewContractError("portable artifact exceeds total byte bound")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise PortableViewPermissionError("portable parent permission denied") from exc
    except OSError as exc:
        raise PortableViewStorageError("portable parent could not be created") from exc
    parent_fd = _open_directory(destination.parent)
    lock_name = f".{destination.name}.publish.lock"
    stage_name: str | None = None
    stage_fd: int | None = None
    pinned: dict[str, _PinnedArtifact] = {}
    lock_created = False
    published = False
    try:
        try:
            lock_fd = os.open(
                lock_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise PortableViewConflictError(
                "portable publication is in progress"
            ) from exc
        except PermissionError as exc:
            raise PortableViewPermissionError(
                "portable lock permission denied"
            ) from exc
        except OSError as exc:
            raise PortableViewStorageError(
                "portable lock could not be created"
            ) from exc
        lock_created = True
        os.close(lock_fd)
        stage_name = _stage_name(parent_fd, destination.name)
        try:
            stage_fd = os.open(
                stage_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except PermissionError as exc:
            raise PortableViewPermissionError(
                "private staging permission denied"
            ) from exc
        except OSError as exc:
            raise PortableViewStorageError(
                "private staging could not be opened"
            ) from exc
        for relative_path in sorted(content):
            try:
                descriptor = os.open(
                    relative_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=stage_fd,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content[relative_path])
                    stream.flush()
                    os.fsync(stream.fileno())
            except PermissionError as exc:
                raise PortableViewPermissionError(
                    "portable file permission denied"
                ) from exc
            except OSError as exc:
                raise PortableViewStorageError(
                    "portable file could not be stored"
                ) from exc
        try:
            os.fsync(stage_fd)
        except OSError as exc:
            raise PortableViewStorageError(
                "portable staging could not be synced"
            ) from exc
        _seal_stage(stage_fd)
        pinned = _pin_artifacts(stage_fd, content)
        try:
            observed = source_identity_verifier()
        except PortableViewError:
            raise
        except Exception as exc:
            raise PortableViewTamperError(
                "source identity re-verification failed"
            ) from exc
        if observed != view.source_identities:
            raise PortableViewTamperError("source identity changed before publication")
        _validate_pinned_names(stage_fd, pinned, content, sync=True)
        _same_directory_at(parent_fd, stage_name, stage_fd)
        _same_directory(destination.parent, parent_fd)
        try:
            rename_directory_exclusive_at(parent_fd, stage_name, destination.name)
        except FileExistsError as exc:
            raise PortableViewConflictError(
                "portable destination already exists"
            ) from exc
        except PermissionError as exc:
            raise PortableViewPermissionError(
                "portable rename permission denied"
            ) from exc
        except OSError as exc:
            raise PortableViewStorageError("portable atomic rename failed") from exc
        try:
            _same_directory_at(parent_fd, destination.name, stage_fd)
            _validate_pinned_names(stage_fd, pinned, content, sync=True)
            _same_directory(destination.parent, parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise PortableViewStorageError(
                    "portable parent could not be synced"
                ) from exc
        except PortableViewError:
            quarantine_name = _quarantine_owned_directory(
                parent_fd, destination.name, stage_fd
            )
            stage_name = quarantine_name
            raise
        published = True
        return destination
    finally:
        if not published:
            _cleanup(parent_fd, stage_name, stage_fd)
        _close_pinned(pinned)
        if stage_fd is not None:
            os.close(stage_fd)
        if lock_created:
            try:
                os.unlink(lock_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _parse_canonical(model: type[ModelT], content: bytes, label: str) -> ModelT:
    try:
        parsed = model.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        raise PortableViewTamperError(f"{label} is invalid") from exc
    if canonical_json_bytes(parsed) != content:
        raise PortableViewTamperError(f"{label} is not canonical")
    return parsed


def verify_portable_view(
    root: str | Path, *, trust_context: PortableTrustContext
) -> VerifiedPortableView:
    """Read through a pinned directory and replay every stored commitment."""

    root_path = Path(root)
    root_fd = _open_directory(root_path)
    pinned: dict[str, _PinnedArtifact] = {}
    try:
        _require_sealed_directory(root_fd)
        limits = {
            MANIFEST_PATH: MAX_MANIFEST_BYTES,
            TABLE_PATH: MAX_TABLE_BYTES,
            VIEW_PATH: MAX_VIEW_BYTES,
        }
        pinned, content = _pin_existing_artifacts(root_fd, limits)
        if sum(map(len, content.values())) > MAX_TOTAL_BYTES:
            raise PortableViewTamperError("portable artifact exceeds total byte bound")
        manifest = _parse_canonical(
            PortableViewManifest, content[MANIFEST_PATH], "portable manifest"
        )
        view = _parse_canonical(PortableLocalView, content[VIEW_PATH], "portable view")
        _verify_trusted_view(view, trust_context)
        if accessible_table_bytes(view.tables) != content[TABLE_PATH]:
            raise PortableViewTamperError("accessible table does not replay from view")
        if sha256_bytes(content[TABLE_PATH]) != view.accessible_table_sha256:
            raise PortableViewTamperError("accessible table digest mismatch")
        if manifest != _manifest_for(content[VIEW_PATH], content[TABLE_PATH], view):
            raise PortableViewTamperError("portable manifest does not replay")
        _validate_pinned_names(root_fd, pinned, content, sync=False)
        _same_directory(root_path, root_fd)
        return VerifiedPortableView(
            manifest=manifest,
            view=view,
            accessible_table_bytes=content[TABLE_PATH],
        )
    finally:
        _close_pinned(pinned)
        os.close(root_fd)


__all__ = [
    "AccessibilityMetadata",
    "CompatibilitySummary",
    "DeltaState",
    "ExactCell",
    "ExactMeasurementTable",
    "ExactTableRow",
    "ExactValueState",
    "MeasurementKind",
    "PortableLocalView",
    "PortableSourceIdentity",
    "PortableSurfaceState",
    "PortableTrustContext",
    "PortableVersions",
    "PortableViewBuildRequest",
    "PortableViewConflictError",
    "PortableViewContractError",
    "PortableViewError",
    "PortableViewManifest",
    "PortableViewPermissionError",
    "PortableViewStorageError",
    "PortableViewTamperError",
    "VerifiedPortableView",
    "accessible_table_bytes",
    "build_portable_view",
    "publish_portable_view",
    "replay_portable_view",
    "verify_portable_view",
]
