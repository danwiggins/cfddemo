"""E12 family-specific source-value adapters over real E07/E08/E09 artifacts."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evidence_inspector.source_value_projection as module
from evidence_inspector.cell_origin_explorer import (
    CellOriginExplorerArtifact,
    build_cell_origin_explorer_artifact,
)
from evidence_inspector.cell_origin_models import BootstrapInformationStatus
from evidence_inspector.cna_explorer import (
    CnaExplorerSnapshot,
    CnaSource,
    CoordinateGridLayer,
    TrustState,
    build_cna_explorer_snapshot,
)
from evidence_inspector.cohort_manifest import MeasurementAnchor
from evidence_inspector.compatibility import TrustState as E05TrustState
from evidence_inspector.fragment_explorer import (
    ExplorerControls,
    ExplorerSourceState,
    FragmentExplorerView,
    FragmentQuantity,
    PanelId,
    build_fragment_explorer_view,
)
from evidence_inspector.method_registry import MethodDefinition, method_definition_sha256
from evidence_inspector.projection_policy_registry import (
    CellOriginProjectionComponent,
    CellOriginStatistic,
    CnaChromosomeProjectionComponent,
    CnaChromosomeStatistic,
    CnaSegmentCoordinate,
    CnaSegmentProjectionComponent,
    CnaSegmentStatistic,
    FragmentStatistic,
    ProjectionPolicyRegistry,
    ProjectionSelectionRule,
    ResolvedProjectionPolicy,
    StatisticUnit,
    cna_coordinate_grid_sha256,
)
from evidence_inspector.source_value_projection import (
    CellOriginLongitudinalValueProjectionV1,
    CnaChromosomeLongitudinalValueProjectionV1,
    CnaReplayInputs,
    FragmentLongitudinalValueProjectionV1,
    SourceMeasurementIdentity,
    SourceValueCoordinateUnresolved,
    SourceValueFamilyMismatch,
    SourceValueMeasurementMismatch,
    SourceValuePolicyRejected,
    SourceValueProjectionForged,
    SourceValueProjectionSetV1,
    SourceValueReplayRejected,
    SourceValueRepresentationDrift,
    SourceValueWithheld,
    project_source_values,
    verify_source_value_projection,
)
from tests import test_cell_origin_explorer as e08
from tests import test_cna_explorer as e09
from tests import test_fragment_explorer as e07
from tests import test_projection_policy_registry as pp

FINITE = ProjectionSelectionRule.FINITE_COMPONENTS
ALL = ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS
SPAN = FragmentQuantity.ALIGNED_REFERENCE_SPAN


# --- fixtures ----------------------------------------------------------------


def _identity(definition: MethodDefinition) -> SourceMeasurementIdentity:
    return SourceMeasurementIdentity(
        method_ref=definition.method_ref,
        method_definition_sha256=method_definition_sha256(definition),
        quantity_id=definition.quantity_id,
        unit=definition.unit,
    )


def _resolve(tmp_path: Path, policy: Any) -> ResolvedProjectionPolicy:
    root = tmp_path / f"registry-{policy.policy_id}-{policy.version}"
    with ProjectionPolicyRegistry(root) as registry:
        receipt = registry.register_policy(policy)
        return registry.resolve(receipt.selector_id, receipt.policy_version)


class Case:
    """One resolved policy, its exact artifact and the builder's inputs."""

    def __init__(
        self,
        policy: ResolvedProjectionPolicy,
        artifact: Any,
        definition: MethodDefinition,
        cna_inputs: CnaReplayInputs | None = None,
    ) -> None:
        self.policy = policy
        self.artifact = artifact
        self.anchor = pp._anchor(definition)
        self.source = _identity(definition)
        self.cna_inputs = cna_inputs

    def project(self, **overrides: Any) -> SourceValueProjectionSetV1:
        values = self.kwargs()
        values.update(overrides)
        return project_source_values(values.pop("policy"), values.pop("artifact"), **values)

    def kwargs(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "artifact": self.artifact,
            "measurement_anchor": self.anchor,
            "source_measurement": self.source,
            "cna_inputs": self.cna_inputs,
        }

    def verify(self, projection: Any, **overrides: Any) -> SourceValueProjectionSetV1:
        values = self.kwargs()
        values.update(overrides)
        return verify_source_value_projection(
            projection, values.pop("policy"), values.pop("artifact"), **values
        )


def _fragment_view(
    *,
    controls: ExplorerControls | None = None,
    right_quantity: FragmentQuantity = SPAN,
    right_state: ExplorerSourceState = ExplorerSourceState.COMPLETE,
) -> FragmentExplorerView:
    left = e07._source("alpha", counts=(2, 3, 5))
    right = e07._source(
        "beta", quantity=right_quantity, counts=(1, 4, 5), state=right_state
    )
    state = e07._state(left, right, controls=controls)
    return build_fragment_explorer_view(e07._request(left, right, state=state))


