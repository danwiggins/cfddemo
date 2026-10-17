"""The MethylBERT Lab adapter fails closed without a checkpoint and GPU."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pysam
import pytest

import traceback_lab.algorithms.methylbert as adapter
import traceback_lab.controls as controls
from traceback_lab import cohorts
from traceback_lab.algorithms.read_summary import READ_SUMMARY
from traceback_lab.derived import DerivedInput, downsample
from traceback_lab.envelope import Metric, ResultEnvelope, provenance
from traceback_lab.methylbert_bundle import Bundle
from traceback_lab.methylbert_child import _training_counts, _tumour_dmrs
from traceback_lab.methylbert_runtime import Aggregate, MIN_ELIGIBLE_READS, NUMERICAL_PACKAGES, RuntimeRefusal
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip, execution_identity, load_registry
from traceback_lab.runner import RunTarget, StepResult, _envelope, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id
import traceback_lab.runner as lab_runner


def _context(tmp_path: Path, **extras: Any) -> InputContext:
    return InputContext(
        root=tmp_path, source_id="source", input_id="input", reference_id="hg38",
        reference_sha256="a" * 64, input_dir=tmp_path / "input",
        eligible_reads=MIN_ELIGIBLE_READS, extras=extras,
    )


def _publish_cohort_methylbert(
    store: LabStore, *, index: int, bundle_sha256: str, publish_adapter: bool = True,
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
    ctx = InputContext(
        root=store.root, source_id=source_id, input_id=input_id, reference_id="hg38",
        reference_sha256=reference_sha256, input_dir=input_dir, eligible_reads=MIN_ELIGIBLE_READS,
        extras={"methylbert_bundle_sha256": bundle_sha256,
                "methylbert_code_sha256": "c" * 64,
                "methylbert_min_classified_reads": 100_000},
    )
    parameters = adapter.METHYLBERT.identity_parameters(ctx)
    assets = adapter.METHYLBERT.identity_assets(ctx)
    tools = [tool.identity() for tool in adapter.METHYLBERT.tools]
    upstream = [{"algorithm_id": READ_SUMMARY.id, "parameters_sha256": read_hash}]
    methylbert_hash = execution_identity(
        adapter.METHYLBERT, reference_sha256=reference_sha256,
        asset_sha256s=assets, tool_pins=tools, parameters=parameters, upstream=upstream,
    )
    plan = {
        "schema": "traceback.lab.execution-plan.v1",
        "algorithm_id": adapter.METHYLBERT.id,
        "algorithm_version": adapter.METHYLBERT.version,
        "input_id": input_id, "reference_sha256": reference_sha256,
        "platform": "ont", "parameters": parameters, "tools": tools,
        "asset_sha256s": assets, "upstream": upstream,
    }
    if publish_adapter:
        store.publish_result(ResultEnvelope(
            algorithm_id="methylbert", algorithm_version=adapter.METHYLBERT.version, status="ok",
            provenance=provenance(tool="fixture", parameters_sha256=methylbert_hash,
                                  source_id=source_id, input_id=input_id, reference_id="hg38"),
            metrics=[Metric(metric_id="tumour_fraction", value=0.25)],
        ), input_id=input_id, execution_plan=plan, upstream_selections=[{
            "algorithm_id": READ_SUMMARY.id, "input_id": input_id,
            "parameters_sha256": read_hash, "attempt_id": read_attempt.name,
        }])
    return source_id, methylbert_hash


def test_cohort_reads_recorded_methylbert_identity_without_live_runtime(tmp_path, monkeypatch):
    store = LabStore(tmp_path)
    monkeypatch.setattr(cohorts, "load_registry", lambda: (READ_SUMMARY, adapter.METHYLBERT))
    monkeypatch.setattr(adapter, "header_model", lambda *_args: "dorado-model")
    runtime = {"name": "A"}
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"fixture_runtime": runtime["name"]})
    first, first_hash = _publish_cohort_methylbert(store, index=1, bundle_sha256="d" * 64)
    runtime["name"] = "B"
    second, second_hash = _publish_cohort_methylbert(store, index=2, bundle_sha256="d" * 64)
    runtime["name"] = "A"
    third, third_hash = _publish_cohort_methylbert(store, index=3, bundle_sha256="e" * 64)
    fourth, fourth_hash = _publish_cohort_methylbert(store, index=4, bundle_sha256="d" * 64)
    assert first_hash != second_hash
    assert first_hash != third_hash
    assert first_hash == fourth_hash

    def no_live_probe(*_args, **_kwargs):
        pytest.fail("cohort reading must use the recorded runtime, not probe this host")

    monkeypatch.setattr(adapter.METHYLBERT, "identity_parameters", no_live_probe)
    monkeypatch.setattr(adapter, "numerical_runtime_identity", no_live_probe)
    member = cohorts.member_from_store(store, first)
    selected = {row.algorithm_id: row for row in member.results}
    assert selected["methylbert"].parameters_sha256 == first_hash
    cohorts.add_sample(store, "model-cohort", first)
    cohorts.add_sample(store, "model-cohort", second)
    cohorts.add_sample(store, "model-cohort", third)
    cohorts.add_sample(store, "model-cohort", fourth)
    generation = cohorts.build_cohort(store, "model-cohort")
    by_source = {row.source_id: row for row in generation.members}
    first_result = next(row for row in by_source[first].results if row.algorithm_id == "methylbert")
    second_result = next(row for row in by_source[second].results if row.algorithm_id == "methylbert")
    third_result = next(row for row in by_source[third].results if row.algorithm_id == "methylbert")
    fourth_result = next(row for row in by_source[fourth].results if row.algorithm_id == "methylbert")
    assert first_result.cohort_compat_key != second_result.cohort_compat_key
    assert first_result.cohort_compat_key != third_result.cohort_compat_key
    assert first_result.cohort_compat_key == fourth_result.cohort_compat_key
    projected = cohorts.project_sample_metric(
        store, "model-cohort", target_source_id=first,
        algorithm_id="methylbert", metric_id="tumour_fraction",
    )
    assert projected is not None and projected.percentile_n == 1
    assert projected.exclusions == {"self": 1, "incompatible": 2}


def test_runner_plan_persists_methylbert_identity_for_cohort_read(tmp_path, monkeypatch):
    store = LabStore(tmp_path)
    monkeypatch.setattr(adapter, "header_model", lambda *_args: "dorado-model")
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"fixture_runtime": "A"})
    source_id, expected_hash = _publish_cohort_methylbert(
        store, index=9, bundle_sha256="d" * 64, publish_adapter=False,
    )
    input_id, reference_sha256 = f"{109:064x}", "a" * 64
    read_hash = execution_identity(READ_SUMMARY, reference_sha256=reference_sha256)
    read_attempt = store.selected_attempt(input_id, READ_SUMMARY.id, read_hash)
    assert read_attempt is not None
    target = RunTarget(
        input_id=input_id, source_id=source_id, input_dir=store.inputs_dir / input_id,
        reference_id="hg38", reference_sha256=reference_sha256,
        eligible_reads=MIN_ELIGIBLE_READS, extras={
            "methylbert_bundle_sha256": "d" * 64,
            "methylbert_code_sha256": "c" * 64,
            "methylbert_min_classified_reads": 100_000,
        },
    )
    monkeypatch.setattr(adapter.METHYLBERT, "preflight", lambda _ctx: Ready())
    ran = []

    def fake_pipeline(stages, **_kwargs):
        request = json.loads(Path(stages[0].argv[-1]).read_bytes())
        scratch = Path(request["out_dir"])
        (scratch / "artifacts").mkdir(exist_ok=True)
        (scratch / "output.json").write_text(json.dumps({
            "metrics": [{"metric_id": "tumour_fraction", "value": 0.25}], "panels": [],
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
        [PlanItem(adapter.METHYLBERT, False)], {"methylbert": "methylbert:METHYLBERT"},
        run_id=run_id, scrubber=Scrubber(root=store.root),
        upstream_results={READ_SUMMARY.id: StepResult(
            READ_SUMMARY.id, "ok", True, read_hash, input_id=input_id,
            attempt_id=read_attempt.name,
        )},
    )
    assert len(ran) == 1 and len(results) == 1
    assert results[0].status == "ok" and results[0].parameters_sha256 == expected_hash
    attempt = store.selected_attempt(input_id, "methylbert", expected_hash)
    assert attempt is not None and attempt.name == results[0].attempt_id
    plan = json.loads((attempt / "execution_plan.json").read_bytes())
    assert plan["platform"] == "ont"
    assert plan["parameters"]["numerical_runtime"] == {"fixture_runtime": "A"}
    assert plan["asset_sha256s"] == ["d" * 64, "c" * 64]
    assert plan["tools"] == [tool.identity() for tool in adapter.METHYLBERT.tools]
    assert plan["upstream"] == [{"algorithm_id": READ_SUMMARY.id, "parameters_sha256": read_hash}]
    assert store.upstream_selections_at(attempt) == ({
        "algorithm_id": READ_SUMMARY.id, "input_id": input_id,
        "parameters_sha256": read_hash, "attempt_id": read_attempt.name,
    },)

    def no_live_probe(*_args, **_kwargs):
        pytest.fail("cohort reading must use the published runtime plan")

    monkeypatch.setattr(adapter.METHYLBERT, "identity_parameters", no_live_probe)
    monkeypatch.setattr(adapter, "numerical_runtime_identity", no_live_probe)
    monkeypatch.setattr(cohorts, "load_registry", lambda: (READ_SUMMARY, adapter.METHYLBERT))
    member = cohorts.member_from_store(store, source_id)
    selected = {row.algorithm_id: row for row in member.results}
    assert selected["methylbert"].parameters_sha256 == expected_hash
    assert selected["methylbert"].metrics["tumour_fraction"] == 0.25


def test_derived_plan_keeps_model_pins_and_absolute_support_floor(tmp_path, monkeypatch):
    spec = downsample(0.5)
    parent = RunTarget(
        input_id="a" * 64, source_id="b" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64,
        eligible_reads=6_000_000,
        extras={"methylbert_bundle_sha256": "d" * 64, "methylbert_code_sha256": "e" * 64,
                "methylbert_min_classified_reads": 100_000, "upstream": {"old": "parent"}},
    )
    derived = DerivedInput(spec, spec.input_id(parent.input_id), parent.input_id, tmp_path / "derived")
    chosen = SimpleNamespace(algorithm=adapter.METHYLBERT)
    monkeypatch.setattr(controls, "derived_plan_items", lambda _items, _transform: [chosen])
    monkeypatch.setattr(controls, "materialize", lambda *_args, **_kwargs: {spec.label(): derived})
    monkeypatch.setattr(controls, "record_derived", lambda *_args: None)
    monkeypatch.setattr(controls.ReadSummary, "open", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(controls, "derived_mask", lambda *_args: None)
    monkeypatch.setattr(controls, "depth_stamp", lambda *_args: {
        "eligible_reads": 3_000_000, "approx_depth_x": 0.5, "mean_span_bp": 150.0,
    })
    seen = []

    def run_plan(_store, _lock, _log, target, items, _entries, **kwargs):
        assert items == [chosen]
        seen.append((target, kwargs["upstream_results"]))
        return []

    monkeypatch.setattr(controls, "run_plan", run_plan)
    summary = SimpleNamespace(artifacts_dir=str(tmp_path))
    results = controls.run_derived(
        SimpleNamespace(), None, None, parent, [spec], [chosen], {}, run_id="test",
        read_summary=summary, timeout_s=1,
        scrubber=SimpleNamespace(), parent_upstream={"read-summary": "current"},
    )
    assert len(results) == 1 and len(seen) == 1
    target, upstream = seen[0]
    assert target.derived == spec.mask_spec()
    assert target.eligible_reads == 3_000_000
    assert target.extras["methylbert_bundle_sha256"] == "d" * 64
    assert target.extras["methylbert_code_sha256"] == "e" * 64
    assert target.extras["methylbert_min_classified_reads"] == 100_000
    assert "upstream" not in target.extras
    assert upstream == {"read-summary": summary}


def test_registry_and_absent_checkpoint_skip_without_gpu_probe(tmp_path, monkeypatch):
    assert "methylbert" in {item.id for item in load_registry()}
    monkeypatch.setattr(adapter, "_ont_header", lambda *_: pytest.fail("no BAM read before bundle selection"))
    assert adapter.METHYLBERT.preflight(_context(tmp_path)) == Skip(
        "missing_asset", "supply a trusted registered bundle and installed-code SHA-256",
    )
    shallow = _context(tmp_path)
    object.__setattr__(shallow, "eligible_reads", MIN_ELIGIBLE_READS - 1)
    assert adapter.METHYLBERT.preflight(shallow).reason_code == "depth"


@pytest.mark.parametrize("reason", ["input_kind", "unqualified_support", "missing_gpu", "model_not_ready"])
def test_methylbert_specific_skips_publish_and_select(tmp_path, reason):
    target = RunTarget(
        input_id="1" * 64, source_id="2" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="3" * 64,
    )
    identity = "4" * 64
    envelope = _envelope(adapter.METHYLBERT, target, identity, status="skipped", skip=Skip(reason, "fixture prerequisite"))
    store = LabStore(tmp_path)
    store.publish_result(envelope, input_id=target.input_id)
    selected = store.read_result(target.input_id, adapter.METHYLBERT.id, identity)
    assert selected is not None and selected.status == "skipped"
    assert selected.skip_reason == reason and selected.metrics == []


def test_bundle_code_and_basecaller_each_change_execution_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model-A")
    runtime = {"python": "3.11", "packages": {"torch": "2.1"}, "cuda": None}
    probes = []

    def numerical_runtime(*, probe_gpu, isolated_gpu):
        assert isolated_gpu is True
        probes.append(probe_gpu)
        return runtime

    monkeypatch.setattr(adapter, "numerical_runtime_identity", numerical_runtime)
    first = _context(tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
                     methylbert_min_classified_reads=100000)

    def identity(ctx):
        algorithm = adapter.METHYLBERT
        return execution_identity(
            algorithm, reference_sha256=ctx.reference_sha256,
            asset_sha256s=algorithm.identity_assets(ctx), parameters=algorithm.identity_parameters(ctx),
        )

    original = identity(first)
    identity(_context(tmp_path))
    assert probes[:2] == [True, False]
    code = adapter.METHYLBERT.identity_parameters(first)["execution_code_sha256s"]
    assert set(code) == set(adapter._EXECUTION_CODE_FILES)
    assert all(len(digest) == 64 for digest in code.values())
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="d" * 64, methylbert_code_sha256="c" * 64,
                                         methylbert_min_classified_reads=100000))
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="d" * 64,
                                         methylbert_min_classified_reads=100000))
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
                                         methylbert_min_classified_reads=200000))
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model-B")
    assert original != identity(first)
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model-A")
    runtime = {"python": "3.11", "packages": {"torch": "2.2"}, "cuda": None}
    assert original != identity(first)


def test_failed_isolated_probe_becomes_a_planned_methylbert_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model")

    def failed_probe(**_kwargs):
        raise RuntimeRefusal("isolated numerical runtime probe failed")

    monkeypatch.setattr(adapter, "numerical_runtime_identity", failed_probe)
    ctx = _context(
        tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
        methylbert_min_classified_reads=100000,
    )
    planned = adapter.METHYLBERT.identity_parameters(ctx)
    assert planned["numerical_runtime"] == {"probe_error": "isolated_numerical_runtime_unavailable"}
    ctx.extras["planned_parameters"] = planned
    assert adapter.METHYLBERT.preflight(ctx) == Skip(
        "model_not_ready", "the isolated numerical runtime probe failed during planning",
    )


def test_preflight_uses_measured_eligible_depth_not_classified_support(tmp_path, monkeypatch):
    ctx = _context(
        tmp_path, reference_fasta=str(tmp_path / "reference.fa"),
        methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
        methylbert_min_classified_reads=100000,
    )
    monkeypatch.setattr(adapter, "_ont_header", lambda *_: True)
    monkeypatch.setattr(adapter, "_has_canonical_contigs", lambda *_: True)
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model")
    monkeypatch.setattr(adapter, "_eligible_tagged", lambda *_: (MIN_ELIGIBLE_READS, 100000))
    monkeypatch.setattr(adapter, "inspect_methylbert_bundle", lambda *_: SimpleNamespace(file=tmp_path / "model.tar"))
    bundle = Bundle("model-1", "f" * 40, "fixture", "test", "a" * 64, 3, 150, {})
    monkeypatch.setattr(adapter, "verify_bundle", lambda *_: bundle)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    observed = {}

    def ready(model, **kwargs):
        observed.update(kwargs)
        assert model is bundle

    monkeypatch.setattr(adapter, "require_ready", ready)
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {
        "python": "3.11", "packages": {name: "fixture" for name in NUMERICAL_PACKAGES},
        "cuda": {"available": True},
    })
    without_floor = _context(
        tmp_path, reference_fasta=str(tmp_path / "reference.fa"),
        methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
    )
    assert adapter.METHYLBERT.preflight(without_floor).reason_code == "unqualified_support"
    assert isinstance(adapter.METHYLBERT.preflight(ctx), Ready)
    assert observed["eligible_reads"] == MIN_ELIGIBLE_READS
    assert observed["reference_sha256"] == "a" * 64
    assert observed["platform"] == "ont"
    assert observed["expected_code_sha256"] == "c" * 64


@pytest.mark.parametrize("platform", ["ONT", "OXFORD_NANOPORE"])
def test_ont_header_uses_the_shared_platform_vocabulary(tmp_path, platform):
    source = tmp_path / "ont.bam"
    with pysam.AlignmentFile(str(source), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}],
        "RG": [{"ID": "reads", "PL": platform}],
    }):
        pass
    assert adapter._ont_header(source)


def test_eligible_bam_excludes_duplicates_low_mapq_and_noncanonical(tmp_path):
    source = tmp_path / "source.bam"
    header = {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [
        {"SN": "chr1", "LN": 1000}, {"SN": "chrUn", "LN": 1000},
    ]}
    with pysam.AlignmentFile(str(source), "wb", header=header) as output:
        for name, flag, mapq, rid in (
            ("keep", 0, 30, 0), ("duplicate", 0x400, 30, 0),
            ("low", 0, 19, 0), ("alt", 0, 30, 1),
        ):
            row = pysam.AlignedSegment()
            row.query_name = name
            row.query_sequence = "ACGT"
            row.flag = flag
            row.reference_id = rid
            row.reference_start = 100
            row.mapping_quality = mapq
            row.cigarstring = "4M"
            row.query_qualities = pysam.qualitystring_to_array("IIII")
            output.write(row)
    target = tmp_path / "eligible.bam"
    assert adapter._write_eligible_bam(source, target, _context(tmp_path)) == 1
    with pysam.AlignmentFile(str(target), "rb") as bam:
        assert [row.query_name for row in bam.fetch(until_eof=True)] == ["keep"]
    assert Path(str(target) + ".bai").exists()


def test_eligible_bam_refuses_a_reference_without_chr_prefixed_nuclear_contigs(tmp_path):
    source = tmp_path / "source.bam"
    with pysam.AlignmentFile(str(source), "wb", header={
        "HD": {"VN": "1.6"},
        "SQ": [{"SN": "1", "LN": 1000}, {"SN": "MT", "LN": 1000}],
    }):
        pass
    target = tmp_path / "eligible.bam"
    with pytest.raises(ValueError, match="chr-prefixed canonical nuclear contigs"):
        adapter._write_eligible_bam(source, target, _context(tmp_path))
    assert not target.exists()


def test_unprefixed_reference_skips_before_model_loading(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / "aligned.bam"
    with pysam.AlignmentFile(str(source), "wb", header={
        "HD": {"VN": "1.6"}, "SQ": [{"SN": "1", "LN": 1000}],
        "RG": [{"ID": "reads", "PL": "ONT"}],
    }):
        pass
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model")
    monkeypatch.setattr(adapter, "inspect_methylbert_bundle", lambda *_: pytest.fail("no model load"))
    ctx = _context(
        tmp_path, reference_fasta=str(tmp_path / "reference.fa"),
        methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
        methylbert_min_classified_reads=100000,
    )
    assert adapter.METHYLBERT.preflight(ctx) == Skip(
        "reference", "MethylBERT needs chr-prefixed canonical nuclear contigs",
    )


def test_private_worker_lifecycle_publishes_only_aggregate(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\nACGT\n", encoding="ascii")
    out = tmp_path / "attempt" / "artifacts"
    out.mkdir(parents=True)
    ctx = _context(
        tmp_path, reference_fasta=str(fasta),
        methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
        methylbert_min_classified_reads=100000, planned_parameters={
            "basecall_model": "dorado-model",
            "execution_code_sha256s": {name: "a" * 64 for name in adapter._EXECUTION_CODE_FILES},
            "numerical_runtime": {"packages": {"torch": "fixture"}},
        },
    )
    monkeypatch.setattr(adapter.METHYLBERT, "preflight", lambda *_: Ready())
    monkeypatch.setattr(adapter, "_digest", lambda *_: "a" * 64)
    monkeypatch.setattr(adapter, "_write_eligible_bam", lambda *_: MIN_ELIGIBLE_READS)
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model")
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"packages": {"torch": "fixture"}})

    def private_copy(_store, _sha, destination):
        destination.write_bytes(b"private model")
        return destination

    bundle = Bundle("model-1", "f" * 40, "fixture", "test", "a" * 64, 3, 150, {})
    monkeypatch.setattr(adapter, "copy_methylbert_bundle", private_copy)
    monkeypatch.setattr(adapter, "unpack_bundle", lambda _archive, _destination: bundle)
    private_path = None

    def fake_worker(argv, *, scratch, env, aggregate_path, model_id, eligible_reads, min_classified_reads):
        nonlocal private_path
        private_path = scratch
        assert argv[1] == "-I"
        assert env["HF_HUB_OFFLINE"] == "1"
        assert "PYTHONPATH" not in env and "HTTP_PROXY" not in env
        assert aggregate_path.parent == scratch
        assert model_id == "model-1"
        assert eligible_reads == MIN_ELIGIBLE_READS
        assert min_classified_reads == 100000
        assert (scratch / "request.json").exists()
        assert json.loads((scratch / "request.json").read_text())["numerical_runtime"] == {"packages": {"torch": "fixture"}}
        return Aggregate("model-1", 0.25, 0.75, 500000)

    monkeypatch.setattr(adapter, "run_worker", fake_worker)
    result = adapter.METHYLBERT.run(ctx, out)
    assert private_path is not None and not private_path.exists()
    assert [row["metric_id"] for row in result.metrics] == ["tumour_fraction", "normal_fraction", "classified_reads"]
    assert result.metrics[0]["n"] == 500000
    assert result.metrics[2]["n"] == MIN_ELIGIBLE_READS
    assert set(result.artifacts) == {"methylbert_provenance.json"}
    assert json.loads(result.artifacts["methylbert_provenance.json"])["bundle_sha256"] == "b" * 64
    assert json.loads(result.artifacts["methylbert_provenance.json"])["execution_code_sha256s"] == {
        name: "a" * 64 for name in adapter._EXECUTION_CODE_FILES
    }
    assert json.loads(result.artifacts["methylbert_provenance.json"])["numerical_runtime"] == {"packages": {"torch": "fixture"}}

    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"packages": {"torch": "changed"}})
    with pytest.raises(RuntimeError, match="numerical runtime changed"):
        adapter.METHYLBERT.run(ctx, out)
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"packages": {"torch": "fixture"}})
    assert json.loads(result.artifacts["methylbert_provenance.json"])["min_classified_reads"] == 100000

    monkeypatch.setattr(adapter, "header_model", lambda *_: "changed-model")
    with pytest.raises(RuntimeError, match="basecall model changed"):
        adapter.METHYLBERT.run(ctx, out)
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model")

    def low_support(_argv, *, scratch, **_kwargs):
        return Aggregate("model-1", 0.25, 0.75, 99999)

    monkeypatch.setattr(adapter, "run_worker", low_support)
    with pytest.raises(RuntimeError, match="qualified floor"):
        adapter.METHYLBERT.run(ctx, out)

    def failing_worker(_argv, *, scratch, **_kwargs):
        nonlocal private_path
        private_path = scratch
        raise RuntimeRefusal("injected child failure")

    monkeypatch.setattr(adapter, "run_worker", failing_worker)
    with pytest.raises(RuntimeRefusal, match="injected child failure"):
        adapter.METHYLBERT.run(ctx, out)
    assert private_path is not None and not private_path.exists()


def test_training_margins_and_tumour_dmrs_are_checked_separately(tmp_path):
    table = tmp_path / "train_seq.csv"
    table.write_text("ctype\tdmr_label\nN\t0\nN\t2\nT\t2\n", encoding="utf-8")
    margins, training_labels = _training_counts(table)
    assert margins["N"] == pytest.approx(2 / 3)
    assert margins["T"] == pytest.approx(1 / 3)
    assert training_labels == {0, 2}
    dmrs = tmp_path / "dmrs.tsv"
    dmrs.write_text("chr\tstart\tend\tctype\tdmr_id\nchr1\t0\t100\tT\t0\nchr1\t100\t200\tT\t1\nchr1\t200\t300\tT\t2\n", encoding="utf-8")
    assert _tumour_dmrs(dmrs) == {0, 1, 2}
    dmrs.write_text("chr\tstart\tend\tctype\tdmr_id\nchr1\t0\t100\tN\t0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tumour-specific"):
        _tumour_dmrs(dmrs)
    table.write_text("ctype\tdmr_label\nN\t0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="both classes"):
        _training_counts(table)
