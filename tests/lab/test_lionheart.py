"""Synthetic verification of the actual BAM-to-LIONHEART feature-extraction adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
import pysam

from tests.lab.test_landing import _dom, needs_chrome
from tests.lab.fake_algorithms import ASSET_PROBE
from traceback_lab.algorithms import lionheart
from traceback_lab.algorithms.read_summary import READ_SUMMARY
from traceback_lab.interpretation.engine import load_library
from traceback_lab.registry import InputContext, PlanItem, Ready, execution_identity, plan, validate_algorithm
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id
from traceback_lab.web.server import RunningLabServer

ID12 = "3f9a1c2b7e04"


def test_planned_asset_and_tool_identity_reaches_real_worker(tmp_path: Path) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    target = RunTarget(
        input_id="b" * 64, source_id="a" * 64, input_dir=tmp_path,
        reference_id="hg38", reference_sha256="c" * 64, eligible_reads=5_000_000,
    )
    run_id = new_run_id()
    [result] = run_plan(
        store, None, store.run_log(run_id), target, [PlanItem(ASSET_PROBE, False)],
        {ASSET_PROBE.id: "tests.lab.fake_algorithms:ASSET_PROBE"},
        run_id=run_id, scrubber=Scrubber(root=tmp_path),
    )
    assert result.status == "ok", result.first_line
    assert result.parameters_sha256 == execution_identity(
        ASSET_PROBE, reference_sha256=target.reference_sha256,
        asset_sha256s=["a" * 64], tool_pins=[{"name": "probe", "version": "2.2.0"}],
    )
    assert result.artifacts_dir is not None
    observed = json.loads((Path(result.artifacts_dir) / "worker_identity.json").read_text())
    assert observed == {"assets": ["a" * 64], "tools": {"probe": "2.2.0"}}


def _setup(tmp_path: Path, monkeypatch) -> tuple[InputContext, Path]:
    root = tmp_path / "root"
    input_dir = root / "lab" / "inputs" / ("b" * 64)
    input_dir.mkdir(parents=True)
    (input_dir / "aligned.bam").write_bytes(b"BAM from selected input")
    (input_dir / "manifest.json").write_text('{"kind":"aligned"}')
    directory = root / "lab" / "assets" / "lionheart" / "inference_resources_v003"
    for relative in lionheart.REQUIRED_FILES:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic resource")
    for relative in lionheart.REQUIRED_DIRS:
        path = directory / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "fixture.bin").write_bytes(b"synthetic mask")
    for assay, count in (("ATAC", 544), ("DNase", 354)):
        (directory / f"{assay}.idx_to_cell_type.csv").write_text(
            "idx,cell_type\n" + "".join(f"{index},{assay} type {index}\n" for index in range(count))
        )
    archive = directory.parent / "inference_resources_v003.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(directory, arcname="inference_resources_v003")
    monkeypatch.setattr(lionheart, "RESOURCE_ARCHIVE_MD5", hashlib.md5(archive.read_bytes()).hexdigest())

    executable = tmp_path / "lionheart"
    executable.write_text(f"""#!{sys.executable}
import argparse
import os
import pathlib
import sys
if sys.argv[1:] == ['--version']:
    print('lionheart, version 2.2.0')
    sys.exit(0)
import numpy as np
parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('--bam_file')
parser.add_argument('--resources_dir')
parser.add_argument('--out_dir')
parser.add_argument('--mosdepth_path')
parser.add_argument('--ld_library_path')
parser.add_argument('--n_jobs')
parser.add_argument('--single_end_mode', action='store_true')
args = parser.parse_args()
assert args.command == 'extract_features' and args.single_end_mode
assert pathlib.Path(args.bam_file).read_bytes() == b'BAM from selected input'
assert pathlib.Path(args.resources_dir).is_dir()
assert pathlib.Path(args.mosdepth_path).is_file()
assert pathlib.Path(args.ld_library_path).is_dir()
if os.environ.get('TRACEBACK_LIONHEART_MUTATE_RESOURCES'):
    (pathlib.Path(args.resources_dir) / 'gc_contents_bin_edges.npy').write_bytes(b'mutated')
