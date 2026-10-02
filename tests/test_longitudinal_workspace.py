"""D08 longitudinal workspace: live authority, gates, projection, privacy.

Integration tests build one coherent world over every merged E12 store
(``tests/longitudinal_workspace_world.py``).  Property tests run the pure
derivation on protected rows taken from that world and then edited, so every
suppression, segment, denominator and filter rule is exercised directly.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import evidence_inspector.longitudinal_workspace as workspace_module
import evidence_inspector.reader_authorization_registry as reader_module
from evidence_inspector.cohort_import import CohortRecordAvailability
from evidence_inspector.cohort_manifest import MemberLineageRole
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.longitudinal_compatibility import (
    LongitudinalNextAction,
    LongitudinalOutcome,
    LongitudinalReason,
)
from evidence_inspector.longitudinal_workspace import (
    LIMITATION_STATEMENT,
    MAX_WORKSPACE_MEMBERS,
    ComparisonRegistryState,
    ComparisonState,
    CovariateContextState,
    DecisionState,
    FamilyPrerequisite,
    HistoryState,
    LongitudinalErrorCode,
    LongitudinalSourceRow,
    LongitudinalWorkspace,
    LongitudinalWorkspaceBoundaryError,
    LongitudinalWorkspaceFilters,
    LongitudinalWorkspaceProjection,
    LongitudinalWorkspaceRequest,
    ProtectedHistory,
    ProtectedLongitudinalRow,
    RowCompatibilityState,
    SuppressionReason,
    ValueState,
    VersionDiffKind,
    VersionDiffReason,
    WorkspaceAuthorityInputs,
    derive_segments,
    derive_version_diff,
    derive_workspace,
    longitudinal_projection_bytes,
    project_longitudinal_workspace,
    timepoint_positions,
)
from evidence_inspector.projection_policy_registry import (
    CellOriginProjectionPolicyV1,
    ProjectionFamily,
)
from evidence_inspector.reader_authorization_registry import (
    MeasurementScope,
    ReaderGrantBinding,
    ReaderRevocationReason,
)
from evidence_inspector.repeatability_comparison import (
    ComparisonAvailability,
    RepeatabilityClassification,
    RepeatabilityReason,
)
from evidence_inspector.result_view_source_registry import (
    ResultViewSourceRegistry,
    ResultViewSourceRegistryStale,
    ResultViewSourceRegistryUnsafe,
)
from tests.longitudinal_workspace_world import (
    DAY,
    World,
    make_world,
    protected_tokens,
)
from tests.test_longitudinal_compatibility import _record as _extra_record

# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory):
    """One read-only world and its workspace, shared by non-mutating tests."""

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reader_module, "_PROCESS_PROFILE", {})
        world = make_world(tmp_path_factory.mktemp("d08-base"))
        try:
            yield world, world.build()
        finally:
            world.close()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    value = make_world(tmp_path)
    try:
        yield value
    finally:
        value.close()


def _boundary(operation) -> LongitudinalWorkspaceBoundaryError:
    with pytest.raises(LongitudinalWorkspaceBoundaryError) as caught:
        operation()
    error = caught.value
    # Closed and safe: no nested exception, constant text.
    assert error.__cause__ is None and error.__context__ is None
    assert str(error) == f"{error.code.value}:{error.remediation.value}"
    return error


def _rows_by_digit(
    workspace: LongitudinalWorkspace,
) -> dict[str, LongitudinalSourceRow]:
    """Map the world's member digits to public rows (A=1, R=3, B=2, C=4)."""

    result = {}
    for protected, row in zip(workspace.protected_rows, workspace.rows, strict=True):
        result[protected.member.linkage_id[-1]] = row
    return result


def _inputs(workspace: LongitudinalWorkspace, **updates) -> WorkspaceAuthorityInputs:
    values = {
        "request": workspace.request,
        "reader_authorization": workspace.reader_authorization,
        "dependency_heads": workspace.dependency_heads,
        "authority": workspace.authority,
        "time_axis": workspace.time_axis,
        "population": workspace.population,
        "covariate_context": workspace.covariate_context,
        "version_diff": workspace.version_diff,
        "anchor_record_sha256": workspace.anchor_record_sha256,
        "limitations": workspace.limitations,
    }
    values.update(updates)
    return WorkspaceAuthorityInputs(**values)


def _derive(world: World, workspace: LongitudinalWorkspace, rows, **updates):
    return derive_workspace(
        _inputs(workspace, **updates),
        tuple(rows),
        positions=timepoint_positions(world.manifest),
    )


def _edit(row: ProtectedLongitudinalRow, **updates) -> ProtectedLongitudinalRow:
    return ProtectedLongitudinalRow.model_validate(
        {**row.model_dump(mode="python"), **updates}
    )


# --- integration: the full live journey -------------------------------------------


