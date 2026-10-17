"""Synthetic calibration mechanics and refusal paths; no assay qualification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from traceback_lab.algorithms import dd_cfdna_chry as donor
from traceback_lab.cli import main as lab_main
from traceback_lab.dd_contract import DonorContractError, inspect_contract, register_contract
from traceback_lab.registry import InputContext, Ready, Skip
from traceback_lab.store import LabStore


SOURCE = "a" * 64
INPUT = "b" * 64
REFERENCE = "c" * 64


def _file(tmp_path: Path, *, status: str = "operator_verified", **changes) -> tuple[Path, str]:
    data = {"schema": "traceback.lab.dd-cfdna-contract.v1", "source_id": SOURCE,
            "input_id": INPUT, "reference_sha256": REFERENCE,
            "male_donor_attested": True, "female_recipient_attested": True,
            "transplant_attested": True, "pregnancy_excluded_attested": True,
            "calibration_status": status, "calibration_source": "anonymous matched mixtures",
            "protocol_label": "synthetic ONT protocol", "female_control_n": 10,
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


@pytest.mark.parametrize("change", [
    {"male_donor_attested": False}, {"female_recipient_attested": False},
    {"pregnancy_excluded_attested": False}, {"min_depth_x": 0},
    {"min_depth_x": 10 ** 1000},
    {"female_background_y_per_million": 4000}, {"male_full_donor_y_per_million": 1000},
    {"female_control_n": 0}, {"calibration_status": "presumed"},
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
