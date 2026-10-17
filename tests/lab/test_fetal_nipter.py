"""Synthetic registration, refusal, and NIPTeR adapter mechanics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.algorithms import fetal_fraction as fetal
from traceback_lab.cli import main as lab_main
from traceback_lab.fetal_contract import FetalContractError, inspect_contract, register_contract
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id


SOURCE = "a" * 64
INPUT = "b" * 64
REFERENCE = "c" * 64


def _contract(tmp_path: Path, *, control_n: int = 20, sex: str = "unknown") -> tuple[Path, str, Path]:
    control = tmp_path / "control.rds"
    control.write_bytes(b"synthetic RDS stand-in; no patient data")
    data = {
        "schema": "traceback.lab.fetal-contract.v1", "source_id": SOURCE,
        "input_id": INPUT, "reference_sha256": REFERENCE, "pregnancy_confirmed": True,
        "fetal_sex": sex, "control_path": str(control),
        "control_sha256": hashlib.sha256(control.read_bytes()).hexdigest(),
        "control_n": control_n, "protocol_label": "synthetic matched protocol",
        "control_euploid_attested": True, "control_protocol_matched_attested": True,
        "nipter_version": "1.0.2",
    }
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), control


def test_contract_is_immutable_and_rehashes_external_control(tmp_path: Path) -> None:
    path, digest, control = _contract(tmp_path)
    store = LabStore(tmp_path / "root")
    with pytest.raises(FetalContractError, match="expected contract SHA"):
        register_contract(store, path, "x")
    registered = register_contract(store, path, digest)
    assert registered == inspect_contract(store, INPUT)
    assert register_contract(store, path, digest) == registered
    control.write_bytes(b"changed")
    with pytest.raises(FetalContractError, match="differs"):
        inspect_contract(store, INPUT)


def test_fetal_cli_register_and_inspect_keep_external_path_private(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, digest, control = _contract(tmp_path)
    root = tmp_path / "root"
    assert lab_main(["fetal", "register", "--file", str(path), "--sha256", digest,
                     "--root", str(root), "--json"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["data"]["manifest_sha256"] == digest
    assert str(control) not in json.dumps(registered)
    assert lab_main(["fetal", "inspect", INPUT, "--root", str(root), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["data"]["control_n"] == 20
    assert str(control) not in json.dumps(inspected)


@pytest.mark.parametrize("edit", [
    {"pregnancy_confirmed": False},
    {"control_euploid_attested": False},
    {"control_protocol_matched_attested": False},
    {"fetal_sex": "assumed"},
    {"control_n": 9},
    {"nipter_version": "2.0.0"},
])
def test_contract_rejects_missing_attestation_or_unsupported_model(tmp_path: Path, edit: dict) -> None:
    path, _, _ = _contract(tmp_path)
    data = json.loads(path.read_text())
    data.update(edit)
    path.write_text(json.dumps(data))
    with pytest.raises(FetalContractError):
        register_contract(LabStore(tmp_path / "root"), path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_nipter_preflight_and_synthetic_adapter_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, digest, control = _contract(tmp_path)
    store = LabStore(tmp_path / "root")
    contract = register_contract(store, path, digest)
    runtime = tmp_path / "Rscript"
    runtime.write_bytes(b"synthetic executable stand-in")
    binary_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    monkeypatch.setattr(fetal, "_rscript", lambda: (runtime, binary_sha))
    monkeypatch.setattr(fetal, "_nipter_package_sha", lambda path: "e" * 64)
    monkeypatch.setattr(fetal, "_compatible_bam_layout", lambda path: True)
    assets = [contract.manifest_sha256, contract.control_sha256, fetal.SCRIPT_SHA256, binary_sha, "e" * 64]
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").write_bytes(b"synthetic BAM stand-in")
    ctx = InputContext(store.root, SOURCE, INPUT, "hg38", REFERENCE, input_dir,
                       eligible_reads=2_000_000, extras={"planned_asset_sha256s": assets})
    assert isinstance(fetal.FETAL_FRACTION.preflight(ctx), Ready)
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "eligible_reads": 1_999_999})).reason_code == "depth"
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "derived": {"transform": "split", "half": "A"}})).reason_code == "unsupported_input"
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "extras": {"planned_asset_sha256s": []}})).reason_code == "missing_asset"
    def fake_r_process(argv, **kwargs):
        assert Path(argv[2]).read_bytes() == fetal.R_CODE.encode("utf-8")
        assert kwargs["log_limit_bytes"] == 4096
        assert kwargs["timeout_seconds"] == 900
        return SimpleNamespace(outcome="exited", returncode=0,
                               stdout=b"13\t0.5\n18\t-0.25\n21\t1.75\n", stdout_truncated=False)
    monkeypatch.setattr(fetal, "run_contained", fake_r_process)
    output = fetal.FETAL_FRACTION.run(ctx, tmp_path / "out")
    values = {item["metric_id"]: item["value"] for item in output.metrics}
    assert values == {"fetal_fraction": None, "trisomy_13_z": 0.5,
                      "trisomy_18_z": -0.25, "trisomy_21_z": 1.75}
    assert not (tmp_path / "out" / "nipter-z.R").exists()
    control.write_bytes(b"changed")
    assert isinstance(fetal.FETAL_FRACTION.preflight(ctx), Skip)
    with pytest.raises(FetalContractError):
        fetal.FETAL_FRACTION.run(ctx, tmp_path / "out")


@pytest.mark.parametrize("output", ["", "13\tnan\n18\t0\n21\t0\n", "13\t1\n18\t2\n22\t3\n"])
def test_nipter_output_parser_refuses_incomplete_or_nonfinite_values(output: str) -> None:
    with pytest.raises(RuntimeError):
        fetal._parse_z_output(output)


def test_derived_prenatal_skip_publishes_through_runner(tmp_path: Path) -> None:
    store = LabStore(tmp_path / "root")
    store.ensure()
    run_id = new_run_id()
    target = RunTarget(INPUT, SOURCE, tmp_path, "hg38", REFERENCE,
                       eligible_reads=2_000_000, derived={"transform": "split", "half": "A"})
    algorithm = replace(fetal.FETAL_FRACTION, upstream=())
    results = run_plan(store, None, store.run_log(run_id), target,
                       [PlanItem(algorithm, False)], {}, run_id=run_id,
                       scrubber=Scrubber(root=store.root))
    assert len(results) == 1 and results[0].status == "skipped"
    assert results[0].skip_reason == "unsupported_input"
    saved = store.read_result(INPUT, algorithm.id, results[0].parameters_sha256)
    assert saved is not None and saved.status == "skipped"
    assert saved.skip_reason == "unsupported_input"


def test_installed_nipter_package_files_are_exactly_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packages = [tmp_path / name for name in ("NIPTeR", "sets", "Rsamtools", "S4Vectors")]
    for package in packages:
        package.mkdir()
        (package / "DESCRIPTION").write_bytes(b"installed package")
    helper = packages[1] / "R-code"
    helper.write_bytes(b"first")
    lines = "\n".join(f"{version}\t{package}" for version, package in
                      zip(("1.0.2", "1.0-25", "2.22.0", "0.44.0"), packages))
    monkeypatch.setattr(fetal, "run_contained", lambda *args, **kwargs: SimpleNamespace(
        outcome="exited", returncode=0, stdout=lines.encode(), stdout_truncated=False))
    original = fetal._nipter_package_sha(tmp_path / "Rscript")
    assert original is not None
    helper.write_bytes(b"second")
    assert fetal._nipter_package_sha(tmp_path / "Rscript") != original
    helper.unlink()
    helper.symlink_to(tmp_path / "outside")
    assert fetal._nipter_package_sha(tmp_path / "Rscript") is None


def test_nipter_requires_canonical_hg38_bam_header(tmp_path: Path) -> None:
    import pysam

    names = [f"chr{chrom}" for chrom in range(1, 23)] + ["chrX", "chrY"]
    lengths = [248_956_422] + [1_000_000] * 22 + [57_227_415]
    path = tmp_path / "aligned.bam"
    def write() -> None:
        with pysam.AlignmentFile(str(path), "wb", header={"HD": {"VN": "1.6"},
                                                        "SQ": [{"SN": name, "LN": length}
                                                               for name, length in zip(names, lengths)]}):
            pass
    write()
    assert fetal._compatible_bam_layout(path)
    names[1], names[2] = names[2], names[1]
    write()
    assert not fetal._compatible_bam_layout(path)
    names[1], names[2] = names[2], names[1]
    lengths[23] -= 1
    write()
    assert not fetal._compatible_bam_layout(path)