def test_live_workspace_from_every_registry(base) -> None:
    world, workspace = base
    rows = _rows_by_digit(workspace)
    assert type(workspace) is LongitudinalWorkspace
    assert [row.row_ordinal for row in workspace.rows] == [1, 2, 3, 4]
    # Item 1: the draw and its technical replicate share one public timepoint;
    # only the draw is a denominator contributor; both remain source rows.
    assert rows["1"].timepoint_ordinal == rows["3"].timepoint_ordinal == 1
    assert rows["1"].offset_seconds == rows["3"].offset_seconds == 0
    assert rows["1"].lineage_role is MemberLineageRole.BIOLOGICAL_DRAW
    assert rows["3"].lineage_role is MemberLineageRole.TECHNICAL_REPLICATE
    assert rows["1"].denominator_contributor and not rows["3"].denominator_contributor
    # Item 2: distinct collection events are ordered timepoints with exact
    # signed-second offsets (unequal spacing is kept).
    assert (rows["2"].timepoint_ordinal, rows["2"].offset_seconds) == (2, 86_400)
    assert (rows["4"].timepoint_ordinal, rows["4"].offset_seconds) == (3, 172_800)
    # D06 states and E06/E07 sources.
    assert rows["1"].record_availability is CohortRecordAvailability.AVAILABLE
    assert rows["4"].record_availability is CohortRecordAvailability.MISSING
    assert rows["4"].values == () and rows["4"].catalog_result_sha256 is None
    assert rows["1"].value_state is ValueState.PROJECTED and rows["1"].values
    assert rows["2"].value_state is ValueState.PROJECTED and rows["2"].values
    assert rows["2"].denominator_ledger_label == "operator-entered, unverified"
    # D03 and D07: the anchor is the reference; B is equivalent and available.
    assert rows["1"].compatibility_state is RowCompatibilityState.ANCHOR
    assert rows["1"].comparison_state is ComparisonState.ANCHOR_REFERENCE
    assert rows["2"].compatibility_state is RowCompatibilityState.EQUIVALENT
    assert rows["2"].next_action is LongitudinalNextAction.USE_DIRECT_COMPARISON
    assert rows["2"].comparison is not None
    assert rows["2"].comparison.member_value - rows["2"].comparison.anchor_value == (
        rows["2"].comparison.delta
    )
    assert rows["4"].comparison is None
    # D04: both imported results are active leaves.
    assert rows["1"].history_state is rows["2"].history_state is HistoryState.ACTIVE
    # One explicitly authorized anchor-relative segment, day 1 -> day 2.
    assert len(workspace.segments) == 1
    segment = workspace.segments[0]
    assert (segment.from_row_ordinal, segment.to_row_ordinal) == (
        rows["1"].row_ordinal,
        rows["2"].row_ordinal,
    )
    assert segment.to_comparison_sha256 == rows["2"].comparison.comparison_sha256
    # D09 aggregate population: A is policy-excluded, R collapsed, C unavailable.
    population = workspace.population
    assert (
        population.declared_members,
        population.included_members,
        population.excluded_members,
        population.unavailable_members,
    ) == (4, 1, 2, 1)
    assert population.declared_denominator_units == 3
    # D09 is not a gate: the policy-excluded anchor still anchors a comparison.
    # D10 context is aggregate and labelled operator-entered.
    covariate = workspace.covariate_context
    assert covariate.state is CovariateContextState.AVAILABLE
    assert covariate.token_provenance == "operator-entered, unverified"
    assert covariate.values_changed is False
    assert workspace.version_diff.kind is VersionDiffKind.INITIAL_VERSION
    assert workspace.limitation_statement == LIMITATION_STATEMENT
    assert workspace.product_release_authorized is False
    assert workspace.release_export_authorized is False
    assert workspace.diagnostic_interpretation_allowed is False
    assert workspace.authority.anchor_row_ordinal == rows["1"].row_ordinal
    assert workspace.authority.d03_series_state is DecisionState.DECIDED
    assert workspace.authority.d07_envelope_sha256 == (
        workspace.protected_rows[2].comparison.repeatability_envelope_sha256
    )


def test_rebuild_is_byte_identical(base) -> None:
    world, workspace = base
    again = world.build()
    assert (
        again.model_copy(
            update={"reader_authorization": workspace.reader_authorization}
        )
        == workspace
    )
    assert longitudinal_projection_bytes(
        project_longitudinal_workspace(again)
    ) == longitudinal_projection_bytes(project_longitudinal_workspace(workspace))


