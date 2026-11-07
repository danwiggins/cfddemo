"""Pinned samtools and minimap2 for the Lab: install, resolve, verify.

The Lab aligns with whatever ``samtools`` and ``minimap2`` are on ``PATH``
unless a pinned aligner store exists.  ``traceback toolchain install
aligners --yes`` builds that store from the same release tarballs, URLs and
sha256 digests the CI job uses (samtools 1.21, minimap2 2.28; Linux x86-64
only, the only platform with an official release artefact).  It lives in the
per-user toolchain cache next to the signed path's toolchains:
``~/.cache/traceback/toolchains/aligners-<pinset sha256>/``.

Resolution order is store, then ``PATH``:

- no store for this platform, or none installed: the ``PATH`` tools, recorded
  as provenance ``unpinned`` with the version each reports;
- a store whose receipt and binaries verify: provenance ``pinned``, with the
  release tarball digests (not the install path) entering the input identity;
- a store that is present but does not verify: TBX-LAB-033 (exit 3), never a
  silent fallback to ``PATH``.

Only the explicit install command touches the network; ``lab run`` and
``lab doctor`` read the cache and hash files.
"""

from __future__ import annotations

import hashlib
import contextlib
import fcntl
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.toolchain import (
    TOOL_MISSING,
    TOOL_WRONG,
    ToolProblem,
    current_platform,
    sha256_file,
    toolchain_cache_root,
)

from .tools import PINNED, Tool, _version

PLATFORM_LINUX = "linux-64"
RECEIPT_NAME = "traceback-aligners.json"
RECEIPT_SCHEMA = "traceback.aligner-toolchain-receipt.v1"
RECEIPT_MODE = 0o600
LOCK_NAME = "aligners.lock"
TOOLCHAIN_CACHE_TOKEN = "<TOOLCHAIN_CACHE>"
# The only environment a source build sees (no CC/CFLAGS/LDFLAGS/
# LD_LIBRARY_PATH/MAKEFLAGS from the caller); recorded in the receipt.
BUILD_ENV: dict[str, str] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
INSTALL_TIMEOUT_SECONDS = 1800
DOWNLOAD_TIMEOUT_SECONDS = 300
_SHA256 = r"^[0-9a-f]{64}$"
_VERSION_PATTERNS = {
    "samtools": r"samtools (\d+\.\d+(?:\.\d+)?)",
    "minimap2": r"^(\d+\.\d+[^\s]*)",
}


@dataclass(frozen=True)
class AlignerPin:
    """One release artefact: where it comes from and what its bytes are."""

    tool: str
    version: str
    url: str
    tarball_sha256: str
    kind: Literal["prebuilt", "source"]
    # prebuilt: the binary's path inside the extracted tarball.
    # source: the extracted source directory; the build installs bin/<tool>.
    member: str
    # The extracted binary's sha256 when the release ships one (prebuilt).
    # A source build's bytes depend on the compiler, so it has none.
    binary_sha256: str | None = None

    @property
    def archive_name(self) -> str:
        return self.url.rsplit("/", 1)[1]


# The same URLs and digests as .github/workflows/ci.yml ("Install Lab aligner
# tools (Linux)"); tests/lab/test_aligner_toolchain.py keeps them in step.
SAMTOOLS_LINUX = AlignerPin(
    tool="samtools",
    version="1.21",
    url="https://github.com/samtools/samtools/releases/download/1.21/samtools-1.21.tar.bz2",
    tarball_sha256="05724b083a6b6f0305fcae5243a056cc36cf826309c3cb9347a6b89ee3fc5ada",
    kind="source",
    member="samtools-1.21",
)
MINIMAP2_LINUX = AlignerPin(
    tool="minimap2",
    version="2.28",
    url="https://github.com/lh3/minimap2/releases/download/v2.28/minimap2-2.28_x64-linux.tar.bz2",
    tarball_sha256="51f2cf0e486d0f9f88ace1aa58fdc56571382a676ea0889ae607301c60693377",
    kind="prebuilt",
    member="minimap2-2.28_x64-linux/minimap2",
    binary_sha256="56e910107685c2448c5b673e094098bfdf70472bfa46f0b38c617edb8438e0dd",
)
# No official macOS release artefact exists for either tool (minimap2 ships a
# Linux x86-64 binary only; Homebrew bottles are Homebrew's, not upstream's),
# so macOS keeps using PATH and records ``unpinned``.
ALIGNER_PINS: dict[str, tuple[AlignerPin, AlignerPin]] = {
    PLATFORM_LINUX: (SAMTOOLS_LINUX, MINIMAP2_LINUX),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InstalledAligner(_Strict):
    version: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=512)
    tarball_sha256: str = Field(pattern=_SHA256)
    binary_relpath: str = Field(pattern=r"^bin/[a-z0-9]+$")
    installed_binary_sha256: str = Field(pattern=_SHA256)
    reported_version: str = Field(min_length=1, max_length=64)


