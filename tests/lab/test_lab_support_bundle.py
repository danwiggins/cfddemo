"""``traceback lab support-bundle``: members, manifest, scrubbing, refusals, --json."""

from __future__ import annotations

import getpass
import gzip
import hashlib
import json
import socket
import tarfile
from pathlib import Path

import pytest

from traceback_lab import STORE_VERSION, __version__
from traceback_lab.envelope import ResultEnvelope, provenance
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli

A = "a" * 64  # parameters_sha256
B = "b" * 64  # source_id
C = "c" * 64  # input_id
RUN_ID = "r-20261106-0910-4e2d"


def _run(capsys, argv):
    code = signed_cli.main(["lab", *argv])
    return code, capsys.readouterr().out


def _failed_envelope(stderr_tail: list[str]) -> ResultEnvelope:
    return ResultEnvelope.model_validate(
        {
            "algorithm_id": "fragment-size",
            "algorithm_version": "1",
            "status": "failed",
            "provenance": provenance(
                tool="traceback_lab.test", parameters_sha256=A, source_id=B, input_id=C,
                reference_id="ref", timestamp_utc="2026-11-06T09:10:00Z",
            ),
            "failure": {"first_line": stderr_tail[0], "stderr_tail": stderr_tail, "exit_code": 1},
        }
    )


def _world(tmp_path: Path) -> tuple[LabStore, str, str]:
    """A store with one run: align ok, fragment-size failed (attempt retained),
    wps failed (attempt pruned).  Returns the store, the login and the host name."""

    login = getpass.getuser()
    host = socket.gethostname()
    store = LabStore(tmp_path / "root")
    store.ensure()
    raw = [
        f"[E::hts_open] fail to open {tmp_path}/DonorName_plasma.bam: truncated",
        f"worker on {host} as {login}: exit 1",
    ]
    attempt_id = store.publish_result(
        _failed_envelope(raw), input_id=C,
        artifacts={"tool.log": f"log from {host} for {login} at {tmp_path}/x\n".encode(),
                   "blob.bin": b"\x00\x01\x02binary\x00"},
    )
    log = store.run_log(RUN_ID)
    log.append("run_start", command="run", sample="3f9a1c2b7e04")
    log.append("step_start", step="align")
    log.append("step_end", step="align", status="ok", duration_s=12.0, input_id=C)
    log.append("step_start", step="fragment-size", parameters_sha256=A)
    log.append("step_end", step="fragment-size", status="failed", first_line=raw[0][:300],
               parameters_sha256=A, input_id=C, attempt_id=attempt_id)
    log.append("step_start", step="wps", parameters_sha256=A)
    log.append("step_end", step="wps", status="failed", first_line="boom", parameters_sha256=A,
               input_id=C, attempt_id="a-20261106T091000000000-deadbeef")
    log.append("run_end", status="partial_failure", exit_code=8)
    return store, login, host


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as archive:
        out = {}
        for info in archive.getmembers():
            assert info.isfile() and info.uname == "" and info.gname == "" and info.uid == 0 and info.gid == 0
            out[info.name] = archive.extractfile(info).read()
    return out