def _variants(token: str) -> set[str]:
    raw = token.encode()
    return {
        token,
        token.upper(),
        raw.hex(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }


def test_public_projection_carries_no_protected_identifier(base) -> None:
    world, workspace = base
    projection = project_longitudinal_workspace(workspace)
    assert type(projection) is LongitudinalWorkspaceProjection
    public = longitudinal_projection_bytes(projection).decode()
    # Structural: no protected-row field exists in the public schema.
    assert "protected_rows" not in LongitudinalWorkspaceProjection.model_fields
    assert "reader_authorization" not in LongitudinalWorkspaceProjection.model_fields
    assert "member" not in LongitudinalSourceRow.model_fields
    tokens = protected_tokens(world)
    assert len(tokens) > 30
    for token in tokens:
        for variant in _variants(token):
            assert variant not in public, token
    for protected in workspace.protected_rows:
        assert protected.member_sha256 not in public
    # The reader grant and session credential never reach public bytes.
    assert world.credential.grant_sha256 not in public
    assert world.credential.state_head_sha256 not in public
    assert workspace.reader_authorization.grant_sha256 not in public
    assert workspace.dependency_heads.reader_authorization.head not in public
    assert workspace.dependency_heads.reader_authorization.id not in public


def test_public_time_axis_bytes_carry_offsets_not_timestamps(base) -> None:
    world, workspace = base
    public = json.loads(
        longitudinal_projection_bytes(project_longitudinal_workspace(workspace))
    )
    axis = public["time_axis"]
    assert axis == {
        "kind": "collection_time",
        "unit": "seconds",
        "definition_sha256": world.manifest.time_axis.definition_sha256,
        "coordinate_semantics": "absolute_collection_time",
        "offset_origin": "first_biological_coordinate",
    }
    assert [row["offset_seconds"] for row in public["rows"]] == [0, 0, 86_400, 172_800]
    text = json.dumps(public)
    for day in DAY.values():
        assert day.isoformat() not in text
        assert str(int(day.timestamp())) not in text
        assert day.date().isoformat() not in text
    for member in world.manifest.members:
        assert member.biological_timepoint_id not in text


def test_public_language_is_descriptive_only(base) -> None:
    _, workspace = base
    public = longitudinal_projection_bytes(
        project_longitudinal_workspace(workspace)
    ).decode()
    assert LIMITATION_STATEMENT == (
        "Comparisons are descriptive technical differences with no causal or "
        "clinical interpretation."
    )
    assert LIMITATION_STATEMENT in public
    lowered = public.lower()
    for word in (
        "increase",
        "decrease",
        "improv",
        "worse",
        "progress",
        "respon",
        "remission",
        "relapse",
        "upward",
        "downward",
        "rising",
        "falling",
        "trend",
        "significant",
        "diagnos",
    ):
        assert word not in lowered.replace(
            "diagnostic_interpretation_allowed", ""
        ), word


# --- integration: authority boundaries ----------------------------------------------


def test_reader_must_hold_a_current_scoped_grant(world: World) -> None:
    forged = ReaderGrantBinding(
        grant_sha256="0" * 64, state_head_sha256=world.credential.state_head_sha256
    )
    for credential in (forged, {"grant_sha256": "0" * 64}, "token", 0):
        error = _boundary(
            lambda credential=credential: world.build(credential=credential)
        )
        assert error.code is LongitudinalErrorCode.PERMISSION_DENIED
    wrong_scope = world.request.model_copy(
        update={
            "measurement": world.request.measurement.model_copy(
                update={"quantity_id": "qty_fragment_other"}
            )
        }
    )
    error = _boundary(lambda: world.build(request=wrong_scope))
    assert error.code is LongitudinalErrorCode.PERMISSION_DENIED
    world.reader.revoke_grant(
        world.grant.payload.grant_selector,
        reason=ReaderRevocationReason.OPERATOR_REQUEST,
    )
    error = _boundary(world.build)
    assert error.code is LongitudinalErrorCode.PERMISSION_DENIED


def test_denial_precedes_every_protected_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    original = workspace_module._selector_record
    monkeypatch.setattr(
        workspace_module,
        "_selector_record",
        lambda *a, **k: called.append(1) or original(*a, **k),
    )
    forged = ReaderGrantBinding(
        grant_sha256="0" * 64, state_head_sha256=world.credential.state_head_sha256
    )
    _boundary(lambda: world.build(credential=forged))
    assert called == []


def test_caller_objects_never_supply_authority(world: World) -> None:
    request = world.request
    candidates = [
        request.model_dump(mode="python"),
        request.model_construct(**request.model_dump(mode="python"), manifest="x"),
        type("Sub", (LongitudinalWorkspaceRequest,), {})(
            **request.model_dump(mode="python")
        ),
        0,
    ]
    for candidate in candidates:
        error = _boundary(lambda candidate=candidate: world.build(request=candidate))
        assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    private = request.model_copy()
    object.__setattr__(private, "__pydantic_extra__", {"status": "available"})
    error = _boundary(lambda: world.build(request=private))
    assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    oversized = request.model_copy(
        update={
            "filters": LongitudinalWorkspaceFilters.model_construct(
                timepoint_ordinals=tuple(range(1, 5_000)),
                lineage_roles=(),
                record_availability=(),
                compatibility_states=(),
            )
        }
    )
    error = _boundary(lambda: world.build(request=oversized))
    assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    # The builder signature has no parameter that accepts a manifest, status,
    # source, decision, comparison, policy, anchor or value.
    import inspect

    names = set(
        inspect.signature(workspace_module.build_longitudinal_workspace).parameters
    )
    assert not names & {
        "manifest",
        "status",
        "source",
        "decision",
        "comparison",
        "policy",
        "anchor",
        "values",
        "summary",
        "role",
        "grant",
    }


def test_store_substitution_and_method_shadows_fail_closed(world: World) -> None:
    from evidence_inspector.repeatability_comparison_registry import (
        RepeatabilityComparisonRegistry,
    )

    class Shadow(RepeatabilityComparisonRegistry):
        pass

    error = _boundary(lambda: world.build(cohort_registry=world.records))
    assert error.code is LongitudinalErrorCode.INTEGRITY_FAILURE
    object.__setattr__(world.d07, "resolve", lambda *a, **k: None)
    try:
        error = _boundary(world.build)
        assert error.code is LongitudinalErrorCode.INTEGRITY_FAILURE
    finally:
        del world.d07.__dict__["resolve"]
    original = world.d07.__class__
    world.d07.__class__ = Shadow
    try:
        error = _boundary(world.build)
        assert error.code is LongitudinalErrorCode.INTEGRITY_FAILURE
    finally:
        world.d07.__class__ = original
    # A shadowed store read before authorization denies uniformly.
    object.__setattr__(world.sources, "registry_identity", lambda *a, **k: None)
    try:
        error = _boundary(world.build)
        assert error.code is LongitudinalErrorCode.PERMISSION_DENIED
    finally:
        del world.sources.__dict__["registry_identity"]


def test_anchor_selection_is_explicit_and_fails_closed(world: World) -> None:
    request = world.request
    stale = request.model_copy(update={"anchor_candidate_page_sha256": "1" * 64})
    error = _boundary(lambda: world.build(request=stale))
    assert error.code is LongitudinalErrorCode.AUTHORITY_STALE
    injected = request.model_copy(
        update={"anchor_selector_id": "anchor_candidate_" + "f" * 40}
    )
    error = _boundary(lambda: world.build(request=injected))
    assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    cross_policy = request.model_copy(
        update={"anchor_policy_selector_id": "anchor_policy_" + "e" * 40}
    )
    error = _boundary(lambda: world.build(request=cross_policy))
    assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    with pytest.raises(Exception):
        request.model_copy(update={"anchor_selector_id": None}).model_validate(
            {**request.model_dump(), "anchor_selector_id": None}
        )


@pytest.mark.parametrize("hook", ["_history_map", "_gather"])
@pytest.mark.parametrize(
    "writer",
    [
        "linkage",
        "projection",
        "trust",
        "reader",
        "e04_import",
        "d06_import",
        "d05_register",
    ],
)
def test_authority_change_during_construction_is_a_read_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch, writer: str, hook: str
) -> None:
    from evidence_inspector.projection_policy_registry import ProjectionFamily  # noqa: F401
    from tests.longitudinal_workspace_world import SIGNING_KEY  # type: ignore[attr-defined]

    def write() -> None:
        if writer == "linkage":
            world.linkage.commit_authorized_revision(
                _extra_record("8").authorized_linkage
            )
        elif writer == "projection":
            policy = world.projections.resolve(
                world.request.projection_policy_selector_id, 1
            ).policy
            world.projections.register_policy(policy.model_copy(update={"version": 2}))
        elif writer == "trust":
            world.trust.revoke_key(SIGNING_KEY.key_id)
        elif writer in {"e04_import", "d06_import"}:
            _import_writer(world, writer)
        elif writer == "d05_register":
            _register_writer(world)
        else:
            world.reader.revoke_grant(
                world.grant.payload.grant_selector,
                reason=ReaderRevocationReason.OPERATOR_REQUEST,
            )

    original = getattr(workspace_module, hook)

    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        write()
        return result

    monkeypatch.setattr(workspace_module, hook, racing)
    error = _boundary(world.build)
    assert error.code in {
        LongitudinalErrorCode.READ_CONFLICT,
        LongitudinalErrorCode.PERMISSION_DENIED,
    }
    if writer != "reader":
        assert error.code is LongitudinalErrorCode.READ_CONFLICT


