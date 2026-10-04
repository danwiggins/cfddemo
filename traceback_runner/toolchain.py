"""Pinned external tools: install from a committed lock, resolve, hash, exec.

modkit 0.6.4 is recorded by three things (signal-methods spec §3.1, §11.8,
§11.10, §11.20):

- its version;
- its conda lock line (package URL and package sha256), from a committed
  ``@EXPLICIT`` lock whose every line carries ``#sha256:``;
- the binary's sha256.

The binary digest has two forms.  conda rewrites the install prefix into the
binary on install (and macOS re-signs it), so the installed file's digest
depends on the install path: two installs of the same lock into different
directories give different digests.  The pin therefore records, per platform,
the digest of the binary *inside the package* (``paths_data`` ``sha256``,
stable across installs), and each install records its own installed digest in
a receipt.  The binary is re-hashed against that receipt right before every
exec.

Toolchains live in the per-user cache ``~/.cache/traceback/toolchains/<lock
sha256>/``, shared by every ROOT.  ``install`` without ``yes=True`` only plans.

Threat model: the OS-user boundary.  In-process code mutation and same-user
filesystem races (for example a swap between the hash and the exec) are out of
scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .references import ReferenceProblem

TOOL_MISSING = "missing"
TOOL_WRONG = "wrong_version_or_digest"
ToolProblemReason = Literal["missing", "wrong_version_or_digest"]

LOCK_DIRECTORY = Path(__file__).resolve().parent / "toolchain_locks"
RECEIPT_NAME = "traceback-toolchain.json"
INSTALL_TIMEOUT_SECONDS = 1800
VERSION_TIMEOUT_SECONDS = 10

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Pinned channel URLs only; the subdir must be the lock's platform or noarch.
_LOCK_LINE = re.compile(
    r"^https://conda\.anaconda\.org/(?P<channel>conda-forge|bioconda)/"
    r"(?P<subdir>[a-z0-9-]+)/(?P<fn>[A-Za-z0-9_.+-]+\.(?:conda|tar\.bz2))"
    r"#sha256:(?P<sha256>[0-9a-f]{64})$"
)


class ToolProblem(ReferenceProblem):
    """TBX-TOOL-001: a pinned tool is missing, or not the pinned bytes.

    ``reason`` splits the one code into its two operator causes, each with its
    own cause and fix text; ``data`` never carries a host path.
    """

    def __init__(
        self,
        reason: ToolProblemReason,
        summary: str,
        *,
        tool: str,
        cause: str,
        fix: str,
    ) -> None:
        super().__init__(
            "TBX-TOOL-001",
            summary,
            cause=cause,
            fix=fix,
            exit_code=3,
            # A missing tool at stage time is retryable once installed (§11.5).
            retryable=reason == TOOL_MISSING,
        )
        self.reason = reason
        self.tool = tool
        self.data = {"reason": reason, "tool": tool}


@dataclass(frozen=True, slots=True)
class ToolPin:
    """What one platform's install of one tool must be."""

    tool: str
    version: str
    platform: str
    lock_name: str
    lock_sha256: str
    package_url: str
    package_sha256: str
    binary_relpath: str
    package_binary_sha256: str
    lock_directory: Path = LOCK_DIRECTORY

    @property
    def lock_line(self) -> str:
        return f"{self.package_url}#sha256:{self.package_sha256}"

    @property
    def lock_path(self) -> Path:
        return self.lock_directory / self.lock_name

    @property
    def package_record_name(self) -> str:
        """The package's ``conda-meta`` record file name."""

        filename = self.package_url.rsplit("/", 1)[1]
        for suffix in (".conda", ".tar.bz2"):
            if filename.endswith(suffix):
                return filename[: -len(suffix)] + ".json"
        raise ValueError("package URL has no conda archive suffix")


