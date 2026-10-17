"""``traceback lab run`` end to end on synthetic uBAM chunks (D9, D20, D22, D36).

samtools and minimap2 are required on this tier (no skip-if-missing): CI
installs versions at the D9 minimums.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pysam
import pytest

from traceback_lab import registry
from traceback_lab import run_command as run_module
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli

REPO = Path(__file__).resolve().parents[2]
SAMTOOLS = shutil.which("samtools")
MINIMAP2 = shutil.which("minimap2")
FAKES = "tests.lab.fake_algorithms"


QUIET = ("--no-downsample", "--no-controls")


def _lab(capsys, *argv: str) -> tuple[int, str, str]:
    # S2-era tests check the main plan only; S5 covers the default derived phase.
    if argv and argv[0] == "run" and "--dry-run" not in argv and "--downsample" not in argv:
        argv = (*argv, *QUIET)
    code = signed_cli.main(["lab", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _json(capsys, *argv: str) -> tuple[int, dict]:
    code, out, _ = _lab(capsys, *argv, "--json")
    return code, json.loads(out)


def _registry(monkeypatch, *names: str) -> None:
    monkeypatch.setattr(registry, "ALGORITHMS", tuple(f"{FAKES}:{name}" for name in names))


def _fake_tool(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


def _path_with(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def _no_staging(root: Path) -> bool:
    return not list((root / "lab").rglob(".staging-*"))


def test_lab_run_missing_ichor_skips_with_install_command(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    from traceback_lab.algorithms import copy_number

    monkeypatch.setattr(registry, "ALGORITHMS", ("read_summary:READ_SUMMARY", "copy_number:COPY_NUMBER"))
    monkeypatch.setattr(copy_number, "toolchain_or_none", lambda: None)
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]),
                          "--mmi", str(shared_mmi))
    assert code == 0, payload
    data = payload["data"]
    copy_number_step = next(step for step in data["steps"] if step["algorithm_id"] == "copy-number")
    assert (copy_number_step["status"], copy_number_step["skip_reason"]) == ("skipped", "missing_toolchain")
    assert data["remediations"] == [{
        "algorithm_id": "copy-number", "reason": "missing_toolchain", "asset": "ichor",
        "command": "traceback toolchain install ichor",
        "argv": ["traceback", "toolchain", "install", "ichor"],
    }]
    human_code, human, _ = _lab(capsys, "run", "latest", "--root", str(lab_world["root"]),
                                "--mmi", str(shared_mmi))
    assert human_code == 0
    assert "SKIP    copy-number skipped (missing_toolchain): ichor: traceback toolchain install ichor" in human


def test_run_aligns_publishes_and_reruns_from_cache(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER")
    root, chunks = lab_world["root"], lab_world["chunks"]
    code, out, _ = _lab(capsys, "run", str(chunks), "--root", str(root))
    assert code == 0, out
    lines = out.splitlines()
    assert lines[0] == f"ROOT  {root}"
    block = lines[lines.index(f"ROOT    {root}") :]
    assert [line.split()[0] for line in block[:4]] == ["ROOT", "RUN", "SAMPLE", "DONE"]
    assert "2 ok" in block[3]
    assert block[-1].startswith("NEXT    traceback lab")

    store = LabStore(root)
    (input_dir,) = [p for p in store.inputs_dir.iterdir() if p.is_dir()]
    manifest = json.loads((input_dir / "manifest.json").read_bytes())
    assert manifest["records"] == {"ubam": 1800, "aligned_primary_plus_unmapped": 1800}
    assert manifest["tools"]["minimap2"] and manifest["tools"]["samtools"]
    assert manifest["align"]["peak_rss_bytes"] > 0
    with pysam.AlignmentFile(str(input_dir / "aligned.bam")) as bam:
        header = bam.header.to_dict()
        assert any("modbase_models=" in rg.get("DS", "") for rg in header["RG"])
        record = next(read for read in bam if not read.is_unmapped)
        assert record.has_tag("MM") and record.has_tag("ML") and record.has_tag("MN")
        assert record.get_tag("RG") == header["RG"][0]["ID"]
    assert (input_dir / "aligned.bam.bai").is_file()
    mmi = next((root / "lab" / "refs").glob("*.mmi"))
    built = mmi.stat().st_mtime_ns
    assert _no_staging(root)

    # Rerun: cached hashes, reused alignment and index, cached results; fast.
    started = time.monotonic()
    code, payload = _json(capsys, "run", str(chunks), "--root", str(root))
    assert time.monotonic() - started < 10
    assert code == 0
    assert all(step["cached"] for step in payload["data"]["steps"])
    assert mmi.stat().st_mtime_ns == built
    assert payload["data"]["sample"]["id12"] in out

    # Stored sample: no chunk paths needed.
    for name in ("latest", payload["data"]["sample"]["id12"]):
        code, again = _json(capsys, "run", name, "--root", str(root))
        assert code == 0 and again["data"]["sample"] == payload["data"]["sample"]

    # The run log names steps and hashes, never paths.
    for log in (root / "lab" / "runs").glob("*.jsonl"):
        text = log.read_text()
        assert str(chunks) not in text and "SYNTH0001" not in text


def test_sex_assertion_is_local_metadata_and_does_not_rekey_results(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER")
    root = lab_world["root"]
    args = ("run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    code, first = _json(capsys, *args)
    assert code == 0
    original = first["data"]["sample"]
    store = LabStore(root)
    generation = store.generation()
    result_bytes = {path.relative_to(store.results_dir): path.read_bytes()
                    for path in store.results_dir.rglob("result.json")}

    code, asserted = _json(capsys, "run", "latest", "--sex", "M", "--root", str(root))
    assert code == 0 and asserted["data"]["sample"] == original
    assert all(step["cached"] for step in asserted["data"]["steps"])
    assert store.generation() == generation + 1
    assert {path.relative_to(store.results_dir): path.read_bytes()
            for path in store.results_dir.rglob("result.json")} == result_bytes
    from traceback_lab.ingest import source_record
    from traceback_lab.views import load_sample, sex_check_view

    record = source_record(LabStore(root), original["source_id"])
    assert record is not None and record["operator_metadata"]["sex"] == "M"
    sample = load_sample(LabStore(root), original["source_id"])
    assert sample is not None
    assert sex_check_view(sample)["state"] == "no_verdict"  # fake registry has no sex-check

    code, conflict = _json(capsys, "run", "latest", "--sex", "F", "--root", str(root))
    assert code == 3 and conflict["data"]["code"] == "TBX-LAB-023"
    assert {"cause", "fix", "retryable", "docs"} <= set(conflict["data"])
    code, human, _ = _lab(capsys, "run", "latest", "--sex", "F", "--root", str(root))
    assert code == 3
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in human.splitlines())
    assert source_record(LabStore(root), original["source_id"]) == record
    code, same = _json(capsys, "run", "latest", "--sex", "M", "--root", str(root))
    assert code == 0 and same["data"]["sample"] == original
    assert store.generation() == generation + 1
    code, reingested = _json(capsys, *args)
    assert code == 0 and reingested["data"]["sample"] == original
    assert store.generation() == generation + 1
    assert source_record(LabStore(root), original["source_id"])["operator_metadata"] == record["operator_metadata"]

    code, conflict = _json(capsys, *args, "--sex", "F")
    assert code == 3 and conflict["data"]["code"] == "TBX-LAB-023"
    assert source_record(LabStore(root), original["source_id"])["operator_metadata"] == record["operator_metadata"]

    # Optional metadata corruption blocks a new assertion, but must not block
    # unrelated measurement work or disappear during source-record refresh.
    malformed = {"schema": "invalid", "sex": "M", "recorded_utc": "invalid"}
    source_file = store.sources_dir / f"{original['source_id']}.json"
    source_file.write_text(json.dumps({**record, "operator_metadata": malformed}))
    for target in ("latest", str(lab_world["chunks"])):
        extra = () if target == "latest" else ("--mmi", str(shared_mmi))
        code, dry = _json(capsys, "run", target, "--root", str(root), *extra, "--dry-run")
        assert code == 0 and dry["data"]["dry_run"]
        code, rerun = _json(capsys, "run", target, "--root", str(root), *extra)
        assert code == 0 and rerun["data"]["sample"] == original
        assert source_record(store, original["source_id"])["operator_metadata"] == malformed
        code, refused = _json(capsys, "run", target, "--root", str(root), *extra, "--sex", "M")
        assert code == 3 and refused["data"]["code"] == "TBX-LAB-023"


def test_missing_cached_upstream_artifact_recomputes_its_dependent(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER")
    root = lab_world["root"]
    code, first = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi),
    )
    assert code == 0
    input_id = first["data"]["sample"]["input_id"]
    store = LabStore(root)
    identity_dir = next((store.results_dir / input_id / "read-summary").iterdir())
    attempt = store.selected_attempt(input_id, "read-summary", identity_dir.name)
    assert attempt is not None
    dependent_identity = next((store.results_dir / input_id / "fragment-size").iterdir()).name
    prior_dependent = store.selected_attempt(input_id, "fragment-size", dependent_identity)
    assert prior_dependent is not None
    (attempt / "artifacts" / "counts.json").unlink()

    code, retry = _json(capsys, "run", "latest", "--root", str(root))
    assert code == 0
    steps = {step["algorithm_id"]: step for step in retry["data"]["steps"]}
    assert not steps["read-summary"]["cached"]
    assert not steps["fragment-size"]["cached"]
    replacement = store.selected_attempt(input_id, "read-summary", identity_dir.name)
    assert replacement is not None and replacement != attempt
    assert (replacement / "artifacts" / "counts.json").is_file()
    assert store.selected_attempt(input_id, "fragment-size", dependent_identity) != prior_dependent

def test_json_stdout_is_one_object_and_progress_goes_to_stderr(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    code, out, err = _lab(
        capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--json"
    )
    assert code == 0
    payload = json.loads(out)
    assert out.count("\n") == 1
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert set(payload["data"]) >= {"root", "run_id", "sample", "counts", "next", "failed", "controls"}
    assert "ROOT" in err and "align" in err


def test_failed_algorithm_exits_8_with_scrubbed_stderr_and_rerun_recomputes_only_it(
    lab_world, capsys, monkeypatch, shared_mmi
) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER", "FAILS", "DEPENDS_ON_FAILURE", "SKIPS")
    root = lab_world["root"]
    argv = ("run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    code, out, _ = _lab(capsys, *argv)
    assert code == 8
    assert "FAILED  copy-number:" in out
    assert "details: traceback lab runs" in out
    assert "1 skipped (depth: instability)" in out and "1 skipped (upstream: tf-concordance)" in out
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in out.splitlines()), label
    assert "CODE       TBX-LAB-016" in out
    code, payload = _json(capsys, *argv)
    assert code == 8
    data = payload["data"]
    assert payload["status"] == "partial_failure"
    assert data["code"] == "TBX-LAB-016" and data["docs"].endswith("#tbx-lab-016")
    assert "copy-number" in data["cause"] and data["fix"]
    assert isinstance(data["retryable"], bool)
    assert data["counts"] == {"ok": 2, "skipped": {"depth": 1, "upstream": 1}, "failed": 1}
    steps = {step["algorithm_id"]: step for step in data["steps"]}
    assert steps["fragment-size"]["cached"] and not steps["copy-number"]["cached"]
    store = LabStore(root)
    input_id = data["sample"]["input_id"]
    result_dirs = list((store.results_dir / input_id / "copy-number").iterdir())
    envelope = store.read_result(input_id, "copy-number", result_dirs[0].name)
    assert envelope.status == "failed" and envelope.metrics == []
    persisted = json.dumps(envelope.model_dump())
    assert "DonorName" not in persisted and "/Users/someone" not in persisted
    assert "<path>" in persisted


def test_algorithm_timeout_kills_its_grandchild(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "HANGS")
    pidfile = lab_world["tmp"] / "grandchild.pid"
    monkeypatch.setenv("LAB_TEST_PIDFILE", str(pidfile))
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--timeout-s", "3"
    )
    assert code == 8
    assert payload["data"]["failed"][0]["first_line"] == "timed out"
    grandchild = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild outlived the algorithm's timeout")


def test_only_unknown_suggests_and_only_adds_upstream(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SUMMARY", "COUNTER", "SECOND")
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--only", "fragmnt-size")
    assert code == 2 and payload["data"]["code"] == "TBX-LAB-001"
    assert "fragment-size" in payload["data"]["fix"]
    code, out, _ = _lab(
        capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--only", "fragment-size"
    )
    assert code == 0
    assert "read-summary             running (upstream)" in out
    assert "bam-qc" not in out


def test_dry_run_writes_nothing_and_ignores_the_lock(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    store = LabStore(root)
    before = sorted(str(p) for p in root.rglob("*"))
    with store.writer("cohort build", "r-20261014-2104-7f3a"):
        snapshot = sorted(str(p) for p in root.rglob("*"))
        code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--dry-run")
        assert sorted(str(p) for p in root.rglob("*")) == snapshot
    assert code == 0
    data = payload["data"]
    assert data["dry_run"] and data["chunks"]["count"] == 3
    assert data["alignment"] == "new source"
    assert data["plan"][0]["algorithm_id"] == "read-summary"
    assert data["budget"]["required_bytes"] > 0
    assert len(before) <= len(snapshot)


def test_stored_dry_run_uses_selected_depth_without_writing(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    monkeypatch.setattr(registry, "ALGORITHMS", (
        "read_summary:READ_SUMMARY", "delfi_ratio:DELFI_RATIO",
    ))
    root = lab_world["root"]
    code, first = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(root),
        "--mmi", str(shared_mmi), "--only", "read-summary",
    )
    assert code == 0, first
    store = LabStore(root)

    def snapshot() -> dict[str, str]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
        }

    with store.writer("other", "r-20261017-1200-aaab"):
        before = snapshot()
        monkeypatch.setattr(run_module.shutil, "disk_usage", lambda _root: SimpleNamespace(free=3 * 1024**3))
        code, payload = _json(
            capsys, "run", "latest", "--root", str(root),
            "--only", "delfi-ratio", "--dry-run",
        )
        human_code, human, _ = _lab(
            capsys, "run", "latest", "--root", str(root),
            "--only", "delfi-ratio", "--dry-run",
        )
        forced_code, forced = _json(
            capsys, "run", "latest", "--root", str(root),
            "--only", "delfi-ratio", "--dry-run", "--force",
        )
        unavailable = []
        for selected in (None, SimpleNamespace(status="failed", metrics=[])):
            with monkeypatch.context() as patch:
                patch.setattr(LabStore, "read_result", lambda _store, *_args: selected)
                missing_code, missing = _json(
                    capsys, "run", "latest", "--root", str(root),
                    "--only", "delfi-ratio", "--dry-run",
                )
                assert missing_code == 0
                unavailable.append(missing["data"])
        with monkeypatch.context() as patch:
            selected = SimpleNamespace(
                status="ok", metrics=[SimpleNamespace(metric_id="eligible_reads", value=2_000_000)],
            )
            patch.setattr(LabStore, "read_result", lambda _store, *_args: selected)
            met_code, met = _json(
                capsys, "run", "latest", "--root", str(root),
                "--only", "delfi-ratio", "--dry-run",
            )
        assert snapshot() == before
    assert code == human_code == forced_code == met_code == 0
    data = payload["data"]
    assert data["sample"]["id12"] == first["data"]["sample"]["id12"]
    assert data["sample"]["input_id"] == first["data"]["sample"]["input_id"]
    assert data["reference_id"] == "synth"
    assert data["chunks"]["count"] == 3 and data["chunks"]["bytes"] > 0
    assert data["alignment"] == "reused (stored sample)"
    assert data["budget"]["required_bytes"] == 0
    assert data["budget"]["free_bytes"] == 3 * 1024**3
    assert data["budget"]["analysis_scratch"] == "not predicted"
    assert data["eligible_reads"] < 1_000_000
    assert data["plan"] == [
        {"algorithm_id": "read-summary", "upstream": True, "decision": "reused (selected result)"},
        {
            "algorithm_id": "delfi-ratio", "upstream": False,
            "decision": "will skip (below eligible-read minimum)",
            "eligible_reads": data["eligible_reads"], "minimum_eligible_reads": 1_000_000,
        },
    ]
    assert "REFERENCE synth" in human
    assert "3 stored chunk(s)" in human
    assert "will skip (below eligible-read minimum)" in human
    assert "has 3.0 GB (reused)" in human
    assert "ANALYSIS  scratch disk not predicted" in human
    assert "eligible_reads" not in forced["data"]
    assert all(row["decision"] == "decided at run time" for row in forced["data"]["plan"])
    for missing in unavailable:
        assert "eligible_reads" not in missing
        assert all(row["decision"] == "decided at run time" for row in missing["plan"])
    assert met["data"]["plan"][1]["decision"] == "depth minimum met; other checks at run time"


def test_second_writer_is_refused(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch)
    store = LabStore(lab_world["root"])
    with store.writer("simulate", "r-20261014-2104-7f3a", step="split"):
        code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]))
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-012"
    assert payload["data"]["holder"]["command"] == "simulate"


def test_no_input_prints_the_quickstart(tmp_path, capsys) -> None:
    code, out, _ = _lab(capsys, "run", "--root", str(tmp_path))
    assert code == 2 and "Quickstart:" in out


def test_unknown_sample_is_tbx_lab_017(lab_world, capsys) -> None:
    code, payload = _json(capsys, "run", "aaaaaaaaaaaa", "--root", str(lab_world["root"]))
    assert code == 4 and payload["data"]["code"] == "TBX-LAB-017"
    code, payload = _json(capsys, "run", "latest", "--root", str(lab_world["root"]))
    assert code == 4


def test_short_disk_exits_3_before_writing_anything(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch)
    from traceback_lab import ingest

    class Usage:
        free = 1024

    monkeypatch.setattr(ingest.shutil, "disk_usage", lambda path: Usage)
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]))
    assert code == 3
    data = payload["data"]
    assert data["code"] == "TBX-LAB-010"
    assert data["step"] == "align" and data["free_bytes"] == 1024 and data["required_bytes"] > 1024
    assert not list(LabStore(lab_world["root"]).inputs_dir.glob("*/manifest.json"))
    assert not (lab_world["root"] / "lab" / "refs").exists()


def test_missing_minimap2_with_unaligned_input_is_tbx_lab_003(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch)
    only = lab_world["tmp"] / "only-samtools"
    only.mkdir()
    (only / "samtools").symlink_to(SAMTOOLS)
    monkeypatch.setenv("PATH", str(only))
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]))
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-003"
    assert payload["data"]["minimap2"] is None
    assert "brew install minimap2" in payload["data"]["fix"]


def test_old_samtools_is_tbx_lab_003(lab_world, capsys, monkeypatch) -> None:
    _registry(monkeypatch)
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(fakes, "samtools", f'if [ "$1" = "--version" ]; then echo "samtools 1.19.2"; exit 0; fi\nexec {SAMTOOLS} "$@"\n')
    _path_with(monkeypatch, fakes)
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-003"
    assert payload["data"]["samtools"] == "1.19.2"


def test_minimap2_failing_mid_stream_leaves_nothing(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(
        fakes,
        "minimap2",
        f"""
        case "$1" in --version) exec {MINIMAP2} --version;; esac
        head -c 20000 > /dev/null
        echo "[ERROR] simulated failure reading {lab_world['tmp']}/DonorName_index.mmi" >&2
        exit 1
        """,
    )
    _path_with(monkeypatch, fakes)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 3
    data = payload["data"]
    assert data["code"] == "TBX-LAB-014" and data["process"] == "minimap2"
    assert "DonorName" not in json.dumps(payload) and str(lab_world["tmp"]) not in data["first_line"]
    assert not list(LabStore(root).inputs_dir.glob("*/manifest.json"))
    assert _no_staging(root)


def test_sort_enospc_is_tbx_lab_011_and_removes_staging(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(
        fakes,
        "samtools",
        f"""
        if [ "$1" = "sort" ]; then
          cat > /dev/null
          echo "samtools sort: failed to create temporary file: No space left on device" >&2
          exit 1
        fi
        exec {SAMTOOLS} "$@"
        """,
    )
    _path_with(monkeypatch, fakes)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-011"
    assert payload["data"]["retryable"] is True
    assert _no_staging(root)


def test_read_count_mismatch_is_tbx_lab_013(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(
        fakes,
        "samtools",
        f"""
        if [ "$1" = "fastq" ]; then
          {SAMTOOLS} "$@" | awk 'NR <= 400'
          exit 0
        fi
        exec {SAMTOOLS} "$@"
        """,
    )
    _path_with(monkeypatch, fakes)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-013"
    assert payload["data"]["ubam_records"] == 1800 and payload["data"]["aligned_records"] == 100
    assert _no_staging(root)


def test_alignment_timeout_kills_a_grandchild(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    pidfile = lab_world["tmp"] / "mm2-grandchild.pid"
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(
        fakes,
        "minimap2",
        f"""
        case "$1" in --version) exec {MINIMAP2} --version;; esac
        sleep 600 &
        echo $! > {pidfile}
        sleep 600
        """,
    )
    _path_with(monkeypatch, fakes)
    monkeypatch.setattr(run_module, "ALIGN_TIMEOUT_S", 3.0)
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi)
    )
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-014"
    assert payload["data"]["timed_out"] is True
    grandchild = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("a grandchild of the alignment pipeline survived")
    assert _no_staging(lab_world["root"])


def test_aligner_version_change_warns_and_realigns(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    root = lab_world["root"]
    argv = ("run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    code, first = _json(capsys, *argv)
    assert code == 0
    first_input = first["data"]["sample"]["input_id"]
    before_inputs = {p.name for p in LabStore(root).inputs_dir.iterdir() if p.is_dir()}
    fakes = lab_world["tmp"] / "fakes"
    _fake_tool(fakes, "minimap2", f'if [ "$1" = "--version" ]; then echo "2.99-r9999"; exit 0; fi\nexec {MINIMAP2} "$@"\n')
    _path_with(monkeypatch, fakes)
    code, human, _ = _lab(capsys, *argv)
    assert code == 0
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in human.splitlines()), label
    assert "CODE       TBX-LAB-019" in human
    assert "2.99-r9999" in human
    progress_lines = human.splitlines()
    warning_line = next(i for i, line in enumerate(progress_lines) if line.startswith("WARN TBX-LAB-019"))
    aligned_line = next(i for i, line in enumerate(progress_lines) if line.startswith("align") and "done" in line)
    assert warning_line < aligned_line
    new_inputs = {p.name for p in LabStore(root).inputs_dir.iterdir() if p.is_dir()} - before_inputs
    assert len(new_inputs) == 1
    (second_input,) = new_inputs
    _fake_tool(fakes, "minimap2", f'if [ "$1" = "--version" ]; then echo "2.98-r9998"; exit 0; fi\nexec {MINIMAP2} "$@"\n')
    code, second = _json(capsys, *argv)
    assert code == 0
    assert second["data"]["sample"]["source_id"] == first["data"]["sample"]["source_id"]
    assert second["data"]["sample"]["input_id"] not in {first_input, second_input}
    (warning,) = second["data"]["warnings"]
    assert warning["code"] == "TBX-LAB-019" and warning["new"]["minimap2"] == "2.98-r9998"
    assert warning["cause"] and warning["fix"]
    assert isinstance(warning["retryable"], bool)
    assert warning["docs"] == "docs/LAB-GUIDE.md#tbx-lab-019"


def test_prealigned_bam_is_ingested(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    code, first = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    aligned = root / "lab" / "inputs" / first["data"]["sample"]["input_id"] / "aligned.bam"
    copy = lab_world["tmp"] / "already_aligned.bam"
    shutil.copyfile(aligned, copy)
    other = lab_world["tmp"] / "root2"
    from traceback_runner.references import register_reference

    register_reference(other, lab_world["fasta"], "synth")
    code, payload = _json(capsys, "run", str(copy), "--root", str(other))
    assert code == 0
    manifest = json.loads((other / "lab" / "inputs" / payload["data"]["sample"]["input_id"] / "manifest.json").read_bytes())
    assert manifest["kind"] == "prealigned"
    assert payload["data"]["counts"]["ok"] == 1


def test_interrupt_exits_6_and_rerun_recomputes_only_the_interrupted(lab_world, shared_mmi, tmp_path) -> None:
    root = lab_world["root"]
    ready = tmp_path / "ready"
    marker = tmp_path / "ran.txt"
    script = textwrap.dedent(
        f"""
        import sys
        from traceback_lab import registry
        registry.ALGORITHMS = ("{FAKES}:SUMMARY", "{FAKES}:COUNTER", "{FAKES}:WAITS")
        from traceback_runner.cli import main
        sys.exit(main(sys.argv[1:]))
        """
    )
    env = {**os.environ, "LAB_TEST_READY": str(ready), "LAB_TEST_MARKER": str(marker), "PYTHONPATH": str(REPO)}
    argv = [sys.executable, "-c", script, "lab", "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi), *QUIET]
    process = subprocess.Popen(argv, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 120
    while not ready.exists() and time.time() < deadline and process.poll() is None:
        time.sleep(0.1)
    assert ready.exists(), process.communicate()
    process.send_signal(signal.SIGINT)
    out, _ = process.communicate(timeout=60)
    assert process.returncode == 6, out
    assert "TBX-LAB-015" in out and "Interrupted during cell-origin" in out
    assert marker.read_text().split() == ["read-summary", "fragment-size"]
    store = LabStore(root)
    (run_id,) = store.run_ids()
    events = store.run_log(run_id).events()
    assert events[-1]["event"] == "run_end" and events[-1]["status"] == "interrupted"
    assert store.read_active() is None
    # Rerun (without the waiting plug-in): the two finished results are cached.
    script2 = script.replace(f', "{FAKES}:WAITS"', "")
    rerun = subprocess.run(
        [sys.executable, "-c", script2, "lab", "run", "latest", "--root", str(root), "--json", *QUIET],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    steps = json.loads(rerun.stdout)["data"]["steps"]
    assert all(step["cached"] for step in steps)
    assert marker.read_text().split() == ["read-summary", "fragment-size"]


def test_sigterm_is_an_interrupt_too(lab_world, shared_mmi, tmp_path) -> None:
    ready = tmp_path / "ready"
    script = textwrap.dedent(
        f"""
        import sys
        from traceback_lab import registry
        registry.ALGORITHMS = ("{FAKES}:WAITS",)
        from traceback_runner.cli import main
        sys.exit(main(sys.argv[1:]))
        """
    )
    env = {**os.environ, "LAB_TEST_READY": str(ready), "PYTHONPATH": str(REPO)}
    process = subprocess.Popen(
        [sys.executable, "-c", script, "lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--json"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 120
    while not ready.exists() and time.time() < deadline and process.poll() is None:
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    out, _ = process.communicate(timeout=60)
    assert process.returncode == 6
    assert json.loads(out)["data"]["code"] == "TBX-LAB-015"


def test_index_from_another_reference_is_refused_before_publish(lab_world, capsys, monkeypatch, tmp_path) -> None:
    _registry(monkeypatch)
    from traceback_lab.synthetic import write_reference

    other = write_reference(tmp_path / "other.fa", seed=3, length=30_000, contigs=("chr1", "chr2", "chrZ"))
    mmi = tmp_path / "other.mmi"
    subprocess.run([MINIMAP2, "-x", "map-ont", "-d", str(mmi), str(other.fasta)], check=True, capture_output=True)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(mmi))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-009"
    assert not list(LabStore(root).inputs_dir.glob("*/manifest.json"))
    assert _no_staging(root)


def test_chunk_changed_during_the_run_is_refused(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    from traceback_lab import ingest

    original = ingest.run_pipeline
    victim = lab_world["chunks"] / "SYNTH0001_pass_synthetic_2.bam"

    def touching(*args, **kwargs):
        os.utime(victim, ns=(1, 1))
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "run_pipeline", touching)
    root = lab_world["root"]
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-005"
    assert payload["data"]["kind"] == "changed during the run"
    assert not list(LabStore(root).inputs_dir.glob("*/manifest.json"))


def test_existing_input_reruns_even_when_the_disk_is_now_short(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    from traceback_lab import ingest

    argv = ("run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi))
    assert _json(capsys, *argv)[0] == 0

    class Usage:
        free = 1024

    monkeypatch.setattr(ingest.shutil, "disk_usage", lambda path: Usage)
    assert _json(capsys, *argv)[0] == 0


def test_enospc_while_copying_a_prealigned_bam_is_tbx_lab_011(lab_world, capsys, monkeypatch, shared_mmi, tmp_path) -> None:
    _registry(monkeypatch)
    import errno

    from traceback_lab import ingest
    from traceback_runner.references import register_reference

    code, first = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi))
    copy = tmp_path / "aligned_copy.bam"
    shutil.copyfile(lab_world["root"] / "lab" / "inputs" / first["data"]["sample"]["input_id"] / "aligned.bam", copy)
    other = tmp_path / "root2"
    register_reference(other, lab_world["fasta"], "synth")

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ingest.shutil, "copyfile", full)
    code, payload = _json(capsys, "run", str(copy), "--root", str(other))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-011"
    assert _no_staging(other)


@pytest.mark.parametrize("flag", [["--sort-mem", "/tmp/DonorName"], ["--threads", "0"]])
def test_alignment_flags_are_validated(lab_world, capsys, flag) -> None:
    code, payload = _json(capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), *flag)
    assert code == 2 and payload["status"] == "usage"
    assert not (lab_world["root"] / "lab").exists()


def test_algorithm_timeout_kills_a_child_in_another_session(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch, "SESSION_HANGS")
    pidfile = lab_world["tmp"] / "session-child.pid"
    monkeypatch.setenv("LAB_TEST_PIDFILE", str(pidfile))
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi), "--timeout-s", "3"
    )
    assert code == 8
    child = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("a child that left the worker's process group survived the timeout")


def test_worker_start_up_does_not_import_the_signed_cli() -> None:
    """Every algorithm step is its own process; importing traceback_runner.cli
    (and its web server) cost ~1.5 s per step and pushed --example past its CI budget."""

    import subprocess
    import sys

    code = (
        "import sys, traceback_lab.worker\n"
        "from traceback_lab.registry import ALGORITHMS, load_entry\n"
        "[load_entry(entry) for entry in ALGORITHMS]\n"
        "print('traceback_runner.cli' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"