def _new_bundle(world: World, name: str) -> None:
    from tests.test_bundles import _measurement, _provenance
    from traceback_runner.bundles import build_result_bundle

    capability = world.capability
    build_result_bundle(
        world.extra["imports"] / name,
        measurement=_measurement(),
        provenance=_provenance(run_token=f"synthetic.run.{name}"),
        method={
            "method_id": capability.method_ref.method_id,
            "version": capability.method_ref.version,
            "method_definition_sha256": capability.method_definition_sha256,
        },
        signing_key=world.extra["key"],
    )


def _import_writer(world: World, writer: str) -> None:
    """Write E04 rows (raw import) or a D06 binding (import for member C)."""

    from tests.test_result_catalog import ALIASES

    extra = world.extra
    if writer == "e04_import":
        _new_bundle(world, "race-e04")
        world.results.import_bundle(
            root_id="root_primary",
            relative_path="race-e04",
            registry=extra["method_registry"],
            authority_head=extra["head"],
            expected_authority_head_sha256=extra["head_sha256"],
            capability=world.capability,
            aliases=ALIASES.model_copy(
                update={
                    "display_alias": "dsp_99999999",
                    "run_alias": "rnx_99999999",
                    "timepoint_alias": "tpt_99999999",
                }
            ),
        )
        return
    _new_bundle(world, "race-d06")
    member_c = next(
        m for m in world.manifest.members if m.linkage_id.endswith("4" * 32)
    )
    world.records.import_bundle(
        selector_id=extra["selector_id"],
        cohort_version=1,
        provider_namespace=member_c.provider_namespace,
        analysis_record_id=member_c.analysis_record_id,
        root_id="root_primary",
        relative_path="race-d06",
        registry=extra["method_registry"],
        authority_head=extra["head"],
        expected_authority_head_sha256=extra["head_sha256"],
        capability=world.capability,
    )


def _register_writer(world: World) -> None:
    """Register a second cohort in the same D05 registry."""

    from tests.test_cohort_manifest import _authority as _provider_authority
    from tests.test_cohort_manifest import _manifest

    member = world.manifest.members[0]
    world.cohort.register(
        _manifest(
            _provider_authority(world.linkage.active_snapshot()),
            (member,),
            cohort_id="cohort_" + "2" * 32,
            measurement_anchor=world.manifest.measurement_anchor,
        )
    )


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ResultViewSourceRegistryStale, LongitudinalErrorCode.AUTHORITY_STALE),
        (ResultViewSourceRegistryUnsafe, LongitudinalErrorCode.INTEGRITY_FAILURE),
        (OSError, LongitudinalErrorCode.STORAGE_FAILURE),
        (ValueError, LongitudinalErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_source_registry_failure_is_a_typed_boundary_error(
    world: World, monkeypatch: pytest.MonkeyPatch, failure, code
) -> None:
    original = workspace_module._call

    def broken(store, cls, name, *args, **kwargs):
        if (cls, name) == (ResultViewSourceRegistry, "resolve"):
            raise failure("protected detail result_" + "a" * 40)
        return original(store, cls, name, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_call", broken)
    error = _boundary(world.build)
    assert error.code is code
    assert "result_" not in str(error)


def test_stable_key_revocation_is_a_withheld_row_not_an_error(world: World) -> None:
    world.trust.revoke_key(world.extra["key"].key_id)
    workspace = world.build()
    rows = _rows_by_digit(workspace)
    for digit in ("1", "2"):
        row = rows[digit]
        assert row.record_availability is CohortRecordAvailability.WITHHELD
        assert row.withheld_reason is not None
        assert row.values == () and row.comparison is None
        assert row.catalog_result_sha256 is None
    assert workspace.segments == ()
    population = workspace.population
    assert population.declared_members == 4
    # Zero included units: A is policy-excluded, B is now withheld (unavailable).
    assert (population.included_members, population.included_denominator_units) == (
        0,
        0,
    )
    assert population.state.value == "no_included_units"
    assert population.unavailable_members == 2


def test_missing_d07_suppresses_numbers_but_keeps_source_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_module, "_PROCESS_PROFILE", {})
    world = make_world(tmp_path, with_d07=False, with_family=False, with_d10=False)
    try:
        workspace = world.build()
    finally:
        world.close()
    rows = _rows_by_digit(workspace)
    assert rows["2"].comparison is None
    assert rows["2"].suppression_reasons == (SuppressionReason.D07_NOT_REGISTERED,)
    assert rows["2"].compatibility_state is RowCompatibilityState.EQUIVALENT
    assert rows["2"].value_state is ValueState.ARTIFACT_NOT_REGISTERED
    assert workspace.segments == ()
    assert workspace.covariate_context.state is CovariateContextState.NOT_REGISTERED
    assert workspace.population.included_members == 1


def test_e08_e09_policies_name_their_missing_prerequisite(base) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    edited = []
    for row in rows:
        if row.values.state is ValueState.PROJECTED:
            row = _edit(
                row,
                values={
                    "state": ValueState.FAMILY_PREREQUISITE_MISSING,
                    "prerequisite": FamilyPrerequisite.E08_CELL_ORIGIN_ARTIFACT_BINDING,
                },
            )
        edited.append(row)
    derived = _derive(world, workspace, edited)
    for row in derived.rows:
        assert row.values == ()
    assert any(
        row.missing_prerequisite is FamilyPrerequisite.E08_CELL_ORIGIN_ARTIFACT_BINDING
        for row in derived.rows
    )
    # The builder maps every non-fragment family to a named prerequisite.
    assert CellOriginProjectionPolicyV1 is not None
    assert {ProjectionFamily.CELL_ORIGIN, ProjectionFamily.CNA_CHROMOSOME} <= set(
        ProjectionFamily
    )


def test_cohort_over_the_bound_is_rejected_before_member_traversal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = workspace_module._call
    reads: list[str] = []

    def page(store, cls, name, *args, **kwargs):
        value = original(store, cls, name, *args, **kwargs)
        if (cls, name) != (CohortRegistry, "list_selectors"):
            return value
        records = tuple(
            item.model_copy(update={"member_count": count}) for item in value.records
        )
        return value.model_copy(update={"records": records})

    for name in ("_snapshot", "_gather"):
        inner = getattr(workspace_module, name)
        monkeypatch.setattr(
            workspace_module,
            name,
            lambda *a, _inner=inner, _name=name, **k: reads.append(_name)
            or _inner(*a, **k),
        )
    monkeypatch.setattr(workspace_module, "_call", page)
    count = MAX_WORKSPACE_MEMBERS + 1
    error = _boundary(world.build)
    assert error.code is LongitudinalErrorCode.INVALID_REQUEST
    assert error.remediation.value == "reduce_cohort_to_bound"
    assert reads == []
    count = MAX_WORKSPACE_MEMBERS
    reads.clear()
    # Exactly at the bound the selection passes; the live cohort (4 members)
    # then disagrees with the doctored page row only on the count.
    world.build()
    assert reads[:2] == ["_snapshot", "_gather"]


# --- property tests on the pure derivation --------------------------------------------


def test_every_d03_outcome_is_distinct_and_only_two_can_carry_numbers(base) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.decision is not None)
    seen = {}
    for outcome in LongitudinalOutcome:
        decision = rows[index].decision.model_copy(update={"outcome": outcome})
        edited = (
            rows[:index] + [_edit(rows[index], decision=decision)] + rows[index + 1 :]
        )
        row = _derive(world, workspace, edited).rows[index]
        seen[outcome] = row.compatibility_state
        assert row.compatibility_state.value == outcome.value
        if outcome in {
            LongitudinalOutcome.EQUIVALENT,
            LongitudinalOutcome.QUALIFIED_COMPATIBLE,
        }:
            assert row.comparison is not None
        else:
            assert row.comparison is None
            assert SuppressionReason.D03_NOT_COMPARABLE in row.suppression_reasons
            assert row.values  # standalone source values stay visible
    assert len(set(seen.values())) == 6
    delta_off = rows[index].decision.model_copy(update={"delta_allowed": False})
    edited = rows[:index] + [_edit(rows[index], decision=delta_off)] + rows[index + 1 :]
    assert _derive(world, workspace, edited).rows[index].comparison is None