class AlignerReceipt(_Strict):
    """Written last by ``install_aligners``: its presence marks a complete install."""

    schema_version: Literal["traceback.aligner-toolchain-receipt.v1"] = RECEIPT_SCHEMA
    platform: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    pinset_sha256: str = Field(pattern=_SHA256)
    tools: dict[str, InstalledAligner]
    # The environment source builds ran with (empty when nothing was built).
    build_env: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class PinnedAligners:
    samtools: Tool
    minimap2: Tool
    prefix: Path
    receipt: AlignerReceipt


Downloader = Callable[[str, Path], None]
Runner = Callable[..., Any]


def pins_for(platform_name: str | None = None) -> tuple[AlignerPin, AlignerPin] | None:
    """The pin set for this host (``None`` when no release artefact is pinned)."""

    name = platform_name if platform_name is not None else current_platform()
    return ALIGNER_PINS.get(name) if name is not None else None


def pinset_sha256(pins: Sequence[AlignerPin]) -> str:
    """The identity of a pin set: tools, versions, URLs and tarball digests."""

    return hashlib.sha256(
        canonical_json_bytes(
            [
                {"tool": pin.tool, "version": pin.version, "url": pin.url, "sha256": pin.tarball_sha256}
                for pin in sorted(pins, key=lambda pin: pin.tool)
            ]
        )
    ).hexdigest()


def store_prefix(pins: Sequence[AlignerPin], cache_root: Path | None = None) -> Path:
    root = cache_root if cache_root is not None else toolchain_cache_root()
    return root / f"aligners-{pinset_sha256(pins)}"


def tool_pins(pins: Sequence[AlignerPin]) -> dict[str, str]:
    """``{tool: tarball sha256}``: what enters ``input_id`` when pinned."""

    return {pin.tool: pin.tarball_sha256 for pin in pins}


_T = TOOLCHAIN_CACHE_TOKEN
_STORES = f"{_T}/aligners-*"
FIX_REINSTALL = (
    "Run `traceback toolchain install aligners --yes` (builds a new store and swaps it in), "
    f"or remove {_STORES} to fall back to PATH"
)
FIX_INSPECT = f"Check access with `ls -ld {_T} {_STORES}`, fix ownership or permissions, then rerun"
# Cause-specific fixes for the ownership/permission checks: for the cache
# directory itself the install would refuse too, so it is never the fix there
# (except for mode, which the install tightens on a directory you own).
_FIX_PRIVATE = {
    ("root", "symlink"): f"Replace the symlink {_T} with a real directory you own "
    f"(mode 0700), then run `traceback toolchain install aligners --yes`",
    ("root", "owner"): f'sudo chown "$USER" {_T} && chmod 0700 {_T}',
    ("root", "mode"): f"chmod go-w {_T} (or run `traceback toolchain install aligners --yes`, "
    "which tightens a cache directory you own)",
    ("store", "symlink"): FIX_REINSTALL,
    ("store", "owner"): f'sudo chown -R "$USER" {_STORES}',
    ("store", "mode"): f"chmod -R go-w {_STORES}",
}


def _broken(cause: str, *, fix: str = FIX_REINSTALL, data: dict[str, Any] | None = None):
    """TBX-LAB-033.  The FIX names the cache as ``<TOOLCHAIN_CACHE>``: --json
    output never carries a host path; human output substitutes the real one
    (``problems.problem_lines``, ``lab doctor``)."""

    from .problems import LabProblem

    return LabProblem("TBX-LAB-033", cause=cause, fix=fix, data={"store": "aligners", **(data or {})})


def _current_uid() -> int:
    return os.getuid()


