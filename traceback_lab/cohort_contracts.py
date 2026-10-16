"""Immutable Lab cohort contracts and read-only metric projection (D8/D31/D35).

``parameters_sha256`` identifies one exact execution; ``cohort_compat_key``
identifies results that may be compared.  A member-specific PoN changes the
former without changing the latter.  The interpretation engine remains the
only source of sample-size gates and percentile statistics.
"""

from __future__ import annotations

import math
from collections import Counter
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_serializer, model_validator

from .identity import ALGORITHM_ID_PATTERN, METRIC_ID_PATTERN, SHA256_PATTERN
from .interpretation.engine import Cohort

COHORT_SCHEMA = "traceback.lab.cohort-generation.v1"


class CohortResult(BaseModel):
    """One selected, status-ok result pinned by exact execution identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm_id: str = Field(pattern=ALGORITHM_ID_PATTERN.pattern)
    parameters_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    cohort_compat_key: str = Field(pattern=SHA256_PATTERN.pattern)
    metrics: dict[str, StrictFloat | StrictInt | None]

    @model_validator(mode="after")
    def _metric_values(self) -> CohortResult:
        for name, value in self.metrics.items():
            if not METRIC_ID_PATTERN.fullmatch(name):
                raise ValueError(f"invalid metric id {name!r}")
            if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
                raise ValueError(f"metric {name!r} must be finite or null")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        return self

    @field_serializer("metrics")
    def _serialize_metrics(self, value: dict[str, StrictFloat | StrictInt | None]) -> dict[str, float | int | None]:
        return dict(value)


class CohortMember(BaseModel):
    """One source identity; donor independence requires an external policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=SHA256_PATTERN.pattern)
    input_id: str = Field(pattern=SHA256_PATTERN.pattern)
    aligner_identity: str = Field(min_length=1, max_length=160)
    results: tuple[CohortResult, ...]
    pon_asset_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def _unique_results(self) -> CohortMember:
        ids = [result.algorithm_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("one selected result per algorithm per member")
        return self


class CohortPon(BaseModel):
    """Pinned build identity for a full panel and its member-specific panels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ids: tuple[str, ...] = Field(min_length=10)
    reference_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    aligner_identity: str = Field(min_length=1, max_length=160)
    grid_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    counting_parameters_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    toolchain_lock_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    builder_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    gc_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    map_sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    centromere_sha256: str = Field(pattern=SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def _distinct_sources(self) -> CohortPon:
        if any(not SHA256_PATTERN.fullmatch(item) for item in self.source_ids):
            raise ValueError("PoN source identities must be SHA-256 digests")
        if tuple(sorted(set(self.source_ids))) != self.source_ids:
            raise ValueError("PoN source identities must be distinct and sorted")
        return self


class CohortGeneration(BaseModel):
    """An immutable snapshot selected through ``current.json`` by the store."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["traceback.lab.cohort-generation.v1"] = Field(default=COHORT_SCHEMA, alias="schema")
    cohort_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    version: StrictInt = Field(ge=1)
    kind: Literal["internal_cohort", "provisional_external"] = "internal_cohort"
    members: tuple[CohortMember, ...]
    pon_n: StrictInt = Field(default=0, ge=0)
    pon_asset_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)
    pon_build: CohortPon | None = None

    @model_validator(mode="after")
    def _independent_members(self) -> CohortGeneration:
        sources = [member.source_id for member in self.members]
        if len(sources) != len(set(sources)):
            raise ValueError("duplicate source_id would inflate cohort membership")
        if self.pon_n > len(self.members):
            raise ValueError("pon_n cannot exceed independent member count")
        if self.pon_build is None:
            if self.pon_n or self.pon_asset_sha256 is not None or any(member.pon_asset_sha256 for member in self.members):
                raise ValueError("PoN identities require build provenance")
        else:
            by_source = {member.source_id: member for member in self.members}
            if self.pon_asset_sha256 is None or self.pon_n != len(self.pon_build.source_ids):
                raise ValueError("full PoN identity and count must match build provenance")
            if any(source_id not in by_source or by_source[source_id].pon_asset_sha256 is None
                   for source_id in self.pon_build.source_ids):
                raise ValueError("every PoN source needs its own leave-one-out panel")
            if any(member.pon_asset_sha256 is not None and member.source_id not in self.pon_build.source_ids
                   for member in self.members):
                raise ValueError("only PoN sources may have leave-one-out panels")
        return self

    @property
    def percentile_n(self) -> int:
        """Distinct source IDs; metric-specific eligible n is projected."""

        return len(self.members)

    @property
    def version_id(self) -> str:
        return f"{self.cohort_id}:{self.version}"