@pytest.mark.parametrize(
    "state",
    [
        ComparisonRegistryState.NOT_REGISTERED,
        ComparisonRegistryState.AMBIGUOUS,
        ComparisonRegistryState.STALE,
    ],
)
def test_every_unavailable_d07_state_strips_all_comparison_fields(base, state) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    edited = rows[:index] + [
        _edit(rows[index], comparison_state=state, comparison=None)
    ]
    edited += rows[index + 1 :]
    derived = _derive(world, workspace, edited)
    row = derived.rows[index]
    assert row.comparison is None and row.comparison_state is ComparisonState.SUPPRESSED
    assert row.values and derived.segments == ()
    assert derived.population == workspace.population


@pytest.mark.parametrize(
    "classification",
    [
        item
        for item in RepeatabilityClassification
        if item
        not in {
            RepeatabilityClassification.EXACT_SAME_VALUE,
            RepeatabilityClassification.NOISY_WITHIN_ENVELOPE,
        }
    ],
)
def test_unavailable_d07_classification_never_publishes_numbers(
    base, classification
) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    comparison = rows[index].comparison.model_copy(
        update={
            "availability": ComparisonAvailability.UNAVAILABLE,
            "classification": classification,
            "reason_codes": (RepeatabilityReason.EVIDENCE_MISSING,),
            **{
                name: None
                for name in (
                    "anchor_value",
                    "member_value",
                    "delta",
                    "anchor_uncertainty_lower",
                    "anchor_uncertainty_upper",
                    "member_uncertainty_lower",
                    "member_uncertainty_upper",
                    "anchor_denominator_count",
                    "member_denominator_count",
                    "maximum_absolute_delta",
                )
            },
        }
    )
    edited = (
        rows[:index]
        + [
            _edit(
                rows[index],
                comparison_state=ComparisonRegistryState.UNAVAILABLE,
                comparison=comparison,
            )
        ]
        + rows[index + 1 :]
    )
    derived = _derive(world, workspace, edited)
    row = derived.rows[index]
    assert row.comparison is None
    assert row.suppression_reasons == (SuppressionReason.D07_UNAVAILABLE,)
    public = longitudinal_projection_bytes(project_longitudinal_workspace(derived))
    assert b"anchor_value" not in public and b"member_value" not in public
    assert derived.segments == ()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "repeatability_envelope_sha256",
            "e" * 64,
            SuppressionReason.D07_BINDING_MISMATCH,
        ),
        ("anchor_policy_sha256", "e" * 64, SuppressionReason.D07_BINDING_MISMATCH),
        ("d03_decision_sha256", "e" * 64, SuppressionReason.D07_BINDING_MISMATCH),
        ("anchor_record_sha256", "e" * 64, SuppressionReason.D07_BINDING_MISMATCH),
    ],
)
def test_d07_comparison_must_bind_the_resolved_anchor_policy_and_envelope(
    base, field, value, reason
) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    comparison = rows[index].comparison.model_copy(update={field: value})
    edited = (
        rows[:index] + [_edit(rows[index], comparison=comparison)] + rows[index + 1 :]
    )
    row = _derive(world, workspace, edited).rows[index]
    assert row.comparison is None and reason in row.suppression_reasons


