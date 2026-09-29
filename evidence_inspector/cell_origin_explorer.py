"""Pure, deterministic view contracts for cell-origin estimates.

The module transforms a reviewed cell-origin result bundle plus the E06 result
view authority boundary into framework-independent dot/interval and exact-table
rows.  It performs no I/O, inference, relabeling, or clinical interpretation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from traceback_runner.serialization import canonical_json_bytes

from .cell_origin_models import (
    BootstrapInformationStatus,
    BootstrapResultV2,
    CellOriginResult,
    DeconvolutionOutputV2,
)
from .cell_origin_pipeline import (
    DEFAULT_TOP_COMPOSITION_ROWS,
    PALETTE,
    CellOriginResultBundle,
    ResourceSummary,
)
from .compatibility import ExecutionState, InformationState, TrustState
from .result_view import (
    CompatibilityContract,
    ResultViewRequest,
    build_result_view,
    result_filters_sha256,
)

MAX_CONTRIBUTORS = 512
MAX_LIMITATIONS = 16
MAX_MARKERS = 20_000
MAX_NOTICES = 64
MAX_PROVENANCE_ITEMS = 256
MAX_SAFE_TEXT_LENGTH = 512
AXIS_LABEL = "estimated fraction among registered atlas contributors"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SafeIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]

_PRIVATE_PREFIX = re.compile(
    r"^(?:donor|patient|read|sample|sequence|path)(?:[_:.-]?[a-z0-9].*)?$",
    re.IGNORECASE,
)
_SAFE_PRIVATE_PREFIX_LEXEMES = {
    "pathology",
    "readiness",
    "readout",
    "sampled",
    "sequencer",
}
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s=:(\[{'\"\\])(?:~[/\\]|\.\.?[/\\]|/|[A-Za-z]:[\\/])"
    r"[^\s,;)\]}'\"]+"
)
_ENCODED_PATH = re.compile(r"(?:%2f|%5c|file%3a)", re.IGNORECASE)
_RELATIVE_FILE_PATH = re.compile(
    r"(?:^|\s)[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.[A-Za-z0-9]{1,12}"
    r"(?=$|[\s,;)])"
)
_RAW_IDENTIFIER = re.compile(
    r"\b(?:donor|patient|read|sample|query)(?:[_:.-]?(?:id)?[_:.-]?)?"
    r"[a-z0-9]*\d+[a-z0-9_.:-]*\b",
    re.IGNORECASE,
)
_SEQUENCE = re.compile(r"(?<![A-Za-z])[ACGTN]{20,}(?![A-Za-z])", re.IGNORECASE)
_SECRET = re.compile(
    r"(?:AWS_SECRET_ACCESS_KEY|PRIVATE_KEY|PASSWORD|SECRET|TOKEN)\s*=",
    re.IGNORECASE,
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "donor_id",
        "patient_id",
        "read_id",
        "read_ids",
        "sample_id",
        "sequence",
        "local_path",
        "path",
        "secret",
    }
)


class CellOriginExplorerError(ValueError):
    """The explorer input or canonical replay failed closed."""


class ExplorerStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"
    NOT_RUN = "not_run"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    MISSING = "missing"


class ValueState(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"


class LimitationId(StrEnum):
    CONDITIONING_NOT_REPORTED = "conditioning_not_reported"
    CROSS_MARKER_LINKAGE_NOT_PRESERVED = "cross_marker_linkage_not_preserved"
    PARTIAL_INPUT = "partial_input"
    UNCERTAINTY_NOT_RUN = "uncertainty_not_run"
    UNCERTAINTY_PARTIAL = "uncertainty_partial"
    UNCERTAINTY_INSUFFICIENT = "uncertainty_insufficient"


class CellOriginExplorerRequest(CompatibilityContract):
    schema_version: Literal["traceback.cell-origin-explorer-request.v1"] = (
        "traceback.cell-origin-explorer-request.v1"
    )
    result_view_request: ResultViewRequest
    bundle: CellOriginResultBundle | None = None

    @model_validator(mode="after")
    def exact_single_source(self) -> CellOriginExplorerRequest:
        _validate_result_view_identity(self.result_view_request)
        source = self.result_view_request.sources[0]
        record = source.record
        status = _result_view_status(self.result_view_request)
        if self.bundle is None:
            if status == ExplorerStatus.READY:
                raise ValueError(
                    "eligible cell-origin source requires its exact bundle"
                )
            return self
        if status != ExplorerStatus.READY:
            raise ValueError("non-ready cell-origin source cannot carry a bundle")

        result = self.bundle.result
        deconvolution = result.deconvolution
        if not isinstance(deconvolution, DeconvolutionOutputV2):
            raise ValueError("cell-origin explorer requires deconvolution v2")
        if result.bootstrap is not None and not isinstance(
            result.bootstrap, BootstrapResultV2
        ):
            raise ValueError("cell-origin explorer accepts only bootstrap v2")
        _validate_bundle_bounds(self.bundle)
        _validate_bundle_denominators(self.bundle)
        _validate_bundle_contributors(self.bundle)
        _assert_private_data_absent(self.bundle.model_dump(mode="json"))
        if result.result_id != record.result_id:
            raise ValueError("bundle result ID does not match E05 record")
        atlas = record.compatibility_key.atlas_asset
        if (
            atlas.asset_id != deconvolution.atlas_id
            or atlas.content_sha256 != deconvolution.atlas_sha256
        ):
            raise ValueError("atlas identity does not match exact deconvolution")
        if _digest(result) != record.result_sha256:
            raise ValueError("result digest does not match E05 record")
        if _digest(self.bundle) != record.bundle_sha256:
            raise ValueError("bundle digest does not match E05 record")
        return self


class CellOriginReplaySource(CompatibilityContract):
    """Replay-safe scientific source with presentation prose removed."""

    schema_version: Literal["traceback.cell-origin-replay-source.v1"] = (
        "traceback.cell-origin-replay-source.v1"
    )
    result: CellOriginResult
    resources: ResourceSummary
    bound_bundle_sha256: Sha256

    @model_validator(mode="after")
    def safe_source(self) -> CellOriginReplaySource:
        if not isinstance(self.result.deconvolution, DeconvolutionOutputV2):
            raise ValueError("replay source requires deconvolution v2")
        if self.result.bootstrap is not None and not isinstance(
            self.result.bootstrap, BootstrapResultV2
        ):
            raise ValueError("replay source accepts only bootstrap v2")
        _validate_source_bounds(self.result)
        _assert_private_data_absent(self.model_dump(mode="json"))
        _validate_source_denominators(self.result, self.resources)
        _validate_source_contributors(self.result, self.resources)
        return self


class CellOriginExplorerReplayRequest(CompatibilityContract):
    """Canonical replay request that never contains chart aliases or notices."""

    schema_version: Literal["traceback.cell-origin-explorer-replay-request.v1"] = (
        "traceback.cell-origin-explorer-replay-request.v1"
    )
    result_view_request: ResultViewRequest
    source: CellOriginReplaySource | None = None

    @model_validator(mode="after")
    def exact_replay_source(self) -> CellOriginExplorerReplayRequest:
        _validate_result_view_identity(self.result_view_request)
        status = _result_view_status(self.result_view_request)
        if status == ExplorerStatus.READY and self.source is None:
            raise ValueError("ready replay request requires a safe source")
        if status != ExplorerStatus.READY and self.source is not None:
            raise ValueError("non-ready replay request cannot contain source values")
        if self.source is None:
            return self
        record = self.result_view_request.sources[0].record
        result = self.source.result
        deconvolution = result.deconvolution
        assert isinstance(deconvolution, DeconvolutionOutputV2)
        atlas = record.compatibility_key.atlas_asset
        assert atlas is not None
        if result.result_id != record.result_id:
            raise ValueError("replay result ID does not match E05 record")
        if _digest(result) != record.result_sha256:
            raise ValueError("replay result digest does not match E05 record")
        if self.source.bound_bundle_sha256 != record.bundle_sha256:
            raise ValueError("replay bundle digest does not match E05 record")
        if (
            atlas.asset_id != deconvolution.atlas_id
            or atlas.content_sha256 != deconvolution.atlas_sha256
        ):
            raise ValueError("replay atlas identity does not match deconvolution")
        return self


class ExplorerBinding(CompatibilityContract):
    result_id: SafeIdentifier
    result_sha256: Sha256
    bundle_id: SafeIdentifier
    bundle_sha256: Sha256
    method_id: SafeIdentifier
    method_version: str = Field(min_length=1, max_length=64)
    method_definition_sha256: Sha256
    cell_origin_method_sha256: Sha256 | None
    atlas_id: SafeIdentifier
    atlas_sha256: Sha256
    authority_head_sha256: Sha256
    filter_sha256: Sha256


class DotIntervalRow(CompatibilityContract):
    contributor_id: SafeIdentifier
    label: SafeIdentifier
    rank: int = Field(ge=1, le=MAX_CONTRIBUTORS)
    estimate_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    uncertainty_status: BootstrapInformationStatus | Literal["not_run"]
    lower_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    upper_fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def no_fake_whiskers(self) -> DotIntervalRow:
        available = self.uncertainty_status == BootstrapInformationStatus.AVAILABLE
        if available != (
            self.lower_fraction is not None and self.upper_fraction is not None
        ):
            raise ValueError("only available uncertainty may contain whiskers")
        if available:
            assert self.lower_fraction is not None and self.upper_fraction is not None
            if not self.lower_fraction <= self.estimate_fraction <= self.upper_fraction:
                raise ValueError("uncertainty interval must contain estimate")
        return self


class ExactTableRow(CompatibilityContract):
    contributor_id: SafeIdentifier
    label: SafeIdentifier
    estimate_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    uncertainty_status: BootstrapInformationStatus | Literal["not_run"]
    lower_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    upper_fraction: float | None = Field(default=None, ge=0.0, le=1.0)


class MarkerSupport(CompatibilityContract):
    registered_markers: int = Field(ge=1)
    usable_markers: int = Field(ge=1)
    excluded_incomplete_atlas_markers: int = Field(ge=0)
    collapsed_duplicate_regions: int = Field(ge=0)
    observed_markers: int = Field(ge=1)


class FragmentDenominators(CompatibilityContract):
    input_fragments: int = Field(ge=0)
    marker_overlaps: int = Field(ge=0)
    classified_fragment_marker_observations: int = Field(ge=0)
    excluded_fewer_than_four_cpgs: int = Field(ge=0)


class AtlasCoverage(CompatibilityContract):
    usable_markers: int = Field(ge=1)
    registered_markers: int = Field(ge=1)
    contributor_count: int = Field(ge=1, le=MAX_CONTRIBUTORS)
    marker_coverage_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class SolverDiagnostics(CompatibilityContract):
    converged: bool
    iterations: int = Field(ge=0)
    residual_l2: float = Field(ge=0.0, allow_inf_nan=False)
    objective_value: float = Field(ge=0.0, allow_inf_nan=False)
    conditioning_state: Literal[ValueState.MISSING] = ValueState.MISSING
    condition_number: None = None
    conditioning_reason: Literal["not_reported_by_cell_origin_deconvolution_v2"] = (
        "not_reported_by_cell_origin_deconvolution_v2"
    )


class CellOriginExplorerView(CompatibilityContract):
    schema_version: Literal["traceback.cell-origin-explorer-view.v1"] = (
        "traceback.cell-origin-explorer-view.v1"
    )
    status: ExplorerStatus
    axis_label: Literal[
        "estimated fraction among registered atlas contributors"
    ] = AXIS_LABEL
    binding: ExplorerBinding
    dot_interval_rows: tuple[DotIntervalRow, ...] = Field(
        max_length=MAX_CONTRIBUTORS
    )
    exact_table_rows: tuple[ExactTableRow, ...] = Field(max_length=MAX_CONTRIBUTORS)
    marker_support: MarkerSupport | None = None
    fragment_denominators: FragmentDenominators | None = None
    atlas_coverage: AtlasCoverage | None = None
    diagnostics: SolverDiagnostics | None = None
    uncertainty_status: BootstrapInformationStatus | Literal["not_run", "unavailable"]
    limitations: tuple[LimitationId, ...] = Field(max_length=MAX_LIMITATIONS)
    request_sha256: Sha256

    @model_validator(mode="after")
    def coherent_rows(self) -> CellOriginExplorerView:
        numerical = self.status == ExplorerStatus.READY
        detail = (
            self.marker_support,
            self.fragment_denominators,
            self.atlas_coverage,
            self.diagnostics,
        )
        if numerical:
            if not self.dot_interval_rows or any(item is None for item in detail):
                raise ValueError("ready explorer requires numerical rows and details")
        elif self.dot_interval_rows or self.exact_table_rows or any(
            item is not None for item in detail
        ):
            raise ValueError("unavailable explorer cannot expose numerical values")
        expected_table = tuple(
            ExactTableRow(
                contributor_id=row.contributor_id,
                label=row.label,
                estimate_fraction=row.estimate_fraction,
                uncertainty_status=row.uncertainty_status,
                lower_fraction=row.lower_fraction,
                upper_fraction=row.upper_fraction,
            )
            for row in self.dot_interval_rows
        )
        if self.exact_table_rows != expected_table:
            raise ValueError("exact table must match dot-and-interval rows")
        row_keys = [
            (-row.estimate_fraction, row.contributor_id)
            for row in self.dot_interval_rows
        ]
        if row_keys != sorted(row_keys):
            raise ValueError("contributor rows must use deterministic order")
        if [row.rank for row in self.dot_interval_rows] != list(
            range(1, len(self.dot_interval_rows) + 1)
        ):
            raise ValueError("contributor ranks must be contiguous")
        contributor_ids = [row.contributor_id for row in self.dot_interval_rows]
        if len(contributor_ids) != len(set(contributor_ids)):
            raise ValueError("view contributors must be unique")
        if self.limitations != tuple(sorted(set(self.limitations), key=str)):
            raise ValueError("limitations must be uniquely sorted")
        return self


class CellOriginExplorerArtifact(CompatibilityContract):
    schema_version: Literal["traceback.cell-origin-explorer-artifact.v1"] = (
        "traceback.cell-origin-explorer-artifact.v1"
    )
    request: CellOriginExplorerReplayRequest
    view: CellOriginExplorerView

    @model_validator(mode="after")
    def replay_exactly(self) -> CellOriginExplorerArtifact:
        if _build_replay_view(self.request) != self.view:
            raise ValueError("cell-origin explorer artifact does not replay exactly")
        return self


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _assert_private_data_absent(value: Any, *, field: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_FIELDS:
                raise ValueError("cell-origin bundle contains a private field")
            _assert_private_data_absent(item, field=f"{field}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _assert_private_data_absent(item, field=f"{field}[{index}]")
    elif isinstance(value, str) and (
        len(value) > MAX_SAFE_TEXT_LENGTH
        or "://" in value
        or _ABSOLUTE_PATH.search(value)
        or _ENCODED_PATH.search(value)
        or _RELATIVE_FILE_PATH.search(value)
        or _RAW_IDENTIFIER.search(value)
        or (
            _SEQUENCE.search(value)
            and re.fullmatch(r"[0-9a-f]{64}", value) is None
        )
        or _SECRET.search(value)
    ):
        raise ValueError(f"cell-origin bundle contains private data at {field}")


def _validate_result_view_identity(request: ResultViewRequest) -> None:
    if len(request.sources) != 1:
        raise ValueError("cell-origin explorer requires exactly one E06 source")
    record = request.sources[0].record
    if record.method.family.value != "cell_origin":
        raise ValueError("E01 method family must be cell_origin")
    if record.compatibility_key.atlas_asset is None:
        raise ValueError("cell-origin compatibility identity requires an atlas")


def _result_view_status(request: ResultViewRequest) -> ExplorerStatus:
    record = request.sources[0].record
    if record.execution_state == ExecutionState.FAILED:
        return ExplorerStatus.FAILED
    if record.execution_state == ExecutionState.NOT_RUN:
        return ExplorerStatus.NOT_RUN
    if record.information_state == InformationState.INSUFFICIENT:
        return ExplorerStatus.INSUFFICIENT_INFORMATION
    filtered = build_result_view(request)
    if (
        record.information_state != InformationState.SUFFICIENT
        or record.trust_state != TrustState.VERIFIED
        or not record.current_capability.research_inspectable
        or filtered.visible_count != 1
    ):
        return ExplorerStatus.MISSING
    return ExplorerStatus.READY


def _validate_source_bounds(result: CellOriginResult) -> None:
    if len(result.marker_counts) > MAX_MARKERS:
        raise ValueError("marker count exceeds explorer bound")
    provenance = result.provenance
    if any(
        len(items) > MAX_PROVENANCE_ITEMS
        for items in (
            provenance.input_artifacts,
            provenance.source_ids,
            provenance.software_versions,
        )
    ):
        raise ValueError("provenance collection exceeds explorer bound")


def _validate_bundle_bounds(bundle: CellOriginResultBundle) -> None:
    _validate_source_bounds(bundle.result)
    if len(bundle.notices) > MAX_NOTICES:
        raise ValueError("notice count exceeds explorer bound")
    if len(bundle.charts.composition_rows) > MAX_CONTRIBUTORS:
        raise ValueError("composition chart exceeds explorer bound")
    if len(bundle.charts.healthy_context_rows) > MAX_CONTRIBUTORS:
        raise ValueError("healthy-context chart exceeds explorer bound")
    if any(not notice or len(notice) > MAX_SAFE_TEXT_LENGTH for notice in bundle.notices):
        raise ValueError("notice text exceeds explorer bound")
    if any(
        not row.label or len(row.label) > 160
        for row in bundle.charts.composition_rows
    ):
        raise ValueError("composition label exceeds explorer bound")


def _validate_source_denominators(
    result: CellOriginResult,
    resources: ResourceSummary,
) -> None:
    provenance = result.provenance
    classified = sum(row.classified_fragment_count for row in result.marker_counts)
    if classified != provenance.classified_fragment_marker_count:
        raise ValueError("classified fragment-marker denominator mismatch")
    if (
        provenance.classified_fragment_marker_count
        + provenance.excluded_fewer_than_four_cpgs
        != provenance.marker_overlap_count
    ):
        raise ValueError("marker-overlap denominator mismatch")
    if resources.usable_markers != len(result.marker_counts):
        raise ValueError("usable marker denominator mismatch")
    if resources.registered_markers != (
        resources.usable_markers
        + resources.excluded_incomplete_atlas_markers
        + resources.collapsed_duplicate_regions
    ):
        raise ValueError("registered marker denominator mismatch")


def _validate_bundle_denominators(bundle: CellOriginResultBundle) -> None:
    _validate_source_denominators(bundle.result, bundle.resources)


def _safe_contributor_id(value: str) -> bool:
    segments = re.split(r"[^a-z0-9]+", value.lower())
    return all(
        segment in _SAFE_PRIVATE_PREFIX_LEXEMES
        or _PRIVATE_PREFIX.fullmatch(segment) is None
        for segment in segments
        if segment
    )


def _validate_source_contributors(
    result: CellOriginResult,
    resources: ResourceSummary,
) -> None:
    estimates = result.deconvolution.estimates
    if len(estimates) > MAX_CONTRIBUTORS:
        raise ValueError("contributor count exceeds explorer bound")
    ids = [item.cell_type_id for item in estimates]
    if len(ids) != len(set(ids)):
        raise ValueError("contributors must be unique")
    if any(not _safe_contributor_id(item) for item in ids):
        raise ValueError("contributor identifier contains a reserved privacy term")
    if resources.cell_type_count != len(estimates):
        raise ValueError("cell-type denominator mismatch")


def _validate_bundle_contributors(bundle: CellOriginResultBundle) -> None:
    result = bundle.result
    estimates = result.deconvolution.estimates
    _validate_source_contributors(result, bundle.resources)
    ids = [item.cell_type_id for item in estimates]
    charts = bundle.charts.composition_rows
    if len(charts) != len(ids):
        raise ValueError("chart contributors do not match exact result")
    ordered = sorted(estimates, key=lambda item: (-item.fraction, item.cell_type_id))
    bootstrap = result.bootstrap
    intervals = (
        {item.cell_type_id: item for item in bootstrap.intervals}
        if isinstance(bootstrap, BootstrapResultV2)
        else {}
    )
    for rank, (estimate, chart) in enumerate(zip(ordered, charts, strict=True), 1):
        if chart.cell_type_id != estimate.cell_type_id:
            raise ValueError("chart contributors do not match exact result")
        if chart.rank != rank:
            raise ValueError("chart rank does not match exact result")
        if chart.fraction != estimate.fraction:
            raise ValueError("chart estimate does not match exact result")
        if chart.percent != estimate.fraction * 100.0:
            raise ValueError("chart percent does not match exact result")
        interval = intervals.get(estimate.cell_type_id)
        expected_status = (
            interval.information_status
            if interval is not None
            else BootstrapInformationStatus.INSUFFICIENT_INFORMATION
        )
        expected_lower = interval.lower_fraction if interval is not None else None
        expected_upper = interval.upper_fraction if interval is not None else None
        expected_available = expected_status == BootstrapInformationStatus.AVAILABLE
        if (
            chart.uncertainty_status != expected_status
            or chart.uncertainty_available != expected_available
            or chart.lower_fraction != expected_lower
            or chart.upper_fraction != expected_upper
            or chart.lower_percent
            != (expected_lower * 100.0 if expected_lower is not None else None)
            or chart.upper_percent
            != (expected_upper * 100.0 if expected_upper is not None else None)
        ):
            raise ValueError("chart uncertainty does not match exact bootstrap")
        if chart.color != PALETTE[(rank - 1) % len(PALETTE)]:
            raise ValueError("chart color does not match deterministic palette")
        if chart.show_by_default != (rank <= DEFAULT_TOP_COMPOSITION_ROWS):
            raise ValueError("chart visibility does not match deterministic rank")


def _to_replay_request(
    request: CellOriginExplorerRequest,
) -> CellOriginExplorerReplayRequest:
    record = request.result_view_request.sources[0].record
    safe_source = (
        CellOriginReplaySource(
            result=request.bundle.result,
            resources=request.bundle.resources,
            bound_bundle_sha256=record.bundle_sha256,
        )
        if request.bundle is not None
        else None
    )
    return CellOriginExplorerReplayRequest(
        result_view_request=request.result_view_request,
        source=safe_source,
    )


def _binding(request: CellOriginExplorerReplayRequest) -> ExplorerBinding:
    source = request.result_view_request.sources[0]
    record = source.record
    atlas = record.compatibility_key.atlas_asset
    assert atlas is not None
    return ExplorerBinding(
        result_id=record.result_id,
        result_sha256=record.result_sha256,
        bundle_id=record.bundle_id,
        bundle_sha256=record.bundle_sha256,
        method_id=record.method.method_id,
        method_version=record.method.version,
        method_definition_sha256=record.method_definition_sha256,
        cell_origin_method_sha256=(
            _digest(request.source.result.method)
            if request.source is not None
            else None
        ),
        atlas_id=atlas.asset_id,
        atlas_sha256=atlas.content_sha256,
        authority_head_sha256=record.current_capability.authority_head_sha256,
        filter_sha256=result_filters_sha256(request.result_view_request.filters),
    )


def _build_replay_view(
    request: CellOriginExplorerReplayRequest,
) -> CellOriginExplorerView:
    status = _result_view_status(request.result_view_request)
    binding = _binding(request)
    request_sha = _digest(request)
    if status != ExplorerStatus.READY:
        return CellOriginExplorerView(
            status=status,
            binding=binding,
            dot_interval_rows=(),
            exact_table_rows=(),
            uncertainty_status="unavailable",
            limitations=(),
            request_sha256=request_sha,
        )

    source = request.source
    assert source is not None
    result = source.result
    deconvolution = result.deconvolution
    assert isinstance(deconvolution, DeconvolutionOutputV2)
    bootstrap = result.bootstrap
    interval_by_id = (
        {item.cell_type_id: item for item in bootstrap.intervals}
        if isinstance(bootstrap, BootstrapResultV2)
        else {}
    )
    ordered = sorted(
        deconvolution.estimates,
        key=lambda item: (-item.fraction, item.cell_type_id),
    )
    rows = []
    for rank, estimate in enumerate(ordered, start=1):
        interval = interval_by_id.get(estimate.cell_type_id)
        rows.append(
            DotIntervalRow(
                contributor_id=estimate.cell_type_id,
                label=estimate.cell_type_id,
                rank=rank,
                estimate_fraction=estimate.fraction,
                uncertainty_status=(
                    interval.information_status if interval is not None else "not_run"
                ),
                lower_fraction=(
                    interval.lower_fraction if interval is not None else None
                ),
                upper_fraction=(
                    interval.upper_fraction if interval is not None else None
                ),
            )
        )
    dot_rows = tuple(rows)
    table_rows = tuple(
        ExactTableRow(
            contributor_id=row.contributor_id,
            label=row.label,
            estimate_fraction=row.estimate_fraction,
            uncertainty_status=row.uncertainty_status,
            lower_fraction=row.lower_fraction,
            upper_fraction=row.upper_fraction,
        )
        for row in dot_rows
    )
    limitations = {LimitationId.CONDITIONING_NOT_REPORTED}
    uncertainty: BootstrapInformationStatus | Literal["not_run", "unavailable"]
    if isinstance(bootstrap, BootstrapResultV2):
        uncertainty = bootstrap.information_status
        limitations.add(LimitationId.CROSS_MARKER_LINKAGE_NOT_PRESERVED)
        if uncertainty == BootstrapInformationStatus.PARTIAL_INFORMATION:
            limitations.add(LimitationId.UNCERTAINTY_PARTIAL)
        elif uncertainty == BootstrapInformationStatus.INSUFFICIENT_INFORMATION:
            limitations.add(LimitationId.UNCERTAINTY_INSUFFICIENT)
    else:
        uncertainty = "not_run"
        limitations.add(LimitationId.UNCERTAINTY_NOT_RUN)
    if result.provenance.partial_input:
        limitations.add(LimitationId.PARTIAL_INPUT)

    resources = source.resources
    provenance = result.provenance
    diagnostics = deconvolution.diagnostics
    return CellOriginExplorerView(
        status=status,
        binding=binding,
        dot_interval_rows=dot_rows,
        exact_table_rows=table_rows,
        marker_support=MarkerSupport(
            registered_markers=resources.registered_markers,
            usable_markers=resources.usable_markers,
            excluded_incomplete_atlas_markers=(
                resources.excluded_incomplete_atlas_markers
            ),
            collapsed_duplicate_regions=resources.collapsed_duplicate_regions,
            observed_markers=len(deconvolution.marker_ids),
        ),
        fragment_denominators=FragmentDenominators(
            input_fragments=provenance.input_fragment_count,
            marker_overlaps=provenance.marker_overlap_count,
            classified_fragment_marker_observations=(
                provenance.classified_fragment_marker_count
            ),
            excluded_fewer_than_four_cpgs=(
                provenance.excluded_fewer_than_four_cpgs
            ),
        ),
        atlas_coverage=AtlasCoverage(
            usable_markers=resources.usable_markers,
            registered_markers=resources.registered_markers,
            contributor_count=resources.cell_type_count,
            marker_coverage_fraction=(
                resources.usable_markers / resources.registered_markers
            ),
        ),
        diagnostics=SolverDiagnostics(
            converged=diagnostics.converged,
            iterations=diagnostics.iterations,
            residual_l2=diagnostics.residual_l2,
            objective_value=diagnostics.objective_value,
        ),
        uncertainty_status=uncertainty,
        limitations=tuple(sorted(limitations, key=str)),
        request_sha256=request_sha,
    )


def build_cell_origin_explorer(
    request: CellOriginExplorerRequest,
) -> CellOriginExplorerView:
    """Build a deterministic view; unavailable states expose no numeric rows."""

    return _build_replay_view(_to_replay_request(request))


def build_cell_origin_explorer_artifact(
    request: CellOriginExplorerRequest,
) -> CellOriginExplorerArtifact:
    replay_request = _to_replay_request(request)
    return CellOriginExplorerArtifact(
        request=replay_request,
        view=_build_replay_view(replay_request),
    )


def canonical_cell_origin_explorer_bytes(
    artifact: CellOriginExplorerArtifact,
) -> bytes:
    return canonical_json_bytes(artifact)


def cell_origin_explorer_from_canonical_bytes(
    content: bytes,
) -> CellOriginExplorerArtifact:
    try:
        artifact = CellOriginExplorerArtifact.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise CellOriginExplorerError(
            "cell-origin explorer artifact is invalid"
        ) from exc
    if canonical_cell_origin_explorer_bytes(artifact) != content:
        raise CellOriginExplorerError("cell-origin explorer JSON is not canonical")
    return artifact


__all__ = [
    "AXIS_LABEL",
    "AtlasCoverage",
    "CellOriginExplorerArtifact",
    "CellOriginExplorerError",
    "CellOriginExplorerRequest",
    "CellOriginExplorerReplayRequest",
    "CellOriginExplorerView",
    "CellOriginReplaySource",
    "DotIntervalRow",
    "ExactTableRow",
    "ExplorerBinding",
    "ExplorerStatus",
    "FragmentDenominators",
    "LimitationId",
    "MarkerSupport",
    "SolverDiagnostics",
    "build_cell_origin_explorer",
    "build_cell_origin_explorer_artifact",
    "canonical_cell_origin_explorer_bytes",
    "cell_origin_explorer_from_canonical_bytes",
]
