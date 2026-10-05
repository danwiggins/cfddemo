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
class ToolchainProblem(ToolProblem):
    """TBX-TOOL-002: the ichorCNA (copy-number) toolchain is missing, or not
    the locked bytes.  Same ``reason`` split as TBX-TOOL-001."""

    def __init__(
        self,
        reason: ToolProblemReason,
        summary: str,
        *,
        tool: str,
        cause: str,
        fix: str,
    ) -> None:
        super().__init__(reason, summary, tool=tool, cause=cause, fix=fix)
        self.code = "TBX-TOOL-002"


@dataclass(frozen=True, slots=True)
class PackageBinary:
    """One executable inside one locked package, pinned at package level."""

    package_url: str
    package_sha256: str
    binary_relpath: str
    package_binary_sha256: str

    @property
    def lock_line(self) -> str:
        return f"{self.package_url}#sha256:{self.package_sha256}"

    @property
    def package_record_name(self) -> str:
        filename = self.package_url.rsplit("/", 1)[1]
        for suffix in (".conda", ".tar.bz2"):
            if filename.endswith(suffix):
                return filename[: -len(suffix)] + ".json"
        raise ValueError("package URL has no conda archive suffix")


@dataclass(frozen=True, slots=True)
class IchorPin:
    """What one platform's ichorCNA toolchain must be (CN1).

    Unlike modkit this is a whole R environment: R 4.4, bioconductor-hmmcopy,
    hmmcopy (readCounter) and r-ichorcna, plus a Traceback driver script.
    Everything that enters a method definition is package-level and stable
    across installs: the lock SHA-256 (it pins every package by SHA-256), the
    r-ichorcna lock line, the driver SHA-256, and the in-package digests of
    ``readCounter`` and ``Rscript``.  Installed digests (conda rewrites the
    prefix into binaries; macOS re-signs them) live in the receipt.
    """

    tool: str
    version: str
    platform: str
    lock_name: str
    lock_sha256: str
    ichorcna_package_url: str
    ichorcna_package_sha256: str
    readcounter: PackageBinary
    rscript: PackageBinary
    driver_sha256: str
    lock_directory: Path = LOCK_DIRECTORY

    @property
    def lock_line(self) -> str:
        return f"{self.ichorcna_package_url}#sha256:{self.ichorcna_package_sha256}"

    @property
    def lock_path(self) -> Path:
        return self.lock_directory / self.lock_name

    @property
    def driver_path(self) -> Path:
        return self.lock_directory / ICHOR_DRIVER_NAME


