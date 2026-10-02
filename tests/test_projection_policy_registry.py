"""Protected E12 projection-policy registry: closed policies and D03-parity storage."""

from __future__ import annotations

import fcntl
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evidence_inspector.projection_policy_registry as registry_module
from tests import registry_storage_checks as storage_checks
from evidence_inspector.cna_explorer import CnaSource, CoordinateGridLayer
from evidence_inspector.cohort_manifest import MeasurementAnchor
from evidence_inspector.fragment_explorer import FragmentQuantity, PanelId
from evidence_inspector.longitudinal_compatibility import (
    measurement_definition_sha256,
)
from evidence_inspector.method_registry import (
    AssetReference,
    MethodDefinition,
    MethodFamily,
    ToolReference,
    method_definition_sha256,
)
from evidence_inspector.projection_policy_registry import (
    CellOriginProjectionComponent,
    CellOriginProjectionPolicyV1,
    CellOriginStatistic,
    CnaChromosomeProjectionComponent,
    CnaChromosomeProjectionPolicyV1,
    CnaChromosomeStatistic,
    CnaSegmentCoordinate,
    CnaSegmentProjectionComponent,
    CnaSegmentProjectionPolicyV1,
    CnaSegmentStatistic,
    FragmentBinCoordinate,
    FragmentProjectionComponent,
    FragmentProjectionPolicyV1,
    FragmentStatistic,
    ProjectionFamily,
    ProjectionMeasurementBinding,
    ProjectionPolicyRegistry,
    ProjectionPolicyRegistryConflict,
    ProjectionPolicyRegistryUnsafe,
    ProjectionSelectionRule,
    ResolvedProjectionPolicy,
    StatisticUnit,
    cna_coordinate_grid_sha256,
    projection_policy_backup_from_bytes,
    projection_policy_sha256,
    require_projection_policy_binding,
)

FINITE = ProjectionSelectionRule.FINITE_COMPONENTS
ALL = ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS
AUTOSOMES = tuple(f"chr{index}" for index in range(1, 23))


def _method(
    family: MethodFamily,
    *,
    quantity_id: str = "qty_fragment_aligned_reference_span",
    unit: str = "unit_bp",
) -> MethodDefinition:
    return MethodDefinition(
        method_id=f"mth_{family.value}_alpha",
        version="1.0.0",
        family=family,
        quantity_id=quantity_id,
        unit=unit,
        parameter_schema_sha256="5" * 64,
        tools=(
            ToolReference(
                tool_id="tool_projection_alpha", version="1.0.0", artifact_sha256="6" * 64
            ),
        ),
        assets=(
            AssetReference(
                asset_id="asset_projection_alpha", version="1.0.0", content_sha256="7" * 64
            ),
        ),
    )


def _binding(definition: MethodDefinition) -> ProjectionMeasurementBinding:
    digest = method_definition_sha256(definition)
    return ProjectionMeasurementBinding(
        method_definition=definition,
        method_ref=definition.method_ref,
        method_definition_sha256=digest,
        quantity_id=definition.quantity_id,
        unit=definition.unit,
        measurement_definition_sha256=measurement_definition_sha256(
            definition.method_ref, digest, definition.quantity_id, definition.unit
        ),
    )


def _anchor(definition: MethodDefinition) -> MeasurementAnchor:
    return MeasurementAnchor(
        measurement_definition_sha256=method_definition_sha256(definition),
        anchor_definition_sha256="9" * 64,
        authority_sha256="8" * 64,
    )


def _common(family: MethodFamily, **method: Any) -> dict[str, Any]:
    definition = _method(family, **method)
    return {"measurement": _binding(definition), "measurement_anchor": _anchor(definition)}


def _bin(index: int, lower: int, upper: int | None) -> FragmentBinCoordinate:
    return FragmentBinCoordinate(
        bin_index=index, lower_inclusive=lower, upper_exclusive=upper
    )


def _fragment_component(
    statistic: FragmentStatistic, bin_: FragmentBinCoordinate
) -> FragmentProjectionComponent:
    unit = (
        StatisticUnit.ALIGNMENT_COUNT
        if statistic == FragmentStatistic.COUNT
        else StatisticUnit.FRACTION
    )
    return FragmentProjectionComponent(statistic=statistic, statistic_unit=unit, bin=bin_)


def _fragment(**overrides: Any) -> FragmentProjectionPolicyV1:
    values: dict[str, Any] = {
        "policy_id": "projpol_fragment_span",
        "version": 1,
        **_common(MethodFamily.FRAGMENT_MEASUREMENT),
        "fragment_quantity": FragmentQuantity.ALIGNED_REFERENCE_SPAN,
        "panel": PanelId.A,
        "selection_rule": FINITE,
        "components": (
            _fragment_component(FragmentStatistic.COUNT, _bin(1, 100, 200)),
            _fragment_component(FragmentStatistic.FRACTION, _bin(1, 100, 200)),
        ),
    }
    values.update(overrides)
    return FragmentProjectionPolicyV1(**values)


def _cell_origin(**overrides: Any) -> CellOriginProjectionPolicyV1:
    values: dict[str, Any] = {
        "policy_id": "projpol_cell_origin",
        "version": 1,
        **_common(
            MethodFamily.CELL_ORIGIN,
            quantity_id="qty_cell_origin_fraction",
            unit="unit_fraction",
        ),
        "atlas_id": "asset_atlas_alpha",
        "atlas_sha256": "a" * 64,
        "selection_rule": FINITE,
        "components": (
            CellOriginProjectionComponent(
                statistic=CellOriginStatistic.ESTIMATED_FRACTION,
                statistic_unit=StatisticUnit.FRACTION,
                contributor_id="hepatocyte",
            ),
        ),
    }
    values.update(overrides)
    return CellOriginProjectionPolicyV1(**values)


def _grid(source: CnaSource, contigs: tuple[str, ...] = AUTOSOMES) -> CoordinateGridLayer:
    return CoordinateGridLayer(
        source=source,
        contig_order=contigs,
        bin_definition_sha256="b" * 64,
        bin_count=len(contigs),
    )


def _cna_common() -> dict[str, Any]:
    return _common(
        MethodFamily.COPY_NUMBER, quantity_id="qty_copy_number", unit="unit_log2_ratio"
    )


