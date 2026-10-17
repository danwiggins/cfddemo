"""Synthetic marker registration, two-component fit, and real worker execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from traceback_lab.algorithms import cell_origin
from traceback_lab.algorithms.meth_tf import METH_TF, solve_two_component
from traceback_lab.assets import AssetError
from traceback_lab.cli import main as lab_main
from traceback_lab.meth_calls import DTYPE, HAS_M, SCHEMA
from traceback_lab.meth_tf_reference import add_reference, inspect_reference
from traceback_lab.registry import Ready, Skip, context_to_json, load_registry
from traceback_lab.store import LabStore, new_run_id

from .bam_fixtures import Read, context_for


def _reference(path: Path, reference_sha256: str, *, model: str = "synthetic-model") -> Path:
    path.write_text(json.dumps({
        "schema": "traceback.lab.meth-tf-reference.v1",
        "reference_sha256": reference_sha256,
        "modbase_model": model,
        "markers": [
            {"chrom": "chr1", "pos0": 101, "healthy": 0.25, "tumour": 0.75},
            {"chrom": "chr1", "pos0": 102, "healthy": 0.75, "tumour": 0.25},
        ],
    }))
    return path


def _calls(base: Path) -> None:
    directory = base / "mc" / "calls"
    directory.mkdir(parents=True)
    rows = []
    for read_row in range(8):
        for pos0, methylated in ((101, read_row < 3), (102, read_row < 5)):
            p_m = 230 if methylated else 10
            rows.append((read_row, 0, pos0, 0, p_m, 0, 255 - p_m, HAS_M))
    np.asarray(rows, dtype=DTYPE).tofile(directory / "calls-00000.bin")
    (directory / "meta.json").write_text(json.dumps({
        "schema": SCHEMA, "chunks": [len(rows)], "codes_sampled": ["C+m"],
        "records_with_mm": 8, "records_decoded": 8,
    }))
    np.asarray([], dtype="<u4").tofile(directory / "invalid_rows.bin")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = context_for(
        tmp_path, [("chr1", 10_000)],
        [Read(f"molecule-{index}", "chr1", 100, "A" * 100) for index in range(8)],
        eligible_reads=2_500_000,
    )
    _calls(tmp_path)
    ctx.extras["upstream"]["meth-calls"] = str(tmp_path / "mc")
    monkeypatch.setattr(cell_origin, "header_model", lambda *_: "synthetic-model")
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
    assert solve_two_component(
        healthy, tumour, np.asarray([3, 0]), np.asarray([8, 0]),
    ) == (None, None)


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
    ctx, _ = _fixture(tmp_path, monkeypatch)
    assert METH_TF.preflight(replace(ctx, eligible_reads=2_499_999)).reason_code == "depth"
    assert METH_TF.preflight(replace(ctx, reference_sha256="f" * 64)).reason_code == "reference"
    monkeypatch.setattr(cell_origin, "header_model", lambda *_: "different-model")
    assert METH_TF.preflight(ctx).reason_code == "missing_tags"


def test_registered_worker_produces_exploratory_fraction_and_pinned_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, registered = _fixture(tmp_path, monkeypatch)
    assert "meth-tf" in [algorithm.id for algorithm in load_registry()]
    assert isinstance(METH_TF.preflight(ctx), Ready)
    output = METH_TF.run(ctx, tmp_path / "out")
    values = _metrics(output)
    assert values["tumor_fraction"] == pytest.approx(0.25, abs=1e-12)
    assert values["identifiable"] is True
    assert values["markers_covered"] == 2 and values["cpg_calls_used"] == 16
    assert values["fit_rms"] == pytest.approx(0, abs=1e-12)
    receipt = json.loads(output.artifacts["meth_tf_fit.json"])
    assert receipt["asset_sha256"] == registered.sha256
    assert receipt["reference_sha256"] == ctx.reference_sha256
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