MODKIT_VERSION = "0.6.4"
MODKIT_PINS: Mapping[str, ToolPin] = {
    "osx-arm64": ToolPin(
        tool="modkit",
        version=MODKIT_VERSION,
        platform="osx-arm64",
        lock_name="modkit-osx-arm64.lock",
        lock_sha256="e181cc8cc3ab08f931a6b3b2099fe89751ff5c82e2ef758aa2e8c45b309eee4d",
        package_url=(
            "https://conda.anaconda.org/bioconda/osx-arm64/"
            "ont-modkit-0.6.4-h2797cb0_0.conda"
        ),
        package_sha256="8de445bc5375d69582548d31ad6e1e39f3bb4ed57194feccb238859e71bffe68",
        binary_relpath="bin/modkit",
        package_binary_sha256=(
            "cea7728a6dd8a3fe5f85b4396321ef70ef29980c437d1f32bc240427a7d97e3b"
        ),
    ),
    "linux-64": ToolPin(
        tool="modkit",
        version=MODKIT_VERSION,
        platform="linux-64",
        lock_name="modkit-linux-64.lock",
        lock_sha256="52f8608bb743740e2b0f778c264cde4a10c90b281e238164374a36c1ae9d6658",
        package_url=(
            "https://conda.anaconda.org/bioconda/linux-64/"
            "ont-modkit-0.6.4-h7f49ad2_0.conda"
        ),
        package_sha256="ab8190dd67a778619155b079307a1dc1cda939668b3d12d0538632100df2dcbe",
        binary_relpath="bin/modkit",
        package_binary_sha256=(
            "6900ef441d0e9abcddb81d266c756882b0514d82f0f5bfda612bddcb21b7a764"
        ),
    ),
}
TOOL_PINS: Mapping[str, Mapping[str, ToolPin]] = {"modkit": MODKIT_PINS}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ToolIdentity(_Strict):
    """The tool identity that enters a method definition's ``tools[]``."""

    schema_version: Literal["traceback.tool-identity.v1"] = "traceback.tool-identity.v1"
    tool_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    platform: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    lock_sha256: str = Field(pattern=_SHA256.pattern)
    lock_line: str = Field(min_length=1, max_length=512)
    package_sha256: str = Field(pattern=_SHA256.pattern)
    package_binary_sha256: str = Field(pattern=_SHA256.pattern)
    installed_binary_sha256: str = Field(pattern=_SHA256.pattern)


