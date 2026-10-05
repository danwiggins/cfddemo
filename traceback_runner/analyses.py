"""The analyses ``traceback run --analysis`` can run (signal methods SH4).

``fragment`` is the built-in fragment-length method.  ``cell-origin`` and
``copy-number`` are reserved here and get their stages from a registry: CO3
and CN3 call :func:`register_analysis_stages` with the method definition and
stage factory of their method.  Until they do, ``run --analysis`` refuses
that analysis (TBX-RUN-011) and every other requested analysis still runs.

Every result is unqualified, local and not for clinical use.

Job identity.  The fragment job keeps its sample token ``local-<ref>``; every
other analysis uses ``local-<ref>:<analysis>``.  The workflow hash is the same
``sha256("local-unqualified-v0:" + method_definition_sha256)`` for every
analysis, computed from that analysis's own definition.  Every resolved
setting (for example ``--modbase-model``) is passed to the definition hook, so
a changed declaration is a changed definition and therefore a new job.

The token suffixes ``cell-origin`` and ``copy-number`` are reserved: a
research policy ID (usability D2, ``local-<ref>:<policy_id>``) must never use
them, so a token always names one thing.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .serialization import canonical_json_bytes

FRAGMENT = "fragment"
CELL_ORIGIN = "cell-origin"
COPY_NUMBER = "copy-number"
# Closed and in run order: fragment always runs first.
ANALYSES: tuple[str, ...] = (FRAGMENT, CELL_ORIGIN, COPY_NUMBER)
# Sample-token suffixes no research policy ID may take (spec §11 item 24).
RESERVED_TOKEN_SUFFIXES = frozenset({CELL_ORIGIN, COPY_NUMBER})
# Every resolved per-analysis setting (closed), and the analysis that takes it.
SETTING_ANALYSIS: Mapping[str, str] = {"modbase_model": CELL_ORIGIN}
CONFIG_KEYS = frozenset(SETTING_ANALYSIS)

LOCAL_SAMPLE_PREFIX = "local-"
_MODBASE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")


def parse_analysis_list(text: str) -> tuple[str, ...]:
    """``--analysis`` value: comma-separated names from the closed list.

    Returned in run order (fragment first), whatever order they were given in.
    Unknown, empty and repeated names are refused.
    """

    names = [item.strip() for item in text.split(",")]
    if not names or any(not name for name in names):
        raise ValueError("--analysis takes comma-separated names, e.g. fragment,cell-origin")
    unknown = sorted(set(names) - set(ANALYSES))
    if unknown:
        raise ValueError(
            f"unknown analysis {', '.join(unknown)}; choose from {', '.join(ANALYSES)}"
        )
    if len(set(names)) != len(names):
        raise ValueError("an analysis is named more than once")
    return tuple(name for name in ANALYSES if name in names)


def validate_modbase_model(value: str) -> str:
    """The declared modified-base model: 1-128 characters, no spaces or slashes."""

    if not _MODBASE_MODEL.fullmatch(value):
        raise ValueError(
            "--modbase-model must be 1-128 characters of letters, digits and . _ @ + -"
        )
    return value


def sample_token(reference_id: str, analysis: str) -> str:
    """``local-<ref>`` for fragment (unchanged); ``local-<ref>:<analysis>`` otherwise."""

    if analysis == FRAGMENT:
        return f"{LOCAL_SAMPLE_PREFIX}{reference_id}"
    if analysis not in RESERVED_TOKEN_SUFFIXES:
        raise ValueError(f"unknown analysis {analysis!r}")
    return f"{LOCAL_SAMPLE_PREFIX}{reference_id}:{analysis}"


def parse_sample_token(token: str) -> tuple[str, str] | None:
    """``(reference_id, analysis)`` of a local job token, or ``None``.

    The suffix after the first ``:`` must be a reserved analysis name; a token
    with any other suffix (a research policy, or damage) names no analysis this
    build can run, and ``local-<ref>:fragment`` is never written.
    """

    if not token.startswith(LOCAL_SAMPLE_PREFIX):
        return None
    reference_id, separator, analysis = token[len(LOCAL_SAMPLE_PREFIX):].partition(":")
    if not reference_id:
        return None
    if not separator:
        return reference_id, FRAGMENT
    if analysis not in RESERVED_TOKEN_SUFFIXES:
        return None
    return reference_id, analysis


def is_reserved_policy_id(value: str) -> bool:
    """Whether ``value`` is a token suffix a research policy ID may not take."""

    return value in RESERVED_TOKEN_SUFFIXES or value == FRAGMENT


# ---------------------------------------------------------------------------
# Stage registry (filled by CO3 / CN3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisStageContext:
    """What a stage factory receives when ``run`` or ``resume`` builds stages."""

    root: Path
    loaded: Any  # references.LoadedReference
    bam_name: str
    index_name: str
    config: Mapping[str, str]
    signing_key: Any
    progress: Callable[[str], None]


@dataclass(frozen=True)
class ReadinessRow:
    """One ``preflight --analysis`` line: a code, an outcome and its detail."""

    code: str
    outcome: str  # ready | blocked | missing | not_set_up
    detail: str

    def as_json(self) -> dict[str, str]:
        return {"code": self.code, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class AnalysisStages:
    """One non-fragment analysis's method and stages.

    ``definition(loaded, config)`` returns the locked E01 method definition for
    the registered reference with every resolved setting in ``config`` bound
    into it (its hash is the job's workflow identity and names the method
    authority store).  It may raise a ``ReferenceProblem`` (for example a
    missing ``--modbase-model``); that refuses only this analysis.

    ``stages(context)`` returns the runner stages.  The last stage (``sign``)
    writes a v4 bundle whose method identity is this definition's.  A stage
    that finds a pinned tool missing raises ``toolchain.ToolProblem``; ``run``
    records that as retryable, never terminal.

    ``readiness(loaded, config)`` (optional) adds ``preflight --analysis`` rows.
    """

    analysis: str
    method_slug: str
    definition: Callable[[Any, Mapping[str, str]], Any]
    stages: Callable[[AnalysisStageContext], tuple[Any, ...]]
    config_keys: frozenset[str] = field(default_factory=frozenset)
    readiness: Callable[[Any, Mapping[str, str]], list[ReadinessRow]] | None = None


_REGISTRY: dict[str, AnalysisStages] = {}


def register_analysis_stages(spec: AnalysisStages) -> AnalysisStages:
    """Register one analysis's stages (once per analysis)."""

    from .local_authority import validate_method_slug

    if spec.analysis not in RESERVED_TOKEN_SUFFIXES:
        raise ValueError("only cell-origin and copy-number take registered stages")
    validate_method_slug(spec.method_slug)
    if any(SETTING_ANALYSIS.get(key) != spec.analysis for key in spec.config_keys):
        raise ValueError("an analysis may only take its own settings from SETTING_ANALYSIS")
    existing = _REGISTRY.get(spec.analysis)
    if existing is not None and existing != spec:
        raise ValueError(f"stages for {spec.analysis} are already registered")
    _REGISTRY[spec.analysis] = spec
    return spec