def _chromosome(**overrides: Any) -> CnaChromosomeProjectionPolicyV1:
    grid = overrides.pop("coordinate_grid", _grid(CnaSource.DOSAGE_QC))
    values: dict[str, Any] = {
        "policy_id": "projpol_cna_dosage",
        "version": 1,
        **_cna_common(),
        "coordinate_grid": grid,
        "coordinate_grid_sha256": cna_coordinate_grid_sha256(grid),
        "selection_rule": FINITE,
        "components": (
            CnaChromosomeProjectionComponent(
                statistic=CnaChromosomeStatistic.LOG2_RATIO,
                statistic_unit=StatisticUnit.LOG2_RATIO,
                chromosome="chr7",
            ),
        ),
    }
    values.update(overrides)
    return CnaChromosomeProjectionPolicyV1(**values)


def _segment_component(
    index: int, contig: str, start: int, end: int
) -> CnaSegmentProjectionComponent:
    return CnaSegmentProjectionComponent(
        statistic=CnaSegmentStatistic.MEDIAN_LOG2,
        statistic_unit=StatisticUnit.LOG2_RATIO,
        segment=CnaSegmentCoordinate(
            segment_index=index, contig=contig, start=start, end=end
        ),
    )


def _segment(**overrides: Any) -> CnaSegmentProjectionPolicyV1:
    grid = overrides.pop(
        "coordinate_grid", _grid(CnaSource.SEGMENTED_CNA, ("chr1", "chr2"))
    )
    values: dict[str, Any] = {
        "policy_id": "projpol_cna_segment",
        "version": 1,
        **_cna_common(),
        "coordinate_grid": grid,
        "coordinate_grid_sha256": cna_coordinate_grid_sha256(grid),
        "selection_rule": FINITE,
        "components": (
            _segment_component(0, "chr1", 0, 1_000_000),
            _segment_component(1, "chr1", 1_000_000, 2_000_000),
        ),
    }
    values.update(overrides)
    return CnaSegmentProjectionPolicyV1(**values)


def _payload(policy: Any) -> dict[str, Any]:
    return json.loads(policy.model_dump_json())


def _validate(model: Any, payload: dict[str, Any]) -> Any:
    """Validate as stored objects are parsed: from exact JSON."""

    return model.model_validate_json(json.dumps(payload))


@pytest.fixture
def registry(tmp_path: Path):
    value = ProjectionPolicyRegistry(tmp_path / "projection-registry")
    try:
        yield value
    finally:
        value.close()


def _reopen(registry: ProjectionPolicyRegistry, receipt, **overrides: Any):
    values = {
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    values.update(overrides)
    return ProjectionPolicyRegistry(registry.root, **values)


# --- Closed policy contracts -------------------------------------------------


def test_each_family_policy_registers_and_resolves_by_selector_version_only(
    registry: ProjectionPolicyRegistry,
) -> None:
    policies = (_fragment(), _cell_origin(), _chromosome(), _segment())
    receipts = [registry.register_policy(policy) for policy in policies]
    assert [item.family for item in receipts] == list(ProjectionFamily)
    for policy, receipt in zip(policies, receipts, strict=True):
        resolved = registry.resolve(receipt.selector_id, 1)
        assert resolved.policy == policy
        assert resolved.policy_sha256 == projection_policy_sha256(policy)
        assert resolved.object_sha256 == receipt.object_sha256
        assert resolved.registry_id == receipt.registry_id
        assert resolved.state_head_sha256 == receipts[-1].state_head_sha256
        assert resolved.live_authority_replayed is False
        assert resolved.clinical_use_authorized is False
    assert registry.register_policy(_fragment()) == receipts[0].model_copy(
        update={"state_head_sha256": receipts[-1].state_head_sha256, "state_version": 4}
    )
    parameters = list(inspect.signature(ProjectionPolicyRegistry.resolve).parameters)
    assert parameters == ["self", "selector_id", "policy_version"]


def test_resolve_rejects_anything_but_an_exact_selector_and_version(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    policy_bytes = _fragment().model_dump_json()
    for selector, version in (
        (receipt.selector_id, 2),
        ("projection_policy_" + "0" * 40, 1),
    ):
        with pytest.raises(ProjectionPolicyRegistryConflict, match="unavailable"):
            registry.resolve(selector, version)
    for selector, version in (
        (receipt.selector_id, True),
        (receipt.selector_id, "1"),
        (receipt.selector_id, 0),
        (receipt.policy_sha256, 1),
        (policy_bytes, 1),
        ("projpol_fragment_span", 1),
        (receipt.selector_id.upper(), 1),
    ):
        with pytest.raises(ProjectionPolicyRegistryConflict, match="invalid"):
            registry.resolve(selector, version)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda p: p.update(selection_rule="top"),
        lambda p: p.update(selection_rule="largest"),
        lambda p: p.update(selection_rule="most_changed"),
        lambda p: p.update(selection_rule="max"),
        lambda p: p.update(selection_rule="min"),
        lambda p: p["components"][0].update(statistic="max"),
        lambda p: p["components"][0].update(statistic="minimum"),
        lambda p: p["components"][0].update(statistic="top"),
        lambda p: p.update(rank=1),
        lambda p: p.update(order_by="value_desc"),
        lambda p: p["components"][0].update(rank=1),
        lambda p: p.update(
            selection_rule="canonical_all_components",
            components=[],
            all_component_statistics=["max"],
        ),
    ),
)
def test_value_ranked_fragment_selection_is_structurally_unrepresentable(
    mutate,
) -> None:
    payload = _payload(_fragment())
    mutate(payload)
    with pytest.raises(ValidationError):
        _validate(FragmentProjectionPolicyV1, payload)


def test_registered_identifiers_are_exact_names_not_selection_rules() -> None:
    """Contributor IDs and contigs are upstream vocabulary matched exactly.

    A contributor whose name contains a ranking word (``BEST4_enterocyte``) is
    still one named contributor; nothing in the policy can turn a name into a
    value-ranked choice, because rules and statistics are closed enums.
    """

    for contributor in ("BEST4_enterocyte", "luminal_epithelial", "T-cell"):
        payload = _payload(_cell_origin())
        payload["components"][0]["contributor_id"] = contributor
        policy = _validate(CellOriginProjectionPolicyV1, payload)
        assert [item.contributor_id for item in policy.components] == [contributor]
    assert _segment(coordinate_grid=_grid(CnaSource.SEGMENTED_CNA, ("chr1", "chr2")))


