"""The golden-path canary's ``--analysis cell-origin`` (signal CO6).

The canary's CLI steps run in-process here (``_cli`` is replaced) so the
cell-origin stage can use the tests' fake modkit; the subprocess path is the
one ``tests/test_canary.py`` covers for fragment length.  All values are
synthetic; no baseline or log is written inside the repository.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_cell_origin_stage import Setup
from traceback_runner import cli
from traceback_runner.analyses import ReadinessRow

REPO = Path(__file__).resolve().parents[1]
CANARY = REPO / "scripts" / "canary" / "real_bam_canary.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("real_bam_canary_co6", CANARY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _in_process(args: list[str], timeout: float) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(args)
    return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue()), (
        time.monotonic() - started
    )


@pytest.fixture
def canary(tmp_path: Path, capsys, monkeypatch) -> tuple[Any, Setup]:
    setup = Setup(tmp_path, capsys, monkeypatch)
    # The modkit pin is the fake's; its readiness row is the stage's own concern.
    monkeypatch.setattr(
        cli, "_modkit_readiness", lambda: ReadinessRow("TBX-TOOL-001", "ready", "verified")
    )
    module = _module()
    monkeypatch.setattr(module, "_cli", _in_process)
    return module, setup


def _once(module: Any, setup: Setup, work: Path) -> dict[str, Any]:
    scrub = module.Scrubber(setup.inputs.fasta_path, setup.inputs.bam_path)
    return module.run_once(
        setup.inputs.fasta_path, setup.inputs.bam_path, work, scrub, 600.0, lambda _: None,
        analysis="cell-origin", modbase_model="model-x", loyfer_dir=setup.inputs.loyfer_dir,
    )


def test_cell_origin_run_collects_its_metrics_reproducibly(canary, tmp_path: Path) -> None:
    module, setup = canary
    first = _once(module, setup, tmp_path / "w1")
    second = _once(module, setup, tmp_path / "w2")
    assert first["failures"] == [], first
    assert module.diff_metrics(first["metrics"], second["metrics"]) == []
    metrics = first["metrics"]
    measurement = metrics["measurement"]
    assert measurement["schema_version"] == "traceback.cell-origin-measurement.v1"
    assert set(measurement) == {"schema_version", "denominators", "fractions", "residual_l2"}
    assert sum(measurement["fractions"].values()) == pytest.approx(1.0)
    assert measurement["denominators"]["classified_fragments"] > 0
    assert metrics["preflight"]["readiness"]["readiness"] == "ready"
    assert metrics["exit_codes"]["assets"] == 0
    assert metrics["verified"] is True and metrics["locator_clean"] is True
    assert metrics["report"] == {"local_banner": True, "mentions_synthetic": False}
    assert len(metrics["measurement_sha256"]) == 64


def test_measurement_path_comes_from_the_manifest(tmp_path: Path) -> None:
    module = _module()

    def manifest(*paths: str) -> Path:
        record = tmp_path / f"r{len(list(tmp_path.iterdir()))}"
        record.mkdir()
        contents = [{"relative_path": path} for path in paths]
        (record / "bundle-manifest.json").write_text(json.dumps({"contents": contents}))
        return record

    good = manifest("charts/cell-origin.v1.json", "measurements/cell-origin.v1.json")
    assert module.measurement_relative(good) == Path("measurements/cell-origin.v1.json")
    for paths in (
        (),
        ("measurements/a.json", "measurements/b.json"),
        ("measurements/../escape.json",),
    ):
        with pytest.raises(ValueError):
            module.measurement_relative(manifest(*paths))


def test_each_analysis_has_its_own_default_baseline(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setenv("TRACEBACK_CANARY_BASELINE", str(tmp_path / "state" / "baseline.json"))
    assert module._default_baseline() == tmp_path / "state" / "baseline.json"
    assert module._default_baseline("cell-origin") == (
        tmp_path / "state" / "baseline-cell-origin.json"
    )
    monkeypatch.delenv("TRACEBACK_CANARY_BASELINE")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert module._default_baseline("cell-origin") == (
        tmp_path / ".config" / "traceback-canary" / "baseline-cell-origin.json"
    )


def test_cell_origin_options_need_cell_origin() -> None:
    module = _module()
    for extra in (
        ["--modbase-model", "m"],
        ["--loyfer-dir", "d"],
        ["--analysis", "cell-origin"],  # no --loyfer-dir
    ):
        with pytest.raises(SystemExit) as exit_info:
            module.parse_args(["--fasta", "f", "--bam", "b", *extra])
        assert exit_info.value.code == 2, extra
    args = module.parse_args(["--fasta", "f", "--bam", "b"])
    assert args.analysis == "fragment" and args.modbase_model is None
    with pytest.raises(SystemExit):
        module.parse_args(["--fasta", "f", "--bam", "b", "--analysis", "copy-number"])


def test_record_then_compare_and_fraction_drift_fails(canary, tmp_path: Path) -> None:
    module, setup = canary
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    work = tmp_path / "work"
    work.mkdir()
    base = [
        "--fasta", str(setup.inputs.fasta_path), "--bam", str(setup.inputs.bam_path),
        "--analysis", "cell-origin", "--modbase-model", "model-x",
        "--loyfer-dir", str(setup.inputs.loyfer_dir),
        "--log-dir", str(logs), "--work-dir", str(work), "--no-notify",
        "--baseline", str(state / "baseline-cell-origin.json"),
    ]
    assert module.main([*base, "--record-baseline", "--repeat", "2"]) == 0
    latest = json.loads((logs / "latest.json").read_text())
    assert latest["analysis"] == "cell-origin" and latest["reproducible"] is True
    assert module.main(base) == 0
    baseline_path = state / "baseline-cell-origin.json"
    baseline = json.loads(baseline_path.read_text())
    contributor = sorted(baseline["metrics"]["measurement"]["fractions"])[0]
    baseline["metrics"]["measurement"]["fractions"][contributor] += 1e-12
    baseline_path.write_text(json.dumps(baseline))
    assert module.main(base) == 1
    latest = json.loads((logs / "latest.json").read_text())
    lines = [line for line in latest["failures"] if f"measurement.fractions.{contributor}" in line]
    assert lines and all(line.endswith("changed (value withheld)") for line in lines), lines
    fraction = baseline["metrics"]["measurement"]["fractions"][contributor]
    assert json.dumps(fraction) not in json.dumps(latest["failures"])
    assert json.dumps(fraction - 1e-12) not in json.dumps(latest["failures"])


def test_a_blocked_readiness_fails_the_canary(canary, tmp_path: Path, monkeypatch) -> None:
    module, setup = canary
    monkeypatch.setattr(
        cli, "_modkit_readiness", lambda: ReadinessRow("TBX-TOOL-001", "missing", "not installed")
    )
    outcome = _once(module, setup, tmp_path / "w")
    assert "preflight says cell-origin is not ready" in outcome["failures"]
    assert outcome["metrics"]["preflight"]["readiness"]["readiness"] == "blocked"
