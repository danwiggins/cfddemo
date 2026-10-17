"""``traceback lab`` CLI: ROOT resolution, output shape, problems, hand-off."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from traceback_lab import cli as lab_cli
from traceback_lab.problems import PROBLEMS, docs_anchor
from traceback_lab.store import LabStore
from traceback_runner import cli as signed_cli

REPO = Path(__file__).resolve().parents[2]


def _run(capsys, argv):
    code = signed_cli.main(["lab", *argv])
    out = capsys.readouterr().out
    return code, out


def test_root_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRACEBACK_ROOT", raising=False)
    assert lab_cli.resolve_root(None) == tmp_path / ".traceback"
    monkeypatch.setenv("TRACEBACK_ROOT", str(tmp_path / "env-root"))
    assert lab_cli.resolve_root(None) == tmp_path / "env-root"
    assert lab_cli.resolve_root(Path("flag-root")) == tmp_path / "flag-root"


def test_first_human_line_is_root(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TRACEBACK_ROOT", str(tmp_path / "shared"))
    for command in (["algorithms"], ["runs"]):
        code, out = _run(capsys, command)
        assert code == 0
        assert out.splitlines()[0] == f"ROOT  {tmp_path / 'shared'}"


def test_json_is_one_object_with_root(tmp_path: Path, capsys) -> None:
    code, out = _run(capsys, ["runs", "--json", "--root", str(tmp_path)])
    assert code == 0
    payload = json.loads(out)
    assert out.count("\n") == 1
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert payload["data"]["root"] == str(tmp_path)
    assert payload["data"]["runs"] == []


def test_runs_lists_and_shows_steps(tmp_path: Path, capsys) -> None:
    store = LabStore(tmp_path)
    store.ensure()
    run_id = "r-20261014-2104-7f3a"
    log = store.run_log(run_id)
    log.append("run_start", command="run", sample="3f9a1c2b7e04")
    log.append("step_start", step="align")
    log.append("step_end", step="align", status="ok", duration_s=12.0, peak_rss_bytes=1024)
    log.append("step_start", step="fragment-size")
    log.append("step_end", step="fragment-size", status="failed", first_line="boom")
    log.append("run_end", status="partial_failure", exit_code=8)
    code, out = _run(capsys, ["runs", "--root", str(tmp_path)])
    assert code == 0 and run_id in out and "3f9a1c2b7e04" in out and "failed=1 ok=1" in out
    code, out = _run(capsys, ["runs", run_id, "--step", "align", "--json", "--root", str(tmp_path)])
    payload = json.loads(out)
    assert code == 0
    assert {event["step"] for event in payload["data"]["events"]} == {"align"}
    assert payload["data"]["run"]["status"] == "partial_failure"


@pytest.mark.parametrize("as_json", [False, True])
def test_unknown_run_is_tbx_lab_017(tmp_path: Path, capsys, as_json: bool) -> None:
    argv = ["runs", "r-20261014-2104-0000", "--root", str(tmp_path)] + (["--json"] if as_json else [])
    code, out = _run(capsys, argv)
    assert code == 4
    if as_json:
        data = json.loads(out)["data"]
        assert data["code"] == "TBX-LAB-017"
        assert data["docs"] == "docs/LAB-GUIDE.md#tbx-lab-017"
        assert {"cause", "fix", "retryable", "root"} <= set(data)
    else:
        lines = out.splitlines()
        assert lines[0] == f"ROOT  {tmp_path}"
        for label in ("CODE", "CAUSE", "FIX", "RETRYABLE", "DOCS"):
            assert any(line.startswith(label) for line in lines), label


def test_newer_store_is_tbx_lab_018(tmp_path: Path, capsys) -> None:
    store = LabStore(tmp_path)
    store.lab.mkdir(parents=True)
    store.version_file.write_text("2\n")
    code, out = _run(capsys, ["runs", "--json", "--root", str(tmp_path)])
    data = json.loads(out)["data"]
    assert code == 3
    assert data["code"] == "TBX-LAB-018"
    assert data["store_version"] == 2


def test_internal_error_is_tbx_lab_020(tmp_path: Path, capsys, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("secret /Users/someone/path")

    monkeypatch.setattr(lab_cli, "load_registry", boom)
    code, out = _run(capsys, ["algorithms", "--json", "--root", str(tmp_path)])
    data = json.loads(out)["data"]
    assert code == 7
    assert data["code"] == "TBX-LAB-020"
    assert "/Users" not in out


def test_runs_command_writes_nothing(tmp_path: Path, capsys) -> None:
    root = tmp_path / "fresh"
    _run(capsys, ["runs", "--root", str(root)])
    _run(capsys, ["algorithms", "--root", str(root)])
    assert not root.exists()


def test_signed_help_lists_lab(capsys) -> None:
    with pytest.raises(SystemExit):
        signed_cli.main(["--help"])
    assert "lab" in capsys.readouterr().out


def _raised_codes() -> set[str]:
    codes: set[str] = set()
    for path in (REPO / "traceback_lab").rglob("*.py"):
        if path.name == "problems.py":
            continue
        codes.update(re.findall(r'"(TBX-LAB-\d{3})"', path.read_text()))
    return codes


def test_raised_codes_and_guide_anchors_agree() -> None:
    guide = (REPO / "docs" / "LAB-GUIDE.md").read_text()
    anchors = set(re.findall(r"^### (TBX-LAB-\d{3})$", guide, flags=re.MULTILINE))
    raised = _raised_codes()
    assert raised <= set(PROBLEMS)
    assert raised == anchors, (sorted(raised - anchors), sorted(anchors - raised))
    for code in raised:
        assert docs_anchor(code) == f"docs/LAB-GUIDE.md#{code.lower()}"


def test_problem_table_exit_codes() -> None:
    assert PROBLEMS["TBX-LAB-015"].exit_code == 6
    assert PROBLEMS["TBX-LAB-016"].exit_code == 8
    assert PROBLEMS["TBX-LAB-019"].exit_code == 0
    assert PROBLEMS["TBX-LAB-025"].exit_code == 0
    if "TBX-LAB-024" in PROBLEMS:
        assert PROBLEMS["TBX-LAB-024"].exit_code == 3  # platform refusal, when that lane is integrated
    assert len(PROBLEMS) >= 24


def test_usage_errors_keep_the_json_contract(tmp_path: Path, capsys) -> None:
    code = signed_cli.main(["lab", "runs", "--json", "--root", str(tmp_path), "--step"])
    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.out)
    assert payload["status"] == "usage"
    assert payload["data"]["root"] == str(tmp_path)
    code = signed_cli.main(["lab", "bogus"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == "" and "invalid choice" in captured.err