def test_cell_origin_rejects_privacy_terms_and_unsorted_contributors() -> None:
    payload = _payload(_cell_origin())
    payload["components"][0]["contributor_id"] = "patient_hepatocyte"
    with pytest.raises(ValidationError, match="privacy"):
        _validate(CellOriginProjectionPolicyV1, payload)
    component = _payload(_cell_origin())["components"][0]
    payload = _payload(_cell_origin())
    payload["components"] = [
        {**component, "contributor_id": "neutrophil"},
        {**component, "contributor_id": "hepatocyte"},
    ]
    with pytest.raises(ValidationError, match="uniquely sorted"):
        _validate(CellOriginProjectionPolicyV1, payload)
    payload["components"] = [component, component]
    with pytest.raises(ValidationError, match="uniquely sorted"):
        _validate(CellOriginProjectionPolicyV1, payload)


@pytest.mark.parametrize(
    ("factory", "family"),
    (
        (_fragment, MethodFamily.CELL_ORIGIN),
        (_cell_origin, MethodFamily.FRAGMENT_MEASUREMENT),
        (_chromosome, MethodFamily.CELL_ORIGIN),
        (_segment, MethodFamily.FRAGMENT_MEASUREMENT),
    ),
)
def test_policy_family_must_match_the_e01_method_family(factory, family) -> None:
    definition = _method(family)
    with pytest.raises(ValidationError, match="E01 method family"):
        factory(measurement=_binding(definition), measurement_anchor=_anchor(definition))


def test_dosage_and_segment_coordinates_never_alias() -> None:
    dosage_grid = _grid(CnaSource.DOSAGE_QC, ("chr1", "chr2"))
    segment_grid = _grid(CnaSource.SEGMENTED_CNA, ("chr1", "chr2"))
    assert dosage_grid.model_dump(exclude={"source"}) == segment_grid.model_dump(
        exclude={"source"}
    )
    assert cna_coordinate_grid_sha256(dosage_grid) != cna_coordinate_grid_sha256(
        segment_grid
    )
    with pytest.raises(ValidationError, match="other E09 source"):
        _chromosome(coordinate_grid=segment_grid)
    with pytest.raises(ValidationError, match="other E09 source"):
        _segment(coordinate_grid=dosage_grid)
    # A segment component cannot be smuggled into a chromosome policy, nor the
    # reverse, and a family literal cannot be relabelled across schemas.
    chromosome = _payload(_chromosome())
    segment = _payload(_segment())
    for target, donor, model in (
        (chromosome, segment, CnaChromosomeProjectionPolicyV1),
        (segment, chromosome, CnaSegmentProjectionPolicyV1),
    ):
        mixed = {**target, "components": donor["components"]}
        with pytest.raises(ValidationError):
            _validate(model, mixed)
        for field in ("family", "coordinate_scheme", "cna_source", "schema_version"):
            relabelled = {**target, field: donor[field]}
            with pytest.raises(ValidationError):
                _validate(model, relabelled)
    swapped_digest = {
        **chromosome,
        "coordinate_grid_sha256": cna_coordinate_grid_sha256(
            _grid(CnaSource.SEGMENTED_CNA)
        ),
    }
    with pytest.raises(ValidationError, match="digest"):
        _validate(CnaChromosomeProjectionPolicyV1, swapped_digest)


def test_rule_and_statistic_vocabularies_are_exactly_pinned() -> None:
    assert [item.value for item in ProjectionSelectionRule] == [
        "finite_components",
        "canonical_all_components",
    ]
    assert [item.value for item in FragmentStatistic] == ["count", "fraction"]
    assert [item.value for item in CellOriginStatistic] == ["estimated_fraction"]
    assert [item.value for item in CnaChromosomeStatistic] == [
        "accepted_read_count",
        "relative_diploid_dosage",
        "log2_ratio",
    ]
    assert [item.value for item in CnaSegmentStatistic] == [
        "median_log2",
        "upstream_copy_number",
        "retained_bin_count",
        "native_span_bin_count",
    ]
    # Every statistic names an existing numeric field of its E09 layer.
    from evidence_inspector.cna_explorer import DosageChromosomeLayer, SegmentLayer

    assert {item.value for item in CnaChromosomeStatistic} <= set(
        DosageChromosomeLayer.model_fields
    )
    assert {item.value for item in CnaSegmentStatistic} <= set(
        SegmentLayer.model_fields
    )
    payload = _payload(
        _fragment(
            selection_rule=ALL,
            components=(),
            all_component_statistics=(FragmentStatistic.COUNT,),
        )
    )
    payload["selection_rule"] = "top"
    with pytest.raises(ValidationError):
        _validate(FragmentProjectionPolicyV1, payload)


def test_import_guard_rejects_a_ranked_or_unitless_vocabulary_member() -> None:
    from enum import StrEnum

    registry_module._require_closed_vocabularies(registry_module._CLOSED_VOCABULARIES)
    for value in ("max", "top_k", "mostChanged", "rank1", "largest_fraction"):
        ranked = StrEnum("RankedStatistic", {"RANKED": value})
        with pytest.raises(ValueError, match="value-ranked"):
            registry_module._require_closed_vocabularies((ranked,))
    unitless = StrEnum("UnitlessStatistic", {"MEAN": "mean"})
    with pytest.raises(ValueError, match="controlled unit"):
        registry_module._require_closed_vocabularies((unitless,))


def test_statistics_and_units_are_closed_per_family() -> None:
    for model, payload, foreign in (
        (FragmentProjectionPolicyV1, _payload(_fragment()), "estimated_fraction"),
        (CellOriginProjectionPolicyV1, _payload(_cell_origin()), "count"),
        (CnaChromosomeProjectionPolicyV1, _payload(_chromosome()), "median_log2"),
        (CnaSegmentProjectionPolicyV1, _payload(_segment()), "log2_ratio"),
    ):
        payload["components"][0]["statistic"] = foreign
        with pytest.raises(ValidationError):
            _validate(model, payload)
    payload = _payload(_fragment())
    payload["components"][0]["statistic_unit"] = "unit_fraction"
    with pytest.raises(ValidationError, match="statistic unit"):
        _validate(FragmentProjectionPolicyV1, payload)
    payload = _payload(_segment())
    payload["components"][0]["statistic_unit"] = "unit_copy_number"
    with pytest.raises(ValidationError, match="statistic unit"):
        _validate(CnaSegmentProjectionPolicyV1, payload)


def test_fragment_quantity_and_unit_must_match_e07_and_d02() -> None:
    with pytest.raises(ValidationError, match="E07"):
        _fragment(fragment_quantity=FragmentQuantity.RAW_QUERY_LENGTH)
    with pytest.raises(ValidationError, match="E07"):
        _fragment(**_common(MethodFamily.FRAGMENT_MEASUREMENT, unit="unit_fraction"))
    raw = _common(
        MethodFamily.FRAGMENT_MEASUREMENT, quantity_id="qty_fragment_raw_query_length"
    )
    assert _fragment(fragment_quantity=FragmentQuantity.RAW_QUERY_LENGTH, **raw)


