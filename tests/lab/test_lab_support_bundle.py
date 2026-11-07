"""``traceback lab support-bundle``: members, manifest, allowlist, scrubbing, refusals, --json."""

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
READ_NAME = "m64011_240101_000001/4194305/ccs"  # a synthetic read name
VCF = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n"


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


def _world(tmp_path: Path, *, stderr: list[str] | None = None) -> tuple[LabStore, str, str]:
    """A store with one run: align ok, fragment-size failed (attempt retained, with
    artifacts that must never ship), wps failed (attempt pruned)."""

    login = getpass.getuser()
    host = socket.gethostname()
    store = LabStore(tmp_path / "root")
    store.ensure()
    raw = stderr or [
        f"[E::hts_open] fail to open {tmp_path}/DonorName_plasma.bam: truncated",
        f"worker on {host} as {login}: exit 1",
    ]
    attempt_id = store.publish_result(
        _failed_envelope(raw), input_id=C,
        artifacts={
            "tool.log": f"log from {host} for {login} at {tmp_path}/x\n".encode(),
            "blob.bin": b"\x00\x01\x02binary\x00",
            "fragments.tsv": f"read\tstart\tend\n{READ_NAME}\t100\t250\n".encode(),
            "calls.vcf": VCF.encode(),
        },
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


def _default_bundle(store: LabStore) -> Path:
    return store.lab / "support" / f"{RUN_ID}.tar.gz"


def test_bundle_members_and_manifest_match(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert out.count("\n") == 1 and payload["schema_version"] == "traceback.cli-result.v2"
    data = payload["data"]
    bundle = _default_bundle(store)
    assert data["root"] == str(store.root) and data["bundle"] == str(bundle)
    assert bundle.is_file() and data["bytes"] == bundle.stat().st_size
    members = _members(bundle)
    attempt = data["failed_steps"][0]["attempt_id"]
    prefix = f"{RUN_ID}/failed/fragment-size/{attempt}"
    expected = [
        f"{RUN_ID}/run.jsonl", f"{RUN_ID}/doctor.json", f"{RUN_ID}/versions.json",
        f"{prefix}/artifacts.json", f"{prefix}/result.json", f"{RUN_ID}/MANIFEST.json",
    ]
    assert list(members) == expected and data["members"] == expected
    manifest = json.loads(members[f"{RUN_ID}/MANIFEST.json"])
    assert manifest["schema"] == "traceback.lab.support-bundle.v1" and manifest["run_id"] == RUN_ID
    # Manifest names are the tar member names, in order, with the real bytes and digests.
    assert [entry["name"] for entry in manifest["members"]] == expected[:-1]
    for entry in manifest["members"]:
        content = members[entry["name"]]
        assert entry["bytes"] == len(content) and entry["sha256"] == hashlib.sha256(content).hexdigest()
    steps = {entry["step"]: entry for entry in manifest["failed_steps"]}
    assert steps["fragment-size"] == {**steps["fragment-size"], "attempt_present": True, "reason": None, "files": 2}
    assert steps["wps"] == {**steps["wps"], "attempt_present": False, "reason": "pruned", "files": 0}
    assert steps["wps"]["attempt_id"] == "a-20261106T091000000000-deadbeef"
    omitted = {entry["name"]: entry["reason"] for entry in manifest["omitted"]}
    assert omitted == {
        f"{prefix}/artifacts/{name}": "not_allowlisted" for name in ("blob.bin", "calls.vcf", "fragments.tsv", "tool.log")
    }
    assert data["omitted"] == manifest["omitted"]
    versions = json.loads(members[f"{RUN_ID}/versions.json"])
    assert versions["traceback"] == __version__ and versions["store_version"] == STORE_VERSION
    assert versions["samtools"].startswith("samtools") and versions["minimap2"] not in {"missing", "unreadable"}
    doctor = json.loads(members[f"{RUN_ID}/doctor.json"])
    assert doctor["command"] == "lab doctor" and doctor["data"]["root"] == "<ROOT>" and "_human" not in doctor
    # The run log keeps one canonical event per line (hashes only, re-serialised after scrubbing).
    log_lines = members[f"{RUN_ID}/run.jsonl"].splitlines()
    assert len(log_lines) == 8 and json.loads(log_lines[0])["event"] == "run_start"
    inventory = json.loads(members[f"{prefix}/artifacts.json"])
    assert set(inventory["files"]) == {"blob.bin", "calls.vcf", "fragments.tsv", "tool.log"}


def test_failed_step_log_is_included_and_scrubbed(tmp_path: Path, capsys) -> None:
    store, login, host = _world(tmp_path)
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root)])
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"ROOT  {store.root}" and lines[1].startswith("PASS  support bundle written")
    assert any(line.startswith("BUNDLE   ") for line in lines) and any(line.startswith("SIZE     ") for line in lines)
    members = _members(_default_bundle(store))
    result = json.loads(members[next(name for name in members if name.endswith("/result.json"))])
    assert result["failure"]["stderr_tail"][0] == "[E::hts_open] fail to open <path>: truncated"
    assert "<redacted>" in result["failure"]["stderr_tail"][1]
    secrets = [str(tmp_path), str(store.root), "DonorName", READ_NAME, "SAMPLE1", "#CHROM", "log from"]
    if len(login) >= 4:  # a two-letter login can be a substring of ordinary words
        secrets.append(login)
    if len(host) >= 4:
        secrets.extend([host, host.split(".", 1)[0]])
    for name, content in members.items():
        text = content.decode("utf-8")
        for needle in secrets:
            assert needle not in text, (name, needle)
    # The gzip header (no FNAME) and the tar headers (no owner) leak nothing either.
    raw_gz = _default_bundle(store).read_bytes()
    assert raw_gz[:2] == b"\x1f\x8b" and not raw_gz[3] & 0x08
    raw_tar = gzip.decompress(raw_gz)
    for needle in secrets:
        assert needle.encode() not in raw_tar, needle