ICHOR_VERSION = "0.5.1"
ICHOR_DRIVER_NAME = "runIchorCNA.R"
# Where install puts the driver inside the environment.
ICHOR_DRIVER_RELPATH = f"share/traceback/{ICHOR_DRIVER_NAME}"
ICHOR_R_LIBRARY_RELPATH = "lib/R/library"
ICHOR_DRIVER_SHA256 = "bc61ce4dfd9dd3e41bbc00b4a2e46cb8fce37a338116915c0cd735f8271afc7b"
_ICHORCNA_URL = "https://conda.anaconda.org/bioconda/noarch/r-ichorcna-0.5.1-r44hdfd78af_1.conda"
_ICHORCNA_SHA256 = "d9583d94e1b44e7bff236e96803c1098f84a6acae30ea0691b224dd1fdd83980"
# Package-level binary digests are the paths_data `sha256` of each package
# (before conda rewrites the prefix), read from an install of each lock.
ICHOR_PINS: Mapping[str, IchorPin] = {
    "osx-arm64": IchorPin(
        tool="ichor",
        version=ICHOR_VERSION,
        platform="osx-arm64",
        lock_name="ichor-osx-arm64.lock",
        lock_sha256="bf507dcd34c66f29f982dd6902a5878b0ed716cf888e4d3a4a2eee9e31881a89",
        ichorcna_package_url=_ICHORCNA_URL,
        ichorcna_package_sha256=_ICHORCNA_SHA256,
        readcounter=PackageBinary(
            package_url="https://conda.anaconda.org/bioconda/osx-arm64/"
            "hmmcopy-0.1.1-hb4a815a_12.conda",
            package_sha256="a7a36d58f4cc5275024e1514fd51aeebbe33182e1de39dfdb19e569fd19057de",
            binary_relpath="bin/readCounter",
            package_binary_sha256=(
                "93e8f211bcce570470dcb95ee4990c58d31e573cb23f2e10205be706faaaf6fe"
            ),
        ),
        rscript=PackageBinary(
            package_url="https://conda.anaconda.org/conda-forge/osx-arm64/"
            "r-base-4.4.3-h35b0bb1_11.conda",
            package_sha256="5d50ea1dbf7b64725bd604da667cd10a274c1d891ac48b9156e7c99d5d2523d7",
            binary_relpath="bin/Rscript",
            package_binary_sha256=(
                "7664db15e94ea746ea688a3cb02f1fbc6a19177eea5f346509478b57bb542712"
            ),
        ),
        driver_sha256=ICHOR_DRIVER_SHA256,
    ),
    "linux-64": IchorPin(
        tool="ichor",
        version=ICHOR_VERSION,
        platform="linux-64",
        lock_name="ichor-linux-64.lock",
        lock_sha256="64c5e414149e9e807cb2dcf8ed65b001df27d15996ab32916a8ddf67d06e9f23",
        ichorcna_package_url=_ICHORCNA_URL,
        ichorcna_package_sha256=_ICHORCNA_SHA256,
        readcounter=PackageBinary(
            package_url="https://conda.anaconda.org/bioconda/linux-64/"
            "hmmcopy-0.1.1-h5b0a936_12.conda",
            package_sha256="ffdda1bf529f69a9af1fc09f6caf5a1c1358614ec5132e85e90e9bb72c6d7c43",
            binary_relpath="bin/readCounter",
            package_binary_sha256=(
                "30e5758de53ba329de069459d0315b73bbeab3eb7e2d86c5fb7509876e74b3d7"
            ),
        ),
        rscript=PackageBinary(
            package_url="https://conda.anaconda.org/conda-forge/linux-64/"
            "r-base-4.4.3-h502d0c9_11.conda",
            package_sha256="f2e18482ab87de3d29520e69b20a34f785d738cb0c007b3c4ef8cf8cbce7fb09",
            binary_relpath="bin/Rscript",
            package_binary_sha256=(
                "a921ecbf6db09cd869443a3caa5cbeec90a738ba800e25d03c8417bfcef6990e"
            ),
        ),
        driver_sha256=ICHOR_DRIVER_SHA256,
    ),
}
TOOL_PINS: Mapping[str, Mapping[str, ToolPin | IchorPin]] = {
    "modkit": MODKIT_PINS,
    "ichor": ICHOR_PINS,
}
# §11.20: `copy-number` is accepted wherever `ichor` is.
TOOL_ALIASES: Mapping[str, str] = {"copy-number": "ichor"}


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


