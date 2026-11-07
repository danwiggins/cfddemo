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


def test_signed_cli_resolves_root_with_the_lab_resolver() -> None:
    from traceback_runner.filesystem import resolve_root

    assert lab_cli.resolve_root is resolve_root


def test_suite_starts_without_an_ambient_traceback_root() -> None:
    import os

    assert "TRACEBACK_ROOT" not in os.environ  # tests/conftest.py


def test_resolve_root_expands_home_and_dotdot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert lab_cli.resolve_root(Path("~/h")) == tmp_path / "h"
    assert lab_cli.resolve_root(Path("a/../b")) == tmp_path / "b"


def _signed(capsys, argv):
    code = signed_cli.main(argv)
    return code, capsys.readouterr().out


def test_signed_and_lab_share_env_root_across_cwds(tmp_path: Path, capsys, monkeypatch) -> None:
    """With TRACEBACK_ROOT set, a signed command and a Lab command started from
    different directories use the same store and both print it."""

    shared = tmp_path / "shared"
    monkeypatch.setenv("TRACEBACK_ROOT", str(shared))
    (tmp_path / "signed-cwd").mkdir()
    (tmp_path / "lab-cwd").mkdir()

    monkeypatch.chdir(tmp_path / "signed-cwd")
    code, out = _signed(capsys, ["jobs"])
    assert code == 0
    assert out.splitlines()[1] == f"ROOT  {shared}"
    code, out = _signed(capsys, ["doctor"])
    assert out.splitlines()[1] == f"ROOT  {shared}"
    assert out.count(f"ROOT  {shared}") == 1  # doctor's data.root is that line
    code, out = _signed(capsys, ["doctor", "--json"])
    assert json.loads(out)["data"]["root"] == str(shared)
    assert not (tmp_path / "signed-cwd" / ".traceback").exists()

    monkeypatch.chdir(tmp_path / "lab-cwd")
    code, out = _run(capsys, ["runs"])
    assert code == 0
    assert out.splitlines()[0] == f"ROOT  {shared}"
    code, out = _run(capsys, ["runs", "--json"])
    assert json.loads(out)["data"]["root"] == str(shared)
    assert not (tmp_path / "lab-cwd" / ".traceback").exists()


def test_signed_write_without_root_goes_to_the_exported_store(tmp_path: Path, capsys, monkeypatch) -> None:
    """A signed write command started elsewhere with TRACEBACK_ROOT exported
    writes to that store and creates nothing under its cwd."""

    from traceback_runner.fixtures import create_local_golden_path_inputs

    inputs = create_local_golden_path_inputs(tmp_path / "inputs", eligible=False)
    store = tmp_path / "store"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.setenv("TRACEBACK_ROOT", str(store))
    monkeypatch.chdir(cwd)
    code, out = _signed(capsys, ["reference", "register", "--fasta", str(inputs.fasta_path), "--id", "ref"])
    assert code == 0, out
    assert out.splitlines()[1] == f"ROOT  {store}"
    assert (store / "references" / "ref").is_dir()
    assert any((store / "references" / "ref").iterdir())
    assert list(cwd.iterdir()) == []


def test_signed_relative_root_resolves_under_cwd_and_json_carries_no_path(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = _signed(capsys, ["jobs", "--root", "rel/dir"])
    assert code == 0
    assert out.splitlines()[1] == f"ROOT  {tmp_path / 'rel' / 'dir'}"
    code, out = _signed(capsys, ["jobs", "--json", "--root", "rel/dir"])
    assert code == 0
    assert str(tmp_path) not in out and "rel/dir" not in out and "root" not in json.loads(out)["data"]


def test_signed_explicit_root_overrides_env(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRACEBACK_ROOT", str(tmp_path / "env-root"))
    code, out = _signed(capsys, ["jobs", "--root", "flag-root"])
    assert code == 0
    assert out.splitlines()[1] == f"ROOT  {tmp_path / 'flag-root'}"
    code, out = _signed(capsys, ["doctor", "--json", "--root", "flag-root"])
    assert json.loads(out)["data"]["root"] == str(tmp_path / "flag-root")
    code, out = _run(capsys, ["runs", "--root", "flag-root"])
    assert out.splitlines()[0] == f"ROOT  {tmp_path / 'flag-root'}"


def test_signed_unset_env_falls_back_to_dot_traceback(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRACEBACK_ROOT", raising=False)
    code, out = _signed(capsys, ["jobs"])
    assert code == 0
    assert out.splitlines()[1] == f"ROOT  {tmp_path / '.traceback'}"
    code, out = _signed(capsys, ["doctor", "--json"])
    assert json.loads(out)["data"]["root"] == str(tmp_path / ".traceback")
    code, out = _signed(capsys, ["jobs", "--json"])
    assert "root" not in json.loads(out)["data"]  # signed --json bytes are unchanged
    code, out = _run(capsys, ["runs"])
    assert out.splitlines()[0] == f"ROOT  {tmp_path / '.traceback'}"


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


def test_sex_flag_help_states_its_preanalytic_limit(capsys) -> None:
    with pytest.raises(SystemExit):
        signed_cli.main(["lab", "run", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--sex {M,F}" in help_text
    assert "not a diagnostic sex or gender determination" in help_text


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
    assert PROBLEMS["TBX-LAB-024"].exit_code == 3
    assert PROBLEMS["TBX-LAB-025"].exit_code == 0
    assert PROBLEMS["TBX-LAB-026"].exit_code == 3 and PROBLEMS["TBX-LAB-026"].retryable
    assert PROBLEMS["TBX-LAB-027"].exit_code == 3 and PROBLEMS["TBX-LAB-027"].retryable
    assert PROBLEMS["TBX-LAB-030"].exit_code == 8
    assert len(PROBLEMS) == 28


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
