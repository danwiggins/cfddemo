"""Bounded execution contract for a future pinned MethylBERT Lab worker.

This module does not activate an algorithm. The worker needs a trusted
tumour/normal checkpoint and an orientation-validated ONT smoke test first.
"""

from __future__ import annotations

import json
import math
import os
import stat
from importlib import metadata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from traceback_runner.contained_process import run_contained

from .methylbert_bundle import Bundle


PINNED_UPSTREAM_COMMIT = "f82f83b0bc20e2af1441795536ec9f338fbbb3e7"
MIN_ELIGIBLE_READS = 2_500_000
MAX_WALL_SECONDS = 2 * 60 * 60
MAX_LOG_BYTES = 64 * 1024
MAX_AGGREGATE_BYTES = 16 * 1024
AGGREGATE_SCHEMA = "traceback.methylbert-aggregate.v1"


class RuntimeRefusal(ValueError):
    """Model, input, environment, or aggregate result violates the contract."""


@dataclass(frozen=True)
class Aggregate:
    model_id: str
    tumour_fraction: float
    normal_fraction: float
    classified_reads: int


def installed_commit() -> str:
    """Read the Git commit of the installed upstream package, not its version label."""

    try:
        distribution = metadata.distribution("methylbert")
        direct = json.loads(distribution.read_text("direct_url.json") or "")
    except (metadata.PackageNotFoundError, OSError, ValueError, TypeError) as exc:
        raise RuntimeRefusal("MethylBERT is not installed from a pinned Git commit") from exc
    try:
        url = urlparse(direct.get("url", "")) if isinstance(direct, dict) else urlparse("")
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
    return commit


def require_ready(
    bundle: Bundle, *, reference_sha256: str, eligible_reads: int | None,
    platform: str, gpu_available: bool, installed_commit_sha: str | None = None,
) -> None:
    """Gate the exact registered model, reference, ONT depth and CUDA runtime."""

    commit = installed_commit() if installed_commit_sha is None else installed_commit_sha
    if bundle.upstream_commit != PINNED_UPSTREAM_COMMIT or commit != PINNED_UPSTREAM_COMMIT:
        raise RuntimeRefusal("MethylBERT source or model was not built at the pinned commit")
    if bundle.reference_sha256 != reference_sha256:
        raise RuntimeRefusal("MethylBERT model reference differs from the input reference")
    if platform != "ont":
        raise RuntimeRefusal("MethylBERT requires declared ONT input")
    if type(eligible_reads) is not int or eligible_reads < MIN_ELIGIBLE_READS:
        raise RuntimeRefusal("MethylBERT needs at least 2,500,000 eligible ONT reads")
    if not gpu_available:
        raise RuntimeRefusal("MethylBERT needs a usable CUDA GPU")


def read_aggregate(path: Path, *, model_id: str) -> Aggregate:
    """Accept only one small aggregate object; never ingest per-read output."""

    try:
        if path.is_symlink():
            raise RuntimeRefusal("MethylBERT aggregate is missing, linked, or oversized")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_AGGREGATE_BYTES:
                raise RuntimeRefusal("MethylBERT aggregate is missing, linked, or oversized")
            data = stream.read(MAX_AGGREGATE_BYTES + 1)
            if len(data) > MAX_AGGREGATE_BYTES:
                raise RuntimeRefusal("MethylBERT aggregate is oversized")
        raw = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeRefusal("MethylBERT aggregate is unreadable or invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema", "model_id", "tumour_fraction", "normal_fraction", "classified_reads",
    } or raw["schema"] != AGGREGATE_SCHEMA or raw["model_id"] != model_id:
        raise RuntimeRefusal("MethylBERT aggregate has unexpected fields or model identity")
    fractions = (raw["tumour_fraction"], raw["normal_fraction"])
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
           for value in fractions) or not math.isclose(sum(fractions), 1.0, rel_tol=0, abs_tol=1e-6):
        raise RuntimeRefusal("MethylBERT aggregate fractions are invalid")
    count = raw["classified_reads"]
    if type(count) is not int or count < 1:
        raise RuntimeRefusal("MethylBERT aggregate has no classified reads")
    return Aggregate(model_id, float(fractions[0]), float(fractions[1]), count)


def run_worker(
    argv: Sequence[str], *, scratch: Path, env: Mapping[str, str],
    aggregate_path: Path, model_id: str, should_abort: Callable[[], bool] | None = None,
) -> Aggregate:
    """Bound a pinned worker's process group, logs, time and published result."""

    if not argv or not os.path.isabs(argv[0]):
        raise RuntimeRefusal("MethylBERT worker executable must be absolute")
    if not scratch.is_absolute() or not aggregate_path.is_absolute():
        raise RuntimeRefusal("MethylBERT scratch and aggregate paths must be absolute")
    if (not aggregate_path.parent.resolve(strict=True).is_relative_to(scratch.resolve(strict=True))
            or aggregate_path.exists() or aggregate_path.is_symlink()):
        raise RuntimeRefusal("MethylBERT aggregate must be a new file inside private scratch")
    result = run_contained(
        argv, env=env, cwd=scratch, timeout_seconds=MAX_WALL_SECONDS,
        log_limit_bytes=MAX_LOG_BYTES, should_abort=should_abort,
    )
    if not result.succeeded:
        raise RuntimeRefusal("MethylBERT worker failed, timed out, or was aborted")
    return read_aggregate(aggregate_path, model_id=model_id)