@pytest.mark.parametrize(
    ("target", "updates", "reason"),
    [
        ("member", {"status": "missing"}, SuppressionReason.RECORD_NOT_AVAILABLE),
        (
            "anchor",
            {"status": "missing"},
            SuppressionReason.ANCHOR_RECORD_NOT_AVAILABLE,
        ),
        ("member", {"history": "superseded"}, SuppressionReason.HISTORY_NOT_ACTIVE),
        (
            "anchor",
            {"history": "superseded"},
            SuppressionReason.ANCHOR_HISTORY_NOT_ACTIVE,
        ),
        ("member", {"history": "not_recorded"}, SuppressionReason.HISTORY_NOT_ACTIVE),
        ("member", {"receipt": "f" * 64}, SuppressionReason.LINKAGE_BINDING_MISMATCH),
        (
            "member",
            {"result": "result_" + "f" * 40},
            SuppressionReason.RESULT_BINDING_MISMATCH,
        ),
    ],
)
def test_each_live_gate_suppresses_comparison_and_segment(
    base, target, updates, reason
) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    member_index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    anchor_index = next(i for i, row in enumerate(rows) if row.is_anchor)
    index = member_index if target == "member" else anchor_index
    row = rows[index]
    if updates.get("status") == "missing":
        status = row.status.model_copy(
            update={"availability": CohortRecordAvailability.MISSING, "binding": None}
        )
        row = _edit(
            row,
            status=status,
            source=None,
            source_state="record_not_available",
            values={"state": ValueState.SOURCE_UNAVAILABLE},
        )
    if "history" in updates:
        state = HistoryState(updates["history"])
        row = _edit(
            row,
            history=ProtectedHistory(
                state=state,
                record_sha256=None if state is HistoryState.NOT_RECORDED else "a" * 64,
            ),
        )
    if "receipt" in updates:
        row = _edit(
            row,
            decision=row.decision.model_copy(
                update={"member_linkage_receipt_sha256": updates["receipt"]}
            ),
        )
    if "result" in updates:
        row = _edit(
            row,
            decision=row.decision.model_copy(
                update={"member_result_id": updates["result"]}
            ),
        )
    rows[index] = row
    derived = _derive(world, workspace, rows)
    public = derived.rows[member_index]
    assert public.comparison is None and reason in public.suppression_reasons
    assert derived.segments == ()
    if "history" in updates and updates["history"] == "superseded":
        assert derived.rows[index].affected_comparison_warning is True
    assert derived.population == workspace.population


def test_incompatible_middle_member_cannot_connect_compatible_endpoints(base) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    public = list(workspace.rows)
    # Synthetic public rows: anchor t1, incompatible t2, compatible t3.
    anchor = public[0]
    eligible = next(row for row in public if row.comparison is not None)
    middle = eligible.model_copy(
        update={
            "row_ordinal": 2,
            "timepoint_ordinal": 2,
            "compatibility_state": RowCompatibilityState.INCOMPATIBLE,
            "comparison_state": ComparisonState.SUPPRESSED,
            "suppression_reasons": (SuppressionReason.D03_NOT_COMPARABLE,),
            "comparison": None,
        }
    )
    late = eligible.model_copy(update={"row_ordinal": 3, "timepoint_ordinal": 3})
    segments = derive_segments((anchor, middle, late))
    assert segments == ()
    # Removing the middle row still never bridges t1 -> t3: segments are only
    # between adjacent timepoints.
    assert derive_segments((anchor, late)) == ()
    # A compatible middle row yields two explicit anchor-relative segments.
    good_middle = eligible.model_copy(update={"row_ordinal": 2, "timepoint_ordinal": 2})
    chained = derive_segments((anchor, good_middle, late))
    assert [(s.from_row_ordinal, s.to_row_ordinal) for s in chained] == [(1, 2), (2, 3)]
    assert all(segment.anchor_relative for segment in chained)
    # Technical replicates are never endpoints; a second draw breaks the chain.
    replicate = eligible.model_copy(
        update={"row_ordinal": 2, "lineage_role": MemberLineageRole.TECHNICAL_REPLICATE}
    )
    assert derive_segments((anchor, replicate)) == derive_segments((anchor,))
    # Two eligible draws at one timepoint are ambiguous: no segment touches it.
    sibling = eligible.model_copy(update={"row_ordinal": 3, "timepoint_ordinal": 2})
    assert derive_segments((anchor, good_middle, sibling)) == ()
    assert rows  # protected rows unchanged


def test_segments_cannot_be_supplied_by_a_renderer(base) -> None:
    _, workspace = base
    bogus = workspace.segments[0].model_copy(update={"to_delta": 99.0})
    with pytest.raises(ValueError):
        LongitudinalWorkspace.model_validate(
            {**workspace.model_dump(mode="python"), "segments": (bogus,)}
        )
    no_comparison = workspace.segments[0].model_copy(
        update={"to_row_ordinal": 4, "to_timepoint_ordinal": 2}
    )
    with pytest.raises(ValueError):
        LongitudinalWorkspace.model_validate(
            {**workspace.model_dump(mode="python"), "segments": (no_comparison,)}
        )


def test_filters_select_rows_without_changing_counts_or_roles(base) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    baseline = _derive(world, workspace, rows)
    cases = [
        LongitudinalWorkspaceFilters(timepoint_ordinals=(1,)),
        LongitudinalWorkspaceFilters(
            lineage_roles=(MemberLineageRole.TECHNICAL_REPLICATE,)
        ),
        LongitudinalWorkspaceFilters(
            record_availability=(CohortRecordAvailability.MISSING,)
        ),
        LongitudinalWorkspaceFilters(
            compatibility_states=(RowCompatibilityState.UNKNOWN,)
        ),
    ]
    replay = {baseline.replay_sha256}
    for filters in cases:
        request = workspace.request.model_copy(update={"filters": filters})
        derived = _derive(world, workspace, rows, request=request)
        projection = project_longitudinal_workspace(derived)
        assert projection.population == workspace.population
        assert projection.total_row_count == 4
        assert derived.rows == baseline.rows
        replay.add(derived.replay_sha256)
        if filters.timepoint_ordinals == (1,):
            roles = [row.lineage_role for row in projection.rows]
            assert roles == [
                MemberLineageRole.BIOLOGICAL_DRAW,
                MemberLineageRole.TECHNICAL_REPLICATE,
            ]
        if filters.lineage_roles:
            assert [row.denominator_contributor for row in projection.rows] == [False]
            assert projection.segments == ()
    assert len(replay) == len(cases) + 1


