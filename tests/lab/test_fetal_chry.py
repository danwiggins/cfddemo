"""Anonymous chrY calibration plumbing; no biological qualification claim."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np

from traceback_lab.algorithms import fetal_fraction_chry as fetal
from traceback_lab.cli import main as lab_main
from traceback_lab.fetal_chry_contract import (
    FetalChrYContractError, _parse, inspect_contract, register_contract,
)
from traceback_lab.registry import PlanItem, Ready, Requirements, Skip, execution_identity, load_registry
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id

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
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["female_control_n"] == 10
    assert inspected["female_background_y_per_million"] == 100_000.0
    assert inspected["full_male_y_per_million"] == 300_000.0
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


def test_deeply_nested_calibration_is_structured_failure(tmp_path: Path, capsys) -> None:
    raw = b"[" * 2048 + b"]" * 2048
    with pytest.raises(FetalChrYContractError, match="invalid JSON"):
        _parse(raw)
    path = tmp_path / "deeply-nested-private.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert lab_main(["fetal", "chry-register", "--file", str(path), "--sha256", digest,
                     "--root", str(tmp_path / "root"), "--json"]) != 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert str(path) not in json.dumps(response)


def test_synthetic_registration_needs_opt_in_and_cannot_run(tmp_path: Path) -> None:
    path, digest = _file(tmp_path, status="synthetic")
    store = LabStore(tmp_path / "root")
    with pytest.raises(FetalChrYContractError, match="--allow-synthetic"):
        register_contract(store, path, digest)
    assert not (store.lab / "assets" / "fetal-fraction-chry" / INPUT).exists()
    contract = register_contract(store, path, digest, allow_synthetic=True)
    assert contract.calibration_status == "synthetic"
    assert inspect_contract(store, INPUT, synthetic=True) == contract
    with pytest.raises(FetalChrYContractError):
        inspect_contract(store, INPUT)
    assert lab_main(["fetal", "chry-inspect", INPUT, "--synthetic", "--root", str(store.root), "--json"]) == 0
    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    ctx = context_for(tmp_path / "data", contigs,
                      [Read("anonymous", "chr1", 100, "A" * 75)], eligible_reads=2_000_000)
    ctx = replace(ctx, root=store.root, source_id=SOURCE, input_id=INPUT,
                  reference_sha256=REFERENCE, approx_depth_x=1,
                  extras={**ctx.extras, "planned_asset_sha256s": [contract.manifest_sha256]})
    assert fetal.FETAL_FRACTION_CHRY.preflight(ctx).reason_code == "missing_asset"
    real_path, real_digest = _file(tmp_path, status="operator_verified")
    real = register_contract(store, real_path, real_digest)
    assert inspect_contract(store, INPUT) == real
    assert inspect_contract(store, INPUT, synthetic=True) == contract


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


def test_registration_source_error_is_path_free(tmp_path: Path, monkeypatch, capsys) -> None:
    from traceback_lab import fetal_chry_contract as contract_module

    path, digest = _file(tmp_path)
    monkeypatch.setattr(contract_module, "_regular_source",
                        lambda _path: (_ for _ in ()).throw(OSError(f"private path {path}")))
    assert lab_main(["fetal", "chry-register", "--file", str(path), "--sha256", digest,
                     "--root", str(tmp_path / "root"), "--json"]) != 0
    assert str(path) not in capsys.readouterr().out


def test_chry_counting_uses_half_open_par_boundaries_and_bounded_slices(monkeypatch) -> None:
    monkeypatch.setattr(fetal, "CHUNK_ROWS", 3)
    starts = np.array([9_999, 10_000, 2_781_478, 2_781_479,
                       56_887_901, 56_887_902, 57_217_414, 57_217_415,
                       3_000_000, 3_000_000], dtype=np.int32)
    chrom = np.full(len(starts), 23, dtype=np.int16)
    chrom[9] = 24  # canonical D21 eligibility excludes alternate contigs
    flags = np.zeros(len(starts), dtype=np.uint16)
    mapqs = np.full(len(starts), 60, dtype=np.uint8)
    flags[8] = 0x400
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

    assert fetal._counts(Summary()) == (3, 7)


def test_f1_protocol_refuses_drift_in_shared_counting_constants(monkeypatch) -> None:
    assert fetal._protocol_matches()
    monkeypatch.setattr(fetal, "MIN_MAPQ", 21)
    assert not fetal._protocol_matches()


def test_duplicate_calibration_fields_are_rejected(tmp_path: Path) -> None:
    path, _ = _file(tmp_path)
    raw = path.read_text().replace('"female_control_n": 10', '"female_control_n": 10, "female_control_n": 99')
    path.write_text(raw)
    with pytest.raises(FetalChrYContractError, match="invalid JSON"):
        register_contract(LabStore(tmp_path / "root"), path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_real_summary_counts_nonpar_y_and_calibrates_only_within_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    summary, _ = fetal.load_summary(ctx)
    assert fetal._counts(summary)[1] == int(summary.eligible().sum())
    assert fetal.FETAL_FRACTION_CHRY.preflight(ctx).reason_code == "depth"
    algorithm = replace(fetal.FETAL_FRACTION_CHRY, requires=Requirements(min_eligible_reads=0))
    assert not isinstance(algorithm.preflight(ctx), Skip)
    assert algorithm.preflight(replace(ctx, approx_depth_x=0.0005)).reason_code == "missing_asset"
    measured = {item["metric_id"]: item["value"] for item in algorithm.run(ctx, tmp_path).metrics}
    assert measured == {"fetal_fraction": pytest.approx(0.5),
                        "fetal_chry_nonpar_reads": 2,
                        "fetal_chry_reads_per_million": 200_000}
    wrong_order = SimpleNamespace(contigs=("chrY", *summary.contigs[1:]), lengths=summary.lengths)
    with monkeypatch.context() as patch:
        patch.setattr(fetal, "load_summary", lambda _ctx: (wrong_order, None))
        assert algorithm.preflight(replace(ctx, input_id="f" * 64)).reason_code == "reference"
    with monkeypatch.context() as patch:
        loads = iter(((summary, None), (wrong_order, None)))
        patch.setattr(fetal, "load_summary", lambda _ctx: next(loads))
        with pytest.raises(RuntimeError, match="BAM reference layout"):
            algorithm.run(ctx, tmp_path)
    assert algorithm.preflight(replace(ctx, derived={"half": "A"})).reason_code == "unsupported_input"
    assert algorithm.preflight(replace(ctx, source_id="f" * 64)).reason_code == "missing_asset"
    assert algorithm.preflight(replace(ctx, reference_sha256="f" * 64)).reason_code == "missing_asset"
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


def test_registered_calibration_enters_identity_and_derived_skip_is_published(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    path, digest = _file(tmp_path)
    contract = register_contract(store, path, digest)
    algorithm = fetal.FETAL_FRACTION_CHRY
    assets = algorithm.asset_sha256s_for_input(store.root, INPUT)
    assert assets == [contract.manifest_sha256]
    assert execution_identity(algorithm, reference_sha256=REFERENCE, asset_sha256s=assets) != execution_identity(
        algorithm, reference_sha256=REFERENCE)

    run_id = new_run_id()
    target = RunTarget(INPUT, SOURCE, tmp_path, "hg38", REFERENCE,
                       eligible_reads=2_000_000, derived={"transform": "split", "half": "A"})
    derived_algorithm = replace(algorithm, upstream=())
    results = run_plan(store, None, store.run_log(run_id), target,
                       [PlanItem(derived_algorithm, False)], {}, run_id=run_id,
                       scrubber=Scrubber(root=store.root))
    assert len(results) == 1 and results[0].status == "skipped"
    assert results[0].skip_reason == "unsupported_input"
    saved = store.read_result(INPUT, derived_algorithm.id, results[0].parameters_sha256)
    assert saved is not None and saved.status == "skipped"


def test_runner_passes_registered_calibration_to_ready_preflight(tmp_path: Path, monkeypatch) -> None:
    contigs = [(f"chr{i}", 248_956_422 if i == 1 else 1_000_000) for i in range(1, 23)]
    contigs += [("chrX", 1_000_000), ("chrY", 57_227_415)]
    ctx = context_for(tmp_path / "data", contigs, [Read("anonymous", "chr1", 100, "A" * 75)])
    summary, _ = fetal.load_summary(ctx)
    monkeypatch.setattr(fetal, "load_summary", lambda _ctx: (summary, None))
    store = LabStore(tmp_path / "root")
    path, digest = _file(tmp_path)
    contract = register_contract(store, path, digest)
    algorithm = replace(fetal.FETAL_FRACTION_CHRY, upstream=(),
                        requires=Requirements(min_eligible_reads=0))
    original_preflight = algorithm.preflight
    seen = []

    def inspect_planned_assets(run_ctx):
        decision = original_preflight(run_ctx)
        seen.append((decision, run_ctx.extras.get("planned_asset_sha256s")))
        return Skip("unsupported_input", "preflight verified without a worker")

    monkeypatch.setattr(algorithm, "preflight", inspect_planned_assets)
    target = RunTarget(INPUT, SOURCE, ctx.input_dir, "hg38", REFERENCE,
                       eligible_reads=10, approx_depth_x=0.01)
    run_id = new_run_id()
    results = run_plan(store, None, store.run_log(run_id), target,
                       [PlanItem(algorithm, False)], {}, run_id=run_id,
                       scrubber=Scrubber(root=store.root))
    assert len(results) == 1 and results[0].status == "skipped"
    assert seen == [(Ready(), [contract.manifest_sha256])]
