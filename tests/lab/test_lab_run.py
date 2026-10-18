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
from threading import Event, Thread
from types import SimpleNamespace

import pysam
import pytest

from traceback_lab import registry
from traceback_lab import doctor
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


def test_low_memory_example_preview_has_complete_warning_fields(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_ram", lambda: 8 * doctor.GIB)
    argv = ("run", "--example", "--dry-run", "--root", str(tmp_path / "root"))
    code, human, _ = _lab(capsys, *argv)
    assert code == 0
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in human.splitlines()), label
    code, payload = _json(capsys, *argv)
    assert code == 0
    warning, = payload["data"]["warnings"]
    assert warning["code"] == "TBX-LAB-025"
    assert warning["cause"] and warning["fix"] and warning["retryable"] is False
    assert warning["docs"] == "docs/LAB-GUIDE.md#tbx-lab-025"
    assert not (tmp_path / "root").exists()


def test_low_memory_warning_is_advisory_in_run_and_dry_run(lab_world, capsys, monkeypatch, shared_mmi) -> None:
    _registry(monkeypatch)
    monkeypatch.setattr(doctor, "_ram", lambda: 8 * doctor.GIB)
    argv = ("run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(shared_mmi))
    code, human, _ = _lab(capsys, *argv, "--dry-run")
    assert code == 0 and "TBX-LAB-025: Less than 16 GiB" in human
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in human.splitlines()), label
    code, dry = _json(capsys, *argv, "--dry-run")
    assert code == 0 and [warning["code"] for warning in dry["data"]["warnings"]] == ["TBX-LAB-025"]
    code, human, _ = _lab(capsys, *argv)
    assert code == 0 and human.count("Less than 16 GiB") == 1
    assert "TBX-LAB-025: Less than 16 GiB" in human
    assert "Use a machine with at least 16 GiB RAM" in human
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert sum(line.startswith(label) for line in human.splitlines()) == 1, label
    code, payload = _json(capsys, *argv)
    assert code == 0
    low_memory = [row for row in payload["data"]["warnings"] if row.get("kind") == "low_memory"]
    assert len(low_memory) == 1
    (warning,) = low_memory
    assert warning["kind"] == "low_memory"
    assert warning["code"] == "TBX-LAB-025"
    assert warning["retryable"] is False
    assert warning["cause"] and warning["docs"].endswith("#tbx-lab-025")
    assert warning["physical_ram_bytes"] == 8 * doctor.GIB
    assert warning["recommended_bytes"] == doctor.MIN_RAM
    assert warning["fix"]
    for detected in (doctor.MIN_RAM, None):
        monkeypatch.setattr(doctor, "_ram", lambda detected=detected: detected)
        code, dry = _json(capsys, *argv, "--dry-run")
        assert code == 0 and dry["data"]["warnings"] == []
    def failed_probe():
        raise OSError("RAM query unavailable")

    monkeypatch.setattr(doctor, "_ram", failed_probe)
    code, dry = _json(capsys, *argv, "--dry-run")
    assert code == 0 and dry["data"]["warnings"] == []


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
    plans = [json.loads(path.read_bytes()) for path in store.results_dir.rglob("execution_plan.json")]
    assert len(plans) == 2
    assert {plan["algorithm_id"] for plan in plans} == {"read-summary", "fragment-size"}
    assert all(plan["schema"] == "traceback.lab.execution-plan.v1" and
               plan["input_id"] == input_dir.name for plan in plans)
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
    from traceback_lab.ingest import source_record

    for target in ("latest", str(lab_world["chunks"])):
        extra = () if target == "latest" else ("--mmi", str(shared_mmi))
        code, dry = _json(capsys, "run", target, "--root", str(root), *extra, "--sex", "M", "--dry-run")
        assert code == 0 and dry["data"]["dry_run"]
        assert store.generation() == generation
        assert "operator_metadata" not in source_record(store, original["source_id"])
        assert {path.relative_to(store.results_dir): path.read_bytes()
                for path in store.results_dir.rglob("result.json")} == result_bytes

    code, asserted = _json(capsys, "run", "latest", "--sex", "M", "--root", str(root))
    assert code == 0 and asserted["data"]["sample"] == original
    assert all(step["cached"] for step in asserted["data"]["steps"])
    assert store.generation() == generation + 1
    assert {path.relative_to(store.results_dir): path.read_bytes()
            for path in store.results_dir.rglob("result.json")} == result_bytes
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
    step_events = [event for event in store.run_log(data["run_id"]).events()
                   if event.get("event") == "step_end" and event.get("step") in steps]
    assert {"failed", "skipped"} <= {event["status"] for event in step_events}
    for event in step_events:
        selected = store.selected_attempt(input_id, event["step"], event["parameters_sha256"])
        assert selected is not None and event["attempt_id"] == selected.name
        assert event["input_id"] == input_id
    first_success = next(event for run_id in store.run_ids()
                         for event in store.run_log(run_id).events()
                         if event.get("event") == "step_end" and event.get("step") == "read-summary")
    selected_success = store.selected_attempt(input_id, "read-summary", first_success["parameters_sha256"])
    assert selected_success is not None and first_success["attempt_id"] == selected_success.name


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


