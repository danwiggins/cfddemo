"""Optional ichorCNA toolchain (CN1): locks, install, identity, doctor, TBX-TOOL-002.

Synthetic only.  A fake micromamba lays out a conda prefix from the lock, so the
install path runs end to end without the network.  Tests that need the real
toolchain run only where it is installed.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from evidence_inspector import ichor_adapter
from traceback_runner import toolchain
from traceback_runner.cli import ExitCode, main
from traceback_runner.references import ReferenceProblem
from traceback_runner.toolchain import (
    ICHOR_CHANNELS,
    ICHOR_POST_LINK_LIBRARIES,
    SUPPORTED_PLATFORMS,
    ToolchainLockError,
    check_toolchain,
    compute_identity,
    load_lock,
    parse_lock,
    render_lock,
)

SHA = "a" * 64


def _minimal_lock(platform: str = "osx-arm64", **overrides: str) -> str:
    packages = [
        (f"https://conda.anaconda.org/conda-forge/{platform}/r-base-4.4.3-h0_1.conda", SHA),
        ("https://conda.anaconda.org/bioconda/noarch/r-ichorcna-0.5.1-r44h_1.conda", SHA),
        (
            f"https://conda.anaconda.org/bioconda/{platform}/"
            "bioconductor-hmmcopy-1.48.0-r44h_1.tar.bz2",
            SHA,
        ),
        (f"https://conda.anaconda.org/bioconda/{platform}/hmmcopy-0.1.1-h_12.conda", SHA),
    ]
    text = render_lock(platform, packages, "b" * 64)
    for old, new in overrides.items():
        assert old in text
        text = text.replace(old, new)
    return text


# --------------------------------------------------------------------------
# Lock files


@pytest.mark.parametrize("platform", SUPPORTED_PLATFORMS)
def test_committed_locks_are_pinned_explicit_lists(platform: str) -> None:
    lock = load_lock("ichor", platform)
    text = lock.content.decode("ascii")
    assert "\n@EXPLICIT\n" in text
    urls = [line for line in text.splitlines() if line.startswith("https://")]
    assert len(urls) == len(lock.packages) > 50
    assert all("#sha256:" in line for line in urls)
    assert all(line.startswith(ICHOR_CHANNELS) for line in urls)
    versions = {item.name: item.version for item in lock.packages}
    assert versions["r-base"].startswith("4.4.")
    assert {"r-ichorcna", "bioconductor-hmmcopy", "hmmcopy"} <= set(versions)
    assert lock.driver_sha256 == toolchain.driver_sha256()


def test_the_locked_ichorcna_is_the_adapter_pin() -> None:
    for platform in SUPPORTED_PLATFORMS:
        package = load_lock("ichor", platform).package("r-ichorcna")
        assert package.version == ichor_adapter.ICHOR_VERSION
        assert package.build == ichor_adapter.ICHOR_BIOCONDA_BUILD
    assert ichor_adapter.ICHOR_COMMIT.startswith("e5a6b30c")


def test_minimal_lock_parses() -> None:
    lock = parse_lock(_minimal_lock().encode(), "osx-arm64")
    assert [item.name for item in lock.packages] == [
        "r-base", "r-ichorcna", "bioconductor-hmmcopy", "hmmcopy"
    ]
    assert lock.sha256 == hashlib.sha256(_minimal_lock().encode()).hexdigest()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({f"#sha256:{SHA}\nhttps://conda.anaconda.org/bioconda/noarch": "\nhttps://conda.anaconda.org/bioconda/noarch"}, "sha256"),
        ({"https://conda.anaconda.org/conda-forge/osx-arm64/r-base": "https://mirror.example/conda-forge/osx-arm64/r-base"}, "pinned channel"),
        ({"https://conda.anaconda.org/bioconda/osx-arm64/hmmcopy": "http://conda.anaconda.org/bioconda/osx-arm64/hmmcopy"}, "pinned channel"),
        ({"bioconda/osx-arm64/hmmcopy": "bioconda/linux-64/hmmcopy"}, "pinned channel"),
        ({"@EXPLICIT\n": ""}, "EXPLICIT"),
        ({"# platform: osx-arm64": "# platform: linux-64"}, "platform"),
        ({"r-base-4.4.3": "r-base-4.5.0"}, "4.4"),
        ({"# driver: runIchorCNA.R sha256:" + "b" * 64 + "\n": ""}, "runIchorCNA.R"),
        ({"hmmcopy-0.1.1-h_12.conda": "hmmcopy-0.1.1-h_12.conda?x=1"}, "pinned channel"),
    ],
)
def test_lock_parser_refuses_unpinned_input(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ToolchainLockError, match=message):
        parse_lock(_minimal_lock(**overrides).encode(), "osx-arm64")


def test_lock_parser_refuses_missing_required_package_and_duplicates() -> None:
    text = _minimal_lock()
    without = "\n".join(line for line in text.splitlines() if "/hmmcopy-0.1.1" not in line)
    with pytest.raises(ToolchainLockError, match="hmmcopy"):
        parse_lock(without.encode(), "osx-arm64")
    duplicate = text + text.splitlines()[-1] + "\n"
    with pytest.raises(ToolchainLockError, match="twice"):
        parse_lock(duplicate.encode(), "osx-arm64")


def test_load_lock_refuses_a_driver_that_drifted(monkeypatch) -> None:
    monkeypatch.setattr(toolchain, "driver_sha256", lambda: "c" * 64)
    with pytest.raises(ToolchainLockError, match="regenerate"):
        load_lock("ichor", "osx-arm64")


def test_the_driver_accepts_exactly_the_adapter_flags() -> None:
    driver = toolchain.driver_file().read_text(encoding="utf-8")
    request_flags = {
        item
        for item in ichor_adapter._expected_argv.__code__.co_consts
        if isinstance(item, str) and item.startswith("--")
    }
    # Every flag the adapter can emit is one the driver accepts.
    for flag in request_flags | {"--mapWig", "--normalPanel"}:
        assert f'"{flag}"' in driver, flag
    assert "run_ichorCNA(" in driver and "cores = 1" in driver


# --------------------------------------------------------------------------
# A fake micromamba and a fake install


FAKE_MICROMAMBA = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
prefix = args[args.index("--prefix") + 1]
lock = args[args.index("--file") + 1]
mode_file = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "fake-mode")
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else "ok"
with open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "fake-env.json"), "w") as h:
    json.dump({{"argv": sys.argv, "env": dict(os.environ)}}, h)
if mode == "fail":
    sys.stderr.write("download refused: incorrect SHA256\\n")
    sys.exit(1)
os.makedirs(os.path.join(prefix, "conda-meta"))
os.makedirs(os.path.join(prefix, "bin"))
library = os.path.join(prefix, "lib", "R", "library")
os.makedirs(library)
owned = {{
    "r-base": ["bin/Rscript", "lib/R/bin/Rscript"],
    "hmmcopy": ["bin/readCounter"],
    "r-ichorcna": ["lib/R/library/ichorCNA/DESCRIPTION"],
}}
rscript = "#!/bin/sh\\n" + ("echo TRACEBACK_TOOLCHAIN_OK\\n" if mode != "no-load" else "exit 1\\n")
for line in open(lock):
    line = line.strip()
    if not line.startswith("https://"):
        continue
    url, sha = line.split("#sha256:")
    stem = url.rsplit("/", 1)[1].replace(".tar.bz2", "").replace(".conda", "")
    name, version, build = stem.rsplit("-", 2)
    files = owned.get(name, [])
    for relative in files:
        path = os.path.join(prefix, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as h:
            h.write(rscript if relative.endswith("Rscript") else name + "\\n")
        os.chmod(path, 0o755)
    record = {{
        "name": name, "version": version, "build": build, "url": url, "sha256": sha,
        "files": files,
        "paths_data": {{"paths": [
            {{"_path": f, "path_type": "hardlink", "sha256": "0" * 64, "size_in_bytes": 1,
              "sha256_in_prefix": "1" * 64}} for f in files]}},
    }}
    with open(os.path.join(prefix, "conda-meta", stem + ".json"), "w") as h:
        json.dump(record, h)
if mode != "no-post-link":
    for name in {post_link}:
        os.makedirs(os.path.join(library, name))
        with open(os.path.join(library, name, "DESCRIPTION"), "w") as h:
            h.write(name + "\\n")
"""


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "toolchains"
    monkeypatch.setenv("TRACEBACK_TOOLCHAIN_HOME", str(home))
    monkeypatch.setattr(toolchain, "host_platform", lambda: "osx-arm64")
    return home