def _require_private(path: Path, what: str, *, root: bool = False) -> None:
    """Refuse a store path another account could have written (TBX-LAB-033).

    The receipt only proves the store is self-consistent; this proves that
    only this user could have put it there.  ``root`` marks the cache
    directory itself, whose fixes differ.
    """

    scope = "root" if root else "store"
    try:
        info = path.lstat()
    except OSError as exc:
        raise _broken(f"the {what} could not be inspected ({type(exc).__name__})", fix=FIX_INSPECT) from None
    if stat.S_ISLNK(info.st_mode):
        raise _broken(f"the {what} is a symlink", fix=_FIX_PRIVATE[scope, "symlink"], data={"refused": what})
    if info.st_uid != _current_uid():
        raise _broken(
            f"the {what} is not owned by this user", fix=_FIX_PRIVATE[scope, "owner"], data={"refused": what}
        )
    if info.st_mode & 0o022:
        raise _broken(
            f"the {what} is writable by group or other", fix=_FIX_PRIVATE[scope, "mode"], data={"refused": what}
        )


@contextlib.contextmanager
def _store_lock(cache_root: Path, *, exclusive: bool) -> Iterator[None]:
    """``flock`` on ``<cache>/aligners.lock``: the install holds it exclusively
    for the swap, resolution shares it, so a run never sees the gap between
    moving the old store aside and renaming the new one in."""

    path = cache_root / LOCK_NAME
    if exclusive:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if exclusive:
            raise
        yield  # no install has ever swapped a store here
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)  # releases the lock


def resolve_pinned_aligners(
    *,
    pins: Sequence[AlignerPin] | None = None,
    platform_name: str | None = None,
    cache_root: Path | None = None,
    probe: bool = False,
) -> PinnedAligners | None:
    """The verified store, ``None`` when there is none, TBX-LAB-033 when damaged.

    Verification checks that this user owns the cache root, the store and
    its files and that no group/other can write them; that the receipt names
    exactly this pin set; that each binary's sha256 matches the receipt and,
    where the release fixes it (minimap2), the pin.  ``probe`` (``lab
    doctor``) also runs each binary's ``--version``, which catches a missing
    shared library; resolution alone executes nothing.
    """

    if pins is None:
        pins = pins_for(platform_name)
        if pins is None:
            return None
    root = cache_root if cache_root is not None else toolchain_cache_root()
    try:
        with _store_lock(root, exclusive=False):
            return _verify_store(tuple(pins), root, probe=probe)
    except OSError as exc:  # the lock, or any store read the checks did not map
        raise _broken(f"the aligner store could not be inspected ({type(exc).__name__})", fix=FIX_INSPECT) from None


def _verify_store(pins: tuple[AlignerPin, ...], root: Path, *, probe: bool) -> PinnedAligners | None:
    prefix = store_prefix(pins, root)
    try:
        if prefix.is_symlink():
            raise _broken("the aligner store directory is a symlink")
        if not prefix.exists():
            return None
    except OSError as exc:
        raise _broken(f"the aligner store could not be inspected ({type(exc).__name__})", fix=FIX_INSPECT) from None
    _require_private(root, "toolchain cache directory", root=True)
    _require_private(prefix, "aligner store directory")
    receipt_path = prefix / RECEIPT_NAME
    try:
        receipt = AlignerReceipt.model_validate_json(receipt_path.read_bytes())
    except FileNotFoundError:
        raise _broken(
            "the aligner store exists but its install receipt is missing (interrupted install?)"
        ) from None
    except (OSError, ValueError, ValidationError):
        raise _broken("the aligner store's install receipt is unreadable or malformed") from None
    _require_private(receipt_path, "aligner store receipt")
    expected = pinset_sha256(pins)
    if receipt.pinset_sha256 != expected or set(receipt.tools) != {pin.tool for pin in pins}:
        raise _broken(
            "the aligner store's receipt names another pin set",
            data={"expected_pinset_sha256": expected},
        )
    tools: dict[str, Tool] = {}
    for pin in pins:
        entry = receipt.tools[pin.tool]
        data = {"tool": pin.tool}
        if entry.tarball_sha256 != pin.tarball_sha256 or entry.version != pin.version:
            raise _broken(f"the receipt's {pin.tool} entry is not the pinned release", data=data)
        if pin.binary_sha256 is not None and entry.installed_binary_sha256 != pin.binary_sha256:
            raise _broken(
                f"the receipt's {pin.tool} binary sha256 is not the pinned release binary", data=data
            )
        binary = prefix / entry.binary_relpath
        try:
            if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
                raise _broken(f"the pinned {pin.tool} binary is missing or not executable", data=data)
            _require_private(binary.parent, "aligner store bin directory")
            _require_private(binary, f"pinned {pin.tool} binary")
            digest = sha256_file(binary)
        except OSError as exc:
            raise _broken(
                f"the pinned {pin.tool} binary could not be read ({type(exc).__name__})", data=data
            ) from None
        if digest != entry.installed_binary_sha256:
            raise _broken(
                f"the pinned {pin.tool} binary's sha256 differs from its install receipt", data=data
            )
        if probe and _version([str(binary), "--version"], _VERSION_PATTERNS[pin.tool]) != entry.reported_version:
            raise _broken(
                f"the pinned {pin.tool} binary does not run or report {entry.reported_version} "
                "(a missing shared library?)", data=data,
            )
        tools[pin.tool] = Tool(
            pin.tool, str(binary), entry.reported_version, provenance=PINNED, pin_sha256=pin.tarball_sha256,
            binary_sha256=digest,
        )
    return PinnedAligners(tools["samtools"], tools["minimap2"], prefix, receipt)