def _fragment_policy(
    *,
    quantity: FragmentQuantity = SPAN,
    panel: PanelId = PanelId.A,
    **overrides: Any,
) -> Any:
    definition = e07._method(e07.METHOD_SUFFIXES[quantity], quantity)
    values: dict[str, Any] = {
        "measurement": pp._binding(definition),
        "measurement_anchor": pp._anchor(definition),
        "fragment_quantity": quantity,
        "panel": panel,
    }
    values.update(overrides)
    return pp._fragment(**values), definition


def _fragment_case(
    tmp_path: Path, view: FragmentExplorerView | None = None, **policy: Any
) -> Case:
    value, definition = _fragment_policy(**policy)
    return Case(_resolve(tmp_path, value), view or _fragment_view(), definition)


def _all_fragment(**policy: Any) -> dict[str, Any]:
    return {
        "selection_rule": ALL,
        "components": (),
        "all_component_statistics": tuple(FragmentStatistic),
        **policy,
    }


def _contributor(contributor: str) -> CellOriginProjectionComponent:
    return CellOriginProjectionComponent(
        statistic=CellOriginStatistic.ESTIMATED_FRACTION,
        statistic_unit=StatisticUnit.FRACTION,
        contributor_id=contributor,
    )


def _cell_origin_case(
    tmp_path: Path,
    *,
    artifact: CellOriginExplorerArtifact | None = None,
    definition: MethodDefinition | None = None,
    **overrides: Any,
) -> Case:
    definition = definition or e08._method()
    values: dict[str, Any] = {
        "measurement": pp._binding(definition),
        "measurement_anchor": pp._anchor(definition),
        "atlas_id": "asset_atlas_alpha",
        "atlas_sha256": e08.ATLAS_SHA,
        "components": (_contributor("liver"),),
    }
    values.update(overrides)
    resolved = _resolve(tmp_path, pp._cell_origin(**values))
    return Case(
        resolved,
        artifact or build_cell_origin_explorer_artifact(e08._request()),
        definition,
    )


def _cna_definition() -> MethodDefinition:
    return pp._method(
        pp.MethodFamily.COPY_NUMBER, quantity_id="qty_copy_number", unit="unit_log2_ratio"
    )


@pytest.fixture(scope="module")
def cna_artifact(tmp_path_factory: pytest.TempPathFactory):
    dosage, segmented, snapshot = e09._snapshot(tmp_path_factory.mktemp("cna"))
    inputs = CnaReplayInputs(
        dosage=dosage,
        segmented=segmented,
        dosage_authority=e09._authority(CnaSource.DOSAGE_QC),
        segmented_authority=e09._authority(CnaSource.SEGMENTED_CNA),
    )
    return snapshot, inputs


def _cna_case(
    tmp_path: Path,
    cna_artifact: tuple[CnaExplorerSnapshot, CnaReplayInputs],
    family: str,
    **overrides: Any,
) -> Case:
    snapshot, inputs = cna_artifact
    assert snapshot.layers is not None
    dosage_grid, segment_grid = snapshot.layers.coordinate_grids
    definition = _cna_definition()
    if family == "chromosome":
        grid = overrides.pop("coordinate_grid", dosage_grid)
        policy = pp._chromosome(
            coordinate_grid=grid,
            coordinate_grid_sha256=cna_coordinate_grid_sha256(grid),
            **overrides,
        )
    else:
        grid = overrides.pop("coordinate_grid", segment_grid)
        policy = pp._segment(
            coordinate_grid=grid,
            coordinate_grid_sha256=cna_coordinate_grid_sha256(grid),
            components=overrides.pop(
                "components", (_segment_component(0, "chr1", 0, 2_000_000),)
            ),
            **overrides,
        )
    return Case(
        _resolve(tmp_path, policy),
        overrides.get("artifact", snapshot),
        definition,
        overrides.get("cna_inputs", inputs),
    )


def _segment_component(
    index: int,
    contig: str,
    start: int,
    end: int,
    statistic: CnaSegmentStatistic = CnaSegmentStatistic.MEDIAN_LOG2,
) -> CnaSegmentProjectionComponent:
    return CnaSegmentProjectionComponent(
        statistic=statistic,
        statistic_unit=module._STATISTIC_UNIT[statistic],
        segment=CnaSegmentCoordinate(
            segment_index=index, contig=contig, start=start, end=end
        ),
    )