def registered_analysis_stages(analysis: str) -> AnalysisStages | None:
    return _REGISTRY.get(analysis)


def resolved_config(spec: AnalysisStages, settings: Mapping[str, str | None]) -> dict[str, str]:
    """The settings this analysis takes, as declared (absent ones left out)."""

    return {
        key: value
        for key in sorted(spec.config_keys)
        if (value := settings.get(key)) is not None
    }


# ---------------------------------------------------------------------------
# Resolved settings of a job, kept for resume
# ---------------------------------------------------------------------------

CONFIG_DIRECTORY = "analysis-config"
_CONFIG_SCHEMA = "traceback.analysis-config.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONFIG_BYTES = 64 * 1024


class AnalysisConfigConflict(ValueError):
    """Two different settings resolved to one workflow hash.

    The method definition did not bind a setting, so a new declaration would
    dedupe onto an earlier job.  Never expected; the run refuses.
    """


def _config_bytes(reference_id: str, analysis: str, config: Mapping[str, str]) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": _CONFIG_SCHEMA,
            "reference_id": reference_id,
            "analysis": analysis,
            "config": dict(sorted(config.items())),
        }
    )


def _config_path(root: Path, workflow_sha256: str) -> Path:
    if not _SHA256.fullmatch(workflow_sha256):
        raise ValueError("workflow hash must be 64 lowercase hex characters")
    return root / CONFIG_DIRECTORY / f"{workflow_sha256}.json"


def record_job_config(
    root: Path,
    workflow_sha256: str,
    reference_id: str,
    analysis: str,
    config: Mapping[str, str],
) -> None:
    """Keep a job's resolved settings under its workflow hash (write-once, 0600).

    ``resume`` reads them back to rebuild the same stages, and refuses unless
    they rebuild the stored workflow hash.  The same hash with other settings
    raises :class:`AnalysisConfigConflict`: the definition did not bind them.
    """

    content = _config_bytes(reference_id, analysis, config)
    directory = root / CONFIG_DIRECTORY
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    metadata = os.stat(directory, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError("ROOT/analysis-config is not a directory of yours")
    path = _config_path(root, workflow_sha256)
    existing = read_job_config(root, workflow_sha256)
    if existing is not None:
        if _config_bytes(*existing) != content:
            raise AnalysisConfigConflict(
                "another setting already resolved to this workflow hash"
            )
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    existing = read_job_config(root, workflow_sha256)
    if existing is None or _config_bytes(*existing) != content:
        raise AnalysisConfigConflict("another setting already resolved to this workflow hash")


def read_job_config(
    root: Path, workflow_sha256: str
) -> tuple[str, str, dict[str, str]] | None:
    """``(reference_id, analysis, config)`` kept for a workflow hash, or ``None``.

    Unreadable or malformed content reads as ``None``; ``resume`` then refuses
    because nothing rebuilds the stored hash.
    """

    import json

    path = _config_path(root, workflow_sha256)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        return None
    try:
        value = json.loads(raw)
        reference_id, analysis, config = (
            value["reference_id"],
            value["analysis"],
            value["config"],
        )
    except (ValueError, KeyError, TypeError):
        return None
    if (
        value.get("schema_version") != _CONFIG_SCHEMA
        or not isinstance(reference_id, str)
        or analysis not in RESERVED_TOKEN_SUFFIXES
        or not isinstance(config, dict)
        or not set(config) <= CONFIG_KEYS
        or not all(isinstance(item, str) for item in config.values())
        or _config_bytes(reference_id, analysis, config) != raw
    ):
        return None
    return reference_id, analysis, dict(config)


__all__ = [
    "ANALYSES",
    "CELL_ORIGIN",
    "CONFIG_KEYS",
    "COPY_NUMBER",
    "FRAGMENT",
    "RESERVED_TOKEN_SUFFIXES",
    "SETTING_ANALYSIS",
    "AnalysisConfigConflict",
    "AnalysisStageContext",
    "AnalysisStages",
    "ReadinessRow",
    "is_reserved_policy_id",
    "parse_analysis_list",
    "parse_sample_token",
    "read_job_config",
    "record_job_config",
    "register_analysis_stages",
    "registered_analysis_stages",
    "resolved_config",
    "sample_token",
    "validate_modbase_model",
]