def test_dry_run_reports_unknown_reuse_for_legacy_mmi_hash_cache(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    _registry(monkeypatch, "SUMMARY")
    store = LabStore(lab_world["root"])
    code, first = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(store.root),
        "--mmi", str(shared_mmi), "--only", "read-summary",
    )
    assert code == 0, first
    cache = store.load_hash_cache()
    cache.pop(store.strong_hash_cache_key(shared_mmi), None)
    cache[store.hash_cache_key(shared_mmi)] = {
        "sha256": hashlib.sha256(shared_mmi.read_bytes()).hexdigest(), "records": -1,
    }
    store.save_hash_cache(cache)
    before = store.hash_cache_file.read_bytes()
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(store.root),
        "--mmi", str(shared_mmi), "--dry-run",
    )
    assert code == 0
    assert payload["data"]["alignment"] == "reuse unknown until index hashed"
    assert store.hash_cache_file.read_bytes() == before


def test_dry_run_rejects_symlinked_mmi_like_real_run(
    lab_world, capsys, monkeypatch, shared_mmi, tmp_path,
) -> None:
    _registry(monkeypatch, "SUMMARY")
    alias = tmp_path / "alias.mmi"
    alias.symlink_to(shared_mmi)
    argv = (
        "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]),
        "--mmi", str(alias),
    )
    for extra in (("--dry-run",), ()):
        code, payload = _json(capsys, *argv, *extra)
        assert code == 3
        assert payload["data"]["code"] == "TBX-LAB-005"


def test_stored_dry_run_retries_concurrent_cohort_selection_without_writing(
    lab_world, capsys, monkeypatch, shared_mmi,
) -> None:
    from traceback_lab.cli import build_parser
    from traceback_lab.store import new_run_id

    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    code, first = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(root),
        "--mmi", str(shared_mmi), "--only", "read-summary",
    )
    assert code == 0, first
    args = build_parser().parse_args([
        "run", "latest", "--root", str(root), "--only", "read-summary", "--dry-run",
    ])
    store = LabStore(root)
    original_read = LabStore.read_result
    read_started = Event()
    writer_done = Event()
    calls = 0

    def read_during_publication(current_store, input_id, algorithm_id, identity):
        nonlocal calls
        selected = original_read(current_store, input_id, algorithm_id, identity)
        calls += 1
        if calls == 1:
            read_started.set()
            assert writer_done.wait(10)
        return selected

    monkeypatch.setattr(LabStore, "read_result", read_during_publication)
    results = []
    errors = []

    def preview() -> None:
        try:
            results.append(run_module._dry_run(args, LabStore(root), lambda _line: None))
        except BaseException as exc:
            errors.append(exc)

    def snapshot() -> dict[str, bytes | None]:
        return {
            str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")
            if path.is_file() or path.is_dir()
        }

    with store.writer("cohort build", new_run_id()):
        before = snapshot()
        thread = Thread(target=preview, daemon=True)
        thread.start()
        assert read_started.wait(10)
        store.bump_cohort_selection_epoch()
        writer_done.set()
        thread.join(10)
        assert not thread.is_alive()  # The preview never waits for the writer lock.
        after = snapshot()
        before.pop("lab/cohort-selection-epoch", None)
        after.pop("lab/cohort-selection-epoch", None)
        assert after == before  # Only the test writer changed ROOT.
    assert not errors
    assert calls == 2
    assert results[0][0] == 0
    assert results[0][1]["data"]["plan"][0]["decision"] == "reused (selected result)"