def _bypass_replay(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Disable family replay so the adapter's own resolution checks are reached."""

    if name == "_replay_cna":
        monkeypatch.setattr(module, name, lambda artifact, inputs: (artifact, "e" * 64))
    else:
        monkeypatch.setattr(module, name, lambda artifact: (artifact, "e" * 64))


# --- E07 fragment ------------------------------------------------------------


def test_fragment_finite_bin_projects_exact_count_and_rational_fraction(
    tmp_path: Path,
) -> None:
    case = _fragment_case(tmp_path)
    result = case.project()

    assert [(item.statistic, item.bin.bin_index) for item in result.projections] == [
        (FragmentStatistic.COUNT, 1),
        (FragmentStatistic.FRACTION, 1),
    ]
    count, fraction = result.projections
    assert isinstance(count, FragmentLongitudinalValueProjectionV1)
    assert count.count == 3 and count.fraction_numerator is None
    assert (fraction.fraction_numerator, fraction.fraction_denominator) == (3, 10)
    assert fraction.statistic_unit == StatisticUnit.FRACTION
    view = case.artifact
    assert count.view_sha256 == view.view_sha256
    assert count.result_id == "result_alpha"
    assert count.policy.policy_sha256 == case.policy.policy_sha256
    assert count.measurement.measurement_definition_sha256 == (
        case.policy.policy.measurement.measurement_definition_sha256
    )
    assert case.verify(result) == result


def test_fragment_panel_b_reads_only_its_own_panel(tmp_path: Path) -> None:
    result = _fragment_case(tmp_path, panel=PanelId.B).project()
    assert [item.count for item in result.projections[:1]] == [4]
    assert {item.result_id for item in result.projections} == {"result_beta"}


def test_fragment_canonical_all_emits_every_chart_bin_in_order(tmp_path: Path) -> None:
    result = _fragment_case(tmp_path, **_all_fragment()).project()

    keys = [(item.bin.bin_index, item.statistic) for item in result.projections]
    assert keys == [
        (index, statistic) for index in range(3) for statistic in FragmentStatistic
    ]
    assert [item.count for item in result.projections[::2]] == [2, 3, 5]
    assert result.projections[-1].bin.upper_exclusive is None


def test_fragment_display_filter_cannot_silently_drop_a_bin(tmp_path: Path) -> None:
    hidden = _fragment_view(
        controls=ExplorerControls(bin_start_inclusive=0, bin_end_exclusive=3,
                                  minimum_count_inclusive=3)
    )
    with pytest.raises(SourceValueCoordinateUnresolved, match="panel bin"):
        _fragment_case(tmp_path, hidden, **_all_fragment()).project()
    windowed = _fragment_view(
        controls=ExplorerControls(bin_start_inclusive=0, bin_end_exclusive=1)
    )
    with pytest.raises(SourceValueCoordinateUnresolved, match="matched 0"):
        _fragment_case(tmp_path / "w", windowed).project()


@pytest.mark.parametrize(
    "bin_",
    [pp._bin(1, 100, 150), pp._bin(1, 90, 200), pp._bin(0, 100, 200), pp._bin(7, 700, 800)],
)
def test_fragment_shifted_or_absent_bin_fails_closed(tmp_path: Path, bin_: Any) -> None:
    components = (pp._fragment_component(FragmentStatistic.COUNT, bin_),)
    with pytest.raises(SourceValueCoordinateUnresolved):
        _fragment_case(tmp_path, components=components).project()


def test_fragment_wrong_panel_quantity_or_withheld_panel_fails_closed(
    tmp_path: Path,
) -> None:
    raw = _fragment_view(right_quantity=FragmentQuantity.RAW_QUERY_LENGTH)
    with pytest.raises(SourceValueMeasurementMismatch, match="quantity"):
        _fragment_case(tmp_path, raw, panel=PanelId.B).project()
    raw_policy = _fragment_case(
        tmp_path / "raw",
        raw,
        panel=PanelId.A,
        quantity=FragmentQuantity.RAW_QUERY_LENGTH,
    )
    with pytest.raises(SourceValueMeasurementMismatch, match="quantity"):
        raw_policy.project()
    failed = _fragment_view(right_state=ExplorerSourceState.FAILED)
    with pytest.raises(SourceValueWithheld):
        _fragment_case(tmp_path / "failed", failed, panel=PanelId.B).project()


def test_fragment_duplicate_row_is_ambiguous_not_first_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _fragment_case(tmp_path)
    view = case.artifact
    duplicated = view.model_copy(
        update={"accessible_rows": (*view.accessible_rows, view.accessible_rows[1])}
    )
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=duplicated)
    _bypass_replay(monkeypatch, "_replay_fragment")
    with pytest.raises(SourceValueCoordinateUnresolved, match="table bin.*matched 2"):
        case.project(artifact=duplicated)
    left = view.left.model_copy(update={"rows": (*view.left.rows, view.left.rows[1])})
    with pytest.raises(SourceValueCoordinateUnresolved, match="panel bin.*matched 2"):
        case.project(artifact=view.model_copy(update={"left": left}))