@pytest.fixture
def fake_micromamba(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "mm"
    bin_dir.mkdir()
    path = bin_dir / "micromamba"
    path.write_text(
        FAKE_MICROMAMBA.format(python=sys.executable, post_link=list(ICHOR_POST_LINK_LIBRARIES)),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _install(capsys, micromamba: Path, *extra: str) -> tuple[int, dict]:
    code = main(["toolchain", "install", *extra, "--micromamba", str(micromamba), "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_dry_run_installs_nothing_and_states_the_network(cache, fake_micromamba, capsys) -> None:
    code, payload = _install(capsys, fake_micromamba, "ichor")
    assert code == ExitCode.OK
    assert "Dry run" in payload["summary"] and "network" in payload["summary"]
    data = payload["data"]
    assert data["network"].startswith("required")
    assert data["lock_sha256"] == load_lock("ichor", "osx-arm64").sha256
    assert data["install_directory"] == str(cache / data["lock_sha256"])
    assert data["command"].startswith(f"{fake_micromamba} create --yes --no-rc")
    assert data["next"] == "traceback toolchain install ichor --yes"
    assert not cache.exists()
    assert not (fake_micromamba.parent / "fake-env.json").exists()


def test_copy_number_is_an_alias(cache, fake_micromamba, capsys) -> None:
    code, payload = _install(capsys, fake_micromamba, "copy-number")
    assert code == ExitCode.OK and payload["data"]["toolchain"] == "ichor"


def test_install_records_the_environment_identity(cache, fake_micromamba, capsys) -> None:
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.OK, payload
    lock = load_lock("ichor", "osx-arm64")
    directory = cache / lock.sha256
    identity = json.loads((directory / "identity.json").read_text())
    assert identity == payload["data"]["identity"]
    assert identity["lock_sha256"] == lock.sha256
    assert set(identity["tools"]) == {"readcounter", "rscript", "runichorcna_r"}
    assert identity["tools"]["runichorcna_r"]["sha256"] == lock.driver_sha256
    assert identity["tools"]["readcounter"]["relative_path"] == "env/bin/readCounter"
    assert len(identity["conda_meta_paths_data_sha256"]) == 64
    assert identity["post_link_libraries"] == sorted(ICHOR_POST_LINK_LIBRARIES)
    assert check_toolchain("ichor", deep=True).state == "ready"
    # micromamba ran by absolute path with a scrubbed environment and the lock copy.
    ran = json.loads((fake_micromamba.parent / "fake-env.json").read_text())
    assert ran["argv"][0] == str(fake_micromamba)
    assert ran["argv"][-1] == str(directory / lock.file_name)
    assert ran["env"]["MAMBA_ROOT_PREFIX"] == str(cache / ".mamba-root")
    assert not (directory / ".smoke").exists()
    # A second install is a no-op that returns the same identity.
    code, again = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.OK and again["data"]["identity"] == identity


def test_failed_download_leaves_an_incomplete_install_that_is_retried(
    cache, fake_micromamba, capsys
) -> None:
    (fake_micromamba.parent / "fake-mode").write_text("fail")
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.RETRYABLE_FAILURE
    assert payload["data"]["code"] == "TBX-TOOL-003" and payload["data"]["retryable"]
    assert check_toolchain("ichor").state == "missing"
    (fake_micromamba.parent / "fake-mode").write_text("ok")
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.OK, payload
    assert check_toolchain("ichor", deep=True).state == "ready"


@pytest.mark.parametrize("mode", ["no-load", "no-post-link"])
def test_an_environment_that_cannot_load_ichorcna_is_not_installed(
    cache, fake_micromamba, capsys, mode
) -> None:
    (fake_micromamba.parent / "fake-mode").write_text(mode)
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code != ExitCode.OK
    assert payload["data"]["code"] in {"TBX-TOOL-002", "TBX-TOOL-003"}
    assert not (cache / load_lock("ichor", "osx-arm64").sha256 / "identity.json").exists()


def test_missing_micromamba_is_refused(cache, tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(toolchain, "MICROMAMBA_CANDIDATES", ())
    monkeypatch.delenv("TRACEBACK_MICROMAMBA", raising=False)
    code = main(["toolchain", "install", "ichor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK and payload["data"]["micromamba"] == "not found"
    code = main(["toolchain", "install", "ichor", "--yes", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.BLOCKED and payload["data"]["code"] == "TBX-TOOL-003"


def test_relative_micromamba_is_never_searched_on_path(cache, capsys) -> None:
    code = main(["toolchain", "install", "ichor", "--micromamba", "micromamba", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK and payload["data"]["micromamba"] == "not found"


# --------------------------------------------------------------------------
# States, TBX-TOOL-002 and doctor


@pytest.fixture
def installed(cache, fake_micromamba, capsys) -> Path:
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.OK, payload
    return cache / load_lock("ichor", "osx-arm64").sha256


def test_not_set_up_is_optional_in_doctor(cache, tmp_path, capsys) -> None:
    status = check_toolchain("ichor")
    assert status.state == "not_set_up"
    code = main(["doctor", "--root", str(tmp_path / "root"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK
    (check,) = [c for c in payload["data"]["checks"] if c["name"] == "copy_number_toolchain"]
    assert check["status"] == "optional"
    assert check["detail"].endswith(
        "not set up (optional); next: traceback toolchain install ichor"
    )


def test_ready_in_doctor(installed, tmp_path, capsys) -> None:
    for extra in ([], ["--deep"]):
        main(["doctor", "--root", str(tmp_path / "root"), *extra, "--json"])
        payload = json.loads(capsys.readouterr().out)
        (check,) = [c for c in payload["data"]["checks"] if c["name"] == "copy_number_toolchain"]
        assert check["status"] == "pass" and "ready (lock" in check["detail"]


def test_a_changed_tool_is_a_wrong_digest(installed, tmp_path, capsys) -> None:
    (installed / "env" / "bin" / "readCounter").write_text("tampered\n")
    status = check_toolchain("ichor")
    assert status.state == "wrong_digest"
    problem = toolchain.toolchain_problem(status)
    assert problem.code == "TBX-TOOL-002" and problem.cause.startswith("wrong digest")
    code = main(["doctor", "--root", str(tmp_path / "root"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK  # optional: never blocks doctor
    (check,) = [c for c in payload["data"]["checks"] if c["name"] == "copy_number_toolchain"]
    assert check["status"] == "warn" and check["code"] == "TBX-TOOL-002"
    assert "wrong digest" in check["detail"] and "next:" in check["detail"]


def test_a_changed_driver_is_a_wrong_digest(installed) -> None:
    (installed / "scripts" / "runIchorCNA.R").write_text("quit()\n")
    assert check_toolchain("ichor").state == "wrong_digest"


def test_a_deleted_tool_is_missing(installed) -> None:
    (installed / "env" / "bin" / "readCounter").unlink()
    status = check_toolchain("ichor")
    assert status.state == "missing"
    problem = toolchain.toolchain_problem(status)
    assert problem.code == "TBX-TOOL-002" and problem.cause.startswith("missing")
    assert "toolchain install ichor --yes" in problem.fix


def test_conda_meta_drift_is_caught_only_by_deep(installed) -> None:
    meta = next((installed / "env" / "conda-meta").glob("r-base-*.json"))
    record = json.loads(meta.read_text())
    record["paths_data"]["paths"][0]["sha256"] = "f" * 64
    meta.write_text(json.dumps(record))
    assert check_toolchain("ichor").state == "ready"
    assert check_toolchain("ichor", deep=True).state == "wrong_digest"


def test_a_post_link_library_change_is_caught_by_deep(installed) -> None:
    library = installed / "env" / "lib" / "R" / "library" / ICHOR_POST_LINK_LIBRARIES[0]
    (library / "DESCRIPTION").write_text("other\n")
    assert check_toolchain("ichor", deep=True).state == "wrong_digest"


def test_an_unlocked_package_is_a_wrong_digest(installed) -> None:
    meta = installed / "env" / "conda-meta" / "extra-1.0-0.json"
    meta.write_text(json.dumps({"name": "extra", "files": [], "paths_data": {"paths": []}}))
    assert check_toolchain("ichor", deep=True).state == "wrong_digest"


def test_an_incomplete_install_is_missing(installed) -> None:
    (installed / "identity.json").unlink()
    assert check_toolchain("ichor").state == "missing"


def test_identity_naming_another_lock_is_a_wrong_digest(installed) -> None:
    identity = json.loads((installed / "identity.json").read_text())
    identity["lock_sha256"] = "0" * 64
    (installed / "identity.json").write_text(json.dumps(identity))
    assert check_toolchain("ichor").state == "wrong_digest"


def test_wrong_digest_install_is_refused_not_overwritten(installed, fake_micromamba, capsys) -> None:
    (installed / "env" / "bin" / "readCounter").write_text("tampered\n")
    code, payload = _install(capsys, fake_micromamba, "ichor", "--yes")
    assert code == ExitCode.BLOCKED and payload["data"]["code"] == "TBX-TOOL-002"
    assert (installed / "env" / "bin" / "readCounter").read_text() == "tampered\n"


def test_require_toolchain_raises_tool_002(cache) -> None:
    with pytest.raises(ReferenceProblem) as raised:
        toolchain.require_toolchain("ichor", retryable=True)
    assert raised.value.code == "TBX-TOOL-002"
    assert raised.value.retryable and raised.value.exit_code == 6


def test_unsupported_platform_is_optional(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(toolchain, "host_platform", lambda: None)
    line = toolchain.doctor_line()
    assert line["status"] == "optional" and "not available on this platform" in line["detail"]


def test_identity_excludes_prefix_dependent_fields(installed) -> None:
    lock = load_lock("ichor", "osx-arm64")
    before = compute_identity(lock, installed)
    meta = next((installed / "env" / "conda-meta").glob("hmmcopy-*.json"))
    record = json.loads(meta.read_text())
    record["paths_data"]["paths"][0]["sha256_in_prefix"] = "9" * 64
    meta.write_text(json.dumps(record))
    assert compute_identity(lock, installed) == before


def test_toolchain_home_must_be_absolute(monkeypatch) -> None:
    monkeypatch.setenv("TRACEBACK_TOOLCHAIN_HOME", "relative/dir")
    with pytest.raises(ValueError):
        toolchain.toolchain_home()
    monkeypatch.delenv("TRACEBACK_TOOLCHAIN_HOME")
    assert toolchain.toolchain_home() == Path.home() / ".cache" / "traceback" / "toolchains"


# --------------------------------------------------------------------------
# The PoN format fix


def test_panel_of_normals_is_an_rds_granges_file() -> None:
    field = ichor_adapter.PanelOfNormalsBinding.model_fields["native_format"]
    assert field.default == "rds_granges"
    source = Path(ichor_adapter.__file__).read_text(encoding="utf-8")
    assert '"/assets/pon.rds"' in source
    assert "rdata_granges" not in source


# --------------------------------------------------------------------------
# Real toolchain, only where installed (not in CI unless the toolchain is).


def _real_status():
    try:
        status = check_toolchain("ichor")
    except Exception:
        return None
    return status if status.ready else None


REAL = _real_status()


@pytest.mark.skipif(REAL is None, reason="the copy-number toolchain is not installed here")
def test_real_toolchain_matches_its_identity_and_lock() -> None:
    status = check_toolchain("ichor", deep=True)
    assert status.state == "ready", status.detail
    identity = status.identity
    assert identity is not None
    lock = load_lock("ichor", toolchain.host_platform() or "")
    assert identity["packages"]["r-ichorcna"] == (
        f"{ichor_adapter.ICHOR_VERSION}-{ichor_adapter.ICHOR_BIOCONDA_BUILD}"
    )
    assert identity["lock_sha256"] == lock.sha256
