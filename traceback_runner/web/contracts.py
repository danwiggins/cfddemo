"""Frozen B01 projection, action, and problem contracts."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from traceback_runner.contracts import JobState, RunnerContract

MAX_ACTIONS = 8
_SAFE_OPERATOR_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'()%;:!?+_-]*$")
_IUPAC_SEQUENCE = re.compile(r"^[ACGTURYSWKMBDHVN]{24,}$", re.IGNORECASE)
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")
_MD5_TEXT = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_FIELDS = ("sha256", "sha256s", "md5", "md5s")


def _canonicalize_path_separators(value: str) -> str:
    """Collapse Unicode slash and reverse-solidus confusables to separators."""

    canonical: list[str] = []
    for character in value:
        name = unicodedata.name(character, "")
        separator_like = (
            character in {"/", "\\"}
            or "SOLIDUS" in name
            or "SLASH" in name
            or name == "SET MINUS"
            or ("BOX DRAWINGS" in name and "DIAGONAL" in name)
        )
        canonical.append("/" if separator_like else character)
    return "".join(canonical)


def _decoded_public_text(value: str) -> str:
    decoded = _canonicalize_path_separators(unicodedata.normalize("NFKC", value))
    for _ in range(8):
        next_value = _canonicalize_path_separators(
            unicodedata.normalize("NFKC", unquote(decoded))
        )
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise ValueError("public text percent decoding did not converge")


def validate_public_text(value: str) -> str:
    """Reject paths, identifiers, credentials, and biological sequence text."""

    decoded = _decoded_public_text(value)
    lowered = decoded.casefold()
    dangerous_schemes = (
        "data|file|ftp|gopher|http|https|javascript|nfs|smb|ssh|telnet|ws|wss"
    )
    if re.search(rf"(?<![A-Za-z0-9_])(?:{dangerous_schemes})\s*:", lowered):
        raise ValueError("public text cannot contain a URI")
    if (
        "/" in decoded
        or "\\" in decoded
        or ".." in decoded
        or re.search(r"(?i)(?:^|\s)[a-z]:[/\\]", decoded)
        or any(root in lowered for root in ("/users/", "/home/", "/volumes/"))
    ):
        raise ValueError("public text cannot contain a path")
    if re.search(
        r"\b(?:source|donor|patient|sample|read|query|path)[ _-]*(?:id|identifier)\b",
        decoded,
        flags=re.IGNORECASE,
    ):
        raise ValueError("public text cannot contain a private identifier")
    if re.search(
        r"(?:authorization|api[_ -]?key|secret|password|token)\s*[:=]\s*\S+",
        decoded,
        flags=re.IGNORECASE,
    ):
        raise ValueError("public text cannot contain a credential")
    nucleotide_candidate = re.sub(r"[^A-Za-z]", "", decoded)
    if _IUPAC_SEQUENCE.fullmatch(nucleotide_candidate):
        raise ValueError("public text cannot contain a raw nucleotide sequence")
    return value


def validate_public_projection(value: Any, *, field_name: str = "") -> None:
    """Recursively enforce the public-text boundary, except typed digests."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            validate_public_projection(nested, field_name=str(key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            validate_public_projection(nested, field_name=field_name)
    elif isinstance(value, str):
        normalized_field = field_name.casefold()
        digest_field = normalized_field.endswith(_DIGEST_FIELDS)
        typed_digest = digest_field and bool(
            _SHA256_TEXT.fullmatch(value) or _MD5_TEXT.fullmatch(value)
        )
        if not typed_digest:
            validate_public_text(value)


