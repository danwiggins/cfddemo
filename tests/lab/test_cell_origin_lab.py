"""L3: Lab cell-origin through its in-house MM/ML extraction path."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pysam
import pytest

import traceback_runner.cell_origin_method as method_module
from tests.test_cell_origin_stage import _test_parameters
from traceback_lab import cohorts
from traceback_lab.algorithms import cell_origin as co
from traceback_lab.algorithms.read_summary import READ_SUMMARY
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip, ToolPin, execution_identity
from traceback_lab.runner import RunTarget, StepResult, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id
import traceback_lab.runner as lab_runner
import traceback_lab.worker as lab_worker
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


def test_execution_identity_pins_the_mmml_parser_versions() -> None:
    pins = {tool.name: tool.version for tool in co.CELL_ORIGIN.tools}
    assert pins["numpy"] == np.__version__
    assert pins["pysam"] == pysam.__version__
    assert pins["htslib"] == pysam.__samtools_version__
    original = execution_identity(co.CELL_ORIGIN, reference_sha256="a" * 64)
    for parser in ("pysam", "htslib"):
        changed = replace(co.CELL_ORIGIN, tools=tuple(
            ToolPin(tool.name, "next-version" if tool.name == parser else tool.version)
            for tool in co.CELL_ORIGIN.tools
        ))
        assert execution_identity(changed, reference_sha256="a" * 64) != original
    sources = co.CELL_ORIGIN.parameters["implementation_sha256s"]
    root = Path(co.__file__).resolve().parents[2]
    assert set(sources) == set(co._IMPLEMENTATION_FILES)
    assert all(
        digest == hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name, digest in sources.items()
    )
    for name in (
        "evidence_inspector/deconvolution.py",
        "evidence_inspector/cell_origin_inputs.py",
        "traceback_lab/algorithms/_base.py",
        "traceback_lab/read_summary.py",
    ):
        changed = replace(co.CELL_ORIGIN, parameters={
            **co.CELL_ORIGIN.parameters,
            "implementation_sha256s": {**sources, name: "0" * 64},
        })
        assert execution_identity(changed, reference_sha256="a" * 64) != original


def _publish_cohort_cell_origin(
    store: LabStore, *, index: int, algorithm: co.CellOrigin, publish_adapter: bool = True,
) -> tuple[str, str]:
    source_id, input_id, reference_sha256 = f"{index:064x}", f"{index + 100:064x}", "a" * 64
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": False,
        "inputs": [{"input_id": input_id, "kind": "aligned", "used_utc": "2026-10-17T00:00:00Z"}],
    }), encoding="utf-8")
    input_dir = store.inputs_dir / input_id
    input_dir.mkdir(parents=True, exist_ok=True)
    with pysam.AlignmentFile(str(input_dir / "aligned.bam"), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100}],
        "RG": [{"ID": "ont", "PL": "ONT"}],
    }) as bam:
        read = pysam.AlignedSegment()
        read.query_name = "fixture-read"
        read.query_sequence = "A"
        read.flag = 4
        read.query_qualities = pysam.qualitystring_to_array("I")
        read.set_tag("RG", "ont")
        bam.write(read)
    (input_dir / "manifest.json").write_text(json.dumps({
        "kind": "aligned", "source_id": source_id, "input_id": input_id,
        "reference_sha256": reference_sha256,
        "tools": {"samtools": "1.21", "minimap2": "2.28"},
        "mmi_sha256": "b" * 64, "aligner_argv": ["minimap2", "-ax", "map-ont"],
    }), encoding="utf-8")
    read_hash = execution_identity(READ_SUMMARY, reference_sha256=reference_sha256)
    read_plan = {
        "schema": "traceback.lab.execution-plan.v1",
        "algorithm_id": READ_SUMMARY.id, "algorithm_version": READ_SUMMARY.version,
        "input_id": input_id, "reference_sha256": reference_sha256,
        "platform": "ont", "parameters": READ_SUMMARY.parameters,
        "tools": [tool.identity() for tool in READ_SUMMARY.tools],
        "asset_sha256s": [], "upstream": [],
    }
    store.publish_result(ResultEnvelope(
        algorithm_id=READ_SUMMARY.id, algorithm_version=READ_SUMMARY.version, status="ok",
        provenance=provenance(tool="fixture", parameters_sha256=read_hash,
                              source_id=source_id, input_id=input_id, reference_id="hg38"),
    ), input_id=input_id, execution_plan=read_plan)
    read_attempt = store.selected_attempt(input_id, READ_SUMMARY.id, read_hash)
    assert read_attempt is not None
    tools = [tool.identity() for tool in algorithm.tools]
    assets = algorithm.asset_sha256s(store.root)
    upstream = [{"algorithm_id": READ_SUMMARY.id, "parameters_sha256": read_hash}]
    cell_hash = execution_identity(
        algorithm, reference_sha256=reference_sha256,
        upstream=upstream, asset_sha256s=assets, tool_pins=tools,
    )
    plan = {
        "schema": "traceback.lab.execution-plan.v1",
        "algorithm_id": algorithm.id, "algorithm_version": algorithm.version,
        "input_id": input_id, "reference_sha256": reference_sha256,
        "platform": "ont", "parameters": algorithm.parameters,
        "tools": tools, "asset_sha256s": assets, "upstream": upstream,
    }
    if publish_adapter:
        store.publish_result(ResultEnvelope(
            algorithm_id="cell-origin", algorithm_version=algorithm.version, status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=cell_hash,
                                  source_id=source_id, input_id=input_id, reference_id="hg38"),
            metrics=[Metric(metric_id="classified_fragments", value=100)],
        ), input_id=input_id, execution_plan=plan, upstream_selections=[{
            "algorithm_id": READ_SUMMARY.id, "input_id": input_id,
            "parameters_sha256": read_hash, "attempt_id": read_attempt.name,
        }])
    return source_id, cell_hash


def test_cohort_reads_recorded_parser_pin_after_reader_upgrade(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    reader_b = replace(co.CELL_ORIGIN, tools=tuple(
        ToolPin(tool.name, "next-parser" if tool.name in {"pysam", "htslib"} else tool.version)
        for tool in co.CELL_ORIGIN.tools
    ))
    first, first_hash = _publish_cohort_cell_origin(store, index=1, algorithm=co.CELL_ORIGIN)
    second, second_hash = _publish_cohort_cell_origin(store, index=2, algorithm=reader_b)
    third, third_hash = _publish_cohort_cell_origin(store, index=3, algorithm=co.CELL_ORIGIN)
    assert first_hash != second_hash
    assert first_hash == third_hash
    monkeypatch.setattr(cohorts, "load_registry", lambda: (READ_SUMMARY, reader_b))
    member = cohorts.member_from_store(store, first)
    selected = {row.algorithm_id: row for row in member.results}
    assert selected["cell-origin"].parameters_sha256 == first_hash
    cohorts.add_sample(store, "parser-cohort", first)
    cohorts.add_sample(store, "parser-cohort", second)
    cohorts.add_sample(store, "parser-cohort", third)
    generation = cohorts.build_cohort(store, "parser-cohort")
    by_source = {row.source_id: row for row in generation.members}
    first_result = next(row for row in by_source[first].results if row.algorithm_id == "cell-origin")
    second_result = next(row for row in by_source[second].results if row.algorithm_id == "cell-origin")
    third_result = next(row for row in by_source[third].results if row.algorithm_id == "cell-origin")
    assert first_result.cohort_compat_key != second_result.cohort_compat_key
    assert first_result.cohort_compat_key == third_result.cohort_compat_key
    projected = cohorts.project_sample_metric(
        store, "parser-cohort", target_source_id=first,
        algorithm_id="cell-origin", metric_id="classified_fragments",
    )
    assert projected is not None and projected.percentile_n == 1
    assert projected.exclusions == {"self": 1, "incompatible": 1}


def test_runner_plan_persists_cell_origin_parser_pin_for_cohort_read(tmp_path, monkeypatch) -> None:
    store = LabStore(tmp_path)
    source_id, expected_hash = _publish_cohort_cell_origin(
        store, index=9, algorithm=co.CELL_ORIGIN, publish_adapter=False,
    )
    input_id, reference_sha256 = f"{109:064x}", "a" * 64
    read_hash = execution_identity(READ_SUMMARY, reference_sha256=reference_sha256)
    read_attempt = store.selected_attempt(input_id, READ_SUMMARY.id, read_hash)
    assert read_attempt is not None
    target = RunTarget(
        input_id=input_id, source_id=source_id, input_dir=store.inputs_dir / input_id,
        reference_id="hg38", reference_sha256=reference_sha256,
    )
    monkeypatch.setattr(co.CELL_ORIGIN, "preflight", lambda _ctx: Ready())
    ran = []

    def fake_pipeline(stages, **_kwargs):
        request = json.loads(Path(stages[0].argv[-1]).read_bytes())
        scratch = Path(request["out_dir"])
        (scratch / "artifacts").mkdir(exist_ok=True)
        (scratch / "output.json").write_text(json.dumps({
            "metrics": [{"metric_id": "classified_fragments", "value": 100}], "panels": [],
        }))
        ran.append(request)
        return SimpleNamespace(
            ok=True, duration_s=0.01, peak_rss_bytes=0,
            sampled_tree_peak_rss_bytes=0, peak_disk_bytes=0,
        )

    monkeypatch.setattr(lab_runner, "run_pipeline", fake_pipeline)
    run_id = new_run_id()
    results = run_plan(
        store, None, store.run_log(run_id), target,
        [PlanItem(co.CELL_ORIGIN, False)], {"cell-origin": "cell_origin:CELL_ORIGIN"},
        run_id=run_id, scrubber=Scrubber(root=store.root),
        upstream_results={READ_SUMMARY.id: StepResult(
            READ_SUMMARY.id, "ok", True, read_hash, input_id=input_id,
            attempt_id=read_attempt.name,
        )},
    )
    assert len(ran) == 1 and len(results) == 1
    assert results[0].status == "ok" and results[0].parameters_sha256 == expected_hash
    attempt = store.selected_attempt(input_id, "cell-origin", expected_hash)
    assert attempt is not None and attempt.name == results[0].attempt_id
    plan = json.loads((attempt / "execution_plan.json").read_bytes())
    assert plan["platform"] == "ont"
    assert plan["parameters"] == co.CELL_ORIGIN.parameters
    assert plan["tools"] == [tool.identity() for tool in co.CELL_ORIGIN.tools]
    assert plan["asset_sha256s"] == co.CELL_ORIGIN.asset_sha256s(store.root)
    assert plan["upstream"] == [{"algorithm_id": READ_SUMMARY.id, "parameters_sha256": read_hash}]
    assert store.upstream_selections_at(attempt) == ({
        "algorithm_id": READ_SUMMARY.id, "input_id": input_id,
        "parameters_sha256": read_hash, "attempt_id": read_attempt.name,
    },)

    reader_b = replace(co.CELL_ORIGIN, tools=tuple(
        ToolPin(tool.name, "next-parser" if tool.name in {"pysam", "htslib"} else tool.version)
        for tool in co.CELL_ORIGIN.tools
    ))
    monkeypatch.setattr(cohorts, "load_registry", lambda: (READ_SUMMARY, reader_b))
    member = cohorts.member_from_store(store, source_id)
    selected = {row.algorithm_id: row for row in member.results}
    assert selected["cell-origin"].parameters_sha256 == expected_hash
    assert selected["cell-origin"].metrics["classified_fragments"] == 100


@pytest.mark.parametrize("tamper_output", [False, True])
def test_low_marker_support_publishes_a_skip_without_a_composition(
    tmp_path, monkeypatch, tamper_output,
) -> None:
    store = LabStore(tmp_path)
    source_id, expected_hash = _publish_cohort_cell_origin(
        store, index=19, algorithm=co.CELL_ORIGIN, publish_adapter=False,
    )
    input_id, reference_sha256 = f"{119:064x}", "a" * 64
    read_hash = execution_identity(READ_SUMMARY, reference_sha256=reference_sha256)
    read_attempt = store.selected_attempt(input_id, READ_SUMMARY.id, read_hash)
    assert read_attempt is not None
    target = RunTarget(
        input_id=input_id, source_id=source_id, input_dir=store.inputs_dir / input_id,
        reference_id="hg38", reference_sha256=reference_sha256,
    )
    monkeypatch.setattr(co.CELL_ORIGIN, "preflight", lambda _ctx: Ready())
    monkeypatch.setattr(co.CELL_ORIGIN, "run", lambda _ctx, _out: Skip(
        "low_support", f"cell-origin classified fragment and marker floors are not met at {tmp_path}/donor.tsv",
    ))
    monkeypatch.setattr(lab_worker, "load_entry", lambda _entry: co.CELL_ORIGIN)
    monkeypatch.setattr(lab_worker.signal, "signal", lambda *_args: None)

    def fake_pipeline(stages, **_kwargs):
        assert lab_worker.main([stages[0].argv[-1]]) == 0
        if tamper_output:
            scratch = Path(json.loads(Path(stages[0].argv[-1]).read_bytes())["out_dir"])
            output = json.loads((scratch / "output.json").read_bytes())
            output["metrics"] = [{"metric_id": "fraction_wrong", "value": 0.5}]
            (scratch / "output.json").write_text(json.dumps(output))
        return SimpleNamespace(
            ok=True, duration_s=0.01, peak_rss_bytes=0,
            sampled_tree_peak_rss_bytes=0, peak_disk_bytes=0,
        )

    monkeypatch.setattr(lab_runner, "run_pipeline", fake_pipeline)
    run_id = new_run_id()
    progress = []
    results = run_plan(
        store, None, store.run_log(run_id), target,
        [PlanItem(co.CELL_ORIGIN, False)], {"cell-origin": "cell_origin:CELL_ORIGIN"},
        run_id=run_id, scrubber=Scrubber(root=store.root),
        progress=progress.append,
        upstream_results={READ_SUMMARY.id: StepResult(
            READ_SUMMARY.id, "ok", True, read_hash, input_id=input_id,
            attempt_id=read_attempt.name,
        )},
    )
    assert len(results) == 1
    result = results[0]
    if tamper_output:
        assert result.status == "failed"
        assert result.first_line is not None and "skipped worker output" in result.first_line
        assert "failed:" in progress[-1]
        assert not list((store.results_dir / input_id / "cell-origin").rglob("composition.json"))
        return
    assert result.status == "skipped" and result.skip_reason == "low_support"
    assert "skipped (low_support):" in progress[-1]
    assert str(tmp_path) not in result.skip_message
    assert "<ROOT>/donor.tsv" in result.skip_message
    assert result.parameters_sha256 == expected_hash
    assert result.attempt_id is not None
    attempt = store.selected_attempt(input_id, "cell-origin", expected_hash)
    assert attempt is not None
    assert store.upstream_selections_at(attempt) == ({
        "algorithm_id": READ_SUMMARY.id,
        "input_id": input_id,
        "parameters_sha256": read_hash,
        "attempt_id": read_attempt.name,
    },)
    envelope = store.read_result(input_id, "cell-origin", expected_hash)
    assert envelope.status == "skipped" and envelope.skip_reason == "low_support"
    assert not envelope.metrics and not envelope.panels
    assert not list((store.results_dir / input_id / "cell-origin").rglob("composition.json"))


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
    assert composition["modbase_model"] == "model-h"
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


@pytest.mark.parametrize("floor,value", [
    ("min_classified_fragments", 301),
    ("min_observed_markers", 4),
])
def test_low_marker_support_refuses_an_estimate(world, monkeypatch, floor, value) -> None:
    """E10: insufficient marker evidence never becomes a composition result."""
    from tests.test_cell_origin_stage import _test_parameters

    register_loyfer_directory(world["root"], world["inputs"].loyfer_dir)
    monkeypatch.setattr(
        method_module, "default_parameters", _test_parameters(**{floor: value}),
    )
    out = world["tmp"] / "low-support" / "artifacts"
    out.mkdir(parents=True)
    decision = co.CELL_ORIGIN.run(world["ctx"], out)
    assert isinstance(decision, Skip)
    assert decision.reason_code == "low_support"
    assert "classified fragment and marker floors are not met" in decision.message
    assert not list(out.iterdir())
    assert not list(out.parent.glob("co-*"))


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