@pytest.mark.parametrize("target", ["table", "panel", "fraction"])
def test_fragment_chart_table_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    case = _fragment_case(tmp_path)
    view = case.artifact
    if target == "table":
        row = view.accessible_rows[1].model_copy(update={"count": 4})
        drifted = view.model_copy(
            update={"accessible_rows": (view.accessible_rows[0], row, *view.accessible_rows[2:])}
        )
    elif target == "panel":
        rows = list(view.left.rows)
        rows[1] = rows[1].model_copy(update={"count": 4, "fraction_numerator": 4})
        drifted = view.model_copy(
            update={"left": view.left.model_copy(update={"rows": tuple(rows)})}
        )
    else:
        row = view.accessible_rows[1].model_copy(update={"fraction_denominator": 11})
        drifted = view.model_copy(
            update={"accessible_rows": (view.accessible_rows[0], row, *view.accessible_rows[2:])}
        )
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=drifted)
    _bypass_replay(monkeypatch, "_replay_fragment")
    with pytest.raises(SourceValueRepresentationDrift):
        case.project(artifact=drifted)
    # Each family-replay bypass still projects the untouched artifact.
    assert case.project().projections[0].count == 3


def test_fragment_e02_chart_and_measurement_table_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _fragment_case(tmp_path)
    view = case.artifact
    source = view.request.sources[0]
    assert source.measurement is not None
    histogram = list(source.measurement.histogram)
    histogram[1] = histogram[1].model_copy(update={"count": 4})
    measurement = source.measurement.model_copy(update={"histogram": tuple(histogram)})
    request = view.request.model_copy(
        update={
            "sources": (
                source.model_copy(update={"measurement": measurement}),
                *view.request.sources[1:],
            )
        }
    )
    drifted = view.model_copy(update={"request": request})
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=drifted)
    _bypass_replay(monkeypatch, "_replay_fragment")
    with pytest.raises(SourceValueRepresentationDrift, match="E02"):
        case.project(artifact=drifted)


# --- E08 cell origin ---------------------------------------------------------


def test_cell_origin_contributor_projects_estimate_and_authorized_interval(
    tmp_path: Path,
) -> None:
    case = _cell_origin_case(tmp_path)
    result = case.project()

    (liver,) = result.projections
    assert isinstance(liver, CellOriginLongitudinalValueProjectionV1)
    assert liver.contributor_id == "liver"
    assert liver.point_estimate == pytest.approx(2 / 3)
    assert liver.interval_state == BootstrapInformationStatus.AVAILABLE
    assert liver.lower_fraction is not None and liver.upper_fraction is not None
    assert liver.atlas_sha256 == e08.ATLAS_SHA
    assert case.verify(result) == result


def test_cell_origin_canonical_all_uses_contributor_id_not_display_rank(
    tmp_path: Path,
) -> None:
    case = _cell_origin_case(
        tmp_path,
        selection_rule=ALL,
        components=(),
        all_component_statistics=(CellOriginStatistic.ESTIMATED_FRACTION,),
    )
    display = [row.contributor_id for row in case.artifact.view.dot_interval_rows]
    assert display == ["liver", "immune"]

    result = case.project()
    assert [item.contributor_id for item in result.projections] == ["immune", "liver"]


def test_cell_origin_unavailable_interval_projects_no_bounds(tmp_path: Path) -> None:
    artifact = build_cell_origin_explorer_artifact(
        e08._request(bundle=e08._bundle(unavailable_second_interval=True))
    )
    case = _cell_origin_case(
        tmp_path,
        artifact=artifact,
        components=(_contributor("immune"), _contributor("liver")),
    )
    states = {
        item.contributor_id: (item.interval_state, item.lower_fraction)
        for item in case.project().projections
    }
    unavailable = [state for state in states.values() if state[1] is None]
    assert unavailable == [(BootstrapInformationStatus.INSUFFICIENT_INFORMATION, None)]


def test_cell_origin_unregistered_contributor_or_atlas_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(SourceValueCoordinateUnresolved, match="registered contributor"):
        _cell_origin_case(tmp_path, components=(_contributor("hepatocyte"),)).project()
    with pytest.raises(SourceValueFamilyMismatch, match="atlas"):
        _cell_origin_case(tmp_path / "atlas", atlas_sha256="f" * 64).project()


def test_cell_origin_withheld_artifact_returns_no_value(tmp_path: Path) -> None:
    artifact = build_cell_origin_explorer_artifact(
        e08._request(trust=E05TrustState.REVOKED)
    )
    with pytest.raises(SourceValueWithheld):
        _cell_origin_case(tmp_path, artifact=artifact).project()


def test_cell_origin_record_must_be_the_builder_source(tmp_path: Path) -> None:
    other = e08._method().model_copy(update={"version": "2.0.0"})
    other = MethodDefinition.model_validate(other.model_dump())
    case = _cell_origin_case(tmp_path, definition=other)
    with pytest.raises(SourceValueMeasurementMismatch, match="source record"):
        case.project()


