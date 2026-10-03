"""Golden-path canary (scripts/canary/) on the generated fixture; no real data.

Every baseline, log and temporary root goes under pytest's tmp_path, and HOME is
pointed there too, so nothing is written to the real home directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from traceback_runner.fixtures import create_local_golden_path_inputs

REPO = Path(__file__).resolve().parents[1]
CANARY = REPO / "scripts" / "canary" / "real_bam_canary.py"
INSTALLER = REPO / "scripts" / "canary" / "install_canary.sh"
SYNTHETIC_BASELINE = REPO / "tests" / "fixtures" / "canary" / "synthetic_baseline.json"


def _environment(home: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "TRACEBACK_CANARY_NO_NOTIFY": "1",
        "TRACEBACK_CANARY_BASELINE": str(home / "unused-default-baseline.json"),
        "TRACEBACK_CANARY_LOG_DIR": str(home / "unused-default-logs"),
    }
    return environment


class Canary:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.inputs = create_local_golden_path_inputs(base / "inputs")
        self.home = base / "home"
        self.work = base / "work"
        self.home.mkdir()
        self.work.mkdir()

    def run(self, *extra: str, baseline: Path, logs: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CANARY),
             "--fasta", str(self.inputs.fasta_path), "--bam", str(self.inputs.bam_path),
             "--baseline", str(baseline), "--log-dir", str(logs),
             "--work-dir", str(self.work), "--no-notify", *extra],
            cwd=REPO,
            env=_environment(self.home),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )


def _latest(logs: Path) -> dict[str, Any]:
    return json.loads((logs / "latest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Canary, Path, Path]:
    """One --record-baseline --repeat 2 run, shared by the comparison tests."""

    canary = Canary(tmp_path_factory.mktemp("canary"))
    baseline = canary.base / "state" / "baseline.json"
    logs = canary.base / "record-logs"
    completed = canary.run("--record-baseline", "--repeat", "2", baseline=baseline, logs=logs)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return canary, baseline, logs


def test_record_baseline_repeat_is_reproducible_and_private(
    recorded: tuple[Canary, Path, Path],
) -> None:
    canary, baseline, logs = recorded
    assert stat.S_IMODE(baseline.stat().st_mode) == 0o600
    result = _latest(logs)
    assert result["status"] == "pass"
    assert result["baseline"]["recorded"] is True
    assert result["reproducible"] is True
    first, second = result["measurement_sha256"]
    assert first == second and len(first) == 64
    metrics = result["runs"][0]["metrics"]
    assert metrics == result["runs"][1]["metrics"]
    assert metrics["measurement"]["records_scanned"] == 1000
    assert (
        metrics["measurement"]["eligible_alignments"]
        == canary.inputs.expected_eligible_alignments
    )
    assert set(metrics["measurement"]["exclusions"]) >= {"duplicate", "unmapped"}
    assert sum(metrics["measurement"]["histogram"].values()) == 866
    assert metrics["preflight"]["outcome"] == "partial"
    assert {"code": "TBX-BAM-002", "outcome": "warn", "role": "analysis_bam"} in (
        metrics["preflight"]["checks"]
    )
    assert metrics["report"] == {"local_banner": True, "mentions_synthetic": False}
    assert metrics["locator_clean"] is True and metrics["verified"] is True
    # A timestamped result plus latest.json, both private; temporary roots removed.
    stamped = [path for path in logs.iterdir() if path.name != "latest.json"]
    assert len(stamped) == 1
    for path in (*stamped, logs / "latest.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(canary.work.iterdir())
    assert not any((canary.home).iterdir())


def test_compare_passes_against_recorded_baseline(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, baseline, _ = recorded
    logs = tmp_path / "logs"
    completed = canary.run(baseline=baseline, logs=logs)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CANARY PASS" in completed.stdout
    result = _latest(logs)
    assert result["baseline"]["compared"] is True
    assert result["baseline"]["diff"] == [] and result["baseline"]["inputs_match"] is True


def test_count_drift_fails_with_readable_diff(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, baseline, _ = recorded
    tampered = json.loads(baseline.read_text(encoding="utf-8"))
    tampered["metrics"]["measurement"]["eligible_alignments"] += 1
    tampered["metrics"]["measurement"]["histogram"]["150-200"] -= 2
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    logs = tmp_path / "logs"
    completed = canary.run(baseline=tampered_path, logs=logs)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "measurement.eligible_alignments: expected 867, got 866" in completed.stdout
    assert "measurement.histogram.150-200: expected 115, got 117" in completed.stdout
    assert "CANARY FAIL" in completed.stdout
    result = _latest(logs)
    assert result["status"] == "fail"
    assert len(result["baseline"]["diff"]) == 2


def test_changed_inputs_fail(recorded: tuple[Canary, Path, Path], tmp_path: Path) -> None:
    canary, baseline, _ = recorded
    tampered = json.loads(baseline.read_text(encoding="utf-8"))
    tampered["inputs"]["bam"]["sha256"] = "0" * 64
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    completed = canary.run(baseline=tampered_path, logs=tmp_path / "logs")
    assert completed.returncode == 1
    assert "inputs changed since the baseline: bam.sha256" in completed.stdout


def test_slow_step_warns_but_passes(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, baseline, _ = recorded
    fast = json.loads(baseline.read_text(encoding="utf-8"))
    fast["wall_seconds"] = {name: 0.001 for name in fast["wall_seconds"]}
    fast_path = tmp_path / "fast.json"
    fast_path.write_text(json.dumps(fast), encoding="utf-8")
    logs = tmp_path / "logs"
    completed = canary.run(baseline=fast_path, logs=logs)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "WARN  run 1: step run took" in completed.stdout
    result = _latest(logs)
    assert result["status"] == "pass"
    assert any("over 2x its baseline" in line for line in result["warnings"])


def test_record_baseline_refuses_to_overwrite(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, baseline, _ = recorded
    copy = tmp_path / "baseline.json"
    shutil.copy2(baseline, copy)
    before = copy.read_bytes()
    completed = canary.run("--record-baseline", baseline=copy, logs=tmp_path / "logs")
    assert completed.returncode == 2
    assert "refusing to overwrite" in completed.stderr
    assert copy.read_bytes() == before
    assert not (tmp_path / "logs").exists()

    forced = canary.run("--record-baseline", "--force", baseline=copy, logs=tmp_path / "logs")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert json.loads(copy.read_bytes())["recorded_at"] != json.loads(before)["recorded_at"]
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_missing_baseline_fails(recorded: tuple[Canary, Path, Path], tmp_path: Path) -> None:
    canary, _, _ = recorded
    completed = canary.run(baseline=tmp_path / "absent.json", logs=tmp_path / "logs")
    assert completed.returncode == 1
    assert "no baseline: run once with --record-baseline" in completed.stdout


def test_result_and_output_carry_no_absolute_paths(
    recorded: tuple[Canary, Path, Path],
) -> None:
    canary, baseline, logs = recorded
    inputs = canary.inputs
    needles = {
        str(inputs.fasta_path), str(inputs.bam_path), str(inputs.bam_path.parent),
        os.path.realpath(inputs.bam_path.parent), str(canary.base),
        os.path.realpath(canary.base),
    }
    for path in [*logs.iterdir(), baseline]:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, path.name
        # No absolute path of any kind in the stored JSON.
        assert '"/' not in text, path.name


def test_committed_synthetic_baseline_still_matches(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, _, _ = recorded
    committed = json.loads(SYNTHETIC_BASELINE.read_text(encoding="utf-8"))
    # Synthetic only: platform-neutral, no input digests or timings.
    assert committed["inputs"] is None and committed["wall_seconds"] is None
    completed = canary.run(baseline=SYNTHETIC_BASELINE, logs=tmp_path / "logs")
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_work_dir_under_an_input_directory_is_refused(
    recorded: tuple[Canary, Path, Path], tmp_path: Path
) -> None:
    canary, baseline, _ = recorded
    completed = subprocess.run(
        [sys.executable, str(CANARY), "--fasta", str(canary.inputs.fasta_path),
         "--bam", str(canary.inputs.bam_path), "--baseline", str(baseline),
         "--log-dir", str(tmp_path / "logs"),
         "--work-dir", str(canary.inputs.bam_path.parent), "--no-notify"],
        env=_environment(canary.home), capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert "under an input's directory" in completed.stderr


def _load_canary_module() -> Any:
    spec = importlib.util.spec_from_file_location("real_bam_canary", CANARY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repeat_mismatch_fails_as_not_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_canary_module()
    fasta = tmp_path / "in" / "r.fa"
    bam = tmp_path / "in" / "s.bam"
    fasta.parent.mkdir()
    fasta.write_text(">c\nACGT\n", encoding="ascii")
    bam.write_bytes(b"BAM")
    counts = iter([866, 865])

    def fake_run_once(*_: Any) -> dict[str, Any]:
        return {
            "metrics": {"measurement": {"eligible_alignments": next(counts)}},
            "wall_seconds": {"run": 1.0},
            "failures": [],
        }

    monkeypatch.setattr(module, "run_once", fake_run_once)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    code = module.main([
        "--fasta", str(fasta), "--bam", str(bam), "--repeat", "2", "--record-baseline",
        "--baseline", str(tmp_path / "b.json"), "--log-dir", str(tmp_path / "logs"),
        "--work-dir", str(work), "--no-notify",
    ])
    assert code == 1
    result = _latest(tmp_path / "logs")
    assert result["reproducible"] is False
    assert any(
        "not reproducible: run 1 vs run 2: measurement.eligible_alignments: "
        "expected 866, got 865" in line
        for line in result["failures"]
    )
    # A non-reproducible run is never recorded as the baseline.
    assert not (tmp_path / "b.json").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="the installer needs bash")
def test_plist_template_renders_to_valid_plist(tmp_path: Path) -> None:
    data = tmp_path / "data & <samples>"
    data.mkdir()
    fasta = data / "ref.fa"
    bam = data / "s.bam"
    for path in (fasta, Path(f"{fasta}.fai"), bam, Path(f"{bam}.bai")):
        path.write_text("x", encoding="ascii")
    environment = {
        **_environment(tmp_path),
        "PYTHON": sys.executable,
        "UV": "/opt/example/bin/uv",
        "TRACEBACK_CANARY_BASELINE": str(tmp_path / "state" / "baseline.json"),
        "TRACEBACK_CANARY_LOG_DIR": str(tmp_path / "logs"),
        "TRACEBACK_CANARY_LAUNCH_AGENTS": str(tmp_path / "agents"),
    }
    completed = subprocess.run(
        ["bash", str(INSTALLER), "render", "--fasta", str(fasta), "--bam", str(bam)],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    plist = plistlib.loads(completed.stdout.encode("utf-8"))
    assert plist["Label"] == "com.traceback.real-bam-canary"
    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    arguments = plist["ProgramArguments"]
    assert arguments[0] == "/opt/example/bin/uv"
    assert arguments[arguments.index("--fasta") + 1] == str(fasta)
    assert arguments[arguments.index("--bam") + 1] == str(bam)
    assert arguments[arguments.index("--baseline") + 1] == str(tmp_path / "state" / "baseline.json")
    assert arguments[arguments.index("--repeat") + 1] == "2"
    assert arguments[5] == f"{REPO}/scripts/canary/real_bam_canary.py"
    assert plist["WorkingDirectory"] == str(REPO)
    assert plist["StandardOutPath"].startswith(str(tmp_path / "logs"))
    # render has no side effects.
    assert not (tmp_path / "agents").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="the installer needs bash")
def test_install_refuses_without_a_baseline(tmp_path: Path) -> None:
    fasta = tmp_path / "in" / "ref.fa"
    bam = tmp_path / "in" / "s.bam"
    fasta.parent.mkdir()
    for path in (fasta, Path(f"{fasta}.fai"), bam, Path(f"{bam}.bai")):
        path.write_text("x", encoding="ascii")
    environment = {
        **_environment(tmp_path),
        "PYTHON": sys.executable,
        "UV": "/opt/example/bin/uv",
        "TRACEBACK_CANARY_BASELINE": str(tmp_path / "state" / "baseline.json"),
        "TRACEBACK_CANARY_LAUNCH_AGENTS": str(tmp_path / "agents"),
    }
    completed = subprocess.run(
        ["bash", str(INSTALLER), "install", "--fasta", str(fasta), "--bam", str(bam)],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert "no baseline" in completed.stderr and "--record-baseline" in completed.stderr
    assert not (tmp_path / "agents").exists()


def test_locator_check_finds_input_paths_and_scrubs_them(tmp_path: Path) -> None:
    module = _load_canary_module()
    fasta = tmp_path / "inputs" / "ref.fa"
    bam = tmp_path / "inputs" / "sample.bam"
    scrub = module.Scrubber(fasta, bam)
    record = tmp_path / "record"
    (record / "charts").mkdir(parents=True)
    (record / "report.html").write_text("<p>clean</p>", encoding="utf-8")
    (record / "charts" / "a.json").write_text(f'{{"source": "{bam}"}}', encoding="utf-8")
    (record / "b.log").write_bytes(f"opened {tmp_path / 'inputs'}/other\n".encode())
    assert scrub.leaks_in(record) == ["b.log", "charts/a.json"]
    assert scrub(f"read {bam} and {fasta}") == "read <BAM> and <FASTA>"
