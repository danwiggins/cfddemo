"""Closed E12 source-value projections from exact E07/E08/E09 artifacts.

This module is the family-specific value adapter that
``docs/E12-INTEGRATION-PLAN.md`` ("Closed source-value projection family")
requires.  It is a set of pure, closed functions with no registry and no
generic numeric fallback: a value leaves an artifact only through one of four
versioned projection contracts, and only for a coordinate and statistic named
by a ``ResolvedProjectionPolicy`` from the protected projection-policy
registry.

The adapter takes an artifact and a policy.  It does not discover where an
artifact lives and does not re-verify E04/E06 live source authority: the E12
builder must pass an artifact it resolved from the family-source registry
under the composite authority fence, plus the D05 anchor of its live-resolved
manifest and the E04/E06 source measurement identity.  See
``docs/SOURCE-VALUE-PROJECTION.md``.

Threat model: in-process code mutation is out of scope.  Caller-built objects
(``model_construct`` forgeries, subclasses, mutated copies) are not trusted:
every artifact and policy is captured as canonical bytes, reparsed and replayed
before a value is read.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator

from evidence_inspector.cell_origin_explorer import (
    CellOriginExplorerArtifact,
    CellOriginExplorerError,
    DotIntervalRow,
    ExactTableRow,
    ExplorerStatus,
    _build_replay_view,
    canonical_cell_origin_explorer_bytes,
    cell_origin_explorer_from_canonical_bytes,
)
from evidence_inspector.cell_origin_models import (
    BootstrapInformationStatus,
    BootstrapResultV2,
    DeconvolutionOutputV2,
)
from evidence_inspector.cna_explorer import (
    MAX_SEGMENTS,
    CnaExplorerError,
    CnaExplorerSnapshot,
    CnaSource,
    CoordinateGridLayer,
    ExplorerAvailability,
    ExplorerInputAuthority,
    cna_explorer_snapshot_bytes,
    cna_explorer_snapshot_from_bytes,
    replay_cna_explorer_snapshot,
)
from evidence_inspector.cohort_manifest import MeasurementAnchor
from evidence_inspector.compatibility import VerifiedMeasurementRecord
from evidence_inspector.copy_number_qc import DosageQcResultBundle
from evidence_inspector.fragment_explorer import (
    MAX_EXPLORER_BINS,
    ExplorerSourceState,
    FragmentExplorerError,
    FragmentExplorerView,
    FragmentQuantity,
    PanelId,
    canonical_fragment_explorer_bytes,
    fragment_explorer_from_canonical_bytes,
    replay_fragment_explorer_view,
)
from evidence_inspector.ichor_adapter import CnvDevelopmentResult
from evidence_inspector.method_registry import (
    MethodReference,
    QuantityId,
    RegistryContract,
    Sha256,
    UnitId,
    canonical_contract_bytes,
)
from evidence_inspector.projection_policy_registry import (
    _STATISTIC_UNIT,
    MAX_OBJECT_BYTES,
    MAX_OBJECT_COLLECTION_ITEMS,
    MAX_OBJECT_GRAPH_DEPTH,
    MAX_OBJECT_GRAPH_NODES,
    MAX_OBJECT_STRING_BYTES,
    MAX_REGISTERED_POLICIES,
    CellOriginProjectionPolicyV1,
    CellOriginStatistic,
    ChromosomeId,
    CnaChromosomeProjectionPolicyV1,
    CnaChromosomeStatistic,
    CnaSegmentCoordinate,
    CnaSegmentProjectionPolicyV1,
    CnaSegmentStatistic,
    FragmentBinCoordinate,
    FragmentProjectionPolicyV1,
    FragmentStatistic,
    PolicySelectorId,
    PolicyVersion,
    ProjectionFamily,
    ProjectionPolicyRegistryConflict,
    ProjectionSelectionRule,
    RegistryId,
    ResolvedProjectionPolicy,
    StatisticUnit,
    cna_coordinate_grid_sha256,
    require_projection_policy_binding,
)
from evidence_inspector.safe_ingress import contract_type_graph, exact_model_bytes
from traceback_runner.serialization import canonical_json_bytes

# The largest complete vector any family artifact can carry: every E09 segment
# times every segment statistic.  Each family is also bounded by its own
# artifact contract (E07 bins, E08 contributors, 22 chromosomes).
MAX_PROJECTED_COMPONENTS = MAX_SEGMENTS * len(CnaSegmentStatistic)
_CHROMOSOMES = tuple(f"chr{index}" for index in range(1, 23))
_REPLAY_FAILURES = (
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    AssertionError,
)


class SourceValueProjectionError(ValueError):
    """A source-value projection failed closed before any value was returned."""


class SourceValuePolicyRejected(SourceValueProjectionError):
    """The policy is not one exact, validated resolved registry policy."""


class SourceValueFamilyMismatch(SourceValueProjectionError):
    """Wrong artifact family, panel, atlas, CNA source or coordinate grid."""


class SourceValueMeasurementMismatch(SourceValueProjectionError):
    """The D05 anchor, D02 tuple, quantity or unit does not bind the source."""


class SourceValueCoordinateUnresolved(SourceValueProjectionError):
    """A coordinate matched zero or several rows, shifted, or is unregistered."""


class SourceValueRepresentationDrift(SourceValueProjectionError):
    """The chart/layer and exact table representations of one value differ."""


class SourceValueWithheld(SourceValueProjectionError):
    """The artifact withholds every value for this coordinate."""


class SourceValueReplayRejected(SourceValueProjectionError):
    """The artifact did not reparse canonically or replay exactly."""


class SourceValueProjectionForged(SourceValueProjectionError):
    """A projection does not equal the adapter's own replayed output."""


# --- public input contracts ------------------------------------------------


class SourceMeasurementIdentity(RegistryContract):
    """The E04/E06 source's method, quantity and unit, supplied by the builder.

    The builder takes these from the live-resolved source, not from the policy;
    they are compared with the policy (``require_projection_policy_binding``)
    and, for E07/E08, with the E05 record embedded in the artifact.
    """

    schema_version: Literal["traceback.e12-source-measurement-identity.v1"] = (
        "traceback.e12-source-measurement-identity.v1"
    )
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId


