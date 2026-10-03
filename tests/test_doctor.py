"""`traceback doctor` real checks (golden-path B7)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest

import traceback_runner.cli as cli
from tests.test_references import _write_fasta
from traceback_runner.cli import ExitCode, main
from traceback_runner.references import register_reference

_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture(autouse=True)
def _plenty_of_disk_and_samtools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: _Usage(10**13, 0, 500 * 1024**3)
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "samtools 1.21\nUsing htslib 1.21\n", ""),
    )


def _doctor(capsys, root: Path, *extra: str) -> tuple[int, dict]:
    code = main(["doctor", "--root", str(root), *extra, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _checks(payload: dict, name: str) -> list[dict]:
    return [check for check in payload["data"]["checks"] if check["name"] == name]


def _one(payload: dict, name: str) -> dict:
    (check,) = _checks(payload, name)
    return check


def test_doctor_prints_resolved_absolute_root_and_drops_real_data_block(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, payload = _doctor(capsys, Path("relative-root"))
    assert code == ExitCode.OK
    assert payload["data"]["root"] == str(Path.cwd() / "relative-root")
    assert os.path.isabs(payload["data"]["root"])
    assert not _checks(payload, "real_data")
    assert not (tmp_path / "relative-root").exists(), "doctor must not create ROOT"


def test_samtools_pass_and_warn_when_absent(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, payload = _doctor(capsys, tmp_path / "r")
    assert _one(payload, "samtools") == {
        "name": "samtools", "status": "pass", "detail": "samtools 1.21"
    }
    monkeypatch.setattr(shutil, "which", lambda name: None)
    code, payload = _doctor(capsys, tmp_path / "r")
    assert code == ExitCode.OK
    assert _one(payload, "samtools")["status"] == "warn"


def test_samtools_warns_when_version_does_not_parse(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "garbage", "")
    )
    _, payload = _doctor(capsys, tmp_path / "r")
    assert _one(payload, "samtools")["status"] == "warn"


def test_disk_pass_and_warn_under_ten_gib(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, payload = _doctor(capsys, tmp_path)
    assert _one(payload, "disk")["status"] == "pass"
    monkeypatch.setattr(shutil, "disk_usage", lambda path: _Usage(10**13, 0, 9 * 1024**3))
    code, payload = _doctor(capsys, tmp_path)
    assert code == ExitCode.OK
    assert _one(payload, "disk")["status"] == "warn"


def test_root_pass_and_warn_when_shared_writable(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    _, payload = _doctor(capsys, root)
    assert _one(payload, "root")["status"] == "pass"
    root.chmod(0o777)
    try:
        _, payload = _doctor(capsys, root)
    finally:
        root.chmod(0o700)
    assert _one(payload, "root")["status"] == "warn"


def test_root_warns_when_it_is_a_file(tmp_path: Path, capsys) -> None:
    root = tmp_path / "file"
    root.write_text("x")
    _, payload = _doctor(capsys, root)
    assert _one(payload, "root")["status"] == "warn"


def test_reference_pass_and_warn_when_fasta_missing(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    _, payload = _doctor(capsys, root)
    assert _one(payload, "reference")["status"] == "warn"  # none registered

    fasta = _write_fasta(tmp_path / "ref")
    register_reference(root, fasta, "tiny")
    _, payload = _doctor(capsys, root)
    check = _one(payload, "reference")
    assert check["status"] == "pass" and check["reference_id"] == "tiny"
    assert str(fasta) not in json.dumps(payload)

    fasta.rename(tmp_path / "moved.fa")
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.OK
    assert _one(payload, "reference")["status"] == "warn"
    assert "missing" in _one(payload, "reference")["detail"]


def test_reference_deep_detects_same_size_byte_change(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    fasta = _write_fasta(tmp_path / "ref")
    register_reference(root, fasta, "tiny")
    _, payload = _doctor(capsys, root, "--deep")
    assert _one(payload, "reference")["status"] == "pass"

    content = bytearray(fasta.read_bytes())
    content[-2:-1] = b"G" if content[-2:-1] != b"G" else b"C"
    fasta.write_bytes(bytes(content))
    _, shallow = _doctor(capsys, root)
    assert _one(shallow, "reference")["status"] == "pass"  # size unchanged
    _, deep = _doctor(capsys, root, "--deep")
    assert _one(deep, "reference")["status"] == "warn"


def test_trust_pass_on_empty_root_and_after_demo(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    _, payload = _doctor(capsys, root)
    assert _one(payload, "trust")["status"] == "pass"

    assert main(["demo", "--root", str(root), "--json"]) == ExitCode.OK
    capsys.readouterr()
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.OK
    assert _one(payload, "trust")["status"] == "pass"


def test_trust_blocked_when_records_exist_without_trust(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    assert main(["demo", "--root", str(root), "--json"]) == ExitCode.OK
    capsys.readouterr()
    (root / cli._TRUST_RELATIVE).unlink()
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.BLOCKED
    assert payload["status"] == "blocked"
    assert _one(payload, "trust")["status"] == "blocked"


def test_trust_blocked_when_result_trust_registry_does_not_open(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "root"
    assert main(["demo", "--root", str(root), "--json"]) == ExitCode.OK
    capsys.readouterr()
    (root / "trust" / "result-trust-registry").write_text("not a registry")
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.BLOCKED
    assert _one(payload, "trust")["status"] == "blocked"


def test_trust_passes_with_a_valid_result_trust_registry(tmp_path: Path, capsys) -> None:
    from evidence_inspector.result_trust_registry import ResultTrustRegistry

    root = tmp_path / "root"
    assert main(["demo", "--root", str(root), "--json"]) == ExitCode.OK
    capsys.readouterr()
    ResultTrustRegistry(root / "trust" / "result-trust-registry").close()
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.OK
    assert _one(payload, "trust")["status"] == "pass"


def test_reference_deep_warns_when_fasta_is_unreadable(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    fasta = _write_fasta(tmp_path / "ref")
    register_reference(root, fasta, "tiny")
    fasta.chmod(0)
    try:
        code, payload = _doctor(capsys, root, "--deep")
    finally:
        fasta.chmod(0o600)
    assert code == ExitCode.OK
    assert _one(payload, "reference") == {
        "name": "reference",
        "status": "warn",
        "detail": "registered FASTA could not be read",
        "reference_id": "tiny",
    }


@pytest.mark.parametrize("damage", ["metadata", "journal"])
def test_trust_blocked_when_registry_structure_is_damaged(
    tmp_path: Path, capsys, damage: str
) -> None:
    from evidence_inspector.result_trust_registry import ResultTrustRegistry

    root = tmp_path / "root"
    assert main(["demo", "--root", str(root), "--json"]) == ExitCode.OK
    capsys.readouterr()
    registry = root / "trust" / "result-trust-registry"
    ResultTrustRegistry(registry).close()
    if damage == "metadata":
        (registry / "registry-metadata.json").write_text("garbage")
    else:
        (registry / "registry-journal.jsonl").unlink()
    code, payload = _doctor(capsys, root)
    assert code == ExitCode.BLOCKED
    assert _one(payload, "trust")["status"] == "blocked"


def test_reference_warns_when_references_directory_is_unreadable(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "root"
    register_reference(root, _write_fasta(tmp_path / "ref"), "tiny")
    (root / "references").chmod(0)
    try:
        code, payload = _doctor(capsys, root)
    finally:
        (root / "references").chmod(0o700)
    assert code == ExitCode.OK
    assert _one(payload, "reference")["status"] == "warn"
    assert payload["data"]["root"] == str(root)