class ToolchainReceipt(_Strict):
    """Written last by ``install``: its presence marks a complete install."""

    schema_version: Literal["traceback.toolchain-receipt.v1"] = (
        "traceback.toolchain-receipt.v1"
    )
    identity: ToolIdentity
    binary_relpath: str = Field(pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


@dataclass(frozen=True, slots=True)
class PinnedTool:
    """A resolved, verified tool: run it only through :func:`exec_pinned`."""

    path: Path
    identity: ToolIdentity


@dataclass(frozen=True, slots=True)
class InstallPlan:
    tool: str
    platform: str
    argv: tuple[str, ...]
    prefix: Path
    lock_sha256: str
    needs_network: bool = True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def current_platform() -> str | None:
    """The conda subdir of this host, or ``None`` when no lock covers it."""

    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return "osx-arm64"
    if sys.platform.startswith("linux") and machine in {"x86_64", "amd64"}:
        return "linux-64"
    return None


def toolchain_cache_root() -> Path:
    """``~/.cache/traceback/toolchains``: per user, shared by every ROOT."""

    return Path.home() / ".cache" / "traceback" / "toolchains"


def pin_for(tool: str, platform_name: str | None = None) -> ToolPin:
    pins = TOOL_PINS.get(tool)
    if pins is None:
        raise ValueError(f"unknown tool {tool!r}")
    name = platform_name if platform_name is not None else current_platform()
    pin = pins.get(name) if name is not None else None
    if pin is None:
        raise ToolProblem(
            TOOL_MISSING,
            f"No pinned {tool} toolchain exists for this platform",
            tool=tool,
            cause=f"{tool} {pins[next(iter(pins))].version} is pinned only for "
            + ", ".join(sorted(pins)),
            fix="Run this analysis on a supported platform",
        )
    return pin


def parse_explicit_lock(text: str, *, platform_name: str) -> tuple[str, ...]:
    """Validate an ``@EXPLICIT`` lock; return its package lines in order.

    Every package line must be a pinned channel URL for this platform (or
    noarch) with a ``#sha256:`` digest.  Comments and blank lines are allowed
    only before ``@EXPLICIT``.
    """

    lines = text.splitlines()
    try:
        marker = lines.index("@EXPLICIT")
    except ValueError as exc:
        raise ValueError("lock has no @EXPLICIT line") from exc
    for line in lines[:marker]:
        if line.strip() and not line.startswith("#"):
            raise ValueError("lock has content before @EXPLICIT")
    packages = []
    for line in lines[marker + 1 :]:
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            raise ValueError("lock line is not a pinned channel URL with #sha256")
        if match["subdir"] not in {platform_name, "noarch"}:
            raise ValueError("lock line is for another platform")
        packages.append(line)
    if not packages:
        raise ValueError("lock lists no packages")
    if len(set(packages)) != len(packages):
        raise ValueError("lock lists a package twice")
    return tuple(packages)


def _read_lock(pin: ToolPin) -> bytes:
    """The committed lock bytes, checked against the pin before any use."""

    wrong = ToolProblem(
        TOOL_WRONG,
        f"The committed {pin.tool} lock file does not match its pin",
        tool=pin.tool,
        cause="The lock file shipped with traceback was edited or is damaged",
        fix="Reinstall traceback from a clean checkout",
    )
    try:
        data = pin.lock_path.read_bytes()
    except OSError as exc:
        raise wrong from exc
    if hashlib.sha256(data).hexdigest() != pin.lock_sha256:
        raise wrong
    try:
        packages = parse_explicit_lock(data.decode("utf-8"), platform_name=pin.platform)
    except (UnicodeError, ValueError) as exc:
        raise wrong from exc
    if pin.lock_line not in packages:
        raise wrong
    return data


def install_prefix(pin: ToolPin, cache_root: Path) -> Path:
    return cache_root / pin.lock_sha256


_MICROMAMBA_CANDIDATES = (
    Path("/opt/homebrew/bin/micromamba"),
    Path("/usr/local/bin/micromamba"),
    Path("/usr/bin/micromamba"),
)


def _executable(path: Path) -> bool:
    return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)


def resolve_micromamba(explicit: Path | None = None) -> Path:
    """micromamba by absolute path: ``--micromamba``, ``$MAMBA_EXE`` or a known
    install location.  ``PATH`` is never searched."""

    def missing(cause: str) -> ToolProblem:
        return ToolProblem(
            TOOL_MISSING,
            "micromamba was not found",
            tool="micromamba",
            cause=cause,
            fix="Install micromamba (for example `brew install micromamba`), or "
            "pass --micromamba with its absolute path",
        )

    if explicit is not None:
        if not _executable(explicit):
            raise missing("--micromamba must be the absolute path of an executable")
        return explicit
    candidates = []
    mamba_exe = os.environ.get("MAMBA_EXE")
    if mamba_exe:
        candidates.append(Path(mamba_exe))
    candidates += list(_MICROMAMBA_CANDIDATES)
    candidates.append(Path.home() / ".local" / "bin" / "micromamba")
    for candidate in candidates:
        if _executable(candidate):
            return candidate
    raise missing("No micromamba at $MAMBA_EXE or the standard install locations")


def plan_install(pin: ToolPin, *, cache_root: Path, micromamba: Path) -> InstallPlan:
    _read_lock(pin)
    prefix = install_prefix(pin, cache_root)
    return InstallPlan(
        tool=pin.tool,
        platform=pin.platform,
        argv=(
            str(micromamba),
            "create",
            "--yes",
            "--no-rc",
            "--prefix",
            str(prefix),
            "--file",
            str(pin.lock_path),
        ),
        prefix=prefix,
        lock_sha256=pin.lock_sha256,
    )


def _wrong(pin: ToolPin, cause: str) -> ToolProblem:
    return ToolProblem(
        TOOL_WRONG,
        f"{pin.tool} is installed but is not the pinned {pin.version} bytes",
        tool=pin.tool,
        cause=cause,
        fix=f"Run `traceback toolchain install {pin.tool} --yes` to reinstall it",
    )


