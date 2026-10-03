"""Deterministic, framework-independent fragment comparison view model.

The explorer consumes already-verified E02 aggregate bundle content and E05
measurement identities. It never verifies files, chooses methods, reads raw
records, or interprets a distribution. Filters only change which canonical
bins are displayed; the signed eligible denominator is never recomputed.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import Field, ValidationError, model_validator

from evidence_inspector.compatibility import (
    CompatibilityContract,
    CompatibilityDecision,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    ResultId,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
    decide_compatibility,
)
from evidence_inspector.method_registry import MethodReference, Sha256
from traceback_runner.bundles import BundleManifest, VerifiedBundle
from traceback_runner.contracts import (
    FragmentMeasurement,
    ResultBundleManifestV2,
    canonical_json_bytes,
)
from traceback_runner.export import FragmentLengthChart

MAX_EXPLORER_SOURCES = 16
MAX_EXPLORER_BINS = 4096
MAX_BIN_COUNT = 2**63 - 1

_MEASUREMENT_PATH = "measurements/fragment-length.v1.json"
_CHART_PATH = "charts/fragment-length.v1.json"
_RESERVED_PRIVACY_TERMS = (
    "donor",
    "sample",
    "patient",
    "run",
    "read",
    "path",
    "sequence",
)
_SAFE_RESERVED_PREFIX_LEXEMES = frozenset(
    {
        "pathology",
        "readiness",
        "readout",
        "runner",
        "runtime",
        "sampled",
        "sequencer",
    }
)


class FragmentQuantity(StrEnum):
    """Three distinct length quantities; values are never interchangeable."""

    RAW_QUERY_LENGTH = "raw_query_length"
    ALIGNED_QUERY_LENGTH = "aligned_query_length"
    ALIGNED_REFERENCE_SPAN = "aligned_reference_span"


_QUANTITY_ID = {
    FragmentQuantity.RAW_QUERY_LENGTH: "qty_fragment_raw_query_length",
    FragmentQuantity.ALIGNED_QUERY_LENGTH: "qty_fragment_aligned_query_length",
    FragmentQuantity.ALIGNED_REFERENCE_SPAN: (
        "qty_fragment_aligned_reference_span"
    ),
}
_DEFINITION_ID = {
    FragmentQuantity.RAW_QUERY_LENGTH: "raw-query-length.v1",
    FragmentQuantity.ALIGNED_QUERY_LENGTH: "aligned-query-length.v1",
    FragmentQuantity.ALIGNED_REFERENCE_SPAN: "aligned-reference-span.v1",
}


class ExplorerSourceState(StrEnum):
    COMPLETE = "complete"
    LOADING = "loading"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"
    REVOKED = "revoked"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class PanelId(StrEnum):
    A = "a"
    B = "b"


class WithholdingCode(StrEnum):
    LOADING = "loading"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"
    REVOKED = "revoked"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class FragmentExplorerError(ValueError):
    """An explorer input or replay failed closed."""


def _digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _contract_digest(
    contract: CompatibilityContract, *, exclude: set[str] | None = None
) -> str:
    payload = contract.model_dump(mode="json", exclude=exclude or set())
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _digest_bytes(encoded)


def _check_private_string(value: str) -> None:
    if (
        value.startswith("/")
        or "\\" in value
        or "://" in value
        or re.match(r"^[A-Za-z]:/", value) is not None
    ):
        raise ValueError("explorer values cannot contain paths or URIs")
    for segment in re.split(r"[^a-z0-9]+", value.lower()):
        if segment in _SAFE_RESERVED_PREFIX_LEXEMES:
            continue
        if any(segment.startswith(term) for term in _RESERVED_PRIVACY_TERMS):
            raise ValueError("explorer value contains a reserved privacy term")


def _check_private_values(value: object) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _check_private_values(nested)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _check_private_values(nested)
    elif isinstance(value, str):
        _check_private_string(value)


class ExplorerMethodSelection(CompatibilityContract):
    result_id: ResultId
    method_ref: MethodReference


class ExplorerControls(CompatibilityContract):
    """Half-open bin window plus a display-only count threshold.

    Neither control changes ``eligible_alignments`` or any bin fraction's
    denominator. ``bin_end_exclusive`` is an array index, not a base-pair end.
    """

    bin_start_inclusive: int = Field(ge=0, lt=MAX_EXPLORER_BINS)
    bin_end_exclusive: int = Field(gt=0, le=MAX_EXPLORER_BINS)
    minimum_count_inclusive: int = Field(default=0, ge=0, le=MAX_BIN_COUNT)

    @model_validator(mode="after")
    def valid_half_open_window(self) -> ExplorerControls:
        if self.bin_start_inclusive >= self.bin_end_exclusive:
            raise ValueError("bin range must be non-empty and half-open")
        return self


class FragmentExplorerState(CompatibilityContract):
    schema_version: Literal["traceback.fragment-explorer-state.v1"] = (
        "traceback.fragment-explorer-state.v1"
    )
    left: ExplorerMethodSelection
    right: ExplorerMethodSelection
    filters_linked: bool
    linked_controls: ExplorerControls | None
    left_controls: ExplorerControls | None
    right_controls: ExplorerControls | None
    state_sha256: Sha256

    @model_validator(mode="after")
    def exact_control_mode_and_digest(self) -> FragmentExplorerState:
        if self.left.result_id == self.right.result_id:
            raise ValueError("two explicit selections require distinct results")
        if self.filters_linked:
            if self.linked_controls is None or any(
                item is not None
                for item in (self.left_controls, self.right_controls)
            ):
                raise ValueError(
                    "linked filters require only linked_controls"
                )
        elif self.linked_controls is not None or any(
            item is None for item in (self.left_controls, self.right_controls)
        ):
            raise ValueError(
                "unlinked filters require both panel-local controls"
            )
        if self.state_sha256 != _contract_digest(
            self, exclude={"state_sha256"}
        ):
            raise ValueError("explorer state digest does not match canonical state")
        _check_private_values(self.model_dump(mode="json"))
        return self

    def controls_for(self, panel: PanelId) -> ExplorerControls:
        if self.filters_linked:
            assert self.linked_controls is not None
            return self.linked_controls
        controls = (
            self.left_controls if panel == PanelId.A else self.right_controls
        )
        assert controls is not None
        return controls


def build_fragment_explorer_state(
    *,
    left: ExplorerMethodSelection,
    right: ExplorerMethodSelection,
    filters_linked: bool,
    linked_controls: ExplorerControls | None = None,
    left_controls: ExplorerControls | None = None,
    right_controls: ExplorerControls | None = None,
) -> FragmentExplorerState:
    payload: dict[str, Any] = {
        "left": left,
        "right": right,
        "filters_linked": filters_linked,
        "linked_controls": linked_controls,
        "left_controls": left_controls,
        "right_controls": right_controls,
    }
    draft = FragmentExplorerState.model_construct(
        schema_version="traceback.fragment-explorer-state.v1",
        **payload,
        state_sha256="0" * 64,
    )
    return FragmentExplorerState(
        **payload,
        state_sha256=_contract_digest(draft, exclude={"state_sha256"}),
    )


class VerifiedFragmentSource(CompatibilityContract):
    """Path-free E02 chart/table content bound to one exact E05 record."""

    schema_version: Literal["traceback.verified-fragment-source.v1"] = (
        "traceback.verified-fragment-source.v1"
    )
    record: VerifiedMeasurementRecord
    quantity: FragmentQuantity
    state: ExplorerSourceState
    manifest: BundleManifest | None
    measurement: FragmentMeasurement | None
    chart: FragmentLengthChart | None

    @model_validator(mode="after")
    def bind_verified_content_and_state(self) -> VerifiedFragmentSource:
        numeric = (self.manifest, self.measurement, self.chart)
        has_all_numeric = all(item is not None for item in numeric)
        has_any_numeric = any(item is not None for item in numeric)
        data_states = {
            ExplorerSourceState.COMPLETE,
            ExplorerSourceState.INSUFFICIENT,
            ExplorerSourceState.REVOKED,
            ExplorerSourceState.UNVERIFIED,
        }
        if self.state in data_states and not has_all_numeric:
            raise ValueError("this source state requires complete verified content")
        if self.state not in data_states and has_any_numeric:
            raise ValueError("withheld source states cannot carry numerical content")
        if has_all_numeric:
            if self.record.execution_state != ExecutionState.COMPLETE:
                raise ValueError("verified numerical content requires completed execution")
            if self.record.trust_state == TrustState.REVOKED:
                expected_state = ExplorerSourceState.REVOKED
            elif self.record.information_state != InformationState.SUFFICIENT:
                expected_state = ExplorerSourceState.INSUFFICIENT
            elif self.record.trust_state != TrustState.VERIFIED:
                expected_state = ExplorerSourceState.UNVERIFIED
            else:
                expected_state = ExplorerSourceState.COMPLETE
        elif self.record.execution_state == ExecutionState.FAILED:
            expected_state = ExplorerSourceState.FAILED
        else:
            expected_state = ExplorerSourceState.UNAVAILABLE
        if self.state != expected_state:
            raise ValueError(
                "source state must be derived from verified content and E05 state"
            )

        expected_quantity_id = _QUANTITY_ID[self.quantity]
        if (
            self.record.method.quantity_id != expected_quantity_id
            or self.record.compatibility_key.quantity_id != expected_quantity_id
            or self.record.method.unit != "unit_bp"
            or self.record.compatibility_key.unit != "unit_bp"
        ):
            raise ValueError("fragment quantity identity or base-pair unit differs")

        if has_all_numeric:
            assert self.manifest is not None
            assert self.measurement is not None
            assert self.chart is not None
            if len(self.chart.rows) > MAX_EXPLORER_BINS:
                raise ValueError("fragment source exceeds explorer bin bound")
            if self.measurement.definition_id != _DEFINITION_ID[self.quantity]:
                raise ValueError("measurement definition conflates fragment quantities")
            measurement_bytes = canonical_json_bytes(self.measurement)
            measurement_sha256 = _digest_bytes(measurement_bytes)
            if self.chart.measurement_sha256 != measurement_sha256:
                raise ValueError("chart is not bound to the canonical measurement")
            expected_rows = tuple(
                (
                    item.bin.lower_inclusive,
                    item.bin.upper_exclusive,
                    item.count,
                )
                for item in self.measurement.histogram
            )
            actual_rows = tuple(
                (item.lower_inclusive, item.upper_exclusive, item.count)
                for item in self.chart.rows
            )
            if actual_rows != expected_rows:
                raise ValueError("E02 chart and measurement table are not exact peers")
            content = {item.relative_path: item for item in self.manifest.contents}
            measurement_entry = content.get(_MEASUREMENT_PATH)
            chart_entry = content.get(_CHART_PATH)
            if (
                measurement_entry is None
                or chart_entry is None
                or measurement_entry.sha256 != measurement_sha256
                or chart_entry.sha256 != _digest_bytes(
                    canonical_json_bytes(self.chart)
                )
            ):
                raise ValueError("manifest does not bind exact chart/table bytes")
            if self.record.result_sha256 != measurement_sha256:
                raise ValueError("E05 result identity does not bind E02 measurement")
            if self.record.bundle_sha256 != _digest_bytes(
                canonical_json_bytes(self.manifest)
            ):
                raise ValueError("E05 bundle identity does not bind E02 manifest")
            if isinstance(self.manifest, ResultBundleManifestV2) and (
                self.manifest.method.method_id != self.record.method.method_id
                or self.manifest.method.version != self.record.method.version
                or self.manifest.method.method_definition_sha256
                != self.record.method_definition_sha256
            ):
                raise ValueError("E02 manifest method does not bind E05 method identity")
        _check_private_values(self.model_dump(mode="json"))
        return self


def fragment_source_from_verified_bundle(
    bundle: VerifiedBundle,
    *,
    record: VerifiedMeasurementRecord,
    quantity: FragmentQuantity,
    state: ExplorerSourceState = ExplorerSourceState.COMPLETE,
) -> VerifiedFragmentSource:
    """Drop the verified local path and retain only exact aggregate content."""

    if not isinstance(bundle.manifest, ResultBundleManifestV2):
        raise FragmentExplorerError("verified E02 bundle lacks exact method identity")

    return VerifiedFragmentSource(
        record=record,
        quantity=quantity,
        state=state,
        manifest=bundle.manifest,
        measurement=bundle.measurement,
        chart=bundle.chart,
    )


class FragmentExplorerRequest(CompatibilityContract):
    schema_version: Literal["traceback.fragment-explorer-request.v1"] = (
        "traceback.fragment-explorer-request.v1"
    )
    sources: tuple[VerifiedFragmentSource, ...] = Field(
        min_length=2, max_length=MAX_EXPLORER_SOURCES
    )
    policy: CompatibilityPolicy
    trusted_policy_sha256: Sha256
    trusted_authority_head_sha256: Sha256
    state: FragmentExplorerState

    @model_validator(mode="after")
    def exact_explicit_sources(self) -> FragmentExplorerRequest:
        source_ids = [item.record.result_id for item in self.sources]
        if source_ids != sorted(source_ids) or len(source_ids) != len(
            set(source_ids)
        ):
            raise ValueError("explorer sources must be uniquely sorted")
        by_id = {item.record.result_id: item for item in self.sources}
        for selection in (self.state.left, self.state.right):
            source = by_id.get(selection.result_id)
            if source is None:
                raise ValueError("explicit method selection is absent from sources")
            if source.record.method.method_ref != selection.method_ref:
                raise ValueError("explicit method selection does not match result")
        if self.trusted_policy_sha256 != compatibility_policy_sha256(self.policy):
            raise ValueError("explorer requires the exact trusted E05 policy")
        return self


class ExplorerBinRow(CompatibilityContract):
    lower_inclusive: int = Field(ge=0)
    upper_exclusive: int | None = Field(default=None, gt=0)
    count: int = Field(ge=0, le=MAX_BIN_COUNT)
    fraction_numerator: int = Field(ge=0, le=MAX_BIN_COUNT)
    fraction_denominator: int = Field(gt=0, le=MAX_BIN_COUNT)

    @model_validator(mode="after")
    def exact_fraction(self) -> ExplorerBinRow:
        if self.fraction_numerator != self.count:
            raise ValueError("bin fraction numerator must equal exact count")
        if (
            self.upper_exclusive is not None
            and self.lower_inclusive >= self.upper_exclusive
        ):
            raise ValueError("bin bounds must be half-open")
        return self


class ExplorerDenominator(CompatibilityContract):
    records_scanned: int = Field(ge=0, le=MAX_BIN_COUNT)
    eligible_alignments: int = Field(gt=0, le=MAX_BIN_COUNT)
    excluded_alignments: int = Field(ge=0, le=MAX_BIN_COUNT)
    displayed_alignments: int = Field(ge=0, le=MAX_BIN_COUNT)
    outside_display_alignments: int = Field(ge=0, le=MAX_BIN_COUNT)

    @model_validator(mode="after")
    def reconcile_totals(self) -> ExplorerDenominator:
        if self.records_scanned != (
            self.eligible_alignments + self.excluded_alignments
        ):
            raise ValueError("scanned total must reconcile exact denominator")
        if self.eligible_alignments != (
            self.displayed_alignments + self.outside_display_alignments
        ):
            raise ValueError("display totals must preserve eligible denominator")
        return self


class ExplorerPanelView(CompatibilityContract):
    panel: PanelId
    selection: ExplorerMethodSelection
    quantity: FragmentQuantity
    source_state: ExplorerSourceState
    controls: ExplorerControls
    rows: tuple[ExplorerBinRow, ...] = Field(max_length=MAX_EXPLORER_BINS)
    denominator: ExplorerDenominator | None
    withholding_code: WithholdingCode | None
    y_axis_max: int | None = Field(default=None, ge=1, le=MAX_BIN_COUNT)

    @model_validator(mode="after")
    def numeric_or_withheld(self) -> ExplorerPanelView:
        if self.source_state == ExplorerSourceState.COMPLETE:
            if self.denominator is None or self.withholding_code is not None:
                raise ValueError("complete panels require an exact denominator")
            if self.y_axis_max is None:
                raise ValueError("complete panels require a numerical y axis")
            if sum(item.count for item in self.rows) != (
                self.denominator.displayed_alignments
            ):
                raise ValueError("panel rows must reconcile displayed total")
            if any(
                item.fraction_denominator
                != self.denominator.eligible_alignments
                for item in self.rows
            ):
                raise ValueError(
                    "row fractions must use the panel eligible denominator"
                )
        elif (
            self.rows
            or self.denominator is not None
            or self.y_axis_max is not None
            or self.withholding_code is None
        ):
            raise ValueError("non-complete panels must withhold every number")
        elif self.withholding_code != WithholdingCode(self.source_state.value):
            raise ValueError("withholding code must match the exact source state")
        return self


class AccessibleTableRow(CompatibilityContract):
    panel: PanelId
    lower_inclusive: int = Field(ge=0)
    upper_exclusive: int | None = Field(default=None, gt=0)
    count: int = Field(ge=0, le=MAX_BIN_COUNT)
    fraction_numerator: int = Field(ge=0, le=MAX_BIN_COUNT)
    fraction_denominator: int = Field(gt=0, le=MAX_BIN_COUNT)


class ExplorerDeltaRow(CompatibilityContract):
    lower_inclusive: int = Field(ge=0)
    upper_exclusive: int | None = Field(default=None, gt=0)
    left_count: int = Field(ge=0, le=MAX_BIN_COUNT)
    right_count: int = Field(ge=0, le=MAX_BIN_COUNT)
    count_delta_right_minus_left: int = Field(
        ge=-MAX_BIN_COUNT, le=MAX_BIN_COUNT
    )

    @model_validator(mode="after")
    def exact_delta(self) -> ExplorerDeltaRow:
        if self.count_delta_right_minus_left != self.right_count - self.left_count:
            raise ValueError("fragment delta must be exact right minus left")
        return self


class FragmentExplorerView(CompatibilityContract):
    schema_version: Literal["traceback.fragment-explorer-view.v1"] = (
        "traceback.fragment-explorer-view.v1"
    )
    request: FragmentExplorerRequest
    state: FragmentExplorerState
    compatibility: CompatibilityDecision
    left: ExplorerPanelView
    right: ExplorerPanelView
    synchronized_comparison: bool
    shared_y_scale: bool
    accessible_rows: tuple[AccessibleTableRow, ...] = Field(
        max_length=MAX_EXPLORER_BINS * 2
    )
    delta_rows: tuple[ExplorerDeltaRow, ...] = Field(
        max_length=MAX_EXPLORER_BINS
    )
    view_sha256: Sha256

    @model_validator(mode="after")
    def exact_table_axes_and_digest(self) -> FragmentExplorerView:
        if self.state != self.request.state:
            raise ValueError("view state must match the embedded replay request")
        sources = {
            item.record.result_id: item for item in self.request.sources
        }
        selected_sources = (
            sources[self.state.left.result_id],
            sources[self.state.right.result_id],
        )
        decision_bindings = {
            self.compatibility.binding.left.result_id: (
                self.compatibility.binding.left
            ),
            self.compatibility.binding.right.result_id: (
                self.compatibility.binding.right
            ),
        }
        for selection, source in zip(
            (self.state.left, self.state.right), selected_sources, strict=True
        ):
            binding = decision_bindings.get(selection.result_id)
            if binding is None or binding.method_ref != selection.method_ref:
                raise ValueError(
                    "compatibility binding must match both state selections"
                )
            if (
                binding.result_sha256 != source.record.result_sha256
                or binding.bundle_id != source.record.bundle_id
                or binding.bundle_sha256 != source.record.bundle_sha256
                or binding.method_definition_sha256
                != source.record.method_definition_sha256
            ):
                raise ValueError(
                    "compatibility binding must match exact verified sources"
                )
        expected_compatibility = decide_compatibility(
            CompatibilityRequest(
                left=selected_sources[0].record,
                right=selected_sources[1].record,
                policy=self.request.policy,
                trusted_policy_sha256=self.request.trusted_policy_sha256,
                trusted_authority_head_sha256=(
                    self.request.trusted_authority_head_sha256
                ),
            )
        )
        if self.compatibility != expected_compatibility:
            raise ValueError(
                "compatibility decision must replay from embedded sources"
            )
        expected_left = _panel_view(
            PanelId.A,
            self.state.left,
            selected_sources[0],
            self.state.controls_for(PanelId.A),
        )
        expected_right = _panel_view(
            PanelId.B,
            self.state.right,
            selected_sources[1],
            self.state.controls_for(PanelId.B),
        )
        expected_comparable_display = (
            self.compatibility.outcome == CompatibilityOutcome.COMPARABLE
            and expected_left.source_state == ExplorerSourceState.COMPLETE
            and expected_right.source_state == ExplorerSourceState.COMPLETE
            and self.state.filters_linked
            and expected_left.controls == expected_right.controls
            and _same_bin_layout(expected_left.rows, expected_right.rows)
        )
        expected_shared = (
            expected_comparable_display
            and self.compatibility.shared_axis_allowed
        )
        if expected_shared:
            expected_axis = max(
                expected_left.y_axis_max or 1,
                expected_right.y_axis_max or 1,
            )
            expected_left = expected_left.model_copy(
                update={"y_axis_max": expected_axis}
            )
            expected_right = expected_right.model_copy(
                update={"y_axis_max": expected_axis}
            )
        if self.left.panel != PanelId.A or self.right.panel != PanelId.B:
            raise ValueError("view panels must retain fixed A/B identities")
        if self.left.selection != self.state.left:
            raise ValueError("left panel selection must match embedded state")
        if self.right.selection != self.state.right:
            raise ValueError("right panel selection must match embedded state")
        if self.left.controls != self.state.controls_for(PanelId.A):
            raise ValueError("left panel controls must match embedded state")
        if self.right.controls != self.state.controls_for(PanelId.B):
            raise ValueError("right panel controls must match embedded state")
        if (
            self.left.quantity != selected_sources[0].quantity
            or self.left.source_state != selected_sources[0].state
        ):
            raise ValueError("left panel must match its exact verified source")
        if (
            self.right.quantity != selected_sources[1].quantity
            or self.right.source_state != selected_sources[1].state
        ):
            raise ValueError("right panel must match its exact verified source")
        if self.left != expected_left or self.right != expected_right:
            raise ValueError(
                "panel output must replay exactly from verified sources"
            )
        expected_table = tuple(
            AccessibleTableRow(panel=panel.panel, **row.model_dump())
            for panel in (self.left, self.right)
            for row in panel.rows
        )
        if self.accessible_rows != expected_table:
            raise ValueError("accessible table must exactly equal plotted series")
        if self.synchronized_comparison != expected_comparable_display:
            raise ValueError(
                "synchronized comparison must exactly follow linked compatibility"
            )
        if self.shared_y_scale != expected_shared:
            raise ValueError("shared y scale must exactly follow compatibility")
        left_local_max = max(
            (item.count for item in self.left.rows), default=1
        ) or 1
        right_local_max = max(
            (item.count for item in self.right.rows), default=1
        ) or 1
        if self.left.source_state == ExplorerSourceState.COMPLETE and (
            self.left.y_axis_max
            != (
                max(left_local_max, right_local_max)
                if expected_shared
                else left_local_max
            )
        ):
            raise ValueError("left y axis must match its exact display mode")
        if self.right.source_state == ExplorerSourceState.COMPLETE and (
            self.right.y_axis_max
            != (
                max(left_local_max, right_local_max)
                if expected_shared
                else right_local_max
            )
        ):
            raise ValueError("right y axis must match its exact display mode")
        expected_deltas: tuple[ExplorerDeltaRow, ...] = ()
        if expected_comparable_display and self.compatibility.delta_allowed:
            expected_deltas = tuple(
                ExplorerDeltaRow(
                    lower_inclusive=left_row.lower_inclusive,
                    upper_exclusive=left_row.upper_exclusive,
                    left_count=left_row.count,
                    right_count=right_row.count,
                    count_delta_right_minus_left=(
                        right_row.count - left_row.count
                    ),
                )
                for left_row, right_row in zip(
                    self.left.rows, self.right.rows, strict=True
                )
            )
        if self.delta_rows != expected_deltas:
            raise ValueError("delta rows must exactly match compatible panel rows")
        if self.view_sha256 != _contract_digest(
            self, exclude={"view_sha256"}
        ):
            raise ValueError("explorer view digest does not match canonical view")
        _check_private_values(self.model_dump(mode="json"))
        return self


def _withholding_code(state: ExplorerSourceState) -> WithholdingCode:
    if state == ExplorerSourceState.COMPLETE:
        raise AssertionError("complete state is not withheld")
    return WithholdingCode(state.value)


def _panel_view(
    panel: PanelId,
    selection: ExplorerMethodSelection,
    source: VerifiedFragmentSource,
    controls: ExplorerControls,
) -> ExplorerPanelView:
    if source.state != ExplorerSourceState.COMPLETE:
        return ExplorerPanelView(
            panel=panel,
            selection=selection,
            quantity=source.quantity,
            source_state=source.state,
            controls=controls,
            rows=(),
            denominator=None,
            withholding_code=_withholding_code(source.state),
            y_axis_max=None,
        )
    assert source.measurement is not None
    assert source.chart is not None
    if controls.bin_end_exclusive > len(source.chart.rows):
        raise FragmentExplorerError("bin window exceeds verified chart rows")
    candidates = source.chart.rows[
        controls.bin_start_inclusive : controls.bin_end_exclusive
    ]
    rows = tuple(
        ExplorerBinRow(
            lower_inclusive=item.lower_inclusive,
            upper_exclusive=item.upper_exclusive,
            count=item.count,
            fraction_numerator=item.count,
            fraction_denominator=source.measurement.eligible_alignments,
        )
        for item in candidates
        if item.count >= controls.minimum_count_inclusive
    )
    displayed = sum(item.count for item in rows)
    denominator = ExplorerDenominator(
        records_scanned=source.measurement.records_scanned,
        eligible_alignments=source.measurement.eligible_alignments,
        excluded_alignments=source.measurement.exclusions.total,
        displayed_alignments=displayed,
        outside_display_alignments=(
            source.measurement.eligible_alignments - displayed
        ),
    )
    return ExplorerPanelView(
        panel=panel,
        selection=selection,
        quantity=source.quantity,
        source_state=source.state,
        controls=controls,
        rows=rows,
        denominator=denominator,
        withholding_code=None,
        y_axis_max=max((item.count for item in rows), default=1) or 1,
    )


def _same_bin_layout(
    left: tuple[ExplorerBinRow, ...], right: tuple[ExplorerBinRow, ...]
) -> bool:
    return tuple(
        (item.lower_inclusive, item.upper_exclusive) for item in left
    ) == tuple((item.lower_inclusive, item.upper_exclusive) for item in right)


def build_fragment_explorer_view(
    request: FragmentExplorerRequest,
) -> FragmentExplorerView:
    """Transform explicit verified sources and state into one replayable view."""

    by_id = {item.record.result_id: item for item in request.sources}
    left_source = by_id[request.state.left.result_id]
    right_source = by_id[request.state.right.result_id]
    compatibility = decide_compatibility(
        CompatibilityRequest(
            left=left_source.record,
            right=right_source.record,
            policy=request.policy,
            trusted_policy_sha256=request.trusted_policy_sha256,
            trusted_authority_head_sha256=request.trusted_authority_head_sha256,
        )
    )
    left = _panel_view(
        PanelId.A,
        request.state.left,
        left_source,
        request.state.controls_for(PanelId.A),
    )
    right = _panel_view(
        PanelId.B,
        request.state.right,
        right_source,
        request.state.controls_for(PanelId.B),
    )
    exact_layout = _same_bin_layout(left.rows, right.rows)
    comparable_display = (
        compatibility.outcome == CompatibilityOutcome.COMPARABLE
        and left.source_state == ExplorerSourceState.COMPLETE
        and right.source_state == ExplorerSourceState.COMPLETE
        and request.state.filters_linked
        and left.controls == right.controls
        and exact_layout
    )
    shared_y_scale = comparable_display and compatibility.shared_axis_allowed
    if shared_y_scale:
        shared_max = max(left.y_axis_max or 1, right.y_axis_max or 1)
        left = left.model_copy(update={"y_axis_max": shared_max})
        right = right.model_copy(update={"y_axis_max": shared_max})
    deltas: tuple[ExplorerDeltaRow, ...] = ()
    if comparable_display and compatibility.delta_allowed:
        deltas = tuple(
            ExplorerDeltaRow(
                lower_inclusive=left_row.lower_inclusive,
                upper_exclusive=left_row.upper_exclusive,
                left_count=left_row.count,
                right_count=right_row.count,
                count_delta_right_minus_left=(
                    right_row.count - left_row.count
                ),
            )
            for left_row, right_row in zip(left.rows, right.rows, strict=True)
        )
    accessible = tuple(
        AccessibleTableRow(panel=panel.panel, **row.model_dump())
        for panel in (left, right)
        for row in panel.rows
    )
    payload: dict[str, Any] = {
        "request": request,
        "state": request.state,
        "compatibility": compatibility,
        "left": left,
        "right": right,
        "synchronized_comparison": comparable_display,
        "shared_y_scale": shared_y_scale,
        "accessible_rows": accessible,
        "delta_rows": deltas,
    }
    draft = FragmentExplorerView.model_construct(
        schema_version="traceback.fragment-explorer-view.v1",
        **payload,
        view_sha256="0" * 64,
    )
    return FragmentExplorerView(
        **payload,
        view_sha256=_contract_digest(draft, exclude={"view_sha256"}),
    )


ExplorerContractT = TypeVar("ExplorerContractT", bound=CompatibilityContract)


def canonical_fragment_explorer_bytes(contract: CompatibilityContract) -> bytes:
    _check_private_values(contract.model_dump(mode="json"))
    return canonical_json_bytes(contract)


def fragment_explorer_from_canonical_bytes(
    model: type[ExplorerContractT], content: bytes
) -> ExplorerContractT:
    try:
        parsed = model.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        raise FragmentExplorerError("fragment explorer JSON is invalid") from exc
    if canonical_fragment_explorer_bytes(parsed) != content:
        raise FragmentExplorerError("fragment explorer JSON is not canonical")
    return parsed


def replay_fragment_explorer_view(
    request: FragmentExplorerRequest, view: FragmentExplorerView
) -> FragmentExplorerView:
    expected = build_fragment_explorer_view(request)
    if view != expected:
        raise FragmentExplorerError(
            "fragment explorer view does not match canonical replay"
        )
    return view


__all__ = [
    "AccessibleTableRow",
    "ExplorerBinRow",
    "ExplorerControls",
    "ExplorerDeltaRow",
    "ExplorerDenominator",
    "ExplorerMethodSelection",
    "ExplorerPanelView",
    "ExplorerSourceState",
    "FragmentExplorerError",
    "FragmentExplorerRequest",
    "FragmentExplorerState",
    "FragmentExplorerView",
    "FragmentQuantity",
    "PanelId",
    "VerifiedFragmentSource",
    "WithholdingCode",
    "build_fragment_explorer_state",
    "build_fragment_explorer_view",
    "canonical_fragment_explorer_bytes",
    "fragment_explorer_from_canonical_bytes",
    "fragment_source_from_verified_bundle",
    "replay_fragment_explorer_view",
]
