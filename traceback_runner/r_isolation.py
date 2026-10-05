"""Run one R script isolated from the operator's R setup (no caller yet; CN3).

What isolation means here:

- ``Rscript --vanilla`` by absolute path: no site or user profile, no saved
  workspace, no user ``.Renviron``.
- A scrubbed environment built from nothing: ``R_LIBS_USER`` and
  ``R_LIBS_SITE`` empty, ``R_PROFILE_USER`` and ``R_ENVIRON_USER`` set to
  ``/dev/null``, a private ``HOME`` and ``TMPDIR`` under the work directory,
  the C locale, UTC, and BLAS/OpenMP limited to one thread.  (R's own
  ``etc/Renviron`` refills an empty ``R_LIBS_USER`` with a default under
  ``HOME``; that ``HOME`` is the private, empty one, so it adds nothing.)
- A bootstrap that asserts ``.libPaths()`` equals the declared library paths
  before anything else runs (exit 86 otherwise) and fixes the RNG kind and
  seed, then ``source()``s the script.  The script's own
  ``commandArgs(trailingOnly = TRUE)`` are exactly ``args``.
- Optional SHA-256 pins on ``Rscript`` and the script, checked right before
  exec.
- The process group is killed on timeout, interrupt or abort, and logs are
  bounded (``contained_process``).
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .contained_process import (
    DEFAULT_LOG_LIMIT_BYTES,
    ContainedProcessError,
    ContainedResult,
    run_contained,
)

LIBPATHS_MISMATCH_EXIT = 86
LIBPATHS_MISMATCH_MARKER = "TRACEBACK_R_LIBPATHS_MISMATCH"
BOOTSTRAP_NAME = ".traceback-r-bootstrap.R"
DEFAULT_SEED = 20261004

# Thread-count variables for every BLAS/OpenMP runtime conda-forge R may link.
SINGLE_THREAD_VARIABLES = (
    "BLIS_NUM_THREADS",
    "GOTO_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "R_DATATABLE_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

R_BOOTSTRAP = f"""\
# Written by traceback; do not edit.  Asserts the library paths and fixes the
# RNG before the method script runs.
local({{
  expected <- strsplit(Sys.getenv("TRACEBACK_R_LIBPATHS"), "\\n", fixed = TRUE)[[1]]
  actual <- .libPaths()
  if (length(expected) == 0L ||
      !identical(normalizePath(actual, winslash = "/", mustWork = FALSE),
                 normalizePath(expected, winslash = "/", mustWork = FALSE))) {{
    message("{LIBPATHS_MISMATCH_MARKER}: ", paste(actual, collapse = ":"))
    quit(save = "no", status = {LIBPATHS_MISMATCH_EXIT}L)
  }}
}})
RNGkind("Mersenne-Twister", "Inversion", "Rejection")
set.seed(as.integer(Sys.getenv("TRACEBACK_R_SEED")))
source(Sys.getenv("TRACEBACK_R_SCRIPT"), echo = FALSE)
"""
R_BOOTSTRAP_SHA256 = hashlib.sha256(R_BOOTSTRAP.encode("utf-8")).hexdigest()


class RIsolationError(ValueError):
    """The R invocation is malformed or a pinned file changed; nothing ran."""


@dataclass(frozen=True)
class RInvocation:
    rscript: Path
    script: Path
    args: tuple[str, ...]
    library_paths: tuple[Path, ...]
    work_dir: Path
    timeout_seconds: float
    seed: int = DEFAULT_SEED
    rscript_sha256: str | None = None
    script_sha256: str | None = None
    log_limit_bytes: int = DEFAULT_LOG_LIMIT_BYTES
    extra_path_dirs: tuple[Path, ...] = field(default=())


@dataclass(frozen=True)
class RRunResult:
    process: ContainedResult
    libpaths_mismatch: bool

    @property
    def succeeded(self) -> bool:
        return self.process.succeeded and not self.libpaths_mismatch


def _absolute(path: Path, label: str) -> Path:
    text = os.fspath(path)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise RIsolationError(f"{label} must be an absolute, normalised path")
    return Path(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check_pin(path: Path, expected: str | None, label: str) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise RIsolationError(f"{label} is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RIsolationError(f"{label} is not a regular file")
    if expected is not None and _sha256(path) != expected:
        raise RIsolationError(f"{label} does not match its pinned SHA-256")


def _validate(invocation: RInvocation) -> None:
    _absolute(invocation.rscript, "Rscript")
    _absolute(invocation.script, "the R script")
    work_dir = _absolute(invocation.work_dir, "the work directory")
    if not work_dir.is_dir() or work_dir.is_symlink():
        raise RIsolationError("the work directory must be an existing directory")
    if not invocation.library_paths:
        raise RIsolationError("at least one R library path must be declared")
    for library in invocation.library_paths:
        _absolute(library, "an R library path")
        if "\n" in os.fspath(library):
            raise RIsolationError("R library paths must not contain newlines")
    for directory in invocation.extra_path_dirs:
        _absolute(directory, "a PATH directory")
        if ":" in os.fspath(directory):
            raise RIsolationError("PATH directories must not contain ':'")
    if any("\x00" in item for item in invocation.args):
        raise RIsolationError("R arguments must be NUL-free")
    if not -(2**31) < invocation.seed < 2**31:
        raise RIsolationError("the RNG seed must fit an R integer")


def isolated_r_environment(invocation: RInvocation) -> dict[str, str]:
    """The complete child environment; nothing is inherited."""

    work_dir = invocation.work_dir
    path_dirs = [
        *(os.fspath(item) for item in invocation.extra_path_dirs),
        os.fspath(invocation.rscript.parent),
        "/usr/bin",
        "/bin",
    ]
    env = {
        "HOME": os.fspath(work_dir / "home"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": ":".join(dict.fromkeys(path_dirs)),
        "R_ENVIRON_USER": "/dev/null",
        "R_LIBS_SITE": "",
        "R_LIBS_USER": "",
        "R_PROFILE_USER": "/dev/null",
        "TMPDIR": os.fspath(work_dir / "tmp"),
        "TRACEBACK_R_LIBPATHS": "\n".join(os.fspath(item) for item in invocation.library_paths),
        "TRACEBACK_R_SCRIPT": os.fspath(invocation.script),
        "TRACEBACK_R_SEED": str(invocation.seed),
        "TZ": "UTC",
    }
    env.update({name: "1" for name in SINGLE_THREAD_VARIABLES})
    return env


def isolated_r_argv(invocation: RInvocation) -> tuple[str, ...]:
    return (
        os.fspath(invocation.rscript),
        "--vanilla",
        os.fspath(invocation.work_dir / BOOTSTRAP_NAME),
        *invocation.args,
    )


def _write_bootstrap(work_dir: Path) -> None:
    target = work_dir / BOOTSTRAP_NAME
    content = R_BOOTSTRAP.encode("utf-8")
    temporary = work_dir / f".{BOOTSTRAP_NAME}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)  # a crashed earlier attempt's leftover
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    os.replace(temporary, target)


def run_isolated_r(
    invocation: RInvocation,
    *,
    should_abort: Callable[[], bool] | None = None,
    environment_overrides: Mapping[str, str] | None = None,
) -> RRunResult:
    """Run the script once; raise ``RIsolationError`` before exec on any defect.

    ``environment_overrides`` exists for tests only and may not touch the
    isolation variables.
    """

    _validate(invocation)
    _check_pin(invocation.rscript, invocation.rscript_sha256, "Rscript")
    _check_pin(invocation.script, invocation.script_sha256, "the R script")
    env = isolated_r_environment(invocation)
    if environment_overrides:
        clash = set(environment_overrides) & set(env)
        if clash:
            raise RIsolationError("overrides may not replace isolation variables")
        env.update(environment_overrides)
    for directory in ("home", "tmp"):
        (invocation.work_dir / directory).mkdir(mode=0o700, exist_ok=True)
    _write_bootstrap(invocation.work_dir)
    try:
        process = run_contained(
            isolated_r_argv(invocation),
            env=env,
            cwd=invocation.work_dir,
            timeout_seconds=invocation.timeout_seconds,
            log_limit_bytes=invocation.log_limit_bytes,
            should_abort=should_abort,
        )
    except ContainedProcessError as exc:
        raise RIsolationError(str(exc)) from exc
    mismatch = (
        process.outcome == "exited"
        and process.returncode == LIBPATHS_MISMATCH_EXIT
        and LIBPATHS_MISMATCH_MARKER.encode("ascii") in process.stderr
    )
    return RRunResult(process=process, libpaths_mismatch=mismatch)


__all__ = [
    "BOOTSTRAP_NAME",
    "DEFAULT_SEED",
    "LIBPATHS_MISMATCH_EXIT",
    "R_BOOTSTRAP",
    "R_BOOTSTRAP_SHA256",
    "RInvocation",
    "RIsolationError",
    "RRunResult",
    "SINGLE_THREAD_VARIABLES",
    "isolated_r_argv",
    "isolated_r_environment",
    "run_isolated_r",
]