@dataclass(frozen=True, slots=True)
class CnaReplayInputs:
    """The exact E09 upstream inputs that ``replay_cna_explorer_snapshot`` needs."""

    dosage: DosageQcResultBundle
    segmented: CnvDevelopmentResult
    dosage_authority: ExplorerInputAuthority
    segmented_authority: ExplorerInputAuthority


# --- projection contracts --------------------------------------------------


class ProjectionPolicyBindingV1(RegistryContract):
    """The registered policy a projection was derived from."""

    schema_version: Literal["traceback.e12-projection-policy-binding.v1"] = (
        "traceback.e12-projection-policy-binding.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_POLICIES, strict=True)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    policy_version: PolicyVersion
    object_sha256: Sha256
    policy_sha256: Sha256
    selection_rule: ProjectionSelectionRule


class ProjectionMeasurementV1(RegistryContract):
    """The exact D02 tuple and D05 anchor the projected value belongs to."""

    schema_version: Literal["traceback.e12-projection-measurement.v1"] = (
        "traceback.e12-projection-measurement.v1"
    )
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    measurement_definition_sha256: Sha256
    measurement_anchor: MeasurementAnchor


def _check_statistic_unit(statistic: Any, unit: StatisticUnit) -> None:
    if _STATISTIC_UNIT.get(statistic) != unit:
        raise ValueError("projection statistic unit does not match its statistic")


_StrictInt = Annotated[int, Field(strict=True)]
_StrictFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
_Fraction = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]


class FragmentLongitudinalValueProjectionV1(RegistryContract):
    """One E07 panel chart bin and one controlled statistic.

    ``count`` is the exact chart/table count.  ``fraction`` is the exact
    rational ``fraction_numerator / fraction_denominator`` over the panel's
    eligible-alignment denominator; it is never rounded to a float.  E07
    carries no uncertainty, so none is projected.
    """

    schema_version: Literal["traceback.e12-fragment-value-projection.v1"] = (
        "traceback.e12-fragment-value-projection.v1"
    )
    family: Literal[ProjectionFamily.FRAGMENT] = ProjectionFamily.FRAGMENT
    coordinate_scheme: Literal["e07_panel_chart_bin.v1"] = "e07_panel_chart_bin.v1"
    policy: ProjectionPolicyBindingV1
    measurement: ProjectionMeasurementV1
    artifact_sha256: Sha256
    view_sha256: Sha256
    panel: PanelId
    fragment_quantity: FragmentQuantity
    result_id: str = Field(min_length=1, max_length=128)
    result_sha256: Sha256
    bundle_id: str = Field(min_length=1, max_length=128)
    bundle_sha256: Sha256
    chart_sha256: Sha256
    bin: FragmentBinCoordinate
    statistic: FragmentStatistic
    statistic_unit: StatisticUnit
    value_state: Literal["observed"] = "observed"
    count: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    fraction_numerator: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    fraction_denominator: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)

    @model_validator(mode="after")
    def exact_value_shape(self) -> FragmentLongitudinalValueProjectionV1:
        _check_statistic_unit(self.statistic, self.statistic_unit)
        if self.statistic == FragmentStatistic.COUNT:
            if self.count is None or self.fraction_numerator is not None or (
                self.fraction_denominator is not None
            ):
                raise ValueError("a count projection carries only the exact count")
        else:
            if (
                self.count is not None
                or self.fraction_numerator is None
                or self.fraction_denominator is None
            ):
                raise ValueError("a fraction projection carries only the exact ratio")
            if self.fraction_numerator > self.fraction_denominator:
                raise ValueError("a bin fraction cannot exceed its denominator")
        return self


class CellOriginLongitudinalValueProjectionV1(RegistryContract):
    """One registered E08 atlas contributor's estimated fraction.

    The interval bounds are present only when the artifact's own uncertainty
    state is ``available``; every other state carries no bounds.
    """

    schema_version: Literal["traceback.e12-cell-origin-value-projection.v1"] = (
        "traceback.e12-cell-origin-value-projection.v1"
    )
    family: Literal[ProjectionFamily.CELL_ORIGIN] = ProjectionFamily.CELL_ORIGIN
    coordinate_scheme: Literal["e08_registered_atlas_contributor.v1"] = (
        "e08_registered_atlas_contributor.v1"
    )
    policy: ProjectionPolicyBindingV1
    measurement: ProjectionMeasurementV1
    artifact_sha256: Sha256
    request_sha256: Sha256
    result_id: str = Field(min_length=1, max_length=128)
    result_sha256: Sha256
    bundle_id: str = Field(min_length=1, max_length=128)
    bundle_sha256: Sha256
    cell_origin_method_sha256: Sha256
    atlas_id: str = Field(min_length=1, max_length=128)
    atlas_sha256: Sha256
    authority_head_sha256: Sha256
    contributor_id: str = Field(min_length=1, max_length=128)
    source_row_sha256: Sha256
    statistic: CellOriginStatistic
    statistic_unit: StatisticUnit
    value_state: Literal["observed"] = "observed"
    point_estimate: _Fraction
    interval_state: BootstrapInformationStatus | Literal["not_run"]
    lower_fraction: _Fraction | None = None
    upper_fraction: _Fraction | None = None

    @model_validator(mode="after")
    def authorized_interval_only(self) -> CellOriginLongitudinalValueProjectionV1:
        _check_statistic_unit(self.statistic, self.statistic_unit)
        available = self.interval_state == BootstrapInformationStatus.AVAILABLE
        bounds = (self.lower_fraction, self.upper_fraction)
        if available != all(item is not None for item in bounds) or (
            not available and any(item is not None for item in bounds)
        ):
            raise ValueError("only an available interval state carries bounds")
        if available:
            assert self.lower_fraction is not None and self.upper_fraction is not None
            if not self.lower_fraction <= self.point_estimate <= self.upper_fraction:
                raise ValueError("interval must contain the point estimate")
        return self


_CHROMOSOME_INTEGER = frozenset({CnaChromosomeStatistic.ACCEPTED_READ_COUNT})
_SEGMENT_INTEGER = frozenset(
    {
        CnaSegmentStatistic.UPSTREAM_COPY_NUMBER,
        CnaSegmentStatistic.RETAINED_BIN_COUNT,
        CnaSegmentStatistic.NATIVE_SPAN_BIN_COUNT,
    }
)