def test_cell_origin_dot_table_drift_and_duplicates_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _cell_origin_case(tmp_path)
    view = case.artifact.view
    dots = list(view.dot_interval_rows)
    dots[0] = dots[0].model_copy(update={"estimate_fraction": 0.5})
    drifted = case.artifact.model_copy(
        update={"view": view.model_copy(update={"dot_interval_rows": tuple(dots)})}
    )
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=drifted)
    _bypass_replay(monkeypatch, "_replay_cell_origin")
    with pytest.raises(SourceValueRepresentationDrift, match="dot and table"):
        case.project(artifact=drifted)
    both = view.model_copy(
        update={
            "dot_interval_rows": tuple(
                row.model_copy(update={"estimate_fraction": 0.5})
                if row.contributor_id == "liver"
                else row
                for row in view.dot_interval_rows
            ),
            "exact_table_rows": tuple(
                row.model_copy(update={"estimate_fraction": 0.5})
                if row.contributor_id == "liver"
                else row
                for row in view.exact_table_rows
            ),
        }
    )
    with pytest.raises(SourceValueRepresentationDrift, match="result"):
        case.project(artifact=case.artifact.model_copy(update={"view": both}))
    duplicate = view.model_copy(
        update={
            "dot_interval_rows": (*view.dot_interval_rows, view.dot_interval_rows[0]),
            "exact_table_rows": (*view.exact_table_rows, view.exact_table_rows[0]),
        }
    )
    with pytest.raises(SourceValueCoordinateUnresolved, match="matched 2"):
        case.project(artifact=case.artifact.model_copy(update={"view": duplicate}))


# --- E09 CNA -----------------------------------------------------------------


def test_cna_chromosome_finite_and_canonical_all(tmp_path: Path, cna_artifact) -> None:
    snapshot, inputs = cna_artifact
    case = _cna_case(tmp_path, cna_artifact, "chromosome")
    (chr7,) = case.project().projections
    assert isinstance(chr7, CnaChromosomeLongitudinalValueProjectionV1)
    layer = snapshot.layers.dosage_chromosomes[6]
    assert chr7.chromosome == "chr7" and chr7.real_value == layer.log2_ratio
    assert chr7.integer_value is None
    assert chr7.coordinate_grid_sha256 == cna_coordinate_grid_sha256(
        snapshot.layers.coordinate_grids[0]
    )

    everything = _cna_case(
        tmp_path / "all",
        cna_artifact,
        "chromosome",
        selection_rule=ALL,
        components=(),
        all_component_statistics=tuple(CnaChromosomeStatistic),
    ).project()
    assert len(everything.projections) == 22 * 3
    assert [item.chromosome for item in everything.projections[::3]] == [
        f"chr{index}" for index in range(1, 23)
    ]
    counts = everything.projections[0::3]
    assert [item.integer_value for item in counts] == [
        item.accepted_read_count for item in inputs.dosage.chromosomes
    ]


def test_cna_segment_finite_and_canonical_all(tmp_path: Path, cna_artifact) -> None:
    snapshot, _ = cna_artifact
    case = _cna_case(tmp_path, cna_artifact, "segment")
    (segment,) = case.project().projections
    assert segment.real_value == snapshot.layers.segments[0].median_log2
    assert case.verify(case.project()).projections == (segment,)

    everything = _cna_case(
        tmp_path / "all",
        cna_artifact,
        "segment",
        selection_rule=ALL,
        components=(),
        all_component_statistics=tuple(CnaSegmentStatistic),
    ).project()
    assert [
        (item.segment.segment_index, item.statistic) for item in everything.projections
    ] == [(index, statistic) for index in range(2) for statistic in CnaSegmentStatistic]
    copy_numbers = [
        item.integer_value
        for item in everything.projections
        if item.statistic == CnaSegmentStatistic.UPSTREAM_COPY_NUMBER
    ]
    assert copy_numbers == [1, 2]


@pytest.mark.parametrize(
    "component",
    [
        _segment_component(0, "chr1", 0, 1_500_000),
        _segment_component(0, "chr1", 0, 4_000_000),
        _segment_component(1, "chr1", 0, 2_000_000),
        _segment_component(5, "chr1", 9_000_000, 10_000_000),
    ],
)
def test_cna_shifted_or_absent_segment_fails_closed(
    tmp_path: Path, cna_artifact, component: Any
) -> None:
    case = _cna_case(tmp_path, cna_artifact, "segment", components=(component,))
    with pytest.raises(SourceValueCoordinateUnresolved):
        case.project()


