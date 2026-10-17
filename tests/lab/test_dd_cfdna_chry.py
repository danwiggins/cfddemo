"""Synthetic calibration mechanics and refusal paths; no assay qualification."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from traceback_lab.algorithms import dd_cfdna_chry as donor
from traceback_lab.cli import main as lab_main
from traceback_lab.dd_contract import DonorContractError, inspect_contract, register_contract
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip, context_to_json
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id

from .bam_fixtures import Read, context_for


SOURCE = "a" * 64
INPUT = "b" * 64
REFERENCE = "c" * 64


def _file(tmp_path: Path, *, status: str = "operator_verified", **changes) -> tuple[Path, str]:
    data = {"schema": "traceback.lab.dd-cfdna-contract.v1", "source_id": SOURCE,
            "input_id": INPUT, "reference_sha256": REFERENCE,
            "male_donor_attested": True, "female_recipient_attested": True,
            "transplant_attested": True, "pregnancy_excluded_attested": True,
            "calibration_status": status, "calibration_source": "anonymous matched mixtures",
            "protocol_label": "synthetic ONT protocol",
            "counting_protocol": "D21-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1",
            "female_control_n": 10,
            "male_calibration_n": 10, "min_depth_x": 4.0,
            "female_background_y_per_million": 1000.0,
            "male_full_donor_y_per_million": 3000.0}
    data.update(changes)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_registration_is_input_scoped_immutable_and_cli_inspectable(tmp_path: Path, capsys) -> None:
    path, digest = _file(tmp_path)
    store = LabStore(tmp_path / "root")
    with pytest.raises(DonorContractError, match="expected"):
        register_contract(store, path, "0" * 64)
    assert lab_main(["donor", "register", "--file", str(path), "--sha256", digest,
                     "--root", str(store.root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["data"]["manifest_sha256"] == digest
    assert lab_main(["donor", "inspect", INPUT, "--root", str(store.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["min_depth_x"] == 4
    assert register_contract(store, path, digest) == inspect_contract(store, INPUT)
    changed, changed_digest = _file(tmp_path, male_full_donor_y_per_million=4000)
    with pytest.raises(DonorContractError, match="different calibration"):
        register_contract(store, changed, changed_digest)
    (store.lab / "assets" / "dd-cfdna-chry" / INPUT / "contract.json").write_bytes(b"{}")
    with pytest.raises(DonorContractError):
        inspect_contract(store, INPUT)


def test_registration_publish_failure_is_structured_and_path_free(tmp_path: Path, monkeypatch, capsys) -> None:
    path, digest = _file(tmp_path)
    root = tmp_path / "root"
    def fail_publish(*_args, **_kwargs):
        raise OSError(f"private path: {path}")
    monkeypatch.setattr(LabStore, "publish_directory", fail_publish)
    assert lab_main(["donor", "register", "--file", str(path), "--sha256", digest,
                     "--root", str(root), "--json"]) != 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert str(path) not in json.dumps(response)
    assert not (root / "lab" / "assets" / "dd-cfdna-chry" / INPUT).exists()


@pytest.mark.parametrize("change", [
    {"male_donor_attested": False}, {"female_recipient_attested": False},
    {"pregnancy_excluded_attested": False}, {"min_depth_x": 0},
    {"min_depth_x": 10 ** 1000},
    {"female_background_y_per_million": 4000}, {"male_full_donor_y_per_million": 1000},
    {"female_control_n": 0}, {"calibration_status": "presumed"},
    {"counting_protocol": "unknown"},
])
def test_contract_refuses_unverified_assertions_and_invalid_calibration(tmp_path: Path, change: dict) -> None:
    path, digest = _file(tmp_path, **change)
    with pytest.raises(DonorContractError):
        register_contract(LabStore(tmp_path / "root"), path, digest)


def test_synthetic_calibration_skips_and_verified_mechanics_use_nonpar_y(tmp_path: Path, monkeypatch) -> None:
    store = LabStore(tmp_path / "root")
    path, digest = _file(tmp_path, status="synthetic")
    contract = register_contract(store, path, digest)
    contigs = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY"]
    lengths = [248_956_422] + [1_000_000] * 22 + [57_227_415]
    chrom = np.zeros(1000, dtype=np.int16)
    chrom[:3] = 23
    starts = np.zeros(1000, dtype=np.int64)
    starts[:3] = [3_000_000, 4_000_000, 10_001]
    summary = SimpleNamespace(contigs=contigs, lengths=lengths,
                              eligible=lambda: np.ones(1000, dtype=bool),
                              column=lambda name: {"contig": chrom, "start": starts}[name])
    monkeypatch.setattr(donor, "load_summary", lambda ctx: (summary, None))
    ctx = InputContext(store.root, SOURCE, INPUT, "hg38", REFERENCE, tmp_path,
                       eligible_reads=1000, approx_depth_x=5,
                       extras={"planned_asset_sha256s": [contract.manifest_sha256]})
    assert isinstance(donor.DD_CFDNA_CHRY.preflight(ctx), Skip)
    # The estimation arithmetic can be exercised with synthetic calibration,
    # while publication requires a separately attested operator-verified asset.
    assert donor._fraction(2, 1000, contract) == pytest.approx(0.5)
    assert donor._fraction(0, 1000, contract) is None
    assert donor._fraction(4, 1000, contract) is None

    # Exercise the registered path with an attested synthetic fixture. This
    # proves plumbing only; the fixture is not a biological verification.
    verified_root = LabStore(tmp_path / "verified")
    path, digest = _file(tmp_path, status="operator_verified")
    verified = register_contract(verified_root, path, digest)
    verified_ctx = replace(ctx, root=verified_root.root,
                           extras={"planned_asset_sha256s": [verified.manifest_sha256]})
    assert isinstance(donor.DD_CFDNA_CHRY.preflight(verified_ctx), Ready)
    values = {item["metric_id"]: item["value"] for item in donor.DD_CFDNA_CHRY.run(verified_ctx, tmp_path).metrics}
    assert values == {"dd_cfdna_fraction": 0.5, "chry_nonpar_reads": 2,
                      "chry_reads_per_million": 2000.0}
    assert donor.DD_CFDNA_CHRY.preflight(replace(verified_ctx, approx_depth_x=3.9)).reason_code == "depth"
    assert donor.DD_CFDNA_CHRY.preflight(replace(verified_ctx, approx_depth_x=float("nan"))).reason_code == "depth"
    assert donor.DD_CFDNA_CHRY.preflight(replace(verified_ctx, derived={"half": "A"})).reason_code == "unsupported_input"
    assert donor.DD_CFDNA_CHRY.preflight(replace(verified_ctx, extras={})).reason_code == "missing_asset"
    lengths[23] -= 1
    assert donor.DD_CFDNA_CHRY.preflight(verified_ctx).reason_code == "reference"


def test_real_bam_summary_and_worker_obey_d21_and_par_boundaries(tmp_path: Path) -> None:
    """Anonymous fixture verifies plumbing; its asserted calibration is not biology."""

    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    reads = [Read(f"autosome-{i}", "chr1", 1000 + i * 1000, "A" * 75) for i in range(7)]
    reads += [
        Read("after-par1", "chrY", 2_781_479, "A" * 75),
        Read("before-par2", "chrY", 56_887_901, "A" * 75),
        Read("inside-par1", "chrY", 10_000, "A" * 75),
        Read("low-mapq", "chrY", 3_000_000, "A" * 75, mapq=19),
        Read("duplicate", "chrY", 3_001_000, "A" * 75, flag=0x400),
        Read("supplementary", "chrY", 3_002_000, "A" * 75, flag=0x800),
    ]
    ctx = context_for(tmp_path, contigs, reads)
    store = LabStore(tmp_path)
    path, digest = _file(tmp_path, min_depth_x=0.001,
                         female_background_y_per_million=100_000,
                         male_full_donor_y_per_million=300_000)
    contract = register_contract(store, path, digest)
    ctx = replace(ctx, source_id=SOURCE, input_id=INPUT, reference_id="hg38",
                  reference_sha256=REFERENCE, eligible_reads=10, approx_depth_x=0.01,
                  extras={**ctx.extras, "planned_asset_sha256s": [contract.manifest_sha256]})
    assert isinstance(donor.DD_CFDNA_CHRY.preflight(ctx), Ready)
    out = tmp_path / "worker"
    out.mkdir()
    request = out / "request.json"
    request.write_text(json.dumps({"entry": "dd_cfdna_chry:DD_CFDNA_CHRY",
                                   "context": context_to_json(ctx), "out_dir": str(out)}))
    subprocess.run([sys.executable, "-m", "traceback_lab.worker", str(request)], check=True)
    metrics = {item["metric_id"]: item for item in json.loads((out / "output.json").read_text())["metrics"]}
    assert metrics["chry_nonpar_reads"]["value"] == 2
    assert metrics["chry_reads_per_million"]["value"] == 200_000
    assert metrics["dd_cfdna_fraction"]["value"] == pytest.approx(0.5)
    assert metrics["dd_cfdna_fraction"]["n"] == 10


def test_derived_donor_skip_publishes_through_runner(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    run_id = new_run_id()
    target = RunTarget(INPUT, SOURCE, tmp_path, "hg38", REFERENCE,
                       eligible_reads=10, derived={"transform": "split", "half": "A"})
    algorithm = replace(donor.DD_CFDNA_CHRY, upstream=())
    results = run_plan(store, None, store.run_log(run_id), target,
                       [PlanItem(algorithm, False)], {}, run_id=run_id,
                       scrubber=Scrubber(root=store.root))
    assert len(results) == 1 and results[0].status == "skipped"
    assert results[0].skip_reason == "unsupported_input"
    saved = store.read_result(INPUT, algorithm.id, results[0].parameters_sha256)
    assert saved is not None and saved.status == "skipped"
    assert saved.skip_reason == "unsupported_input"
