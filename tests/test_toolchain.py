"""Pinned toolchains: committed locks, per-user install, resolve, exec (CO1).

Synthetic only: micromamba and modkit are stand-in scripts; nothing reaches
the network and nothing is written outside ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from traceback_runner import cli
from traceback_runner.toolchain import (
    LOCK_DIRECTORY,
    MODKIT_PINS,
    RECEIPT_NAME,
    TOOL_MISSING,
    TOOL_WRONG,
    PinnedTool,
    ToolIdentity,
    ToolPin,
    ToolProblem,
    exec_pinned,
    install,
    parse_explicit_lock,
    plan_install,
    resolve_micromamba,
    resolve_tool,
)

_LOCK_TEXT = (
    "# platform: osx-arm64\n"
    "@EXPLICIT\n"
    "https://conda.anaconda.org/conda-forge/osx-arm64/libzlib-1.0-h0_0.conda"
    "#sha256:" + "1" * 64 + "\n"
    "https://conda.anaconda.org/bioconda/osx-arm64/ont-modkit-0.6.4-h0_0.conda"
    "#sha256:" + "2" * 64 + "\n"
)

# Stands in for micromamba: `create --yes --no-rc --prefix P --file L` lays
# out a prefix with a modkit script and its conda-meta package record, using
# the pin values in $FAKE_MAMBA_SPEC.
_FAKE_MICROMAMBA = r'''
import hashlib, json, os, sys
args = sys.argv[1:]
assert args[:3] == ["create", "--yes", "--no-rc"], args
prefix = args[args.index("--prefix") + 1]
spec = json.load(open(os.environ["FAKE_MAMBA_SPEC"]))
if spec.get("fail"):
    sys.exit(1)
os.makedirs(os.path.join(prefix, "bin"))
os.makedirs(os.path.join(prefix, "conda-meta"))
binary = os.path.join(prefix, "bin", "modkit")
with open(binary, "w") as handle:
    handle.write("#!" + sys.executable + "\n" + spec["modkit_source"])
os.chmod(binary, 0o755)
digest = hashlib.sha256(open(binary, "rb").read()).hexdigest()
record = {
    "url": spec["package_url"],
    "sha256": spec["package_sha256"],
    "paths_data": {"paths": [{
        "_path": "bin/modkit",
        "sha256": spec["package_binary_sha256"],
        "sha256_in_prefix": digest,
    }]},
}
with open(os.path.join(prefix, "conda-meta", spec["record_name"]), "w") as handle:
    json.dump(record, handle)
'''

_FAKE_MODKIT = (
    "import sys\n"
    "if sys.argv[1:] == ['--version']:\n"
    "    print('modkit 0.6.4')\n"
)


def _script(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _test_pin(tmp_path: Path) -> ToolPin:
    locks = tmp_path / "locks"
    locks.mkdir()
    lock = locks / "modkit-osx-arm64.lock"
    lock.write_text(_LOCK_TEXT, encoding="utf-8")
    return ToolPin(
        tool="modkit",
        version="0.6.4",
        platform="osx-arm64",
        lock_name=lock.name,
        lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
        package_url="https://conda.anaconda.org/bioconda/osx-arm64/ont-modkit-0.6.4-h0_0.conda",
        package_sha256="2" * 64,
        binary_relpath="bin/modkit",
        package_binary_sha256="3" * 64,
        lock_directory=locks,
    )


def _fake_micromamba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pin: ToolPin,
    *,
    modkit_source: str = _FAKE_MODKIT,
    fail: bool = False,
) -> Path:
    spec = tmp_path / "fake-mamba-spec.json"
    spec.write_text(
        json.dumps(
            {
                "package_url": pin.package_url,
                "package_sha256": pin.package_sha256,
                "package_binary_sha256": pin.package_binary_sha256,
                "record_name": pin.package_record_name,
                "modkit_source": modkit_source,
                "fail": fail,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_MAMBA_SPEC", str(spec))
    return _script(tmp_path / "mamba-bin" / "micromamba", _FAKE_MICROMAMBA)


def _installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ToolPin, Path, PinnedTool]:
    pin = _test_pin(tmp_path)
    cache = tmp_path / "cache"
    micromamba = _fake_micromamba(tmp_path, monkeypatch, pin)
    tool, fresh = install(pin, cache_root=cache, micromamba=micromamba)
    assert fresh
    return pin, cache, tool


# --- committed locks and pins ------------------------------------------------


@pytest.mark.parametrize("platform_name", sorted(MODKIT_PINS))
def test_committed_lock_matches_its_pin(platform_name: str) -> None:
    pin = MODKIT_PINS[platform_name]
    data = (LOCK_DIRECTORY / pin.lock_name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == pin.lock_sha256
    packages = parse_explicit_lock(data.decode("utf-8"), platform_name=platform_name)
    assert pin.lock_line in packages
    assert pin.version == "0.6.4"
    assert f"/{platform_name}/ont-modkit-0.6.4-" in pin.package_url


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_LOCK_TEXT.replace("@EXPLICIT\n", ""), "no @EXPLICIT"),
        (_LOCK_TEXT.replace("#sha256:" + "1" * 64, ""), "pinned channel URL"),
        (_LOCK_TEXT.replace("#sha256:" + "1" * 64, "#" + "a" * 32), "pinned channel URL"),
        (_LOCK_TEXT.replace("conda.anaconda.org/conda-forge", "example.org/x"), "pinned"),
        (_LOCK_TEXT.replace("conda-forge/osx-arm64", "conda-forge/linux-64"), "platform"),
        ("@EXPLICIT\n", "no packages"),
        ("pkg\n" + _LOCK_TEXT, "before @EXPLICIT"),
    ],
)
def test_lock_parser_refuses_unpinned_lines(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_explicit_lock(text, platform_name="osx-arm64")


def test_an_edited_lock_is_refused_before_use(tmp_path: Path) -> None:
    pin = _test_pin(tmp_path)
    # Still a valid lock that carries the pinned line: only the digest pin catches it.
    edited = _LOCK_TEXT.replace("# platform: osx-arm64", "# platform: osx-arm64 (edited)")
    parse_explicit_lock(edited, platform_name="osx-arm64")
    pin.lock_path.write_text(edited, encoding="utf-8")
    with pytest.raises(ToolProblem) as raised:
        plan_install(pin, cache_root=tmp_path / "cache", micromamba=Path(sys.executable))
    assert (raised.value.code, raised.value.reason) == ("TBX-TOOL-001", TOOL_WRONG)


# --- micromamba by absolute path ---------------------------------------------


def test_micromamba_is_never_searched_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = _script(tmp_path / "path-bin" / "micromamba", "")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.delenv("MAMBA_EXE", raising=False)
    monkeypatch.setattr("traceback_runner.toolchain._MICROMAMBA_CANDIDATES", ())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(ToolProblem) as raised:
        resolve_micromamba()
    assert (raised.value.reason, raised.value.tool) == (TOOL_MISSING, "micromamba")
    monkeypatch.setenv("MAMBA_EXE", str(on_path))
    assert resolve_micromamba() == on_path


def test_micromamba_explicit_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ToolProblem):
        resolve_micromamba(Path("micromamba"))


# --- install -------------------------------------------------------------------


def test_install_writes_receipt_into_the_lock_keyed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin, cache, tool = _installed(tmp_path, monkeypatch)
    prefix = cache / pin.lock_sha256
    assert tool.path == prefix / "bin" / "modkit" and tool.path.is_absolute()
    assert (prefix / RECEIPT_NAME).is_file()
    identity = tool.identity
    assert identity.lock_line == pin.lock_line
    assert identity.package_binary_sha256 == pin.package_binary_sha256
    assert identity.installed_binary_sha256 == hashlib.sha256(
        tool.path.read_bytes()
    ).hexdigest()
    again, fresh = install(pin, cache_root=cache, micromamba=Path(sys.executable))
    assert not fresh and again == tool  # reused; micromamba not run again


def test_install_replaces_a_partial_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pin = _test_pin(tmp_path)
    cache = tmp_path / "cache"
    (cache / pin.lock_sha256 / "conda-meta").mkdir(parents=True)  # no receipt
    micromamba = _fake_micromamba(tmp_path, monkeypatch, pin)
    tool, fresh = install(pin, cache_root=cache, micromamba=micromamba)
    assert fresh and tool.path.is_file()


def test_failed_install_is_missing_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = _test_pin(tmp_path)
    micromamba = _fake_micromamba(tmp_path, monkeypatch, pin, fail=True)
    with pytest.raises(ToolProblem) as raised:
        install(pin, cache_root=tmp_path / "cache", micromamba=micromamba)
    assert raised.value.reason == TOOL_MISSING and raised.value.retryable
    assert not (tmp_path / "cache" / pin.lock_sha256 / RECEIPT_NAME).exists()


def test_install_refuses_a_package_whose_binary_digest_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = _test_pin(tmp_path)
    micromamba = _fake_micromamba(
        tmp_path, monkeypatch, replace(pin, package_binary_sha256="4" * 64)
    )
    with pytest.raises(ToolProblem) as raised:
        install(pin, cache_root=tmp_path / "cache", micromamba=micromamba)
    assert raised.value.reason == TOOL_WRONG
    assert not (tmp_path / "cache" / pin.lock_sha256 / RECEIPT_NAME).exists()


def test_install_refuses_a_binary_of_another_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = _test_pin(tmp_path)
    micromamba = _fake_micromamba(
        tmp_path, monkeypatch, pin, modkit_source="print('modkit 0.6.3')\n"
    )
    with pytest.raises(ToolProblem) as raised:
        install(pin, cache_root=tmp_path / "cache", micromamba=micromamba)
    assert raised.value.reason == TOOL_WRONG


# --- resolve: missing vs wrong version/digest ------------------------------------


def test_resolve_without_install_is_missing(tmp_path: Path) -> None:
    pin = _test_pin(tmp_path)
    with pytest.raises(ToolProblem) as raised:
        resolve_tool(pin, cache_root=tmp_path / "cache")
    problem = raised.value
    assert (problem.code, problem.reason, problem.retryable) == (
        "TBX-TOOL-001",
        TOOL_MISSING,
        True,
    )
    assert "toolchain install modkit" in problem.fix


def test_resolve_refuses_a_changed_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pin, cache, tool = _installed(tmp_path, monkeypatch)
    with tool.path.open("a", encoding="utf-8") as handle:
        handle.write("# changed\n")
    with pytest.raises(ToolProblem) as raised:
        resolve_tool(pin, cache_root=cache)
    assert (raised.value.reason, raised.value.retryable) == (TOOL_WRONG, False)


@pytest.mark.parametrize("field", ["version", "package_sha256", "package_binary_sha256"])
def test_resolve_refuses_a_receipt_for_another_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    pin, cache, _tool = _installed(tmp_path, monkeypatch)
    other = {"version": "0.6.3", "package_sha256": "5" * 64, "package_binary_sha256": "5" * 64}
    with pytest.raises(ToolProblem) as raised:
        resolve_tool(replace(pin, **{field: other[field]}), cache_root=cache)
    assert raised.value.reason == TOOL_WRONG


@pytest.mark.parametrize("field", ["version", "package_binary_sha256", "lock_line"])
def test_resolve_refuses_a_tampered_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Install and package record are intact; only the receipt names another pin.
    pin, cache, tool = _installed(tmp_path, monkeypatch)
    receipt_path = cache / pin.lock_sha256 / RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["identity"][field] = {
        "version": "0.6.3",
        "package_binary_sha256": "5" * 64,
        "lock_line": "https://conda.anaconda.org/bioconda/osx-arm64/other.conda",
    }[field]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ToolProblem) as raised:
        resolve_tool(pin, cache_root=cache)
    assert raised.value.reason == TOOL_WRONG


def test_resolve_refuses_a_package_record_for_another_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin, cache, _tool = _installed(tmp_path, monkeypatch)
    record_path = cache / pin.lock_sha256 / "conda-meta" / pin.package_record_name
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["sha256"] = "6" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ToolProblem) as raised:
        resolve_tool(pin, cache_root=cache)
    assert raised.value.reason == TOOL_WRONG


# --- exec: absolute path, hashed right before exec -------------------------------


def test_exec_runs_by_absolute_path_and_rehashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin, _cache, tool = _installed(tmp_path, monkeypatch)
    process = exec_pinned(tool, ["--version"], stdout=subprocess.PIPE, text=True)
    out, _ = process.communicate(timeout=30)
    assert out.strip() == "modkit 0.6.4"
    # Swapped after resolve: refused before exec.
    tool.path.write_text(f"#!{sys.executable}\nprint('modkit 0.6.4')\n", encoding="utf-8")
    with pytest.raises(ToolProblem) as raised:
        exec_pinned(tool, ["--version"])
    assert raised.value.reason == TOOL_WRONG


def test_exec_refuses_a_relative_path(tmp_path: Path) -> None:
    identity = ToolIdentity(
        tool_id="modkit",
        version="0.6.4",
        platform="osx-arm64",
        lock_sha256="1" * 64,
        lock_line="x",
        package_sha256="1" * 64,
        package_binary_sha256="1" * 64,
        installed_binary_sha256="1" * 64,
    )
    with pytest.raises(ValueError, match="absolute"):
        exec_pinned(PinnedTool(path=Path("modkit"), identity=identity), [])


# --- CLI ---------------------------------------------------------------------


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from traceback_runner.toolchain import pin_for

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    micromamba = _fake_micromamba(tmp_path, monkeypatch, pin_for("modkit"))
    monkeypatch.setenv("MAMBA_EXE", str(micromamba))
    return home, micromamba


@pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"}, reason="modkit is pinned for macOS and Linux"
)
def test_cli_dry_run_changes_nothing_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from traceback_runner.toolchain import current_platform

    if current_platform() is None:
        pytest.skip("no modkit lock for this machine")
    home, _ = _cli_env(tmp_path, monkeypatch)
    assert cli.main(["toolchain", "install", "modkit", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["data"]["dry_run"] is True and payload["data"]["needs_network"] is True
    assert str(tmp_path) not in json.dumps(payload)  # JSON carries no host path
    assert not (home / ".cache").exists()
    assert cli.main(["toolchain", "install", "modkit"]) == 0
    human = capsys.readouterr().out
    assert "needs the network" in human and "--yes" in human
    assert str(home / ".cache" / "traceback" / "toolchains") in human
    assert not (home / ".cache").exists()


@pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"}, reason="modkit is pinned for macOS and Linux"
)
def test_cli_install_with_yes_then_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from traceback_runner.toolchain import current_platform, pin_for

    if current_platform() is None:
        pytest.skip("no modkit lock for this machine")
    home, _ = _cli_env(tmp_path, monkeypatch)
    assert cli.main(["toolchain", "install", "modkit", "--yes", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["summary"] == "modkit 0.6.4 installed and verified"
    pin = pin_for("modkit")
    assert (home / ".cache/traceback/toolchains" / pin.lock_sha256 / RECEIPT_NAME).is_file()
    assert cli.main(["toolchain", "install", "modkit", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"].startswith("modkit 0.6.4 already")


def test_cli_missing_micromamba_is_tool_001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAMBA_EXE", raising=False)
    monkeypatch.setattr("traceback_runner.toolchain._MICROMAMBA_CANDIDATES", ())
    monkeypatch.setattr("traceback_runner.toolchain.current_platform", lambda: "osx-arm64")
    # The dry run still prints the plan and exits 0, naming the prerequisite.
    assert cli.main(["toolchain", "install", "modkit", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["data"]["dry_run"] is True and dry["data"]["micromamba_found"] is False
    assert "micromamba" in dry["data"]["next"]
    code = cli.main(["toolchain", "install", "modkit", "--yes", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["data"]["code"] == "TBX-TOOL-001"
    assert payload["data"]["reason"] == TOOL_MISSING
    assert payload["data"]["docs"].endswith("#tbx-tool-001")


def test_cli_unsupported_platform_is_tool_001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("traceback_runner.toolchain.current_platform", lambda: None)
    assert cli.main(["toolchain", "install", "modkit", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["data"]["code"] == "TBX-TOOL-001"


def test_toolchain_is_listed_in_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "toolchain" in capsys.readouterr().out


def test_kill_process_group_kills_survivors_after_the_leader_exits(tmp_path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    from traceback_runner.toolchain import kill_process_group

    pid_file = tmp_path / "child.pid"
    script = (
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    leader.wait(timeout=30)  # the leader exits; its child keeps the group alive
    child_pid = int(pid_file.read_text())
    assert leader.returncode == 0

    kill_process_group(leader)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGKILL)
        raise AssertionError("a group member survived kill_process_group")