def test_cna_grid_or_family_alias_fails_closed(tmp_path: Path, cna_artifact) -> None:
    snapshot, _ = cna_artifact
    dosage_grid = snapshot.layers.coordinate_grids[0]
    shifted = CoordinateGridLayer(
        source=CnaSource.DOSAGE_QC,
        contig_order=dosage_grid.contig_order,
        bin_definition_sha256="c" * 64,
        bin_count=dosage_grid.bin_count,
    )
    with pytest.raises(SourceValueFamilyMismatch, match="grid"):
        _cna_case(tmp_path, cna_artifact, "chromosome", coordinate_grid=shifted).project()
    undeclared = CoordinateGridLayer(
        source=CnaSource.DOSAGE_QC,
        contig_order=("chr7",),
        bin_definition_sha256=dosage_grid.bin_definition_sha256,
        bin_count=dosage_grid.bin_count,
    )
    with pytest.raises(SourceValueFamilyMismatch, match="grid"):
        _cna_case(
            tmp_path / "u", cna_artifact, "chromosome", coordinate_grid=undeclared
        ).project()


def test_cna_unavailable_snapshot_returns_no_value(tmp_path: Path, cna_artifact) -> None:
    _, inputs = cna_artifact
    revoked = inputs.segmented_authority.model_copy(
        update={"trust_state": TrustState.REVOKED}
    )
    unavailable = build_cna_explorer_snapshot(
        inputs.dosage,
        inputs.segmented,
        dosage_authority=inputs.dosage_authority,
        segmented_authority=revoked,
    )
    revoked_inputs = CnaReplayInputs(
        dosage=inputs.dosage,
        segmented=inputs.segmented,
        dosage_authority=inputs.dosage_authority,
        segmented_authority=revoked,
    )
    case = _cna_case(tmp_path, cna_artifact, "chromosome")
    with pytest.raises(SourceValueWithheld):
        case.project(artifact=unavailable, cna_inputs=revoked_inputs)
    # A snapshot cannot be replayed against other authority.
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=unavailable)


@pytest.mark.parametrize("target", ["table", "chart", "upstream"])
def test_cna_segment_chart_table_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cna_artifact, target: str
) -> None:
    snapshot, inputs = cna_artifact
    case = _cna_case(tmp_path, cna_artifact, "segment")
    segments = list(snapshot.tables.segments)
    segments[0] = segments[0].model_copy(update={"median_log2": 0.5})
    if target == "table":
        drifted = snapshot.model_copy(
            update={"tables": snapshot.tables.model_copy(update={"segments": tuple(segments)})}
        )
    elif target == "chart":
        drifted = snapshot.model_copy(
            update={"chart": snapshot.chart.model_copy(update={"segments": tuple(segments)})}
        )
    else:
        layers = snapshot.layers.model_copy(update={"segments": tuple(segments)})
        drifted = snapshot.model_copy(
            update={
                "layers": layers,
                "chart": snapshot.chart.model_copy(update={"segments": tuple(segments)}),
                "tables": snapshot.tables.model_copy(update={"segments": tuple(segments)}),
            }
        )
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=drifted)
    _bypass_replay(monkeypatch, "_replay_cna")
    with pytest.raises(SourceValueRepresentationDrift):
        case.project(artifact=drifted)


def test_cna_dosage_chart_layer_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cna_artifact
) -> None:
    snapshot, _ = cna_artifact
    case = _cna_case(tmp_path, cna_artifact, "chromosome")
    rows = list(snapshot.chart.dosage_chromosomes)
    rows[6] = rows[6].model_copy(update={"log2_ratio": 9.0})
    drifted = snapshot.model_copy(
        update={"chart": snapshot.chart.model_copy(update={"dosage_chromosomes": tuple(rows)})}
    )
    _bypass_replay(monkeypatch, "_replay_cna")
    with pytest.raises(SourceValueRepresentationDrift):
        case.project(artifact=drifted)
    doubled = snapshot.model_copy(
        update={
            "chart": snapshot.chart.model_copy(
                update={"dosage_chromosomes": (*snapshot.chart.dosage_chromosomes, snapshot.chart.dosage_chromosomes[6])}
            )
        }
    )
    with pytest.raises(SourceValueCoordinateUnresolved, match="matched 2"):
        case.project(artifact=doubled)


# --- cross-family closure ----------------------------------------------------


def test_wrong_family_artifact_or_inputs_fail_closed(tmp_path: Path, cna_artifact) -> None:
    snapshot, inputs = cna_artifact
    fragment = _fragment_case(tmp_path / "f")
    cell = _cell_origin_case(tmp_path / "c")
    chromosome = _cna_case(tmp_path / "k", cna_artifact, "chromosome")
    with pytest.raises(SourceValueFamilyMismatch):
        fragment.project(artifact=cell.artifact)
    with pytest.raises(SourceValueFamilyMismatch):
        cell.project(artifact=fragment.artifact)
    with pytest.raises(SourceValueFamilyMismatch):
        fragment.project(artifact=snapshot, cna_inputs=inputs)
    with pytest.raises(SourceValueFamilyMismatch):
        chromosome.project(cna_inputs=None)
    with pytest.raises(SourceValueFamilyMismatch):
        chromosome.project(artifact=fragment.artifact)