def test_filter_order_is_normalized_and_byte_identical(world: World) -> None:
    first = world.request.model_copy(
        update={
            "filters": LongitudinalWorkspaceFilters(
                timepoint_ordinals=(3, 1),
                record_availability=(
                    CohortRecordAvailability.MISSING,
                    CohortRecordAvailability.AVAILABLE,
                ),
            )
        }
    )
    second = world.request.model_copy(
        update={
            "filters": LongitudinalWorkspaceFilters(
                timepoint_ordinals=(1, 3),
                record_availability=(
                    CohortRecordAvailability.AVAILABLE,
                    CohortRecordAvailability.MISSING,
                ),
            )
        }
    )
    one = project_longitudinal_workspace(world.build(request=first))
    two = project_longitudinal_workspace(world.build(request=second))
    assert longitudinal_projection_bytes(one) == longitudinal_projection_bytes(two)


@pytest.mark.parametrize(
    "change",
    [
        "member",
        "decision",
        "comparison",
        "source",
        "denominator",
        "covariate",
        "policy",
    ],
)
def test_every_commitment_changes_the_replay_digest(base, change) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    updates = {}
    if change == "member":
        rows[index] = _edit(
            rows[index],
            history=rows[index].history.model_copy(
                update={"affected_comparison_count": 1}
            ),
        )
    elif change == "decision":
        rows[index] = _edit(
            rows[index],
            decision=rows[index].decision.model_copy(
                update={"decision_sha256": "d" * 64}
            ),
        )
    elif change == "comparison":
        rows[index] = _edit(
            rows[index],
            comparison=rows[index].comparison.model_copy(
                update={"comparison_sha256": "c" * 64}
            ),
        )
    elif change == "source":
        rows[index] = _edit(
            rows[index],
            source=rows[index].source.model_copy(update={"source_sha256": "b" * 64}),
        )
    elif change == "denominator":
        updates["population"] = workspace.population.model_copy(
            update={"population_sha256": "a" * 64}
        )
    elif change == "covariate":
        updates["covariate_context"] = workspace.covariate_context.model_copy(
            update={"context_sha256": "9" * 64}
        )
    else:
        updates["authority"] = workspace.authority.model_copy(
            update={"projection_policy_sha256": "8" * 64}
        )
    assert _derive(world, workspace, rows, **updates).replay_sha256 != (
        workspace.replay_sha256
    )


def test_unequal_intervals_and_shared_collection_positions(base) -> None:
    world, _ = base
    positions = timepoint_positions(world.manifest)
    assert sorted(positions.values()) == [(1, 0), (2, 86_400), (3, 172_800)]
    members = world.manifest.members
    draw = next(m for m in members if m.linkage_id.endswith("1" * 32))
    replicate = next(
        m for m in members if m.lineage_role is MemberLineageRole.TECHNICAL_REPLICATE
    )
    assert (
        positions[(draw.time_coordinate, draw.biological_timepoint_id)]
        == positions[(replicate.time_coordinate, replicate.biological_timepoint_id)]
    )


def test_version_diff_reports_membership_and_policy_changes(base) -> None:
    world, _ = base
    first = world.manifest
    second = first.model_copy(
        update={
            "version": 2,
            "members": first.members[:3],
            "policies": first.policies.model_copy(
                update={"missingness_sha256": "4" * 64}
            ),
        }
    )
    diff = derive_version_diff((first, second))
    assert diff.kind is VersionDiffKind.PREDECESSOR
    assert (
        diff.added_member_count,
        diff.removed_member_count,
        diff.unchanged_member_count,
    ) == (
        0,
        1,
        3,
    )
    assert diff.reasons == (
        VersionDiffReason.MEMBERS_REMOVED,
        VersionDiffReason.MISSINGNESS_POLICY_CHANGED,
    )
    assert diff.silent_upgrade is False and diff.d03_policy_change == "not_comparable"
    text = diff.model_dump_json()
    for member in first.members:
        assert member.linkage_id not in text and member.subject_token not in text


def test_exactly_the_bound_derives_deterministically_and_one_more_is_rejected(
    base,
) -> None:
    world, workspace = base
    template = workspace.protected_rows[3]  # a missing draw
    positions = {}
    rows = []
    for index in range(MAX_WORKSPACE_MEMBERS):
        member = template.member.model_copy(
            update={
                "time_coordinate": template.member.time_coordinate + index * 60,
                "biological_timepoint_id": f"timepoint_{index:032x}",
            }
        )
        import hashlib

        from evidence_inspector.method_registry import canonical_contract_bytes

        digest = hashlib.sha256(canonical_contract_bytes(member)).hexdigest()
        positions[(member.time_coordinate, member.biological_timepoint_id)] = (
            index + 1,
            index * 60,
        )
        rows.append(
            template.model_copy(
                update={
                    "row_ordinal": index + 1,
                    "member": member,
                    "member_sha256": digest,
                    "status": template.status.model_copy(
                        update={"member_sha256": digest}
                    ),
                    "is_anchor": index == 0,
                    "decision_state": DecisionState.ANCHOR
                    if index == 0
                    else DecisionState.NOT_IN_SERIES,
                }
            )
        )
    authority = workspace.authority.model_copy(update={"anchor_row_ordinal": 1})
    inputs = _inputs(workspace, authority=authority)
    one = derive_workspace(inputs, tuple(rows), positions=positions)
    two = derive_workspace(inputs, tuple(rows), positions=positions)
    assert len(one.rows) == MAX_WORKSPACE_MEMBERS
    assert one == two
    extra = rows[-1].model_copy(update={"row_ordinal": MAX_WORKSPACE_MEMBERS + 1})
    with pytest.raises(ValueError):
        derive_workspace(inputs, (*rows, extra), positions=positions)