if os.environ.get('TRACEBACK_LIONHEART_MUTATE_LIBHTS'):
    (pathlib.Path(args.ld_library_path) / 'libhts.so').write_bytes(b'mutated')
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
    (tmp_path / "libhts.so").write_bytes(b"fixture libhts")
    for helper in ("samtools", "unpigz"):
        helper_path = tmp_path / helper
        helper_path.write_text("#!/bin/sh\nexit 0\n")
        helper_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("TRACEBACK_LIONHEART_BIN", str(executable))
    monkeypatch.setenv("TRACEBACK_LIONHEART_MOSDEPTH_BIN", str(mosdepth))
    monkeypatch.setenv("TRACEBACK_LIONHEART_RESOURCES", str(directory))
    monkeypatch.setenv("TRACEBACK_LIONHEART_ARCHIVE", str(archive))
    monkeypatch.setenv("TRACEBACK_LIONHEART_ENABLE_ONT", "1")
    monkeypatch.setattr(lionheart, "_package_pin", lambda _executable: ("2.2.0", "d" * 64))
    monkeypatch.setattr(lionheart, "_pyarrow_available", lambda _python: True)
    ctx = InputContext(
        root=root, source_id="a" * 64, input_id="b" * 64, reference_id="hg38",
        reference_sha256="c" * 64, input_dir=input_dir,
        eligible_reads=5_000_000, approx_depth_x=1.0,
    )
    ctx = replace(ctx, extras={
        "asset_sha256s": lionheart.LIONHEART.asset_sha256s(root),
        "tool_versions": {pin.name: pin.version for pin in lionheart.LIONHEART.tools},
    })
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
    assert payload["scores"][543]["assay"] == "ATAC"
    assert payload["scores"][544]["assay"] == "DNase"
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


@pytest.mark.parametrize("platform,allowed", [
    ("ILLUMINA", False), ("ONT", True), ("OXFORD_NANOPORE", True), (None, False),
])
def test_prealigned_lionheart_requires_declared_ont_platform(tmp_path, monkeypatch, platform, allowed) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    (ctx.input_dir / "manifest.json").write_text('{"kind":"prealigned"}')
    header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 1000}]}
    if platform is not None:
        header["RG"] = [{"ID": "library", "PL": platform}]
    with pysam.AlignmentFile(str(ctx.input_dir / "aligned.bam"), "wb", header=header):
        pass
    assert lionheart._ont_input(ctx) is allowed
    if not allowed:
        assert lionheart.LIONHEART.preflight(ctx).reason_code == "platform_unverified"


