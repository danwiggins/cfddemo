"""Normal Lab worker acceptance on physically consistent paired-end BAMs."""

from __future__ import annotations

import json
from pathlib import Path

import pysam
import pytest

from traceback_lab.cohorts import member_from_store
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli
from traceback_runner.references import register_reference


def _paired_bam(path: Path, count: int = 120) -> None:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"},
              "SQ": [{"SN": "chr1", "LN": 50000}],
              "RG": [{"ID": "library-1", "PL": "ILLUMINA"}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for index in range(count):
            start = 100 + index * 250
            insert = 140 if index < 40 else 180
            mate_start = start + insert - 100
            for first in (True, False):
                read = pysam.AlignedSegment()
                read.query_name = f"template-{index:04d}"
                read.query_sequence = "A" * 100
                read.query_qualities = pysam.qualitystring_to_array("I" * 100)
                read.flag = 99 if first else 147
                read.reference_id = 0
                read.reference_start = start if first else mate_start
                read.mapping_quality = 60
                read.cigarstring = "100M"
                read.next_reference_id = 0
                read.next_reference_start = mate_start if first else start
                read.template_length = insert if first else -insert
                read.set_tag("RG", "library-1")
                bam.write(read)
    sorted_path = path.with_name(path.stem + ".sorted.bam")
    pysam.sort("-o", str(sorted_path), str(path))
    sorted_path.replace(path)


def _ont_bam(path: Path, count: int = 120) -> None:
    header = {"HD": {"VN": "1.6", "SO": "coordinate"},
              "SQ": [{"SN": "chr1", "LN": 50000}],
              "RG": [{"ID": "library-1", "PL": "ONT"}]}
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for index in range(count):
            span = 140 if index < 40 else 180
            read = pysam.AlignedSegment()
            read.query_name = f"ont-molecule-{index:04d}"
            read.query_sequence = "A" * span
            read.query_qualities = pysam.qualitystring_to_array("I" * span)
            read.flag = 0
            read.reference_id = 0
            read.reference_start = 100 + index * 250
            read.mapping_quality = 60
            read.cigarstring = f"{span}M"
            read.set_tag("RG", "library-1")
            bam.write(read)


def _run(path: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    code = signed_cli.main([
        "lab", "run", str(path), "--root", str(root), "--reference", "hg38",
        "--no-downsample", "--no-controls", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["data"]["counts"]["failed"] == 0
    return payload["data"]


def test_realistic_paired_bams_keep_platform_results_and_cohorts_separate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    reference = tmp_path / "reference.fa"
    reference.write_text(">chr1\n" + "A" * 50000 + "\n")
    pysam.faidx(str(reference))
    root = tmp_path / "root"
    register_reference(root, reference, "hg38")
    illumina_bam = tmp_path / "illumina.bam"
    ont_bam = tmp_path / "ont.bam"
    _paired_bam(illumina_bam)
    _ont_bam(ont_bam)

    illumina = _run(illumina_bam, root, capsys)
    ont = _run(ont_bam, root, capsys)
    assert illumina["sample"]["platform"] == "illumina"
    assert ont["sample"]["platform"] == "ont"
    illumina_steps = {step["algorithm_id"]: step for step in illumina["steps"]}
    ont_steps = {step["algorithm_id"]: step for step in ont["steps"]}
    for name in ("read-summary", "depth", "bam-qc", "short-read-fragment-size"):
        assert illumina_steps[name]["status"] == "ok"
    for name in ("read-summary", "depth", "bam-qc", "fragment-size"):
        assert ont_steps[name]["status"] == "ok"
    assert illumina_steps["fragment-size"]["status"] == "skipped"
    assert illumina_steps["fragment-size"]["skip_reason"] == "platform"
    assert ont_steps["fragment-size"]["status"] == "ok"
    assert ont_steps["short-read-fragment-size"]["status"] == "skipped"
    assert ont_steps["short-read-fragment-size"]["skip_reason"] == "platform"

    store = LabStore(root)
    short = next(item for item in member_from_store(store, illumina["sample"]["source_id"]).results
                 if item.algorithm_id == "short-read-fragment-size")
    selected = store.read_result(illumina["sample"]["input_id"], short.algorithm_id,
                                 short.parameters_sha256)
    assert selected is not None
    metrics = {metric.metric_id: metric.value for metric in selected.metrics}
    assert metrics["mode_bp"] == 180
    assert metrics["short_fraction_lt150"] == pytest.approx(40 / 120)
    assert {metric.metric_id: metric.n for metric in selected.metrics}["short_fraction_lt150"] == 120
    ont_fragment = next(item for item in member_from_store(store, ont["sample"]["source_id"]).results
                        if item.algorithm_id == "fragment-size")
    ont_selected = store.read_result(ont["sample"]["input_id"], ont_fragment.algorithm_id,
                                     ont_fragment.parameters_sha256)
    assert ont_selected is not None
    ont_metrics = {metric.metric_id: metric for metric in ont_selected.metrics}
    assert ont_metrics["mode_bp"].value == 180
    assert ont_metrics["mode_bp"].n == 120
    assert ont_metrics["short_fraction_lt150"].value == pytest.approx(40 / 120)
    assert ont_metrics["short_fraction_lt150"].n == 120
    illumina_depth = next(item for item in member_from_store(store, illumina["sample"]["source_id"]).results
                          if item.algorithm_id == "depth")
    ont_depth = next(item for item in member_from_store(store, ont["sample"]["source_id"]).results
                      if item.algorithm_id == "depth")
    assert illumina_depth.parameters_sha256 != ont_depth.parameters_sha256
    assert illumina_depth.cohort_compat_key != ont_depth.cohort_compat_key