@pytest.mark.parametrize(
    "bins",
    (
        ((1, 100, 200), (1, 100, 250)),  # one index, two boundaries
        ((1, 100, 200), (2, 150, 300)),  # overlap
        ((1, 100, 200), (2, 250, 300)),  # adjacent indices not contiguous
        ((1, 100, None), (3, 300, 400)),  # unbounded bin is not last
        ((2, 200, 300), (1, 100, 200)),  # unsorted
        ((1, 100, 200), (3, 50, 90)),  # later index, earlier boundary
    ),
)
def test_fragment_bins_must_be_exact_ordered_and_contiguous(bins) -> None:
    components = tuple(
        _fragment_component(FragmentStatistic.COUNT, _bin(*item)) for item in bins
    )
    with pytest.raises(ValidationError):
        _fragment(components=components)
    with pytest.raises(ValidationError, match="half-open"):
        _bin(0, 200, 100)


@pytest.mark.parametrize(
    "segments",
    (
        ((0, "chr1", 0, 100), (0, "chr1", 0, 200)),
        ((0, "chr1", 0, 100), (1, "chr1", 50, 200)),
        ((0, "chr2", 0, 100), (1, "chr1", 0, 100)),
        ((0, "chr3", 0, 100),),
        ((1, "chr1", 100, 200), (0, "chr1", 0, 100)),
    ),
)
def test_segments_must_be_exact_declared_and_non_overlapping(segments) -> None:
    components = tuple(_segment_component(*item) for item in segments)
    with pytest.raises(ValidationError):
        _segment(components=components)
    with pytest.raises(ValidationError, match="half-open"):
        CnaSegmentCoordinate(segment_index=0, contig="chr1", start=10, end=10)


def test_chromosome_must_be_declared_by_the_dosage_grid() -> None:
    with pytest.raises(ValidationError, match="not declared"):
        _chromosome(coordinate_grid=_grid(CnaSource.DOSAGE_QC, ("chr1", "chr2")))
    payload = _payload(_chromosome())
    payload["components"][0]["chromosome"] = "chrX"
    with pytest.raises(ValidationError):
        _validate(CnaChromosomeProjectionPolicyV1, payload)


def test_canonical_all_components_is_explicit_and_never_a_subset() -> None:
    policy = _fragment(
        selection_rule=ALL,
        components=(),
        all_component_statistics=(FragmentStatistic.COUNT, FragmentStatistic.FRACTION),
    )
    assert policy.components == ()
    with pytest.raises(ValidationError, match="no subset"):
        _fragment(selection_rule=ALL, all_component_statistics=(FragmentStatistic.COUNT,))
    with pytest.raises(ValidationError, match="no subset"):
        _fragment(selection_rule=ALL, components=())
    with pytest.raises(ValidationError, match="not canonical"):
        _fragment(
            selection_rule=ALL,
            components=(),
            all_component_statistics=(
                FragmentStatistic.FRACTION,
                FragmentStatistic.COUNT,
            ),
        )
    with pytest.raises(ValidationError, match="explicit components only"):
        _fragment(all_component_statistics=(FragmentStatistic.COUNT,))
    with pytest.raises(ValidationError, match="explicit components only"):
        _fragment(components=())
    for factory, statistic in (
        (_cell_origin, CellOriginStatistic.ESTIMATED_FRACTION),
        (_chromosome, CnaChromosomeStatistic.LOG2_RATIO),
        (_segment, CnaSegmentStatistic.MEDIAN_LOG2),
    ):
        assert factory(
            selection_rule=ALL, components=(), all_component_statistics=(statistic,)
        )


def test_d02_tuple_and_d05_anchor_are_bound_exactly() -> None:
    definition = _method(MethodFamily.FRAGMENT_MEASUREMENT)
    binding = _payload(_binding(definition))
    for field, value in (
        ("method_definition_sha256", "0" * 64),
        ("measurement_definition_sha256", "0" * 64),
        ("quantity_id", "qty_fragment_raw_query_length"),
        ("unit", "unit_fraction"),
        ("method_ref", {"method_id": "mth_other", "version": "1.0.0"}),
    ):
        with pytest.raises(ValidationError):
            _validate(ProjectionMeasurementBinding, {**binding, field: value})
    other = _anchor(definition).model_copy(
        update={"measurement_definition_sha256": "0" * 64}
    )
    with pytest.raises(ValidationError, match="anchor"):
        _fragment(measurement_anchor=other)


def test_consumer_binding_check_requires_exact_anchor_and_measurement(
    registry: ProjectionPolicyRegistry,
) -> None:
    policy = _fragment()
    receipt = registry.register_policy(policy)
    resolved = registry.resolve(receipt.selector_id, 1)
    exact = {
        "measurement_anchor": policy.measurement_anchor,
        "method_ref": policy.measurement.method_ref,
        "method_definition_sha256": policy.measurement.method_definition_sha256,
        "quantity_id": policy.measurement.quantity_id,
        "unit": policy.measurement.unit,
    }
    require_projection_policy_binding(resolved, **exact)
    for field, value in (
        (
            "measurement_anchor",
            policy.measurement_anchor.model_copy(
                update={"anchor_definition_sha256": "0" * 64}
            ),
        ),
        (
            "method_ref",
            policy.measurement.method_ref.model_copy(update={"version": "2.0.0"}),
        ),
        ("method_definition_sha256", "0" * 64),
        ("quantity_id", "qty_fragment_raw_query_length"),
        ("unit", "unit_fraction"),
    ):
        with pytest.raises(ProjectionPolicyRegistryConflict, match="requested"):
            require_projection_policy_binding(resolved, **{**exact, field: value})
    with pytest.raises(ProjectionPolicyRegistryConflict, match="not resolved"):
        require_projection_policy_binding(policy, **exact)  # type: ignore[arg-type]


def test_registration_accepts_only_exact_validated_family_policies(
    registry: ProjectionPolicyRegistry,
) -> None:
    valid = _cell_origin()
    forged = CellOriginProjectionPolicyV1.model_construct(
        **{
            **dict(valid),
            "components": (
                CellOriginProjectionComponent.model_construct(
                    statistic=CellOriginStatistic.ESTIMATED_FRACTION,
                    statistic_unit=StatisticUnit.READ_COUNT,
                    contributor_id="hepatocyte",
                ),
            ),
        }
    )

    class Subclass(CellOriginProjectionPolicyV1):
        pass

    subclass = Subclass(**dict(valid))
    private = valid.model_copy()
    object.__setattr__(private, "__pydantic_extra__", {"rank": 1})
    for candidate in (
        valid.model_dump(),
        valid.model_dump_json().encode(),
        projection_policy_sha256(valid),
        forged,
        subclass,
        private,
        None,
    ):
        with pytest.raises(ProjectionPolicyRegistryConflict, match="exact closed"):
            registry.register_policy(candidate)
    assert registry.list_selectors().state_version == 0