def test_bundle_members_and_manifest_match(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert out.count("\n") == 1 and payload["schema_version"] == "traceback.cli-result.v2"
    data = payload["data"]
    bundle = store.lab / "support" / f"{RUN_ID}.tar.gz"
    assert data["root"] == str(store.root) and data["bundle"] == str(bundle)
    assert bundle.is_file() and data["bytes"] == bundle.stat().st_size
    members = _members(bundle)
    attempt = data["failed_steps"][0]["attempt_id"]
    expected = {
        f"{RUN_ID}/run.jsonl", f"{RUN_ID}/doctor.json", f"{RUN_ID}/versions.json",
        f"{RUN_ID}/failed/fragment-size/{attempt}/result.json",
        f"{RUN_ID}/failed/fragment-size/{attempt}/artifacts.json",
        f"{RUN_ID}/failed/fragment-size/{attempt}/artifacts/tool.log",
        f"{RUN_ID}/MANIFEST.json",
    }
    assert set(members) == expected and data["members"] == list(members)
    assert data["members"][-1] == f"{RUN_ID}/MANIFEST.json"
    manifest = json.loads(members[f"{RUN_ID}/MANIFEST.json"])
    assert manifest["schema"] == "traceback.lab.support-bundle.v1" and manifest["run_id"] == RUN_ID
    listed = {entry["name"]: entry for entry in manifest["members"]}
    assert set(listed) == expected - {f"{RUN_ID}/MANIFEST.json"}
    for name, entry in listed.items():
        assert entry["bytes"] == len(members[name]) and entry["sha256"] == hashlib.sha256(members[name]).hexdigest()
    steps = {entry["step"]: entry for entry in manifest["failed_steps"]}
    assert steps["fragment-size"]["attempt_present"] is True and steps["fragment-size"]["files"] == 3
    assert steps["wps"]["attempt_present"] is False and steps["wps"]["files"] == 0
    assert [entry["reason"] for entry in manifest["skipped"]] == ["binary"]
    assert manifest["skipped"][0]["name"].endswith("/artifacts/blob.bin")
    versions = json.loads(members[f"{RUN_ID}/versions.json"])
    assert versions["traceback"] == __version__ and versions["store_version"] == STORE_VERSION
    assert versions["samtools"].startswith("samtools") and versions["minimap2"] not in {"missing", "unreadable"}
    doctor = json.loads(members[f"{RUN_ID}/doctor.json"])
    assert doctor["command"] == "lab doctor" and doctor["data"]["root"] == "<ROOT>" and "_human" not in doctor
    # The run log is the store's file, line for line (it holds hashes only).
    assert len(members[f"{RUN_ID}/run.jsonl"].splitlines()) == 8


def test_failed_step_log_is_included_and_scrubbed(tmp_path: Path, capsys) -> None:
    store, login, host = _world(tmp_path)
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root)])
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"ROOT  {store.root}" and lines[1].startswith("PASS  support bundle written")
    assert any(line.startswith("BUNDLE   ") for line in lines) and any(line.startswith("SIZE     ") for line in lines)
    members = _members(store.lab / "support" / f"{RUN_ID}.tar.gz")
    attempt = next(name for name in members if name.endswith("/result.json"))
    result = json.loads(members[attempt])
    assert result["failure"]["stderr_tail"][0] == "[E::hts_open] fail to open <path>: truncated"
    assert "<redacted>" in result["failure"]["stderr_tail"][1]
    log = members[attempt.replace("result.json", "artifacts/tool.log")].decode()
    assert log == "log from <redacted> for <redacted> at <path>\n"
    for name, content in members.items():
        text = content.decode("utf-8")
        assert str(tmp_path) not in text, name
        assert str(store.root) not in text, name
        assert "DonorName" not in text, name
        if len(login) >= 4:  # a two-letter login can be a substring of ordinary words
            assert login not in text, name
        if len(host) >= 4:
            assert host not in text, name
            assert host.split(".", 1)[0] not in text, name
    # The gzip header (no FNAME) and the tar headers (no owner) leak nothing either.
    raw_gz = (store.lab / "support" / f"{RUN_ID}.tar.gz").read_bytes()
    assert raw_gz[:2] == b"\x1f\x8b" and not raw_gz[3] & 0x08
    raw_tar = gzip.decompress(raw_gz)
    for needle in (str(tmp_path), "DonorName", *( [login] if len(login) >= 4 else []), *([host] if len(host) >= 4 else [])):
        assert needle.encode() not in raw_tar, needle


@pytest.mark.parametrize("as_json", [False, True])
def test_unknown_run_is_tbx_lab_028(tmp_path: Path, capsys, as_json: bool) -> None:
    store, _, _ = _world(tmp_path)
    argv = ["support-bundle", "r-20261106-0000-0000", "--root", str(store.root)] + (["--json"] if as_json else [])
    code, out = _run(capsys, argv)
    assert code == 4
    if as_json:
        data = json.loads(out)["data"]
        assert data["code"] == "TBX-LAB-028" and data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-028"
        assert data["candidates"] == [RUN_ID] and data["retryable"] is False
    else:
        lines = out.splitlines()
        assert lines[0] == f"ROOT  {store.root}"
        for label in ("CODE       TBX-LAB-028", "CAUSE", "FIX", "RETRYABLE  no", "DOCS"):
            assert any(line.startswith(label) for line in lines), label
    assert not (store.lab / "support").exists()
    code, out = _run(capsys, ["support-bundle", "not-a-run-id", "--root", str(store.root), "--json"])
    assert code == 4 and json.loads(out)["data"]["code"] == "TBX-LAB-028"


def test_refuses_to_overwrite_unless_force(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    out_path = tmp_path / "elsewhere" / "bundle.tar.gz"
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--json"])
    assert code == 0 and json.loads(out)["data"]["bundle"] == str(out_path)
    first = out_path.read_bytes()
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--json"])
    data = json.loads(out)["data"]
    assert code == 2 and data["code"] == "TBX-LAB-029" and data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-029"
    assert "--force" in data["fix"] and str(tmp_path) not in json.dumps({k: v for k, v in data.items() if k != "root"})
    assert out_path.read_bytes() == first
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--force", "--json"])
    assert code == 0 and out_path.is_file()
    (tmp_path / "first.tar.gz").write_bytes(first)
    assert set(_members(out_path)) == set(_members(tmp_path / "first.tar.gz"))
    assert not [path for path in out_path.parent.iterdir() if path.name.startswith(".")], "no temp file left behind"