def _check_numeric_shape(
    integer: bool, integer_value: int | None, real_value: float | None
) -> None:
    if integer != (integer_value is not None) or integer == (real_value is not None):
        raise ValueError("projection value type does not match its statistic")


class CnaChromosomeLongitudinalValueProjectionV1(RegistryContract):
    """One E09 ``dosage_qc`` whole-chromosome statistic on one dosage grid."""

    schema_version: Literal["traceback.e12-cna-chromosome-value-projection.v1"] = (
        "traceback.e12-cna-chromosome-value-projection.v1"
    )
    family: Literal[ProjectionFamily.CNA_CHROMOSOME] = ProjectionFamily.CNA_CHROMOSOME
    coordinate_scheme: Literal["e09_dosage_qc_chromosome.v1"] = (
        "e09_dosage_qc_chromosome.v1"
    )
    cna_source: Literal[CnaSource.DOSAGE_QC] = CnaSource.DOSAGE_QC
    policy: ProjectionPolicyBindingV1
    measurement: ProjectionMeasurementV1
    artifact_sha256: Sha256
    coordinate_grid_sha256: Sha256
    source_result_sha256: Sha256
    source_authority_sha256: Sha256
    chromosome: ChromosomeId
    statistic: CnaChromosomeStatistic
    statistic_unit: StatisticUnit
    value_state: Literal["observed"] = "observed"
    integer_value: _StrictInt | None = None
    real_value: _StrictFloat | None = None

    @model_validator(mode="after")
    def exact_value_shape(self) -> CnaChromosomeLongitudinalValueProjectionV1:
        _check_statistic_unit(self.statistic, self.statistic_unit)
        _check_numeric_shape(
            self.statistic in _CHROMOSOME_INTEGER, self.integer_value, self.real_value
        )
        return self


class CnaSegmentLongitudinalValueProjectionV1(RegistryContract):
    """One E09 ``segmented_cna`` segment statistic on one segmented grid."""

    schema_version: Literal["traceback.e12-cna-segment-value-projection.v1"] = (
        "traceback.e12-cna-segment-value-projection.v1"
    )
    family: Literal[ProjectionFamily.CNA_SEGMENT] = ProjectionFamily.CNA_SEGMENT
    coordinate_scheme: Literal["e09_segmented_cna_segment.v1"] = (
        "e09_segmented_cna_segment.v1"
    )
    cna_source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    policy: ProjectionPolicyBindingV1
    measurement: ProjectionMeasurementV1
    artifact_sha256: Sha256
    coordinate_grid_sha256: Sha256
    source_result_sha256: Sha256
    source_authority_sha256: Sha256
    segment: CnaSegmentCoordinate
    statistic: CnaSegmentStatistic
    statistic_unit: StatisticUnit
    value_state: Literal["observed"] = "observed"
    integer_value: _StrictInt | None = None
    real_value: _StrictFloat | None = None

    @model_validator(mode="after")
    def exact_value_shape(self) -> CnaSegmentLongitudinalValueProjectionV1:
        _check_statistic_unit(self.statistic, self.statistic_unit)
        _check_numeric_shape(
            self.statistic in _SEGMENT_INTEGER, self.integer_value, self.real_value
        )
        return self


LongitudinalValueProjection = Annotated[
    FragmentLongitudinalValueProjectionV1
    | CellOriginLongitudinalValueProjectionV1
    | CnaChromosomeLongitudinalValueProjectionV1
    | CnaSegmentLongitudinalValueProjectionV1,
    Field(discriminator="family"),
]


def _projection_key(projection: Any) -> tuple[Any, ...]:
    """Canonical (coordinate, statistic) order; never a value."""

    statistic_order = tuple(type(projection.statistic)).index(projection.statistic)
    if isinstance(projection, FragmentLongitudinalValueProjectionV1):
        return (projection.bin.bin_index, statistic_order)
    if isinstance(projection, CellOriginLongitudinalValueProjectionV1):
        return (projection.contributor_id, statistic_order)
    if isinstance(projection, CnaChromosomeLongitudinalValueProjectionV1):
        return (_CHROMOSOMES.index(projection.chromosome), statistic_order)
    return (projection.segment.segment_index, statistic_order)


class SourceValueProjectionSetV1(RegistryContract):
    """The complete, canonically ordered output of one adapter call."""

    schema_version: Literal["traceback.e12-source-value-projection-set.v1"] = (
        "traceback.e12-source-value-projection-set.v1"
    )
    family: ProjectionFamily
    policy: ProjectionPolicyBindingV1
    measurement: ProjectionMeasurementV1
    artifact_sha256: Sha256
    projections: tuple[LongitudinalValueProjection, ...] = Field(
        min_length=1, max_length=MAX_PROJECTED_COMPONENTS
    )
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def one_binding_canonical_order(self) -> SourceValueProjectionSetV1:
        for item in self.projections:
            if (
                item.family != self.family
                or item.policy != self.policy
                or item.measurement != self.measurement
                or item.artifact_sha256 != self.artifact_sha256
            ):
                raise ValueError("projections must share one family and binding")
        keys = [_projection_key(item) for item in self.projections]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("projections must be uniquely canonically ordered")
        return self


# --- capture and shared checks ---------------------------------------------

ContractT = TypeVar("ContractT", bound=RegistryContract)


_RESOLVED_MODEL_TYPES, _RESOLVED_ENUM_TYPES = contract_type_graph(ResolvedProjectionPolicy)
_EXACT_GRAPHS: dict[type[BaseModel], tuple[frozenset[Any], frozenset[Any]]] = {
    model: contract_type_graph(model)
    for model in (
        FragmentExplorerView,
        CellOriginExplorerArtifact,
        CnaExplorerSnapshot,
        DosageQcResultBundle,
        CnvDevelopmentResult,
        ExplorerInputAuthority,
        SourceValueProjectionSetV1,
        MeasurementAnchor,
        SourceMeasurementIdentity,
    )
}
# Generous structural bounds: every family artifact is also bounded by its own
# contract (E07 bins, E08 canonical bytes, E09 bins and segments).
_MAX_CAPTURE_BYTES = 256 * 1024 * 1024
_MAX_CAPTURE_NODES = 64_000_000
_MAX_CAPTURE_ITEMS = 2_000_000


