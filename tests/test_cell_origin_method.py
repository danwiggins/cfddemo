"""CO2: the locked cell-origin method definition and its parameters.

Every input that can change a number must change the method hash: each
parameter, the modkit package digest and each asset digest.  Synthetic
registrations only; nothing here is a real asset or a real number.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from evidence_inspector.cell_origin_models import (
    UXM_METHYLATED_MIN_INCLUSIVE,
    UXM_MINIMUM_CPGS,
    UXM_UNMETHYLATED_MAX_EXCLUSIVE,
)
from evidence_inspector.cell_origin_pipeline import (
    DEFAULT_BOOTSTRAP_REPLICATES,
    DEFAULT_MAXIMUM_CALLS,
    DEFAULT_MAXIMUM_CPGS_PER_GROUP,
    DEFAULT_MAXIMUM_GROUPS,
    DEFAULT_MODKIT_FILTER_THRESHOLD,
    DEFAULT_RANDOM_SEED,
)
from evidence_inspector.cell_origin_prefilter import PREFILTER_REASONS
from evidence_inspector.deconvolution import DEFAULT_TOLERANCE, NNLS_SOLVER_IMPLEMENTATION_ID
from evidence_inspector.method_registry import MethodFamily, method_definition_sha256
from tests.test_method_assets import _loyfer_dir
from traceback_runner import cli
from traceback_runner.cell_origin_method import (
    METHOD_ID,
    PENDING_SCIENTIST_SIGNOFF,
    CellOriginCapsV1,
    CellOriginParametersV1,
    cell_origin_definition_at,
    cell_origin_method_definition,
    default_parameters,
    parameters_sha256,
)
from traceback_runner.contracts import RegisteredReference
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import local_method_definition
from traceback_runner.references import (
    LOYFER_DIRECTORY_FILES,
    AssetKind,
    ReferenceProblem,
    load_reference,
)
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.toolchain import MODKIT_PINS, pin_for

PIN = MODKIT_PINS["osx-arm64"]


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


@pytest.fixture(scope="module")
def registered(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RegisteredReference]:
    """A ROOT with a registered synthetic reference and three synthetic Loyfer files."""

    base = tmp_path_factory.mktemp("co2")
    inputs = create_local_golden_path_inputs(base / "inputs", reads=10)
    root = base / "root"
    code, payload = _main(
        "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    code, payload = _main("method-asset", "register", "--from-dir", _loyfer_dir(base), "--root", root)
    assert code == 0, payload
    return root, load_reference(root, "ref").registered


def _assets(root: Path) -> dict[AssetKind, Any]:
    from traceback_runner.cell_origin_method import registered_loyfer_assets

    return registered_loyfer_assets(root)


def test_defaults_are_the_values_the_code_applies() -> None:
    parameters = default_parameters()
    assert parameters.modkit_filter_threshold == DEFAULT_MODKIT_FILTER_THRESHOLD == 0.912109375
    assert "--filter-threshold" in parameters.modkit_arguments
    threshold = parameters.modkit_arguments.index("--filter-threshold") + 1
    assert float(parameters.modkit_arguments[threshold]) == DEFAULT_MODKIT_FILTER_THRESHOLD
    assert parameters.modification_codes == ("h", "m")
    assert (parameters.uxm_min_cpgs, parameters.u_max_exclusive, parameters.m_min_inclusive) == (
        UXM_MINIMUM_CPGS, UXM_UNMETHYLATED_MAX_EXCLUSIVE, UXM_METHYLATED_MIN_INCLUSIVE
    ) == (4, 0.251, 0.75)
    assert parameters.min_mapq == 20
    assert parameters.excluded_alignment_reasons == tuple(r.value for r in PREFILTER_REASONS)
    assert parameters.nnls_row_scale == "sqrt_count"  # Q3, pending sign-off
    assert "nnls_row_scale" in PENDING_SCIENTIST_SIGNOFF
    assert (parameters.nnls_tolerance, parameters.nnls_solver_id) == (
        DEFAULT_TOLERANCE, NNLS_SOLVER_IMPLEMENTATION_ID
    )
    assert (parameters.bootstrap_replicates, parameters.bootstrap_random_seed) == (
        DEFAULT_BOOTSTRAP_REPLICATES, DEFAULT_RANDOM_SEED
    ) == (200, 7)
    assert parameters.caps == CellOriginCapsV1(
        maximum_calls=DEFAULT_MAXIMUM_CALLS,
        maximum_groups=DEFAULT_MAXIMUM_GROUPS,
        maximum_cpgs_per_group=DEFAULT_MAXIMUM_CPGS_PER_GROUP,
    )
    assert parameters.cap_policy == "refuse"
    assert parameters.reference_range_comparison == "excluded"
    assert parameters.modbase_model_declared is None
    # Canonical JSON round-trips exactly.
    content = canonical_json_bytes(parameters)
    assert CellOriginParametersV1.model_validate_json(content) == parameters
    assert parameters_sha256(parameters) == hashlib.sha256(content).hexdigest()


def test_definition_binds_tool_package_digest_and_every_asset(registered) -> None:
    root, reference = registered
    assets = _assets(root)
    definition = cell_origin_method_definition(reference, assets, PIN, default_parameters())
    assert definition.method_id == METHOD_ID == "mth_cell_origin_loyfer_uxm"
    assert definition.family == MethodFamily.CELL_ORIGIN
    assert definition.version == "1.0.0-local-ref"
    (tool,) = definition.tools
    assert (tool.tool_id, tool.version, tool.artifact_sha256) == (
        "tool_modkit", "0.6.4", PIN.package_sha256
    )
    # The registered FASTA first (the catalog's reference asset), then Loyfer.
    fragment = local_method_definition(reference)
    assert definition.assets[0] == fragment.assets[0]
    assert {item.asset_id: item.content_sha256 for item in definition.assets[1:]} == {
        asset_id: assets[kind].file_sha256
        for kind, (_, asset_id) in LOYFER_DIRECTORY_FILES.items()
    }
    assert definition.parameter_schema_sha256 == parameters_sha256(default_parameters())


def test_settings_enter_the_definition_through_the_root_loader(registered) -> None:
    root, reference = registered
    plain = cell_origin_definition_at(root, reference, {}, platform_name="osx-arm64")
    declared = cell_origin_definition_at(
        root, reference, {"modbase_model": "model-a"}, platform_name="osx-arm64"
    )
    other = cell_origin_definition_at(
        root, reference, {"modbase_model": "model-b"}, platform_name="osx-arm64"
    )
    hashes = {method_definition_sha256(item) for item in (plain, declared, other)}
    assert len(hashes) == 3
    linux = cell_origin_definition_at(root, reference, {}, platform_name="linux-64")
    assert linux.tools[0].artifact_sha256 == MODKIT_PINS["linux-64"].package_sha256
    assert method_definition_sha256(linux) != method_definition_sha256(plain)


def test_unregistered_assets_refuse_with_the_register_command(tmp_path: Path) -> None:
    inputs = create_local_golden_path_inputs(tmp_path / "inputs", reads=10)
    root = tmp_path / "root"
    code, _ = _main(
        "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0
    reference = load_reference(root, "ref").registered
    with pytest.raises(ReferenceProblem) as refused:
        cell_origin_definition_at(root, reference, {}, platform_name="osx-arm64")
    assert refused.value.code == "TBX-ASSET-004"
    assert "method-asset register" in refused.value.fix


# --------------------------------------------------------------------------
# One mutation per parameter, tool digest and asset digest: each changes the hash
# --------------------------------------------------------------------------

_PARAMETER_MUTATIONS: dict[str, Any] = {
    "schema_version": None,  # a Literal: the only value is the current schema
    "modkit_subcommand": None,  # a Literal
    "modkit_arguments": lambda p: (*p.modkit_arguments, "--ignore-index"),
    "modkit_filter_threshold": lambda p: 0.75,
    "modification_codes": lambda p: ("m",),
    "modification_collapse": None,  # a Literal
    "uxm_min_cpgs": lambda p: 3,
    "u_max_exclusive": lambda p: 0.25,
    "m_min_inclusive": lambda p: 0.8,
    "prefilter_policy": None,  # a Literal
    "prefilter_scope": None,  # a Literal
    "min_mapq": lambda p: 30,
    "excluded_alignment_reasons": lambda p: p.excluded_alignment_reasons[:-1],
    "nnls_row_scale": lambda p: "reference_count",
    "nnls_tolerance": lambda p: 1e-10,
    "nnls_max_iterations": lambda p: 5_000,
    "nnls_solver_id": lambda p: "traceback.other-nnls.v1",
    "normalization": None,  # a Literal
    "bootstrap_replicates": lambda p: 201,
    "bootstrap_random_seed": lambda p: 8,
    "bootstrap_confidence_level": lambda p: 0.9,
    "caps": lambda p: p.caps.model_copy(update={"maximum_calls": 999_999}),
    "cap_policy": None,  # a Literal
    "min_classified_fragments": lambda p: p.min_classified_fragments + 1,
    "min_observed_markers": lambda p: p.min_observed_markers + 1,
    "atlas_contributor_set": None,  # a Literal
    "reference_range_comparison": None,  # a Literal
    "modbase_model_declared": lambda p: "model-a",
}
_CAPS_MUTATIONS = {"maximum_groups": 99_999, "maximum_cpgs_per_group": 9_999}


def test_every_parameter_has_a_mutation_case() -> None:
    # A new parameter without a case here fails, so none can escape the hash test.
    assert set(_PARAMETER_MUTATIONS) == set(CellOriginParametersV1.model_fields)
    for name, mutate in _PARAMETER_MUTATIONS.items():
        if mutate is None:
            annotation = str(CellOriginParametersV1.model_fields[name].annotation)
            assert "Literal" in annotation, name
    assert set(_CAPS_MUTATIONS) | {"maximum_calls"} == set(CellOriginCapsV1.model_fields)


def _cases() -> list[tuple[str, str]]:
    cases = [
        ("parameter", name)
        for name, mutate in _PARAMETER_MUTATIONS.items()
        if mutate is not None
    ]
    cases += [("caps", name) for name in _CAPS_MUTATIONS]
    cases += [("tool", "package_sha256"), ("tool", "version")]
    cases += [("asset", kind.value) for kind in LOYFER_DIRECTORY_FILES]
    cases += [("reference", "asset_sha256")]
    return cases


@pytest.mark.parametrize(("part", "name"), _cases())
def test_changing_one_input_changes_the_definition_hash(registered, part, name) -> None:
    root, reference = registered
    assets = _assets(root)
    parameters = default_parameters()
    tool: Any = PIN
    base = method_definition_sha256(
        cell_origin_method_definition(reference, assets, tool, parameters)
    )
    if part == "parameter":
        parameters = parameters.model_copy(
            update={name: _PARAMETER_MUTATIONS[name](parameters)}
        )
        # Validated again, so a mutation is a value the contract accepts.
        parameters = CellOriginParametersV1.model_validate(parameters.model_dump())
    elif part == "caps":
        parameters = parameters.model_copy(
            update={"caps": parameters.caps.model_copy(update={name: _CAPS_MUTATIONS[name]})}
        )
    elif part == "tool":
        tool = dataclasses.replace(
            PIN, **{name: "9" * 64 if name == "package_sha256" else "0.6.5"}
        )
    elif part == "asset":
        kind = AssetKind(name)
        assets = {**assets, kind: assets[kind].model_copy(update={"file_sha256": "7" * 64})}
    else:
        reference = reference.model_copy(update={"asset_sha256": "8" * 64})
    changed = method_definition_sha256(
        cell_origin_method_definition(reference, assets, tool, parameters)
    )
    assert changed != base, (part, name)


def test_platform_pin_is_the_current_one() -> None:
    try:
        pin = pin_for("modkit")
    except ReferenceProblem:
        pytest.skip("no modkit pin for this platform")
    assert pin.package_sha256 in {item.package_sha256 for item in MODKIT_PINS.values()}