def _object_sha256_for(registry_id: str, epoch: str, policy) -> str:
    import hashlib

    stored = registry_module.RegisteredProjectionPolicyObject(
        registry_id=registry_id, registry_epoch_sha256=epoch, policy=policy
    )
    return hashlib.sha256(
        registry_module.registered_projection_object_bytes(stored)
    ).hexdigest()


def test_resolved_policy_contract_rejects_rebinding() -> None:
    policy = _fragment()
    epoch = "e" * 64
    values = {
        "registry_id": "projection_registry_" + "1" * 32,
        "registry_epoch_sha256": epoch,
        "state_version": 1,
        "state_head_sha256": "2" * 64,
        "selector_id": registry_module._selector_id(epoch, policy.policy_id),
        "policy_version": 1,
        "object_sha256": "3" * 64,
        "policy_sha256": projection_policy_sha256(policy),
        "policy": policy,
    }
    with pytest.raises(ValidationError, match="object digest"):
        ResolvedProjectionPolicy(**values)
    values["object_sha256"] = _object_sha256_for(
        values["registry_id"], epoch, policy
    )
    assert ResolvedProjectionPolicy(**values)
    for field, value in (
        ("selector_id", registry_module._selector_id(epoch, "projpol_other")),
        ("policy_version", 2),
        ("policy_sha256", projection_policy_sha256(_cell_origin())),
        ("policy", _fragment(panel=PanelId.B)),
        ("live_authority_replayed", True),
    ):
        with pytest.raises(ValidationError):
            ResolvedProjectionPolicy(**{**values, field: value})


# --- Versions, selector page and privacy -------------------------------------


def test_versions_append_contiguously_within_one_family(
    registry: ProjectionPolicyRegistry,
) -> None:
    first = registry.register_policy(_fragment())
    with pytest.raises(ProjectionPolicyRegistryConflict, match="extend"):
        registry.register_policy(_fragment(version=3))
    with pytest.raises(ProjectionPolicyRegistryConflict, match="already registered"):
        registry.register_policy(_fragment(panel=PanelId.B))
    second = registry.register_policy(_fragment(version=2, panel=PanelId.B))
    assert second.selector_id == first.selector_id
    assert registry.resolve(first.selector_id, 1).policy.panel == PanelId.A
    assert registry.resolve(first.selector_id, 2).policy.panel == PanelId.B
    with pytest.raises(ProjectionPolicyRegistryConflict, match="family or scheme"):
        registry.register_policy(_cell_origin(policy_id="projpol_fragment_span", version=3))
    page = registry.list_selectors()
    assert [(row.policy_version, row.latest_version) for row in page.records] == [
        (1, False),
        (2, True),
    ]


