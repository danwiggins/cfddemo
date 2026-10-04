"""Optional analysis toolchains, installed per user from committed lock files.

``traceback toolchain install ichor`` (alias ``copy-number``) creates one
micromamba environment from ``toolchains/ichor-<platform>.lock``: an
``@EXPLICIT`` list of pinned conda-forge and bioconda package URLs, each with
its ``#sha256:`` digest, which micromamba enforces on download.

The environment lives at ``~/.cache/traceback/toolchains/<lock sha256>/env``
and is shared by every ROOT.  A new lock is a new directory, never an edit.
After a successful install, ``identity.json`` records what was installed: the
lock SHA-256, the SHA-256s of ``runIchorCNA.R``, ``readCounter`` and
``Rscript``, and a digest over every package's ``conda-meta`` ``paths_data``.
It is written last, so a directory without it is an incomplete install.

The toolchain is optional.  Without it the fragment and cell-origin paths are
unaffected; ``doctor`` reports it as "not set up (optional)".
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform as host
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .contained_process import ContainedProcessError, run_contained
from .references import ReferenceProblem
from .serialization import canonical_json_bytes

IDENTITY_SCHEMA = "traceback.toolchain-identity.v1"
SUPPORTED_PLATFORMS = ("osx-arm64", "linux-64")
ICHOR_CHANNELS = (
    "https://conda.anaconda.org/conda-forge",
    "https://conda.anaconda.org/bioconda",
)
ICHOR_SPECS = ("r-base=4.4", "r-ichorcna", "bioconductor-hmmcopy", "hmmcopy")
# Package -> required version prefix ("" = any locked version).
ICHOR_REQUIRED_PACKAGES = {
    "r-base": "4.4.",
    "r-ichorcna": "",
    "bioconductor-hmmcopy": "",
    "hmmcopy": "",
}
# R packages that bioconda installs from Bioconductor tarballs in post-link
# scripts (md5-checked, outside conda-meta).  A post-link failure does not fail
# micromamba, so the install checks they are present and loadable.
ICHOR_POST_LINK_LIBRARIES = (
    "BSgenome.Hsapiens.UCSC.hg19",
    "BSgenome.Hsapiens.UCSC.hg38",
    "GenomeInfoDbData",
)
# Loaded by the post-install smoke check through the isolated R runner.
ICHOR_R_PACKAGES = ("ichorCNA", "HMMcopy", "GenomeInfoDb", *ICHOR_POST_LINK_LIBRARIES)
TOOLCHAIN_ALIASES = {"ichor": "ichor", "copy-number": "ichor"}
LOCK_DIRECTORY = Path("traceback_runner") / "toolchains"
# Upstream ichorCNA v0.5.1 ships no runIchorCNA.R; this Traceback driver calls
# ichorCNA::run_ichorCNA() with the adapter's flags.  Its SHA-256 is declared
# in every lock, so a driver change is a new lock.
DRIVER_NAME = "runIchorCNA.R"
HOME_ENVIRONMENT = "TRACEBACK_TOOLCHAIN_HOME"
MICROMAMBA_ENVIRONMENT = "TRACEBACK_MICROMAMBA"
MICROMAMBA_CANDIDATES = (
    "/opt/homebrew/bin/micromamba",
    "/usr/local/bin/micromamba",
    "~/.local/bin/micromamba",
    "~/bin/micromamba",
)
INSTALL_TIMEOUT_SECONDS = 3600
_MAX_LOCK_BYTES = 256 * 1024
_MAX_META_BYTES = 64 * 1024 * 1024
_MAX_IDENTITY_BYTES = 64 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_FILE = r"[A-Za-z0-9_][A-Za-z0-9._+-]*\.(?:conda|tar\.bz2)"
_FORWARDED_NETWORK_VARIABLES = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)

Platform = Literal["osx-arm64", "linux-64"]
State = Literal[
    "ready", "not_set_up", "missing", "wrong_digest", "unsupported_platform"
]


class ToolchainLockError(ValueError):
    """A committed lock file is not an acceptable pinned ``@EXPLICIT`` list."""


@dataclass(frozen=True)
class LockedPackage:
    url: str
    sha256: str
    name: str
    version: str
    build: str


@dataclass(frozen=True)
class ToolchainLock:
    toolchain: str
    platform: str
    sha256: str
    content: bytes
    packages: tuple[LockedPackage, ...]
    driver_sha256: str

    @property
    def file_name(self) -> str:
        return f"{self.toolchain}-{self.platform}.lock"

    def package(self, name: str) -> LockedPackage:
        return next(item for item in self.packages if item.name == name)


def _url_pattern(platform: str) -> re.Pattern[str]:
    channels = "|".join(re.escape(item) for item in ICHOR_CHANNELS)
    return re.compile(
        rf"^(?P<url>(?:{channels})/(?:noarch|{re.escape(platform)})/"
        rf"(?P<file>{_PACKAGE_FILE}))#sha256:(?P<sha>[0-9a-f]{{64}})$"
    )


def _split_file_name(file_name: str) -> tuple[str, str, str]:
    stem = re.sub(r"\.(conda|tar\.bz2)$", "", file_name)
    parts = stem.rsplit("-", 2)
    if len(parts) != 3 or not all(parts):
        raise ToolchainLockError(f"package file name does not parse: {file_name}")
    return parts[0], parts[1], parts[2]


def parse_lock(content: bytes, platform: str, toolchain: str = "ichor") -> ToolchainLock:
    """Accept only a pinned ``@EXPLICIT`` list for ``platform``; refuse the rest."""

    if platform not in SUPPORTED_PLATFORMS:
        raise ToolchainLockError(f"unsupported platform {platform}")
    if len(content) > _MAX_LOCK_BYTES:
        raise ToolchainLockError("lock file is too large")
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ToolchainLockError("lock file is not ASCII") from exc
    pattern = _url_pattern(platform)
    explicit = False
    declared_platform = None
    declared_driver = None
    packages: list[LockedPackage] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("# platform: "):
                declared_platform = line.removeprefix("# platform: ").strip()
            elif line.startswith(f"# driver: {DRIVER_NAME} sha256:"):
                if declared_driver is not None:
                    raise ToolchainLockError("the driver is declared twice")
                declared_driver = line.rsplit("sha256:", 1)[1].strip()
            continue
        if line == "@EXPLICIT":
            if explicit or packages:
                raise ToolchainLockError("@EXPLICIT must appear once, before packages")
            explicit = True
            continue
        if not explicit:
            raise ToolchainLockError(f"line {number}: package before @EXPLICIT")
        match = pattern.match(line)
        if match is None:
            raise ToolchainLockError(
                f"line {number}: not a pinned channel URL with a #sha256: digest"
            )
        name, version, build = _split_file_name(match["file"])
        packages.append(LockedPackage(match["url"], match["sha"], name, version, build))
    if not explicit:
        raise ToolchainLockError("lock file has no @EXPLICIT line")
    if declared_platform != platform:
        raise ToolchainLockError("lock file does not declare this platform")
    if not packages:
        raise ToolchainLockError("lock file lists no packages")
    if declared_driver is None or not _HEX64.match(declared_driver):
        raise ToolchainLockError(f"lock file does not pin {DRIVER_NAME} by SHA-256")
    names = [item.name for item in packages]
    if len(names) != len(set(names)):
        raise ToolchainLockError("a package is listed twice")
    for name, version_prefix in ICHOR_REQUIRED_PACKAGES.items():
        found = [item for item in packages if item.name == name]
        if not found:
            raise ToolchainLockError(f"required package {name} is not locked")
        if not found[0].version.startswith(version_prefix):
            raise ToolchainLockError(f"{name} is not at version {version_prefix}x")
    return ToolchainLock(
        toolchain=toolchain,
        platform=platform,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        packages=tuple(packages),
        driver_sha256=declared_driver,
    )


def render_lock(
    platform: str, packages: Iterable[tuple[str, str]], driver_sha256: str
) -> str:
    lines = [
        "# Traceback optional toolchain: ichor (copy number). Do not edit by hand;",
        "# regenerate with scripts/lock_ichor_toolchain.py. This file's SHA-256",
        "# names the install directory and enters the method's tools[].",
        f"# platform: {platform}",
        f"# channels: {' '.join(ICHOR_CHANNELS)}",
        f"# specs: {' '.join(ICHOR_SPECS)}",
        f"# driver: {DRIVER_NAME} sha256:{driver_sha256}",
        "@EXPLICIT",
    ]
    lines += [f"{url}#sha256:{sha}" for url, sha in packages]
    return "\n".join(lines) + "\n"


def canonical_toolchain(name: str) -> str:
    try:
        return TOOLCHAIN_ALIASES[name]
    except KeyError:
        raise ValueError(f"unknown toolchain {name}") from None


def host_platform() -> str | None:
    system, machine = host.system(), host.machine().lower()
    if system == "Darwin" and machine == "arm64":
        return "osx-arm64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-64"
    return None


def lock_file(toolchain: str, platform: str) -> Path:
    return Path(__file__).resolve().parent / "toolchains" / f"{toolchain}-{platform}.lock"


def driver_file() -> Path:
    return Path(__file__).resolve().parent / "toolchains" / DRIVER_NAME


def driver_sha256() -> str:
    return hashlib.sha256(driver_file().read_bytes()).hexdigest()


def load_lock(toolchain: str, platform: str) -> ToolchainLock:
    """The committed lock, refused unless it pins the packaged driver's bytes."""

    path = lock_file(toolchain, platform)
    with path.open("rb") as handle:
        content = handle.read(_MAX_LOCK_BYTES + 1)
    lock = parse_lock(content, platform, toolchain)
    if lock.driver_sha256 != driver_sha256():
        raise ToolchainLockError(
            f"{DRIVER_NAME} differs from the SHA-256 its lock declares; regenerate the lock"
        )
    return lock


