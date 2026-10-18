"""Synthetic registration, refusal, and NIPTeR adapter mechanics."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.algorithms import fetal_fraction as fetal
from traceback_lab.cli import main as lab_main
from traceback_lab.fetal_contract import FetalContractError, inspect_contract, register_contract
from traceback_lab.pipeline import Stage, run_pipeline
from traceback_lab.registry import InputContext, PlanItem, Ready, Skip
from traceback_lab.runner import RunTarget, run_plan
from traceback_lab.scrub import Scrubber
from traceback_lab.store import LabStore, new_run_id


SOURCE = "a" * 64
INPUT = "b" * 64
REFERENCE = "c" * 64


def test_r_process_output_is_bounded(tmp_path: Path) -> None:
    result = fetal._run_bounded_r(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000); sys.stderr.write('y' * 100000)"],
        cwd=tmp_path, timeout_seconds=10, log_limit_bytes=4096)
    assert result.outcome == "exited" and result.returncode == 0
    assert result.stdout_truncated and len(result.stdout) == 4096


@pytest.mark.skipif(os.name == "nt", reason="inherited pipe regression uses POSIX cleanup")
def test_r_process_rejects_pipe_held_open_by_descendant(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    launcher = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "sys.stdout.write('13\\t1\\n18\\t2\\n21\\t3\\nEND\\n')\n"
        "sys.stdout.flush()\n"
    )
    try:
        result = fetal._run_bounded_r(
            [sys.executable, "-c", launcher, str(child_pid)],
            cwd=tmp_path, timeout_seconds=10)
        assert result.outcome == "pipe_unclosed"
        assert result.stdout_truncated
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="Lab process-group cancellation uses POSIX groups")
def test_outer_lab_timeout_kills_r_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "r-ticks.txt"
    child = tmp_path / "r-child.py"
    child.write_text(
        "import sys, time\n"
        "with open(sys.argv[1], 'a') as out:\n"
        "    while True:\n"
        "        out.write('tick\\n'); out.flush(); time.sleep(0.05)\n")
    worker = tmp_path / "lab-worker.py"
    worker.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd()))\n"
        "from traceback_lab.algorithms.fetal_fraction import _run_bounded_r\n"
        f"_run_bounded_r([sys.executable, {str(child)!r}, {str(marker)!r}], "
        f"cwd=Path({str(tmp_path)!r}), timeout_seconds=90)\n")
    def wait_for_child(_elapsed: float) -> None:
        deadline = time.monotonic() + 60
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.is_file(), "R descendant did not start before the test deadline"

    result = run_pipeline([Stage("prenatal-worker", [sys.executable, str(worker)])],
                          timeout_s=2, cwd=Path.cwd(), on_tick=wait_for_child)
    assert result.timed_out
    assert marker.is_file()
    stopped_at = marker.stat().st_size
    time.sleep(0.3)
    assert marker.stat().st_size == stopped_at


@pytest.mark.skipif(os.name == "nt", reason="Lab process-group cleanup uses POSIX groups")
def test_inner_r_timeout_reaps_descendant_after_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .fake_algorithms import INNER_R_TIMEOUT

    marker = tmp_path / "inner-r-ticks.txt"
    monkeypatch.setenv("LAB_TEST_INNER_R_TICKS", str(marker))
    store = LabStore(tmp_path / "root")
    store.ensure()
    run_id = new_run_id()
    target = RunTarget(INPUT, SOURCE, tmp_path, "hg38", REFERENCE)
    results = run_plan(
        store, None, store.run_log(run_id), target,
        [PlanItem(INNER_R_TIMEOUT, False)],
        {INNER_R_TIMEOUT.id: "tests.lab.fake_algorithms:INNER_R_TIMEOUT"},
        run_id=run_id, timeout_s=20, scrubber=Scrubber(root=store.root),
    )
    assert len(results) == 1 and results[0].status == "failed"
    saved = store.read_result(INPUT, INNER_R_TIMEOUT.id, results[0].parameters_sha256)
    assert saved is not None and saved.failure is not None
    assert not saved.failure.timed_out, "the inner timeout must finish before the outer deadline"
    assert marker.is_file(), "the R descendant must run before the inner timeout"
    stopped_at = marker.stat().st_size
    time.sleep(0.5)
    assert marker.stat().st_size == stopped_at
    assert not (store.lab / "tmp" / run_id).exists()


@pytest.mark.skipif(os.name == "nt", reason="planning probe process groups use POSIX cleanup")
def test_planning_and_preflight_package_probes_kill_launcher_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, _ = _contract(tmp_path)
    store = LabStore(tmp_path / "root")
    _stored_input(store)
    register_contract(store, path, digest)
    marker, pids = tmp_path / "ticks", tmp_path / "pids"
    child = tmp_path / "ticker.py"
    child.write_text(
        "import sys, time\n"
        "with open(sys.argv[1], 'ab') as out:\n"
        "    while True:\n"
        "        out.write(b'tick\\n'); out.flush(); time.sleep(0.05)\n"
    )
    launcher = tmp_path / "Rscript"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}, {str(marker)!r}])\n"
        f"with open({str(pids)!r}, 'a') as out: out.write(str(child.pid) + '\\n')\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "for _ in range(100):\n"
        "    if marker.exists(): break\n"
        "    time.sleep(0.01)\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(fetal, "_rscript", lambda: (launcher, "d" * 64))
    try:
        assets = fetal.FETAL_FRACTION.asset_sha256s_for_input(store.root, INPUT)
        assert len(assets) == 4 and marker.exists()
        ctx = InputContext(store.root, SOURCE, INPUT, "hg38", REFERENCE, tmp_path,
                           eligible_reads=2_000_000, extras={"planned_asset_sha256s": assets})
        assert fetal.FETAL_FRACTION.preflight(ctx).reason_code == "missing_toolchain"
        stopped_at = marker.stat().st_size
        time.sleep(0.3)
        assert marker.stat().st_size == stopped_at
    finally:
        if pids.exists():
            for pid in pids.read_text().splitlines():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


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


def _stored_input(store: LabStore, *, source_id: str = SOURCE,
                  reference_sha256: str = REFERENCE) -> None:
    store.ensure()
    directory = store.inputs_dir / INPUT
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps({
        "input_id": INPUT, "source_id": source_id,
        "reference_sha256": reference_sha256,
    }))


def test_contract_is_immutable_and_rehashes_external_control(tmp_path: Path) -> None:
    path, digest, control = _contract(tmp_path)
    store = LabStore(tmp_path / "root")
    with pytest.raises(FetalContractError, match="expected contract SHA"):
        register_contract(store, path, "x")
    with pytest.raises(FetalContractError, match="stored input manifest"):
        register_contract(store, path, digest)
    _stored_input(store)
    registered = register_contract(store, path, digest)
    assert registered == inspect_contract(store, INPUT)
    assert register_contract(store, path, digest) == registered
    control.write_bytes(b"changed")
    with pytest.raises(FetalContractError, match="differs"):
        inspect_contract(store, INPUT)


def test_fetal_cli_register_and_inspect_keep_external_path_private(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, digest, control = _contract(tmp_path)
    root = tmp_path / "root"
    _stored_input(LabStore(root))
    assert lab_main(["fetal", "register", "--file", str(path), "--sha256", digest,
                     "--root", str(root), "--json"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["data"]["manifest_sha256"] == digest
    assert str(control) not in json.dumps(registered)
    assert lab_main(["fetal", "inspect", INPUT, "--root", str(root), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["data"]["control_n"] == 20
    assert str(control) not in json.dumps(inspected)


def test_prenatal_cli_refuses_duplicate_attestation_before_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path, _, _ = _contract(tmp_path)
    raw = path.read_bytes().replace(
        b'"control_euploid_attested": true',
        b'"control_euploid_attested": false, "control_euploid_attested": true',
    )
    path.write_bytes(raw)
    store = LabStore(tmp_path / "root")
    _stored_input(store)
    assert lab_main(["fetal", "register", "--file", str(path),
                     "--sha256", hashlib.sha256(raw).hexdigest(),
                     "--root", str(store.root), "--json"]) != 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "invalid JSON" in response["data"]["cause"]
    assert not (store.lab / "assets" / "fetal-fraction" / INPUT).exists()


def test_prenatal_cli_reports_uncertain_publication_and_inspect_recovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, control = _contract(tmp_path)
    store = LabStore(tmp_path / "root")
    _stored_input(store)
    publish = LabStore.publish_directory

    def publish_then_fail(self: LabStore, parent: Path, name: str, build) -> Path:
        published = publish(self, parent, name, build)
        raise OSError(f"private fsync failure after publishing {published}")

    monkeypatch.setattr(LabStore, "publish_directory", publish_then_fail)
    assert lab_main(["fetal", "register", "--file", str(path), "--sha256", digest,
                     "--root", str(store.root), "--json"]) == 3
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert response["summary"] == "Prenatal contract publication uncertain"
    assert "inspect registration before retrying" in response["data"]["cause"]
    assert str(path) not in json.dumps(response)
    assert str(control) not in json.dumps(response)
    assert inspect_contract(store, INPUT).manifest_sha256 == digest


@pytest.mark.parametrize("manifest_change", [
    {"source_id": "d" * 64}, {"reference_sha256": "e" * 64},
])
def test_prenatal_registration_refuses_wrong_stored_input_binding(
    tmp_path: Path, manifest_change: dict,
) -> None:
    store = LabStore(tmp_path / "root")
    _stored_input(store, **manifest_change)
    path, digest, _ = _contract(tmp_path)
    with pytest.raises(FetalContractError, match="does not match stored input provenance"):
        register_contract(store, path, digest)
    assert not (store.lab / "assets" / "fetal-fraction" / INPUT).exists()
    _stored_input(store)
    assert register_contract(store, path, digest).manifest_sha256 == digest


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
    _stored_input(store)
    contract = register_contract(store, path, digest)
    runtime = tmp_path / "Rscript"
    runtime.write_bytes(b"synthetic executable stand-in")
    binary_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    monkeypatch.setattr(fetal, "_rscript", lambda: (runtime, binary_sha))
    monkeypatch.setattr(fetal, "_nipter_package_sha", lambda path, **_kwargs: "e" * 64)
    monkeypatch.setattr(fetal, "_compatible_bam_layout", lambda path: True)
    assets = [contract.manifest_sha256, contract.control_sha256, fetal.SCRIPT_SHA256, binary_sha, "e" * 64]
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "aligned.bam").write_bytes(b"synthetic BAM stand-in")
    ctx = InputContext(store.root, SOURCE, INPUT, "hg38", REFERENCE, input_dir,
                       eligible_reads=2_000_000, extras={"planned_asset_sha256s": assets})
    assert isinstance(fetal.FETAL_FRACTION.preflight(ctx), Ready)
    with monkeypatch.context() as patch:
        patch.setattr(fetal, "_rscript", lambda: None)
        assert fetal.FETAL_FRACTION.preflight(ctx).reason_code == "missing_toolchain"
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "eligible_reads": 1_999_999})).reason_code == "depth"
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "derived": {"transform": "split", "half": "A"}})).reason_code == "unsupported_input"
    assert fetal.FETAL_FRACTION.preflight(ctx.__class__(
        **{**ctx.__dict__, "extras": {"planned_asset_sha256s": []}})).reason_code == "missing_asset"
    with pytest.raises(RuntimeError, match="reference changed"):
        fetal.FETAL_FRACTION.run(replace(ctx, reference_sha256="f" * 64), tmp_path / "out")
    with pytest.raises(RuntimeError, match="verified calibrated parent"):
        fetal.FETAL_FRACTION.run(replace(ctx, derived={"transform": "split", "half": "A"}), tmp_path / "out")
    def fake_r_process(argv, **kwargs):
        assert Path(argv[2]).read_bytes() == fetal.R_CODE.encode("utf-8")
        assert Path(argv[4]).read_bytes() == control.read_bytes()
        assert Path(argv[4]) != control
        assert Path(argv[4]).parent != tmp_path / "out"
        assert kwargs["log_limit_bytes"] == 4096
        assert kwargs["timeout_seconds"] == 900
        return SimpleNamespace(outcome="exited", returncode=0,
                               stdout=b"13\t0.5\n18\t-0.25\n21\t1.75\nEND\n", stdout_truncated=False)
    monkeypatch.setattr(fetal, "_run_bounded_r", fake_r_process)
    output = fetal.FETAL_FRACTION.run(ctx, tmp_path / "out")
    values = {item["metric_id"]: item["value"] for item in output.metrics}
    assert values == {"fetal_fraction": None, "trisomy_13_z": 0.5,
                      "trisomy_18_z": -0.25, "trisomy_21_z": 1.75}
    assert not (tmp_path / "out" / "nipter-z.R").exists()
    assert not list((tmp_path / "out").glob("nipter-control-*.rds"))
    assert not list(tmp_path.glob("nipter-private-*"))
    control.write_bytes(b"changed")
    assert isinstance(fetal.FETAL_FRACTION.preflight(ctx), Skip)
    with pytest.raises(FetalContractError):
        fetal.FETAL_FRACTION.run(ctx, tmp_path / "out")


@pytest.mark.parametrize("output", ["", "13\tnan\n18\t0\n21\t0\nEND\n",
                                     "13\t1\n18\t2\n22\t3\nEND\n",
                                     "13\t1\n18\t2\n21\t3\n",
                                     "13\t1\n18\t2\n21\t1.7"])
def test_nipter_output_parser_refuses_incomplete_or_nonfinite_values(output: str) -> None:
    with pytest.raises(RuntimeError):
        fetal._parse_z_output(output)


def test_nipter_control_snapshot_rejects_changed_bytes_without_retaining_copy(tmp_path: Path) -> None:
    control = tmp_path / "control.rds"
    control.write_bytes(b"private synthetic RDS stand-in")
    with pytest.raises(RuntimeError, match="changed during snapshot"):
        fetal._stage_control(control, "0" * 64, tmp_path)
    assert not list(tmp_path.glob("nipter-control-*.rds"))


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
    monkeypatch.setattr(fetal, "_run_bounded_r", lambda *args, **kwargs: SimpleNamespace(
        outcome="exited", returncode=0, stdout=lines.encode(), stdout_truncated=False))
    original = fetal._nipter_package_sha(tmp_path / "Rscript")
    assert original is not None
    helper.write_bytes(b"second")
    assert fetal._nipter_package_sha(tmp_path / "Rscript") != original
    helper.write_bytes(b"first")
    ambiguous = packages[0] / "a"
    ambiguous.write_bytes(b"x\x00b\x00y")
    one_file = fetal._nipter_package_sha(tmp_path / "Rscript")
    ambiguous.write_bytes(b"x")
    (packages[0] / "b").write_bytes(b"y")
    assert fetal._nipter_package_sha(tmp_path / "Rscript") != one_file
    helper.unlink()
    helper.symlink_to(tmp_path / "outside")
    assert fetal._nipter_package_sha(tmp_path / "Rscript") is None


def test_nipter_package_limit_counts_directories_before_sorting(tmp_path: Path, monkeypatch) -> None:
    packages = [tmp_path / name for name in ("NIPTeR", "sets", "Rsamtools", "S4Vectors")]
    for package in packages:
        package.mkdir()
        (package / "DESCRIPTION").write_bytes(b"installed package")
    lines = "\n".join(f"{version}\t{package}" for version, package in
                      zip(("1.0.2", "1.0-25", "2.22.0", "0.44.0"), packages))
    monkeypatch.setattr(fetal, "_run_bounded_r", lambda *args, **kwargs: SimpleNamespace(
        outcome="exited", returncode=0, stdout=lines.encode(), stdout_truncated=False))
    monkeypatch.setattr(fetal, "MAX_PACKAGE_ENTRIES", 4)
    assert fetal._nipter_package_sha(tmp_path / "Rscript") is not None
    (packages[0] / "empty").mkdir()
    assert fetal._nipter_package_sha(tmp_path / "Rscript") is None


def test_nipter_requires_canonical_hg38_bam_header(tmp_path: Path) -> None:
    import pysam

    names = [f"chr{chrom}" for chrom in range(1, 23)] + ["chrX", "chrY"]
    lengths = list(fetal._HG38_LENGTHS)
    path = tmp_path / "aligned.bam"
    def write() -> None:
        with pysam.AlignmentFile(str(path), "wb", header={"HD": {"VN": "1.6", "SO": "coordinate"},
                                                        "SQ": [{"SN": name, "LN": length}
                                                               for name, length in zip(names, lengths)]}):
            pass
    write()
    assert fetal._compatible_bam_layout(path)
    lengths[12] -= 1  # chr13 is one of the reported z-scores
    write()
    assert not fetal._compatible_bam_layout(path)
    lengths[12] += 1
    with pysam.AlignmentFile(str(path), "wb", header={"HD": {"VN": "1.6", "SO": "unsorted"},
                                                  "SQ": [{"SN": name, "LN": length}
                                                         for name, length in zip(names, lengths)]}):
        pass
    assert not fetal._compatible_bam_layout(path)
    names[1], names[2] = names[2], names[1]
    write()
    assert not fetal._compatible_bam_layout(path)
    names[1], names[2] = names[2], names[1]
    lengths[23] -= 1
    write()
    assert not fetal._compatible_bam_layout(path)
