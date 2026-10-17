"""D36 problem fields through real Lab CLI refusal paths, in both output modes."""

from __future__ import annotations

import json
import errno
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import pysam

from traceback_lab import cli as lab_cli
from traceback_lab import ingest
from traceback_lab import run_command as run_module
from traceback_lab.store import LabStore
from traceback_lab.synthetic import rg_line, write_chunks, write_reference
from traceback_runner import cli as signed_cli


def _assert_both_modes(
    capsys: pytest.CaptureFixture[str], argv: list[str], code: str, exit_code: int,
) -> tuple[str, dict]:
    human_code = signed_cli.main(["lab", *argv])
    human = capsys.readouterr().out
    json_code = signed_cli.main(["lab", *argv, "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert human_code == json_code == exit_code
    assert output.count("\n") == 1
    assert payload["schema_version"] == "traceback.cli-result.v2"
    data = payload["data"]
    assert data["code"] == code
    assert data["cause"] and data["fix"]
    assert isinstance(data["retryable"], bool)
    assert data["docs"] == f"docs/LAB-GUIDE.md#{code.lower()}"
    for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
        assert any(line.startswith(label) for line in human.splitlines()), (code, label)
    assert f"CODE       {code}" in human
    return human, data


@pytest.mark.parametrize("problem", ["truncated", "duplicate", "mixed_model"])
def test_chunk_refusals_have_full_cli_fields(
    lab_world, capsys: pytest.CaptureFixture[str], problem: str,
) -> None:
    chunks = lab_world["chunks"]
    if problem == "truncated":
        path = chunks / "SYNTH0001_pass_synthetic_1.bam"
        content = path.read_bytes()
        path.write_bytes(content[: len(content) // 2])
        code = "TBX-LAB-006"
    elif problem == "duplicate":
        shutil.copyfile(
            chunks / "SYNTH0001_pass_synthetic_0.bam",
            chunks / "SYNTH0001_pass_synthetic_7.bam",
        )
        code = "TBX-LAB-007"
    else:
        reference = write_reference(lab_world["tmp"] / "other.fa", length=5000, contigs=("chr1",))
        write_chunks(
            chunks, reference, chunks=1, reads_per_chunk=20, seed=5,
            stem="OTHER_pass_model", rg=rg_line(run="other", model="dna_r9.4.1_e8_hac@v3.3"),
        )
        (chunks / "OTHER_pass_model_0.bam").rename(chunks / "OTHER_pass_model_5.bam")
        code = "TBX-LAB-008"
    _assert_both_modes(
        capsys, ["run", str(chunks), "--root", str(lab_world["root"])], code, 3,
    )


def test_lock_refusal_identifies_holder_in_both_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    store = LabStore(tmp_path)
    run_id = "r-20261016-1200-abcd"
    with store.writer("run", run_id, step="align"):
        human, data = _assert_both_modes(
            capsys, ["run", "latest", "--root", str(tmp_path)], "TBX-LAB-012", 3,
        )
    holder = data["holder"]
    assert holder["command"] == "run"
    assert holder["run_id"] == run_id
    assert holder["pid"] == os.getpid()
    assert holder["step"] == "align"
    assert isinstance(holder["age_min"], int)
    for detail in (run_id, str(os.getpid()), "align"):
        assert detail in human


def test_example_cohort_and_corrupt_roster_have_full_cli_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    store = LabStore(tmp_path)
    source_id = "a" * 64
    store.sources_dir.mkdir(parents=True)
    (store.sources_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id, "example": True, "inputs": [],
    }))
    _assert_both_modes(
        capsys,
        ["cohort", "add", source_id[:12], "--cohort", "healthy", "--root", str(tmp_path)],
        "TBX-LAB-021", 3,
    )
    cohort_dir = store.lab / "cohorts" / "healthy"
    cohort_dir.mkdir(parents=True)
    (cohort_dir / "members.json").write_text("not json")
    _assert_both_modes(
        capsys, ["cohort", "build", "--cohort", "healthy", "--root", str(tmp_path)],
        "TBX-LAB-022", 3,
    )


@pytest.mark.parametrize(
    ("argv", "code", "exit_code"),
    [
        (["run", "missing.bam", "--only", "fragmnt-size", "--dry-run"], "TBX-LAB-001", 2),
        (["run", "missing.bam", "--downsample", "1.5"], "TBX-LAB-002", 2),
        (["run", "missing.bam"], "TBX-LAB-005", 3),
        (["runs", "r-20261016-1200-abcd"], "TBX-LAB-017", 4),
    ],
)
def test_simple_refusals_have_full_cli_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    argv: list[str], code: str, exit_code: int,
) -> None:
    _assert_both_modes(capsys, [*argv, "--root", str(tmp_path)], code, exit_code)


def test_missing_reference_has_full_cli_fields(
    lab_world, capsys: pytest.CaptureFixture[str],
) -> None:
    root = lab_world["tmp"] / "no-reference"
    _assert_both_modes(
        capsys, ["run", str(lab_world["chunks"]), "--root", str(root)], "TBX-LAB-004", 3,
    )


def test_newer_store_and_internal_error_have_full_cli_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LabStore(tmp_path)
    store.lab.mkdir(parents=True)
    store.version_file.write_text("2\n")
    _assert_both_modes(capsys, ["runs", "--root", str(tmp_path)], "TBX-LAB-018", 3)

    def fail_registry():
        raise RuntimeError("synthetic registry failure")

    monkeypatch.setattr(lab_cli, "load_registry", fail_registry)
    _assert_both_modes(capsys, ["algorithms", "--root", str(tmp_path)], "TBX-LAB-020", 7)


def test_missing_aligner_has_full_cli_fields(
    lab_world, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = lab_world["tmp"] / "samtools-only"
    tools.mkdir()
    samtools = shutil.which("samtools")
    assert samtools is not None
    (tools / "samtools").symlink_to(samtools)
    monkeypatch.setenv("PATH", str(tools))
    _assert_both_modes(
        capsys,
        ["run", str(lab_world["chunks"]), "--root", str(lab_world["root"])],
        "TBX-LAB-003", 3,
    )


def test_aligned_reference_mismatch_has_full_cli_fields(
    lab_world, capsys: pytest.CaptureFixture[str],
) -> None:
    reference = ingest.resolve_reference(lab_world["root"], None)
    contigs = list(reference.contigs)
    contigs[0] = (contigs[0][0], contigs[0][1] + 1)
    bam = lab_world["tmp"] / "mismatched.bam"
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": name, "LN": length} for name, length in contigs],
        "RG": [{"ID": "synthetic", "PL": "ONT"}],
    }
    with pysam.AlignmentFile(str(bam), "wb", header=header):
        pass
    _assert_both_modes(
        capsys, ["run", str(bam), "--root", str(lab_world["root"])],
        "TBX-LAB-009", 3,
    )


def test_short_disk_has_measured_fields_in_both_modes(
    lab_world, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Usage:
        free = 1024

    monkeypatch.setattr(ingest.shutil, "disk_usage", lambda path: Usage)
    human, data = _assert_both_modes(
        capsys,
        ["run", str(lab_world["chunks"]), "--root", str(lab_world["root"])],
        "TBX-LAB-010", 3,
    )
    assert data["step"] == "align"
    assert data["volume"] == "ROOT"
    assert data["free_bytes"] == 1024
    assert data["required_bytes"] > data["free_bytes"]
    assert "align" in human and "ROOT" in human


def test_only_cell_origin_dry_run_labels_upstream_without_writing(
    lab_world, capsys: pytest.CaptureFixture[str],
) -> None:
    root = lab_world["root"]
    argv = [
        "lab", "run", str(lab_world["chunks"]), "--root", str(root),
        "--only", "cell-origin", "--dry-run",
    ]
    assert not (root / "lab").exists()
    assert signed_cli.main(argv) == 0
    human = capsys.readouterr().out
    assert "PLAN      read-summary (upstream):" in human
    assert "PLAN      cell-origin:" in human
    assert signed_cli.main([*argv, "--json"]) == 0
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    payload = json.loads(output)
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert payload["data"]["root"] == str(root)
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["plan"] == [
        {"algorithm_id": "read-summary", "upstream": True, "decision": "decided after alignment"},
        {"algorithm_id": "cell-origin", "upstream": False, "decision": "decided after alignment"},
    ]
    assert not (root / "lab").exists()


@pytest.mark.parametrize("problem", ["enospc", "count_mismatch", "process_failure"])
def test_alignment_refusals_have_full_cli_fields_without_running_tools(
    lab_world, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    mmi = lab_world["tmp"] / "unused.mmi"
    mmi.write_bytes(b"synthetic index placeholder")
    monkeypatch.setattr(run_module, "ensure_mmi", lambda *args, **kwargs: (mmi, "0" * 64))
    reference = ingest.resolve_reference(lab_world["root"], None)

    def fake_pipeline(stages, **kwargs):
        if problem == "enospc":
            raise OSError(errno.ENOSPC, "synthetic volume full")
        if problem == "process_failure":
            return SimpleNamespace(
                ok=False, failed_stage=SimpleNamespace(name="minimap2", returncode=1),
                stages=[], timed_out=False, first_line=lambda scrubber: "synthetic tool failure",
            )
        staging = kwargs["cwd"]
        header = {
            "HD": {"VN": "1.6", "SO": "coordinate"},
            "SQ": [{"SN": name, "LN": length} for name, length in reference.contigs],
        }
        with pysam.AlignmentFile(str(staging / "aligned.bam"), "wb", header=header):
            pass
        return SimpleNamespace(
            ok=True, duration_s=1.0, peak_rss_bytes=0, peak_disk_bytes=0, stages=[],
        )

    monkeypatch.setattr(ingest, "run_pipeline", fake_pipeline)
    code = {
        "enospc": "TBX-LAB-011",
        "count_mismatch": "TBX-LAB-013",
        "process_failure": "TBX-LAB-014",
    }[problem]
    _, data = _assert_both_modes(
        capsys,
        ["run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--mmi", str(mmi)],
        code, 3,
    )
    if problem == "count_mismatch":
        assert data["ubam_records"] == 1800 and data["aligned_records"] == 0
    elif problem == "process_failure":
        assert data["process"] == "minimap2" and data["exit_code"] == 1


def test_interrupted_run_has_full_cli_fields_without_running_algorithms(
    lab_world, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = ingest.resolve_reference(lab_world["root"], None)
    bam = lab_world["tmp"] / "prealigned.bam"
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [{"SN": name, "LN": length} for name, length in reference.contigs],
    }
    with pysam.AlignmentFile(str(bam), "wb", header=header):
        pass

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "run_plan", interrupt)
    human, data = _assert_both_modes(
        capsys, ["run", str(bam), "--root", str(lab_world["root"])],
        "TBX-LAB-015", 6,
    )
    assert "Interrupted during" in human
    assert data["retryable"] is True
