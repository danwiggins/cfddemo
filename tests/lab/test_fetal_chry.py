"""Anonymous chrY calibration plumbing; no biological qualification claim."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import numpy as np

from traceback_lab.algorithms import fetal_fraction_chry as fetal
from traceback_lab.cli import main as lab_main
from traceback_lab.fetal_chry_contract import (
    FetalChrYContractError, inspect_contract, register_contract,
)
from traceback_lab.registry import Requirements, Skip, load_registry
from traceback_lab.store import LabStore

from .bam_fixtures import Read, context_for

SOURCE = "a" * 64
INPUT = "b" * 64
REFERENCE = "c" * 64


def _file(tmp_path: Path, *, status: str = "operator_verified", **changes) -> tuple[Path, str]:
    data = {"schema": "traceback.lab.fetal-chry-calibration.v1", "source_id": SOURCE,
            "input_id": INPUT, "reference_sha256": REFERENCE,
            "pregnancy_confirmed": True, "maternal_female_attested": True,
            "male_fetus_attested": True, "transplant_excluded_attested": True,
            "calibration_status": status, "calibration_source": "anonymous matched mixtures",
            "protocol_label": "anonymous ONT study",
            "counting_protocol": "F1-canonical-chrY-nonPAR-starts-per-million-all-eligible.v1",
            "female_control_n": 10, "male_mixture_n": 10, "min_depth_x": 0.001,
            "female_background_y_per_million": 100_000.0,
            "full_male_y_per_million": 300_000.0,
            "validated_min_fraction": 0.1, "validated_max_fraction": 0.9}
    data.update(changes)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_registration_is_immutable_and_cli_keeps_labels_private(tmp_path: Path, capsys) -> None:
    path, digest = _file(tmp_path, calibration_source="private study label")
    store = LabStore(tmp_path / "root")
    assert lab_main(["fetal", "chry-register", "--file", str(path), "--sha256", digest,
                     "--root", str(store.root), "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["data"]["manifest_sha256"] == digest
    assert "private study label" not in json.dumps(response)
    assert str(path) not in json.dumps(response)
    assert lab_main(["fetal", "chry-inspect", INPUT, "--root", str(store.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["female_control_n"] == 10
    assert register_contract(store, path, digest) == inspect_contract(store, INPUT)
    original = path.read_bytes()
    changed, changed_digest = _file(tmp_path, full_male_y_per_million=400_000.0)
    with pytest.raises(FetalChrYContractError, match="different calibration"):
        register_contract(store, changed, changed_digest)
    (store.lab / "assets" / "fetal-fraction-chry" / INPUT / "contract.json").write_bytes(b"{}")
    path.write_bytes(original)
    with pytest.raises(FetalChrYContractError, match="restore its exact original bytes"):
        register_contract(store, path, digest)


@pytest.mark.parametrize("change", [
    {"pregnancy_confirmed": False}, {"maternal_female_attested": False},
    {"male_fetus_attested": False}, {"transplant_excluded_attested": False},
    {"counting_protocol": "other"}, {"female_control_n": 0},
    {"male_mixture_n": 0}, {"min_depth_x": 0},
    {"min_depth_x": 10 ** 1000}, {"female_background_y_per_million": -1},
    {"full_male_y_per_million": 100_000}, {"validated_min_fraction": -0.1},
    {"validated_max_fraction": 1.1}, {"calibration_status": "presumed"},
])
def test_calibration_refuses_unsupported_assertions_and_bounds(tmp_path: Path, change: dict) -> None:
    path, digest = _file(tmp_path, **change)
    with pytest.raises(FetalChrYContractError):
        register_contract(LabStore(tmp_path / "root"), path, digest)


def test_synthetic_registration_needs_opt_in_and_cannot_run(tmp_path: Path) -> None:
    path, digest = _file(tmp_path, status="synthetic")
    store = LabStore(tmp_path / "root")
    with pytest.raises(FetalChrYContractError, match="--allow-synthetic"):
        register_contract(store, path, digest)
    assert not (store.lab / "assets" / "fetal-fraction-chry" / INPUT).exists()
    contract = register_contract(store, path, digest, allow_synthetic=True)
    assert contract.calibration_status == "synthetic"
    ctx = context_for(tmp_path / "data", [("chr1", 248_956_422)],
                      [Read("anonymous", "chr1", 100, "A" * 75)], eligible_reads=2_000_000)
    ctx = replace(ctx, root=store.root, source_id=SOURCE, input_id=INPUT,
                  reference_sha256=REFERENCE, approx_depth_x=1,
                  extras={**ctx.extras, "planned_asset_sha256s": [contract.manifest_sha256]})
    assert fetal.FETAL_FRACTION_CHRY.preflight(ctx).reason_code == "missing_asset"


def test_registration_publish_error_is_path_free(tmp_path: Path, monkeypatch, capsys) -> None:
    path, digest = _file(tmp_path)
    root = tmp_path / "root"

    def fail_publish(*_args, **_kwargs):
        raise OSError(f"private path {path}")

    monkeypatch.setattr(LabStore, "publish_directory", fail_publish)
    assert lab_main(["fetal", "chry-register", "--file", str(path), "--sha256", digest,
                     "--root", str(root), "--json"]) != 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert str(path) not in json.dumps(response)
    assert not (root / "lab" / "assets" / "fetal-fraction-chry" / INPUT).exists()


def test_chry_counting_uses_half_open_par_boundaries_and_bounded_slices(monkeypatch) -> None:
    monkeypatch.setattr(fetal, "CHUNK_ROWS", 3)
    starts = np.array([9_999, 10_000, 2_781_478, 2_781_479,
                       56_887_901, 56_887_902, 57_217_414, 57_217_415], dtype=np.int32)
    chrom = np.full(len(starts), 23, dtype=np.int16)
    flags = np.zeros(len(starts), dtype=np.uint16)
    mapqs = np.full(len(starts), 60, dtype=np.uint8)
    flags[0] = 0x400
    mapqs[7] = 19
    columns = {"contig": chrom, "start": starts, "flag": flags, "mapq": mapqs}

    class Summary:
        rows = len(starts)

        @staticmethod
        def column(name):
            return columns[name]

        @staticmethod
        def eligible():
            pytest.fail("full-summary eligibility mask must not be allocated")

    assert fetal._counts(Summary()) == (2, 6)


def test_real_summary_counts_nonpar_y_and_calibrates_only_within_bounds(tmp_path: Path) -> None:
    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    reads = [Read(f"autosome-{i}", "chr1", 1000 + i * 1000, "A" * 75) for i in range(7)]
    reads += [Read("first-nonpar", "chrY", 2_781_479, "A" * 75),
              Read("last-nonpar", "chrY", 56_887_901, "A" * 75),
              Read("par", "chrY", 10_000, "A" * 75),
              Read("low-mapq", "chrY", 3_000_000, "A" * 75, mapq=19),
              Read("duplicate", "chrY", 3_001_000, "A" * 75, flag=0x400)]
    ctx = context_for(tmp_path, contigs, reads, eligible_reads=10)
    store = LabStore(tmp_path)
    path, digest = _file(tmp_path)
    contract = register_contract(store, path, digest)
    ctx = replace(ctx, source_id=SOURCE, input_id=INPUT, reference_sha256=REFERENCE,
                  approx_depth_x=0.01,
                  extras={**ctx.extras, "planned_asset_sha256s": [contract.manifest_sha256]})
    assert fetal.FETAL_FRACTION_CHRY.preflight(ctx).reason_code == "depth"
    algorithm = replace(fetal.FETAL_FRACTION_CHRY, requires=Requirements(min_eligible_reads=0))
    assert not isinstance(algorithm.preflight(ctx), Skip)
    measured = {item["metric_id"]: item["value"] for item in algorithm.run(ctx, tmp_path).metrics}
    assert measured == {"fetal_fraction": pytest.approx(0.5),
                        "fetal_chry_nonpar_reads": 2,
                        "fetal_chry_reads_per_million": 200_000}
    assert algorithm.preflight(replace(ctx, derived={"half": "A"})).reason_code == "unsupported_input"
    assert algorithm.preflight(replace(ctx, source_id="f" * 64)).reason_code == "unsupported_input"
    assert algorithm.preflight(replace(ctx, reference_sha256="f" * 64)).reason_code == "reference"
    assert algorithm.preflight(replace(ctx, extras={**ctx.extras, "planned_asset_sha256s": []})).reason_code == "missing_asset"
    assert fetal._fraction(0, 10, contract) is None
    assert fetal._fraction(3, 10, contract) is None


def test_registry_and_interpretation_keep_chr_y_separate_from_nipter() -> None:
    from traceback_lab.interpretation.engine import load_library

    entries = {item.id for item in load_registry()}
    assert {"fetal-fraction", "fetal-fraction-chry"} <= entries
    library = load_library()
    assert library.entry("fetal-fraction", "fetal_fraction").raw["performance"] is None
    assert library.entry("fetal-fraction-chry", "fetal_fraction").raw["tier"] == "SPECULATIVE"