def resolve_aligners(
    *,
    platform_name: str | None = None,
    cache_root: Path | None = None,
    probe: bool = False,
) -> tuple[Tool, Tool]:
    """samtools and minimap2: the verified store first, else ``PATH`` (unpinned)."""

    from .tools import find_minimap2, find_samtools

    pinned = resolve_pinned_aligners(platform_name=platform_name, cache_root=cache_root, probe=probe)
    if pinned is not None:
        return pinned.samtools, pinned.minimap2
    return find_samtools(), find_minimap2()


# -- install -----------------------------------------------------------------------------
def _download(url: str, destination: Path) -> None:
    """Fetch one https URL to ``destination`` (only the install command calls this)."""

    if not url.startswith("https://"):
        raise ValueError("aligner pins are https URLs")
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, destination.open("wb") as out:  # noqa: S310 - pinned https URL
        shutil.copyfileobj(response, out)


def _problem(reason: str, summary: str, *, tool: str, cause: str, fix: str) -> ToolProblem:
    return ToolProblem(reason, summary, tool=tool, cause=cause, fix=fix)


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Regular files and directories under the extraction root only."""

    members = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
            raise ValueError("unsafe tar member")
        member.mode &= 0o755
        members.append(member)
    return members


def _extract(archive: Path, into: Path, pin: AlignerPin) -> None:
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            members = _safe_members(tar)
            if hasattr(tarfile, "data_filter"):  # Python >= 3.11.4
                tar.extractall(into, members=members, filter="data")  # noqa: S202 - checked members
            else:  # pragma: no cover - older 3.11 patch releases
                tar.extractall(into, members=members)  # noqa: S202 - checked members
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise _problem(
            TOOL_WRONG,
            f"The {pin.tool} release tarball could not be extracted",
            tool=pin.tool,
            cause=f"{pin.archive_name} verified by digest but is not a readable tar.bz2 ({type(exc).__name__})",
            fix="Run the install again; if it repeats, report the tarball name",
        ) from exc


def _build_source(pin: AlignerPin, source: Path, staging: Path, *, runner: Runner, log: Any, timeout: float) -> None:
    """``./configure && make && make install`` into ``staging`` (samtools)."""

    jobs = str(max(1, min(8, os.cpu_count() or 1)))
    steps = (
        ["./configure", f"--prefix={staging}", "--without-curses"],
        ["make", "-j", jobs],
        ["make", "install"],
    )
    for argv in steps:
        try:
            completed = runner(
                argv, cwd=str(source), check=False, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, env=dict(BUILD_ENV),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _problem(
                TOOL_MISSING,
                f"Building {pin.tool} {pin.version} did not finish",
                tool=pin.tool,
                cause=f"`{' '.join(argv[:2])}` could not run or timed out",
                fix="Install a C compiler, make, and the zlib, bzip2, xz and curl development headers "
                "(Debian/Ubuntu: build-essential zlib1g-dev libbz2-dev liblzma-dev libcurl4-openssl-dev), "
                "then run the install again; see the install log next to the toolchain cache",
            ) from exc
        if completed.returncode != 0:
            raise _problem(
                TOOL_MISSING,
                f"Building {pin.tool} {pin.version} failed",
                tool=pin.tool,
                cause=f"`{' '.join(argv[:2])}` exited {completed.returncode}",
                fix="Install a C compiler, make, and the zlib, bzip2, xz and curl development headers "
                "(Debian/Ubuntu: build-essential zlib1g-dev libbz2-dev liblzma-dev libcurl4-openssl-dev), "
                "then run the install again; see the install log next to the toolchain cache",
            )


def _reported_version(pin: AlignerPin, binary: Path) -> str:
    version = _version([str(binary), "--version"], _VERSION_PATTERNS[pin.tool])
    if version is None or not (version == pin.version or version.startswith(pin.version + "-") or version.startswith(pin.version + ".")):
        raise _problem(
            TOOL_WRONG,
            f"The installed {pin.tool} does not report version {pin.version}",
            tool=pin.tool,
            cause=f"`{pin.tool} --version` reported {version or 'nothing usable'}",
            fix="Run the install again; if it repeats, report the tarball name",
        )
    return version


def _install_io_problem(exc: OSError, what: str) -> ToolProblem:
    return _problem(
        TOOL_MISSING,
        "The aligner install could not write the toolchain cache",
        tool="aligners",
        cause=f"{what} failed ({type(exc).__name__}, errno {exc.errno})",
        fix="Check ownership and free space of the toolchain cache; if another "
        "`traceback toolchain install aligners` is running, wait for it, then run the install again",
    )


def _stage_tool(pin: AlignerPin, staging: Path, src: Path, *, downloader: Downloader, runner: Runner,
                log: Any, timeout: float) -> InstalledAligner:
    archive = src / pin.archive_name
    try:
        downloader(pin.url, archive)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "missing, retryable"
        raise _problem(
            TOOL_MISSING,
            f"Downloading {pin.tool} {pin.version} failed",
            tool=pin.tool,
            cause=f"{pin.archive_name} could not be fetched ({type(exc).__name__})",
            fix="Check the network, then run the install again",
        ) from exc
    digest = sha256_file(archive)
    if digest != pin.tarball_sha256:
        raise _problem(
            TOOL_WRONG,
            f"The downloaded {pin.tool} release does not match its pinned digest",
            tool=pin.tool,
            cause=f"{pin.archive_name} sha256 {digest[:12]}… differs from the pinned {pin.tarball_sha256[:12]}…",
            fix="Nothing was installed. Check the network path (proxy, mirror), then run the install again",
        )
    _extract(archive, src, pin)
    binary = staging / "bin" / pin.tool
    if pin.kind == "prebuilt":
        member = src / pin.member
        if member.is_symlink() or not member.is_file():
            raise _problem(
                TOOL_WRONG,
                f"The {pin.tool} release tarball has no {pin.member}",
                tool=pin.tool,
                cause="the digest-verified tarball does not contain the pinned binary path",
                fix="Run the install again; if it repeats, report the tarball name",
            )
        if pin.binary_sha256 is not None and sha256_file(member) != pin.binary_sha256:
            raise _problem(
                TOOL_WRONG,
                f"The {pin.tool} release binary does not match its pinned digest",
                tool=pin.tool,
                cause=f"{pin.member} sha256 differs from the pinned {pin.binary_sha256[:12]}…",
                fix="Nothing was installed. Run the install again; if it repeats, report the tarball name",
            )
        shutil.copyfile(member, binary)
        binary.chmod(0o755)
    else:
        _build_source(pin, src / pin.member, staging, runner=runner, log=log, timeout=timeout)
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise _problem(
            TOOL_MISSING,
            f"The {pin.tool} install produced no executable",
            tool=pin.tool,
            cause=f"bin/{pin.tool} is missing after the build",
            fix="See the install log next to the toolchain cache, then run the install again",
        )
    binary.chmod(0o755)
    return InstalledAligner(
        version=pin.version,
        url=pin.url,
        tarball_sha256=pin.tarball_sha256,
        binary_relpath=f"bin/{pin.tool}",
        installed_binary_sha256=sha256_file(binary),
        reported_version=_reported_version(pin, binary),
    )


def _tighten_owned_root(cache_root: Path) -> None:
    """chmod 0700 a cache directory this user owns but others can write.

    A parent created under umask 0002 (Ubuntu/RHEL user-private groups) or by
    hand can be 0775; tightening one's own directory is safe.  A symlink or a
    directory owned by someone else is left alone and refused afterwards.
    """

    try:
        info = cache_root.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == _current_uid() and info.st_mode & 0o022:
            cache_root.chmod(0o700)
    except OSError as exc:
        raise _install_io_problem(exc, "tightening the toolchain cache permissions") from exc


def install_aligners(
    pins: Sequence[AlignerPin],
    *,
    cache_root: Path,
    platform_name: str,
    downloader: Downloader = _download,
    runner: Runner = subprocess.run,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> tuple[PinnedAligners, bool]:
    """Download, verify, build/extract and publish the store; ``(store, newly_installed)``.

    A verified store is reused.  Everything is built under a sibling staging
    directory; only a complete, receipted staging directory replaces the
    prefix (the old one is moved aside first and deleted after the swap), so
    an interrupted or failed install never removes a store or leaves a half
    one.  A cache root another account could write is refused (TBX-LAB-033).
    """

    from traceback_runner.references import ReferenceProblem

    pins = tuple(pins)
    try:
        existing = resolve_pinned_aligners(pins=pins, cache_root=cache_root)
    except ReferenceProblem:  # a damaged store is replaced below
        existing = None
    if existing is not None:
        return existing, False
    prefix = store_prefix(pins, cache_root)
    if prefix.parent != cache_root:
        raise RuntimeError("install prefix escaped the toolchain cache")
    try:
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _install_io_problem(exc, "creating the toolchain cache") from exc
    _tighten_owned_root(cache_root)
    _require_private(cache_root, "toolchain cache directory", root=True)
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{prefix.name}.staging-", dir=cache_root))
    except OSError as exc:
        raise _install_io_problem(exc, "creating the staging directory") from exc
    log_path = cache_root / f"{prefix.name}.install.log"
    retired: Path | None = None
    try:
        try:
            (staging / "bin").mkdir(mode=0o755)
            src = staging / "src"
            src.mkdir()
            installed: dict[str, InstalledAligner] = {}
            with log_path.open("wb") as log:
                for pin in pins:
                    installed[pin.tool] = _stage_tool(
                        pin, staging, src, downloader=downloader, runner=runner, log=log, timeout=timeout
                    )
            shutil.rmtree(src, ignore_errors=True)
            receipt = AlignerReceipt(
                platform=platform_name, pinset_sha256=pinset_sha256(pins), tools=installed,
                build_env=dict(BUILD_ENV) if any(pin.kind == "source" for pin in pins) else {},
            )
            staged = staging / f".{RECEIPT_NAME}.tmp"
            staged.write_bytes(receipt.model_dump_json().encode("utf-8"))
            os.chmod(staged, RECEIPT_MODE)  # explicit: a umask 0002 would leave it group-writable
            os.replace(staged, staging / RECEIPT_NAME)
            staging.chmod(0o700)
            (staging / "bin").chmod(0o755)
        except OSError as exc:
            raise _install_io_problem(exc, "writing the aligner store") from exc
        # The staged store must pass the same checks resolution applies, or
        # it is never published (and the old store is never moved aside).
        for path, what in (
            (staging, "aligner store directory"),
            (staging / RECEIPT_NAME, "aligner store receipt"),
            (staging / "bin", "aligner store bin directory"),
            *((staging / "bin" / pin.tool, f"pinned {pin.tool} binary") for pin in pins),
        ):
            _require_private(path, what)
        swap_error: OSError | None = None
        try:
            with _store_lock(cache_root, exclusive=True):
                # A concurrent install may have published a verified store since
                # the check above, and a run may already be using it: keep it.
                try:
                    published = _verify_store(pins, cache_root, probe=False)
                except ReferenceProblem:
                    published = None
                if published is not None:
                    return published, False
                try:
                    if prefix.is_symlink() or prefix.exists():
                        retired = Path(tempfile.mkdtemp(prefix=f".{prefix.name}.old-", dir=cache_root))
                        os.replace(prefix, retired / "store")
                    os.replace(staging, prefix)
                except OSError as exc:
                    if retired is not None and not prefix.exists() and (retired / "store").exists():
                        os.replace(retired / "store", prefix)  # put the old store back
                    swap_error = exc
        except OSError as exc:  # the lock itself
            swap_error = exc
        if swap_error is not None:
            # A concurrent install may have published the same store first.
            try:
                raced = resolve_pinned_aligners(pins=pins, cache_root=cache_root)
            except ReferenceProblem:
                raced = None
            if raced is not None:
                return raced, False
            raise _install_io_problem(swap_error, "publishing the aligner store") from swap_error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if retired is not None:
            shutil.rmtree(retired, ignore_errors=True)
    resolved = resolve_pinned_aligners(pins=pins, cache_root=cache_root)
    if resolved is None:  # pragma: no cover - the rename just happened
        raise RuntimeError("the aligner store vanished after install")
    return resolved, True


def install_plan(pins: Sequence[AlignerPin], cache_root: Path) -> dict[str, Any]:
    """What ``install`` would do: per tool URL and digest, and the target."""

    return {
        "tools": [
            {
                "tool": pin.tool,
                "version": pin.version,
                "url": pin.url,
                "tarball_sha256": pin.tarball_sha256,
                "build": "from source (./configure && make)" if pin.kind == "source" else "prebuilt binary",
            }
            for pin in pins
        ],
        "pinset_sha256": pinset_sha256(pins),
        "target": str(store_prefix(pins, cache_root)),
    }


# -- CLI: traceback toolchain install aligners ---------------------------------------------
def main_install(args: Any) -> int:
    """``traceback toolchain install aligners [--yes] [--json]``."""

    from traceback_runner.cli import ExitCode, _emit, _problem as _problem_payload, _result
    from traceback_runner.references import ReferenceProblem

    command = "toolchain install"
    as_json = bool(args.as_json)
    platform_name = current_platform()
    pins = pins_for(platform_name)
    cache_root = toolchain_cache_root()
    if pins is None:
        shown = platform_name or "this platform"
        payload = _result(
            command,
            "ok",
            f"No pinned aligner artefact exists for {shown}: the Lab keeps using samtools and "
            "minimap2 from PATH and records them as unpinned",
            data={
                "tool": "aligners",
                "platform": platform_name,
                "platform_pinned": False,
                "pinned_platforms": sorted(ALIGNER_PINS),
                "dry_run": not args.yes,
                "needs_network": False,
                "next": "brew install samtools minimap2 (macOS); `traceback lab doctor` shows the provenance",
            },
        )
        _emit(payload, as_json=as_json)
        return int(ExitCode.OK)
    plan = install_plan(pins, cache_root)
    data: dict[str, object] = {
        "tool": "aligners",
        "platform": platform_name,
        "platform_pinned": True,
        "pinset_sha256": plan["pinset_sha256"],
        "tools": plan["tools"],
        "needs_network": True,
    }
    try:
        if not args.yes:
            payload = _result(
                command,
                "ok",
                "Dry run: samtools 1.21 and minimap2 2.28 would be downloaded, digest-verified and "
                "installed into the per-user toolchain cache; this needs the network and a C "
                "toolchain for samtools. Re-run with --yes to install",
                data={**data, "dry_run": True},
            )
            if not as_json:
                payload["data"]["target"] = plan["target"]
            _emit(payload, as_json=as_json)
            return int(ExitCode.OK)
        store, installed = install_aligners(pins, cache_root=cache_root, platform_name=platform_name)
        payload = _result(
            command,
            "ok",
            "samtools 1.21 and minimap2 2.28 "
            + ("installed and verified" if installed else "already installed and verified")
            + "; the Lab now records them as pinned",
            data={
                **data,
                "dry_run": False,
                "installed": {
                    name: {
                        "reported_version": entry.reported_version,
                        "installed_binary_sha256": entry.installed_binary_sha256,
                    }
                    for name, entry in sorted(store.receipt.tools.items())
                },
            },
        )
        _emit(payload, as_json=as_json)
        return int(ExitCode.OK)
    except ReferenceProblem as problem:  # ToolProblem, or TBX-LAB-033 for an unsafe cache
        payload = _problem_payload(command, problem)
        if not as_json and isinstance(payload["data"].get("fix"), str):
            payload["data"]["fix"] = payload["data"]["fix"].replace(TOOLCHAIN_CACHE_TOKEN, str(cache_root))
        _emit(payload, as_json=as_json)
        return int(problem.exit_code)


__all__ = [
    "ALIGNER_PINS",
    "MINIMAP2_LINUX",
    "PLATFORM_LINUX",
    "RECEIPT_NAME",
    "SAMTOOLS_LINUX",
    "AlignerPin",
    "AlignerReceipt",
    "PinnedAligners",
    "install_aligners",
    "install_plan",
    "main_install",
    "pins_for",
    "pinset_sha256",
    "resolve_aligners",
    "resolve_pinned_aligners",
    "store_prefix",
    "tool_pins",
]
