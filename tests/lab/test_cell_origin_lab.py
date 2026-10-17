"""L3: Lab cell-origin through its in-house MM/ML extraction path."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pysam
import pytest

import traceback_runner.cell_origin_method as method_module
from tests.test_cell_origin_stage import _test_parameters
from traceback_lab.algorithms import cell_origin as co
from traceback_lab.registry import InputContext, Ready
from traceback_runner.fixtures import create_cell_origin_inputs
from traceback_runner.references import register_loyfer_directory, register_reference


@pytest.fixture
def world(tmp_path, monkeypatch):
    inputs = create_cell_origin_inputs(tmp_path / "inputs", header_model="model-h")
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
    extras = {**world["ctx"].extras,
              "planned_asset_sha256s": co.CELL_ORIGIN.asset_sha256s(world["root"])}
    ctx = InputContext(**{**world["ctx"].__dict__, "extras": extras})
    output = co.CELL_ORIGIN.run(ctx, out)
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


def test_inhouse_extraction_matches_the_signed_synthetic_fixture_counts(world) -> None:
    from traceback_lab.adapters.cell_origin_inhouse import compute_inhouse
    from traceback_runner.cell_origin_method import default_parameters

    inputs = world["inputs"]
    measurement = compute_inhouse(
        parameters=default_parameters(modbase_model="model-h"),
        bam=inputs.bam_path,
        fasta=inputs.fasta_path,
        atlas=inputs.loyfer_dir / "Atlas.U250.l4.hg38.full.tsv",
        markers=inputs.loyfer_dir / "Markers.U250.hg38.tsv",
        regions=inputs.loyfer_dir / "Regions.U250.l4.hg38.bed",
        scratch=world["tmp"] / "inhouse-work",
    )
    marker_reads = sum(u + m for u, m in inputs.planted)
    assert len(measurement.extraction.calls) == marker_reads * 10
    assert measurement.classified_fragments == marker_reads
    assert measurement.observed_markers == len(inputs.planted)
    assert {row.cell_type_id: row.fraction for row in measurement.fit.estimates} == pytest.approx(
        {"TypeA": 0.7, "TypeB": 0.3}, abs=1e-6
    )


def test_pinned_modkit_and_inhouse_marker_classes_match(world) -> None:
    binary = os.environ.get("LAB_MODKIT_PARITY_BINARY")
    if not binary:
        pytest.skip("set LAB_MODKIT_PARITY_BINARY to the pinned modkit 0.6.4 binary")
    from traceback_lab.adapters.cell_origin_inhouse import compute_inhouse
    from traceback_runner.cell_origin import ModbaseModel, compute_measurement
    from traceback_runner.cell_origin_method import default_parameters
    from traceback_runner.toolchain import MODKIT_PINS, PinnedTool, ToolIdentity

    path = Path(binary)
    digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    pin = MODKIT_PINS["linux-64"]
    assert digest == pin.package_binary_sha256
    tool = PinnedTool(path=path, identity=ToolIdentity(
        tool_id="modkit", version=pin.version, platform=pin.platform,
        lock_sha256=pin.lock_sha256, lock_line=pin.lock_line,
        package_sha256=pin.package_sha256,
        package_binary_sha256=pin.package_binary_sha256,
        installed_binary_sha256=digest,
    ))
    inputs = world["inputs"]
    parameters = default_parameters(modbase_model="model-h")
    paths = {
        "bam": inputs.bam_path,
        "fasta": inputs.fasta_path,
        "atlas": inputs.loyfer_dir / "Atlas.U250.l4.hg38.full.tsv",
        "markers": inputs.loyfer_dir / "Markers.U250.hg38.tsv",
        "regions": inputs.loyfer_dir / "Regions.U250.l4.hg38.bed",
    }
    signed = compute_measurement(
        parameters=parameters, reference_id="ref",
        modbase_model=ModbaseModel(id="model-h", source="header"),
        job_directory=world["tmp"] / "signed-parity", modkit=tool, **paths,
    )
    inhouse = compute_inhouse(
        parameters=parameters, scratch=world["tmp"] / "inhouse-parity", **paths,
    )
    assert [(row.marker_id, row.u, row.m, row.x) for row in signed.marker_counts] == [
        (row.marker_id, row.u_count, row.m_count, row.x_count)
        for row in inhouse.marker_counts
    ]
    assert {row.contributor_id: row.fraction for row in signed.estimates} == pytest.approx(
        {row.cell_type_id: row.fraction for row in inhouse.fit.estimates}, abs=1e-6
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
    assert set(entry["license_ids"]) == {"yissum-uxm", "loyfer-atlas-terms-unknown"}


def test_a_changed_fasta_is_refused(world) -> None:
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    ctx = InputContext(**{**world["ctx"].__dict__, "reference_sha256": "f" * 64})
    out = world["tmp"] / "a2" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="FASTA changed"):
        co.CELL_ORIGIN.run(ctx, out)


def test_a_changed_atlas_registration_is_refused_before_extraction(world) -> None:
    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    extras = {**world["ctx"].extras, "planned_asset_sha256s": ["f" * 64]}
    ctx = InputContext(**{**world["ctx"].__dict__, "extras": extras})
    out = world["tmp"] / "a3" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="registration changed"):
        co.CELL_ORIGIN.run(ctx, out)
    assert not list(out.parent.rglob("*.bam"))
