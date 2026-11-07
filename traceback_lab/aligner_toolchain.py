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
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
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


def _broken(cause: str, *, data: dict[str, Any] | None = None):
    from .problems import LabProblem

    return LabProblem(
        "TBX-LAB-033",
        cause=cause,
        fix="Run `traceback toolchain install aligners --yes` (replaces the damaged store), "
        "or remove ~/.cache/traceback/toolchains/aligners-* to fall back to PATH",
        data={"store": "aligners", **(data or {})},
    )


def resolve_pinned_aligners(
    *,
    pins: Sequence[AlignerPin] | None = None,
    platform_name: str | None = None,
    cache_root: Path | None = None,
) -> PinnedAligners | None:
    """The verified store, ``None`` when there is none, TBX-LAB-033 when damaged.

    Verification re-hashes both binaries against the receipt and checks the
    receipt names exactly this pin set; nothing is executed.
    """

    if pins is None:
        pins = pins_for(platform_name)
        if pins is None:
            return None
    prefix = store_prefix(pins, cache_root)
    if prefix.is_symlink():
        raise _broken("the aligner store directory is a symlink")
    if not prefix.exists():
        return None
    receipt_path = prefix / RECEIPT_NAME
    try:
        receipt = AlignerReceipt.model_validate_json(receipt_path.read_bytes())
    except FileNotFoundError:
        raise _broken("the aligner store exists but its install receipt is missing (interrupted install?)") from None
    except (OSError, ValueError, ValidationError):
        raise _broken("the aligner store's install receipt is unreadable or malformed") from None
    expected = pinset_sha256(pins)
    if receipt.pinset_sha256 != expected or set(receipt.tools) != {pin.tool for pin in pins}:
        raise _broken("the aligner store's receipt names another pin set", data={"expected_pinset_sha256": expected})
    tools: dict[str, Tool] = {}
    for pin in pins:
        entry = receipt.tools[pin.tool]
        if entry.tarball_sha256 != pin.tarball_sha256 or entry.version != pin.version:
            raise _broken(f"the receipt's {pin.tool} entry is not the pinned release", data={"tool": pin.tool})
        binary = prefix / entry.binary_relpath
        if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise _broken(f"the pinned {pin.tool} binary is missing or not executable", data={"tool": pin.tool})
        if sha256_file(binary) != entry.installed_binary_sha256:
            raise _broken(
                f"the pinned {pin.tool} binary's sha256 differs from its install receipt", data={"tool": pin.tool}
            )
        tools[pin.tool] = Tool(
            pin.tool, str(binary), entry.reported_version, provenance=PINNED, pin_sha256=pin.tarball_sha256
        )
    return PinnedAligners(tools["samtools"], tools["minimap2"], prefix, receipt)


def resolve_aligners(
    *,
    platform_name: str | None = None,
    cache_root: Path | None = None,
) -> tuple[Tool, Tool]:
    """samtools and minimap2: the verified store first, else ``PATH`` (unpinned)."""

    from .tools import find_minimap2, find_samtools

    pinned = resolve_pinned_aligners(platform_name=platform_name, cache_root=cache_root)
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


def _extract(archive: Path, into: Path, pin: AlignerPin) -> None:
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(into, filter="data")  # noqa: S202 - digest-verified release tarball, data filter
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
                timeout=timeout,
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

    A verified store is reused.  A prefix that does not verify (partial or
    damaged) is replaced.  Everything is built under a sibling staging
    directory and renamed into place after the receipt is written, so an
    interrupted install never leaves a half store at the prefix.
    """

    pins = tuple(pins)
    try:
        existing = resolve_pinned_aligners(pins=pins, cache_root=cache_root)
    except Exception:  # noqa: BLE001 - a damaged store is replaced
        existing = None
    if existing is not None:
        return existing, False
    prefix = store_prefix(pins, cache_root)
    if prefix.parent != cache_root:
        raise RuntimeError("install prefix escaped the toolchain cache")
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if prefix.is_symlink():
        prefix.unlink()
    elif prefix.exists():
        shutil.rmtree(prefix)
    staging = Path(tempfile.mkdtemp(prefix=f".{prefix.name}.staging-", dir=cache_root))
    log_path = cache_root / f"{prefix.name}.install.log"
    try:
        (staging / "bin").mkdir()
        src = staging / "src"
        src.mkdir()
        installed: dict[str, InstalledAligner] = {}
        with log_path.open("wb") as log:
            for pin in pins:
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
                installed[pin.tool] = InstalledAligner(
                    version=pin.version,
                    url=pin.url,
                    tarball_sha256=pin.tarball_sha256,
                    binary_relpath=f"bin/{pin.tool}",
                    installed_binary_sha256=sha256_file(binary),
                    reported_version=_reported_version(pin, binary),
                )
        shutil.rmtree(src, ignore_errors=True)
        receipt = AlignerReceipt(platform=platform_name, pinset_sha256=pinset_sha256(pins), tools=installed)
        staged = staging / f".{RECEIPT_NAME}.tmp"
        staged.write_bytes(receipt.model_dump_json().encode("utf-8"))
        os.replace(staged, staging / RECEIPT_NAME)
        os.replace(staging, prefix)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
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
    except ToolProblem as problem:
        _emit(_problem_payload(command, problem), as_json=as_json)
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
