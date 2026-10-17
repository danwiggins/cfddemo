"""D36 human and JSON problem fields for Lab run outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceback_lab.cli import emit
from traceback_lab.run_command import _aligner_warning, _summary
from traceback_lab.runner import StepResult
from traceback_lab.store import LabStore


def _run_summary(store: LabStore, *, failed: bool, warning: bool):
    aligned = SimpleNamespace(manifest={}, source_id="a" * 64, input_id="b" * 64)
    results = [
        StepResult("fragment-size", "failed", False, "c" * 64, first_line="synthetic failure")
    ] if failed else []
    warnings = [
        _aligner_warning(
            {"old": {"minimap2": "2.28"}, "new": {"minimap2": "2.29"}}, 1.0, 2.0,
        )
    ] if warning else []
    return _summary(
        store, "r-20261016-1200-0000", aligned, "a" * 12,
        None, results, warnings, 1.0,
    )


@pytest.mark.parametrize("as_json", [False, True])
def test_partial_failure_has_standard_problem_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool,
) -> None:
    store = LabStore(tmp_path)
    code, payload = _run_summary(store, failed=True, warning=False)
    emit(payload, as_json=as_json, root=store.root)
    out = capsys.readouterr().out

    assert code == 8
    if as_json:
        data = json.loads(out)["data"]
        assert out.count("\n") == 1
        assert data["code"] == "TBX-LAB-016"
        assert data["cause"] == "1 algorithm(s) failed: fragment-size"
        assert "traceback lab runs r-20261016-1200-0000 --step fragment-size" in data["fix"]
        assert data["retryable"] is False
        assert data["docs"].endswith("#tbx-lab-016")
        assert data["counts"]["failed"] == 1
    else:
        assert out.splitlines()[0] == f"ROOT    {store.root}"
        for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
            assert any(line.startswith(label) for line in out.splitlines()), label
        assert "CODE       TBX-LAB-016" in out
        assert "FAILED  fragment-size: synthetic failure" in out
        assert "--step fragment-size" in out


@pytest.mark.parametrize("as_json", [False, True])
def test_aligner_warning_has_standard_problem_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool,
) -> None:
    store = LabStore(tmp_path)
    code, payload = _run_summary(store, failed=False, warning=True)
    emit(payload, as_json=as_json, root=store.root)
    out = capsys.readouterr().out

    assert code == 0
    if as_json:
        result = json.loads(out)
        assert out.count("\n") == 1
        (warning,) = result["data"]["warnings"]
        assert warning["code"] == "TBX-LAB-019"
        assert "2.28" in warning["cause"] and "2.29" in warning["cause"]
        assert "--dry-run" in warning["fix"]
        assert warning["retryable"] is False
        assert warning["docs"].endswith("#tbx-lab-019")
        assert warning["old"] == {"minimap2": "2.28"}
        assert warning["new"] == {"minimap2": "2.29"}
        assert warning["estimate_min"] == [1.0, 2.0]
    else:
        assert "WARN    Aligner identity changed" in out
        assert "CODE       TBX-LAB-019" in out
        for label in ("CAUSE", "FIX", "RETRYABLE", "DOCS"):
            assert any(line.startswith(label) for line in out.splitlines()), label
