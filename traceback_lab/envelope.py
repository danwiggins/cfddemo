"""The Lab result envelope (``traceback.lab.result.v1``) and provenance stamp (D2).

A result carries measurement identity only.  Tier, footnote, license,
reference and meaning are joined at read time from the interpretation library
(D3), so ``interpretation_version`` and ``cohort_version`` are not stored here.
There is no signature: the provenance stamp is the whole trust story (D2).
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traceback_runner.serialization import canonical_json_bytes

from . import __version__
from .identity import ALGORITHM_ID_PATTERN, METRIC_ID_PATTERN, SHA256_PATTERN

RESULT_SCHEMA = "traceback.lab.result.v1"
SCHEMA_FILE = Path(__file__).with_name("schemas") / f"{RESULT_SCHEMA}.schema.json"

Status = Literal["ok", "skipped", "failed"]

# Skip reasons the runner and algorithms may record (D7, D14, D22, D36).
SKIP_REASONS: tuple[str, ...] = (
    "depth",
    "derived_input",
    "disk",
    "missing_toolchain",
    "missing_asset",
    "no_pon",
    "invalid_asset",
    "missing_channel",
    "low_support",
    "missing_tags",
    "example_reference",
    "input_unavailable",
    "platform",
    "unsupported_input",
    "platform_unverified",
    "superseded_method",
    "derived_not_supported",
    "missing_input",
    "input_kind",
    "reference",
    "upstream",
    "unqualified_support",
    "missing_gpu",
    "model_not_ready",
)

Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN.pattern)]
_DATA_REF = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,200}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(_Strict):
    """The provenance stamp: who computed what from which input, when."""

    # Identifiers only: a provenance field can never carry a path (D34).
    tool: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    tool_version: str = Field(pattern=r"^[A-Za-z0-9_.+:-]{1,40}$")
    parameters_sha256: Sha256
    source_id: Sha256
    input_id: Sha256
    reference_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    timestamp_utc: str

    @field_validator("timestamp_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise ValueError("timestamp_utc must be YYYY-MM-DDTHH:MM:SSZ")
        return value


class Depth(_Strict):
    eligible_reads: int = Field(ge=0)
    index_mapped_upper_bound: int | None = Field(default=None, ge=0)
    mean_span_bp: float | None = Field(default=None, ge=0)
    approx_depth_x: float = Field(ge=0)


JSONScalar = float | int | str | bool | None


class Metric(_Strict):
    metric_id: str = Field(pattern=METRIC_ID_PATTERN.pattern)
    value: JSONScalar | list[JSONScalar] | dict[str, JSONScalar]
    unit: str | None = Field(default=None, max_length=40)
    ci: tuple[float, float] | None = None
    ci_level: float | None = Field(default=None, gt=0, lt=1)
    n: int | None = Field(default=None, ge=0)

    @field_validator("value")
    @classmethod
    def _finite(cls, value: Any) -> Any:
        items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("metric values must be finite (use null for none)")
        return value


class Panel(_Strict):
    panel_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    kind: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    data_ref: str

    @field_validator("data_ref")
    @classmethod
    def _relative(cls, value: str) -> str:
        if not _DATA_REF.fullmatch(value) or "//" in value:
            raise ValueError("data_ref must be a relative POSIX path with no '..'")
        return value


class Failure(_Strict):
    """A failed result's scrubbed evidence (D20, D34)."""

    first_line: str = Field(max_length=300)
    stderr_tail: list[Annotated[str, Field(max_length=300)]] = Field(max_length=50)
    exit_code: int | None = None
    timed_out: bool = False


class ResultEnvelope(_Strict):
    schema_: Literal["traceback.lab.result.v1"] = Field(default=RESULT_SCHEMA, alias="schema")
    algorithm_id: str = Field(pattern=ALGORITHM_ID_PATTERN.pattern)
    algorithm_version: str = Field(min_length=1, max_length=20)
    status: Status
    skip_reason: str | None = None
    skip_message: str | None = Field(default=None, max_length=300)
    provenance: Provenance
    depth: Depth | None = None
    metrics: list[Metric] = Field(default_factory=list)
    panels: list[Panel] = Field(default_factory=list)
    failure: Failure | None = None
    duration_s: float | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _status_shape(self) -> ResultEnvelope:
        if self.status == "skipped":
            if self.skip_reason not in SKIP_REASONS:
                raise ValueError(f"a skipped result needs a skip_reason in {SKIP_REASONS}")
        elif self.skip_reason is not None:
            raise ValueError("only a skipped result has a skip_reason")
        if self.status == "failed":
            if self.failure is None:
                raise ValueError("a failed result carries its scrubbed failure")
            if self.metrics or self.panels:
                raise ValueError("a failed algorithm shows no metrics (D7)")
        elif self.failure is not None:
            raise ValueError("only a failed result carries a failure")
        if self.status == "skipped" and (self.metrics or self.panels):
            raise ValueError("a skipped result carries no metrics")
        ids = [metric.metric_id for metric in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("metric ids are unique within a result")
        panels = [panel.panel_id for panel in self.panels]
        if len(panels) != len(set(panels)):
            raise ValueError("panel ids are unique within a result")
        return self

    def to_bytes(self) -> bytes:
        """Canonical JSON bytes as stored on disk."""

        return canonical_json_bytes(self.model_dump(mode="json", by_alias=True))

    @classmethod
    def from_bytes(cls, content: bytes) -> ResultEnvelope:
        return cls.model_validate(json.loads(content.decode("utf-8")))


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def provenance(
    *,
    tool: str,
    parameters_sha256: str,
    source_id: str,
    input_id: str,
    reference_id: str,
    timestamp_utc: str | None = None,
    tool_version: str = __version__,
) -> Provenance:
    return Provenance(
        tool=tool,
        tool_version=tool_version,
        parameters_sha256=parameters_sha256,
        source_id=source_id,
        input_id=input_id,
        reference_id=reference_id,
        timestamp_utc=timestamp_utc or utc_now(),
    )


def json_schema() -> dict[str, Any]:
    """The JSON Schema of the envelope, as committed under ``schemas/``."""

    return ResultEnvelope.model_json_schema(by_alias=True)


__all__ = [
    "Depth",
    "Failure",
    "Metric",
    "Panel",
    "Provenance",
    "RESULT_SCHEMA",
    "ResultEnvelope",
    "SCHEMA_FILE",
    "SKIP_REASONS",
    "json_schema",
    "provenance",
    "utc_now",
]
