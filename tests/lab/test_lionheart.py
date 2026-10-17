"""Synthetic verification of the actual BAM-to-LIONHEART feature-extraction adapter."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from traceback_lab.algorithms import lionheart
from traceback_lab.registry import InputContext, Ready, execution_identity
from traceback_lab.interpretation.engine import load_library
from traceback_lab.store import LabStore
from traceback_lab.web.server import RunningLabServer
from tests.lab.test_landing import _dom, needs_chrome

ID12 = "3f9a1c2b7e04"


def _setup(tmp_path: Path, monkeypatch) -> tuple[InputContext, Path]:
    root = tmp_path / "root"
    input_dir = root / "lab" / "inputs" / ("b" * 64)
    input_dir.mkdir(parents=True)
    (input_dir / "aligned.bam").write_bytes(b"BAM from selected input")
    directory = root / "lab" / "assets" / "lionheart" / "inference_resources_v003"
    for relative in lionheart.REQUIRED_FILES:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic resource")
    for relative in lionheart.REQUIRED_DIRS:
        path = directory / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "fixture.bin").write_bytes(b"synthetic mask")
    for assay in ("ATAC", "DNase"):
        (directory / f"{assay}.idx_to_cell_type.csv").write_text(
            "idx,cell_type\n" + "".join(f"{index},{assay} type {index}\n" for index in range(449))
        )
    archive = directory.parent / "inference_resources_v003.tar.gz"
    archive.write_bytes(b"synthetic archive")
    monkeypatch.setattr(lionheart, "RESOURCE_ARCHIVE_MD5", hashlib.md5(archive.read_bytes()).hexdigest())

    executable = tmp_path / "lionheart"
    executable.write_text(f"""#!{sys.executable}
import argparse
import pathlib
import sys
import numpy as np
if sys.argv[1:] == ['--version']:
    print('lionheart, version 2.2.0')
    sys.exit(0)
parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('--bam_file')
parser.add_argument('--resources_dir')
parser.add_argument('--out_dir')
parser.add_argument('--mosdepth_path')
parser.add_argument('--n_jobs')
parser.add_argument('--single_end_mode', action='store_true')
args = parser.parse_args()
assert args.command == 'extract_features' and args.single_end_mode
assert pathlib.Path(args.bam_file).read_bytes() == b'BAM from selected input'
assert pathlib.Path(args.resources_dir).is_dir()
assert pathlib.Path(args.mosdepth_path).is_file()
output = pathlib.Path(args.out_dir) / 'dataset'
output.mkdir(parents=True)
matrix = np.zeros((10, 898))
matrix[0] = np.linspace(-1, 1, 898)
np.save(output / 'feature_dataset.npy', matrix)
""")
    executable.chmod(0o755)
    mosdepth = tmp_path / "mosdepth"
    mosdepth.write_text("#!/bin/sh\necho 'modified mosdepth --single-end-length'\n")
    mosdepth.chmod(0o755)
    monkeypatch.setenv("TRACEBACK_LIONHEART_BIN", str(executable))
    monkeypatch.setenv("TRACEBACK_LIONHEART_MOSDEPTH_BIN", str(mosdepth))
    monkeypatch.setenv("TRACEBACK_LIONHEART_RESOURCES", str(directory))
    monkeypatch.setenv("TRACEBACK_LIONHEART_ARCHIVE", str(archive))
    monkeypatch.setenv("TRACEBACK_LIONHEART_ENABLE_ONT", "1")
    ctx = InputContext(
        root=root, source_id="a" * 64, input_id="b" * 64, reference_id="hg38",
        reference_sha256="c" * 64, input_dir=input_dir,
        eligible_reads=5_000_000, approx_depth_x=1.0,
    )
    return ctx, directory


def test_lionheart_invokes_upstream_extraction_on_selected_bam_and_emits_raw_scores(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    assert isinstance(lionheart.LIONHEART.preflight(ctx), Ready)
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    result = lionheart.LIONHEART.run(ctx, out)
    assert result.metrics[0]["value"] == 898
    assert result.panels[0]["data_ref"] == "artifacts/lionheart_scores.json"
    payload = json.loads(result.artifacts["lionheart_scores.json"])
    assert payload["input_mode"] == "experimental_single_end_ont"
    assert payload["score_kind"] == "sample_standardized_pearson_r"
    assert len(payload["scores"]) == 898
    assert payload["scores"][0] == {"assay": "ATAC", "cell_type": "ATAC type 0", "score": -1.0}
    assert payload["scores"][449]["assay"] == "DNase"
    assert payload["scores"][-1]["score"] == 1.0
    before = execution_identity(lionheart.LIONHEART, reference_sha256=ctx.reference_sha256,
                                asset_sha256s=lionheart.LIONHEART.asset_sha256s(ctx.root))
    (directory / "gc_contents_bin_edges.npy").write_bytes(b"changed resource")
    after = execution_identity(lionheart.LIONHEART, reference_sha256=ctx.reference_sha256,
                               asset_sha256s=lionheart.LIONHEART.asset_sha256s(ctx.root))
    assert before != after


def test_lionheart_preflight_requires_opt_in_depth_reference_and_pinned_assets(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    assert lionheart.LIONHEART.preflight(InputContext(**{**ctx.__dict__, "eligible_reads": 4_999_999})).reason_code == "depth"
    assert lionheart.LIONHEART.preflight(InputContext(**{**ctx.__dict__, "reference_id": "synth"})).reason_code == "reference"
    monkeypatch.delenv("TRACEBACK_LIONHEART_ENABLE_ONT")
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "platform_unverified"
    monkeypatch.setenv("TRACEBACK_LIONHEART_ENABLE_ONT", "1")
    (directory / "ATAC.idx_to_cell_type.csv").unlink()
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"


def test_lionheart_resource_digest_rejects_symlink(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    (directory / "linked.bin").symlink_to(directory / "gc_contents_bin_edges.npy")
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"


@needs_chrome
def test_lionheart_scores_are_labeled_research_only_in_signals(tmp_path) -> None:
    library = load_library()
    license_id = "lionheart-resources-research"
    sample = {
        "found": True, "id12": ID12, "source_id": "a" * 64, "full_input_id": "b" * 64,
        "example": False, "run_utc": "2026-10-16T00:00:00Z", "cohort_n": 0,
        "algorithms": [{"algorithm_id": "lionheart", "status": "ok", "hidden": False,
                        "skip_reason": None, "metrics": [{"metric_id": "cell_types_scored", "value": 898}],
                        "provenance": {"input_id": "b" * 64}}],
        "catalog": [{"algorithm_id": "lionheart", "tier": "SPECULATIVE", "origin_tier": "NEWER",
                     "footnote": "No validated ONT performance", "citation_checked": False,
                     "license_ids": [license_id]}],
        "licenses": [{"license_id": license_id, **library.licenses[license_id]}],
        "library_health": {"version": library.version, "error": None},
    }
    rows = [{"assay": "ATAC" if index < 449 else "DNase", "cell_type": f"synthetic {index}",
             "score": round(index / 1000, 3)} for index in range(898)]
    payload = json.dumps({"kind": "lionheart-scores.v1", "input_mode": "experimental_single_end_ont",
                          "score_kind": "sample_standardized_pearson_r", "scores": rows}).encode()
    with RunningLabServer(LabStore(tmp_path / "root")) as server:
        server.app.api.status = lambda: {"generation": 1, "active": None, "interrupted": None,
                                         "run_log_unavailable": None, "library": sample["library_health"]}
        server.app.api.samples = lambda: {"samples": [{"id12": ID12, "run_utc": sample["run_utc"],
                                                        "depth_x": 1.0, "example": False}], "root_has_runs": True}
        server.app.api.sample = lambda id12: sample if id12 == ID12 else {"found": False, "candidates": []}
        server.app.api.artifact = lambda id12, label, algorithm_id, panel_id: (
            payload if (id12, label, algorithm_id, panel_id) ==
            (ID12, "full", "lionheart", "cell-type-scores") else None
        )
        dom = _dom(server, tmp_path / "browser", route=f"/sample/{ID12}/signals",
                   want=lambda page: "All 898 raw scores" in page)
    assert "Experimental ONT single-end extraction" in dom
    assert "not cell fractions or cancer predictions" in dom
    assert "RESEARCH-ONLY RESOURCES: LIONHEART cell-type scores" in dom
    assert "synthetic 0" in dom and "synthetic 897" in dom
