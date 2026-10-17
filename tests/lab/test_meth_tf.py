"""Synthetic marker registration, two-component fit, and real worker execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pysam
import pytest

from traceback_lab.algorithms import cell_origin
from traceback_lab.algorithms._meth import load_calls
from traceback_lab.algorithms.meth_calls import METH_CALLS
from traceback_lab.algorithms.meth_tf import (
    METH_TF,
    solve_two_component,
    tally_markers,
    weighted_reference_condition_number,
)
from traceback_lab.assets import AssetError
from traceback_lab.cli import main as lab_main
from traceback_lab.meth_tf_reference import CALL_DEFINITION, add_reference, inspect_reference
from traceback_lab.read_summary import SPLIT_SALT, ReadSummary, h64
from traceback_lab.registry import Ready, Skip, context_to_json, load_registry, plan
from traceback_lab.store import LabStore, new_run_id

from .bam_fixtures import Read, context_for


def _reference(
    path: Path, reference_sha256: str, *, model: str = "synthetic-model", third: bool = False,
) -> Path:
    markers = [
        {"chrom": "chr1", "pos0": 101, "healthy": 0.25, "tumour": 0.75},
        {"chrom": "chr1", "pos0": 103, "healthy": 0.75, "tumour": 0.25},
    ]
    if third:
        markers.append({"chrom": "chr1", "pos0": 105, "healthy": 0.5, "tumour": 0.5})
    path.write_text(json.dumps({
        "schema": "traceback.lab.meth-tf-reference.v1",
        "reference_sha256": reference_sha256,
        "modbase_model": model,
        "call_definition": CALL_DEFINITION,
        "markers": markers,
    }))
    return path


def _fixture(tmp_path: Path):
    reads = [
        Read(
            f"molecule-{index}", "chr1", 100, "ACGCGCGAAAA", mm="C+m?,0,0;",
            ml=[230 if index < 3 else 10, 230 if index < 5 else 10],
        )
        for index in range(8)
    ]
    ctx = context_for(
        tmp_path, [("chr1", 10_000)],
        reads, eligible_reads=2_500_000, modbase_model="synthetic-model",
    )
    METH_CALLS.run(ctx, tmp_path / "mc")
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    source = _reference(tmp_path / "marker-source.json", ctx.reference_sha256)
    store = LabStore(tmp_path)
    with store.writer("synthetic meth-TF reference", new_run_id()):
        registered = add_reference(store, source, source_label="synthetic marker fixture", terms="unknown")
    ctx.extras["planned_asset_sha256s"] = [registered.sha256]
    return ctx, registered


def _metrics(output) -> dict[str, object]:
    return {item["metric_id"]: item["value"] for item in output.metrics}


def test_analytic_nnls_recovers_known_mixture_and_rejects_rank_deficiency() -> None:
    healthy = np.asarray([0.25, 0.75])
    tumour = np.asarray([0.75, 0.25])
    fraction, residual = solve_two_component(
        healthy, tumour, np.asarray([3, 5]), np.asarray([8, 8]),
    )
    assert fraction == pytest.approx(0.25, abs=1e-12)
    assert residual == pytest.approx(0, abs=1e-12)
    assert solve_two_component(
        healthy, healthy, np.asarray([3, 5]), np.asarray([8, 8]),
    ) == (None, None)
    assert weighted_reference_condition_number(healthy, healthy, np.asarray([8, 8])) is None
    assert solve_two_component(
        healthy, tumour, np.asarray([3, 0]), np.asarray([8, 0]),
    ) == (None, None)
    boundary, _ = solve_two_component(
        healthy, tumour, np.asarray([0, 7]), np.asarray([3, 7]),
    )
    assert boundary == pytest.approx(0)
    boundary, _ = solve_two_component(
        healthy, tumour, np.asarray([3, 0]), np.asarray([3, 7]),
    )
    assert boundary == pytest.approx(1)
    nearly_same = np.asarray([0.250001, 0.750001])
    condition = weighted_reference_condition_number(
        healthy, nearly_same, np.asarray([3, 7]),
    )
    assert condition is not None and condition > 1_000_000
    # Full rank can still be poorly conditioned; the diagnostic reports this
    # without turning a numerical threshold into a biological cutoff.
    fraction, _ = solve_two_component(
        healthy, nearly_same, np.asarray([1, 5]), np.asarray([3, 7]),
    )
    assert fraction is not None


def test_public_asset_add_and_inspect_pin_marker_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _reference(tmp_path / "source.json", "a" * 64)
    code = lab_main([
        "asset", "add", "meth-tf-reference", "--file", str(source),
        "--source-label", "synthetic marker fixture", "--terms", "unknown",
        "--root", str(tmp_path), "--json",
    ])
    added = json.loads(capsys.readouterr().out)
    assert code == 0 and added["data"]["asset"]["kind"] == "meth-tf-reference"
    code = lab_main(["asset", "inspect", "meth-tf-reference", "--root", str(tmp_path), "--json"])
    inspected = json.loads(capsys.readouterr().out)
    assert code == 0 and inspected["data"]["asset"] == added["data"]["asset"]


def test_missing_or_changed_reference_refuses_without_using_unpinned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context_for(
        tmp_path, [("chr1", 10_000)], [Read("one", "chr1", 100, "A" * 100)],
        eligible_reads=2_500_000,
    )
    assert isinstance(METH_TF.preflight(ctx), Skip)
    assert METH_TF.preflight(ctx).reason_code == "missing_asset"
    source = _reference(tmp_path / "reference-source.json", ctx.reference_sha256)
    store = LabStore(tmp_path)
    with store.writer("synthetic meth-TF reference", new_run_id()):
        record = add_reference(store, source, source_label="synthetic marker fixture", terms="unknown")
    assert inspect_reference(store, expected_sha256=record.sha256).reference.modbase_model == "synthetic-model"
    assert record.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(AssetError, match="different"):
        other = _reference(tmp_path / "other-source.json", ctx.reference_sha256, model="other-model")
        with store.writer("replacement refused", new_run_id()):
            add_reference(store, other, source_label="other fixture", terms="unknown")
    record.file.write_bytes(record.file.read_bytes() + b" ")
    with pytest.raises(AssetError, match="SHA-256"):
        inspect_reference(store)
    assert METH_TF.asset_sha256s(tmp_path) == []
    assert METH_TF.preflight(ctx).reason_code == "missing_asset"


def test_reference_model_genome_and_depth_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _ = _fixture(tmp_path)
    assert METH_TF.preflight(replace(ctx, eligible_reads=2_499_999)).reason_code == "depth"
    assert METH_TF.preflight(replace(ctx, reference_sha256="f" * 64)).reason_code == "reference"
    monkeypatch.setattr(cell_origin, "header_model", lambda *_: "different-model")
    assert METH_TF.preflight(ctx).reason_code == "missing_tags"


def test_registered_worker_produces_exploratory_fraction_and_pinned_attempt(tmp_path: Path) -> None:
    ctx, registered = _fixture(tmp_path)
    assert "meth-tf" in [algorithm.id for algorithm in load_registry()]
    assert METH_TF.cost == "slow"
    assert "meth-tf" not in [item.algorithm.id for item in plan(load_registry(), profile="fast")]
    assert isinstance(METH_TF.preflight(ctx), Ready)
    output = METH_TF.run(ctx, tmp_path / "out")
    values = _metrics(output)
    assert values["tumor_fraction"] == pytest.approx(0.25, abs=1e-12)
    assert values["identifiable"] is True
    assert values["markers_covered"] == 2 and values["cpg_calls_used"] == 16
    assert values["fit_rms"] is None  # exactly two observed markers have no residual degrees of freedom
    assert values["reference_condition_number"] == pytest.approx(2)
    receipt = json.loads(output.artifacts["meth_tf_fit.json"])
    assert receipt["asset_sha256"] == registered.sha256
    assert receipt["reference_sha256"] == ctx.reference_sha256
    assert receipt["reference_condition_number"] == pytest.approx(2)
    ctx.extras["planned_asset_sha256s"] = ["0" * 64]
    with pytest.raises(ValueError, match="execution planning"):
        METH_TF.run(ctx, tmp_path / "out")
    ctx.extras["planned_asset_sha256s"] = [registered.sha256]

    out = tmp_path / "worker-out"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "entry": "meth_tf:METH_TF", "context": context_to_json(ctx),
        "out_dir": str(out),
    }))
    completed = subprocess.run(
        [sys.executable, "-m", "traceback_lab.worker", str(request)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    measured = json.loads((out / "output.json").read_text())
    assert {item["metric_id"]: item["value"] for item in measured["metrics"]}["tumor_fraction"] == pytest.approx(0.25)

    changed_input = tmp_path / "changed-model"
    changed_input.mkdir()
    with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "rb") as bam:
        header = bam.header.to_dict()
    for read_group in header["RG"]:
        read_group["DS"] = read_group["DS"].replace("synthetic-model", "changed-model")
    with pysam.AlignmentFile(str(changed_input / "aligned.bam"), "wb", header=header):
        pass
    changed_request = tmp_path / "changed-model-request.json"
    changed_request.write_text(json.dumps({
        "entry": "meth_tf:METH_TF", "context": context_to_json(replace(ctx, input_dir=changed_input)),
        "out_dir": str(tmp_path / "changed-model-output"),
    }))
    changed = subprocess.run(
        [sys.executable, "-m", "traceback_lab.worker", str(changed_request)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert changed.returncode != 0 and "modified-base model changed" in changed.stderr


def test_real_modification_bam_flows_through_call_store_worker_and_derived_mask(tmp_path: Path) -> None:
    # Eight actual records, with a synthetic depth override solely to exercise
    # the 2.5M gate's other branches; this is not a depth validation receipt.
    a_names = [f"a-{index}" for index in range(100) if h64(SPLIT_SALT, f"a-{index}") < 2**63][:4]
    b_names = [f"b-{index}" for index in range(100) if h64(SPLIT_SALT, f"b-{index}") >= 2**63][:4]
    assert len(a_names) == len(b_names) == 4
    reads = [
        Read(
            name, "chr1", 100, "ACGCGCGAAAA", mm="C+m?,0,0;",
            ml=[230 if index < (1 if half == "A" else 2) else 10,
                230 if index < (3 if half == "A" else 2) else 10],
        )
        for half, names in (("A", a_names), ("B", b_names))
        for index, name in enumerate(names)
    ]
    ctx = context_for(
        tmp_path, [("chr1", 10_000)], reads,
        eligible_reads=2_500_000, modbase_model="synthetic-model",
    )
    reference_source = _reference(tmp_path / "marker-source.json", ctx.reference_sha256, third=True)
    store = LabStore(tmp_path)
    with store.writer("synthetic meth-TF reference", new_run_id()):
        reference = add_reference(store, reference_source, source_label="synthetic CpG panel", terms="unknown")
    ctx.extras["planned_asset_sha256s"] = [reference.sha256]
    assert isinstance(METH_CALLS.preflight(ctx), Ready)
    METH_CALLS.run(ctx, tmp_path / "decoded")
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "decoded")
    assert cell_origin.header_model(ctx.input_dir / "aligned.bam") == "synthetic-model"
    assert isinstance(METH_TF.preflight(ctx), Ready)
    summary = ReadSummary.open(tmp_path / "read-summary" / "columns")
    calls, mask = load_calls(ctx)
    methylated, counts = tally_markers(calls, mask, summary, reference)
    assert methylated.tolist() == [3, 5, 0]
    assert counts.tolist() == [8, 8, 0]

    request = tmp_path / "real-request.json"
    out = tmp_path / "real-worker-out"
    request.write_text(json.dumps({
        "entry": "meth_tf:METH_TF", "context": context_to_json(ctx), "out_dir": str(out),
    }))
    completed = subprocess.run(
        [sys.executable, "-m", "traceback_lab.worker", str(request)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    measured = json.loads((out / "output.json").read_text())
    assert {item["metric_id"]: item["value"] for item in measured["metrics"]}["tumor_fraction"] == pytest.approx(0.25)
    fit = json.loads((out / "artifacts" / "meth_tf_fit.json").read_text())
    assert fit["markers_registered"] == 3 and fit["markers_covered"] == 2
    assert fit["cpg_calls_used"] == 16 and fit["call_definition"] == CALL_DEFINITION
    assert fit["fit_rms"] is None

    derived_ctx = replace(ctx, derived={"transform": "split", "half": "A"})
    derived_calls, derived_mask = load_calls(derived_ctx)
    methylated, counts = tally_markers(derived_calls, derived_mask, summary, reference)
    assert methylated.tolist() == [1, 3, 0]
    assert counts.tolist() == [4, 4, 0]
    assert isinstance(METH_TF.preflight(derived_ctx), Ready)
    derived_output = METH_TF.run(derived_ctx, tmp_path / "derived-out")
    assert _metrics(derived_output)["tumor_fraction"] == pytest.approx(0)