def test_d05_anchor_and_d02_source_measurement_are_required_exactly(
    tmp_path: Path, cna_artifact
) -> None:
    for case in (
        _fragment_case(tmp_path / "f"),
        _cell_origin_case(tmp_path / "c"),
        _cna_case(tmp_path / "k", cna_artifact, "chromosome"),
    ):
        wrong_anchor = MeasurementAnchor(
            measurement_definition_sha256=case.anchor.measurement_definition_sha256,
            anchor_definition_sha256="0" * 64,
            authority_sha256=case.anchor.authority_sha256,
        )
        with pytest.raises(SourceValueMeasurementMismatch):
            case.project(measurement_anchor=wrong_anchor)
        wrong_unit = case.source.model_copy(update={"unit": "unit_other"})
        with pytest.raises(SourceValueMeasurementMismatch):
            case.project(source_measurement=wrong_unit)
        wrong_quantity = SourceMeasurementIdentity.model_validate(
            {**case.source.model_dump(), "quantity_id": "qty_other"}
        )
        with pytest.raises(SourceValueMeasurementMismatch):
            case.project(source_measurement=wrong_quantity)
        with pytest.raises(SourceValueMeasurementMismatch):
            case.project(source_measurement=case.source.model_dump())


def test_caller_created_scalar_or_incomplete_vector_never_verifies(
    tmp_path: Path, cna_artifact
) -> None:
    case = _fragment_case(tmp_path, **_all_fragment())
    result = case.project()
    edited = result.projections[0].model_copy(update={"count": 99})
    forged = SourceValueProjectionSetV1.model_validate(
        {**result.model_dump(), "projections": (edited, *result.projections[1:])}
    )
    with pytest.raises(SourceValueProjectionForged):
        case.verify(forged)
    subset = SourceValueProjectionSetV1.model_validate(
        {**result.model_dump(), "projections": result.projections[:2]}
    )
    with pytest.raises(SourceValueProjectionForged):
        case.verify(subset)
    with pytest.raises(SourceValueProjectionForged):
        case.verify(result.model_dump())
    other = _fragment_case(tmp_path / "o", panel=PanelId.B, **_all_fragment())
    with pytest.raises(SourceValueProjectionForged):
        other.verify(result)
    unvalidated = SourceValueProjectionSetV1.model_construct(**dict(result))
    with pytest.raises(SourceValueProjectionForged):
        case.verify(unvalidated.model_copy(update={"artifact_sha256": "nope"}))
    assert case.verify(result) == result
    # The entry point takes no value, subset, ordering or selector.
    assert list(inspect.signature(project_source_values).parameters) == [
        "policy",
        "artifact",
        "measurement_anchor",
        "source_measurement",
        "cna_inputs",
    ]


@pytest.mark.parametrize(
    "update",
    [
        {"selection_rule": "top_components"},
        {"all_component_statistics": ("maximum",), "selection_rule": ALL, "components": ()},
        {"components": ()},
    ],
)
def test_value_ranked_or_forged_policy_is_rejected(
    tmp_path: Path, update: dict[str, Any]
) -> None:
    case = _fragment_case(tmp_path)
    forged_policy = case.policy.policy.model_copy(update=update)
    forged = case.policy.model_copy(update={"policy": forged_policy})
    with pytest.raises(SourceValuePolicyRejected):
        case.project(policy=forged)
    with pytest.raises(SourceValuePolicyRejected):
        case.project(policy=case.policy.model_dump())


def test_model_construct_artifacts_are_replayed_not_trusted(
    tmp_path: Path, cna_artifact
) -> None:
    fragment = _fragment_case(tmp_path / "f")
    view = fragment.artifact
    forged = FragmentExplorerView.model_construct(**{**dict(view), "view_sha256": "0" * 64})
    with pytest.raises(SourceValueReplayRejected):
        fragment.project(artifact=forged)

    class Subclass(FragmentExplorerView):
        pass

    with pytest.raises(SourceValueFamilyMismatch):
        fragment.project(artifact=Subclass.model_validate(view.model_dump()))
    snapshot, inputs = cna_artifact
    chromosome = _cna_case(tmp_path / "k", cna_artifact, "chromosome")
    with pytest.raises(SourceValueReplayRejected):
        chromosome.project(
            cna_inputs=CnaReplayInputs(
                dosage=inputs.dosage.model_dump(),  # type: ignore[arg-type]
                segmented=inputs.segmented,
                dosage_authority=inputs.dosage_authority,
                segmented_authority=inputs.segmented_authority,
            )
        )


# --- projection contracts ----------------------------------------------------


