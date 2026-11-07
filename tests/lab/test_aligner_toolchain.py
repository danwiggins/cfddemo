"""Pinned samtools + minimap2 for the Lab (``traceback toolchain install aligners``).

Every install here uses fake tarballs and digests computed from them: nothing
is downloaded.  The store's "binaries" are shell scripts that report the
pinned version and exec the real PATH tools, so a real ``lab run`` can align
through them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import threading
import urllib.request
from pathlib import Path

import pytest

from traceback_lab import aligner_toolchain as at
from traceback_lab import doctor
from traceback_lab.identity import aligned_input_id
from traceback_lab.ingest import aligned_input_id_for_manifest
from traceback_lab.problems import LabProblem
from traceback_lab.store import LabStore
from traceback_lab.tools import PINNED, UNPINNED, aligner_provenance, pins_of, resolve_aligner_tools
from traceback_runner import cli as signed_cli
from traceback_runner.toolchain import TOOL_WRONG, ToolProblem

REPO = Path(__file__).resolve().parents[2]
SAMTOOLS = shutil.which("samtools")
MINIMAP2 = shutil.which("minimap2")
FAKE_PLATFORM = "linux-64"


def _script(version_line: str, real: str | None) -> bytes:
    tail = f'exec {real} "$@"\n' if real else "exit 1\n"
    return f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n{tail}'.encode()


def _tarball(path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:bz2") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fake_release(tmp_path: Path) -> dict[str, object]:
    """Two prebuilt fake releases (samtools 1.21, minimap2 2.28) and their pins."""

    releases = tmp_path / "releases"
    releases.mkdir()
    sources: dict[str, Path] = {}
    pins = []
    for tool, version, line, real in (
        ("samtools", "1.21", "samtools 1.21", SAMTOOLS),
        ("minimap2", "2.28", "2.28-r1209", MINIMAP2),
    ):
        archive = releases / f"{tool}-{version}.tar.bz2"
        body = _script(line, real)
        digest = _tarball(archive, {f"{tool}-{version}/{tool}": body})
        url = f"https://example.invalid/{tool}-{version}.tar.bz2"
        sources[url] = archive
        # Like the real pins: only minimap2's release binary digest is pinned.
        binary = hashlib.sha256(body).hexdigest() if tool == "minimap2" else None
        pins.append(at.AlignerPin(tool, version, url, digest, "prebuilt", f"{tool}-{version}/{tool}", binary))

    def downloader(url: str, destination: Path) -> None:
        shutil.copyfile(sources[url], destination)

    return {"pins": tuple(pins), "downloader": downloader, "sources": sources}


@pytest.fixture
def pinned_host(monkeypatch, tmp_path: Path, fake_release) -> Path:
    """This host is a pinned platform whose pins are the fake releases; returns the cache root."""

    cache = tmp_path / "toolchains"
    monkeypatch.setattr(at, "current_platform", lambda: FAKE_PLATFORM)
    monkeypatch.setattr(at, "toolchain_cache_root", lambda: cache)
    monkeypatch.setitem(at.ALIGNER_PINS, FAKE_PLATFORM, fake_release["pins"])
    return cache


def _replant(store: at.PinnedAligners, tool: str, body: bytes) -> None:
    """Replace one store binary AND rewrite the receipt to match it (a consistent plant)."""

    binary = store.prefix / "bin" / tool
    binary.write_bytes(body)
    receipt = json.loads((store.prefix / at.RECEIPT_NAME).read_bytes())
    receipt["tools"][tool]["installed_binary_sha256"] = hashlib.sha256(body).hexdigest()
    (store.prefix / at.RECEIPT_NAME).write_text(json.dumps(receipt))


def _install(fake_release, cache: Path) -> at.PinnedAligners:
    store, installed = at.install_aligners(
        fake_release["pins"], cache_root=cache, platform_name=FAKE_PLATFORM,
        downloader=fake_release["downloader"],
    )
    assert installed
    return store


# -- pins ---------------------------------------------------------------------------------
def test_linux_pins_are_the_ci_workflow_urls_and_digests() -> None:
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    for pin in at.ALIGNER_PINS[at.PLATFORM_LINUX]:
        assert pin.url in workflow, pin.tool
        assert re.search(rf"{pin.tarball_sha256}\s+{pin.tool}\.tar\.bz2", workflow), pin.tool
    assert {pin.tool: pin.version for pin in at.ALIGNER_PINS[at.PLATFORM_LINUX]} == {
        "samtools": "1.21", "minimap2": "2.28",
    }
    assert "osx-arm64" not in at.ALIGNER_PINS  # no upstream macOS release artefact
    assert at.MINIMAP2_LINUX.binary_sha256 and at.SAMTOOLS_LINUX.binary_sha256 is None


# -- install ------------------------------------------------------------------------------
def test_install_verifies_digests_and_resolves_pinned(tmp_path, fake_release) -> None:
    cache = tmp_path / "toolchains"
    store = _install(fake_release, cache)
    assert store.prefix == at.store_prefix(fake_release["pins"], cache)
    assert (store.prefix / at.RECEIPT_NAME).is_file()
    assert not list(cache.glob(".aligners-*staging-*"))
    resolved = at.resolve_pinned_aligners(pins=fake_release["pins"], cache_root=cache)
    assert resolved is not None
    assert (resolved.samtools.provenance, resolved.minimap2.provenance) == (PINNED, PINNED)
    assert resolved.samtools.version == "1.21" and resolved.minimap2.version == "2.28-r1209"
    assert resolved.samtools.pin_sha256 == fake_release["pins"][0].tarball_sha256
    # A second install reuses the verified store.
    again, installed = at.install_aligners(
        fake_release["pins"], cache_root=cache, platform_name=FAKE_PLATFORM,
        downloader=lambda url, dest: pytest.fail("a verified store is not downloaded again"),
    )
    assert not installed and again.prefix == store.prefix


def test_install_refuses_a_tarball_that_misses_its_digest(tmp_path, fake_release) -> None:
    cache = tmp_path / "toolchains"

    def tampered(url: str, destination: Path) -> None:
        fake_release["downloader"](url, destination)
        with destination.open("ab") as handle:
            handle.write(b"x")

    with pytest.raises(ToolProblem) as caught:
        at.install_aligners(fake_release["pins"], cache_root=cache, platform_name=FAKE_PLATFORM, downloader=tampered)
    assert caught.value.reason == TOOL_WRONG
    assert "pinned digest" in caught.value.summary
    assert not at.store_prefix(fake_release["pins"], cache).exists()
    assert at.resolve_pinned_aligners(pins=fake_release["pins"], cache_root=cache) is None


def test_install_under_a_group_umask_publishes_a_store_that_verifies(tmp_path, fake_release) -> None:
    cache = tmp_path / "toolchains"
    previous = os.umask(0o002)  # Ubuntu/RHEL user-private groups
    try:
        store = _install(fake_release, cache)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((store.prefix / at.RECEIPT_NAME).stat().st_mode) == 0o600
    assert at.resolve_pinned_aligners(pins=fake_release["pins"], cache_root=cache) is not None


def test_a_staged_store_that_fails_the_checks_is_never_published(pinned_host, fake_release, monkeypatch) -> None:
    store = _install(fake_release, pinned_host)
    (store.prefix / "bin" / "minimap2").write_bytes(b"#!/bin/sh\n")  # damaged: the install will replace it
    monkeypatch.setattr(at, "RECEIPT_MODE", 0o664)
    with pytest.raises(LabProblem) as caught:
        at.install_aligners(
            fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM,
            downloader=fake_release["downloader"],
        )
    assert caught.value.code == "TBX-LAB-033" and "receipt is writable by group or other" in caught.value.cause
    assert (store.prefix / "bin" / "minimap2").read_bytes() == b"#!/bin/sh\n"  # old store untouched
    assert not [p for p in pinned_host.iterdir() if p.name.startswith(".")]


def test_resolution_waits_for_the_install_swap(pinned_host, fake_release) -> None:
    store = _install(fake_release, pinned_host)
    result: dict[str, object] = {}
    aside = pinned_host / "aside"
    with at._store_lock(pinned_host, exclusive=True):
        os.replace(store.prefix, aside)  # the gap an install's swap opens
        worker = threading.Thread(target=lambda: result.setdefault("tools", resolve_aligner_tools()))
        worker.start()
        worker.join(0.5)
        assert worker.is_alive()  # waiting, not falling back to PATH
        os.replace(aside, store.prefix)
    worker.join(10)
    assert result["tools"][0].provenance == PINNED


def test_a_symlinked_cache_root_is_refused_as_a_symlink(tmp_path, fake_release) -> None:
    real = tmp_path / "real-cache"
    _install(fake_release, real)
    link = tmp_path / "linked-cache"
    link.symlink_to(real)
    with pytest.raises(LabProblem) as caught:
        at.resolve_pinned_aligners(pins=fake_release["pins"], cache_root=link)
    assert caught.value.cause == "the toolchain cache directory is a symlink"
    assert caught.value.fix.startswith("Replace the symlink <TOOLCHAIN_CACHE> with a real directory")
    # The install refuses it too, with the same fix (never "run the install").
    with pytest.raises(LabProblem) as install:
        at.install_aligners(
            fake_release["pins"], cache_root=link, platform_name=FAKE_PLATFORM, downloader=fake_release["downloader"]
        )
    assert install.value.fix == caught.value.fix


def test_source_pin_runs_configure_make_install_into_staging(tmp_path, fake_release) -> None:
    releases = tmp_path / "src-release"
    releases.mkdir()
    archive = releases / "samtools-1.21.tar.bz2"
    digest = _tarball(archive, {"samtools-1.21/configure": b"#!/bin/sh\n"})
    url = "https://example.invalid/samtools-1.21.tar.bz2"
    source_pin = at.AlignerPin("samtools", "1.21", url, digest, "source", "samtools-1.21")
    pins = (source_pin, fake_release["pins"][1])
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    def runner(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs["env"] == at.BUILD_ENV  # nothing inherited from the caller
        if argv[:1] == ["./configure"]:
            runner.prefix = Path(argv[1].removeprefix("--prefix="))
        if argv == ["make", "install"]:
            binary = runner.prefix / "bin" / "samtools"
            binary.write_bytes(_script("samtools 1.21", SAMTOOLS))
            binary.chmod(0o755)
        return Done()

    def downloader(target_url: str, destination: Path) -> None:
        shutil.copyfile(archive if target_url == url else fake_release["sources"][target_url], destination)

    store, _ = at.install_aligners(
        pins, cache_root=tmp_path / "toolchains", platform_name=FAKE_PLATFORM, downloader=downloader, runner=runner,
    )
    assert [argv[0] for argv in calls] == ["./configure", "make", "make"]
    assert store.samtools.pin_sha256 == digest
    assert store.receipt.build_env == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


# -- resolution ---------------------------------------------------------------------------
def test_without_a_store_path_tools_are_unpinned(pinned_host) -> None:
    samtools, minimap2 = resolve_aligner_tools()
    assert samtools.path == SAMTOOLS and minimap2.path == MINIMAP2
    assert (samtools.provenance, minimap2.provenance) == (UNPINNED, UNPINNED)
    assert samtools.version and minimap2.version
    assert pins_of(samtools, minimap2) is None
    provenance = aligner_provenance(samtools, minimap2)
    assert provenance["provenance"] == UNPINNED and provenance["samtools"]["version"] == samtools.version


def test_an_unpinned_platform_uses_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(at, "current_platform", lambda: "osx-arm64")
    monkeypatch.setattr(at, "toolchain_cache_root", lambda: tmp_path / "toolchains")
    samtools, minimap2 = resolve_aligner_tools()
    assert (samtools.provenance, minimap2.provenance) == (UNPINNED, UNPINNED)


def test_an_installed_store_wins_over_path(pinned_host, fake_release) -> None:
    store = _install(fake_release, pinned_host)
    samtools, minimap2 = resolve_aligner_tools()
    assert samtools.path == str(store.prefix / "bin" / "samtools")
    assert minimap2.path == str(store.prefix / "bin" / "minimap2")
    assert pins_of(samtools, minimap2) == at.tool_pins(fake_release["pins"])


@pytest.mark.parametrize("damage", ["binary", "receipt_missing", "receipt_garbage", "not_executable"])
def test_a_damaged_store_is_tbx_lab_033_never_path(pinned_host, fake_release, damage) -> None:
    store = _install(fake_release, pinned_host)
    binary = store.prefix / "bin" / "minimap2"
    if damage == "binary":
        binary.write_bytes(binary.read_bytes() + b"# changed\n")
    elif damage == "receipt_missing":
        (store.prefix / at.RECEIPT_NAME).unlink()
    elif damage == "receipt_garbage":
        (store.prefix / at.RECEIPT_NAME).write_text("{")
    else:
        binary.chmod(0o644)
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.code == "TBX-LAB-033"
    assert caught.value.exit_code == 3
    assert "traceback toolchain install aligners --yes" in caught.value.fix
    assert "remove <TOOLCHAIN_CACHE>/aligners-*" in caught.value.fix  # a token: --json carries no host path
    assert str(pinned_host) not in caught.value.fix
    from traceback_lab.problems import problem_lines

    assert "<TOOLCHAIN_CACHE>" not in "\n".join(problem_lines(caught.value))  # the terminal gets the real path
    assert str(pinned_host) not in json.dumps(caught.value.data)


def test_a_consistently_planted_minimap2_is_refused_by_its_pin(pinned_host, fake_release) -> None:
    store = _install(fake_release, pinned_host)
    _replant(store, "minimap2", _script("2.28-r1209", MINIMAP2) + b"# planted\n")
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.code == "TBX-LAB-033" and "pinned release binary" in caught.value.cause


@pytest.mark.parametrize("where", ["cache_root", "prefix", "bin", "binary", "receipt"])
def test_a_store_others_can_write_is_tbx_lab_033(pinned_host, fake_release, where) -> None:
    store = _install(fake_release, pinned_host)
    target = {
        "cache_root": pinned_host, "prefix": store.prefix, "bin": store.prefix / "bin",
        "binary": store.prefix / "bin" / "samtools", "receipt": store.prefix / at.RECEIPT_NAME,
    }[where]
    target.chmod(target.stat().st_mode | 0o020)
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.code == "TBX-LAB-033" and "writable by group or other" in caught.value.cause
    if where == "cache_root":
        assert caught.value.fix.startswith("chmod go-w <TOOLCHAIN_CACHE> ")
    else:
        assert caught.value.fix == "chmod -R go-w <TOOLCHAIN_CACHE>/aligners-*"
    assert "--yes`" not in caught.value.fix.split("(")[0]  # the first step is never the refusing install


def test_a_store_owned_by_another_user_is_tbx_lab_033(pinned_host, fake_release, monkeypatch) -> None:
    _install(fake_release, pinned_host)
    monkeypatch.setattr(at, "_current_uid", lambda: 4242424)
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.code == "TBX-LAB-033" and "not owned by this user" in caught.value.cause
    assert caught.value.cause.startswith("the toolchain cache directory")
    assert caught.value.fix == 'sudo chown "$USER" <TOOLCHAIN_CACHE> && chmod 0700 <TOOLCHAIN_CACHE>'
    with pytest.raises(LabProblem) as inner:
        at._require_private(pinned_host, "aligner store directory")
    assert inner.value.fix == 'sudo chown -R "$USER" <TOOLCHAIN_CACHE>/aligners-*'
    with pytest.raises(LabProblem) as install:
        at.install_aligners(
            fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM,
            downloader=fake_release["downloader"],
        )
    assert install.value.fix == caught.value.fix


def test_an_unreadable_store_is_tbx_lab_033(pinned_host, fake_release, monkeypatch) -> None:
    store = _install(fake_release, pinned_host)
    real = Path.is_symlink

    def denied(self):
        if self == store.prefix:
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "is_symlink", denied)
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.code == "TBX-LAB-033" and "PermissionError" in caught.value.cause


def test_install_tightens_a_group_writable_cache_root_it_owns(tmp_path, fake_release) -> None:
    cache = tmp_path / "toolchains"
    cache.mkdir(mode=0o700)
    cache.chmod(0o775)  # e.g. created by hand under umask 0002
    _install(fake_release, cache)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert at.resolve_pinned_aligners(pins=fake_release["pins"], cache_root=cache) is not None


@pytest.mark.parametrize("damage", ["binary", "receipt_missing"])
def test_damage_fixes_say_reinstall(pinned_host, fake_release, damage) -> None:
    store = _install(fake_release, pinned_host)
    if damage == "binary":
        (store.prefix / "bin" / "samtools").write_bytes(b"#!/bin/sh\n")
    else:
        (store.prefix / at.RECEIPT_NAME).unlink()
    with pytest.raises(LabProblem) as caught:
        resolve_aligner_tools()
    assert caught.value.fix.startswith("Run `traceback toolchain install aligners --yes`")
    at.install_aligners(  # and that fix works
        fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM,
        downloader=fake_release["downloader"],
    )
    assert resolve_aligner_tools()[0].provenance == PINNED


def test_an_install_racing_a_published_store_keeps_it(pinned_host, fake_release, monkeypatch) -> None:
    store = _install(fake_release, pinned_host)
    inode = store.prefix.stat().st_ino
    real = at.resolve_pinned_aligners
    calls = {"n": 0}

    def first_sees_nothing(**kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(**kwargs)  # the other install publishes in between

    monkeypatch.setattr(at, "resolve_pinned_aligners", first_sees_nothing)
    again, installed = at.install_aligners(
        fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM,
        downloader=fake_release["downloader"],
    )
    assert not installed
    assert store.prefix.stat().st_ino == inode  # not swapped under a run that resolved it
    assert not [p for p in pinned_host.iterdir() if p.name.startswith(".")]


def test_install_maps_a_cache_write_failure_to_a_tool_problem(tmp_path, fake_release, monkeypatch) -> None:
    def no_mkdtemp(*args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(at.tempfile, "mkdtemp", no_mkdtemp)
    with pytest.raises(ToolProblem) as caught:
        at.install_aligners(
            fake_release["pins"], cache_root=tmp_path / "toolchains", platform_name=FAKE_PLATFORM,
            downloader=fake_release["downloader"],
        )
    assert "PermissionError" in caught.value.cause


def test_a_failed_reinstall_keeps_the_old_store_and_a_good_one_swaps_it(pinned_host, fake_release) -> None:
    store = _install(fake_release, pinned_host)
    (store.prefix / "bin" / "minimap2").write_bytes(b"#!/bin/sh\n")  # damaged

    def offline(url: str, destination: Path) -> None:
        raise OSError("offline")

    with pytest.raises(ToolProblem):
        at.install_aligners(fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM, downloader=offline)
    assert (store.prefix / "bin" / "minimap2").read_bytes() == b"#!/bin/sh\n"  # not rmtree'd first
    again, installed = at.install_aligners(
        fake_release["pins"], cache_root=pinned_host, platform_name=FAKE_PLATFORM,
        downloader=fake_release["downloader"],
    )
    assert installed and again.prefix == store.prefix
    assert resolve_aligner_tools()[1].provenance == PINNED
    assert not [p for p in pinned_host.iterdir() if p.name.startswith(".")]  # no staging or retired dirs


def test_doctor_runs_the_pinned_binaries(pinned_host, fake_release, tmp_path) -> None:
    store = _install(fake_release, pinned_host)
    # Digest-consistent but cannot run (as with a missing shared library).
    _replant(store, "samtools", b"#!/bin/sh\nexit 127\n")
    assert resolve_aligner_tools()[0].provenance == PINNED  # resolution executes nothing
    check = _provenance_check(LabStore(tmp_path / "root"))
    assert check["status"] == "blocked" and check["detail"].startswith("TBX-LAB-033: ")
    assert "does not run" in check["detail"]


# -- identity -----------------------------------------------------------------------------
_IDENTITY = dict(
    source_id="a" * 64,
    reference_sha256="b" * 64,
    mmi_sha256="c" * 64,
    minimap2_version="2.31-r1302",
    samtools_version="1.24",
    aligner_argv=["minimap2", "-ax", "map-ont", "-y"],
)


def test_unpinned_identity_is_the_pre_pinning_input_id() -> None:
    # Computed by origin/main's identity.py before pinning existed.
    legacy = "35faeb73bf96772f352010191d0d544f2ec64a53c7f8fd658d464cc656b4bb0c"
    assert aligned_input_id(**_IDENTITY) == legacy
    assert aligned_input_id(**_IDENTITY, tool_pins=None) == legacy


def test_pinned_identity_names_digests_not_paths(tmp_path, fake_release) -> None:
    here = _install(fake_release, tmp_path / "laptop" / "toolchains")
    there = _install(fake_release, tmp_path / "ci-runner" / "cache")
    assert here.samtools.path != there.samtools.path

    def input_id(store: at.PinnedAligners) -> str:
        return aligned_input_id(
            **{**_IDENTITY, "samtools_version": str(store.samtools.version),
               "minimap2_version": str(store.minimap2.version)},
            tool_pins=pins_of(store.samtools, store.minimap2),
        )

    assert input_id(here) == input_id(there)
    unpinned = aligned_input_id(
        **{**_IDENTITY, "samtools_version": "1.21", "minimap2_version": "2.28-r1209"}
    )
    assert input_id(here) != unpinned
    with pytest.raises(ValueError):
        aligned_input_id(**_IDENTITY, tool_pins={"samtools": "d" * 64})


# -- doctor -------------------------------------------------------------------------------
def _provenance_check(store: LabStore) -> dict:
    (check,) = [c for c in doctor.lab_checks(store) if c["name"] == "lab_aligner_provenance"]
    return check


def test_doctor_shows_pinned_unpinned_and_damaged(pinned_host, fake_release, tmp_path) -> None:
    store = LabStore(tmp_path / "root")
    check = _provenance_check(store)
    assert check["status"] == "warn" and check["detail"].startswith("unpinned:")
    assert check["fix"] == "traceback toolchain install aligners --yes"

    installed = _install(fake_release, pinned_host)
    check = _provenance_check(store)
    assert check["status"] == "pass" and check["detail"].startswith("pinned: samtools 1.21, minimap2 2.28-r1209")

    (installed.prefix / "bin" / "samtools").write_bytes(b"#!/bin/sh\n")
    check = _provenance_check(store)
    assert check["status"] == "blocked" and check["blocks_example"]
    assert check["detail"].startswith("TBX-LAB-033: ")
    assert "traceback toolchain install aligners --yes" in check["fix"]
    assert "<TOOLCHAIN_CACHE>/aligners-*" in check["fix"] and str(pinned_host) not in check["fix"]


def test_doctor_on_an_unpinned_platform_passes_without_a_fix(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(at, "current_platform", lambda: "osx-arm64")
    monkeypatch.setattr(at, "toolchain_cache_root", lambda: tmp_path / "toolchains")
    check = _provenance_check(LabStore(tmp_path / "root"))
    assert check["status"] == "pass" and "no pinned release artefact" in check["detail"]
    assert "fix" not in check


# -- CLI ----------------------------------------------------------------------------------
def test_install_dry_run_changes_nothing(pinned_host, capsys) -> None:
    code = signed_cli.main(["toolchain", "install", "aligners", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["data"]["dry_run"] is True and payload["data"]["needs_network"] is True
    assert {row["tool"] for row in payload["data"]["tools"]} == {"samtools", "minimap2"}
    assert str(pinned_host) not in json.dumps(payload)
    assert not pinned_host.exists()


def test_install_on_an_unpinned_platform_says_path(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(at, "current_platform", lambda: "osx-arm64")
    monkeypatch.setattr(at, "toolchain_cache_root", lambda: tmp_path / "toolchains")
    code = signed_cli.main(["toolchain", "install", "aligners", "--yes", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["data"]["platform_pinned"] is False
    assert "unpinned" in payload["summary"]
    assert not (tmp_path / "toolchains").exists()


# -- lab run ------------------------------------------------------------------------------
def _run(capsys, root: Path, chunks: Path, mmi: Path) -> tuple[int, dict]:
    code = signed_cli.main([
        "lab", "run", str(chunks), "--root", str(root), "--mmi", str(mmi),
        "--no-downsample", "--no-controls", "--json",
    ])
    return code, json.loads(capsys.readouterr().out)


def _aligner_event(root: Path, payload: dict) -> dict:
    log = root / "lab" / "runs" / f"{payload['data']['run_id']}.jsonl"
    (event,) = [json.loads(line) for line in log.read_text().splitlines() if '"aligner_tools"' in line]
    return event


def test_lab_run_records_provenance_and_keeps_existing_input_ids(
    lab_world, capsys, monkeypatch, shared_mmi, pinned_host, fake_release
) -> None:
    from traceback_lab import registry

    monkeypatch.setattr(registry, "ALGORITHMS", ())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("lab run used the network"))
    monkeypatch.setattr(at, "_download", lambda *a, **k: pytest.fail("lab run used the network"))
    root, chunks = lab_world["root"], lab_world["chunks"]
    lab = LabStore(root)

    code, first = _run(capsys, root, chunks, shared_mmi)
    assert code == 0, first
    unpinned_id = first["data"]["sample"]["input_id"]
    manifest = json.loads((lab.inputs_dir / unpinned_id / "manifest.json").read_bytes())
    assert manifest["tool_provenance"]["provenance"] == UNPINNED
    assert manifest["tool_pins"] is None
    assert aligned_input_id_for_manifest(manifest) == unpinned_id
    legacy = {key: value for key, value in manifest.items() if key not in {"tool_pins", "tool_provenance"}}
    assert aligned_input_id_for_manifest(legacy) == unpinned_id  # a manifest written before pinning
    assert manifest["tool_provenance"]["samtools"]["version"] == manifest["tools"]["samtools"]
    event = _aligner_event(root, first)
    assert event["provenance"] == UNPINNED and event["minimap2"]["version"] == manifest["tools"]["minimap2"]

    _install(fake_release, pinned_host)
    code, pinned = _run(capsys, root, chunks, shared_mmi)
    assert code == 0, pinned
    pinned_id = pinned["data"]["sample"]["input_id"]
    assert pinned_id != unpinned_id
    manifest = json.loads((lab.inputs_dir / pinned_id / "manifest.json").read_bytes())
    assert manifest["tool_provenance"]["provenance"] == PINNED
    assert manifest["tool_provenance"]["minimap2"]["pin_sha256"] == fake_release["pins"][1].tarball_sha256
    assert manifest["tool_provenance"]["minimap2"]["installed_binary_sha256"] == fake_release["pins"][1].binary_sha256
    assert manifest["tool_pins"] == at.tool_pins(fake_release["pins"])
    assert aligned_input_id_for_manifest(manifest) == pinned_id
    event = _aligner_event(root, pinned)
    assert event["provenance"] == PINNED
    for log in (root / "lab" / "runs").glob("*.jsonl"):
        assert str(pinned_host) not in log.read_text()
    assert str(pinned_host) not in json.dumps(manifest)

    # Back to PATH with the same versions: the pre-existing input keeps its input_id and is reused.
    shutil.rmtree(at.store_prefix(fake_release["pins"], pinned_host))
    before = {p.name for p in lab.inputs_dir.iterdir() if p.is_dir()}
    code, again = _run(capsys, root, chunks, shared_mmi)
    assert code == 0 and again["data"]["sample"]["input_id"] == unpinned_id
    assert {p.name for p in lab.inputs_dir.iterdir() if p.is_dir()} == before


def test_lab_run_refuses_a_damaged_store(lab_world, capsys, monkeypatch, shared_mmi, pinned_host, fake_release) -> None:
    from traceback_lab import registry

    monkeypatch.setattr(registry, "ALGORITHMS", ())
    store = _install(fake_release, pinned_host)
    (store.prefix / "bin" / "samtools").write_bytes(b"#!/bin/sh\nexit 0\n")
    code, payload = _run(capsys, lab_world["root"], lab_world["chunks"], shared_mmi)
    assert code == 3
    assert payload["data"]["code"] == "TBX-LAB-033"
    assert payload["data"]["docs"] == "docs/LAB-GUIDE.md#tbx-lab-033"
    assert not [p for p in LabStore(lab_world["root"]).inputs_dir.glob("*") if p.is_dir()]


def test_dry_run_prints_the_tools_line(lab_world, capsys, pinned_host, fake_release) -> None:
    argv = ["lab", "run", str(lab_world["chunks"]), "--root", str(lab_world["root"]), "--dry-run"]
    assert signed_cli.main(argv) == 0
    assert re.search(r"^TOOLS     samtools \S+ · minimap2 \S+ \(unpinned\)$", capsys.readouterr().out, re.M)
    _install(fake_release, pinned_host)
    assert signed_cli.main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["aligner_tools"]["provenance"] == PINNED