def test_protected_row_rejects_result_details_on_unavailable_records(base) -> None:
    _, workspace = base
    available = next(row for row in workspace.protected_rows if row.source is not None)
    missing = available.status.model_copy(
        update={"availability": CohortRecordAvailability.MISSING, "binding": None}
    )
    with pytest.raises(ValueError):
        _edit(available, status=missing)
    public = next(row for row in workspace.rows if row.values)
    with pytest.raises(ValueError):
        LongitudinalSourceRow.model_validate(
            {
                **public.model_dump(mode="python"),
                "record_availability": CohortRecordAvailability.MISSING,
            }
        )
    with pytest.raises(ValueError):
        LongitudinalSourceRow.model_validate(
            {
                **public.model_dump(mode="python"),
                "compatibility_state": RowCompatibilityState.REGISTERED_BRIDGE,
            }
        )


def test_reason_codes_and_actions_are_copied_exactly(base) -> None:
    _, workspace = base
    protected = next(
        row for row in workspace.protected_rows if row.decision is not None
    )
    public = workspace.rows[protected.row_ordinal - 1]
    assert (
        public.compatibility_reasons
        == protected.decision.reason_codes
        == (LongitudinalReason.EXACT_MATCH,)
    )
    assert public.next_action is protected.decision.next_action
    assert public.bridge_execution_state == "not_executed"
    assert MeasurementScope  # imported scope contract stays the reader scope type


def test_unauthorized_caller_learns_nothing_about_store_health(world: World) -> None:
    forged = ReaderGrantBinding(
        grant_sha256="0" * 64, state_head_sha256=world.credential.state_head_sha256
    )
    for overrides in (
        {"cohort_registry": world.records},
        {"result_view_source_registry": world.family},
        {},
    ):
        error = _boundary(
            lambda overrides=overrides: world.build(credential=forged, **overrides)
        )
        assert error.code is LongitudinalErrorCode.PERMISSION_DENIED


def _doctor_page(monkeypatch, cls, name: str, update) -> None:
    original = workspace_module._call

    def doctored(store, store_cls, method, *args, **kwargs):
        value = original(store, store_cls, method, *args, **kwargs)
        if (store_cls, method) != (cls, name):
            return value
        return value.model_copy(
            update={"records": tuple(update(item) for item in value.records)}
        )

    monkeypatch.setattr(workspace_module, "_call", doctored)


def test_stable_stale_source_is_an_authority_error_not_a_missing_source(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evidence_inspector.result_view_source_registry import SourceAuthorityState

    _doctor_page(
        monkeypatch,
        ResultViewSourceRegistry,
        "list_selectors",
        lambda item: item.model_copy(
            update={"authority_state": SourceAuthorityState.STALE}
        ),
    )
    error = _boundary(world.build)
    assert error.code is LongitudinalErrorCode.AUTHORITY_STALE


def test_stable_stale_series_is_an_authority_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evidence_inspector.longitudinal_decision_registry import (
        LongitudinalDecisionRegistry,
        SeriesAuthorityState,
    )

    _doctor_page(
        monkeypatch,
        LongitudinalDecisionRegistry,
        "list_selectors",
        lambda item: item.model_copy(
            update={"authority_state": SeriesAuthorityState.STALE}
        ),
    )
    error = _boundary(world.build)
    assert error.code is LongitudinalErrorCode.AUTHORITY_STALE
    assert error.remediation.value == "refresh_decision_registration"


def test_replay_digest_ignores_the_live_d07_replay_instant(base) -> None:
    world, workspace = base
    rows = list(workspace.protected_rows)
    index = next(i for i, row in enumerate(rows) if row.comparison is not None)
    later = rows[index].comparison.replayed_at.replace(year=2030)
    rows[index] = _edit(
        rows[index],
        comparison=rows[index].comparison.model_copy(update={"replayed_at": later}),
    )
    derived = _derive(world, workspace, rows)
    assert derived.replay_sha256 == workspace.replay_sha256
    assert longitudinal_projection_bytes(
        project_longitudinal_workspace(derived)
    ) == longitudinal_projection_bytes(project_longitudinal_workspace(workspace))


def test_public_heads_admit_no_reader_slot(base) -> None:
    _, workspace = base
    heads = list(workspace.authority.heads)
    reader = workspace.authority.heads[0].model_copy(
        update={
            "slot": "reader_authorization",
            "head": workspace.dependency_heads.reader_authorization,
        }
    )
    relabelled = heads[0].model_copy(
        update={"head": workspace.dependency_heads.reader_authorization}
    )
    for forged in (
        (reader, *heads[1:]),
        (*heads[:-1], heads[0]),
        (relabelled, *heads[1:]),
    ):
        with pytest.raises(ValueError):
            workspace.authority.model_validate(
                {**workspace.authority.model_dump(mode="python"), "heads": forged}
            )


def test_public_comparison_carries_no_timestamp(base) -> None:
    _, workspace = base
    public = longitudinal_projection_bytes(project_longitudinal_workspace(workspace))
    assert b"replayed_at" not in public
    assert b"2026-" not in public
    protected = next(row for row in workspace.protected_rows if row.comparison)
    assert protected.comparison.replayed_at.tzinfo is not None


@pytest.mark.parametrize(
    ("part", "field"),
    [
        ("d09_population", "record_status_sha256"),
        ("d09_population", "population_sha256"),
        ("d03_series", "object_sha256"),
    ],
)
def test_d10_context_must_derive_from_this_builds_d09_and_d03(
    world: World, monkeypatch: pytest.MonkeyPatch, part: str, field: str
) -> None:
    from evidence_inspector.covariate_context_registry import CovariateContextRegistry

    original = workspace_module._call

    def doctored(store, cls, name, *args, **kwargs):
        value = original(store, cls, name, *args, **kwargs)
        if (cls, name) != (CovariateContextRegistry, "resolve"):
            return value
        live = value.live
        changed = getattr(live, part).model_copy(update={field: "0" * 64})
        return value.model_copy(
            update={"live": live.model_copy(update={part: changed})}
        )

    monkeypatch.setattr(workspace_module, "_call", doctored)
    error = _boundary(world.build)
    assert error.code is LongitudinalErrorCode.READ_CONFLICT
