"""The host sleep assertion held by stage-executing commands."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from traceback_runner import awake


def _script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "caffeinate"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def test_holds_an_assertion_tied_to_this_process_and_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = tmp_path / "argv"
    monkeypatch.setattr(
        awake, "CAFFEINATE", _script(tmp_path, f'echo "$@" > "{argv}"\nexec sleep 600\n')
    )
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    with awake.stay_awake() as holder:
        assert holder is not None and holder.poll() is None
        deadline = time.monotonic() + 10
        while not argv.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert argv.read_text().split() == ["-i", "-s", "-w", str(os.getpid())]
    assert holder.poll() is not None


def test_releases_the_assertion_when_the_block_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(awake, "CAFFEINATE", _script(tmp_path, "exec sleep 600\n"))
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    with pytest.raises(RuntimeError):
        with awake.stay_awake() as holder:
            raise RuntimeError("stage failed")
    assert holder is not None and holder.poll() is not None


def test_is_a_no_op_without_the_tool_or_off_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    monkeypatch.setattr(awake, "CAFFEINATE", tmp_path / "absent")
    with awake.stay_awake() as holder:
        assert holder is None
    monkeypatch.setattr(awake.sys, "platform", "linux")
    monkeypatch.setattr(awake, "CAFFEINATE", _script(tmp_path, "exec sleep 600\n"))
    with awake.stay_awake() as holder:
        assert holder is None


def test_a_tool_that_cannot_start_does_not_block_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "caffeinate"
    broken.write_text("not executable")
    broken.chmod(0o644)
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    monkeypatch.setattr(awake, "CAFFEINATE", broken)
    with awake.stay_awake() as holder:
        assert holder is None
