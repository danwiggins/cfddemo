"""Bounded execution contract for a future pinned MethylBERT Lab worker.

This module does not activate an algorithm. The worker needs a trusted
tumour/normal checkpoint and an orientation-validated ONT smoke test first.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import stat
import sys
from importlib import metadata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from traceback_runner.contained_process import run_contained

from .methylbert_bundle import Bundle
from .methylbert_gpu_probe import NUMERICAL_PACKAGES as NUMERICAL_PACKAGES
from .methylbert_gpu_probe import package_versions, probe_cuda


PINNED_UPSTREAM_COMMIT = "f82f83b0bc20e2af1441795536ec9f338fbbb3e7"
MIN_ELIGIBLE_READS = 2_500_000
MAX_WALL_SECONDS = 2 * 60 * 60
MAX_LOG_BYTES = 64 * 1024
MAX_AGGREGATE_BYTES = 16 * 1024
MAX_CODE_FILE_BYTES = 8 * 1024 * 1024
MAX_CODE_TOTAL_BYTES = 64 * 1024 * 1024
AGGREGATE_SCHEMA = "traceback.methylbert-aggregate.v1"
CHILD_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
    "HOME", "TMPDIR", "TMP", "TEMP", "HF_HOME", "HF_HUB_OFFLINE",
    "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE", "TOKENIZERS_PARALLELISM", "NO_PROXY",
})
def numerical_runtime_identity(*, probe_gpu: bool, isolated_gpu: bool) -> dict[str, Any]:
    """Key numerical dependencies without importing Torch for an absent model."""

    cuda: dict[str, Any] | None = None
    if probe_gpu:
        if isolated_gpu:
            probe_env = {name: os.environ[name] for name in (
                "PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
            ) if name in os.environ}
            probe_env.update({"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                              "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
                              "NO_PROXY": "*"})
            result = run_contained(
                [sys.executable, "-I", str(Path(__file__).with_name("methylbert_gpu_probe.py"))],
                env=probe_env, cwd=Path(__file__).resolve().parent,
                timeout_seconds=30, log_limit_bytes=8 * 1024,
            )
            try:
                identity = json.loads(result.stdout) if result.succeeded and not result.stdout_truncated else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                identity = None
            if (not isinstance(identity, dict) or set(identity) != {"python", "packages", "cuda"}
                    or not isinstance(identity["packages"], dict) or not isinstance(identity["cuda"], dict)):
                raise RuntimeRefusal("MethylBERT isolated numerical runtime probe failed")
            return identity
        else:
            cuda = probe_cuda()
    return {"python": sys.version.split()[0], "packages": package_versions(), "cuda": cuda}


class RuntimeRefusal(ValueError):
    """Model, input, environment, or aggregate result violates the contract."""


def apply_precision_flags(expected: object) -> None:
    """Restore selected Torch precision settings after model-package imports."""

    cuda = expected.get("cuda") if isinstance(expected, dict) else None
    if not isinstance(cuda, dict):
        raise RuntimeRefusal("MethylBERT selected precision settings are missing")
    booleans = ("matmul_allow_tf32", "cudnn_allow_tf32", "cudnn_benchmark")
    if any(type(cuda.get(key)) is not bool for key in booleans):
        raise RuntimeRefusal("MethylBERT selected precision settings are invalid")
    precision = cuda.get("float32_matmul_precision")
    if precision not in {"highest", "high", "medium"}:
        raise RuntimeRefusal("MethylBERT selected matmul precision is invalid")
    import torch

    torch.set_float32_matmul_precision(precision)
    torch.backends.cuda.matmul.allow_tf32 = cuda["matmul_allow_tf32"]
    torch.backends.cudnn.allow_tf32 = cuda["cudnn_allow_tf32"]
    torch.backends.cudnn.benchmark = cuda["cudnn_benchmark"]


def require_numerical_runtime(expected: object) -> dict[str, Any]:
    """Refuse package or CUDA drift before loading a trusted checkpoint."""

    actual = numerical_runtime_identity(probe_gpu=True, isolated_gpu=False)
    if not isinstance(expected, dict) or actual != expected:
        raise RuntimeRefusal("MethylBERT numerical runtime differs from the selected execution identity")
    return actual


@dataclass(frozen=True)
class Aggregate:
    model_id: str
    tumour_fraction: float
    normal_fraction: float
    classified_reads: int


@dataclass(frozen=True)
class InstalledCodeIdentity:
    commit: str
    sha256: str


def installed_identity() -> InstalledCodeIdentity:
    """Capture Git provenance and distribution RECORD-listed Python file bytes.

    This does not prove which package bytes a later child process imports.
    """

    try:
        distribution = metadata.distribution("methylbert")
        direct = json.loads(distribution.read_text("direct_url.json") or "")
    except (metadata.PackageNotFoundError, OSError, ValueError, TypeError) as exc:
        raise RuntimeRefusal("MethylBERT is not installed from a pinned Git commit") from exc
    raw_url = direct.get("url") if isinstance(direct, dict) else None
    if not isinstance(raw_url, str):
        raise RuntimeRefusal("MethylBERT has an invalid source URL")
    try:
        url = urlparse(raw_url)
    except ValueError as exc:
        raise RuntimeRefusal("MethylBERT has an invalid source URL") from exc
    path = url.path.removesuffix(".git").lower().strip("/")
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    if (url.scheme != "https" or url.hostname != "github.com" or url.username is not None
            or path != "compepigen/methylbert" or not isinstance(vcs, dict) or vcs.get("vcs") != "git"
            or not isinstance(commit, str) or len(commit) != 40
            or any(char not in "0123456789abcdef" for char in commit)):
        raise RuntimeRefusal("MethylBERT is not installed from the pinned upstream repository")
    files = distribution.files or ()
    names = sorted({str(item).replace("\\", "/") for item in files
                    if str(item).replace("\\", "/").startswith("methylbert/")
                    and str(item).endswith(".py")})
    if not names or len(names) > 1000:
        raise RuntimeRefusal("MethylBERT installed code files are missing or excessive")
    digest = hashlib.sha256()
    total = 0
    for name in names:
        if ".." in Path(name).parts:
            raise RuntimeRefusal("MethylBERT installed code path is invalid")
        path = Path(distribution.locate_file(name))
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_CODE_FILE_BYTES:
                    raise RuntimeRefusal("MethylBERT installed code file is invalid")
                total += info.st_size
                if total > MAX_CODE_TOTAL_BYTES:
                    raise RuntimeRefusal("MethylBERT installed code exceeds the size cap")
                digest.update(name.encode("utf-8") + b"\0" + info.st_size.to_bytes(8, "big"))
                remaining = info.st_size
                while remaining:
                    chunk = stream.read(min(1 << 20, remaining))
                    if not chunk:
                        raise RuntimeRefusal("MethylBERT installed code changed while hashing")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise RuntimeRefusal("MethylBERT installed code grew while hashing")
        except OSError as exc:
            raise RuntimeRefusal("MethylBERT installed code cannot be read") from exc
    return InstalledCodeIdentity(commit, digest.hexdigest())


def require_ready(
    bundle: Bundle, *, reference_sha256: str, eligible_reads: int | None,
    platform: str, gpu_available: bool, expected_code_sha256: str,
) -> InstalledCodeIdentity:
    """Check a bundle and caller-asserted reference, ONT depth, and CUDA state.

    The eventual adapter must derive those assertions from verified inputs.
    """

    code = installed_identity()
    if bundle.upstream_commit != PINNED_UPSTREAM_COMMIT or code.commit != PINNED_UPSTREAM_COMMIT:
        raise RuntimeRefusal("MethylBERT source or model was not built at the pinned commit")
    if len(code.sha256) != 64 or any(char not in "0123456789abcdef" for char in code.sha256):
        raise RuntimeRefusal("MethylBERT installed code has no valid byte digest")
    if (not isinstance(expected_code_sha256, str) or len(expected_code_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_code_sha256)
            or code.sha256 != expected_code_sha256):
        raise RuntimeRefusal("MethylBERT installed code differs from the trusted byte digest")
    if bundle.reference_sha256 != reference_sha256:
        raise RuntimeRefusal("MethylBERT model reference differs from the input reference")
    if platform != "ont":
        raise RuntimeRefusal("MethylBERT requires declared ONT input")
    if type(eligible_reads) is not int or eligible_reads < MIN_ELIGIBLE_READS:
        raise RuntimeRefusal("MethylBERT needs at least 2,500,000 eligible ONT reads")
    if type(gpu_available) is not bool or not gpu_available:
        raise RuntimeRefusal("MethylBERT needs a usable CUDA GPU")
    return code


def read_aggregate(path: Path, *, model_id: str, eligible_reads: int) -> Aggregate:
    """Accept only one small aggregate object; never ingest per-read output."""

    if type(eligible_reads) is not int or eligible_reads < 1:
        raise RuntimeRefusal("MethylBERT eligible-read count is missing or invalid")
    try:
        if path.is_symlink():
            raise RuntimeRefusal("MethylBERT aggregate is missing, linked, or oversized")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_AGGREGATE_BYTES:
                raise RuntimeRefusal("MethylBERT aggregate is missing, linked, or oversized")
            data = stream.read(MAX_AGGREGATE_BYTES + 1)
            if len(data) > MAX_AGGREGATE_BYTES:
                raise RuntimeRefusal("MethylBERT aggregate is oversized")
        raw = json.loads(data)
    except RuntimeRefusal:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RuntimeRefusal("MethylBERT aggregate is unreadable or invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema", "model_id", "tumour_fraction", "normal_fraction", "classified_reads",
    } or raw["schema"] != AGGREGATE_SCHEMA or raw["model_id"] != model_id:
        raise RuntimeRefusal("MethylBERT aggregate has unexpected fields or model identity")
    fractions = (raw["tumour_fraction"], raw["normal_fraction"])
    if any(type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value)
           for value in fractions) or not math.isclose(sum(fractions), 1.0, rel_tol=0, abs_tol=1e-6):
        raise RuntimeRefusal("MethylBERT aggregate fractions are invalid")
    count = raw["classified_reads"]
    if type(count) is not int or count < 1:
        raise RuntimeRefusal("MethylBERT aggregate has no classified reads")
    if count > eligible_reads:
        raise RuntimeRefusal("MethylBERT aggregate has more classified than eligible reads")
    return Aggregate(model_id, float(fractions[0]), float(fractions[1]), count)


def run_worker(
    argv: Sequence[str], *, scratch: Path, env: Mapping[str, str],
    aggregate_path: Path, model_id: str, eligible_reads: int,
    should_abort: Callable[[], bool] | None = None,
) -> Aggregate:
    """Run a supplied command with bounded time, logs, and aggregate output.

    The future adapter must enforce readiness and outer-worker cancellation.
    """

    if not argv or not os.path.isabs(argv[0]):
        raise RuntimeRefusal("MethylBERT worker executable must be absolute")
    if set(env) - CHILD_ENV_KEYS:
        raise RuntimeRefusal("MethylBERT child environment contains an unapproved variable")
    if any(env.get(key) != "1" for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE")):
        raise RuntimeRefusal("MethylBERT child model hubs must be offline")
    if type(eligible_reads) is not int or eligible_reads < MIN_ELIGIBLE_READS:
        raise RuntimeRefusal("MethylBERT needs at least 2,500,000 eligible ONT reads")
    if not scratch.is_absolute() or not aggregate_path.is_absolute():
        raise RuntimeRefusal("MethylBERT scratch and aggregate paths must be absolute")
    try:
        scratch_root = scratch.resolve(strict=True)
        parent = aggregate_path.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeRefusal("MethylBERT scratch or aggregate parent is missing") from exc
    if not parent.is_relative_to(scratch_root) or aggregate_path.exists() or aggregate_path.is_symlink():
        raise RuntimeRefusal("MethylBERT aggregate must be a new file inside private scratch")
    result = run_contained(
        argv, env=env, cwd=scratch, timeout_seconds=MAX_WALL_SECONDS,
        log_limit_bytes=MAX_LOG_BYTES, should_abort=should_abort,
    )
    if not result.succeeded:
        raise RuntimeRefusal("MethylBERT worker failed, timed out, or was aborted")
    try:
        if not aggregate_path.parent.resolve(strict=True).is_relative_to(scratch_root):
            raise RuntimeRefusal("MethylBERT aggregate escaped private scratch")
    except OSError as exc:
        raise RuntimeRefusal("MethylBERT aggregate parent disappeared") from exc
    return read_aggregate(aggregate_path, model_id=model_id, eligible_reads=eligible_reads)
