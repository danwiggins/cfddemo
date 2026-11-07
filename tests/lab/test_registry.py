"""The plug-in contract, registry loading and run planning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from traceback_runner.serialization import canonical_json_bytes
from traceback_lab.problems import LabProblem
from traceback_lab.registry import (
    AlgorithmOutput,
    InputContext,
    Ready,
    Requirements,
    ToolPin,
    execution_identity,
    load_registry,
    plan,
    validate_algorithm,
)


@dataclass
class Fake:
    id: str
    version: str = "1"
    family: str = "qc"
    requires: Requirements = field(default_factory=Requirements)
    consumes: str = "read_summary"
    cost: str = "fast"
    parameters: dict[str, Any] = field(default_factory=dict)
    tools: tuple[ToolPin, ...] = ()
    upstream: tuple[str, ...] = ()

    def preflight(self, ctx: InputContext) -> Ready:
        return Ready()

    def run(self, ctx: InputContext, out: Path) -> AlgorithmOutput:
        return AlgorithmOutput()


READ_SUMMARY = Fake("read-summary")
FRAGMENT = Fake("fragment-size", upstream=("read-summary",))
CELL_ORIGIN = Fake("cell-origin", cost="slow", consumes="full_bam", upstream=("read-summary",))
BAM_QC = Fake("bam-qc", upstream=("read-summary",))
REGISTRY = (READ_SUMMARY, BAM_QC, FRAGMENT, CELL_ORIGIN)


def test_validate_refuses_bad_plugins() -> None:
    for bad in (
        Fake("Bad_Id"),
        Fake("x", family="astrology"),
        Fake("x", consumes="vibes"),
        Fake("x", cost="medium"),
        Fake("x", version=""),
        Fake("x", requires=Requirements(min_eligible_reads=-1)),
    ):
        with pytest.raises(ValueError):
            validate_algorithm(bad)
    with pytest.raises(TypeError):
        validate_algorithm(object())


def test_load_registry_checks_duplicates_and_upstream_order() -> None:
    module = __name__
    assert [a.id for a in load_registry([f"{module}:READ_SUMMARY", f"{module}:FRAGMENT"])] == [
        "read-summary",
        "fragment-size",
    ]
    with pytest.raises(ValueError, match="earlier"):
        load_registry([f"{module}:FRAGMENT", f"{module}:READ_SUMMARY"])
    with pytest.raises(ValueError, match="duplicate"):
        load_registry([f"{module}:READ_SUMMARY", f"{module}:READ_SUMMARY"])
    with pytest.raises(ValueError):
        load_registry(["no-colon"])


def test_shipped_registry_loads() -> None:
    load_registry()


def test_shipped_parameters_preserve_identity_after_recorded_json_roundtrip() -> None:
    for algorithm in load_registry():
        encoded = canonical_json_bytes(algorithm.parameters)
        recorded = json.loads(encoded)
        assert canonical_json_bytes(recorded) == encoded, algorithm.id


def test_profile_fast_drops_slow_algorithms() -> None:
    ids = [item.algorithm.id for item in plan(REGISTRY, profile="fast")]
    assert ids == ["read-summary", "bam-qc", "fragment-size"]
    assert [item.algorithm.id for item in plan(REGISTRY)] == [a.id for a in REGISTRY]


def test_only_overrides_profile_and_labels_upstream() -> None:
    items = plan(REGISTRY, only=["cell-origin"], profile="fast")
    assert [(item.algorithm.id, item.upstream_only) for item in items] == [
        ("read-summary", True),
        ("cell-origin", False),
    ]


def test_only_unknown_id_suggests_the_closest() -> None:
    with pytest.raises(LabProblem) as caught:
        plan(REGISTRY, only=["fragmnt-size"])
    problem = caught.value
    assert problem.code == "TBX-LAB-001" and problem.exit_code == 2
    assert "Did you mean fragment-size?" in problem.fix
    assert problem.data["suggestion"] == "fragment-size"


def test_execution_identity_changes_with_parameters_and_tools() -> None:
    base = execution_identity(Fake("x"), reference_sha256="b" * 64)
    assert base != execution_identity(Fake("x", parameters={"k": 1}), reference_sha256="b" * 64)
    assert base != execution_identity(
        Fake("x", tools=(ToolPin("samtools", "1.24"),)), reference_sha256="b" * 64
    )
    # license_id is interpretation, not identity
    assert execution_identity(
        Fake("x", tools=(ToolPin("samtools", "1.24", "MIT"),)), reference_sha256="b" * 64
    ) == execution_identity(
        Fake("x", tools=(ToolPin("samtools", "1.24", None),)), reference_sha256="b" * 64
    )
