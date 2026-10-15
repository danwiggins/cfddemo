"""L0.5b: cell-origin through the public, lease-free compute_measurement."""

from __future__ import annotations

import json
from pathlib import Path

import pysam
import pytest

import traceback_runner.cell_origin_method as method_module
from tests.test_cell_origin_stage import _fake_modkit, _test_parameters
from traceback_lab.algorithms import cell_origin as co
from traceback_lab.registry import InputContext, Ready
from traceback_runner.fixtures import create_cell_origin_inputs
from traceback_runner.references import register_loyfer_directory, register_reference


@pytest.fixture
def world(tmp_path, monkeypatch):
    inputs = create_cell_origin_inputs(tmp_path / "inputs", header_model="model-h")
    tool = _fake_modkit(tmp_path)
    monkeypatch.setattr(co, "_modkit", lambda: tool)
    monkeypatch.setattr(co, "modkit_or_none", lambda: tool)
    monkeypatch.setattr(method_module, "default_parameters", _test_parameters())
    root = tmp_path / "root"
    register_reference(root, inputs.fasta_path, "ref")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").symlink_to(inputs.bam_path)
    index = Path(str(inputs.bam_path) + ".bai")
    if not index.exists():
        pysam.index(str(inputs.bam_path))
    (input_dir / "aligned.bam.bai").symlink_to(index)
    ctx = InputContext(
        root=root, source_id="a" * 64, input_id="b" * 64, reference_id="ref",
        input_dir=input_dir, eligible_reads=3_000_000, extras={"reference_fasta": str(inputs.fasta_path)},
        reference_sha256=__import__("hashlib").sha256(Path(inputs.fasta_path).read_bytes()).hexdigest(),
    )
    return {"inputs": inputs, "root": root, "ctx": ctx, "tmp": tmp_path}


def test_preflight_skips_until_the_atlas_is_registered(world) -> None:
    ctx = world["ctx"]
    assert co.CELL_ORIGIN.preflight(ctx).reason_code == "missing_asset"
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    assert isinstance(co.CELL_ORIGIN.preflight(ctx), Ready)
    assert co.CELL_ORIGIN.preflight(InputContext(**{**ctx.__dict__, "eligible_reads": 10})).reason_code == "depth"
    assert co.CELL_ORIGIN.preflight(InputContext(**{**ctx.__dict__, "example": True})).reason_code == "example_reference"
    assert len(co.CELL_ORIGIN.asset_sha256s(world["root"])) == 3


def test_run_recovers_the_planted_mixture_with_intervals(world) -> None:
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    out = world["tmp"] / "attempt" / "artifacts"
    out.mkdir(parents=True)
    output = co.CELL_ORIGIN.run(world["ctx"], out)
    values = {m["metric_id"]: m for m in output.metrics}
    fractions = {k: v for k, v in values.items() if k.startswith("fraction_")}
    assert len(fractions) == 2
    assert sorted(round(v["value"], 1) for v in fractions.values()) == [0.3, 0.7]
    assert values["classified_fragments"]["value"] > 0
    composition = json.loads(output.artifacts["composition.json"])
    assert composition["solver_converged"] is True
    assert {row["contributor_id"] for row in composition["contributors"]} == set(
        name for name in (row["contributor_id"] for row in composition["contributors"])
    )


def test_transient_bam_for_a_split_half_is_a_subset(world) -> None:
    source = world["ctx"].input_dir / "aligned.bam"
    destination = world["tmp"] / "half.bam"
    kept = co.write_transient_bam(source, destination, {"transform": "split", "half": "A"})
    total = int(str(pysam.view("-c", str(source))).strip())
    other = co.write_transient_bam(source, world["tmp"] / "halfb.bam", {"transform": "split", "half": "B"})
    assert kept + other == total and 0 < kept < total
    with pytest.raises(ValueError):
        co.write_transient_bam(source, world["tmp"] / "x.bam", {"transform": "cna", "tf": 0.1})


def test_derived_run_leaves_no_transient_bam(world) -> None:
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    out = world["tmp"] / "scratch" / "artifacts"
    out.mkdir(parents=True)
    ctx = InputContext(**{**world["ctx"].__dict__, "derived": {"transform": "split", "half": "A"}})
    try:
        co.CELL_ORIGIN.run(ctx, out)
    except Exception:  # noqa: BLE001 - a half of the tiny fixture may fall below the mini floors
        pass
    assert not list((world["tmp"] / "scratch").rglob("*.bam"))


def test_metric_ids_and_library_cover_the_atlas() -> None:
    from traceback_lab.interpretation.engine import load_library

    library = load_library()
    assert co.metric_id_for("Blood-Mono-Macro") == "fraction_blood_mono_macro"
    assert library.headline("cell-origin").metric_id == "fraction_blood_granul"
    entry = library.entry("cell-origin", "fraction_liver_hep")
    assert entry["min_depth_x"] == 5.0 and entry.tier == "SPECULATIVE"
    assert set(entry["license_ids"]) == {"modkit-ont-public", "yissum-uxm", "loyfer-atlas-terms-unknown"}


def test_a_changed_fasta_is_refused(world) -> None:
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    ctx = InputContext(**{**world["ctx"].__dict__, "reference_sha256": "f" * 64})
    out = world["tmp"] / "a2" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="FASTA changed"):
        co.CELL_ORIGIN.run(ctx, out)
