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
    DeconvolutionOutputV2,
)
from .cell_origin_pipeline import CellOriginResultBundle
from .compatibility import ExecutionState, InformationState, TrustState
from .result_view import (
    CompatibilityContract,
    ResultViewRequest,
    build_result_view,
    result_filters_sha256,
)

MAX_CONTRIBUTORS = 512
MAX_LIMITATIONS = 16
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

_PRIVATE_SEGMENT = re.compile(
    r"(?:^|[^a-z0-9])(?:donor|patient|read|sample|sequence|path)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s=:(\[{'\"\\])(?:/[^\s,;)\]}'\"]+|[A-Za-z]:[\\/][^\s,;)\]}'\"]+)"
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
        if len(self.result_view_request.sources) != 1:
            raise ValueError("cell-origin explorer requires exactly one E06 source")
        source = self.result_view_request.sources[0]
        record = source.record
        if record.method.family.value != "cell_origin":
            raise ValueError("E01 method family must be cell_origin")
        if record.compatibility_key.atlas_asset is None:
            raise ValueError("cell-origin compatibility identity requires an atlas")
        if self.bundle is None:
            if (
                record.execution_state == ExecutionState.COMPLETE
                and record.information_state == InformationState.SUFFICIENT
                and record.trust_state == TrustState.VERIFIED
                and record.current_capability.research_inspectable
            ):
                raise ValueError(
                    "eligible cell-origin source requires its exact bundle"
                )
            return self

        result = self.bundle.result
        deconvolution = result.deconvolution
        if not isinstance(deconvolution, DeconvolutionOutputV2):
            raise ValueError("cell-origin explorer requires deconvolution v2")
        if result.bootstrap is not None and not isinstance(
            result.bootstrap, BootstrapResultV2
        ):
            raise ValueError("cell-origin explorer accepts only bootstrap v2")
        _assert_private_data_absent(self.bundle.model_dump(mode="json"))
        if result.result_id != record.result_id:
            raise ValueError("bundle result ID does not match E05 record")
        atlas = record.compatibility_key.atlas_asset
        if (
            atlas.asset_id != deconvolution.atlas_id
            or atlas.content_sha256 != deconvolution.atlas_sha256
        ):
            raise ValueError("atlas identity does not match exact deconvolution")
        _validate_bundle_denominators(self.bundle)
        _validate_bundle_contributors(self.bundle)
        if _digest(result) != record.result_sha256:
            raise ValueError("result digest does not match E05 record")
        if _digest(self.bundle) != record.bundle_sha256:
            raise ValueError("bundle digest does not match E05 record")
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
    request: CellOriginExplorerRequest
    view: CellOriginExplorerView

    @model_validator(mode="after")
    def replay_exactly(self) -> CellOriginExplorerArtifact:
        if build_cell_origin_explorer(self.request) != self.view:
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
        "://" in value
        or _ABSOLUTE_PATH.search(value)
        or (
            _SEQUENCE.search(value)
            and re.fullmatch(r"[0-9a-f]{64}", value) is None
        )
        or _SECRET.search(value)
    ):
        raise ValueError(f"cell-origin bundle contains private data at {field}")


def _validate_bundle_denominators(bundle: CellOriginResultBundle) -> None:
    result = bundle.result
    resources = bundle.resources
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


def _validate_bundle_contributors(bundle: CellOriginResultBundle) -> None:
    result = bundle.result
    estimates = result.deconvolution.estimates
    if len(estimates) > MAX_CONTRIBUTORS:
        raise ValueError("contributor count exceeds explorer bound")
    ids = [item.cell_type_id for item in estimates]
    if len(ids) != len(set(ids)):
        raise ValueError("contributors must be unique")
    if any(_PRIVATE_SEGMENT.search(item) for item in ids):
        raise ValueError("contributor identifier contains a reserved privacy term")
    if bundle.resources.cell_type_count != len(estimates):
        raise ValueError("cell-type denominator mismatch")
    charts = bundle.charts.composition_rows
    if {item.cell_type_id for item in charts} != set(ids) or len(charts) != len(ids):
        raise ValueError("chart contributors do not match exact result")
    by_id = {item.cell_type_id: item for item in charts}
    for estimate in estimates:
        chart = by_id[estimate.cell_type_id]
        if chart.fraction != estimate.fraction:
            raise ValueError("chart estimate does not match exact result")


def _status(request: CellOriginExplorerRequest) -> ExplorerStatus:
    record = request.result_view_request.sources[0].record
    if record.execution_state == ExecutionState.FAILED:
        return ExplorerStatus.FAILED
    if record.execution_state == ExecutionState.NOT_RUN:
        return ExplorerStatus.NOT_RUN
    if record.information_state == InformationState.INSUFFICIENT:
        return ExplorerStatus.INSUFFICIENT_INFORMATION
    filtered = build_result_view(request.result_view_request)
    if (
        record.information_state != InformationState.SUFFICIENT
        or record.trust_state != TrustState.VERIFIED
        or not record.current_capability.research_inspectable
        or filtered.visible_count != 1
        or request.bundle is None
    ):
        return ExplorerStatus.MISSING
    return ExplorerStatus.READY


def _binding(request: CellOriginExplorerRequest) -> ExplorerBinding:
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
            _digest(request.bundle.result.method)
            if request.bundle is not None
            else None
        ),
        atlas_id=atlas.asset_id,
        atlas_sha256=atlas.content_sha256,
        authority_head_sha256=record.current_capability.authority_head_sha256,
        filter_sha256=result_filters_sha256(request.result_view_request.filters),
    )


def build_cell_origin_explorer(
    request: CellOriginExplorerRequest,
) -> CellOriginExplorerView:
    """Build a deterministic view; unavailable states expose no numeric rows."""

    status = _status(request)
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

    bundle = request.bundle
    assert bundle is not None
    result = bundle.result
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

    resources = bundle.resources
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


def build_cell_origin_explorer_artifact(
    request: CellOriginExplorerRequest,
) -> CellOriginExplorerArtifact:
    return CellOriginExplorerArtifact(
        request=request,
        view=build_cell_origin_explorer(request),
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
    "CellOriginExplorerView",
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