def _missing(pin: ToolPin) -> ToolProblem:
    return ToolProblem(
        TOOL_MISSING,
        f"{pin.tool} {pin.version} is not installed",
        tool=pin.tool,
        cause=f"No complete {pin.tool} toolchain in the per-user cache "
        "(~/.cache/traceback/toolchains)",
        fix=f"Run `traceback toolchain install {pin.tool}` to see the plan, then "
        "add --yes (needs the network)",
    )


def _version_line(binary: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0].strip() if completed.returncode == 0 and lines else None


def _verify_package_record(pin: ToolPin, prefix: Path, installed_sha256: str) -> None:
    """The ``conda-meta`` record proves the binary came from the pinned package."""

    record_path = prefix / "conda-meta" / pin.package_record_name
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise _wrong(pin, "The package record of the install is missing") from exc
    if not isinstance(record, dict):
        raise _wrong(pin, "The package record of the install is malformed")
    if record.get("url") != pin.package_url or record.get("sha256") != pin.package_sha256:
        raise _wrong(pin, "The installed package is not the pinned package")
    paths = record.get("paths_data", {}).get("paths", [])
    entries = [
        entry
        for entry in paths
        if isinstance(entry, dict) and entry.get("_path") == pin.binary_relpath
    ]
    if len(entries) != 1:
        raise _wrong(pin, "The package record does not list the binary once")
    entry = entries[0]
    if entry.get("sha256") != pin.package_binary_sha256:
        raise _wrong(pin, "The package's binary digest differs from the pin")
    if entry.get("sha256_in_prefix", entry.get("sha256")) != installed_sha256:
        raise _wrong(pin, "The binary changed after it was installed")


def _check_binary(pin: ToolPin, prefix: Path, installed_sha256: str) -> Path:
    binary = prefix / pin.binary_relpath
    if not _executable(binary):
        raise _missing(pin)
    if sha256_file(binary) != installed_sha256:
        raise _wrong(pin, "The binary's sha256 differs from its install receipt")
    _verify_package_record(pin, prefix, installed_sha256)
    if _version_line(binary) != f"{pin.tool} {pin.version}":
        raise _wrong(pin, f"The binary does not report version {pin.version}")
    return binary


