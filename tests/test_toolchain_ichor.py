"""The optional ichorCNA (copy-number) toolchain (CN1) on the CO1 toolchain module.

Synthetic only: a stand-in micromamba lays out a conda prefix from the committed
lock, so install, receipt, doctor and TBX-TOOL-002 run end to end without the
network.  Tests that need the real toolchain run only where it is installed.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from evidence_inspector import ichor_adapter
from traceback_runner import cli, toolchain
from traceback_runner.toolchain import (
    ICHOR_DRIVER_RELPATH,
    ICHOR_PINS,
    ICHOR_POST_LINK_LIBRARIES,
    RECEIPT_NAME,
    TOOL_MISSING,
    TOOL_WRONG,
    IchorPin,
    ToolchainProblem,
    parse_explicit_lock,
    pin_for,
    resolve_ichor,
)

# Stands in for micromamba.  It reads its spec from fake-spec.json next to
# itself (install gives micromamba an environment built from nothing, so an
# environment variable would not reach it) and lays out a prefix: one
# conda-meta record per locked package, readCounter and Rscript recorded with
# the pinned in-package digests, and the post-link R data packages.
_FAKE_MICROMAMBA = r'''
import hashlib, json, os, sys
here = os.path.dirname(os.path.abspath(sys.argv[0]))
spec = json.load(open(os.path.join(here, "fake-spec.json")))
with open(os.path.join(here, "fake-ran.json"), "w") as handle:
    json.dump({"argv": sys.argv, "env": dict(os.environ)}, handle)
args = sys.argv[1:]
assert args[:3] == ["create", "--yes", "--no-rc"], args
prefix = args[args.index("--prefix") + 1]
lock = args[args.index("--file") + 1]
if spec["mode"] == "fail":
    sys.stderr.write("tarball has incorrect SHA256\n")
    sys.exit(1)
smoke = "echo TRACEBACK_TOOLCHAIN_OK\n" if spec["mode"] != "no-load" else "exit 1\n"
contents = {
    "bin/Rscript": "#!/bin/sh\n" + smoke,
    "bin/readCounter": "#!/bin/sh\necho fixedStep\n",
}
for line in open(lock):
    line = line.strip()
    if not line.startswith("https://"):
        continue
    url, sha = line.split("#sha256:")
    stem = url.rsplit("/", 1)[1].replace(".tar.bz2", "").replace(".conda", "")
    name, version, build = stem.rsplit("-", 2)
    files = list(spec["owned"].get(url, [])) or [f"lib/R/library/{name}/DESCRIPTION"]
    paths = []
    for relative in files:
        path = os.path.join(prefix, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(contents.get(relative, name + "\n"))
        os.chmod(path, 0o755)
        installed = hashlib.sha256(open(path, "rb").read()).hexdigest()
        package = spec["package_binary"].get(relative, installed)
        entry = {"_path": relative, "path_type": "hardlink", "sha256": package,
                 "size_in_bytes": 1}
        if package != installed:
            entry["sha256_in_prefix"] = installed
        paths.append(entry)
    record = {"name": name, "version": version, "build": build, "url": url,
              "sha256": sha, "files": files, "paths_data": {"paths": paths}}
    os.makedirs(os.path.join(prefix, "conda-meta"), exist_ok=True)
    with open(os.path.join(prefix, "conda-meta", stem + ".json"), "w") as handle:
        json.dump(record, handle)
if spec["mode"] != "no-post-link":
    for name in spec["post_link"]:
        os.makedirs(os.path.join(prefix, "lib", "R", "library", name), exist_ok=True)
        with open(os.path.join(prefix, "lib", "R", "library", name, "DESCRIPTION"), "w") as h:
            h.write(name + "\n")
'''

PLATFORM = "osx-arm64"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("traceback_runner.toolchain.current_platform", lambda: PLATFORM)
    bin_dir = tmp_path / "mm"
    bin_dir.mkdir()
    micromamba = bin_dir / "micromamba"
    micromamba.write_text("#!" + sys.executable + "\n" + _FAKE_MICROMAMBA, encoding="utf-8")
    micromamba.chmod(micromamba.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("MAMBA_EXE", str(micromamba))
    pin = ICHOR_PINS[PLATFORM]
    _mode(micromamba, "ok", pin)
    return {"home": home, "micromamba": micromamba, "cache": home / ".cache/traceback/toolchains"}


def _mode(micromamba: Path, mode: str, pin: IchorPin | None = None) -> None:
    pin = pin or ICHOR_PINS[PLATFORM]
    spec = {
        "mode": mode,
        "owned": {
            pin.readcounter.package_url: [pin.readcounter.binary_relpath],
            pin.rscript.package_url: [pin.rscript.binary_relpath, "lib/R/bin/Rscript"],
        },
        "package_binary": {
            pin.readcounter.binary_relpath: pin.readcounter.package_binary_sha256,
            pin.rscript.binary_relpath: pin.rscript.package_binary_sha256,
        },
        "post_link": list(ICHOR_POST_LINK_LIBRARIES),
    }
    (micromamba.parent / "fake-spec.json").write_text(json.dumps(spec), encoding="utf-8")


def _run(capsys, *args: str) -> tuple[int, dict]:
    code = cli.main(["toolchain", "install", *args, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _prefix(env: dict[str, Path]) -> Path:
    return env["cache"] / ICHOR_PINS[PLATFORM].lock_sha256


def _doctor(capsys, tmp_path: Path, *extra: str) -> dict:
    code = cli.main(["doctor", "--root", str(tmp_path / "root"), *extra, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload  # an optional toolchain never blocks doctor
    (check,) = [c for c in payload["data"]["checks"] if c["name"] == "copy_number_toolchain"]
    return check


# --------------------------------------------------------------------------
# Locks, pins and the driver


@pytest.mark.parametrize("platform_name", sorted(ICHOR_PINS))
def test_committed_locks_match_their_pins(platform_name: str) -> None:
    pin = ICHOR_PINS[platform_name]
    data = pin.lock_path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == pin.lock_sha256
    packages = toolchain._read_ichor_lock(pin)
    assert len(packages) > 50
    assert all(line.startswith(toolchain.ICHOR_CHANNELS) for line in packages)
    assert all("#sha256:" in line for line in packages)
    names = {line.rsplit("/", 1)[1].rsplit("-", 2)[0]: line for line in packages}
    assert {"r-base", "r-ichorcna", "bioconductor-hmmcopy", "hmmcopy"} <= set(names)
    assert "/r-base-4.4." in names["r-base"]
    assert pin.lock_line in packages
    assert hashlib.sha256(pin.driver_path.read_bytes()).hexdigest() == pin.driver_sha256


def test_the_locked_ichorcna_is_the_adapter_pin() -> None:
    for pin in ICHOR_PINS.values():
        assert pin.version == ichor_adapter.ICHOR_VERSION
        assert f"r-ichorcna-{ichor_adapter.ICHOR_VERSION}-{ichor_adapter.ICHOR_BIOCONDA_BUILD}." in (
            pin.lock_line
        )
    assert ichor_adapter.ICHOR_COMMIT == "e5a6b30c061efc20d12c241eefb2dc7ed911cb6c"


def test_render_round_trips_through_the_lock_parser() -> None:
    text = toolchain.render_ichor_lock(
        "linux-64",
        [("https://conda.anaconda.org/bioconda/noarch/x-1.0-0.conda", "a" * 64)],
        "b" * 64,
    )
    assert parse_explicit_lock(text, platform_name="linux-64") == (
        "https://conda.anaconda.org/bioconda/noarch/x-1.0-0.conda#sha256:" + "a" * 64,
    )


def test_an_edited_lock_is_refused(tmp_path: Path) -> None:
    pin = ICHOR_PINS[PLATFORM]
    edited = tmp_path / pin.lock_name
    edited.write_bytes(pin.lock_path.read_bytes() + b"\n")
    (tmp_path / toolchain.ICHOR_DRIVER_NAME).write_bytes(pin.driver_path.read_bytes())
    with pytest.raises(ToolchainProblem) as raised:
        toolchain._read_ichor_lock(replace(pin, lock_directory=tmp_path))
    assert (raised.value.code, raised.value.reason) == ("TBX-TOOL-002", TOOL_WRONG)


def test_a_drifted_driver_is_refused(tmp_path: Path) -> None:
    pin = ICHOR_PINS[PLATFORM]
    (tmp_path / pin.lock_name).write_bytes(pin.lock_path.read_bytes())
    (tmp_path / toolchain.ICHOR_DRIVER_NAME).write_text("quit()\n")
    with pytest.raises(ToolchainProblem, match="does not match its lock"):
        toolchain._read_ichor_lock(replace(pin, lock_directory=tmp_path))


def test_the_driver_takes_the_adapter_flags_and_refuses_ignored_ones() -> None:
    driver = ICHOR_PINS[PLATFORM].driver_path.read_text(encoding="utf-8")
    flags = {
        item
        for item in ichor_adapter._expected_argv.__code__.co_consts
        if isinstance(item, str) and item.startswith("--")
    }
    for flag in flags | {"--mapWig", "--normalPanel"}:
        assert f'"{flag}"' in driver, flag
    assert "run_ichorCNA(" in driver and "cores = 1" in driver
    # ichorCNA 0.5.1 ignores lambda; an explicit value must stop the run.
    assert '--lambda must be NULL' in driver and "lambda =" not in driver
    # The adapter's ".15g" numbers can carry an exponent sign.
    assert "[-+0-9.e,]" in driver


# --------------------------------------------------------------------------
# Install


def test_dry_run_changes_nothing_and_states_the_network(env, capsys) -> None:
    code, payload = _run(capsys, "ichor")
    assert code == 0
    data = payload["data"]
    assert data["dry_run"] is True and data["needs_network"] is True
    assert data["tool"] == "ichor" and data["lock_sha256"] == ICHOR_PINS[PLATFORM].lock_sha256
    assert "network" in payload["summary"] and "--yes" in payload["summary"]
    assert str(env["home"]) not in json.dumps(payload)  # JSON carries no host path
    assert not env["cache"].exists()
    assert not (env["micromamba"].parent / "fake-ran.json").exists()
    assert cli.main(["toolchain", "install", "copy-number"]) == 0
    human = capsys.readouterr().out
    assert str(_prefix(env)) in human and "--no-rc" in human


def test_install_records_identity_and_provenance(env, capsys) -> None:
    code, payload = _run(capsys, "copy-number", "--yes")
    assert code == 0, payload
    assert payload["summary"] == "ichorCNA 0.5.1 toolchain installed and verified"
    pin = ICHOR_PINS[PLATFORM]
    receipt = json.loads((_prefix(env) / RECEIPT_NAME).read_text())
    identity = receipt["identity"]
    assert identity == payload["data"]["identity"]
    # Package-level only: these are what a method definition records.
    assert identity["lock_sha256"] == pin.lock_sha256
    assert identity["driver_sha256"] == pin.driver_sha256
    assert identity["readcounter_package_binary_sha256"] == pin.readcounter.package_binary_sha256
    assert identity["rscript_package_binary_sha256"] == pin.rscript.package_binary_sha256
    installed = receipt["installed"]
    assert installed["readcounter_sha256"] == hashlib.sha256(
        (_prefix(env) / "bin/readCounter").read_bytes()
    ).hexdigest()
    assert installed["post_link_libraries"] == sorted(ICHOR_POST_LINK_LIBRARIES)
    assert len(installed["conda_meta_paths_data_sha256"]) == 64
    assert (_prefix(env) / ICHOR_DRIVER_RELPATH).read_bytes() == pin.driver_path.read_bytes()
    assert not (_prefix(env) / ".traceback-smoke").exists()
    resolve_ichor(pin, cache_root=env["cache"], check="deep")
    # micromamba ran by absolute path, with an environment built from nothing.
    ran = json.loads((env["micromamba"].parent / "fake-ran.json").read_text())
    assert ran["argv"][0] == str(env["micromamba"])
    assert ran["env"]["MAMBA_ROOT_PREFIX"] == str(env["cache"] / ".mamba-root")
    assert ran["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert "MAMBA_EXE" not in ran["env"]
    code, again = _run(capsys, "ichor", "--yes")
    assert code == 0 and again["summary"].endswith("already installed and verified")


def test_failed_download_is_missing_and_retryable_then_recovers(env, capsys) -> None:
    _mode(env["micromamba"], "fail")
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 3
    data = payload["data"]
    assert (data["code"], data["reason"], data["retryable"]) == ("TBX-TOOL-002", TOOL_MISSING, True)
    assert data["docs"].endswith("#tbx-tool-002")
    assert not (_prefix(env) / RECEIPT_NAME).exists()
    assert (env["cache"] / f"{ICHOR_PINS[PLATFORM].lock_sha256}.install.log").is_file()
    _mode(env["micromamba"], "ok")
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 0, payload


@pytest.mark.parametrize("mode", ["no-load", "no-post-link"])
def test_an_environment_that_cannot_run_ichorcna_gets_no_receipt(env, capsys, mode) -> None:
    _mode(env["micromamba"], mode)
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 3 and payload["data"]["code"] == "TBX-TOOL-002"
    assert payload["data"]["reason"] == TOOL_MISSING
    assert not (_prefix(env) / RECEIPT_NAME).exists()


def test_missing_micromamba_is_tool_002_for_ichor(env, capsys, monkeypatch) -> None:
    monkeypatch.delenv("MAMBA_EXE")
    monkeypatch.setattr("traceback_runner.toolchain._MICROMAMBA_CANDIDATES", ())
    code, dry = _run(capsys, "ichor")
    assert code == 0 and dry["data"]["micromamba_found"] is False
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 3
    assert (payload["data"]["code"], payload["data"]["reason"]) == ("TBX-TOOL-002", TOOL_MISSING)


def test_unsupported_platform_is_tool_002(env, capsys, monkeypatch) -> None:
    monkeypatch.setattr("traceback_runner.toolchain.current_platform", lambda: None)
    code, payload = _run(capsys, "ichor")
    assert code == 3 and payload["data"]["code"] == "TBX-TOOL-002"
    with pytest.raises(ToolchainProblem):
        toolchain.resolve_copy_number_toolchain()


# --------------------------------------------------------------------------
# Verification states, doctor


@pytest.fixture
def installed(env, capsys) -> Path:
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 0, payload
    return _prefix(env)


def _state(env, check: str = "shallow") -> str:
    try:
        resolve_ichor(ICHOR_PINS[PLATFORM], cache_root=env["cache"], check=check)  # type: ignore[arg-type]
    except ToolchainProblem as problem:
        assert problem.code == "TBX-TOOL-002"
        return problem.reason
    return "ready"


def test_doctor_not_set_up_is_optional(env, capsys, tmp_path) -> None:
    check = _doctor(capsys, tmp_path)
    assert check["status"] == "optional"
    assert check["detail"] == (
        "copy number (ichorCNA toolchain): not set up (optional); "
        "next: traceback toolchain install ichor"
    )


def test_doctor_ready_shallow_and_deep(installed, capsys, tmp_path) -> None:
    for extra in ((), ("--deep",)):
        check = _doctor(capsys, tmp_path, *extra)
        assert check["status"] == "pass" and "ready (lock " in check["detail"]


def test_a_changed_readcounter_is_a_wrong_digest(installed, env, capsys, tmp_path) -> None:
    (installed / "bin/readCounter").write_text("#!/bin/sh\necho other\n")
    assert _state(env) == TOOL_WRONG
    check = _doctor(capsys, tmp_path)
    assert check["status"] == "warn" and check["code"] == "TBX-TOOL-002"
    assert check["reason"] == TOOL_WRONG and "next:" in check["detail"]


def test_a_deleted_rscript_is_missing(installed, env) -> None:
    (installed / "bin/Rscript").unlink()
    assert _state(env) == TOOL_MISSING


def test_a_changed_driver_is_a_wrong_digest(installed, env) -> None:
    (installed / ICHOR_DRIVER_RELPATH).write_text("quit()\n")
    assert _state(env) == TOOL_WRONG


def test_a_receipt_for_another_lock_is_a_wrong_digest(installed, env) -> None:
    receipt = json.loads((installed / RECEIPT_NAME).read_text())
    receipt["identity"]["lock_sha256"] = "0" * 64
    (installed / RECEIPT_NAME).write_text(json.dumps(receipt))
    assert _state(env) == TOOL_WRONG


def test_an_incomplete_install_is_missing(installed, env, capsys, tmp_path) -> None:
    (installed / RECEIPT_NAME).unlink()
    assert _state(env) == TOOL_MISSING
    assert _doctor(capsys, tmp_path)["reason"] == TOOL_MISSING


def test_a_package_record_for_another_package_is_a_wrong_digest(installed, env) -> None:
    meta = next((installed / "conda-meta").glob("hmmcopy-*.json"))
    record = json.loads(meta.read_text())
    record["sha256"] = "f" * 64
    meta.write_text(json.dumps(record))
    assert _state(env) == TOOL_WRONG


def test_an_installed_library_file_change_is_caught_only_by_deep(installed, env) -> None:
    target = installed / "lib/R/library/r-ichorcna/DESCRIPTION"
    target.write_text("tampered\n")
    assert _state(env) == "ready"
    assert _state(env, "deep") == TOOL_WRONG


def test_conda_meta_drift_is_caught_only_by_deep(installed, env) -> None:
    meta = next((installed / "conda-meta").glob("r-ichorcna-*.json"))
    record = json.loads(meta.read_text())
    record["version"] = "9.9.9"
    meta.write_text(json.dumps(record))
    assert _state(env) == "ready"
    assert _state(env, "deep") == TOOL_WRONG


def test_a_post_link_library_change_is_caught_by_deep(installed, env) -> None:
    library = installed / "lib/R/library" / ICHOR_POST_LINK_LIBRARIES[0]
    (library / "DESCRIPTION").write_text("other\n")
    assert _state(env, "deep") == TOOL_WRONG


def test_an_unlocked_package_is_caught_by_deep(installed, env) -> None:
    (installed / "conda-meta" / "extra-1.0-0.json").write_text(
        json.dumps({"name": "extra", "url": "https://example.invalid/x", "files": []})
    )
    assert _state(env, "deep") == TOOL_WRONG


def test_install_replaces_a_damaged_install(installed, env, capsys) -> None:
    (installed / "bin/readCounter").write_text("#!/bin/sh\necho other\n")
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 0 and payload["summary"].endswith("installed and verified")
    assert _state(env, "deep") == "ready"


def test_resolve_for_cn3_returns_absolute_tool_paths(installed, env) -> None:
    resolved = toolchain.resolve_copy_number_toolchain(cache_root=env["cache"])
    for path in (resolved.readcounter, resolved.rscript, resolved.driver, resolved.r_library):
        assert path.is_absolute() and path.exists()
    assert resolved.identity.tool_id == "ichor"


def test_toolchain_help_lists_ichor_and_its_alias(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["toolchain", "install", "--help"])
    out = capsys.readouterr().out
    assert "ichor" in out and "copy-number" in out and "modkit" in out


# --------------------------------------------------------------------------
# The PoN format fix


def test_panel_of_normals_is_an_rds_granges_file() -> None:
    field = ichor_adapter.PanelOfNormalsBinding.model_fields["native_format"]
    assert field.default == "rds_granges"
    source = Path(ichor_adapter.__file__).read_text(encoding="utf-8")
    assert '"/assets/pon.rds"' in source
    assert "rdata_granges" not in source


# --------------------------------------------------------------------------
# The real toolchain, only where it is installed


def _real():
    try:
        return toolchain.resolve_copy_number_toolchain()
    except Exception:
        return None


@pytest.mark.skipif(_real() is None, reason="the copy-number toolchain is not installed here")
def test_real_toolchain_verifies_deep_and_matches_the_adapter() -> None:
    pin = pin_for("ichor")
    assert isinstance(pin, IchorPin)
    resolved = resolve_ichor(pin, cache_root=toolchain.toolchain_cache_root(), check="deep")
    assert resolved.identity.version == ichor_adapter.ICHOR_VERSION
    assert resolved.installed.post_link_libraries == tuple(sorted(ICHOR_POST_LINK_LIBRARIES))


def test_a_file_replaced_by_a_symlink_is_caught_by_deep(installed, env) -> None:
    target = installed / "lib/R/library/r-ichorcna/DESCRIPTION"
    target.unlink()
    target.symlink_to(installed / "lib/R/library/hmmcopy/DESCRIPTION")
    assert _state(env, "deep") == TOOL_WRONG


def test_a_malformed_package_record_is_a_wrong_digest_not_a_crash(
    installed, env, capsys, tmp_path
) -> None:
    meta = next((installed / "conda-meta").glob("hmmcopy-*.json"))
    record = json.loads(meta.read_text())
    record["paths_data"] = None
    meta.write_text(json.dumps(record))
    assert _state(env) == TOOL_WRONG
    check = _doctor(capsys, tmp_path)
    assert check["status"] == "warn" and check["code"] == "TBX-TOOL-002"


def test_reinstall_repairs_damage_only_deep_sees(installed, env, capsys) -> None:
    (installed / "lib/R/library/r-ichorcna/DESCRIPTION").write_text("tampered\n")
    assert _state(env) == "ready" and _state(env, "deep") == TOOL_WRONG
    code, payload = _run(capsys, "ichor", "--yes")
    assert code == 0 and payload["summary"] == "ichorCNA 0.5.1 toolchain installed and verified"
    assert _state(env, "deep") == "ready"


def test_micromamba_never_sees_the_operators_home_or_r_setup(installed, env) -> None:
    ran = json.loads((env["micromamba"].parent / "fake-ran.json").read_text())["env"]
    assert ran["HOME"] == str(env["cache"] / ".home")
    assert ran["R_ENVIRON_USER"] == "/dev/null" and ran["R_PROFILE_USER"] == "/dev/null"
    assert ran["R_LIBS_USER"] == ""