@pytest.mark.parametrize("problem", ["changing", "unreadable"])
def test_dry_run_selected_summary_reports_actionable_unavailability(problem) -> None:
    from contextlib import contextmanager

    from traceback_lab.problems import LabProblem
    from traceback_lab.store import ResultOrderUnavailable, SelectionSnapshotChanged

    class UnstableStore:
        reads = 0

        @contextmanager
        def selection_read_context(self):
            if problem == "unreadable":
                raise ResultOrderUnavailable("invalid cohort selection journal")
            yield
            raise SelectionSnapshotChanged("cohort selection changed")

        def read_result(self, *_args):
            self.reads += 1
            return None

    store = UnstableStore()
    with pytest.raises(LabProblem) as caught:
        run_module._dry_run_read_summary(store, "a" * 64, "b" * 64)
    assert caught.value.code == ("TBX-LAB-012" if problem == "changing" else "TBX-LAB-022")
    assert ("Retry" if problem == "changing" else "Restore") in caught.value.fix
    assert store.reads == (3 if problem == "changing" else 0)


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
    source_file = store.sources_dir / f"{first['data']['sample']['source_id']}.json"
    record = json.loads(source_file.read_text())
    record["excluded"] = [4]
    source_file.write_text(json.dumps(record))
    input_id = first["data"]["sample"]["input_id"]
    identity = next((store.results_dir / input_id / "read-summary").iterdir()).name
    selected_summary = store.read_result(input_id, "read-summary", identity)
    assert selected_summary is not None
    expected_eligible = next(
        metric.value for metric in selected_summary.metrics if metric.metric_id == "eligible_reads"
    )

    def snapshot() -> dict[str, str]:
        return {
            str(path.relative_to(root)): (
                "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
            for path in root.rglob("*") if path.is_dir() or path.is_file()
        }

    def selected_result_for(value):
        def selected_result(_store, selected_input_id, algorithm_id, parameters_sha256):
            assert (selected_input_id, algorithm_id, parameters_sha256) == (
                input_id, "read-summary", identity,
            )
            return value

        return selected_result

    with store.writer("other", "r-20261017-1200-aaab"):
        before = snapshot()
        monkeypatch.setattr(
            run_module, "shutil",
            SimpleNamespace(disk_usage=lambda _root: SimpleNamespace(free=3 * 1024**3)),
        )
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
                patch.setattr(LabStore, "read_result", selected_result_for(selected))
                missing_code, missing = _json(
                    capsys, "run", "latest", "--root", str(root),
                    "--only", "delfi-ratio", "--dry-run",
                )
                assert missing_code == 0
                unavailable.append(missing["data"])
        with monkeypatch.context() as patch:
            selected = SimpleNamespace(
                status="ok", metrics=[
                    SimpleNamespace(metric_id="eligible_reads", value=2_000_000),
                    SimpleNamespace(metric_id="approx_depth_x", value=1.0),
                ],
            )
            patch.setattr(LabStore, "read_result", selected_result_for(selected))
            met_code, met = _json(
                capsys, "run", "latest", "--root", str(root),
                "--only", "delfi-ratio", "--dry-run",
            )
        with monkeypatch.context() as patch:
            patch.setattr(LabStore, "read_result", selected_result_for(SimpleNamespace(status="ok", metrics=[])))
            no_depth_code, no_depth = _json(
                capsys, "run", "latest", "--root", str(root),
                "--only", "delfi-ratio", "--dry-run",
            )
        with monkeypatch.context() as patch:
            patch.setattr(LabStore, "read_result", selected_result_for(SimpleNamespace(
                status="ok", metrics=[SimpleNamespace(metric_id="eligible_reads", value=2_000_000)],
            )))
            partial_depth_code, partial_depth = _json(
                capsys, "run", "latest", "--root", str(root),
                "--only", "delfi-ratio", "--dry-run",
            )
        stored_manifest = json.loads((store.inputs_dir / input_id / "manifest.json").read_text())
        for malformed in (
            {**stored_manifest, "reference_sha256": None},
            {**stored_manifest, "reference_sha256": "invalid"},
            ["invalid"],
        ):
            with monkeypatch.context() as patch:
                patch.setattr(run_module, "input_manifest", lambda _store, _input_id: malformed)
                bad_code, bad = _json(capsys, "run", "latest", "--root", str(root), "--dry-run")
                expected = "TBX-LAB-024" if isinstance(malformed, list) else "TBX-LAB-017"
                assert bad_code == (3 if isinstance(malformed, list) else 4)
                assert bad["data"]["code"] == expected
                assert bad["data"]["input_id"] == input_id
                if expected == "TBX-LAB-017":
                    assert "fresh ROOT" in bad["data"]["fix"]
        from traceback_lab.sample_metadata import SCHEMA as SEX_METADATA_SCHEMA

        with monkeypatch.context() as patch:
            patch.setattr(run_module, "input_manifest", lambda _store, _input_id: ["invalid"])
            patch.setattr(run_module, "source_record", lambda _store, _source_id: {
                **record,
                "operator_metadata": {
                    "schema": SEX_METADATA_SCHEMA, "sex": "M", "recorded_utc": "2026-10-17T00:00:00Z",
                },
            })
            conflict_code, conflict = _json(
                capsys, "run", "latest", "--root", str(root), "--sex", "F", "--dry-run",
            )
            assert conflict_code == 3 and conflict["data"]["code"] == "TBX-LAB-024"
        with monkeypatch.context() as patch:
            patch.setattr(run_module, "source_record", lambda _store, _source_id: {
                **record, "chunks": [{"size": "invalid"}],
            })
            incomplete_code, incomplete = _json(
                capsys, "run", "latest", "--root", str(root), "--dry-run",
            )
            incomplete_human_code, incomplete_human, _ = _lab(
                capsys, "run", "latest", "--root", str(root), "--dry-run",
            )
        assert snapshot() == before
    assert code == human_code == forced_code == met_code == no_depth_code == partial_depth_code == incomplete_code == incomplete_human_code == 0
    data = payload["data"]
    assert data["sample"]["id12"] == first["data"]["sample"]["id12"]
    assert data["sample"]["input_id"] == first["data"]["sample"]["input_id"]
    assert data["reference_id"] == "synth"
    assert data["chunks"]["count"] == 3 and data["chunks"]["bytes"] > 0
    assert data["chunks"]["excluded"] == [4]
    assert data["alignment"] == "reused (stored sample)"
    assert data["budget"]["required_bytes"] == 0
    assert data["budget"]["free_bytes"] == 3 * 1024**3
    assert data["budget"]["analysis_scratch"] == "not predicted"
    assert data["eligible_reads"] == expected_eligible < 1_000_000
    assert data["plan"] == [
        {"algorithm_id": "read-summary", "upstream": True, "decision": "reused (selected result)"},
        {
            "algorithm_id": "delfi-ratio", "upstream": False,
            "decision": "skip expected below depth minimum (unless cached)",
            "eligible_reads": data["eligible_reads"], "minimum_eligible_reads": 1_000_000,
        },
    ]
    assert "REFERENCE synth" in human
    assert "3 stored chunk(s)" in human
    assert "excluded [4]" in human
    assert "skip expected below depth minimum (unless cached)" in human
    assert "has 3.0 GB (reused; not a sufficiency check)" in human
    assert "ANALYSIS  scratch disk not predicted" in human
    assert "eligible_reads" not in forced["data"]
    assert all(row["decision"] == "decided at run time" for row in forced["data"]["plan"])
    for missing in unavailable:
        assert "eligible_reads" not in missing
        assert all(row["decision"] == "decided at run time" for row in missing["plan"])
    assert met["data"]["plan"][1]["decision"] == "depth minimum met; other checks at run time"
    assert "eligible_reads" not in no_depth["data"]
    assert no_depth["data"]["plan"][0]["decision"] == "reused (selected result)"
    assert no_depth["data"]["plan"][1]["decision"] == "decided at run time"
    assert "eligible_reads" not in partial_depth["data"]
    assert partial_depth["data"]["plan"][0]["decision"] == "reused (selected result)"
    assert partial_depth["data"]["plan"][1]["decision"] == "decided at run time"
    assert incomplete["data"]["chunks"] == {"count": None, "bytes": None, "excluded": [4]}
    assert "INPUT     unknown (stored chunk record incomplete) · excluded [4]" in incomplete_human


def test_stored_illumina_dry_run_uses_platform_bound_read_summary(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    from tests.lab.test_platforms import _prealigned_world
    from traceback_lab.algorithms.read_summary import READ_SUMMARY
    from traceback_lab.registry import execution_identity

    root, bam = _prealigned_world(tmp_path, ("ILLUMINA",))
    monkeypatch.setattr(registry, "ALGORITHMS", (
        "read_summary:READ_SUMMARY", "delfi_ratio:DELFI_RATIO",
    ))
    code, first = _json(capsys, "run", str(bam), "--root", str(root),
                        "--reference", "hg38", "--only", "read-summary")
    assert code == 0, first
    store = LabStore(root)
    input_id = first["data"]["sample"]["input_id"]
    manifest = json.loads((store.inputs_dir / input_id / "manifest.json").read_bytes())
    identity = execution_identity(
        READ_SUMMARY, reference_sha256=manifest["reference_sha256"], platform="illumina",
    )
    selected = store.read_result(input_id, "read-summary", identity)
    assert selected is not None and selected.status == "ok"
    eligible = next(metric.value for metric in selected.metrics if metric.metric_id == "eligible_reads")

    dry_code, dry = _json(capsys, "run", "latest", "--root", str(root),
                          "--only", "delfi-ratio", "--dry-run")
    assert dry_code == 0, dry
    assert dry["data"]["sample"]["platform"] == "illumina"
    assert dry["data"]["eligible_reads"] == eligible
    assert dry["data"]["plan"] == [
        {"algorithm_id": "read-summary", "upstream": True, "decision": "reused (selected result)"},
        {"algorithm_id": "delfi-ratio", "upstream": False, "decision": "skip expected (platform)"},
    ]
    with monkeypatch.context() as patch:
        original_read_result = LabStore.read_result

        def high_depth_result(current_store, selected_id, algorithm_id, parameters_sha256):
            if algorithm_id == "read-summary":
                assert selected_id == input_id and parameters_sha256 == identity
                return SimpleNamespace(status="ok", metrics=[
                    SimpleNamespace(metric_id="eligible_reads", value=2_000_000),
                    SimpleNamespace(metric_id="approx_depth_x", value=1.0),
                ])
            return original_read_result(current_store, selected_id, algorithm_id, parameters_sha256)

        patch.setattr(LabStore, "read_result", high_depth_result)
        met_code, met = _json(capsys, "run", "latest", "--root", str(root),
                              "--only", "delfi-ratio", "--dry-run")
    assert met_code == 0, met
    assert met["data"]["eligible_reads"] == 2_000_000
    assert met["data"]["plan"][1]["decision"] == "skip expected (platform)"


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
    (warning,) = [row for row in second["data"]["warnings"] if row.get("code") == "TBX-LAB-019"]
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
    # Rerun the same plan: finished results are cached and only cell-origin runs.
    rerun = subprocess.run(
        [sys.executable, "-c", script, "lab", "run", "latest", "--root", str(root), "--json", *QUIET],
        cwd=REPO, env={**env, "LAB_TEST_RESUME": "1"}, capture_output=True, text=True, timeout=120,
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    steps = {step["algorithm_id"]: step for step in json.loads(rerun.stdout)["data"]["steps"]}
    assert set(steps) == {"read-summary", "fragment-size", "cell-origin"}
    assert steps["read-summary"]["cached"] and steps["fragment-size"]["cached"]
    assert steps["cell-origin"]["status"] == "ok" and not steps["cell-origin"]["cached"]
    assert marker.read_text().split() == ["read-summary", "fragment-size", "cell-origin"]


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


def _tiny_aligned_bam(lab_world, *, read_group: dict | None = None) -> Path:
    with pysam.FastaFile(str(lab_world["fasta"])) as reference:
        contigs = [{"SN": name, "LN": length} for name, length in
                   zip(reference.references, reference.lengths)]
    bam = lab_world["tmp"] / "one-aligned.bam"
    read_group = read_group or {"ID": "synthetic", "PL": "ONT"}
    header = pysam.AlignmentHeader.from_dict({
        "HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": contigs,
        "RG": [read_group],
    })
    with pysam.AlignmentFile(str(bam), "wb", header=header) as output:
        read = pysam.AlignedSegment(header)
        read.query_name = "synthetic"
        read.query_sequence = "A" * 100
        read.query_qualities = pysam.qualitystring_to_array("I" * 100)
        read.reference_id = 0
        read.reference_start = 0
        read.mapping_quality = 60
        read.cigarstring = "100M"
        read.set_tag("RG", read_group["ID"])
        output.write(read)
    return bam


@pytest.mark.parametrize("cache_seeded", [False, True])
def test_low_disk_refusal_leaves_registered_root_unchanged(lab_world, capsys, monkeypatch, cache_seeded) -> None:
    """AC4: a rejected new input must not change even Lab bookkeeping."""
    from traceback_lab.ingest import Budget

    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    bam = _tiny_aligned_bam(lab_world)
    if cache_seeded:
        from traceback_lab.ingest import inspect_chunks

        inspect_chunks([bam], LabStore(root))

    def inventory() -> dict[str, tuple[str, bytes | None]]:
        return {
            str(path.relative_to(root)): (
                "link" if path.is_symlink() else "dir" if path.is_dir() else "file",
                path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            )
            for path in root.rglob("*")
        }

    before = inventory()
    monkeypatch.setattr(run_module, "disk_budget", lambda *_args, **_kwargs: Budget({"aligned": 1024}, 0))
    code, payload = _json(capsys, "run", str(bam), "--root", str(root))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-010"
    assert payload["data"]["required_bytes"] == 1024
    assert payload["data"]["free_bytes"] == 0
    assert payload["data"]["step"] == "ingest"
    assert inventory() == before


def test_prealigned_cold_hash_once_then_reuses_under_low_disk(lab_world, capsys, monkeypatch) -> None:
    from traceback_lab import ingest

    _registry(monkeypatch, "SUMMARY")
    bam = _tiny_aligned_bam(lab_world)
    original_hash = ingest._sha256_file
    hashed = []

    def counted(path):
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(ingest, "_sha256_file", counted)
    argv = ("run", str(bam), "--root", str(lab_world["root"]))
    first_code, first = _json(capsys, *argv)
    assert first_code == 0, first
    assert hashed.count(bam) == 1
    first_hashes = list(hashed)

    monkeypatch.setattr(run_module, "disk_budget", lambda *_args, **_kwargs: ingest.Budget({"aligned": 1024}, 0))
    second_code, second = _json(capsys, *argv)
    assert second_code == 0, second
    assert hashed == first_hashes


def test_prealigned_middle_capacity_does_not_hash_and_count_twice(lab_world, capsys, monkeypatch) -> None:
    """A valid aligned BAM's cheap budget should use the aligned formula."""
    from traceback_lab import ingest

    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    bam = _tiny_aligned_bam(lab_world)
    reference = ingest.resolve_reference(root, "synth")
    size = bam.stat().st_size
    exact = ingest.disk_budget(
        LabStore(root), ingest.ChunkSet([ingest.Chunk(bam, bam.name, None, size, aligned=True)]),
        mmi_needed=False, fasta_bytes=0,
    ).required
    conservative = ingest.disk_budget(
        LabStore(root), ingest.ChunkSet([ingest.Chunk(bam, bam.name, None, size)]),
        mmi_needed=True, fasta_bytes=reference.loaded.source.fasta_size_bytes,
    ).required
    assert exact < conservative
    free = (exact + conservative) // 2
    monkeypatch.setattr(ingest.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free))

    hashes = []
    counts = []
    original_hash, original_count = ingest._sha256_file, ingest.count_records

    def counted_hash(path):
        hashes.append(path)
        return original_hash(path)

    def counted_records(path, *flags):
        counts.append(path)
        return original_count(path, *flags)

    monkeypatch.setattr(ingest, "_sha256_file", counted_hash)
    monkeypatch.setattr(ingest, "count_records", counted_records)
    code, payload = _json(capsys, "run", str(bam), "--root", str(root))
    assert code == 0, payload
    assert hashes.count(bam) == 1
    assert counts.count(bam) == 1


def test_existing_mmi_middle_capacity_does_not_hash_chunks_twice(
    lab_world, shared_mmi, capsys, monkeypatch,
) -> None:
    """An existing MMI removes the index reserve from the cheap bound."""
    from traceback_lab import ingest

    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    paths = sorted(lab_world["chunks"].glob("*.bam"))
    chunkset = ingest.ChunkSet([
        ingest.Chunk(path, path.name, None, path.stat().st_size) for path in paths
    ])
    reference = ingest.resolve_reference(root, "synth")
    exact = ingest.disk_budget(
        LabStore(root), chunkset, mmi_needed=False,
        fasta_bytes=reference.loaded.source.fasta_size_bytes,
    ).required
    conservative = ingest.disk_budget(
        LabStore(root), chunkset, mmi_needed=True,
        fasta_bytes=reference.loaded.source.fasta_size_bytes,
    ).required
    assert exact < conservative
    free = (exact + conservative) // 2
    monkeypatch.setattr(ingest.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free))
    original_hash = ingest._sha256_file
    hashed = []

    def counted(path):
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(ingest, "_sha256_file", counted)
    code, payload = _json(
        capsys, "run", str(lab_world["chunks"]), "--root", str(root), "--mmi", str(shared_mmi),
    )
    assert code == 0, payload
    assert all(hashed.count(path) == 1 for path in paths)
    assert hashed.count(shared_mmi) == 1


def test_malformed_single_chunk_with_skip_bad_uses_normal_ingest_error(lab_world, capsys) -> None:
    bad = lab_world["tmp"] / "broken.bam"
    bad.write_bytes(b"not a BAM")
    code, payload = _json(
        capsys, "run", str(bad), "--skip-bad-chunks", "--root", str(lab_world["root"]),
    )
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-006"


def test_mixed_aligned_and_unaligned_inputs_keep_ingest_refusal(lab_world, capsys) -> None:
    unaligned = next(lab_world["chunks"].glob("*.bam"))
    with pysam.AlignmentFile(str(unaligned), "rb", check_sq=False) as source:
        read_group = source.header.to_dict()["RG"][0]
    aligned = _tiny_aligned_bam(lab_world, read_group=read_group)
    code, payload = _json(
        capsys, "run", str(aligned), str(unaligned), "--root", str(lab_world["root"]),
    )
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-009"


def test_capacity_drop_after_read_only_preflight_is_checked_under_lock(lab_world, capsys, monkeypatch) -> None:
    from traceback_lab.ingest import Budget

    _registry(monkeypatch, "SUMMARY")
    bam = _tiny_aligned_bam(lab_world)
    calls = []

    def changing_budget(*_args, **_kwargs):
        calls.append(1)
        return Budget({"aligned": 1024}, 1024 if len(calls) == 1 else 0)

    monkeypatch.setattr(run_module, "disk_budget", changing_budget)
    code, payload = _json(capsys, "run", str(bam), "--root", str(lab_world["root"]))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-010"
    assert payload["data"]["step"] == "ingest"
    assert len(calls) == 2
    assert not list(LabStore(lab_world["root"]).inputs_dir.glob("*/manifest.json"))


@pytest.mark.parametrize("mmi_present", [False, True])
def test_low_disk_unaligned_refusal_leaves_root_unchanged(lab_world, capsys, monkeypatch, mmi_present) -> None:
    """The read-only gate includes alignment with or without a cached index."""
    from traceback_lab.ingest import Budget

    _registry(monkeypatch, "SUMMARY")
    root = lab_world["root"]
    before = {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
              for path in root.rglob("*")}
    monkeypatch.setattr(run_module, "disk_budget", lambda *_args, **_kwargs: Budget({"align": 1024}, 0))
    monkeypatch.setattr(run_module, "require_aligner_tools", lambda: (
        SimpleNamespace(version="samtools-test"), SimpleNamespace(version="minimap2-test")))
    mmi = lab_world["tmp"] / "external.mmi"
    if mmi_present:
        mmi.write_bytes(b"index identity")
    argv = ("run", str(lab_world["chunks"]), "--root", str(root))
    if mmi_present:
        argv += ("--mmi", str(mmi))
    code, payload = _json(capsys, *argv)
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-010"
    assert payload["data"]["step"] == "align"
    assert {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")} == before


def test_low_disk_unprepared_example_does_not_create_root(tmp_path, capsys, monkeypatch) -> None:
    root = tmp_path / "missing-root"
    monkeypatch.setattr(run_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    code, payload = _json(capsys, "run", "--example", "--root", str(root))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-010"
    assert payload["data"]["step"] == "example"
    assert payload["data"]["free_bytes"] == 0
    assert not root.exists()


def test_partial_example_without_registration_uses_generation_preflight(lab_world, capsys, monkeypatch) -> None:
    """A marker and chunks alone do not mean prepare can be skipped."""
    from traceback_lab.example import CHUNKS, CONTIG_LENGTH, MARKER, READS_PER_CHUNK, SEED, example_dir

    root = lab_world["root"]
    base = example_dir(root)
    chunks = base / "chunks"
    chunks.mkdir(parents=True)
    for number in range(CHUNKS):
        (chunks / f"partial_{number}.bam").write_bytes(b"")
    (base / "example.json").write_text(json.dumps({
        "schema": MARKER, "seed": SEED, "contig_length": CONTIG_LENGTH,
        "reads_per_chunk": READS_PER_CHUNK, "chunks": CHUNKS,
    }))
    before = {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
              for path in root.rglob("*")}
    monkeypatch.setattr(run_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    code, payload = _json(capsys, "run", "--example", "--root", str(root))
    assert code == 3 and payload["data"]["code"] == "TBX-LAB-010"
    assert payload["data"]["step"] == "example"
    assert {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")} == before


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
