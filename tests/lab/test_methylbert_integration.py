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
from traceback_lab.methylbert_bundle import Bundle
from traceback_lab.methylbert_child import _training_counts, _tumour_dmrs
from traceback_lab.methylbert_runtime import Aggregate, MIN_ELIGIBLE_READS, NUMERICAL_PACKAGES, RuntimeRefusal
from traceback_lab.registry import InputContext, Ready, Skip, execution_identity, load_registry
from traceback_lab.runner import RunTarget, _envelope
from traceback_lab.store import LabStore


def _context(tmp_path: Path, **extras: Any) -> InputContext:
    return InputContext(
        root=tmp_path, source_id="source", input_id="input", reference_id="hg38",
        reference_sha256="a" * 64, input_dir=tmp_path / "input",
        eligible_reads=MIN_ELIGIBLE_READS, extras=extras,
    )


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
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="d" * 64, methylbert_code_sha256="c" * 64,
                                         methylbert_min_classified_reads=100000))
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="d" * 64,
                                         methylbert_min_classified_reads=100000))
    assert original != identity(_context(tmp_path, methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
                                         methylbert_min_classified_reads=200000))
    monkeypatch.setattr(adapter, "header_model", lambda *_: "dorado-model-B")
    assert original != identity(first)
    runtime = {"python": "3.11", "packages": {"torch": "2.2"}, "cuda": None}
    assert original != identity(first)


def test_preflight_uses_measured_eligible_depth_not_classified_support(tmp_path, monkeypatch):
    ctx = _context(
        tmp_path, reference_fasta=str(tmp_path / "reference.fa"),
        methylbert_bundle_sha256="b" * 64, methylbert_code_sha256="c" * 64,
        methylbert_min_classified_reads=100000,
    )
    monkeypatch.setattr(adapter, "_ont_header", lambda *_: True)
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
            "child_sha256": "a" * 64, "numerical_runtime": {"packages": {"torch": "fixture"}},
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

    def fake_worker(argv, *, scratch, env, aggregate_path, model_id, eligible_reads):
        nonlocal private_path
        private_path = scratch
        assert argv[1] == "-I"
        assert env["HF_HUB_OFFLINE"] == "1"
        assert "PYTHONPATH" not in env and "HTTP_PROXY" not in env
        assert aggregate_path.parent == scratch
        assert model_id == "model-1"
        assert eligible_reads == MIN_ELIGIBLE_READS
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
    assert json.loads(result.artifacts["methylbert_provenance.json"])["child_sha256"] == "a" * 64
    assert json.loads(result.artifacts["methylbert_provenance.json"])["numerical_runtime"] == {"packages": {"torch": "fixture"}}

    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"packages": {"torch": "changed"}})
    with pytest.raises(RuntimeError, match="numerical runtime changed"):
        adapter.METHYLBERT.run(ctx, out)
    monkeypatch.setattr(adapter, "numerical_runtime_identity", lambda **_kwargs: {"packages": {"torch": "fixture"}})
    assert json.loads(result.artifacts["methylbert_provenance.json"])["min_classified_reads"] == 100000

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