def test_lionheart_opt_out_does_not_read_resource_archive(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("TRACEBACK_LIONHEART_ENABLE_ONT")
    monkeypatch.setattr(lionheart, "validated_resource_digest",
                        lambda *_args: pytest.fail("disabled Lionheart must not read the resource archive"))
    assert lionheart.LIONHEART.asset_sha256s(ctx.root) == []
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "platform_unverified"


def test_lionheart_opt_out_does_not_discover_or_hash_tools(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("TRACEBACK_LIONHEART_ENABLE_ONT")
    def unexpected(*_args, **_kwargs):
        pytest.fail("disabled Lionheart must not discover or hash installed tools")
    for name in ("_program", "_package_pin", "_file_hash"):
        monkeypatch.setattr(lionheart, name, unexpected)
    algorithm = validate_algorithm(lionheart.LIONHEART)
    assert algorithm.tools == ()
    assert plan((READ_SUMMARY, algorithm), only=(algorithm.id,))[-1].algorithm is algorithm
    assert execution_identity(algorithm, reference_sha256=ctx.reference_sha256)
    assert algorithm.preflight(ctx).reason_code == "platform_unverified"


def test_lionheart_preflight_requires_parquet_reader_in_cli_environment(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(lionheart, "_pyarrow_available", lambda _python: False)
    decision = lionheart.LIONHEART.preflight(ctx)
    assert decision.reason_code == "missing_toolchain"
    assert "Install pyarrow in the LIONHEART CLI's Python environment" in decision.message


def test_lionheart_preflight_explains_unsupported_launcher(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    Path(os.environ["TRACEBACK_LIONHEART_BIN"]).write_text("#!/usr/bin/env python3\nprint('lionheart, version 2.2.0')\n")
    decision = lionheart.LIONHEART.preflight(ctx)
    assert decision.reason_code == "missing_toolchain"
    assert "direct interpreter shebang" in decision.message


def test_lionheart_preflight_requires_upstream_helpers_and_library_dir(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    original_pins = lionheart.LIONHEART.tools
    alternate_library = tmp_path / "other-library"
    alternate_library.mkdir()
    (alternate_library / "libhts.so").write_bytes(b"fixture libhts")
    monkeypatch.setenv("TRACEBACK_LIONHEART_LD_LIBRARY_PATH", str(alternate_library))
    assert lionheart.LIONHEART.tools != original_pins
    alternate_pins = lionheart.LIONHEART.tools
    (alternate_library / "libhts.so").write_bytes(b"replacement libhts")
    assert lionheart.LIONHEART.tools != alternate_pins
    monkeypatch.delenv("TRACEBACK_LIONHEART_LD_LIBRARY_PATH")
    (tmp_path / "unpigz").unlink()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_toolchain"
    (tmp_path / "unpigz").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "unpigz").chmod(0o755)
    monkeypatch.setenv("TRACEBACK_LIONHEART_LD_LIBRARY_PATH", str(tmp_path / "absent"))
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_toolchain"


def test_lionheart_resource_digest_rejects_symlink(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    (directory / "linked.bin").symlink_to(directory / "gc_contents_bin_edges.npy")
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"


def test_good_archive_does_not_authorize_modified_extracted_resources(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    assert isinstance(lionheart.LIONHEART.preflight(ctx), Ready)
    (directory / "gc_contents_bin_edges.npy").write_bytes(b"modified after archive verification")
    assert lionheart.LIONHEART.asset_sha256s(ctx.root) == []
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(ValueError, match="differ from the verified archive"):
        lionheart.LIONHEART.run(ctx, out)


def test_archive_digest_must_match_published_record(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    archive = directory.parent / "inference_resources_v003.tar.gz"
    with archive.open("ab") as stream:
        stream.write(b"changed archive")
    assert lionheart.LIONHEART.asset_sha256s(ctx.root) == []
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"
    with pytest.raises(ValueError, match="does not match Zenodo record"):
        lionheart.validated_resource_digest(directory, archive)


def test_verified_archive_still_rejects_unsafe_member(tmp_path, monkeypatch) -> None:
    ctx, directory = _setup(tmp_path, monkeypatch)
    archive = directory.parent / "inference_resources_v003.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(directory, arcname="inference_resources_v003")
        member = tarfile.TarInfo("../escape.txt")
        data = b"unsafe"
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(lionheart, "RESOURCE_ARCHIVE_MD5", hashlib.md5(archive.read_bytes()).hexdigest())
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_asset"
    with pytest.raises(ValueError, match="unsafe path"):
        lionheart.validated_resource_digest(directory, archive)


def test_toolchain_change_after_planning_is_refused(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    executable = Path(os.environ["TRACEBACK_LIONHEART_BIN"])
    executable.write_bytes(executable.read_bytes() + b"\n# changed after planning\n")
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="toolchain changed"):
        lionheart.LIONHEART.run(ctx, out)


def test_loaded_libhts_change_after_planning_or_during_extraction_is_refused(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    library = tmp_path / "libhts.so"
    library.write_bytes(b"changed after planning")
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="toolchain changed since execution planning"):
        lionheart.LIONHEART.run(ctx, out)

    ctx, _ = _setup(tmp_path / "second", monkeypatch)
    monkeypatch.setenv("TRACEBACK_LIONHEART_MUTATE_LIBHTS", "1")
    out = tmp_path / "scratch-second" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="toolchain changed during extraction"):
        lionheart.LIONHEART.run(ctx, out)


def test_extractor_mutating_its_resource_snapshot_is_refused(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("TRACEBACK_LIONHEART_MUTATE_RESOURCES", "1")
    out = tmp_path / "scratch" / "artifacts"
    out.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="snapshot changed"):
        lionheart.LIONHEART.run(ctx, out)


def test_unrecorded_installed_package_is_not_accepted(tmp_path, monkeypatch) -> None:
    ctx, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(lionheart, "_package_pin", lambda _executable: None)
    assert lionheart.LIONHEART.preflight(ctx).reason_code == "missing_toolchain"


def test_installed_python_package_code_enters_tool_pin(tmp_path, monkeypatch) -> None:
    package = tmp_path / "lionheart"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("VERSION = '2.2.0'\n")
    metadata = tmp_path / "lionheart-2.2.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: lionheart\nVersion: 2.2.0\n")
    (metadata / "RECORD").write_text("lionheart/__init__.py,,\n")
    executable = tmp_path / "lionheart-cli"
    executable.write_text(f"#!{sys.executable}\nprint('lionheart, version 2.2.0')\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    first = lionheart._package_pin(executable)
    assert first is not None and first[0] == "2.2.0"
    source.write_text("VERSION = '2.2.0'\nCHANGED = True\n")
    second = lionheart._package_pin(executable)
    assert second is not None and second[1] != first[1]


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
    rows = [{"assay": "ATAC" if index < 544 else "DNase", "cell_type": f"synthetic {index}",
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