class MetricProjection(BaseModel):
    """Eligible values and explicit exclusions for one comparable metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cohort_version: str
    kind: Literal["internal_cohort", "provisional_external"]
    values: tuple[float, ...]
    source_ids: tuple[str, ...]
    aligner_counts: dict[str, int]
    exclusions: dict[str, int]
    pon_n: int

    @model_validator(mode="after")
    def _freeze_counts(self) -> MetricProjection:
        object.__setattr__(self, "aligner_counts", MappingProxyType(dict(self.aligner_counts)))
        object.__setattr__(self, "exclusions", MappingProxyType(dict(self.exclusions)))
        return self

    @field_serializer("aligner_counts", "exclusions")
    def _serialize_counts(self, value: dict[str, int]) -> dict[str, int]:
        return dict(value)

    @property
    def percentile_n(self) -> int:
        return len(self.values)

    def interpretation_cohort(self) -> Cohort:
        """Feed the existing D8 engine after leave-one-out and eligibility."""

        return Cohort(self.values, version=self.cohort_version, kind=self.kind)


def project_metric(
    generation: CohortGeneration,
    *,
    algorithm_id: str,
    metric_id: str,
    cohort_compat_key: str,
    target_source_id: str | None = None,
) -> MetricProjection:
    """Select status-ok, compatible finite values, excluding self first.

    The target's own result is excluded by *sample* identity even when its
    input or exact execution hash changed.  The caller then passes the result
    to ``interpretation.engine.classify``; n gates apply to this reduced set.
    """

    if not ALGORITHM_ID_PATTERN.fullmatch(algorithm_id):
        raise ValueError("invalid algorithm_id")
    if not METRIC_ID_PATTERN.fullmatch(metric_id):
        raise ValueError("invalid metric_id")
    if not SHA256_PATTERN.fullmatch(cohort_compat_key):
        raise ValueError("invalid cohort_compat_key")
    if target_source_id is not None and not SHA256_PATTERN.fullmatch(target_source_id):
        raise ValueError("invalid target_source_id")
    values: list[float] = []
    sources: list[str] = []
    aligners: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    for member in generation.members:
        if member.source_id == target_source_id:
            exclusions["self"] += 1
            continue
        result = next((item for item in member.results if item.algorithm_id == algorithm_id), None)
        if result is None:
            exclusions["missing_result"] += 1
            continue
        if result.cohort_compat_key != cohort_compat_key:
            exclusions["incompatible"] += 1
            continue
        value = result.metrics.get(metric_id)
        if value is None:
            exclusions["missing_metric"] += 1
            continue
        values.append(value)
        sources.append(member.source_id)
        aligners[member.aligner_identity] += 1
    return MetricProjection(
        cohort_version=generation.version_id,
        kind=generation.kind,
        values=tuple(values),
        source_ids=tuple(sources),
        aligner_counts=dict(sorted(aligners.items())),
        exclusions=dict(sorted(exclusions.items())),
        pon_n=generation.pon_n,
    )


__all__ = [
    "COHORT_SCHEMA", "CohortGeneration", "CohortMember", "CohortPon", "CohortResult",
    "MetricProjection", "project_metric",
]