def test_selector_page_is_private_bounded_and_paginated(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipts = [
        registry.register_policy(policy)
        for policy in (_fragment(), _cell_origin(), _chromosome(), _segment())
    ]
    page = registry.list_selectors(limit=3)
    assert len(page.records) == 3
    rest = registry.list_selectors(
        after_selector_id=page.next_after_selector_id,
        after_policy_version=page.next_after_policy_version,
    )
    rows = page.records + rest.records
    assert rest.next_after_selector_id is None
    assert sorted(row.selector_id for row in rows) == sorted(
        item.selector_id for item in receipts
    )
    content = page.model_dump_json() + rest.model_dump_json()
    for private in (
        "projpol_",
        "hepatocyte",
        "asset_atlas_alpha",
        "chr7",
        "chr1",
        "mth_",
        "qty_",
        "1000000",
    ):
        assert private not in content
    assert set(json.loads(rows[0].model_dump_json())) == {
        "schema_version",
        "selector_id",
        "policy_version",
        "latest_version",
        "object_sha256",
        "policy_sha256",
        "family",
        "selection_rule",
        "component_count",
        "measurement_definition_sha256",
        "anchor_definition_sha256",
    }
    for limit in (0, 101, True):
        with pytest.raises(ProjectionPolicyRegistryConflict, match="page bound"):
            registry.list_selectors(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(ProjectionPolicyRegistryConflict, match="incomplete"):
        registry.list_selectors(after_selector_id=receipts[0].selector_id)
    with pytest.raises(ProjectionPolicyRegistryConflict, match="cursor is invalid"):
        registry.list_selectors(after_selector_id="d03_series_x", after_policy_version=1)


# --- Conformance with real E07/E08/E09 artifacts -----------------------------


def test_policies_bind_real_e07_e08_e09_vocabularies(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    from tests.test_cell_origin_explorer import _bundle
    from tests.test_cell_origin_explorer import _method as e08_method
    from tests.test_cna_explorer import _snapshot
    from tests.test_fragment_explorer import _method as e07_method
    from tests.test_fragment_explorer import _source

    quantity = FragmentQuantity.ALIGNED_REFERENCE_SPAN
    source = _source("alpha", quantity=quantity)
    assert source.chart is not None
    definition = e07_method("alpha", quantity)
    assert source.record.method.quantity_id == definition.quantity_id
    bins = tuple(
        _bin(index, row.lower_inclusive, row.upper_exclusive)
        for index, row in enumerate(source.chart.rows)
    )
    assert bins[-1].upper_exclusive is None
    fragment = _fragment(
        measurement=_binding(definition),
        measurement_anchor=_anchor(definition),
        fragment_quantity=quantity,
        components=tuple(
            _fragment_component(statistic, item)
            for item in bins
            for statistic in FragmentStatistic
        ),
    )

    bundle = _bundle()
    estimates = bundle.result.deconvolution.estimates
    definition = e08_method()
    cell_origin = _cell_origin(
        measurement=_binding(definition),
        measurement_anchor=_anchor(definition),
        atlas_id=bundle.result.deconvolution.atlas_id,
        atlas_sha256=bundle.result.deconvolution.atlas_sha256,
        components=tuple(
            CellOriginProjectionComponent(
                statistic=CellOriginStatistic.ESTIMATED_FRACTION,
                statistic_unit=StatisticUnit.FRACTION,
                contributor_id=contributor,
            )
            for contributor in sorted(item.cell_type_id for item in estimates)
        ),
    )

    _, _, snapshot = _snapshot(tmp_path / "cna")
    assert snapshot.layers is not None
    dosage_grid, segment_grid = snapshot.layers.coordinate_grids
    chromosome = _chromosome(
        coordinate_grid=dosage_grid,
        coordinate_grid_sha256=cna_coordinate_grid_sha256(dosage_grid),
        components=tuple(
            CnaChromosomeProjectionComponent(
                statistic=statistic,
                statistic_unit=registry_module._STATISTIC_UNIT[statistic],
                chromosome=layer.chromosome,
            )
            for layer in snapshot.layers.dosage_chromosomes
            for statistic in CnaChromosomeStatistic
        ),
    )
    segment = _segment(
        coordinate_grid=segment_grid,
        coordinate_grid_sha256=cna_coordinate_grid_sha256(segment_grid),
        components=tuple(
            CnaSegmentProjectionComponent(
                statistic=statistic,
                statistic_unit=registry_module._STATISTIC_UNIT[statistic],
                segment=CnaSegmentCoordinate(
                    segment_index=layer.segment_index,
                    contig=layer.contig,
                    start=layer.start,
                    end=layer.end,
                ),
            )
            for layer in snapshot.layers.segments
            for statistic in CnaSegmentStatistic
        ),
    )
    for policy in (fragment, cell_origin, chromosome, segment):
        receipt = registry.register_policy(policy)
        assert registry.resolve(receipt.selector_id, 1).policy == policy
    # The E07 alias table is the vocabulary source, not a restatement.
    from evidence_inspector import fragment_explorer

    assert dict(registry_module._E07_QUANTITY_ID) == fragment_explorer._QUANTITY_ID


# --- D03-parity storage ------------------------------------------------------


def test_cumulative_object_byte_bound_rejects_before_publication(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = registry.register_policy(_fragment())
    stored = (registry.root / "objects" / f"{first.object_sha256}.json").stat()
    monkeypatch.setattr(registry_module, "MAX_TOTAL_OBJECT_BYTES", stored.st_size + 1)
    with pytest.raises(ProjectionPolicyRegistryConflict, match="byte bound"):
        registry.register_policy(_cell_origin())
    monkeypatch.undo()
    assert registry.list_selectors().state_version == 1
    assert len(os.listdir(registry.root / "objects")) == 1


def test_object_tamper_and_extra_entries_fail_closed(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    object_path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    original = object_path.read_bytes()
    object_path.write_bytes(original.replace(b'"panel":"a"', b'"panel":"b"'))
    object_path.chmod(0o600)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="digest"):
        registry.resolve(receipt.selector_id, 1)
    object_path.write_bytes(original)
    object_path.chmod(0o600)
    (registry.root / "objects" / "notes.txt").write_text("private")
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="invalid object"):
        registry.list_selectors()


def test_open_instance_detects_rollback_without_the_process_fence(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = registry.root / "registry-journal.jsonl"
    registry.register_policy(_fragment())
    before = journal.read_bytes()
    registry.register_policy(_segment())
    journal.write_bytes(before)
    monkeypatch.setattr(registry_module, "_REGISTRY_PROCESS_HEADS", {})
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="rollback"):
        registry.list_selectors()


def test_committed_object_deletion_and_journal_rollback_fail_closed(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    object_path = registry.root / "objects" / f"{receipt.object_sha256}.json"
    content = object_path.read_bytes()
    object_path.unlink()
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()
    registry.close()
    object_path.write_bytes(content)
    object_path.chmod(0o600)
    (registry.root / "registry-journal.jsonl").write_bytes(b"")
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="rollback"):
        _reopen(registry, receipt)


def _object_bytes_from_scratch(
    registry: ProjectionPolicyRegistry, tmp_path: Path, policy: Any
) -> tuple[str, bytes]:
    """Build exact object bytes for ``registry`` without committing them."""

    value = registry_module.RegisteredProjectionPolicyObject(
        registry_id=registry._metadata.registry_id,
        registry_epoch_sha256=registry._metadata.registry_epoch_sha256,
        policy=policy,
    )
    content = registry_module.registered_projection_object_bytes(value)
    import hashlib

    return hashlib.sha256(content).hexdigest(), content


def _plant(registry: ProjectionPolicyRegistry, digest: str, content: bytes) -> Path:
    path = registry.root / "objects" / f"{digest}.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_interrupted_publication_orphan_is_removed_on_next_registration(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    orphan = _plant(registry, *_object_bytes_from_scratch(registry, tmp_path, _segment()))
    assert registry.list_selectors().state_version == 0
    receipt = registry.register_policy(_fragment())
    assert receipt.state_version == 1
    assert not orphan.exists()
    assert os.listdir(registry.root / "objects") == [f"{receipt.object_sha256}.json"]


def test_exact_uncommitted_object_is_adopted_and_two_orphans_fail_closed(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    digest, content = _object_bytes_from_scratch(registry, tmp_path, _fragment())
    _plant(registry, digest, content)
    receipt = registry.register_policy(_fragment())
    assert receipt.object_sha256 == digest
    _plant(registry, *_object_bytes_from_scratch(registry, tmp_path, _segment()))
    _plant(registry, "f" * 64, b"{}")
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="inconsistent"):
        registry.list_selectors()


def test_object_bound_to_another_registry_fails_closed(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    other = ProjectionPolicyRegistry(tmp_path / "other")
    try:
        receipt = other.register_policy(_fragment())
        content = (other.root / "objects" / f"{receipt.object_sha256}.json").read_bytes()
    finally:
        other.close()
    _plant(registry, receipt.object_sha256, content)
    entry = registry_module._build_journal_entry(
        sequence=1,
        previous_entry_sha256=registry._genesis_head_sha256,
        object_sha256=receipt.object_sha256,
        object_bytes=len(content),
    )
    journal = registry.root / "registry-journal.jsonl"
    journal.write_bytes(registry_module.canonical_contract_bytes(entry) + b"\n")
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="history"):
        registry.list_selectors()


def test_exact_crash_temporary_is_recovered_on_reopen(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    registry.close()
    temporary = registry.root / "objects" / (".tmp-" + "a" * 32)
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    reopened = _reopen(registry, receipt)
    try:
        assert not temporary.exists()
        assert reopened.resolve(receipt.selector_id, 1).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        reopened.close()


def test_reopen_requires_exact_retained_identity_and_head(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    registry.close()
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="required"):
        ProjectionPolicyRegistry(registry.root)
    for override in (
        {"expected_state_head_sha256": "0" * 64},
        {"expected_registry_epoch_sha256": "0" * 64},
        {"expected_registry_id": "projection_registry_" + "0" * 32},
        {"expected_registry_id": "d03_registry_" + "0" * 32},
        {"expected_state_head_sha256": None},
    ):
        with pytest.raises(ProjectionPolicyRegistryUnsafe, match="expected"):
            _reopen(registry, receipt, **override)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="inherit"):
        ProjectionPolicyRegistry(
            registry.root.parent / "fresh",
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256=receipt.state_head_sha256,
        )


def test_missing_metadata_never_bootstraps_existing_storage(
    registry: ProjectionPolicyRegistry,
) -> None:
    receipt = registry.register_policy(_fragment())
    registry.close()
    (registry.root / "registry-metadata.json").unlink()
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="metadata is missing"):
        _reopen(registry, receipt)


def test_backup_restore_preserves_identity(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    registry.register_policy(_fragment())
    receipt = registry.register_policy(_fragment(version=2, panel=PanelId.B))
    backup = registry.backup_bytes()
    assert projection_policy_backup_from_bytes(backup).state_head_sha256 == (
        receipt.state_head_sha256
    )
    values = {
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }
    restored = ProjectionPolicyRegistry.restore(tmp_path / "restored", backup, **values)
    try:
        resolved = restored.resolve(receipt.selector_id, 2)
        assert resolved.policy_sha256 == receipt.policy_sha256
        assert resolved.state_head_sha256 == receipt.state_head_sha256
    finally:
        restored.close()
    with pytest.raises(ProjectionPolicyRegistryConflict, match="already exists"):
        ProjectionPolicyRegistry.restore(tmp_path / "restored", backup, **values)


def test_old_backup_cannot_authenticate_as_current_state(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    registry.register_policy(_fragment())
    old_backup = registry.backup_bytes()
    current = registry.register_policy(_segment())
    target = tmp_path / "rollback-restore"
    with pytest.raises(ProjectionPolicyRegistryConflict, match="expected head"):
        ProjectionPolicyRegistry.restore(
            target,
            old_backup,
            expected_registry_id=current.registry_id,
            expected_registry_epoch_sha256=current.registry_epoch_sha256,
            expected_state_head_sha256=current.state_head_sha256,
        )
    assert not target.exists()


def test_tampered_backup_rejects_before_creating_restore_target(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    receipt = registry.register_policy(_fragment())
    backup = registry.backup_bytes()
    tampered = backup.replace(b'\\"panel\\":\\"a\\"', b'\\"panel\\":\\"b\\"', 1)
    assert tampered != backup
    target = tmp_path / "tampered-restore"
    with pytest.raises(ProjectionPolicyRegistryConflict):
        ProjectionPolicyRegistry.restore(
            target,
            tampered,
            expected_registry_id=receipt.registry_id,
            expected_registry_epoch_sha256=receipt.registry_epoch_sha256,
            expected_state_head_sha256=receipt.state_head_sha256,
        )
    assert not target.exists()


def test_peer_rejects_rollback_to_its_own_preappend_head(
    registry: ProjectionPolicyRegistry,
) -> None:
    identity = registry.list_selectors()
    peer = ProjectionPolicyRegistry(
        registry.root,
        expected_registry_id=identity.registry_id,
        expected_registry_epoch_sha256=identity.registry_epoch_sha256,
        expected_state_head_sha256=identity.state_head_sha256,
    )
    journal_path = registry.root / "registry-journal.jsonl"
    empty_journal = journal_path.read_bytes()
    try:
        registry.register_policy(_fragment())
        journal_path.write_bytes(empty_journal)
        with pytest.raises(ProjectionPolicyRegistryUnsafe, match="rollback"):
            peer.list_selectors()
    finally:
        peer.close()


def test_head_change_before_return_fails_closed(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    original = registry_module.projection_policy_sha256
    journal = registry.root / "registry-journal.jsonl"

    def append_then_digest(policy):
        # Simulates a committed append landing between load and return.
        entry = registry_module._build_journal_entry(
            sequence=2,
            previous_entry_sha256=receipt.state_head_sha256,
            object_sha256="f" * 64,
            object_bytes=10,
        )
        with journal.open("ab") as handle:
            handle.write(registry_module.canonical_contract_bytes(entry) + b"\n")
        return original(policy)

    monkeypatch.setattr(registry_module, "projection_policy_sha256", append_then_digest)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="head changed"):
        registry.resolve(receipt.selector_id, 1)


def test_instance_and_class_callable_shadows_are_rejected(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    for name in ("resolve", "register_policy", "list_selectors", "backup_bytes"):
        object.__getattribute__(registry, "__dict__")[name] = lambda *a, **k: None
        with pytest.raises(ProjectionPolicyRegistryUnsafe, match="callable"):
            getattr(registry, name)
        del object.__getattribute__(registry, "__dict__")[name]
    monkeypatch.setattr(
        ProjectionPolicyRegistry, "_require_final_head", lambda self, head: None
    )
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="callable"):
        registry.resolve(receipt.selector_id, 1)


@pytest.mark.parametrize(
    "name", ("_PP_SELECTOR_ID", "_PP_POLICY_SHA256", "_PP_RESOLVED", "_PP_LOAD_STATE")
)
def test_sealed_alias_replacement_is_rejected(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    receipt = registry.register_policy(_fragment())
    monkeypatch.setattr(registry_module, name, lambda *a, **k: None)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="authority callable"):
        registry.resolve(receipt.selector_id, 1)


@pytest.mark.parametrize("name", ("_metadata", "_trusted_head_sha256", "_head_key"))
def test_instance_authority_state_replacement_is_rejected(
    registry: ProjectionPolicyRegistry, name: str
) -> None:
    receipt = registry.register_policy(_fragment())
    instance = object.__getattribute__(registry, "__dict__")
    original = instance[name]
    if name == "_metadata":
        instance[name] = original.model_copy(update={"registry_epoch_sha256": "0" * 64})
    else:
        instance[name] = {
            "_trusted_head_sha256": "0" * 64,
            "_head_key": (0, 0, "x", "y"),
        }[name]
    try:
        with pytest.raises(ProjectionPolicyRegistryUnsafe, match="authority state"):
            registry.resolve(receipt.selector_id, 1)
    finally:
        instance[name] = original


@pytest.mark.parametrize("name", (".registry.lock", "registry-metadata.json"))
def test_bound_control_file_substitution_fails_closed(
    registry: ProjectionPolicyRegistry, name: str
) -> None:
    registry.register_policy(_fragment())
    path = registry.root / name
    bound = registry.root / f"{name}.bound"
    os.replace(path, bound)
    path.write_bytes(bound.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(ProjectionPolicyRegistryUnsafe, match="storage"):
            registry.list_selectors()
    finally:
        path.unlink()
        os.replace(bound, path)


def test_torn_journal_append_is_truncated_and_registration_retries(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = {"journal": 0}

    def torn_write(descriptor: int, content) -> int:
        data = bytes(content)
        if data.endswith(b"\n") and b"e12-projection-journal-entry" in data:
            calls["journal"] += 1
            original_write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")
        return original_write(descriptor, content)

    monkeypatch.setattr(os, "write", torn_write)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="append failed"):
        registry.register_policy(_fragment())
    monkeypatch.undo()
    assert calls["journal"] == 1
    assert (registry.root / "registry-journal.jsonl").read_bytes() == b""
    assert registry.list_selectors().state_version == 0
    receipt = registry.register_policy(_fragment())
    assert receipt.state_version == 1
    assert registry.resolve(receipt.selector_id, 1).object_sha256 == receipt.object_sha256


def _restore_values(receipt) -> dict[str, str]:
    return {
        "expected_registry_id": receipt.registry_id,
        "expected_registry_epoch_sha256": receipt.registry_epoch_sha256,
        "expected_state_head_sha256": receipt.state_head_sha256,
    }


def test_failed_restore_removes_its_partial_target_and_can_retry(
    registry: ProjectionPolicyRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    backup = registry.backup_bytes()
    original_link = os.link

    def failing_link(source, destination, *args, **kwargs):
        if destination == "registry-journal.jsonl":
            raise OSError("disk full")
        return original_link(source, destination, *args, **kwargs)

    target = tmp_path / "partial-restore"
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="restore failed"):
        ProjectionPolicyRegistry.restore(target, backup, **_restore_values(receipt))
    monkeypatch.undo()
    assert not target.exists()
    restored = ProjectionPolicyRegistry.restore(target, backup, **_restore_values(receipt))
    try:
        assert restored.resolve(receipt.selector_id, 1).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()


def test_failed_restore_root_open_removes_the_empty_target(
    registry: ProjectionPolicyRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    backup = registry.backup_bytes()
    target = tmp_path / "root-open-restore"
    original_open = os.open

    def failing_open(path, *args, **kwargs):
        # The restore root is staged under a hidden sibling name first.
        if (
            isinstance(path, str)
            and path.startswith(f".{target.name}.staging-")
            and kwargs.get("dir_fd") is not None
        ):
            raise OSError("descriptor exhausted")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="restore failed"):
        ProjectionPolicyRegistry.restore(target, backup, **_restore_values(receipt))
    monkeypatch.undo()
    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.staging-*"))


def test_failed_restore_reopen_removes_the_target_and_can_retry(
    registry: ProjectionPolicyRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    backup = registry.backup_bytes()
    target = tmp_path / "reopen-restore"
    original_flock = fcntl.flock
    restored_lock = target / ".registry.lock"

    def failing_flock(descriptor, operation):
        # Only the restored registry's own lock fails, during its final reopen.
        if restored_lock.exists():
            lock = restored_lock.stat()
            bound = os.fstat(descriptor)
            if (bound.st_dev, bound.st_ino) == (lock.st_dev, lock.st_ino):
                raise OSError("lock unavailable")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", failing_flock)
    with pytest.raises(ProjectionPolicyRegistryUnsafe, match="restore failed"):
        ProjectionPolicyRegistry.restore(target, backup, **_restore_values(receipt))
    monkeypatch.undo()
    assert not target.exists()
    restored = ProjectionPolicyRegistry.restore(target, backup, **_restore_values(receipt))
    try:
        assert restored.resolve(receipt.selector_id, 1).object_sha256 == (
            receipt.object_sha256
        )
    finally:
        restored.close()


def test_interpreter_warning_registry_does_not_disable_the_registry(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = registry.register_policy(_fragment())
    monkeypatch.setitem(registry_module.__dict__, "__warningregistry__", {})
    assert registry.resolve(receipt.selector_id, 1).object_sha256 == receipt.object_sha256


# --- shared storage behaviour (tests/registry_storage_checks.py) ----------------


def test_storage_torn_tail_needs_explicit_operator_recovery(
    registry: ProjectionPolicyRegistry,
) -> None:
    storage_checks.check_torn_tail_recovery(
        registry,
        lambda: registry.register_policy(_fragment()),
        lambda values: _reopen(registry, values),
        ProjectionPolicyRegistryUnsafe,
    )


def test_storage_interrupted_append_truncates_on_any_exception(
    registry: ProjectionPolicyRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_append_interrupt_truncates(
        registry,
        registry_module,
        lambda: registry.register_policy(_fragment()),
        monkeypatch,
    )


def test_storage_lock_descriptor_is_read_under_the_process_lock(
    registry: ProjectionPolicyRegistry, tmp_path: Path
) -> None:
    storage_checks.check_lock_reads_descriptor_under_process_lock(
        registry, ProjectionPolicyRegistryUnsafe, tmp_path
    )


def test_storage_owned_temporaries_are_swept_and_directories_fail_closed(
    registry: ProjectionPolicyRegistry,
) -> None:
    registry.register_policy(_fragment())
    storage_checks.check_owned_temporaries(
        registry,
        lambda values: _reopen(registry, values),
        ProjectionPolicyRegistryUnsafe,
    )


def test_storage_interrupted_creation_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_checks.check_interrupted_creation(
        ProjectionPolicyRegistry,
        lambda root, values: ProjectionPolicyRegistry(
            root, **storage_checks.expected(values)
        ),
        tmp_path / "created",
        registry_module,
        "_commit_staged_root",
        "_discard_staged_root",
        monkeypatch,
    )


def test_storage_interrupted_restore_is_staged(
    registry: ProjectionPolicyRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.register_policy(_fragment())
    storage_checks.check_interrupted_restore(
        registry,
        lambda target, backup, values: ProjectionPolicyRegistry.restore(
            target, backup, **storage_checks.expected(values)
        ),
        registry_module,
        tmp_path,
        monkeypatch,
    )


def test_storage_creation_under_a_symlinked_parent(tmp_path: Path) -> None:
    storage_checks.check_creation_under_symlinked_parent(
        lambda root: ProjectionPolicyRegistry(root),
        lambda root, values: ProjectionPolicyRegistry(
            root, **storage_checks.expected(values)
        ),
        tmp_path,
    )