def test_host_words_redact_free_text_only_and_non_ascii_login(tmp_path: Path, capsys, monkeypatch) -> None:
    from traceback_lab import scrub, support_bundle_command

    monkeypatch.setattr(support_bundle_command.socket, "gethostname", lambda: "lab")
    monkeypatch.setattr(scrub.getpass, "getuser", lambda: "jöhn")
    store, _, _ = _world(tmp_path, stderr=["worker on LAB.local as jöhn: exit 1", "lab-tools: fragment-size failed"])
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--json"])
    assert code == 0, out
    members = _members(_default_bundle(store))
    result = json.loads(members[next(name for name in members if name.endswith("/result.json"))])
    assert result["failure"]["stderr_tail"] == [
        "worker on <redacted>.local as <redacted>: exit 1", "<redacted>-tools: fragment-size failed",
    ]
    assert result["algorithm_id"] == "fragment-size" and result["provenance"]["tool"] == "traceback_lab.test"
    doctor = json.loads(members[f"{RUN_ID}/doctor.json"])
    names = {check["name"] for check in doctor["data"]["checks"]}
    assert {"lab_samtools", "lab_minimap2"} <= names  # identifiers are never rewritten
    assert doctor["command"] == "lab doctor"  # an identifier, not free text
    assert "<redacted>" in doctor["summary"] and "lab" not in doctor["summary"].lower()  # free text
    log_lines = [json.loads(line) for line in members[f"{RUN_ID}/run.jsonl"].splitlines()]
    assert log_lines[0]["command"] == "run" and log_lines[0]["event"] == "run_start"
    assert log_lines[4]["first_line"] == "worker on <redacted>.local as <redacted>: exit 1"
    for name in members:
        assert name.startswith(f"{RUN_ID}/") and "<redacted>" not in name
    assert "jöhn" not in gzip.decompress(_default_bundle(store).read_bytes()).decode("utf-8")