def pin_for(tool: str, platform_name: str | None = None) -> ToolPin | IchorPin:
    tool = TOOL_ALIASES.get(tool, tool)
    pins = TOOL_PINS.get(tool)
    if pins is None:
        raise ValueError(f"unknown tool {tool!r}")
    name = platform_name if platform_name is not None else current_platform()
    pin = pins.get(name) if name is not None else None
    if pin is None:
        problem = ToolchainProblem if tool == "ichor" else ToolProblem
        raise problem(
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


# --------------------------------------------------------------------------
# ichorCNA (copy number, CN1): a whole locked R environment

ICHOR_PLATFORMS = ("osx-arm64", "linux-64")
ICHOR_CHANNELS = (
    "https://conda.anaconda.org/conda-forge",
    "https://conda.anaconda.org/bioconda",
)
ICHOR_SPECS = ("r-base=4.4", "r-ichorcna", "bioconductor-hmmcopy", "hmmcopy")
# R packages bioconda installs from Bioconductor tarballs in post-link scripts
# (md5-checked by the script, outside conda-meta).  A failed post-link does not
# fail micromamba, so install checks they exist and load.
ICHOR_POST_LINK_LIBRARIES = (
    "BSgenome.Hsapiens.UCSC.hg19",
    "BSgenome.Hsapiens.UCSC.hg38",
    "GenomeInfoDbData",
)
ICHOR_R_PACKAGES = ("ichorCNA", "HMMcopy", "GenomeInfoDb", *ICHOR_POST_LINK_LIBRARIES)
ICHOR_SMOKE_MARKER = "TRACEBACK_TOOLCHAIN_OK"
_ICHOR_DRIVER_LINE = re.compile(
    rf"^# driver: {re.escape(ICHOR_DRIVER_NAME)} sha256:(?P<sha256>[0-9a-f]{{64}})$", re.M
)
# conda-meta path types written at link time without a package digest.
_GENERATED_PATH_TYPES = frozenset({"pyc_file", "unix_python_entry_point", "directory"})
IchorCheck = Literal["shallow", "deep"]


def render_ichor_lock(
    platform_name: str, packages: Sequence[tuple[str, str]], driver_sha256: str
) -> str:
    lines = [
        "# Traceback optional toolchain: ichor (copy number). Do not edit by hand;",
        "# regenerate with scripts/lock_ichor_toolchain.py, then update ICHOR_PINS.",
        f"# platform: {platform_name}",
        f"# channels: {' '.join(ICHOR_CHANNELS)}",
        f"# specs: {' '.join(ICHOR_SPECS)}",
        f"# driver: {ICHOR_DRIVER_NAME} sha256:{driver_sha256}",
        "@EXPLICIT",
        *(f"{url}#sha256:{sha256}" for url, sha256 in packages),
    ]
    return "\n".join(lines) + "\n"


def _ichor_problem(reason: ToolProblemReason, summary: str, cause: str) -> ToolchainProblem:
    fix = (
        "Run `traceback toolchain install ichor` to see the plan, then add --yes "
        "(needs the network); an incomplete install is removed first"
        if reason == TOOL_MISSING
        else "Run `traceback toolchain install ichor --yes`, which replaces a damaged "
        "install; for an edited lock or driver, reinstall traceback from a clean checkout"
    )
    return ToolchainProblem(reason, summary, tool="ichor", cause=cause, fix=fix)


def _ichor_wrong(cause: str) -> ToolchainProblem:
    return _ichor_problem(
        TOOL_WRONG, "The ichorCNA toolchain does not match its lock", f"Wrong digest: {cause}"
    )


def _ichor_missing(cause: str) -> ToolchainProblem:
    return _ichor_problem(
        TOOL_MISSING, "The ichorCNA toolchain is missing or incomplete", f"Missing: {cause}"
    )


def _read_ichor_lock(pin: IchorPin) -> tuple[str, ...]:
    """The committed lock and driver, checked against the pin before any use."""

    try:
        _read_lock(pin)  # type: ignore[arg-type]  # same lock fields as ToolPin
        text = pin.lock_path.read_text(encoding="utf-8")
        driver = sha256_file(pin.driver_path)
    except ToolProblem as problem:
        raise _ichor_wrong(problem.cause) from problem
    except OSError as exc:
        raise _ichor_wrong("the committed lock or driver is unreadable") from exc
    packages = parse_explicit_lock(text, platform_name=pin.platform)
    declared = _ICHOR_DRIVER_LINE.findall(text)
    if declared != [pin.driver_sha256] or driver != pin.driver_sha256:
        raise _ichor_wrong("the committed driver differs from the one its lock pins")
    for item in (pin.readcounter, pin.rscript):
        if item.lock_line not in packages:
            raise _ichor_wrong("the lock does not list a pinned package")
    return packages


class IchorIdentity(_Strict):
    """Package-level identity: what enters a method definition's ``tools[]``."""

    schema_version: Literal["traceback.ichor-toolchain-identity.v1"] = (
        "traceback.ichor-toolchain-identity.v1"
    )
    tool_id: Literal["ichor"] = "ichor"
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    platform: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    lock_sha256: str = Field(pattern=_SHA256.pattern)
    lock_line: str = Field(min_length=1, max_length=512)
    driver_sha256: str = Field(pattern=_SHA256.pattern)
    readcounter_package_binary_sha256: str = Field(pattern=_SHA256.pattern)
    rscript_package_binary_sha256: str = Field(pattern=_SHA256.pattern)


class IchorInstalled(_Strict):
    """Per-install provenance: digests that depend on the install path."""

    readcounter_sha256: str = Field(pattern=_SHA256.pattern)
    rscript_sha256: str = Field(pattern=_SHA256.pattern)
    driver_sha256: str = Field(pattern=_SHA256.pattern)
    conda_meta_paths_data_sha256: str = Field(pattern=_SHA256.pattern)
    post_link_libraries: tuple[str, ...]
    post_link_libraries_sha256: str = Field(pattern=_SHA256.pattern)


class IchorReceipt(_Strict):
    """Written last by ``install_ichor``: its presence marks a complete install."""

    schema_version: Literal["traceback.ichor-toolchain-receipt.v1"] = (
        "traceback.ichor-toolchain-receipt.v1"
    )
    identity: IchorIdentity
    installed: IchorInstalled


@dataclass(frozen=True, slots=True)
class IchorToolchain:
    """A resolved, verified ichorCNA environment (CN3 runs it)."""

    prefix: Path
    identity: IchorIdentity
    installed: IchorInstalled

    @property
    def readcounter(self) -> Path:
        return self.prefix / "bin" / "readCounter"

    @property
    def rscript(self) -> Path:
        return self.prefix / "bin" / "Rscript"

    @property
    def driver(self) -> Path:
        return self.prefix / ICHOR_DRIVER_RELPATH

    @property
    def r_library(self) -> Path:
        return self.prefix / ICHOR_R_LIBRARY_RELPATH


def _expected_ichor_identity(pin: IchorPin) -> IchorIdentity:
    return IchorIdentity(
        version=pin.version,
        platform=pin.platform,
        lock_sha256=pin.lock_sha256,
        lock_line=pin.lock_line,
        driver_sha256=pin.driver_sha256,
        readcounter_package_binary_sha256=pin.readcounter.package_binary_sha256,
        rscript_package_binary_sha256=pin.rscript.package_binary_sha256,
    )


def _conda_meta(prefix: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted((prefix / "conda-meta").glob("*.json")):
        record = json.loads(entry.read_text(encoding="utf-8"))
        name = record.get("name") if isinstance(record, dict) else None
        if not isinstance(name, str) or name in records or not _well_formed(record):
            raise ValueError("conda-meta has a malformed or duplicate record")
        records[name] = record
    return records


def _well_formed(record: Mapping[str, Any]) -> bool:
    """The record fields verification reads have the shapes conda writes."""

    files = record.get("files", [])
    paths_data = record.get("paths_data", {"paths": []})
    paths = paths_data.get("paths") if isinstance(paths_data, dict) else None
    return (
        isinstance(files, list)
        and all(isinstance(item, str) for item in files)
        and isinstance(paths, list)
        and all(isinstance(item, dict) and isinstance(item.get("_path"), str) for item in paths)
    )


def conda_meta_paths_digest(records: Mapping[str, Mapping[str, Any]]) -> str:
    """SHA-256 over every package's identity and ``paths_data``.

    ``sha256_in_prefix`` (it depends on the install path) is excluded.
    """

    summary = []
    for name in sorted(records):
        record = records[name]
        paths = record.get("paths_data", {}).get("paths", [])
        summary.append(
            {
                "name": name,
                "version": record.get("version"),
                "build": record.get("build"),
                "url": record.get("url"),
                "sha256": record.get("sha256"),
                "paths": sorted(
                    [
                        str(item.get("_path")),
                        str(item.get("path_type")),
                        str(item.get("sha256")),
                        str(item.get("size_in_bytes")),
                    ]
                    for item in paths
                ),
            }
        )
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _untracked_r_libraries(prefix: Path, records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """R packages that no conda-meta record owns: the post-link installs."""

    owned = set()
    for record in records.values():
        for item in record.get("files", []):
            parts = str(item).split("/")
            if len(parts) > 4 and parts[:3] == ["lib", "R", "library"]:
                owned.add(parts[3])
    library = prefix / ICHOR_R_LIBRARY_RELPATH
    return sorted(
        entry.name
        for entry in library.iterdir()
        if entry.is_dir() and not entry.is_symlink() and entry.name not in owned
    )


def _tree_digest(base: Path, names: Sequence[str]) -> str:
    rows = []
    for name in names:
        for path in sorted((base / name).rglob("*")):
            relative = path.relative_to(base).as_posix()
            if path.is_symlink():
                rows.append([relative, "symlink", os.readlink(path)])
            elif path.is_file():
                rows.append([relative, "file", sha256_file(path)])
    encoded = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_package_binary(
    prefix: Path, records: Mapping[str, Mapping[str, Any]], item: PackageBinary, installed: str
) -> None:
    binary = prefix / item.binary_relpath
    if not _executable(binary):
        raise _ichor_missing(f"{item.binary_relpath} is gone")
    if sha256_file(binary) != installed:
        raise _ichor_wrong(f"{item.binary_relpath} differs from its install receipt")
    record = next(
        (value for value in records.values() if value.get("url") == item.package_url), None
    )
    if record is None or record.get("sha256") != item.package_sha256:
        raise _ichor_wrong(f"the package that owns {item.binary_relpath} is not the pinned one")
    entries = [
        entry
        for entry in record.get("paths_data", {}).get("paths", [])
        if isinstance(entry, dict) and entry.get("_path") == item.binary_relpath
    ]
    if len(entries) != 1 or entries[0].get("sha256") != item.package_binary_sha256:
        raise _ichor_wrong(f"the package's {item.binary_relpath} digest differs from the pin")
    if entries[0].get("sha256_in_prefix", entries[0].get("sha256")) != installed:
        raise _ichor_wrong(f"{item.binary_relpath} changed after it was installed")


def _check_installed_files(prefix: Path, records: Mapping[str, Mapping[str, Any]]) -> None:
    """Every file a package installed still has the bytes conda recorded."""

    for name in sorted(records):
        for entry in records[name].get("paths_data", {}).get("paths", []):
            if not isinstance(entry, dict) or entry.get("path_type") in _GENERATED_PATH_TYPES:
                continue
            relative = str(entry.get("_path", ""))
            path = prefix / relative
            if ".." in Path(relative).parts or not relative:
                raise _ichor_wrong(f"{name} records an unsafe path")
            expected = entry.get("sha256_in_prefix", entry.get("sha256"))
            if entry.get("path_type") == "softlink":
                # A recorded link must still be a link to the recorded bytes
                # (or, for a directory link, to a directory) inside the prefix.
                if not path.is_symlink():
                    raise _ichor_wrong(f"{relative} is no longer a link")
                target = path.resolve()
                if not target.is_relative_to(prefix.resolve()):
                    raise _ichor_wrong(f"{relative} now points outside the toolchain")
                if target.is_dir():
                    continue
            elif path.is_symlink() or not path.is_file():
                raise _ichor_wrong(f"{relative} from {name} is no longer a regular file")
            try:
                actual = sha256_file(path)
            except OSError as exc:
                raise _ichor_wrong(f"{relative} from {name} is gone") from exc
            if actual != expected:
                raise _ichor_wrong(f"{relative} from {name} changed since install")


def resolve_ichor(
    pin: IchorPin, *, cache_root: Path, check: IchorCheck = "shallow"
) -> IchorToolchain:
    """The verified ichorCNA environment, or TBX-TOOL-002 (missing / wrong).

    ``shallow`` (every run): the receipt names this pin; ``readCounter``,
    ``Rscript`` and the driver are re-hashed against it and against their
    package records.  ``deep`` (``doctor --deep``): also every installed file,
    the ``conda-meta`` digest, the package set against the lock, and the
    post-link Bioconductor data packages.
    """

    packages = _read_ichor_lock(pin)
    prefix = install_prefix(pin, cache_root)  # type: ignore[arg-type]
    receipt_path = prefix / RECEIPT_NAME
    if not receipt_path.is_file():
        raise _ichor_missing("no complete install in the per-user toolchain cache")
    try:
        receipt = IchorReceipt.model_validate_json(receipt_path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise _ichor_wrong("the install receipt is unreadable") from exc
    if receipt.identity != _expected_ichor_identity(pin):
        raise _ichor_wrong("the install receipt names another lock, driver or package")
    installed = receipt.installed
    try:
        records = _conda_meta(prefix)
    except (OSError, ValueError) as exc:
        raise _ichor_wrong("the package records are unreadable") from exc
    _check_package_binary(prefix, records, pin.readcounter, installed.readcounter_sha256)
    _check_package_binary(prefix, records, pin.rscript, installed.rscript_sha256)
    try:
        driver = sha256_file(prefix / ICHOR_DRIVER_RELPATH)
    except OSError as exc:
        raise _ichor_missing("the installed driver is gone") from exc
    if driver != pin.driver_sha256 or driver != installed.driver_sha256:
        raise _ichor_wrong("the installed driver differs from the pinned one")
    if check == "deep":
        locked_urls = {line.split("#sha256:", 1)[0] for line in packages}
        if {str(record.get("url")) for record in records.values()} != locked_urls:
            raise _ichor_wrong("the installed packages differ from the lock")
        if conda_meta_paths_digest(records) != installed.conda_meta_paths_data_sha256:
            raise _ichor_wrong("the conda-meta digest changed since install")
        _check_installed_files(prefix, records)
        try:
            untracked = _untracked_r_libraries(prefix, records)
            libraries = _tree_digest(prefix / ICHOR_R_LIBRARY_RELPATH, untracked)
        except OSError as exc:
            raise _ichor_wrong("the R library is unreadable") from exc
        if (
            tuple(untracked) != installed.post_link_libraries
            or libraries != installed.post_link_libraries_sha256
        ):
            raise _ichor_wrong("a Bioconductor data package changed since install")
    return IchorToolchain(prefix=prefix, identity=receipt.identity, installed=installed)


def micromamba_environment(cache_root: Path) -> dict[str, str]:
    """micromamba's environment, built from nothing but network settings."""

    env = {
        # bioconda's data-package post-link scripts need md5 (/sbin on macOS)
        # and curl; they fetch md5-pinned Bioconductor tarballs.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        # Post-link scripts run R CMD INSTALL: a private HOME and no user R
        # startup files keep the operator's R setup out of the environment.
        "HOME": str(cache_root / ".home"),
        "R_ENVIRON_USER": "/dev/null",
        "R_PROFILE_USER": "/dev/null",
        "R_LIBS_USER": "",
        "R_LIBS_SITE": "",
        "MAMBA_ROOT_PREFIX": str(cache_root / ".mamba-root"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    for key in (
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _ichor_smoke_check(prefix: Path) -> None:
    """Load every required R package through the isolated R runner."""

    from .r_isolation import RInvocation, RIsolationError, run_isolated_r

    work = prefix / ".traceback-smoke"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    script = work / "check.R"
    names = ", ".join(f'"{name}"' for name in ICHOR_R_PACKAGES)
    script.write_text(
        f"for (p in c({names})) suppressPackageStartupMessages("
        "library(p, character.only = TRUE))\n"
        f'cat("{ICHOR_SMOKE_MARKER}\\n")\n',
        encoding="utf-8",
    )
    try:
        result = run_isolated_r(
            RInvocation(
                rscript=prefix / "bin" / "Rscript",
                script=script,
                args=(),
                library_paths=(prefix / ICHOR_R_LIBRARY_RELPATH,),
                work_dir=work,
                timeout_seconds=600,
                log_limit_bytes=64 * 1024,
            )
        )
    except RIsolationError as exc:
        raise _ichor_missing(f"the installed R could not run ({exc})") from exc
    if not result.succeeded or ICHOR_SMOKE_MARKER.encode("ascii") not in result.process.stdout:
        (prefix.parent / f"{prefix.name}.smoke.log").write_bytes(result.process.stderr)
        raise _ichor_missing(
            "the environment cannot load ichorCNA (often a Bioconductor data package "
            "whose post-link download failed); see the smoke log next to the toolchain"
        )
    shutil.rmtree(work)


def install_ichor(
    pin: IchorPin,
    *,
    cache_root: Path,
    micromamba: Path,
    progress: Callable[[str], None] = lambda line: None,
    timeout: float = INSTALL_TIMEOUT_SECONDS * 2,
) -> tuple[IchorToolchain, bool]:
    """Install ``pin`` into the per-user cache; ``(toolchain, newly_installed)``.

    A complete install is reused.  A prefix without a valid receipt is removed
    first, and so is one whose receipt fails verification (a damaged install is
    replaced, as for modkit).  The receipt is written last.
    """

    from .contained_process import ContainedProcessError, run_contained

    plan = plan_install(pin, cache_root=cache_root, micromamba=micromamba)  # type: ignore[arg-type]
    _read_ichor_lock(pin)
    try:
        # Deep: a damage only --deep sees must be repaired here, not reused.
        return resolve_ichor(pin, cache_root=cache_root, check="deep"), False
    except ToolchainProblem:
        pass
    prefix = plan.prefix
    if prefix.parent != cache_root or prefix.name != pin.lock_sha256:
        raise RuntimeError("install prefix escaped the toolchain cache")
    if prefix.is_symlink():
        prefix.unlink()
    elif prefix.exists():
        progress("removing an incomplete earlier install")
        shutil.rmtree(prefix)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (cache_root / ".home").mkdir(exist_ok=True, mode=0o700)
    progress("installing the locked ichorCNA toolchain (network required; several minutes)")
    try:
        result = run_contained(
            plan.argv,
            env=micromamba_environment(cache_root),
            cwd=cache_root,
            timeout_seconds=timeout,
            log_limit_bytes=256 * 1024,
        )
    except ContainedProcessError as exc:
        raise _ichor_missing(f"micromamba could not run ({exc})") from exc
    (cache_root / f"{pin.lock_sha256}.install.log").write_bytes(
        result.stdout + b"\n--- stderr ---\n" + result.stderr
    )
    if not result.succeeded:
        raise _ichor_missing(
            f"micromamba {result.outcome}"
            + (f" with exit {result.returncode}" if result.returncode is not None else "")
            + " (no network, a timeout, or a package digest did not match the lock); "
            "see the install log next to the toolchain"
        )
    driver_target = prefix / ICHOR_DRIVER_RELPATH
    driver_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pin.driver_path, driver_target)
    _ichor_smoke_check(prefix)
    try:
        records = _conda_meta(prefix)
        untracked = _untracked_r_libraries(prefix, records)
    except (OSError, ValueError) as exc:
        raise _ichor_wrong(f"the package records are unreadable ({exc})") from exc
    absent = sorted(set(ICHOR_POST_LINK_LIBRARIES) - set(untracked))
    if absent:
        raise _ichor_missing("Bioconductor data packages did not install: " + ", ".join(absent))
    installed = IchorInstalled(
        readcounter_sha256=sha256_file(prefix / pin.readcounter.binary_relpath),
        rscript_sha256=sha256_file(prefix / pin.rscript.binary_relpath),
        driver_sha256=sha256_file(driver_target),
        conda_meta_paths_data_sha256=conda_meta_paths_digest(records),
        post_link_libraries=tuple(untracked),
        post_link_libraries_sha256=_tree_digest(prefix / ICHOR_R_LIBRARY_RELPATH, untracked),
    )
    receipt = IchorReceipt(identity=_expected_ichor_identity(pin), installed=installed)
    staged = prefix / f".{RECEIPT_NAME}.tmp"
    staged.write_bytes(receipt.model_dump_json().encode("utf-8"))
    os.replace(staged, prefix / RECEIPT_NAME)
    return resolve_ichor(pin, cache_root=cache_root, check="deep"), True


def resolve_copy_number_toolchain(
    *, cache_root: Path | None = None, platform_name: str | None = None
) -> IchorToolchain:
    """For CN3: the verified toolchain or TBX-TOOL-002 (missing is retryable)."""

    pin = pin_for("ichor", platform_name)
    assert isinstance(pin, IchorPin)
    return resolve_ichor(
        pin, cache_root=cache_root if cache_root is not None else toolchain_cache_root()
    )


def ichor_doctor_check(*, deep: bool = False, cache_root: Path | None = None) -> dict[str, Any]:
    """One doctor line: ready, or "not set up (optional); next: <command>".

    Never ``blocked``: an optional toolchain only refuses its own analysis.
    """

    label = "copy number (ichorCNA toolchain)"
    root = cache_root if cache_root is not None else toolchain_cache_root()
    check: dict[str, Any] = {"name": "copy_number_toolchain"}
    try:
        pin = pin_for("ichor")
    except ToolProblem:
        return check | {
            "status": "optional",
            "detail": f"{label}: not available on this platform "
            f"(locks exist for {', '.join(ICHOR_PLATFORMS)})",
        }
    assert isinstance(pin, IchorPin)
    prefix = install_prefix(pin, root)  # type: ignore[arg-type]
    if not prefix.exists() and not prefix.is_symlink():
        return check | {
            "status": "optional",
            "detail": f"{label}: not set up (optional); next: traceback toolchain install ichor",
        }
    try:
        resolve_ichor(pin, cache_root=root, check="deep" if deep else "shallow")
    except ToolProblem as problem:
        return check | {
            "status": "warn",
            "code": problem.code,
            "reason": problem.reason,
            "detail": f"{label}: {problem.cause}; next: {problem.fix}",
        }
    return check | {
        "status": "pass",
        "detail": f"{label}: ready (lock {pin.lock_sha256[:12]}); "
        + ("every installed file re-checked" if deep else "--deep re-checks every file"),
    }


__all__ = [
    "ICHOR_PINS",
    "ICHOR_POST_LINK_LIBRARIES",
    "IchorIdentity",
    "IchorInstalled",
    "IchorPin",
    "IchorReceipt",
    "IchorToolchain",
    "PackageBinary",
    "ToolchainProblem",
    "conda_meta_paths_digest",
    "ichor_doctor_check",
    "install_ichor",
    "render_ichor_lock",
    "resolve_copy_number_toolchain",
    "resolve_ichor",
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