def test_projection_contracts_are_closed_and_value_shaped(tmp_path: Path) -> None:
    result = _fragment_case(tmp_path).project()
    count, fraction = result.projections
    payload = count.model_dump()
    with pytest.raises(ValidationError):
        FragmentLongitudinalValueProjectionV1.model_validate(
            {**payload, "fraction_numerator": 3, "fraction_denominator": 10}
        )
    with pytest.raises(ValidationError):
        FragmentLongitudinalValueProjectionV1.model_validate(
            {**payload, "statistic_unit": StatisticUnit.FRACTION}
        )
    with pytest.raises(ValidationError):
        FragmentLongitudinalValueProjectionV1.model_validate({**payload, "rank": 1})
    with pytest.raises(ValidationError):
        FragmentLongitudinalValueProjectionV1.model_validate(
            {**fraction.model_dump(), "fraction_numerator": 11}
        )
    with pytest.raises(ValidationError, match="canonically ordered"):
        SourceValueProjectionSetV1.model_validate(
            {**result.model_dump(), "projections": (fraction, count)}
        )
    with pytest.raises(ValidationError, match="one family and binding"):
        SourceValueProjectionSetV1.model_validate(
            {
                **result.model_dump(),
                "projections": (count.model_copy(update={"artifact_sha256": "1" * 64}),),
            }
        )
    with pytest.raises(ValidationError):
        count.model_copy(update={"count": 2}).__class__.model_validate(
            {**payload, "count": True}
        )


def test_cell_origin_and_cna_contracts_reject_unauthorized_shapes(
    tmp_path: Path, cna_artifact
) -> None:
    (liver,) = _cell_origin_case(tmp_path / "c").project().projections
    with pytest.raises(ValidationError, match="bounds"):
        CellOriginLongitudinalValueProjectionV1.model_validate(
            {**liver.model_dump(), "interval_state": "not_run"}
        )
    with pytest.raises(ValidationError, match="contain"):
        CellOriginLongitudinalValueProjectionV1.model_validate(
            {**liver.model_dump(), "point_estimate": 0.01}
        )
    (chr7,) = _cna_case(tmp_path / "k", cna_artifact, "chromosome").project().projections
    with pytest.raises(ValidationError, match="value type"):
        CnaChromosomeLongitudinalValueProjectionV1.model_validate(
            {**chr7.model_dump(), "integer_value": 1}
        )
    with pytest.raises(ValidationError):
        CnaChromosomeLongitudinalValueProjectionV1.model_validate(
            {**chr7.model_dump(), "chromosome": "chrX"}
        )
    with pytest.raises(ValidationError):
        CnaChromosomeLongitudinalValueProjectionV1.model_validate(
            {**chr7.model_dump(), "cna_source": "segmented_cna"}
        )


def test_chromosome_policy_components_use_registry_units() -> None:
    component = CnaChromosomeProjectionComponent(
        statistic=CnaChromosomeStatistic.ACCEPTED_READ_COUNT,
        statistic_unit=StatisticUnit.READ_COUNT,
        chromosome="chr1",
    )
    assert module._STATISTIC_UNIT[component.statistic] == component.statistic_unit


def test_nested_caller_subclasses_are_rejected_not_normalized(tmp_path: Path) -> None:
    from evidence_inspector.fragment_explorer import ExplorerBinRow

    class Row(ExplorerBinRow):
        pass

    case = _fragment_case(tmp_path)
    view = case.artifact
    rows = (
        view.left.rows[0],
        Row.model_validate(view.left.rows[1].model_dump()),
        *view.left.rows[2:],
    )
    nested = view.model_copy(update={"left": view.left.model_copy(update={"rows": rows})})
    with pytest.raises(SourceValueReplayRejected):
        case.project(artifact=nested)

    class Projection(FragmentLongitudinalValueProjectionV1):
        pass

    result = case.project()
    first = Projection.model_validate(result.projections[0].model_dump())
    disguised = result.model_copy(update={"projections": (first, *result.projections[1:])})
    with pytest.raises(SourceValueProjectionForged):
        case.verify(disguised)


def test_projection_binds_the_exact_registry_head(tmp_path: Path) -> None:
    policy, definition = _fragment_policy()
    other, _ = _fragment_policy(policy_id="projpol_fragment_other")
    with ProjectionPolicyRegistry(tmp_path / "registry") as registry:
        receipt = registry.register_policy(policy)
        before = registry.resolve(receipt.selector_id, 1)
        registry.register_policy(other)
        after = registry.resolve(receipt.selector_id, 1)
    view = _fragment_view()
    first = Case(before, view, definition).project()
    second = Case(after, view, definition).project()
    assert first.policy.state_head_sha256 == before.state_head_sha256
    assert (first.policy.state_version, second.policy.state_version) == (1, 2)
    assert first != second
    with pytest.raises(SourceValueProjectionForged):
        Case(after, view, definition).verify(first)