def test_attempt_outside_root_is_omitted(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    attempts = store.result_dir(C, "fragment-size", A) / "attempts"
    outside = tmp_path / "outside" / "attempts"
    outside.parent.mkdir()
    attempts.rename(outside)
    attempt_id = next(path.name for path in outside.iterdir())
    marker = "OUTSIDE-ROOT-MARKER"
    (outside / attempt_id / "artifacts.json").write_text(json.dumps({"files": {}, "note": marker}))
    attempts.symlink_to(outside, target_is_directory=True)
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--json"])
    assert code == 0, out
    data = json.loads(out)["data"]
    entry = next(step for step in data["failed_steps"] if step["step"] == "fragment-size")
    assert entry["attempt_present"] is False and entry["reason"] == "outside_root" and entry["files"] == 0
    assert data["omitted"] == [{"name": f"{RUN_ID}/failed/fragment-size/{attempt_id}", "reason": "outside_root"}]
    members = _members(_default_bundle(store))
    assert not [name for name in members if "/failed/" in name]
    assert marker.encode() not in gzip.decompress(_default_bundle(store).read_bytes())


@pytest.mark.parametrize("as_json", [False, True])
def test_unknown_run_is_tbx_lab_028(tmp_path: Path, capsys, as_json: bool) -> None:
    store, _, _ = _world(tmp_path)
    argv = ["support-bundle", "r-20261106-0000-0000", "--root", str(store.root)] + (["--json"] if as_json else [])
    code, out = _run(capsys, argv)
    assert code == 4
    if as_json:
        data = json.loads(out)["data"]
        assert data["code"] == "TBX-LAB-028" and data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-028"
        assert data["candidates"] == [RUN_ID] and data["retryable"] is False and data["reason"] == "unknown_run"
    else:
        lines = out.splitlines()
        assert lines[0] == f"ROOT  {store.root}"
        for label in ("CODE       TBX-LAB-028", "CAUSE", "FIX", "RETRYABLE  no", "DOCS"):
            assert any(line.startswith(label) for line in lines), label
    assert not (store.lab / "support").exists()
    code, out = _run(capsys, ["support-bundle", "not-a-run-id", "--root", str(store.root), "--json"])
    assert code == 4 and json.loads(out)["data"]["code"] == "TBX-LAB-028"


def test_empty_run_log_is_tbx_lab_028_with_its_own_cause(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    empty = "r-20261106-0911-0a0b"
    (store.runs_dir / f"{empty}.jsonl").write_bytes(b"")
    code, out = _run(capsys, ["support-bundle", empty, "--root", str(store.root), "--json"])
    data = json.loads(out)["data"]
    assert code == 4 and data["code"] == "TBX-LAB-028" and data["reason"] == "empty_log"
    assert "no events" in data["cause"] and data["candidates"] == [RUN_ID]
    assert not (store.lab / "support").exists()


def test_refuses_to_overwrite_unless_force(tmp_path: Path, capsys) -> None:
    store, _, _ = _world(tmp_path)
    out_path = tmp_path / "elsewhere" / "bundle.tar.gz"
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--json"])
    assert code == 0 and json.loads(out)["data"]["bundle"] == str(out_path)
    first = out_path.read_bytes()
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--json"])
    data = json.loads(out)["data"]
    assert code == 2 and data["code"] == "TBX-LAB-029" and data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-029"
    assert "--force" in data["fix"] and data["is_directory"] is False
    assert str(tmp_path) not in json.dumps({k: v for k, v in data.items() if k != "root"})
    assert out_path.read_bytes() == first
    code, out = _run(capsys, ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(out_path), "--force", "--json"])
    assert code == 0 and out_path.is_file()
    (tmp_path / "first.tar.gz").write_bytes(first)
    assert list(_members(out_path)) == list(_members(tmp_path / "first.tar.gz"))
    assert not [path for path in out_path.parent.iterdir() if path.name.startswith(".")], "no temp file left behind"


@pytest.mark.parametrize("force", [False, True])
def test_out_at_a_directory_is_tbx_lab_029(tmp_path: Path, capsys, force: bool) -> None:
    store, _, _ = _world(tmp_path)
    directory = tmp_path / "a-directory"
    directory.mkdir()
    argv = ["support-bundle", RUN_ID, "--root", str(store.root), "--out", str(directory), "--json"]
    code, out = _run(capsys, argv + (["--force"] if force else []))
    data = json.loads(out)["data"]
    assert code == 2 and data["code"] == "TBX-LAB-029" and data["is_directory"] is True
    assert "directory" in data["cause"] and str(tmp_path) not in json.dumps({k: v for k, v in data.items() if k != "root"})
    assert directory.is_dir() and not list(directory.iterdir())
