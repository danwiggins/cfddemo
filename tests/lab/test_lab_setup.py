"""``traceback lab setup``: runs the steps ``lab doctor`` lists, asks before downloads, never guesses a path."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.lab.test_cli_error_triggers import _assert_both_modes
from traceback_lab import setup_command
from traceback_lab.cli import build_parser
from traceback_lab.doctor import lab_checks
from traceback_lab.ingest import mmi_path, resolve_reference
from traceback_lab.scrub import Scrubber
from traceback_lab.setup_command import (
    DOWNLOAD,
    FLAGS,
    INPUT,
    LOCAL,
    NO_TTY_JSON,
    SetupContext,
    StepFailed,
    StepSpec,
    run_signed,
    step_names,
)
from traceback_lab.store import LabStore, new_run_id
from traceback_runner import cli as signed_cli

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "LAB-GUIDE.md"
JSON_STEP_KEYS = {"name", "kind", "status", "reason", "command", "flag", "exit_code", "stderr_tail"}
JSON_DATA_KEYS = {"root", "yes", "only", "interactive", "steps", "counts", "doctor_before", "doctor_after"}


def _run(capsys, argv: list[str]) -> tuple[int, str]:
    code = signed_cli.main(["lab", "setup", *argv])
    return code, capsys.readouterr().out


def _json(capsys, argv: list[str]) -> tuple[int, dict]:
    code, out = _run(capsys, [*argv, "--json"])
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["schema_version"] == "traceback.cli-result.v2"
    return code, payload


def _steps(out: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^STEP  (\S+)  (\S+)", out, re.M)]


class FakeWorld:
    """A doctor whose checks pass once their fake step has run; every runner is local-only."""

    def __init__(self, monkeypatch, tmp_path: Path, checks: list[tuple[str, str, str]]) -> None:
        self.tmp_path = tmp_path
        self.order = [name for name, _kind, _fix in checks]
        self.fixes = {name: fix for name, _kind, fix in checks}
        self.done: set[str] = set()
        self.calls: list[str] = []
        catalogue: dict[str, StepSpec] = {}
        for name, kind, _fix in checks:
            if kind in {LOCAL, DOWNLOAD}:
                catalogue[name] = StepSpec(kind, run=self._runner(name), after=("ichor_toolchain",) if name == "ichor_assets" else ())
            elif kind == INPUT:
                catalogue[name] = StepSpec(INPUT, placeholder="LOYFER_DIR" if name == "loyfer_assets" else "hg38.fa")
        self.failing: dict[str, StepFailed] = {}
        monkeypatch.setattr(setup_command, "CATALOGUE", catalogue)
        monkeypatch.setattr(setup_command, "lab_checks", self.checks)
        monkeypatch.setattr(setup_command, "_interactive", lambda: False)

    def _runner(self, name: str):
        def run(context: SetupContext) -> str:
            self.calls.append(name)
            failure = self.failing.get(name)
            if failure is not None:
                raise failure
            (self.tmp_path / f"{name}.done").write_text("ok")
            self.done.add(name)
            return f"fake {name} done"

        return run

    def checks(self, store: LabStore) -> list[dict[str, Any]]:
        rows = [{"name": "python", "status": "pass", "detail": "Python 3.11", "blocks_example": False}]
        for name in self.order:
            if name in self.done:
                rows.append({"name": name, "status": "pass", "detail": "done", "blocks_example": False})
            else:
                row = {"name": name, "status": "warn", "detail": "missing", "blocks_example": False}
                if self.fixes[name] is not None:
                    row["fix"] = self.fixes[name]
                rows.append(row)
        return rows


@pytest.fixture
def world(monkeypatch, tmp_path: Path) -> FakeWorld:
    return FakeWorld(monkeypatch, tmp_path, [
        ("mmi", LOCAL, "build the index"),
        ("ichor_toolchain", DOWNLOAD, "traceback toolchain install ichor"),
        ("ichor_assets", LOCAL, "traceback method-asset register --from-toolchain copy-number --root <ROOT>"),
        ("staging", LOCAL, "traceback lab gc --staging-only --apply --root <ROOT>"),
    ])


def test_all_local_steps_run_in_doctor_order_and_exit_0(world: FakeWorld, tmp_path: Path, capsys) -> None:
    world.order = ["mmi", "ichor_assets", "staging"]
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root)])
    assert code == 0, out
    assert out.splitlines()[0] == f"ROOT  {root}"
    assert world.calls == ["mmi", "ichor_assets", "staging"]
    assert _steps(out) == [("mmi", "ok"), ("ichor_assets", "ok"), ("staging", "ok")]
    assert "FLAG" not in out and "[y/N]" not in out
    runs = out.index("RUN   build the index")
    assert runs < out.index("STEP  mmi  ok")
    assert "PASS  setup complete: 3 ok" in out
    assert "DOCTOR  Lab ready for `traceback lab run --example`" in out
    # One ``setup`` run in the run log, with a step per executed step.
    signed_cli.main(["lab", "runs", "--root", str(root), "--json"])
    rows = json.loads(capsys.readouterr().out)["data"]["runs"]
    assert [row["command"] for row in rows] == ["setup"] and rows[0]["status"] == "ok"
    assert rows[0]["steps"] == {"ok": 3}


def test_download_step_is_refused_without_yes_when_not_a_tty(world: FakeWorld, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root)])
    assert code == 8, out
    assert "ichor_toolchain" not in world.calls and world.calls == ["mmi", "staging"]
    assert _steps(out) == [("mmi", "ok"), ("ichor_toolchain", "refused"), ("ichor_assets", "skipped"), ("staging", "ok")]
    flag = out.index(f"FLAG  {FLAGS['ichor_toolchain']}")
    assert flag < out.index("STEP  ichor_toolchain  refused")
    assert "non-interactive and no --yes" in out
    assert "needs ichor_toolchain first (refused)" in out
    assert "PARTIAL_FAILURE  setup incomplete" in out and "rerun with --yes" in out
    assert "CODE " not in out  # nothing failed: no problem block


def test_yes_runs_the_download_step_after_its_flag_line(world: FakeWorld, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root), "--yes"])
    assert code == 0, out
    assert world.calls == ["mmi", "ichor_toolchain", "ichor_assets", "staging"]
    assert _steps(out) == [("mmi", "ok"), ("ichor_toolchain", "ok"), ("ichor_assets", "ok"), ("staging", "ok")]
    flag = out.index(f"FLAG  {FLAGS['ichor_toolchain']}")
    assert flag < out.index("RUN   traceback toolchain install ichor") < out.index("STEP  ichor_toolchain  ok")
    assert (tmp_path / "ichor_toolchain.done").is_file()


@pytest.mark.parametrize("answer,status,ran", [("y", "ok", True), ("yes", "ok", True), ("", "refused", False), ("n", "refused", False)])
def test_tty_prompt_per_download_step(world: FakeWorld, tmp_path: Path, capsys, monkeypatch, answer, status, ran) -> None:
    monkeypatch.setattr(setup_command, "_interactive", lambda: True)
    prompts: list[str] = []
    # A terminal echoes the typed answer and its newline; the fake does the same.
    monkeypatch.setattr("builtins.input", lambda: (prompts.append("asked"), print(answer), answer)[2])
    code, out = _run(capsys, ["--root", str(tmp_path / "root"), "--only", "ichor_toolchain"])
    assert prompts == ["asked"]
    assert "Run the ichor_toolchain step?" in out and "[y/N]" in out
    assert ("ichor_toolchain" in world.calls) is ran
    assert _steps(out) == [("ichor_toolchain", status)]
    assert code == (0 if ran else 8)
    if not ran:
        assert "declined at the prompt" in out


@pytest.mark.parametrize("tty", [True, False])
def test_json_never_prompts_and_refuses_without_yes(world: FakeWorld, tmp_path: Path, capsys, monkeypatch, tty) -> None:
    monkeypatch.setattr(setup_command, "_interactive", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted under --json"))
    root = tmp_path / "root"
    code = signed_cli.main(["lab", "setup", "--root", str(root), "--only", "ichor_toolchain,ichor_assets", "--json"])
    captured = capsys.readouterr()
    assert code == 8 and captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    step = payload["data"]["steps"][0]
    assert step["status"] == "refused" and step["reason"].startswith(NO_TTY_JSON)
    assert step["flag"] == FLAGS["ichor_toolchain"]
    assert payload["data"]["steps"][1]["status"] == "skipped"
    assert payload["summary"].endswith("rerun with --yes")
    assert world.calls == []
    flag = captured.err.index(f"FLAG  {FLAGS['ichor_toolchain']}")
    assert flag < captured.err.index("STEP  ichor_toolchain  refused")
    assert "[y/N]" not in captured.err and "[y/N]" not in captured.out


def test_json_with_yes_runs_the_download_step(world: FakeWorld, tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted under --json"))
    code = signed_cli.main(["lab", "setup", "--root", str(tmp_path / "root"), "--only", "ichor_toolchain", "--yes", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0 and world.calls == ["ichor_toolchain"]
    assert payload["data"]["steps"][0]["status"] == "ok" and payload["data"]["steps"][0]["flag"] == FLAGS["ichor_toolchain"]
    assert captured.err.index(f"FLAG  {FLAGS['ichor_toolchain']}") < captured.err.index("RUN   traceback toolchain install ichor")


def test_needs_input_prints_the_template_and_never_guesses_a_path(monkeypatch, tmp_path: Path, capsys) -> None:
    world = FakeWorld(monkeypatch, tmp_path, [
        ("loyfer_assets", INPUT, "traceback method-asset register --from-dir LOYFER_DIR --root <ROOT>"),
        ("staging", LOCAL, "traceback lab gc --staging-only --apply --root <ROOT>"),
    ])
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root)])
    assert code == 8, out
    assert _steps(out) == [("loyfer_assets", "needs_input"), ("staging", "ok")]
    assert f"FLAG  {FLAGS['loyfer_assets']}" in out
    assert f"TEMPLATE  traceback method-asset register --from-dir LOYFER_DIR --root {root}" in out
    assert "replace LOYFER_DIR with your local path" in out
    assert world.calls == ["staging"]
    assert "supply the printed template paths" in out
    code, payload = _json(capsys, ["--root", str(root)])
    step = payload["data"]["steps"][0]
    assert step["status"] == "needs_input" and "LOYFER_DIR" in step["command"]
    assert step["command"].endswith("--root <ROOT>")
    assert "--from-dir LOYFER_DIR" in step["reason"]
    # No host path other than data.root, and no guessed directory.
    assert str(tmp_path) not in json.dumps({k: v for k, v in payload["data"].items() if k != "root"})


def test_manual_steps_are_skipped_with_their_command(monkeypatch, tmp_path: Path, capsys) -> None:
    world = FakeWorld(monkeypatch, tmp_path, [("lab_minimap2", "manual", "brew install minimap2")])
    del world
    code, out = _run(capsys, ["--root", str(tmp_path / "root")])
    assert code == 0
    assert _steps(out) == [("lab_minimap2", "skipped")]
    assert "STEP  lab_minimap2  skipped  manual; run it yourself: brew install minimap2" in out


def test_a_failing_step_is_tbx_lab_032_and_later_steps_still_run(world: FakeWorld, tmp_path: Path, capsys) -> None:
    world.failing["mmi"] = StepFailed("exit 1", exit_code=1, tail=["minimap2: cannot open <path>", "<redacted> aborted"])
    root = tmp_path / "root"
    human, data = _assert_both_modes(capsys, ["setup", "--root", str(root), "--yes"], "TBX-LAB-032", 8)
    assert data["retryable"] is True
    # The helper ran twice: the human run attempted all four steps; the --json run found only mmi left.
    assert world.calls == ["mmi", "ichor_toolchain", "ichor_assets", "staging", "mmi"]
    assert _steps(human) == [("mmi", "failed"), ("ichor_toolchain", "ok"), ("ichor_assets", "ok"), ("staging", "ok")]
    assert "FAILED  mmi  exit 1" in human and "  | minimap2: cannot open <path>" in human
    assert human.index("FAILED  mmi") < human.index("CODE       TBX-LAB-032")
    assert data["failed"] == ["mmi"]
    failed = next(step for step in data["steps"] if step["name"] == "mmi")
    assert failed["exit_code"] == 1 and failed["stderr_tail"] == ["minimap2: cannot open <path>", "<redacted> aborted"]
    assert "CAUSE      1 of 4 setup step(s) failed; first: mmi (exit 1); 3 ok, 1 failed" in human
    assert data["cause"].startswith("1 of 1 setup step(s) failed; first: mmi (exit 1)")
    assert "traceback lab setup --root <ROOT>" in data["fix"]
    assert data["counts"] == {"ok": 0, "skipped": 0, "needs_input": 0, "refused": 0, "failed": 1}
    signed_cli.main(["lab", "runs", "--root", str(root), "--json"])
    rows = json.loads(capsys.readouterr().out)["data"]["runs"]
    assert all(row["command"] == "setup" and row["status"] == "partial_failure" for row in rows)
    with LabStore(root).writer("run", new_run_id(), step="ingest"):  # the failed run released the lock
        pass


def test_a_step_that_raises_is_failed_with_a_scrubbed_reason(world: FakeWorld, tmp_path: Path, capsys) -> None:
    def explode(context: SetupContext) -> str:
        raise RuntimeError(f"secret {tmp_path}/private.txt and /Users/someone/kit")

    setup_command.CATALOGUE["staging"] = StepSpec(LOCAL, run=explode)
    code, payload = _json(capsys, ["--root", str(tmp_path / "root"), "--only", "staging"])
    assert code == 8 and payload["data"]["code"] == "TBX-LAB-032"
    step = payload["data"]["steps"][0]
    assert step["status"] == "failed" and step["reason"] == "RuntimeError while running the step"
    assert step["stderr_tail"] == ["secret <path> and <path>"]
    assert "/Users" not in json.dumps(payload) and str(tmp_path) not in json.dumps(payload["data"]["steps"])


def test_refused_while_another_writer_holds_the_lock(world: FakeWorld, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    store = LabStore(root)
    run_id = new_run_id()
    with store.writer("run", run_id, step="align"):
        human, data = _assert_both_modes(capsys, ["setup", "--root", str(root), "--yes"], "TBX-LAB-012", 3)
    assert data["holder"]["run_id"] == run_id and data["retryable"] is True
    assert world.calls == []
    assert "STEP" not in human


def test_only_filters_steps_and_reports_satisfied_ones(world: FakeWorld, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root), "--only", "staging,python"])
    assert code == 0, out
    assert world.calls == ["staging"]
    assert _steps(out) == [("python", "skipped"), ("staging", "ok")]
    assert "STEP  python  skipped  nothing to do: doctor passes this check" in out
    code, payload = _json(capsys, ["--root", str(root), "--only", "mmi,python,mmi,python"])
    assert payload["data"]["only"] == ["mmi", "python"]
    assert [(s["name"], s["kind"], s["status"]) for s in payload["data"]["steps"]] == [("python", "manual", "skipped"), ("mmi", "local", "ok")]
    # ichor_assets alone runs even though ichor_toolchain is unconfirmed: the dependency only holds inside one run.
    code, out = _run(capsys, ["--root", str(root), "--only", "ichor_assets"])
    assert code == 0 and _steps(out) == [("ichor_assets", "ok")]


def test_only_reasons_come_from_the_checks_own_status(monkeypatch, tmp_path: Path, capsys) -> None:
    world = FakeWorld(monkeypatch, tmp_path, [("ram", "manual", None), ("staging", LOCAL, "gc")])
    world.done.add("staging")
    code, out = _run(capsys, ["--root", str(tmp_path / "root"), "--only", "ram,staging,loyfer_assets"])
    assert code == 0 and world.calls == []
    assert "STEP  ram  skipped  doctor reports warn; no automatic fix" in out
    assert "STEP  staging  skipped  nothing to do: doctor passes this check" in out
    assert "STEP  loyfer_assets  skipped  doctor lists no check with this name this run" in out


def test_an_installed_toolchain_satisfies_the_assets_prerequisite(world: FakeWorld, tmp_path: Path, capsys) -> None:
    world.done.add("ichor_toolchain")  # doctor passes it; nothing to confirm or download
    code, out = _run(capsys, ["--root", str(tmp_path / "root"), "--only", "ichor_toolchain,ichor_assets"])
    assert code == 0, out
    assert world.calls == ["ichor_assets"]
    assert _steps(out) == [("ichor_toolchain", "skipped"), ("ichor_assets", "ok")]
    assert "FLAG" not in out and "[y/N]" not in out


def test_manual_and_blocked_steps_print_no_flag_line(monkeypatch, tmp_path: Path, capsys) -> None:
    world = FakeWorld(monkeypatch, tmp_path, [
        ("pmd_asset", "manual", "Restore the original pinned PMD bytes, then run traceback lab asset inspect pmd --root <ROOT>"),
        ("ichor_toolchain", DOWNLOAD, "traceback toolchain install ichor"),
        ("ichor_assets", LOCAL, "traceback method-asset register --from-toolchain copy-number --root <ROOT>"),
    ])
    code, payload = _json(capsys, ["--root", str(tmp_path / "root")])
    assert code == 8 and world.calls == []
    by_name = {step["name"]: step for step in payload["data"]["steps"]}
    assert by_name["pmd_asset"]["status"] == "skipped" and by_name["pmd_asset"]["flag"] is None
    assert by_name["ichor_assets"]["status"] == "skipped" and by_name["ichor_assets"]["flag"] is None
    assert by_name["ichor_toolchain"]["flag"] == FLAGS["ichor_toolchain"]
    code, out = _run(capsys, ["--root", str(tmp_path / "root")])
    assert out.count("FLAG  ") == 1 and FLAGS["pmd_asset"] not in out


def test_a_lab_problem_in_a_step_renders_its_fix_with_the_real_root(world: FakeWorld, tmp_path: Path, capsys) -> None:
    from traceback_lab.problems import LabProblem

    def refuse(context: SetupContext) -> str:
        raise LabProblem("TBX-LAB-004", cause="no usable reference", fix="register one under <ROOT>/references")

    setup_command.CATALOGUE["mmi"] = StepSpec(LOCAL, run=refuse)
    root = tmp_path / "root"
    code, out = _run(capsys, ["--root", str(root), "--only", "mmi"])
    assert code == 8
    assert "STEP  mmi  failed  TBX-LAB-004: no usable reference" in out
    assert f"  | fix: register one under {root}/references" in out
    code, payload = _json(capsys, ["--root", str(root), "--only", "mmi"])
    assert payload["data"]["steps"][0]["stderr_tail"] == ["fix: register one under <ROOT>/references"]


def test_only_refuses_unknown_step_names(tmp_path: Path, capsys) -> None:
    code = signed_cli.main(["lab", "setup", "--root", str(tmp_path), "--only", "bogus,mmi"])
    captured = capsys.readouterr()
    assert code == 2 and "unknown: bogus" in captured.err and "signed_*" in captured.err
    code = signed_cli.main(["lab", "setup", "--root", str(tmp_path), "--only", "", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["status"] == "usage"
    assert step_names("signed_reference,mmi") == ["signed_reference", "mmi"]
    assert not (tmp_path / "lab").exists()


def test_json_shape(world: FakeWorld, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    code, payload = _json(capsys, ["--root", str(root)])
    assert code == 8 and payload["status"] == "partial_failure" and payload["command"] == "lab setup"
    data = payload["data"]
    assert set(data) == JSON_DATA_KEYS
    assert data["root"] == str(root) and data["yes"] is False and data["only"] is None and data["interactive"] is False
    assert all(set(step) == JSON_STEP_KEYS for step in data["steps"])
    assert [(s["name"], s["status"]) for s in data["steps"]] == [
        ("mmi", "ok"), ("ichor_toolchain", "refused"), ("ichor_assets", "skipped"), ("staging", "ok"),
    ]
    assert data["counts"] == {"ok": 2, "skipped": 1, "needs_input": 0, "refused": 1, "failed": 0}
    assert data["doctor_before"]["setup_steps"] == [world.fixes[name] for name in world.order]
    assert data["doctor_after"]["setup_steps"] == [world.fixes["ichor_toolchain"], world.fixes["ichor_assets"]]
    assert data["doctor_after"]["status"] == "ok" and data["doctor_after"]["summary"].startswith("Lab ready")
    assert next(s for s in data["steps"] if s["name"] == "ichor_toolchain")["flag"] == FLAGS["ichor_toolchain"]
    assert str(tmp_path) not in json.dumps({k: v for k, v in data.items() if k != "root"})
    assert capsys.readouterr().out == ""  # --json: exactly one stdout line, consumed above


# -- real local steps ---------------------------------------------------------------------
def test_staging_step_removes_orphans_under_the_real_doctor(lab_world: dict[str, Path], capsys) -> None:
    root = lab_world["root"]
    store = LabStore(root)
    store.ensure()
    orphan = store.inputs_dir / ".staging-abcdef012345"
    orphan.mkdir(parents=True)
    (orphan / "part").write_bytes(b"x" * 10)
    assert any(c["name"] == "staging" for c in lab_checks(store) if c["status"] == "warn")
    code, out = _run(capsys, ["--root", str(root), "--only", "staging"])
    assert code == 0, out
    assert f"RUN   traceback lab gc --staging-only --apply --root {root}" in out
    assert _steps(out) == [("staging", "ok")] and "1 orphaned staging directory removed" in out
    assert not orphan.exists()
    assert not any(c["name"] == "staging" for c in lab_checks(store))


def test_mmi_step_builds_the_index_locally_without_a_prompt(lab_world: dict[str, Path], capsys) -> None:
    root = lab_world["root"]
    store = LabStore(root)
    reference = resolve_reference(root, None)
    assert not mmi_path(store, reference).exists()
    code, out = _run(capsys, ["--root", str(root), "--only", "mmi"])
    assert code == 0, out
    assert "[y/N]" not in out and "FLAG" not in out
    assert f"RUN   minimap2 -x map-ont -d {root}/lab/refs/{reference.sha256}.mmi <FASTA of reference synth>" in out
    assert _steps(out) == [("mmi", "ok")] and "minimap2 index built for reference synth" in out
    assert mmi_path(store, reference).is_file()
    assert next(c for c in lab_checks(store) if c["name"] == "mmi")["status"] == "pass"
    code, out = _run(capsys, ["--root", str(root), "--only", "mmi"])
    assert code == 0 and _steps(out) == [("mmi", "skipped")]


def test_a_step_runs_only_when_doctor_asks_for_the_catalogue_command(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(setup_command, "_interactive", lambda: False)
    checks = [
        {"name": "ichor_toolchain", "status": "warn", "detail": "x", "fix": "brew install r", "blocks_example": False},
        {"name": "staging", "status": "warn", "detail": "x", "fix": "rm -rf something", "blocks_example": False},
        {"name": "mmi", "status": "warn", "detail": "x", "fix": "traceback lab run <chunk dir> --root <ROOT> builds it once (or pass --mmi PATH)", "blocks_example": False},
    ]
    monkeypatch.setattr(setup_command, "lab_checks", lambda store: checks)
    code, payload = _json(capsys, ["--root", str(tmp_path / "root")])
    by_name = {step["name"]: step for step in payload["data"]["steps"]}
    assert by_name["ichor_toolchain"]["status"] == "skipped" and by_name["ichor_toolchain"]["kind"] == "manual"
    assert by_name["ichor_toolchain"]["reason"] == "manual; run it yourself: brew install r"
    assert by_name["staging"]["status"] == "skipped" and by_name["staging"]["reason"].endswith("rm -rf something")
    assert by_name["mmi"]["kind"] == "local" and by_name["mmi"]["status"] == "failed"  # ran: no reference under this ROOT
    assert code == 8


def test_setup_doctor_summary_matches_standalone_doctor(lab_world: dict[str, Path], capsys) -> None:
    root = lab_world["root"]
    code, payload = _json(capsys, ["--root", str(root), "--only", "staging"])
    assert code == 0
    signed_cli.main(["lab", "doctor", "--root", str(root), "--json"])
    doctor = json.loads(capsys.readouterr().out)
    assert payload["data"]["doctor_after"]["summary"] == doctor["summary"]
    assert payload["data"]["doctor_after"]["setup_steps"] == doctor["data"]["setup_steps"]
    assert payload["data"]["doctor_after"]["status"] == doctor["status"]
    assert "active" not in payload["data"]["doctor_after"]["warnings"]
    code, out = _run(capsys, ["--root", str(root), "--only", "staging"])
    assert f"DOCTOR  {doctor['summary']}" in out


def test_run_signed_kills_a_stalled_child(tmp_path: Path, monkeypatch) -> None:
    import sys
    import time

    monkeypatch.setattr(setup_command, "_child_argv", lambda argv: [sys.executable, "-c", "import time; time.sleep(60)"])
    context = SetupContext(
        store=LabStore(tmp_path / "root"), scrubber=Scrubber(root=tmp_path / "root"), say=lambda line: None,
        ask=lambda text: None, yes=True, interactive=False, echo_child=False,
    )
    started = time.monotonic()
    with pytest.raises(StepFailed) as failure:
        run_signed(context, ["anything"], timeout_s=0.5)
    assert time.monotonic() - started < 10
    assert failure.value.reason == "timed out after 0.5 s and was killed" and failure.value.exit_code is None


def test_run_signed_kills_a_stalled_grandchild_that_holds_the_pipes(tmp_path: Path, monkeypatch) -> None:
    import sys
    import time

    spawn = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    monkeypatch.setattr(setup_command, "_child_argv", lambda argv: [sys.executable, "-c", spawn])
    context = SetupContext(
        store=LabStore(tmp_path / "root"), scrubber=Scrubber(root=tmp_path / "root"), say=lambda line: None,
        ask=lambda text: None, yes=True, interactive=False, echo_child=False,
    )
    started = time.monotonic()
    with pytest.raises(StepFailed) as failure:
        run_signed(context, ["anything"], timeout_s=1.0)
    assert time.monotonic() - started < 5  # the grandchild was killed too: its pipes closed
    assert failure.value.reason == "timed out after 1 s and was killed"


def test_run_signed_runs_a_child_and_scrubs_its_tail(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    said: list[str] = []
    context = SetupContext(
        store=LabStore(root), scrubber=Scrubber(root=root), say=said.append, ask=said.append,
        yes=True, interactive=False, echo_child=True,
    )
    assert run_signed(context, ["lab", "algorithms", "--root", str(root)]) == "command exited 0"
    echoed = capsys.readouterr().out
    assert "ROOT  <ROOT>" in echoed and str(root) not in echoed  # echoed child lines are scrubbed
    context.echo_child = False
    with pytest.raises(StepFailed) as failure:
        run_signed(context, ["lab", "runs", "no-such-run", "--root", str(root), "--json"])
    assert failure.value.exit_code == 4
    assert failure.value.tail and "TBX-LAB-017" in failure.value.tail[-1]
    assert str(tmp_path) not in "\n".join(failure.value.tail)


def test_signed_children_run_under_the_setup_writer_lock(tmp_path: Path, monkeypatch) -> None:
    """``method-asset register`` takes ROOT/.operator.lock, not ROOT/lab/.lock: it runs under setup's lock."""

    from traceback_runner.cli import _operator_lock

    root = tmp_path / "root"
    store = LabStore(root)
    said: list[str] = []
    context = SetupContext(
        store=store, scrubber=Scrubber(root=root), say=said.append, ask=said.append,
        yes=True, interactive=False, echo_child=False,
    )
    busy = "local action or unexpired worker lease is active"
    argv = ["method-asset", "register", "--from-toolchain", "copy-number", "--root", str(root)]
    with store.writer("setup", new_run_id(), step="ichor_assets"):
        try:
            from traceback_runner.toolchain import resolve_copy_number_toolchain

            resolve_copy_number_toolchain()
            installed = True
        except Exception:  # noqa: BLE001 - any toolchain problem means "not installed here"
            installed = False
        if installed:
            assert setup_command._run_ichor_assets(context) == "ichorCNA method assets registered"
        else:
            with pytest.raises(StepFailed) as failure:
                setup_command._run_ichor_assets(context)
            assert any("TBX-TOOL-002" in line for line in failure.value.tail), failure.value.tail
            assert not any(busy in line for line in failure.value.tail)
        # With no toolchain in HOME the child gets past the Lab lock and fails on the toolchain, not on a lock.
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        with pytest.raises(StepFailed) as failure:
            run_signed(context, argv)
        assert any("TBX-TOOL-002" in line for line in failure.value.tail), failure.value.tail
        assert not any(busy in line for line in failure.value.tail)
    # The same child is refused while the signed operator lock is held: the two locks are distinct.
    with _operator_lock(root), pytest.raises(StepFailed) as failure:
        run_signed(context, argv)
    assert failure.value.exit_code not in (0, None)
    assert any(busy in line for line in failure.value.tail), failure.value.tail
    assert str(root) not in "\n".join(failure.value.tail)
    assert "TBX-TOOL-002" not in "\n".join(failure.value.tail)


# -- guide ---------------------------------------------------------------------------------
def test_guide_documents_setup_and_repeats_every_flag_line() -> None:
    guide = GUIDE.read_text()
    section = guide.index("### Setup in one command")
    assert guide.index("<!-- lab-journey -->") < section < guide.index("## One-time setup for real kits")
    assert "traceback lab setup [--yes] [--only STEP,...] [--json]" in guide
    assert "### TBX-LAB-032" in guide
    for name, flag in FLAGS.items():
        assert flag in guide, name
    # The journey block itself never runs setup (it would download things).
    block = re.search(r"<!-- lab-journey -->\s*```bash\n(.*?)```", guide, re.S)
    assert block and "setup" not in block.group(1)


def test_parser_lists_setup() -> None:
    parser = build_parser()
    args = parser.parse_args(["setup", "--yes", "--only", "mmi,staging", "--root", "r"])
    assert args.lab_command == "setup" and args.yes is True and args.only == ["mmi", "staging"]
    assert parser.parse_args(["setup"]).only is None