def toolchain_home() -> Path:
    override = os.environ.get(HOME_ENVIRONMENT)
    if override:
        if not os.path.isabs(override):
            raise ValueError(f"{HOME_ENVIRONMENT} must be an absolute path")
        return Path(os.path.normpath(override))
    return Path.home() / ".cache" / "traceback" / "toolchains"


def install_directory(lock: ToolchainLock, home: Path | None = None) -> Path:
    return (home or toolchain_home()) / lock.sha256


# --------------------------------------------------------------------------
# Environment identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} is too large")
    return data


def _conda_meta(prefix: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for entry in sorted((prefix / "conda-meta").glob("*.json")):
        record = json.loads(_read_bounded(entry, _MAX_META_BYTES))
        name = record.get("name")
        if not isinstance(name, str) or name in records:
            raise ValueError("conda-meta has a malformed or duplicate record")
        records[name] = record
    return records


def conda_meta_paths_digest(records: Mapping[str, Mapping[str, Any]]) -> str:
    """SHA-256 over every package's identity and ``paths_data``.

    Fields that depend on the install prefix (``sha256_in_prefix``) are
    excluded, so one lock gives one digest on every machine of a platform.
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
                        item.get("_path"),
                        item.get("path_type"),
                        item.get("sha256"),
                        item.get("size_in_bytes"),
                    ]
                    for item in paths
                ),
            }
        )
    return hashlib.sha256(canonical_json_bytes(summary)).hexdigest()


def _owned_path(record: Mapping[str, Any], relative: str) -> str:
    if record.get("files", []).count(relative) != 1:
        raise ValueError(f"{record.get('name')} does not own {relative}")
    return relative


def compute_identity(lock: ToolchainLock, directory: Path) -> dict[str, Any]:
    """Describe the installed environment; raise ``ValueError`` if it is not the lock's."""

    prefix = directory / "env"
    records = _conda_meta(prefix)
    locked = {item.name: item for item in lock.packages}
    if set(records) != set(locked):
        raise ValueError("installed packages differ from the lock")
    for name, item in locked.items():
        record = records[name]
        if record.get("url") != item.url or (
            record.get("sha256") not in {None, item.sha256}
        ):
            raise ValueError(f"installed {name} is not the locked package")
    files = {
        "readcounter": f"env/{_owned_path(records['hmmcopy'], 'bin/readCounter')}",
        "rscript": f"env/{_owned_path(records['r-base'], 'bin/Rscript')}",
        "runichorcna_r": f"scripts/{DRIVER_NAME}",
    }
    tools = {
        key: {"relative_path": relative, "sha256": _sha256_file(directory / relative)}
        for key, relative in files.items()
    }
    if tools["runichorcna_r"]["sha256"] != lock.driver_sha256:
        raise ValueError(f"the installed {DRIVER_NAME} is not the locked driver")
    untracked = _untracked_r_libraries(prefix, records)
    absent = sorted(set(ICHOR_POST_LINK_LIBRARIES) - set(untracked))
    if absent:
        raise ValueError(
            "a Bioconductor data package did not install (post-link): " + ", ".join(absent)
        )
    return {
        "schema_version": IDENTITY_SCHEMA,
        "toolchain": lock.toolchain,
        "platform": lock.platform,
        "lock_sha256": lock.sha256,
        "packages": {
            name: f"{locked[name].version}-{locked[name].build}"
            for name in sorted(ICHOR_REQUIRED_PACKAGES)
        },
        "tools": tools,
        "r_library": "env/lib/R/library",
        "conda_meta_paths_data_sha256": conda_meta_paths_digest(records),
        "post_link_libraries": sorted(untracked),
        "post_link_libraries_sha256": _tree_digest(
            prefix / "lib" / "R" / "library", sorted(untracked)
        ),
    }


def _untracked_r_libraries(
    prefix: Path, records: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """R packages that no conda-meta record owns: post-link installs."""

    owned = set()
    for record in records.values():
        for item in record.get("files", []):
            parts = item.split("/")
            if len(parts) > 4 and parts[:3] == ["lib", "R", "library"]:
                owned.add(parts[3])
    library = prefix / "lib" / "R" / "library"
    return sorted(
        entry.name
        for entry in library.iterdir()
        if entry.is_dir() and not entry.is_symlink() and entry.name not in owned
    )


def _tree_digest(base: Path, names: Iterable[str]) -> str:
    """SHA-256 over (relative path, kind, content digest) of every entry."""

    rows = []
    for name in names:
        root = base / name
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(base).as_posix()
            if path.is_symlink():
                rows.append([relative, "symlink", os.readlink(path)])
            elif path.is_file():
                rows.append([relative, "file", _sha256_file(path)])
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


@dataclass(frozen=True)
class ToolchainStatus:
    toolchain: str
    state: State
    detail: str
    platform: str | None
    lock_sha256: str | None = None
    directory: Path | None = None
    identity: Mapping[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def _load_identity(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_read_bounded(directory / "identity.json", _MAX_IDENTITY_BYTES))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_toolchain(
    toolchain: str = "ichor", *, deep: bool = False, home: Path | None = None
) -> ToolchainStatus:
    """Classify the installed toolchain without changing anything.

    Shallow: the lock copy and the three tool files are re-hashed against
    ``identity.json``.  ``deep`` also recomputes the ``conda-meta`` digest and
    checks every installed package against the lock.
    """

    name = canonical_toolchain(toolchain)
    platform = host_platform()
    if platform is None:
        return ToolchainStatus(
            name,
            "unsupported_platform",
            f"not available on this platform (locks exist for {', '.join(SUPPORTED_PLATFORMS)})",
            None,
        )
    lock = load_lock(name, platform)
    directory = install_directory(lock, home)

    def status(state: State, detail: str, identity: Mapping[str, Any] | None = None):
        return ToolchainStatus(name, state, detail, platform, lock.sha256, directory, identity)

    if not directory.exists() and not directory.is_symlink():
        return status("not_set_up", "not set up (optional)")
    if directory.is_symlink() or not directory.is_dir():
        return status("wrong_digest", "the install directory is not a directory")
    identity = _load_identity(directory)
    if identity is None:
        return status("missing", "the install is incomplete (no identity record)")
    if identity.get("schema_version") != IDENTITY_SCHEMA or identity.get(
        "lock_sha256"
    ) != lock.sha256:
        return status("wrong_digest", "the identity record does not name this lock")
    try:
        if _sha256_file(directory / lock.file_name) != lock.sha256:
            return status("wrong_digest", "the installed lock copy differs from the lock")
    except OSError:
        return status("missing", "the installed lock copy is missing")
    tools = identity.get("tools")
    if not isinstance(tools, dict) or set(tools) != {"runichorcna_r", "readcounter", "rscript"}:
        return status("wrong_digest", "the identity record is malformed")
    for key, entry in sorted(tools.items()):
        relative = entry.get("relative_path") if isinstance(entry, dict) else None
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        if (
            not isinstance(relative, str)
            or not relative.startswith(("env/", "scripts/"))
            or ".." in Path(relative).parts
            or not isinstance(expected, str)
            or not _HEX64.match(expected)
        ):
            return status("wrong_digest", "the identity record is malformed")
        try:
            actual = _sha256_file(directory / relative)
        except OSError:
            return status("missing", f"{Path(relative).name} is missing")
        if actual != expected:
            return status("wrong_digest", f"{Path(relative).name} changed since install")
    if deep:
        try:
            recomputed = compute_identity(lock, directory)
        except (OSError, ValueError) as exc:
            return status("wrong_digest", f"the environment does not match the lock ({exc})")
        if recomputed != identity:
            return status("wrong_digest", "the environment's conda-meta digest changed")
    return status("ready", f"ready (lock {lock.sha256[:12]})", identity)


def install_command_hint(toolchain: str = "ichor", *, yes: bool = False) -> str:
    return f"traceback toolchain install {toolchain}" + (" --yes" if yes else "")


def toolchain_problem(status: ToolchainStatus, *, retryable: bool = False) -> ReferenceProblem:
    """TBX-TOOL-002, split into "missing" and "wrong digest"."""

    if status.state in {"missing", "not_set_up"}:
        return ReferenceProblem(
            "TBX-TOOL-002",
            "The ichorCNA toolchain is missing or incomplete",
            cause=f"missing: {status.detail}",
            fix=f"Run {install_command_hint(status.toolchain, yes=True)} (needs the network)",
            exit_code=6 if retryable else 3,
            retryable=retryable,
        )
    if status.state == "wrong_digest":
        return ReferenceProblem(
            "TBX-TOOL-002",
            "The ichorCNA toolchain does not match its lock",
            cause=f"wrong digest: {status.detail}",
            fix=(
                "Delete the toolchain directory that doctor prints, then run "
                f"{install_command_hint(status.toolchain, yes=True)}"
            ),
            exit_code=3,
            retryable=False,
        )
    raise ValueError(f"no problem for state {status.state}")


def require_toolchain(toolchain: str = "ichor", *, retryable: bool = False) -> ToolchainStatus:
    """The ready status, or TBX-TOOL-002 (CN3 calls this before staging)."""

    status = check_toolchain(toolchain, deep=True)
    if status.state == "unsupported_platform":
        raise ReferenceProblem(
            "TBX-TOOL-002",
            "The ichorCNA toolchain is not available on this platform",
            cause=f"missing: {status.detail}",
            fix="Run copy number on macOS arm64 or Linux x86-64",
        )
    if not status.ready:
        raise toolchain_problem(status, retryable=retryable)
    return status


# --------------------------------------------------------------------------
# Install


def resolve_micromamba(explicit: Path | None = None) -> Path | None:
    """An absolute micromamba path; never a ``PATH`` search."""

    if explicit is not None:
        return explicit if explicit.is_absolute() else None
    configured = os.environ.get(MICROMAMBA_ENVIRONMENT)
    if configured:
        return Path(configured) if os.path.isabs(configured) else None
    for candidate in MICROMAMBA_CANDIDATES:
        path = Path(os.path.expanduser(candidate))
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def micromamba_argv(micromamba: Path, directory: Path, lock: ToolchainLock) -> tuple[str, ...]:
    return (
        os.fspath(micromamba),
        "create",
        "--yes",
        "--no-rc",
        "--no-env",
        "--prefix",
        os.fspath(directory / "env"),
        "--file",
        os.fspath(directory / lock.file_name),
    )


def micromamba_environment(home: Path) -> dict[str, str]:
    env = {
        # bioconda's data-package post-link scripts need md5 (/sbin on macOS)
        # and curl; they fetch md5-pinned Bioconductor tarballs.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ.get("HOME", os.fspath(Path.home())),
        "MAMBA_ROOT_PREFIX": os.fspath(home / ".mamba-root"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    env.update(
        {key: os.environ[key] for key in _FORWARDED_NETWORK_VARIABLES if key in os.environ}
    )
    return env


@dataclass(frozen=True)
class InstallPlan:
    toolchain: str
    platform: str
    lock: ToolchainLock
    directory: Path
    micromamba: Path | None
    argv: tuple[str, ...] | None

    def describe(self) -> dict[str, Any]:
        return {
            "toolchain": self.toolchain,
            "platform": self.platform,
            "lock": f"{LOCK_DIRECTORY.as_posix()}/{self.lock.file_name}",
            "lock_sha256": self.lock.sha256,
            "packages": len(self.lock.packages),
            "key_packages": {
                name: self.lock.package(name).version for name in sorted(ICHOR_REQUIRED_PACKAGES)
            },
            "install_directory": os.fspath(self.directory),
            "micromamba": os.fspath(self.micromamba) if self.micromamba else "not found",
            "command": " ".join(self.argv) if self.argv else None,
            "network": (
                "required: downloads every locked package from conda.anaconda.org "
                "(conda-forge, bioconda); each download is checked against its SHA-256"
            ),
        }


def plan_install(toolchain: str, *, micromamba: Path | None = None) -> InstallPlan:
    name = canonical_toolchain(toolchain)
    platform = host_platform()
    if platform is None:
        raise ReferenceProblem(
            "TBX-TOOL-003",
            "No ichorCNA lock exists for this platform; nothing was installed",
            cause=f"locks exist for {', '.join(SUPPORTED_PLATFORMS)} only",
            fix="Run copy number on macOS arm64 or Linux x86-64",
        )
    lock = load_lock(name, platform)
    directory = install_directory(lock)
    resolved = resolve_micromamba(micromamba)
    argv = micromamba_argv(resolved, directory, lock) if resolved else None
    return InstallPlan(name, platform, lock, directory, resolved, argv)


def _write_new(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_incomplete(directory: Path, home: Path) -> None:
    if directory.parent != home or directory.is_symlink() or not directory.is_dir():
        raise ReferenceProblem(
            "TBX-TOOL-002",
            "The ichorCNA toolchain directory is not one this command created",
            cause=f"wrong digest: {directory.name} is not a plain directory in the cache",
            fix="Delete it by hand, then install again",
        )
    shutil.rmtree(directory)


SMOKE_MARKER = "TRACEBACK_TOOLCHAIN_OK"


def _smoke_check(plan: InstallPlan) -> None:
    """Load every required R package through the isolated runner.

    micromamba exits 0 even when a bioconda post-link script failed, so this
    is what proves the environment can run ichorCNA.
    """

    from .r_isolation import RInvocation, RIsolationError, run_isolated_r

    work = plan.directory / ".smoke"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    script = work / "check.R"
    packages = ", ".join(f'"{name}"' for name in ICHOR_R_PACKAGES)
    script.write_text(
        f"for (p in c({packages})) suppressPackageStartupMessages(library(p, character.only = TRUE))\n"
        f'cat("{SMOKE_MARKER}\\n")\n',
        encoding="utf-8",
    )
    prefix = plan.directory / "env"
    try:
        result = run_isolated_r(
            RInvocation(
                rscript=prefix / "bin" / "Rscript",
                script=script,
                args=(),
                library_paths=(prefix / "lib" / "R" / "library",),
                work_dir=work,
                timeout_seconds=600,
                log_limit_bytes=64 * 1024,
            )
        )
    except RIsolationError as exc:
        raise ReferenceProblem(
            "TBX-TOOL-003",
            "The installed environment could not run R; the install is incomplete",
            cause=str(exc),
            fix=f"Run {install_command_hint(plan.toolchain, yes=True)} again",
        ) from exc
    if not result.succeeded or SMOKE_MARKER.encode("ascii") not in result.process.stdout:
        _write_new(plan.directory / "smoke.log", result.process.stderr)
        raise ReferenceProblem(
            "TBX-TOOL-003",
            "The installed environment cannot load ichorCNA; the install is incomplete",
            cause=(
                "an R package failed to load (often a Bioconductor data package whose "
                f"post-link download failed); see {plan.directory / 'smoke.log'}"
            ),
            fix=f"Check the network, then run {install_command_hint(plan.toolchain, yes=True)}",
            exit_code=6,
            retryable=True,
        )
    shutil.rmtree(work)


def install_toolchain(
    plan: InstallPlan,
    *,
    progress: Callable[[str], None] = lambda line: None,
    timeout_seconds: float = INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Install ``plan`` (``--yes`` was given); return the identity record."""

    if plan.micromamba is None or plan.argv is None:
        raise ReferenceProblem(
            "TBX-TOOL-003",
            "micromamba was not found; nothing was installed",
            cause="no micromamba at --micromamba, $TRACEBACK_MICROMAMBA or a standard location",
            fix="Install micromamba (brew install micromamba), or pass --micromamba /ABS/PATH",
        )
    home = plan.directory.parent
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(home / ".install.lock", "a+b") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReferenceProblem(
                "TBX-TOOL-003",
                "Another toolchain install is running; nothing was changed",
                cause="the per-user toolchain install lock is held",
                fix="Wait for the other install to finish, then run this again",
                exit_code=6,
                retryable=True,
            ) from None
        status = check_toolchain(plan.toolchain, home=home)
        if status.ready:
            return dict(status.identity or {})
        if status.state == "wrong_digest":
            raise toolchain_problem(status)
        if status.state == "missing":
            progress("removing an incomplete earlier install")
            _remove_incomplete(plan.directory, home)
        plan.directory.mkdir(mode=0o755)
        _write_new(plan.directory / plan.lock.file_name, plan.lock.content)
        (plan.directory / "scripts").mkdir(mode=0o755)
        _write_new(plan.directory / "scripts" / DRIVER_NAME, driver_file().read_bytes())
        progress(
            f"installing {len(plan.lock.packages)} locked packages "
            f"(network required); this can take several minutes"
        )
        try:
            result = run_contained(
                plan.argv,
                env=micromamba_environment(home),
                cwd=plan.directory,
                timeout_seconds=timeout_seconds,
                log_limit_bytes=256 * 1024,
            )
        except ContainedProcessError as exc:
            raise ReferenceProblem(
                "TBX-TOOL-003",
                "micromamba could not be started; the install is incomplete",
                cause=str(exc),
                fix="Check the micromamba path, then run this again",
            ) from exc
        _write_new(
            plan.directory / "install.log",
            result.stdout + b"\n--- stderr ---\n" + result.stderr,
        )
        if not result.succeeded:
            raise ReferenceProblem(
                "TBX-TOOL-003",
                "micromamba did not finish; the install is incomplete",
                cause=(
                    f"micromamba {result.outcome}"
                    + (f" with exit {result.returncode}" if result.returncode is not None else "")
                    + "; a download failed, was refused by its SHA-256, or timed out "
                    f"(see {plan.directory / 'install.log'})"
                ),
                fix=f"Check the network, then run {install_command_hint(plan.toolchain, yes=True)}",
                exit_code=6,
                retryable=True,
            )
        _smoke_check(plan)
        try:
            identity = compute_identity(plan.lock, plan.directory)
        except (OSError, ValueError) as exc:
            raise ReferenceProblem(
                "TBX-TOOL-002",
                "The installed environment does not match the lock",
                cause=f"wrong digest: {exc}",
                fix=f"Delete {plan.directory}, then run "
                f"{install_command_hint(plan.toolchain, yes=True)}",
            ) from exc
        _write_new(plan.directory / "identity.json", canonical_json_bytes(identity) + b"\n")
        final = check_toolchain(plan.toolchain, deep=True, home=home)
        if not final.ready:
            raise toolchain_problem(final)
        return identity


def doctor_line(*, deep: bool = False) -> dict[str, Any]:
    """One doctor check, "not set up (optional); next: <command>" style.

    Never ``blocked``: an optional toolchain only refuses its own analysis.
    """

    try:
        status = check_toolchain("ichor", deep=deep)
    except (OSError, ValueError) as exc:
        return {
            "name": "copy_number_toolchain",
            "status": "warn",
            "detail": f"copy number (ichorCNA toolchain): the lock could not be read ({exc})",
        }
    label = "copy number (ichorCNA toolchain)"
    check: dict[str, Any] = {"name": "copy_number_toolchain"}
    if status.state == "ready":
        check |= {
            "status": "pass",
            "detail": f"{label}: {status.detail}"
            + ("; environment digest re-checked" if deep else "; --deep re-checks the environment"),
        }
    elif status.state == "not_set_up":
        check |= {
            "status": "optional",
            "detail": f"{label}: not set up (optional); next: {install_command_hint()}",
        }
    elif status.state == "unsupported_platform":
        check |= {"status": "optional", "detail": f"{label}: {status.detail}"}
    else:
        problem = toolchain_problem(status)
        check |= {
            "status": "warn",
            "code": problem.code,
            "detail": f"{label}: {problem.cause}; next: {problem.fix}",
        }
    if status.directory is not None and status.state != "not_set_up":
        check["directory"] = os.fspath(status.directory)
    return check


__all__ = [
    "ICHOR_CHANNELS",
    "ICHOR_REQUIRED_PACKAGES",
    "ICHOR_SPECS",
    "IDENTITY_SCHEMA",
    "LOCK_DIRECTORY",
    "SUPPORTED_PLATFORMS",
    "InstallPlan",
    "LockedPackage",
    "ToolchainLock",
    "ToolchainLockError",
    "ToolchainStatus",
    "check_toolchain",
    "compute_identity",
    "conda_meta_paths_digest",
    "doctor_line",
    "host_platform",
    "install_toolchain",
    "load_lock",
    "parse_lock",
    "plan_install",
    "render_lock",
    "require_toolchain",
    "resolve_micromamba",
    "toolchain_home",
    "toolchain_problem",
]