def resolve_tool(pin: ToolPin, *, cache_root: Path) -> PinnedTool:
    """The verified installed tool, or TBX-TOOL-001 (missing / wrong)."""

    _read_lock(pin)
    prefix = install_prefix(pin, cache_root)
    receipt_path = prefix / RECEIPT_NAME
    if not receipt_path.is_file():
        raise _missing(pin)
    try:
        receipt = ToolchainReceipt.model_validate_json(receipt_path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise _wrong(pin, "The install receipt is unreadable") from exc
    identity = receipt.identity
    expected = (
        pin.tool,
        pin.version,
        pin.platform,
        pin.lock_sha256,
        pin.lock_line,
        pin.package_sha256,
        pin.package_binary_sha256,
        pin.binary_relpath,
    )
    found = (
        identity.tool_id,
        identity.version,
        identity.platform,
        identity.lock_sha256,
        identity.lock_line,
        identity.package_sha256,
        identity.package_binary_sha256,
        receipt.binary_relpath,
    )
    if found != expected:
        raise _wrong(pin, "The install receipt names another version or package")
    binary = _check_binary(pin, prefix, identity.installed_binary_sha256)
    return PinnedTool(path=binary, identity=identity)


def resolve_modkit(
    *, cache_root: Path | None = None, platform_name: str | None = None
) -> PinnedTool:
    return resolve_tool(
        pin_for("modkit", platform_name),
        cache_root=cache_root if cache_root is not None else toolchain_cache_root(),
    )


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def install(
    pin: ToolPin,
    *,
    cache_root: Path,
    micromamba: Path,
    runner: Runner = subprocess.run,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> tuple[PinnedTool, bool]:
    """Install ``pin`` into the per-user cache; ``(tool, newly_installed)``.

    A complete install (valid receipt) is reused.  A prefix without a valid
    receipt is a partial or damaged install of this exact lock and is removed
    first.  The receipt is written last.
    """

    plan = plan_install(pin, cache_root=cache_root, micromamba=micromamba)
    try:
        return resolve_tool(pin, cache_root=cache_root), False
    except ToolProblem:
        pass
    prefix = plan.prefix
    if prefix.parent != cache_root or prefix.name != pin.lock_sha256:
        raise RuntimeError("install prefix escaped the toolchain cache")
    if prefix.is_symlink():
        prefix.unlink()
    elif prefix.exists():
        shutil.rmtree(prefix)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = cache_root / f"{pin.lock_sha256}.install.log"
    with log_path.open("wb") as log:
        try:
            completed = runner(
                list(plan.argv),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolProblem(
                TOOL_MISSING,
                f"Installing {pin.tool} did not finish",
                tool=pin.tool,
                cause="micromamba could not run or timed out",
                fix="Check the network and the install log next to the toolchain "
                "cache, then run the install again",
            ) from exc
    if completed.returncode != 0:
        raise ToolProblem(
            TOOL_MISSING,
            f"Installing {pin.tool} failed",
            tool=pin.tool,
            cause="micromamba exited non-zero (no network, or a package digest "
            "did not match the lock)",
            fix="Check the network and the install log next to the toolchain "
            "cache, then run the install again",
        )
    binary = prefix / pin.binary_relpath
    if not _executable(binary):
        raise _missing(pin)
    installed_sha256 = sha256_file(binary)
    _check_binary(pin, prefix, installed_sha256)
    identity = ToolIdentity(
        tool_id=pin.tool,
        version=pin.version,
        platform=pin.platform,
        lock_sha256=pin.lock_sha256,
        lock_line=pin.lock_line,
        package_sha256=pin.package_sha256,
        package_binary_sha256=pin.package_binary_sha256,
        installed_binary_sha256=installed_sha256,
    )
    receipt = ToolchainReceipt(identity=identity, binary_relpath=pin.binary_relpath)
    staged = prefix / f".{RECEIPT_NAME}.tmp"
    staged.write_bytes(receipt.model_dump_json().encode("utf-8"))
    os.replace(staged, prefix / RECEIPT_NAME)
    return resolve_tool(pin, cache_root=cache_root), True


def exec_pinned(
    tool: PinnedTool,
    arguments: Sequence[str],
    **popen_kwargs: Any,
) -> subprocess.Popen[Any]:
    """Start the tool by its absolute path after re-hashing the binary.

    The child leads its own process group so a caller can kill the whole
    group (:func:`kill_process_group`) on timeout, interrupt or a cap hit.
    """

    if not tool.path.is_absolute():
        raise ValueError("a pinned tool runs by absolute path only")
    try:
        digest = sha256_file(tool.path)
    except OSError as exc:
        raise ToolProblem(
            TOOL_MISSING,
            f"{tool.identity.tool_id} disappeared before it could run",
            tool=tool.identity.tool_id,
            cause="The installed binary is gone",
            fix=f"Run `traceback toolchain install {tool.identity.tool_id} --yes`",
        ) from exc
    if digest != tool.identity.installed_binary_sha256:
        raise ToolProblem(
            TOOL_WRONG,
            f"{tool.identity.tool_id} changed after it was verified",
            tool=tool.identity.tool_id,
            cause="The binary's sha256 differs from its install receipt",
            fix=f"Run `traceback toolchain install {tool.identity.tool_id} --yes`",
        )
    return subprocess.Popen(
        [str(tool.path), *arguments], start_new_session=True, **popen_kwargs
    )


def kill_process_group(process: subprocess.Popen[Any]) -> None:
    """Kill the child's whole process group and reap it."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


__all__ = [
    "MODKIT_PINS",
    "MODKIT_VERSION",
    "InstallPlan",
    "PinnedTool",
    "ToolIdentity",
    "ToolPin",
    "ToolProblem",
    "ToolchainReceipt",
    "current_platform",
    "exec_pinned",
    "install",
    "kill_process_group",
    "parse_explicit_lock",
    "pin_for",
    "plan_install",
    "resolve_micromamba",
    "resolve_modkit",
    "resolve_tool",
    "sha256_file",
    "toolchain_cache_root",
]