def _require_exact_graph(value: object, model: type[BaseModel]) -> None:
    """Reject a caller object whose graph holds a foreign or subclassed node.

    Serializing and reparsing would silently normalize a nested subclass, a
    ``model_construct`` node or private/extra state; this rejects it instead.
    """

    model_types, enum_types = _EXACT_GRAPHS[model]
    exact_model_bytes(
        value,
        model,
        model_types=model_types,
        enum_types=enum_types,
        max_bytes=_MAX_CAPTURE_BYTES,
        max_nodes=_MAX_CAPTURE_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH * 2,
        max_collection_items=_MAX_CAPTURE_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def _reparse_exact(model: type[ContractT], content: bytes) -> ContractT:
    parsed = model.model_validate_json(content)
    if canonical_contract_bytes(parsed) != content:
        raise ValueError("contract bytes are not canonical")
    return parsed


def _capture_policy(policy: object) -> ResolvedProjectionPolicy:
    """Capture the caller's object as bounded exact bytes and revalidate them.

    A ``model_construct`` forgery, subclass, nested foreign object, private or
    extra state, or a policy whose rule or statistic is outside the closed
    registry vocabulary (for example a value-ranked ``top``) is rejected.
    """

    if type(policy) is not ResolvedProjectionPolicy:
        raise SourceValuePolicyRejected("projection policy is not resolved")
    try:
        content = exact_model_bytes(
            policy,
            ResolvedProjectionPolicy,
            model_types=_RESOLVED_MODEL_TYPES,
            enum_types=_RESOLVED_ENUM_TYPES,
            max_bytes=MAX_OBJECT_BYTES,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        return _reparse_exact(ResolvedProjectionPolicy, content)
    except (ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValuePolicyRejected("resolved projection policy is invalid") from exc


def _capture_contract(model: type[ContractT], value: object, label: str) -> ContractT:
    if type(value) is not model:
        raise SourceValueMeasurementMismatch(f"{label} is not an exact contract")
    try:
        _require_exact_graph(value, model)
        return _reparse_exact(model, canonical_contract_bytes(value))  # type: ignore[arg-type]
    except (ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueMeasurementMismatch(f"{label} is invalid") from exc


def _policy_binding(resolved: ResolvedProjectionPolicy) -> ProjectionPolicyBindingV1:
    return ProjectionPolicyBindingV1(
        registry_id=resolved.registry_id,
        registry_epoch_sha256=resolved.registry_epoch_sha256,
        state_version=resolved.state_version,
        state_head_sha256=resolved.state_head_sha256,
        selector_id=resolved.selector_id,
        policy_version=resolved.policy_version,
        object_sha256=resolved.object_sha256,
        policy_sha256=resolved.policy_sha256,
        selection_rule=resolved.policy.selection_rule,
    )


def _measurement_binding(resolved: ResolvedProjectionPolicy) -> ProjectionMeasurementV1:
    measurement = resolved.policy.measurement
    return ProjectionMeasurementV1(
        method_ref=measurement.method_ref,
        method_definition_sha256=measurement.method_definition_sha256,
        quantity_id=measurement.quantity_id,
        unit=measurement.unit,
        measurement_definition_sha256=measurement.measurement_definition_sha256,
        measurement_anchor=resolved.policy.measurement_anchor,
    )


def _require_source_record(
    record: VerifiedMeasurementRecord, source: SourceMeasurementIdentity
) -> None:
    """The artifact's embedded E05 record must be the builder's E04/E06 source."""

    if (
        record.method.method_ref != source.method_ref
        or record.method_definition_sha256 != source.method_definition_sha256
        or record.method.quantity_id != source.quantity_id
        or record.method.unit != source.unit
        or record.compatibility_key.quantity_id != source.quantity_id
        or record.compatibility_key.unit != source.unit
    ):
        raise SourceValueMeasurementMismatch(
            "artifact source record does not match the source measurement"
        )


def _exactly_one(
    rows: Iterable[Any], predicate: Callable[[Any], bool], label: str
) -> Any:
    matches = [row for row in rows if predicate(row)]
    if len(matches) != 1:
        raise SourceValueCoordinateUnresolved(
            f"{label} must match exactly one row, matched {len(matches)}"
        )
    return matches[0]


class _RowIndex:
    """Rows grouped once by an exact key, so each lookup is constant time.

    Duplicates are kept, not collapsed: a lookup still requires exactly one.
    """

    def __init__(self, rows: Iterable[Any], key: Callable[[Any], Any], label: str):
        self._label = label
        self._rows: dict[Any, list[Any]] = {}
        for row in rows:
            self._rows.setdefault(key(row), []).append(row)

    def one(self, key: Any) -> Any:
        matches = self._rows.get(key, ())
        if len(matches) != 1:
            raise SourceValueCoordinateUnresolved(
                f"{self._label} must match exactly one row, matched {len(matches)}"
            )
        return matches[0]


def _bounds(row: Any) -> tuple[int, int | None]:
    return (row.lower_inclusive, row.upper_exclusive)


def _interval(row: Any) -> tuple[str, int, int]:
    return (row.contig, row.start, row.end)


def _statistics_for(policy: Any) -> tuple[Any, ...]:
    return tuple(policy.all_component_statistics)


def _is_all(policy: Any) -> bool:
    return policy.selection_rule == ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS


# --- E07 fragment ------------------------------------------------------------


def _replay_fragment(view: object) -> tuple[FragmentExplorerView, str]:
    if type(view) is not FragmentExplorerView:
        raise SourceValueFamilyMismatch("fragment policy requires an E07 view")
    try:
        _require_exact_graph(view, FragmentExplorerView)
        content = canonical_fragment_explorer_bytes(view)
        parsed = fragment_explorer_from_canonical_bytes(FragmentExplorerView, content)
        replayed = replay_fragment_explorer_view(parsed.request, parsed)
    except (FragmentExplorerError, ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueReplayRejected("E07 view does not replay exactly") from exc
    return replayed, hashlib.sha256(content).hexdigest()


def _fragment_projections(
    resolved: ResolvedProjectionPolicy,
    view_input: object,
    source_measurement: SourceMeasurementIdentity,
) -> tuple[str, list[FragmentLongitudinalValueProjectionV1]]:
    policy = resolved.policy
    assert isinstance(policy, FragmentProjectionPolicyV1)
    view, artifact_sha256 = _replay_fragment(view_input)
    panel = view.left if policy.panel == PanelId.A else view.right
    if panel.panel != policy.panel:
        raise SourceValueFamilyMismatch("E07 panel identity does not match policy")
    source = _exactly_one(
        view.request.sources,
        lambda item: item.record.result_id == panel.selection.result_id,
        "E07 panel source",
    )
    if (
        panel.quantity != policy.fragment_quantity
        or source.quantity != policy.fragment_quantity
    ):
        raise SourceValueMeasurementMismatch("E07 panel quantity differs from policy")
    _require_source_record(source.record, source_measurement)
    if (
        panel.source_state != ExplorerSourceState.COMPLETE
        or source.state != ExplorerSourceState.COMPLETE
        or panel.denominator is None
        or source.chart is None
        or source.measurement is None
    ):
        raise SourceValueWithheld("E07 panel withholds its values")
    chart_rows = source.chart.rows
    histogram = source.measurement.histogram
    eligible = source.measurement.eligible_alignments
    if len(chart_rows) > MAX_EXPLORER_BINS or len(histogram) != len(chart_rows):
        raise SourceValueRepresentationDrift("E02 chart and table differ in length")

    if _is_all(policy):
        requested = [
            (FragmentBinCoordinate(
                bin_index=index,
                lower_inclusive=row.lower_inclusive,
                upper_exclusive=row.upper_exclusive,
            ), statistic)
            for index, row in enumerate(chart_rows)
            for statistic in _statistics_for(policy)
        ]
    else:
        requested = [(item.bin, item.statistic) for item in policy.components]

    common = {
        "policy": _policy_binding(resolved),
        "measurement": _measurement_binding(resolved),
        "artifact_sha256": artifact_sha256,
        "view_sha256": view.view_sha256,
        "panel": policy.panel,
        "fragment_quantity": policy.fragment_quantity,
        "result_id": source.record.result_id,
        "result_sha256": source.record.result_sha256,
        "bundle_id": source.record.bundle_id,
        "bundle_sha256": source.record.bundle_sha256,
        "chart_sha256": hashlib.sha256(canonical_json_bytes(source.chart)).hexdigest(),
    }
    chart_index = _RowIndex(chart_rows, _bounds, "E07 chart bin")
    panel_index = _RowIndex(panel.rows, _bounds, "E07 panel bin")
    table_index = _RowIndex(
        (row for row in view.accessible_rows if row.panel == policy.panel),
        _bounds,
        "E07 table bin",
    )
    projections = []
    for bin_, statistic in requested:
        bounds = (bin_.lower_inclusive, bin_.upper_exclusive)
        if bin_.bin_index >= len(chart_rows):
            raise SourceValueCoordinateUnresolved("E07 bin is outside the chart")
        chart_row = chart_rows[bin_.bin_index]
        if _bounds(chart_row) != bounds:
            raise SourceValueCoordinateUnresolved("E07 bin boundaries are shifted")
        chart_index.one(bounds)
        table_entry = histogram[bin_.bin_index]
        if (
            table_entry.bin.lower_inclusive,
            table_entry.bin.upper_exclusive,
        ) != bounds or table_entry.count != chart_row.count:
            raise SourceValueRepresentationDrift("E02 chart and table rows differ")
        panel_row = panel_index.one(bounds)
        table_row = table_index.one(bounds)
        if not (
            panel_row.count == table_row.count == chart_row.count
            and panel_row.fraction_numerator == table_row.fraction_numerator
            and panel_row.fraction_denominator
            == table_row.fraction_denominator
            == panel.denominator.eligible_alignments
            == eligible
        ):
            raise SourceValueRepresentationDrift("E07 chart and table values differ")
        if statistic == FragmentStatistic.COUNT:
            value: dict[str, Any] = {"count": table_row.count}
        else:
            value = {
                "fraction_numerator": table_row.fraction_numerator,
                "fraction_denominator": table_row.fraction_denominator,
            }
        projections.append(
            FragmentLongitudinalValueProjectionV1(
                **common,
                bin=bin_,
                statistic=statistic,
                statistic_unit=_STATISTIC_UNIT[statistic],
                **value,
            )
        )
    return artifact_sha256, projections


# --- E08 cell origin ---------------------------------------------------------


def _replay_cell_origin(artifact: object) -> tuple[CellOriginExplorerArtifact, str]:
    if type(artifact) is not CellOriginExplorerArtifact:
        raise SourceValueFamilyMismatch("cell-origin policy requires an E08 artifact")
    try:
        _require_exact_graph(artifact, CellOriginExplorerArtifact)
        content = canonical_cell_origin_explorer_bytes(artifact)
        parsed = cell_origin_explorer_from_canonical_bytes(content)
        if _build_replay_view(parsed.request) != parsed.view:
            raise CellOriginExplorerError("E08 view does not replay")
    except (CellOriginExplorerError, ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueReplayRejected("E08 artifact does not replay exactly") from exc
    return parsed, hashlib.sha256(content).hexdigest()


def _table_fields(row: DotIntervalRow | ExactTableRow) -> tuple[Any, ...]:
    return (
        row.contributor_id,
        row.label,
        row.estimate_fraction,
        row.uncertainty_status,
        row.lower_fraction,
        row.upper_fraction,
    )


def _cell_origin_projections(
    resolved: ResolvedProjectionPolicy,
    artifact_input: object,
    source_measurement: SourceMeasurementIdentity,
) -> tuple[str, list[CellOriginLongitudinalValueProjectionV1]]:
    policy = resolved.policy
    assert isinstance(policy, CellOriginProjectionPolicyV1)
    artifact, artifact_sha256 = _replay_cell_origin(artifact_input)
    view = artifact.view
    sources = artifact.request.result_view_request.sources
    if len(sources) != 1:
        raise SourceValueFamilyMismatch("E08 artifact must carry one source")
    record = sources[0].record
    _require_source_record(record, source_measurement)
    binding = view.binding
    if binding.atlas_id != policy.atlas_id or binding.atlas_sha256 != policy.atlas_sha256:
        raise SourceValueFamilyMismatch("E08 atlas does not match the policy atlas")
    if view.status != ExplorerStatus.READY or artifact.request.source is None:
        raise SourceValueWithheld("E08 artifact withholds its values")
    result = artifact.request.source.result
    deconvolution = result.deconvolution
    if not isinstance(deconvolution, DeconvolutionOutputV2) or (
        deconvolution.atlas_id != policy.atlas_id
        or deconvolution.atlas_sha256 != policy.atlas_sha256
    ):
        raise SourceValueFamilyMismatch("E08 deconvolution atlas does not match")
    if binding.cell_origin_method_sha256 is None:
        raise SourceValueWithheld("E08 artifact has no cell-origin method digest")
    registered = [item.cell_type_id for item in deconvolution.estimates]
    intervals = (
        {item.cell_type_id: item for item in result.bootstrap.intervals}
        if isinstance(result.bootstrap, BootstrapResultV2)
        else {}
    )

    if _is_all(policy):
        if len(set(registered)) != len(registered) or not (
            sorted(registered)
            == sorted(row.contributor_id for row in view.dot_interval_rows)
            == sorted(row.contributor_id for row in view.exact_table_rows)
        ):
            raise SourceValueRepresentationDrift(
                "E08 registered contributors differ between chart and table"
            )
        # Canonical order is contributor ID, never the view's estimate rank.
        requested = [
            (contributor, statistic)
            for contributor in sorted(registered)
            for statistic in _statistics_for(policy)
        ]
    else:
        requested = [(item.contributor_id, item.statistic) for item in policy.components]

    common = {
        "policy": _policy_binding(resolved),
        "measurement": _measurement_binding(resolved),
        "artifact_sha256": artifact_sha256,
        "request_sha256": view.request_sha256,
        "result_id": binding.result_id,
        "result_sha256": binding.result_sha256,
        "bundle_id": binding.bundle_id,
        "bundle_sha256": binding.bundle_sha256,
        "cell_origin_method_sha256": binding.cell_origin_method_sha256,
        "atlas_id": binding.atlas_id,
        "atlas_sha256": binding.atlas_sha256,
        "authority_head_sha256": binding.authority_head_sha256,
    }
    estimate_index = _RowIndex(
        deconvolution.estimates,
        lambda item: item.cell_type_id,
        "E08 registered contributor",
    )
    dot_index = _RowIndex(
        view.dot_interval_rows, lambda row: row.contributor_id, "E08 dot row"
    )
    table_index = _RowIndex(
        view.exact_table_rows, lambda row: row.contributor_id, "E08 table row"
    )
    projections = []
    for contributor, statistic in requested:
        estimate = estimate_index.one(contributor)
        dot = dot_index.one(contributor)
        table = table_index.one(contributor)
        if _table_fields(dot) != _table_fields(table):
            raise SourceValueRepresentationDrift("E08 dot and table rows differ")
        interval = intervals.get(contributor)
        expected = (
            estimate.fraction,
            interval.information_status if interval is not None else "not_run",
            interval.lower_fraction if interval is not None else None,
            interval.upper_fraction if interval is not None else None,
        )
        if expected != _table_fields(table)[2:]:
            raise SourceValueRepresentationDrift("E08 table differs from its result")
        projections.append(
            CellOriginLongitudinalValueProjectionV1(
                **common,
                contributor_id=contributor,
                source_row_sha256=hashlib.sha256(
                    canonical_json_bytes(table)
                ).hexdigest(),
                statistic=statistic,
                statistic_unit=_STATISTIC_UNIT[statistic],
                point_estimate=table.estimate_fraction,
                interval_state=table.uncertainty_status,
                lower_fraction=table.lower_fraction,
                upper_fraction=table.upper_fraction,
            )
        )
    return artifact_sha256, projections


# --- E09 CNA ---------------------------------------------------------------


def _capture_cna_inputs(inputs: object) -> CnaReplayInputs:
    if type(inputs) is not CnaReplayInputs:
        raise SourceValueReplayRejected("CNA projection requires exact replay inputs")
    expected = (
        (inputs.dosage, DosageQcResultBundle),
        (inputs.segmented, CnvDevelopmentResult),
        (inputs.dosage_authority, ExplorerInputAuthority),
        (inputs.segmented_authority, ExplorerInputAuthority),
    )
    captured = []
    try:
        for value, model in expected:
            if type(value) is not model:
                raise TypeError("CNA replay input is not an exact model")
            _require_exact_graph(value, model)
            captured.append(model.model_validate_json(value.model_dump_json()))
    except (ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueReplayRejected("CNA replay inputs are invalid") from exc
    return CnaReplayInputs(*captured)


def _replay_cna(
    snapshot: object, inputs: CnaReplayInputs
) -> tuple[CnaExplorerSnapshot, str]:
    if type(snapshot) is not CnaExplorerSnapshot:
        raise SourceValueFamilyMismatch("CNA policy requires an E09 snapshot")
    try:
        _require_exact_graph(snapshot, CnaExplorerSnapshot)
        content = cna_explorer_snapshot_bytes(snapshot)
        parsed = cna_explorer_snapshot_from_bytes(content)
        replayed = replay_cna_explorer_snapshot(
            inputs.dosage,
            inputs.segmented,
            parsed,
            dosage_authority=inputs.dosage_authority,
            segmented_authority=inputs.segmented_authority,
        )
    except (CnaExplorerError, ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueReplayRejected("E09 snapshot does not replay exactly") from exc
    return replayed, hashlib.sha256(content).hexdigest()


def _cna_grid_and_binding(
    snapshot: CnaExplorerSnapshot,
    source: CnaSource,
    grid: CoordinateGridLayer,
    grid_sha256: str,
) -> tuple[Any, dict[str, str]]:
    if snapshot.availability != ExplorerAvailability.AVAILABLE or snapshot.layers is None:
        raise SourceValueWithheld("E09 snapshot withholds its values")
    layers = snapshot.layers
    artifact_grid = _exactly_one(
        layers.coordinate_grids, lambda item: item.source == source, "E09 grid"
    )
    if (
        artifact_grid != grid
        or cna_coordinate_grid_sha256(artifact_grid) != grid_sha256
    ):
        raise SourceValueFamilyMismatch("E09 coordinate grid differs from the policy")
    input_binding = _exactly_one(
        snapshot.provenance.input_bindings,
        lambda item: item.source == source,
        "E09 input binding",
    )
    return layers, {
        "coordinate_grid_sha256": grid_sha256,
        "source_result_sha256": input_binding.result_sha256,
        "source_authority_sha256": input_binding.authority_sha256,
    }


def _cna_value(item: Any, statistic: Any, integer: frozenset[Any]) -> dict[str, Any]:
    value = getattr(item, statistic.value)
    if statistic in integer:
        if type(value) is not int:
            raise SourceValueMeasurementMismatch("CNA count statistic is not an integer")
        return {"integer_value": value}
    if type(value) is not float:
        raise SourceValueMeasurementMismatch("CNA real statistic is not a float")
    return {"real_value": value}


def _cna_chromosome_projections(
    resolved: ResolvedProjectionPolicy,
    snapshot_input: object,
    inputs: CnaReplayInputs,
) -> tuple[str, list[CnaChromosomeLongitudinalValueProjectionV1]]:
    policy = resolved.policy
    assert isinstance(policy, CnaChromosomeProjectionPolicyV1)
    snapshot, artifact_sha256 = _replay_cna(snapshot_input, inputs)
    layers, cna_binding = _cna_grid_and_binding(
        snapshot,
        CnaSource.DOSAGE_QC,
        policy.coordinate_grid,
        policy.coordinate_grid_sha256,
    )
    declared = set(policy.coordinate_grid.contig_order)
    if _is_all(policy):
        if tuple(item.chromosome for item in layers.dosage_chromosomes) != _CHROMOSOMES:
            raise SourceValueRepresentationDrift("E09 dosage layer is not canonical")
        requested = [
            (chromosome, statistic)
            for chromosome in _CHROMOSOMES
            for statistic in _statistics_for(policy)
        ]
    else:
        requested = [(item.chromosome, item.statistic) for item in policy.components]

    common = {
        "policy": _policy_binding(resolved),
        "measurement": _measurement_binding(resolved),
        "artifact_sha256": artifact_sha256,
        **cna_binding,
    }
    def chromosome_of(item: Any) -> str:
        return str(item.chromosome)

    chart_index = _RowIndex(
        snapshot.chart.dosage_chromosomes, chromosome_of, "E09 dosage chart"
    )
    layer_index = _RowIndex(layers.dosage_chromosomes, chromosome_of, "E09 dosage layer")
    table_index = _RowIndex(inputs.dosage.chromosomes, chromosome_of, "E09 dosage table")
    projections = []
    for chromosome, statistic in requested:
        if chromosome not in declared:
            raise SourceValueCoordinateUnresolved("chromosome is not on the dosage grid")
        chart = chart_index.one(chromosome)
        layer = layer_index.one(chromosome)
        table = table_index.one(chromosome)
        if chart != layer or any(
            getattr(table, field) != getattr(layer, field)
            for field in (
                "ordinal",
                "accepted_read_count",
                "relative_diploid_dosage",
                "log2_ratio",
                "dosage_direction",
            )
        ):
            raise SourceValueRepresentationDrift("E09 dosage chart and table differ")
        projections.append(
            CnaChromosomeLongitudinalValueProjectionV1(
                **common,
                chromosome=chromosome,
                statistic=statistic,
                statistic_unit=_STATISTIC_UNIT[statistic],
                **_cna_value(table, statistic, _CHROMOSOME_INTEGER),
            )
        )
    return artifact_sha256, projections


_SEGMENT_UPSTREAM_FIELD = {
    CnaSegmentStatistic.MEDIAN_LOG2: "median_log2",
    CnaSegmentStatistic.UPSTREAM_COPY_NUMBER: "copy_number",
    CnaSegmentStatistic.RETAINED_BIN_COUNT: "retained_bin_count",
    CnaSegmentStatistic.NATIVE_SPAN_BIN_COUNT: "native_span_bin_count",
}


def _cna_segment_projections(
    resolved: ResolvedProjectionPolicy,
    snapshot_input: object,
    inputs: CnaReplayInputs,
) -> tuple[str, list[CnaSegmentLongitudinalValueProjectionV1]]:
    policy = resolved.policy
    assert isinstance(policy, CnaSegmentProjectionPolicyV1)
    snapshot, artifact_sha256 = _replay_cna(snapshot_input, inputs)
    layers, cna_binding = _cna_grid_and_binding(
        snapshot,
        CnaSource.SEGMENTED_CNA,
        policy.coordinate_grid,
        policy.coordinate_grid_sha256,
    )
    declared = set(policy.coordinate_grid.contig_order)
    if _is_all(policy):
        if not (
            snapshot.chart.segments == snapshot.tables.segments == layers.segments
        ) or len(layers.segments) != len(inputs.segmented.segments):
            raise SourceValueRepresentationDrift("E09 segment chart and table differ")
        requested = [
            (
                CnaSegmentCoordinate(
                    segment_index=item.segment_index,
                    contig=item.contig,
                    start=item.start,
                    end=item.end,
                ),
                statistic,
            )
            for item in layers.segments
            for statistic in _statistics_for(policy)
        ]
    else:
        requested = [(item.segment, item.statistic) for item in policy.components]

    common = {
        "policy": _policy_binding(resolved),
        "measurement": _measurement_binding(resolved),
        "artifact_sha256": artifact_sha256,
        **cna_binding,
    }
    upstream = inputs.segmented.segments

    def index_of(item: Any) -> int:
        return int(item.segment_index)

    chart_by_index = _RowIndex(snapshot.chart.segments, index_of, "E09 segment chart")
    chart_by_interval = _RowIndex(snapshot.chart.segments, _interval, "E09 segment chart")
    table_by_index = _RowIndex(snapshot.tables.segments, index_of, "E09 segment table")
    table_by_interval = _RowIndex(
        snapshot.tables.segments, _interval, "E09 segment table"
    )
    layer_by_index = _RowIndex(layers.segments, index_of, "E09 segment layer")
    projections = []
    for segment, statistic in requested:
        if segment.contig not in declared:
            raise SourceValueCoordinateUnresolved("segment is not on the segmented grid")
        interval = (segment.contig, segment.start, segment.end)
        chart = chart_by_index.one(segment.segment_index)
        if _interval(chart) != interval:
            raise SourceValueCoordinateUnresolved("E09 segment interval is shifted")
        chart_by_interval.one(interval)
        table = table_by_index.one(segment.segment_index)
        table_by_interval.one(interval)
        layer = layer_by_index.one(segment.segment_index)
        if not chart == table == layer:
            raise SourceValueRepresentationDrift("E09 segment chart and table differ")
        if segment.segment_index >= len(upstream):
            raise SourceValueRepresentationDrift("E09 segment is absent upstream")
        source_segment = upstream[segment.segment_index]
        if (
            (source_segment.contig, source_segment.start, source_segment.end) != interval
            or any(
                getattr(source_segment, upstream_field) != getattr(table, field.value)
                for field, upstream_field in _SEGMENT_UPSTREAM_FIELD.items()
            )
        ):
            raise SourceValueRepresentationDrift("E09 segment differs from its result")
        projections.append(
            CnaSegmentLongitudinalValueProjectionV1(
                **common,
                segment=segment,
                statistic=statistic,
                statistic_unit=_STATISTIC_UNIT[statistic],
                **_cna_value(table, statistic, _SEGMENT_INTEGER),
            )
        )
    return artifact_sha256, projections


# --- entry points ------------------------------------------------------------


def project_source_values(
    policy: ResolvedProjectionPolicy,
    artifact: FragmentExplorerView | CellOriginExplorerArtifact | CnaExplorerSnapshot,
    *,
    measurement_anchor: MeasurementAnchor,
    source_measurement: SourceMeasurementIdentity,
    cna_inputs: CnaReplayInputs | None = None,
) -> SourceValueProjectionSetV1:
    """Project the policy's exact coordinates from one exact family artifact.

    ``measurement_anchor`` is the D05 anchor of the builder's live-resolved
    manifest and ``source_measurement`` the identity of its E04/E06 source;
    ``cna_inputs`` are the exact E09 upstream inputs and are required for, and
    only accepted with, a CNA policy.  The function takes no value, subset,
    ordering or selector: every coordinate comes from the resolved policy or,
    for ``canonical_all_components``, from the complete artifact.
    """

    resolved = _capture_policy(policy)
    anchor = _capture_contract(MeasurementAnchor, measurement_anchor, "D05 anchor")
    source = _capture_contract(
        SourceMeasurementIdentity, source_measurement, "source measurement"
    )
    try:
        require_projection_policy_binding(
            resolved,
            measurement_anchor=anchor,
            method_ref=source.method_ref,
            method_definition_sha256=source.method_definition_sha256,
            quantity_id=source.quantity_id,
            unit=source.unit,
        )
    except ProjectionPolicyRegistryConflict as exc:
        raise SourceValueMeasurementMismatch(
            "policy does not bind the source measurement"
        ) from exc
    family_policy = resolved.policy
    cna = isinstance(
        family_policy, (CnaChromosomeProjectionPolicyV1, CnaSegmentProjectionPolicyV1)
    )
    if cna != (cna_inputs is not None):
        raise SourceValueFamilyMismatch("CNA replay inputs do not match the family")
    projections: Sequence[Any]
    if isinstance(family_policy, FragmentProjectionPolicyV1):
        artifact_sha256, projections = _fragment_projections(resolved, artifact, source)
    elif isinstance(family_policy, CellOriginProjectionPolicyV1):
        artifact_sha256, projections = _cell_origin_projections(
            resolved, artifact, source
        )
    elif isinstance(family_policy, CnaChromosomeProjectionPolicyV1):
        artifact_sha256, projections = _cna_chromosome_projections(
            resolved, artifact, _capture_cna_inputs(cna_inputs)
        )
    elif isinstance(family_policy, CnaSegmentProjectionPolicyV1):
        artifact_sha256, projections = _cna_segment_projections(
            resolved, artifact, _capture_cna_inputs(cna_inputs)
        )
    else:  # pragma: no cover - the resolved union is closed
        raise SourceValuePolicyRejected("projection family is not registered")
    if not projections:
        raise SourceValueCoordinateUnresolved("projection resolved no components")
    try:
        return SourceValueProjectionSetV1(
            family=family_policy.family,
            policy=_policy_binding(resolved),
            measurement=_measurement_binding(resolved),
            artifact_sha256=artifact_sha256,
            projections=tuple(projections),
        )
    except ValidationError as exc:
        raise SourceValueProjectionError("projection set is not canonical") from exc


def verify_source_value_projection(
    projection: SourceValueProjectionSetV1,
    policy: ResolvedProjectionPolicy,
    artifact: FragmentExplorerView | CellOriginExplorerArtifact | CnaExplorerSnapshot,
    *,
    measurement_anchor: MeasurementAnchor,
    source_measurement: SourceMeasurementIdentity,
    cna_inputs: CnaReplayInputs | None = None,
) -> SourceValueProjectionSetV1:
    """Reject any projection that is not this adapter's exact replayed output.

    A caller-created scalar, edited value, reordered or truncated vector, or a
    projection copied onto another artifact or policy fails here.
    """

    if type(projection) is not SourceValueProjectionSetV1:
        raise SourceValueProjectionForged("projection is not an exact projection set")
    try:
        _require_exact_graph(projection, SourceValueProjectionSetV1)
        captured = _reparse_exact(
            SourceValueProjectionSetV1, canonical_contract_bytes(projection)
        )
    except (ValidationError, *_REPLAY_FAILURES) as exc:
        raise SourceValueProjectionForged("projection set is invalid") from exc
    expected = project_source_values(
        policy,
        artifact,
        measurement_anchor=measurement_anchor,
        source_measurement=source_measurement,
        cna_inputs=cna_inputs,
    )
    if canonical_contract_bytes(captured) != canonical_contract_bytes(expected):
        raise SourceValueProjectionForged("projection does not replay from its source")
    return expected


__all__ = [
    "MAX_PROJECTED_COMPONENTS",
    "CellOriginLongitudinalValueProjectionV1",
    "CnaChromosomeLongitudinalValueProjectionV1",
    "CnaReplayInputs",
    "CnaSegmentLongitudinalValueProjectionV1",
    "FragmentLongitudinalValueProjectionV1",
    "LongitudinalValueProjection",
    "ProjectionMeasurementV1",
    "ProjectionPolicyBindingV1",
    "SourceMeasurementIdentity",
    "SourceValueCoordinateUnresolved",
    "SourceValueFamilyMismatch",
    "SourceValueMeasurementMismatch",
    "SourceValuePolicyRejected",
    "SourceValueProjectionError",
    "SourceValueProjectionForged",
    "SourceValueProjectionSetV1",
    "SourceValueReplayRejected",
    "SourceValueRepresentationDrift",
    "SourceValueWithheld",
    "project_source_values",
    "verify_source_value_projection",
]