def _safe_operator_text(value: str) -> str:
    if _SAFE_OPERATOR_TEXT.fullmatch(value) is None:
        raise ValueError("operator text contains characters outside the safe grammar")
    decoded = _decoded_public_text(value)
    lowered = decoded.casefold()
    dangerous_schemes = (
        "data|file|ftp|gopher|http|https|javascript|nfs|smb|ssh|telnet|ws|wss"
    )
    if (
        re.search(rf"(?<![A-Za-z0-9_])(?:{dangerous_schemes})\s*:", lowered)
        or "//" in decoded
    ):
        raise ValueError("operator text cannot contain a URL")
    if "/" in decoded or "\\" in decoded or ".." in decoded:
        raise ValueError("operator text cannot contain a path")
    if re.search(
        r"\b(?:source|donor|patient|sample|read|path)[ _-]*(?:id|identifier)\b",
        decoded,
        flags=re.IGNORECASE,
    ):
        raise ValueError("operator text cannot contain a private identifier")
    if re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        decoded,
        flags=re.IGNORECASE,
    ):
        raise ValueError("operator text cannot contain a UUID-like identifier")
    try:
        validate_public_text(value)
    except ValueError as exc:
        raise ValueError(str(exc).replace("public text", "operator text")) from exc
    if re.search(
        r"(?:authorization|api[_ -]?key|secret|password|token)\s*[:=]\s*\S+",
        decoded,
        flags=re.IGNORECASE,
    ):
        raise ValueError("operator text cannot contain a credential")
    return value


def _bundled_docs_path(value: str) -> str:
    if "\\" in value or "?" in value or "#" in value:
        raise ValueError("documentation path must be a bundled POSIX path")
    path = PurePosixPath(value)
    if (
        not value.startswith("docs/")
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or path.suffix != ".md"
        or str(path) != value
    ):
        raise ValueError("documentation path must name a bundled Markdown file")
    return value


SafeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
    AfterValidator(_safe_operator_text),
]
BundledDocsPath = Annotated[
    str,
    StringConstraints(
        min_length=9, max_length=160, pattern=r"^docs/[A-Za-z0-9._/-]+\.md$"
    ),
    AfterValidator(_bundled_docs_path),
]
OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=12,
        max_length=96,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$",
    ),
]
CorrelationId = Annotated[
    str,
    StringConstraints(pattern=r"^cor_[a-z0-9]{16,48}$"),
]


class ProblemOwner(StrEnum):
    OPERATOR = "operator"
    SUPPORT = "support"
    WORKFLOW_ADMIN = "workflow_admin"


class ProblemDetail(RunnerContract):
    """One safe problem shape shared by local API, UI, CLI, and support."""

    schema_version: Literal["traceback.problem-detail.v1"] = (
        "traceback.problem-detail.v1"
    )
    code: str = Field(pattern=r"^TBX-[A-Z]+-[0-9]{3}$")
    problem: SafeText
    cause: SafeText
    fix: SafeText
    docs_path: BundledDocsPath
    owner: ProblemOwner
    retryable: bool
    correlation_id: CorrelationId
    preserved_work: SafeText
    repeated_work: SafeText


class ActionKind(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    RETRY = "retry"
    VERIFY = "verify"
    SAVE_LOCAL_COPY = "save_local_copy"


class JobAction(RunnerContract):
    action: ActionKind
    label: SafeText
    expected_revision: int = Field(ge=0)
    enabled: bool
    disabled_reason: SafeText | None = None

    @model_validator(mode="after")
    def explain_disabled_action(self) -> JobAction:
        if self.enabled == (self.disabled_reason is not None):
            raise ValueError("disabled actions require exactly one safe reason")
        return self


class JobProjection(RunnerContract):
    """Browser-safe projection; never contains a source path or raw identifier."""

    schema_version: Literal["traceback.job-projection.v1"] = (
        "traceback.job-projection.v1"
    )
    job_id: OpaqueId
    state: JobState
    stage_label: SafeText
    updated_at: datetime
    revision: int = Field(ge=0)
    stale: bool
    headline: SafeText
    owner: ProblemOwner
    next_action: SafeText
    problem: ProblemDetail | None = None
    actions: tuple[JobAction, ...] = Field(default=(), max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def stale_projection_disables_mutations(self) -> JobProjection:
        kinds = [action.action for action in self.actions]
        if kinds != sorted(kinds, key=str) or len(kinds) != len(set(kinds)):
            raise ValueError("job actions must be uniquely sorted")
        if self.stale and any(action.enabled for action in self.actions):
            raise ValueError("stale projections cannot expose enabled actions")
        if any(action.expected_revision != self.revision for action in self.actions):
            raise ValueError("job actions must bind the current projection revision")
        return self
