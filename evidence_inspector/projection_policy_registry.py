"""Protected append-only registry of closed E12 source-value projection policies.

E12 projects standalone values from E07 fragment, E08 cell-origin and E09 CNA
artifacts only through a policy that names one family, one coordinate scheme,
the exact D02 measurement-definition tuple, the D05 measurement anchor, and
either a finite set of explicit components or the canonical-all-components
rule.  This registry stores those policies as immutable canonical objects
behind an opaque selector plus version.  Callers resolve only by
selector/version and cannot submit policy bytes, digests, coordinates or
subsets at resolve time.

The registry does not replay anything against live authority: a projection
policy is a closed, content-addressed rule, and no durable live authority
exists that could change its meaning (see ``docs/PROJECTION-POLICY-REGISTRY.md``).
Every read reparses and revalidates each committed object, checks the journal
chain and the rollback fence, and revalidates the head before return.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.cell_origin_explorer as e08_module
import evidence_inspector.fragment_explorer as e07_module
from evidence_inspector.cna_explorer import CnaSource, CoordinateGridLayer
from evidence_inspector.cohort_manifest import MeasurementAnchor
from evidence_inspector.fragment_explorer import FragmentQuantity, PanelId
from evidence_inspector.longitudinal_compatibility import (
    measurement_definition_sha256 as d02_measurement_definition_sha256,
)
from evidence_inspector.method_registry import (
    MethodDefinition,
    MethodFamily,
    MethodReference,
    QuantityId,
    RegistryContract,
    UnitId,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.method_registry import (
    method_definition_sha256 as e01_method_definition_sha256,
)
from evidence_inspector.registry_storage import (
    begin_staged_root as _begin_staged_root,
    commit_staged_root as _commit_staged_root,
    commit_staging_directory as _commit_staging_directory,
    discard_staged_root as _discard_staged_root,
    make_staging_directory as _make_staging_directory,
    recover_torn_journal_tail as _recover_torn_journal_tail,
    remove_owned_temporaries as _remove_owned_temporaries,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)
from traceback_runner.serialization import canonical_json_bytes

MAX_REGISTERED_POLICIES = 10_000
MAX_POLICY_VERSION = 1_000
MAX_FINITE_COMPONENTS = 8_192
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 32
MAX_OBJECT_GRAPH_NODES = 200_000
MAX_OBJECT_COLLECTION_ITEMS = MAX_FINITE_COMPONENTS
MAX_OBJECT_STRING_BYTES = 4096
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_COORDINATE = 2**62
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()

# Vocabularies are read from the E07/E08/E09 contracts, never restated, so a
# policy cannot drift from what the artifact can contain.
_E07_QUANTITY_ID = MappingProxyType(dict(e07_module._QUANTITY_ID))
_E07_UNIT = "unit_bp"
_E07_MAX_BINS = e07_module.MAX_EXPLORER_BINS
_E08_MAX_CONTRIBUTORS = e08_module.MAX_CONTRIBUTORS
_E08_SAFE_CONTRIBUTOR_ID = e08_module._safe_contributor_id

RegistryId = Annotated[
    str, StringConstraints(pattern=r"^projection_registry_[0-9a-f]{32}$")
]
PolicySelectorId = Annotated[
    str, StringConstraints(pattern=r"^projection_policy_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PolicyId = Annotated[
    str,
    StringConstraints(
        min_length=9, max_length=64, pattern=r"^projpol_[a-z0-9]+(?:_[a-z0-9]+)*$"
    ),
]
SafeIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    ),
]
ChromosomeId = Annotated[
    str, StringConstraints(pattern=r"^chr(?:[1-9]|1[0-9]|2[0-2])$")
]
PolicyVersion = Annotated[int, Field(ge=1, le=MAX_POLICY_VERSION, strict=True)]

# Words that name a value-derived choice.  No member of this module's closed
# rule, statistic or unit vocabularies may contain one (checked at import).
_VALUE_RANKED_LEXEMES = frozenset(
    {
        "argmax",
        "argmin",
        "best",
        "biggest",
        "bottom",
        "dominant",
        "greatest",
        "highest",
        "largest",
        "least",
        "lowest",
        "max",
        "maximum",
        "min",
        "minimum",
        "most",
        "rank",
        "ranked",
        "ranking",
        "smallest",
        "top",
        "worst",
    }
)


class ProjectionPolicyRegistryError(RuntimeError):
    """Sanitized registry failure."""


class ProjectionPolicyRegistryConflict(ProjectionPolicyRegistryError):
    pass


class ProjectionPolicyRegistryUnsafe(ProjectionPolicyRegistryError):
    pass


class ProjectionFamily(StrEnum):
    FRAGMENT = "fragment"
    CELL_ORIGIN = "cell_origin"
    CNA_CHROMOSOME = "cna_chromosome"
    CNA_SEGMENT = "cna_segment"


class ProjectionSelectionRule(StrEnum):
    """The only two selection rules; nothing chooses by value."""

    FINITE_COMPONENTS = "finite_components"
    CANONICAL_ALL_COMPONENTS = "canonical_all_components"


class FragmentStatistic(StrEnum):
    COUNT = "count"
    FRACTION = "fraction"


class CellOriginStatistic(StrEnum):
    ESTIMATED_FRACTION = "estimated_fraction"


class CnaChromosomeStatistic(StrEnum):
    ACCEPTED_READ_COUNT = "accepted_read_count"
    RELATIVE_DIPLOID_DOSAGE = "relative_diploid_dosage"
    LOG2_RATIO = "log2_ratio"


class CnaSegmentStatistic(StrEnum):
    MEDIAN_LOG2 = "median_log2"
    UPSTREAM_COPY_NUMBER = "upstream_copy_number"
    RETAINED_BIN_COUNT = "retained_bin_count"
    NATIVE_SPAN_BIN_COUNT = "native_span_bin_count"


class StatisticUnit(StrEnum):
    ALIGNMENT_COUNT = "unit_alignment_count"
    FRACTION = "unit_fraction"
    READ_COUNT = "unit_read_count"
    RELATIVE_DOSAGE = "unit_relative_dosage"
    LOG2_RATIO = "unit_log2_ratio"
    COPY_NUMBER = "unit_copy_number"
    BIN_COUNT = "unit_bin_count"


_STATISTIC_UNIT: MappingProxyType[StrEnum, StatisticUnit] = MappingProxyType(
    {
        FragmentStatistic.COUNT: StatisticUnit.ALIGNMENT_COUNT,
        FragmentStatistic.FRACTION: StatisticUnit.FRACTION,
        CellOriginStatistic.ESTIMATED_FRACTION: StatisticUnit.FRACTION,
        CnaChromosomeStatistic.ACCEPTED_READ_COUNT: StatisticUnit.READ_COUNT,
        CnaChromosomeStatistic.RELATIVE_DIPLOID_DOSAGE: StatisticUnit.RELATIVE_DOSAGE,
        CnaChromosomeStatistic.LOG2_RATIO: StatisticUnit.LOG2_RATIO,
        CnaSegmentStatistic.MEDIAN_LOG2: StatisticUnit.LOG2_RATIO,
        CnaSegmentStatistic.UPSTREAM_COPY_NUMBER: StatisticUnit.COPY_NUMBER,
        CnaSegmentStatistic.RETAINED_BIN_COUNT: StatisticUnit.BIN_COUNT,
        CnaSegmentStatistic.NATIVE_SPAN_BIN_COUNT: StatisticUnit.BIN_COUNT,
    }
)


def _statistic_order(statistic: StrEnum) -> int:
    return tuple(type(statistic)).index(statistic)


def _identifier_words(value: str) -> tuple[str, ...]:
    """Split an identifier into lowercase letter-only words.

    Words break at every non-letter (digits, ``_``, ``.``, ``:``, ``-``) and at
    a lower-to-upper camel-case boundary, so ``top1``, ``rank-1``, ``max2`` and
    ``mostChanged`` all expose their ranking word.
    """

    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    return tuple(item for item in re.split(r"[^a-z]+", spaced.lower()) if item)


def _reject_value_ranked_identifier(value: str, label: str) -> None:
    """Reject one of this module's own vocabulary words that names a ranking.

    It is applied only to the closed rule/statistic/unit enums below.  It is
    deliberately not applied to upstream-registered identifiers (E08
    contributor and atlas IDs, E09 contigs): those are biology vocabulary
    matched by exact equality, so a word check there could only reject real
    names (``BEST4_enterocyte``) without making any selection value-ranked.
    """

    if any(word in _VALUE_RANKED_LEXEMES for word in _identifier_words(value)):
        raise ValueError(f"{label} names a value-ranked selection")


# The rule and statistic vocabularies are closed enums; this guard keeps a later
# edit from adding a value-ranked member to them without failing at import.
_CLOSED_VOCABULARIES: tuple[type[StrEnum], ...] = (
    ProjectionSelectionRule,
    FragmentStatistic,
    CellOriginStatistic,
    CnaChromosomeStatistic,
    CnaSegmentStatistic,
    StatisticUnit,
)


def _require_closed_vocabularies(vocabularies: tuple[type[StrEnum], ...]) -> None:
    for vocabulary in vocabularies:
        for member in vocabulary:
            _reject_value_ranked_identifier(member.value, vocabulary.__name__)
            if (
                vocabulary not in (StatisticUnit, ProjectionSelectionRule)
                and member not in _STATISTIC_UNIT
            ):
                raise ValueError(f"{vocabulary.__name__} member has no controlled unit")


_require_closed_vocabularies(_CLOSED_VOCABULARIES)


def cna_coordinate_grid_sha256(grid: CoordinateGridLayer) -> str:
    """Digest one E09 coordinate-grid layer with E09's own canonical encoding."""

    return hashlib.sha256(canonical_json_bytes(grid)).hexdigest()


class ProjectionMeasurementBinding(RegistryContract):
    """The exact D02 measurement-definition tuple, derived from one E01 method."""

    schema_version: Literal["traceback.e12-projection-measurement-binding.v1"] = (
        "traceback.e12-projection-measurement-binding.v1"
    )
    method_definition: MethodDefinition
    method_ref: MethodReference
    method_definition_sha256: Sha256
    quantity_id: QuantityId
    unit: UnitId
    measurement_definition_sha256: Sha256

    @model_validator(mode="after")
    def exact_d02_tuple(self) -> ProjectionMeasurementBinding:
        definition = self.method_definition
        if self.method_ref != definition.method_ref:
            raise ValueError("projection measurement method does not match E01")
        if self.method_definition_sha256 != e01_method_definition_sha256(definition):
            raise ValueError("projection measurement method digest does not match E01")
        if self.quantity_id != definition.quantity_id or self.unit != definition.unit:
            raise ValueError("projection measurement quantity or unit does not match E01")
        if self.measurement_definition_sha256 != d02_measurement_definition_sha256(
            self.method_ref,
            self.method_definition_sha256,
            self.quantity_id,
            self.unit,
        ):
            raise ValueError("projection measurement does not bind the D02 definition")
        return self


def _check_common(
    policy: Any,
    *,
    method_family: MethodFamily,
    statistic_type: type[StrEnum],
    component_count: int,
) -> None:
    if policy.measurement.method_definition.family != method_family:
        raise ValueError("projection family does not match the E01 method family")
    if (
        policy.measurement_anchor.measurement_definition_sha256
        != policy.measurement.method_definition_sha256
    ):
        # Mirrors the D06 import rule: a D05 anchor names its E01 method.
        raise ValueError("D05 measurement anchor does not bind the policy method")
    statistics = policy.all_component_statistics
    if policy.selection_rule == ProjectionSelectionRule.FINITE_COMPONENTS:
        if component_count < 1 or statistics:
            raise ValueError("finite projection policy requires explicit components only")
    elif policy.selection_rule != ProjectionSelectionRule.CANONICAL_ALL_COMPONENTS:
        raise ValueError("projection selection rule is not registered")
    else:
        if component_count != 0 or not statistics:
            raise ValueError(
                "canonical-all-components policy requires statistics and no subset"
            )
        if any(type(item) is not statistic_type for item in statistics) or list(
            statistics
        ) != sorted(set(statistics), key=_statistic_order):
            raise ValueError("canonical-all-components statistics are not canonical")


def _check_component_unit(statistic: StrEnum, unit: StatisticUnit) -> None:
    if _STATISTIC_UNIT[statistic] != unit:
        raise ValueError("projection statistic unit does not match its statistic")


class FragmentBinCoordinate(RegistryContract):
    """One E02/E07 chart-row index and its exact half-open base-pair bounds.

    ``upper_exclusive`` is null only for the explicitly unbounded final bin.
    """

    bin_index: int = Field(ge=0, lt=_E07_MAX_BINS, strict=True)
    lower_inclusive: int = Field(ge=0, le=MAX_COORDINATE, strict=True)
    upper_exclusive: int | None = Field(gt=0, le=MAX_COORDINATE, strict=True)

    @model_validator(mode="after")
    def half_open(self) -> FragmentBinCoordinate:
        if (
            self.upper_exclusive is not None
            and self.lower_inclusive >= self.upper_exclusive
        ):
            raise ValueError("fragment bin bounds must be half-open")
        return self


class FragmentProjectionComponent(RegistryContract):
    statistic: FragmentStatistic
    statistic_unit: StatisticUnit
    bin: FragmentBinCoordinate

    @model_validator(mode="after")
    def exact_unit(self) -> FragmentProjectionComponent:
        _check_component_unit(self.statistic, self.statistic_unit)
        return self


class FragmentProjectionPolicyV1(RegistryContract):
    """E07: one panel, one fragment quantity, explicit chart bins and statistics."""

    schema_version: Literal["traceback.e12-fragment-projection-policy.v1"] = (
        "traceback.e12-fragment-projection-policy.v1"
    )
    family: Literal[ProjectionFamily.FRAGMENT] = ProjectionFamily.FRAGMENT
    coordinate_scheme: Literal["e07_panel_chart_bin.v1"] = "e07_panel_chart_bin.v1"
    policy_id: PolicyId
    version: PolicyVersion
    measurement: ProjectionMeasurementBinding
    measurement_anchor: MeasurementAnchor
    fragment_quantity: FragmentQuantity
    panel: PanelId
    selection_rule: ProjectionSelectionRule
    components: tuple[FragmentProjectionComponent, ...] = Field(
        default=(), max_length=MAX_FINITE_COMPONENTS
    )
    all_component_statistics: tuple[FragmentStatistic, ...] = Field(
        default=(), max_length=len(FragmentStatistic)
    )

    @model_validator(mode="after")
    def exact_policy(self) -> FragmentProjectionPolicyV1:
        _check_common(
            self,
            method_family=MethodFamily.FRAGMENT_MEASUREMENT,
            statistic_type=FragmentStatistic,
            component_count=len(self.components),
        )
        if (
            self.measurement.quantity_id != _E07_QUANTITY_ID[self.fragment_quantity]
            or self.measurement.unit != _E07_UNIT
        ):
            raise ValueError("fragment quantity or base-pair unit differs from E07")
        keys = [
            (item.bin.bin_index, _statistic_order(item.statistic))
            for item in self.components
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("fragment components must be uniquely sorted")
        bins: dict[int, FragmentBinCoordinate] = {}
        for item in self.components:
            if bins.setdefault(item.bin.bin_index, item.bin) != item.bin:
                raise ValueError("one fragment bin index cannot name two boundaries")
        ordered = [bins[index] for index in sorted(bins)]
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (
                previous.upper_exclusive is None
                or previous.upper_exclusive > current.lower_inclusive
                or (
                    current.bin_index == previous.bin_index + 1
                    and previous.upper_exclusive != current.lower_inclusive
                )
            ):
                raise ValueError("fragment bins must be ordered, disjoint and contiguous")
        return self


class CellOriginProjectionComponent(RegistryContract):
    statistic: CellOriginStatistic
    statistic_unit: StatisticUnit
    contributor_id: SafeIdentifier

    @model_validator(mode="after")
    def exact_component(self) -> CellOriginProjectionComponent:
        _check_component_unit(self.statistic, self.statistic_unit)
        if not _E08_SAFE_CONTRIBUTOR_ID(self.contributor_id):
            raise ValueError("contributor ID contains a reserved privacy term")
        return self


class CellOriginProjectionPolicyV1(RegistryContract):
    """E08: registered atlas contributors, never the view's value rank."""

    schema_version: Literal["traceback.e12-cell-origin-projection-policy.v1"] = (
        "traceback.e12-cell-origin-projection-policy.v1"
    )
    family: Literal[ProjectionFamily.CELL_ORIGIN] = ProjectionFamily.CELL_ORIGIN
    coordinate_scheme: Literal["e08_registered_atlas_contributor.v1"] = (
        "e08_registered_atlas_contributor.v1"
    )
    policy_id: PolicyId
    version: PolicyVersion
    measurement: ProjectionMeasurementBinding
    measurement_anchor: MeasurementAnchor
    atlas_id: SafeIdentifier
    atlas_sha256: Sha256
    selection_rule: ProjectionSelectionRule
    components: tuple[CellOriginProjectionComponent, ...] = Field(
        default=(), max_length=_E08_MAX_CONTRIBUTORS
    )
    all_component_statistics: tuple[CellOriginStatistic, ...] = Field(
        default=(), max_length=len(CellOriginStatistic)
    )

    @model_validator(mode="after")
    def exact_policy(self) -> CellOriginProjectionPolicyV1:
        _check_common(
            self,
            method_family=MethodFamily.CELL_ORIGIN,
            statistic_type=CellOriginStatistic,
            component_count=len(self.components),
        )
        keys = [
            (item.contributor_id, _statistic_order(item.statistic))
            for item in self.components
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("cell-origin components must be uniquely sorted by ID")
        return self


class CnaChromosomeProjectionComponent(RegistryContract):
    statistic: CnaChromosomeStatistic
    statistic_unit: StatisticUnit
    chromosome: ChromosomeId

    @model_validator(mode="after")
    def exact_unit(self) -> CnaChromosomeProjectionComponent:
        _check_component_unit(self.statistic, self.statistic_unit)
        return self


def _check_cna_grid(
    grid: CoordinateGridLayer, digest: str, source: CnaSource
) -> None:
    if grid.source != source:
        raise ValueError("CNA coordinate grid belongs to the other E09 source")
    if cna_coordinate_grid_sha256(grid) != digest:
        raise ValueError("CNA coordinate-grid digest is invalid")


class CnaChromosomeProjectionPolicyV1(RegistryContract):
    """E09 ``dosage_qc`` whole-chromosome summaries on one dosage grid."""

    schema_version: Literal["traceback.e12-cna-chromosome-projection-policy.v1"] = (
        "traceback.e12-cna-chromosome-projection-policy.v1"
    )
    family: Literal[ProjectionFamily.CNA_CHROMOSOME] = ProjectionFamily.CNA_CHROMOSOME
    coordinate_scheme: Literal["e09_dosage_qc_chromosome.v1"] = (
        "e09_dosage_qc_chromosome.v1"
    )
    cna_source: Literal[CnaSource.DOSAGE_QC] = CnaSource.DOSAGE_QC
    policy_id: PolicyId
    version: PolicyVersion
    measurement: ProjectionMeasurementBinding
    measurement_anchor: MeasurementAnchor
    coordinate_grid: CoordinateGridLayer
    coordinate_grid_sha256: Sha256
    selection_rule: ProjectionSelectionRule
    components: tuple[CnaChromosomeProjectionComponent, ...] = Field(
        default=(), max_length=22 * len(CnaChromosomeStatistic)
    )
    all_component_statistics: tuple[CnaChromosomeStatistic, ...] = Field(
        default=(), max_length=len(CnaChromosomeStatistic)
    )

    @model_validator(mode="after")
    def exact_policy(self) -> CnaChromosomeProjectionPolicyV1:
        _check_common(
            self,
            method_family=MethodFamily.COPY_NUMBER,
            statistic_type=CnaChromosomeStatistic,
            component_count=len(self.components),
        )
        _check_cna_grid(
            self.coordinate_grid, self.coordinate_grid_sha256, CnaSource.DOSAGE_QC
        )
        declared = set(self.coordinate_grid.contig_order)
        if any(item.chromosome not in declared for item in self.components):
            raise ValueError("chromosome is not declared by the dosage grid")
        keys = [
            (int(item.chromosome[3:]), _statistic_order(item.statistic))
            for item in self.components
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("chromosome components must be uniquely sorted")
        return self


class CnaSegmentCoordinate(RegistryContract):
    """One E09 segment identity: index and zero-based half-open interval."""

    segment_index: int = Field(ge=0, lt=100_000, strict=True)
    contig: SafeIdentifier
    start: int = Field(ge=0, le=MAX_COORDINATE, strict=True)
    end: int = Field(gt=0, le=MAX_COORDINATE, strict=True)

    @model_validator(mode="after")
    def half_open(self) -> CnaSegmentCoordinate:
        if self.start >= self.end:
            raise ValueError("segment bounds must be zero-based half-open")
        return self


class CnaSegmentProjectionComponent(RegistryContract):
    statistic: CnaSegmentStatistic
    statistic_unit: StatisticUnit
    segment: CnaSegmentCoordinate

    @model_validator(mode="after")
    def exact_unit(self) -> CnaSegmentProjectionComponent:
        _check_component_unit(self.statistic, self.statistic_unit)
        return self


class CnaSegmentProjectionPolicyV1(RegistryContract):
    """E09 ``segmented_cna`` segments on one segmented grid."""

    schema_version: Literal["traceback.e12-cna-segment-projection-policy.v1"] = (
        "traceback.e12-cna-segment-projection-policy.v1"
    )
    family: Literal[ProjectionFamily.CNA_SEGMENT] = ProjectionFamily.CNA_SEGMENT
    coordinate_scheme: Literal["e09_segmented_cna_segment.v1"] = (
        "e09_segmented_cna_segment.v1"
    )
    cna_source: Literal[CnaSource.SEGMENTED_CNA] = CnaSource.SEGMENTED_CNA
    policy_id: PolicyId
    version: PolicyVersion
    measurement: ProjectionMeasurementBinding
    measurement_anchor: MeasurementAnchor
    coordinate_grid: CoordinateGridLayer
    coordinate_grid_sha256: Sha256
    selection_rule: ProjectionSelectionRule
    components: tuple[CnaSegmentProjectionComponent, ...] = Field(
        default=(), max_length=MAX_FINITE_COMPONENTS
    )
    all_component_statistics: tuple[CnaSegmentStatistic, ...] = Field(
        default=(), max_length=len(CnaSegmentStatistic)
    )

    @model_validator(mode="after")
    def exact_policy(self) -> CnaSegmentProjectionPolicyV1:
        _check_common(
            self,
            method_family=MethodFamily.COPY_NUMBER,
            statistic_type=CnaSegmentStatistic,
            component_count=len(self.components),
        )
        _check_cna_grid(
            self.coordinate_grid, self.coordinate_grid_sha256, CnaSource.SEGMENTED_CNA
        )
        order = {
            contig: index for index, contig in enumerate(self.coordinate_grid.contig_order)
        }
        if any(item.segment.contig not in order for item in self.components):
            raise ValueError("segment contig is not declared by the segmented grid")
        keys = [
            (item.segment.segment_index, _statistic_order(item.statistic))
            for item in self.components
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("segment components must be uniquely sorted")
        segments: dict[int, CnaSegmentCoordinate] = {}
        for item in self.components:
            if (
                segments.setdefault(item.segment.segment_index, item.segment)
                != item.segment
            ):
                raise ValueError("one segment index cannot name two intervals")
        ordered = [segments[index] for index in sorted(segments)]
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_key = (order[previous.contig], previous.end)
            current_key = (order[current.contig], current.start)
            if previous_key > current_key:
                raise ValueError("segments must follow grid order without overlap")
        return self


ProjectionPolicy = Annotated[
    FragmentProjectionPolicyV1
    | CellOriginProjectionPolicyV1
    | CnaChromosomeProjectionPolicyV1
    | CnaSegmentProjectionPolicyV1,
    Field(discriminator="family"),
]
_POLICY_TYPES: tuple[type[RegistryContract], ...] = (
    FragmentProjectionPolicyV1,
    CellOriginProjectionPolicyV1,
    CnaChromosomeProjectionPolicyV1,
    CnaSegmentProjectionPolicyV1,
)


def projection_policy_sha256(policy: RegistryContract) -> str:
    """Return the registered digest of one exact family policy."""

    if type(policy) not in _POLICY_TYPES:
        raise TypeError("projection policy must be one exact family policy")
    return hashlib.sha256(
        b"traceback-e12-projection-policy-v1\0" + canonical_contract_bytes(policy)
    ).hexdigest()


def _policy_component_count(policy: RegistryContract) -> int:
    return len(policy.components)  # type: ignore[attr-defined]


class ProjectionPolicyRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.e12-projection-registry-metadata.v1"] = (
        "traceback.e12-projection-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    ProjectionPolicyRegistryMetadata
)


class RegisteredProjectionPolicyObject(RegistryContract):
    """One stored policy bound to the registry identity that accepted it."""

    schema_version: Literal["traceback.e12-registered-projection-policy.v1"] = (
        "traceback.e12-registered-projection-policy.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    policy: ProjectionPolicy


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredProjectionPolicyObject
)


def _selector_id(epoch: str, policy_id: str) -> str:
    digest = hashlib.sha256(
        b"traceback-e12-projection-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + policy_id.encode("ascii")
    ).hexdigest()
    return f"projection_policy_{digest[:40]}"


class ProjectionPolicyJournalEntry(RegistryContract):
    schema_version: Literal["traceback.e12-projection-journal-entry.v1"] = (
        "traceback.e12-projection-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_POLICIES, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class ProjectionPolicyRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.e12-projection-registration-receipt.v1"] = (
        "traceback.e12-projection-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    policy_version: PolicyVersion
    object_sha256: Sha256
    policy_sha256: Sha256
    family: ProjectionFamily


class ResolvedProjectionPolicy(RegistryContract):
    """Protected policy resolved by selector/version under one registry head.

    ``live_authority_replayed`` is literally false: nothing live governs a
    closed policy.  Consumers bind ``measurement_anchor`` to the resolved D05
    manifest and ``measurement`` to the E04/E06 source themselves.
    """

    schema_version: Literal["traceback.e12-resolved-projection-policy.v1"] = (
        "traceback.e12-resolved-projection-policy.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    selector_id: PolicySelectorId
    policy_version: PolicyVersion
    object_sha256: Sha256
    policy_sha256: Sha256
    policy: ProjectionPolicy
    live_authority_replayed: Literal[False] = False
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> ResolvedProjectionPolicy:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.policy.policy_id
        ):
            raise ValueError("resolved projection selector does not match its policy")
        if self.policy_version != self.policy.version:
            raise ValueError("resolved projection version does not match its policy")
        if self.policy_sha256 != projection_policy_sha256(self.policy):
            raise ValueError("resolved projection policy digest is invalid")
        stored = RegisteredProjectionPolicyObject(
            registry_id=self.registry_id,
            registry_epoch_sha256=self.registry_epoch_sha256,
            policy=self.policy,
        )
        if self.object_sha256 != hashlib.sha256(
            registered_projection_object_bytes(stored)
        ).hexdigest():
            raise ValueError("resolved projection object digest is invalid")
        return self


class ProjectionPolicySelectorRecord(RegistryContract):
    """Privacy-safe row: digests and controlled states, no policy content."""

    schema_version: Literal["traceback.e12-projection-selector-record.v1"] = (
        "traceback.e12-projection-selector-record.v1"
    )
    selector_id: PolicySelectorId
    policy_version: PolicyVersion
    latest_version: bool
    object_sha256: Sha256
    policy_sha256: Sha256
    family: ProjectionFamily
    selection_rule: ProjectionSelectionRule
    component_count: int = Field(ge=0, le=MAX_FINITE_COMPONENTS, strict=True)
    measurement_definition_sha256: Sha256
    anchor_definition_sha256: Sha256

    @model_validator(mode="after")
    def rule_matches_count(self) -> ProjectionPolicySelectorRecord:
        if (self.selection_rule == ProjectionSelectionRule.FINITE_COMPONENTS) != (
            self.component_count > 0
        ):
            raise ValueError("projection selector row count does not match its rule")
        return self


class ProjectionPolicySelectorPage(RegistryContract):
    schema_version: Literal["traceback.e12-projection-selector-page.v1"] = (
        "traceback.e12-projection-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    records: tuple[ProjectionPolicySelectorRecord, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: PolicySelectorId | None
    next_after_policy_version: int | None = Field(
        default=None, ge=1, le=MAX_POLICY_VERSION
    )


class ProjectionPolicyBackupObject(RegistryContract):
    schema_version: Literal["traceback.e12-projection-backup-object.v1"] = (
        "traceback.e12-projection-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class ProjectionPolicyBackup(RegistryContract):
    schema_version: Literal["traceback.e12-projection-backup.v1"] = (
        "traceback.e12-projection-backup.v1"
    )
    metadata: ProjectionPolicyRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_POLICIES)
    state_head_sha256: Sha256
    journal: tuple[ProjectionPolicyJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_POLICIES
    )
    objects: tuple[ProjectionPolicyBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_POLICIES
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(ProjectionPolicyBackup)
_POLICY_MODEL_TYPES, _POLICY_ENUM_TYPES = contract_type_graph(*_POLICY_TYPES)


def require_projection_policy_binding(
    resolved: ResolvedProjectionPolicy,
    *,
    measurement_anchor: MeasurementAnchor,
    method_ref: MethodReference,
    method_definition_sha256: str,
    quantity_id: str,
    unit: str,
) -> None:
    """Require a resolved policy to bind the exact D05 anchor and D02 tuple.

    The E12 builder calls this with the anchor of the live-resolved D05
    manifest and the method/quantity/unit of the E04/E06 source; the registry
    itself has no live cohort or source to compare against.
    """

    if type(resolved) is not ResolvedProjectionPolicy:
        raise ProjectionPolicyRegistryConflict("projection policy is not resolved")
    policy = resolved.policy
    if (
        type(measurement_anchor) is not MeasurementAnchor
        or type(method_ref) is not MethodReference
        or policy.measurement_anchor != measurement_anchor
        or policy.measurement.method_ref != method_ref
        or policy.measurement.method_definition_sha256 != method_definition_sha256
        or policy.measurement.quantity_id != quantity_id
        or policy.measurement.unit != unit
    ):
        raise ProjectionPolicyRegistryConflict(
            "projection policy does not bind the requested measurement"
        )


def registered_projection_object_bytes(value: RegisteredProjectionPolicyObject) -> bytes:
    """Return exact bounded canonical bytes for one stored policy object."""

    return exact_model_bytes(
        value,
        RegisteredProjectionPolicyObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_projection_object_from_bytes(
    content: bytes,
) -> RegisteredProjectionPolicyObject:
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredProjectionPolicyObject.model_validate_json(content)
        if registered_projection_object_bytes(value) != content:
            raise ValueError("registered projection object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered projection object is not canonical") from None


def _capture_policy(policy: object) -> RegistryContract:
    """Capture one caller policy as exact bounded bytes, then revalidate them."""

    policy_type = type(policy)
    if policy_type not in _POLICY_TYPES:
        raise TypeError("projection policy must be one exact family policy")
    content = exact_model_bytes(
        policy,
        policy_type,  # type: ignore[arg-type]
        model_types=_POLICY_MODEL_TYPES,
        enum_types=_POLICY_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )
    return policy_type.model_validate_json(content)  # type: ignore[attr-defined]


def _canonical_backup_bytes(backup: ProjectionPolicyBackup) -> bytes:
    return exact_model_bytes(
        backup,
        ProjectionPolicyBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_POLICIES,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("projection policy registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("projection policy registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "projection policy registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("projection policy registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("projection policy registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_registry_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 52
        and value.startswith("projection_registry_")
        and all(character in "0123456789abcdef" for character in value[20:])
    )


def _is_policy_selector(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 58
        and value.startswith("projection_policy_")
        and all(character in "0123456789abcdef" for character in value[18:])
    )


def _is_policy_version(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_POLICY_VERSION


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry object exceeds its bound"
            )
        chunks.append(chunk)


def _publish_file(directory_fd: int, name: str, content: bytes) -> None:
    temporary = f".tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)


def _read_exact_object(directory_fd: int, digest: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            f"{digest}.json",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry object digest is invalid"
        )
    return content


def _journal_entry_sha256(entry: ProjectionPolicyJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-e12-projection-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: ProjectionPolicyRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-e12-projection-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> ProjectionPolicyJournalEntry:
    placeholder = ProjectionPolicyJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return ProjectionPolicyJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_policy_history(
    metadata: ProjectionPolicyRegistryMetadata,
    values: list[RegisteredProjectionPolicyObject],
) -> None:
    """In journal order every object binds this registry, and each selector's
    versions append 1, 2, ... N within one family and coordinate scheme."""

    counts: dict[str, int] = {}
    families: dict[str, tuple[str, str]] = {}
    for value in values:
        if (
            value.registry_id != metadata.registry_id
            or value.registry_epoch_sha256 != metadata.registry_epoch_sha256
        ):
            raise ValueError("projection policy object binds another registry")
        policy = value.policy
        selector_id = _selector_id(metadata.registry_epoch_sha256, policy.policy_id)
        expected = counts.get(selector_id, 0) + 1
        if policy.version != expected:
            raise ValueError("projection policy versions are not contiguous")
        scheme = (policy.family.value, policy.coordinate_scheme)
        if families.setdefault(selector_id, scheme) != scheme:
            raise ValueError("projection selector cannot change family or scheme")
        counts[selector_id] = expected


def _validate_backup(backup: ProjectionPolicyBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    values: dict[str, RegisteredProjectionPolicyObject] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            values[item.object_sha256] = registered_projection_object_from_bytes(
                content
            )
        except (UnicodeError, ValueError):
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry backup digest is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup state is invalid"
        )
    try:
        _validate_policy_history(
            backup.metadata,
            [values[entry.object_sha256] for entry in backup.journal],
        )
    except ValueError:
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup history is invalid"
        ) from None


def projection_policy_backup_from_bytes(content: bytes) -> ProjectionPolicyBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_POLICIES,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = ProjectionPolicyBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("projection policy registry backup is not canonical")
    except (TypeError, ValueError):
        raise ProjectionPolicyRegistryConflict(
            "projection policy registry backup is invalid"
        ) from None
    _validate_backup(backup)
    return backup


def _remove_partial_restore(
    parent_fd: int | None, name: str, root_fd: int, objects_fd: int | None
) -> None:
    """Remove only the files a failed restore created, then its root."""

    try:
        if objects_fd is not None:
            for entry in os.listdir(objects_fd):
                os.unlink(entry, dir_fd=objects_fd)
            os.rmdir("objects", dir_fd=root_fd)
        for entry in os.listdir(root_fd):
            try:
                os.unlink(entry, dir_fd=root_fd)
            except OSError:
                os.rmdir(entry, dir_fd=root_fd)
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _registry_instance_snapshot(
    registry: ProjectionPolicyRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_root_identity",
        "_objects_identity",
        "_lock_identity",
        "_journal_identity",
        "_metadata_identity",
        "_process_lock",
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    if type(instance) is not dict or any(name not in instance for name in required):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            ProjectionPolicyRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            ) from None
        root_identity = (root_observed.st_dev, root_observed.st_ino)
        metadata_identity = (metadata_observed.st_dev, metadata_observed.st_ino)
        derived_head_key = (
            root_identity[0],
            root_identity[1],
            metadata.registry_id,
            metadata.registry_epoch_sha256,
        )
        if (
            instance["_root_identity"] != root_identity
            or instance["_metadata_identity"] != metadata_identity
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"] != derived_head_key
        ):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry authority state changed"
            )
    return (
        id(instance["root"]),
        instance["_root_identity"],
        instance["_objects_identity"],
        instance["_lock_identity"],
        instance["_journal_identity"],
        instance["_metadata_identity"],
        id(instance["_process_lock"]),
        metadata_bytes,
        instance["_genesis_head_sha256"],
        instance["_head_key"],
        instance["_trusted_head_sha256"],
    )


def _seal_registry_instance(registry: ProjectionPolicyRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class ProjectionPolicyRegistry:
    """Descriptor-relative immutable projection-policy publication."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "backup_bytes",
            "close",
            "list_selectors",
            "register_policy",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
        expected_state_head_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values):
            if (
                any(item is None for item in expected_values)
                or not _is_registry_id(expected_registry_id)
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry expected identity or head is invalid"
                )
        self.root = _snapshot_path(root)
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        final_root = self.root
        staged_root: Path | None = None
        try:
            # A new root is built in a hidden sibling and published with one
            # rename, so an interrupted creation never leaves a half-built
            # root at the final path.
            staged_root = _begin_staged_root(final_root)
            root_created = staged_root is not None
            if staged_root is not None:
                self.root = staged_root
            root_lstat = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or stat.S_IMODE(root_lstat.st_mode) != 0o700
                or root_lstat.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry root must be private"
                )
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            self._root_fd = os.open(self.root, flags)
            bound = os.fstat(self._root_fd)
            if (bound.st_dev, bound.st_ino) != (root_lstat.st_dev, root_lstat.st_ino):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry root changed"
                )
            self._root_identity = (bound.st_dev, bound.st_ino)
            if root_created:
                os.mkdir("objects", 0o700, dir_fd=self._root_fd)
            self._objects_fd = os.open("objects", flags, dir_fd=self._root_fd)
            objects = os.fstat(self._objects_fd)
            if (
                not stat.S_ISDIR(objects.st_mode)
                or stat.S_IMODE(objects.st_mode) != 0o700
                or objects.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry objects are unsafe"
                )
            self._objects_identity = (objects.st_dev, objects.st_ino)
            self._lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR
                | (os.O_CREAT if root_created else 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            lock_metadata = os.fstat(self._lock_fd)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
                or lock_metadata.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry lock is unsafe"
                )
            self._lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
            self._journal_fd = os.open(
                "registry-journal.jsonl",
                os.O_RDWR
                | (os.O_CREAT if root_created else 0)
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            journal_metadata = os.fstat(self._journal_fd)
            if (
                not stat.S_ISREG(journal_metadata.st_mode)
                or stat.S_IMODE(journal_metadata.st_mode) != 0o600
                or journal_metadata.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PP_LOCK(self, exclusive=True):
                self._metadata = _PP_LOAD_OR_CREATE_METADATA(
                    self, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._root_identity[0],
                    self._root_identity[1],
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _PP_RECOVER_TEMPORARY_OBJECTS(self)
                _, head = _PP_LOAD_STATE(self, check_trusted_head=False)
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise ProjectionPolicyRegistryUnsafe(
                            "new projection policy registry cannot inherit an "
                            "expected identity"
                        )
                elif any(item is None for item in expected_values):
                    raise ProjectionPolicyRegistryUnsafe(
                        "projection policy registry expected identity and head are "
                        "required"
                    )
                if not root_created and expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise ProjectionPolicyRegistryUnsafe(
                        "projection policy registry expected identity or head is "
                        "invalid"
                    )
                if staged_root is not None:
                    _commit_staged_root(staged_root, final_root, self._root_fd)
                    self.root = final_root
                    staged_root = None
                self._trusted_head_sha256 = head
                _PP_ACCEPT_OBSERVED_HEAD(
                    self, _PP_LOAD_JOURNAL(self), head, check_instance=False
                )
                _seal_registry_instance(self)
        except BaseException:
            # Construction has not installed the instance seal yet, so cleanup
            # cannot pass through the public integrity-checked close boundary.
            for name in (
                "_journal_fd",
                "_metadata_fd",
                "_lock_fd",
                "_objects_fd",
                "_root_fd",
            ):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)
            if staged_root is not None:
                _discard_staged_root(staged_root)
            raise

    def close(self) -> None:
        _require_registry_integrity(self)
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            return
        with lock:
            for name in (
                "_journal_fd",
                "_metadata_fd",
                "_lock_fd",
                "_objects_fd",
                "_root_fd",
            ):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)

    def __enter__(self) -> ProjectionPolicyRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _PP_CLOSE(self)

    def __del__(self) -> None:
        try:
            _PP_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry is closed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _PP_VALIDATE_STORAGE(self)
                yield
                _PP_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise ProjectionPolicyRegistryUnsafe("projection policy registry is closed")
        try:
            root_path = os.stat(self.root, follow_symlinks=False)
            root_bound = os.fstat(self._root_fd)
            objects_path = os.stat(
                "objects", dir_fd=self._root_fd, follow_symlinks=False
            )
            objects_bound = os.fstat(self._objects_fd)
            lock_path = os.stat(
                ".registry.lock", dir_fd=self._root_fd, follow_symlinks=False
            )
            lock_bound = os.fstat(self._lock_fd)
            journal_path = os.stat(
                "registry-journal.jsonl",
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            journal_bound = os.fstat(self._journal_fd)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (root_bound.st_dev, root_bound.st_ino) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
            or not stat.S_ISDIR(objects_path.st_mode)
            or (objects_path.st_dev, objects_path.st_ino) != self._objects_identity
            or (objects_bound.st_dev, objects_bound.st_ino) != self._objects_identity
            or stat.S_IMODE(objects_bound.st_mode) != 0o700
            or objects_bound.st_uid != os.geteuid()
            or not stat.S_ISREG(lock_path.st_mode)
            or (lock_path.st_dev, lock_path.st_ino) != self._lock_identity
            or (lock_bound.st_dev, lock_bound.st_ino) != self._lock_identity
            or stat.S_IMODE(lock_bound.st_mode) != 0o600
            or lock_bound.st_uid != os.geteuid()
            or not stat.S_ISREG(journal_path.st_mode)
            or (journal_path.st_dev, journal_path.st_ino) != self._journal_identity
            or (journal_bound.st_dev, journal_bound.st_ino) != self._journal_identity
            or stat.S_IMODE(journal_bound.st_mode) != 0o600
            or journal_bound.st_uid != os.geteuid()
        ):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry storage changed"
            )
        if self._metadata_fd is not None:
            try:
                metadata_path = os.stat(
                    "registry-metadata.json",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                metadata_bound = os.fstat(self._metadata_fd)
            except OSError:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry storage changed"
                ) from None
            if (
                not stat.S_ISREG(metadata_path.st_mode)
                or (metadata_path.st_dev, metadata_path.st_ino)
                != self._metadata_identity
                or (metadata_bound.st_dev, metadata_bound.st_ino)
                != self._metadata_identity
                or stat.S_IMODE(metadata_bound.st_mode) != 0o600
                or metadata_bound.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root or objects directory is always unlinked under the exclusive
        # lock; a directory under that name makes unlink fail, so recovery
        # fails closed.
        if self._root_fd is None or self._objects_fd is None:
            raise ProjectionPolicyRegistryUnsafe("projection policy registry is closed")
        try:
            for directory_fd in (self._root_fd, self._objects_fd):
                _remove_owned_temporaries(directory_fd)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> ProjectionPolicyRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry metadata is missing"
                ) from None
            metadata = ProjectionPolicyRegistryMetadata(
                registry_id=f"projection_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
            )
            try:
                _PP_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _PP_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                ProjectionPolicyRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        return metadata

    def _load_journal(self) -> tuple[ProjectionPolicyJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ProjectionPolicyRegistryUnsafe("projection policy registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry journal is incomplete"
            )
        entries: list[ProjectionPolicyJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_POLICIES:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(ProjectionPolicyJournalEntry, line)
            except Exception:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: ProjectionPolicyJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ProjectionPolicyRegistryUnsafe("projection policy registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException as error:
            # Remove any torn suffix so the committed chain stays readable; the
            # object it named remains an uncommitted remnant for later cleanup.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            # An interrupt or other non-OS failure keeps its own type.
            if not isinstance(error, OSError):
                raise
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[ProjectionPolicyJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredProjectionPolicyObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise ProjectionPolicyRegistryUnsafe("projection policy registry is closed")
        journal = _PP_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_POLICIES + 1:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredProjectionPolicyObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry journal binding is invalid"
                )
            try:
                value = registered_projection_object_from_bytes(content)
            except ValueError:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry object is invalid"
                ) from None
            loaded[entry.object_sha256] = (value, content)
        try:
            _validate_policy_history(
                self._metadata, [loaded[entry.object_sha256][0] for entry in journal]
            )
        except ValueError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry history is invalid"
            ) from None
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _PP_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry state rollback detected"
                )
        return loaded, head

    def _require_final_head(self, head: str) -> None:
        """Revalidate the committed head immediately before a read returns."""

        journal = _PP_LOAD_JOURNAL(self)
        final = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if final != head or self._trusted_head_sha256 != head:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry head changed during read"
            )

    def register_policy(self, policy: object) -> ProjectionPolicyRegistrationReceipt:
        """Publish one exact closed family policy immutably.

        Registration is the protected operator path.  A policy version ``N``
        requires versions ``1..N-1`` of the same ``policy_id`` and family, and a
        version number is never reused with other content.
        """

        _require_registry_integrity(self)
        try:
            captured_policy = _capture_policy(policy)
            captured = RegisteredProjectionPolicyObject(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                policy=captured_policy,
            )
            content = registered_projection_object_bytes(captured)
            captured = registered_projection_object_from_bytes(content)
        except Exception:
            raise ProjectionPolicyRegistryConflict(
                "projection policy is not an exact closed family policy"
            ) from None
        digest = hashlib.sha256(content).hexdigest()
        epoch = self._metadata.registry_epoch_sha256
        selector_id = _PP_SELECTOR_ID(epoch, captured.policy.policy_id)
        with _PP_LOCK(self, exclusive=True):
            _PP_RECOVER_TEMPORARY_OBJECTS(self)
            loaded, head = _PP_LOAD_STATE(self)
            assert self._objects_fd is not None
            # The journal is the commit point: an object without an entry is
            # the remnant of an interrupted registration and is never adopted
            # unless its exact bytes are being registered again.
            for name in os.listdir(self._objects_fd):
                if name[:64] not in loaded and name != f"{digest}.json":
                    _read_exact_object(self._objects_fd, name[:64])
                    os.unlink(name, dir_fd=self._objects_fd)
            os.fsync(self._objects_fd)
            if digest in loaded:
                if loaded[digest][1] != content:
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy object digest conflicts"
                    )
            else:
                history = [
                    value.policy
                    for value, _ in loaded.values()
                    if _PP_SELECTOR_ID(epoch, value.policy.policy_id) == selector_id
                ]
                versions = sorted(item.version for item in history)
                if captured.policy.version in versions:
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy version is already registered with "
                        "other content"
                    )
                if captured.policy.version != len(versions) + 1:
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy version must extend its selector history"
                    )
                if any(
                    (item.family, item.coordinate_scheme)
                    != (captured.policy.family, captured.policy.coordinate_scheme)
                    for item in history
                ):
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy selector cannot change family or scheme"
                    )
                if len(loaded) >= MAX_REGISTERED_POLICIES:
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy registry is full"
                    )
                if (
                    sum(len(item[1]) for item in loaded.values()) + len(content)
                    > MAX_TOTAL_OBJECT_BYTES
                ):
                    raise ProjectionPolicyRegistryConflict(
                        "projection policy registry byte bound would be exceeded"
                    )
                try:
                    _PP_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                except FileExistsError:
                    if _read_exact_object(self._objects_fd, digest) != content:
                        raise ProjectionPolicyRegistryConflict(
                            "projection policy publication conflicts"
                        ) from None
                _PP_APPEND_JOURNAL(
                    self,
                    _build_journal_entry(
                        sequence=len(loaded) + 1,
                        previous_entry_sha256=head,
                        object_sha256=digest,
                        object_bytes=len(content),
                    ),
                )
            final, final_head = _PP_LOAD_STATE(self)
            if digest not in final or final[digest][1] != content:
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy publication is unproven"
                )
            return _PP_RECEIPT(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(final),
                state_head_sha256=final_head,
                selector_id=selector_id,
                policy_version=captured.policy.version,
                object_sha256=digest,
                policy_sha256=_PP_POLICY_SHA256(captured.policy),
                family=captured.policy.family,
            )

    def resolve(self, selector_id: str, policy_version: int) -> ResolvedProjectionPolicy:
        """Return one registered policy by opaque selector and version only."""

        _require_registry_integrity(self)
        if not _is_policy_selector(selector_id) or not _is_policy_version(
            policy_version
        ):
            raise ProjectionPolicyRegistryConflict(
                "projection policy selector is invalid"
            )
        with _PP_LOCK(self, exclusive=False):
            loaded, head = _PP_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            matches = [
                (digest, value)
                for digest, (value, _) in loaded.items()
                if _PP_SELECTOR_ID(epoch, value.policy.policy_id) == selector_id
                and value.policy.version == policy_version
            ]
            if len(matches) != 1:
                raise ProjectionPolicyRegistryConflict(
                    "projection policy selector is unavailable"
                )
            digest, value = matches[0]
            resolved = _PP_RESOLVED(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                selector_id=selector_id,
                policy_version=policy_version,
                object_sha256=digest,
                policy_sha256=_PP_POLICY_SHA256(value.policy),
                policy=value.policy,
            )
            _PP_REQUIRE_FINAL_HEAD(self, head)
            return resolved

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        after_policy_version: int | None = None,
        limit: int = 50,
    ) -> ProjectionPolicySelectorPage:
        """Return one bounded privacy-safe page of digests and controlled states."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise ProjectionPolicyRegistryConflict(
                "projection policy selector page bound is invalid"
            )
        if (after_selector_id is None) != (after_policy_version is None):
            raise ProjectionPolicyRegistryConflict(
                "projection policy selector cursor is incomplete"
            )
        if after_selector_id is not None and (
            not _is_policy_selector(after_selector_id)
            or not _is_policy_version(after_policy_version)
        ):
            raise ProjectionPolicyRegistryConflict(
                "projection policy selector cursor is invalid"
            )
        with _PP_LOCK(self, exclusive=False):
            loaded, head = _PP_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            ordered = sorted(
                (
                    _PP_SELECTOR_ID(epoch, value.policy.policy_id),
                    value.policy.version,
                    digest,
                    value,
                )
                for digest, (value, _) in loaded.items()
            )
            latest: dict[str, int] = {}
            for selector_id, version, _, _ in ordered:
                latest[selector_id] = max(latest.get(selector_id, 0), version)
            if after_selector_id is not None:
                cursor = (after_selector_id, after_policy_version)
                ordered = [item for item in ordered if item[:2] > cursor]
            selected = ordered[:limit]
            rows = tuple(
                _PP_SELECTOR_RECORD(
                    selector_id=selector_id,
                    policy_version=version,
                    latest_version=latest[selector_id] == version,
                    object_sha256=digest,
                    policy_sha256=_PP_POLICY_SHA256(value.policy),
                    family=value.policy.family,
                    selection_rule=value.policy.selection_rule,
                    component_count=_policy_component_count(value.policy),
                    measurement_definition_sha256=(
                        value.policy.measurement.measurement_definition_sha256
                    ),
                    anchor_definition_sha256=(
                        value.policy.measurement_anchor.anchor_definition_sha256
                    ),
                )
                for selector_id, version, digest, value in selected
            )
            more = len(ordered) > len(selected)
            page = _PP_SELECTOR_PAGE(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                records=rows,
                next_after_selector_id=(rows[-1].selector_id if more and rows else None),
                next_after_policy_version=(
                    rows[-1].policy_version if more and rows else None
                ),
            )
            _PP_REQUIRE_FINAL_HEAD(self, head)
            return page

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _PP_LOCK(self, exclusive=False):
            loaded, head = _PP_LOAD_STATE(self)
            backup = ProjectionPolicyBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_PP_LOAD_JOURNAL(self),
                objects=tuple(
                    ProjectionPolicyBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise ProjectionPolicyRegistryConflict(
                    "projection policy registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> ProjectionPolicyRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        backup = projection_policy_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry backup expected head is invalid"
            )
        target = _snapshot_path(root)
        parent = target.parent
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd: int | None = None
        root_fd: int | None = None
        objects_fd: int | None = None
        created = False
        staging_name = target.name
        completed = False
        try:
            parent_lstat = os.stat(parent, follow_symlinks=False)
            parent_fd = os.open(parent, directory_flags)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry restore parent changed"
                )
            staging_name = _make_staging_directory(parent_fd, target.name)
            created = True
            root_lstat = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or (root_lstat.st_dev, root_lstat.st_ino)
                != (root_bound.st_dev, root_bound.st_ino)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry restore root changed"
                )
            os.mkdir("objects", 0o700, dir_fd=root_fd)
            objects_lstat = os.stat("objects", dir_fd=root_fd, follow_symlinks=False)
            objects_fd = os.open("objects", directory_flags, dir_fd=root_fd)
            objects_bound = os.fstat(objects_fd)
            if (
                not stat.S_ISDIR(objects_lstat.st_mode)
                or (objects_lstat.st_dev, objects_lstat.st_ino)
                != (objects_bound.st_dev, objects_bound.st_ino)
                or stat.S_IMODE(objects_bound.st_mode) != 0o700
                or objects_bound.st_uid != os.geteuid()
            ):
                raise ProjectionPolicyRegistryUnsafe(
                    "projection policy registry restore objects changed"
                )
            lock_fd = os.open(
                ".registry.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            os.close(lock_fd)
            _publish_file(
                root_fd,
                "registry-metadata.json",
                canonical_contract_bytes(backup.metadata),
            )
            for item in backup.objects:
                _publish_file(
                    objects_fd,
                    f"{item.object_sha256}.json",
                    item.object_json.encode("utf-8"),
                )
            _publish_file(
                root_fd,
                "registry-journal.jsonl",
                b"".join(
                    canonical_contract_bytes(entry) + b"\n" for entry in backup.journal
                ),
            )
            os.fsync(objects_fd)
            os.fsync(root_fd)
            os.fsync(parent_fd)
            # The staged root becomes the target only once it is complete.
            _commit_staging_directory(parent_fd, staging_name, target.name, root_fd)
            staging_name = target.name
            # Reopen through the normal checks before the restore counts as
            # complete, so a target that cannot open is removed, not left to
            # block a retry.
            restored = _PP_CONSTRUCT(
                target,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise ProjectionPolicyRegistryConflict(
                "projection policy registry restore target already exists"
            ) from None
        except OSError:
            raise ProjectionPolicyRegistryUnsafe(
                "projection policy registry restore failed"
            ) from None
        finally:
            if created and not completed:
                if root_fd is not None:
                    _remove_partial_restore(
                        parent_fd, staging_name, root_fd, objects_fd
                    )
                elif parent_fd is not None:
                    try:
                        os.rmdir(staging_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            for descriptor in (objects_fd, root_fd, parent_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return restored

    @classmethod
    def recover_torn_journal_tail(
        cls,
        root: str | Path,
        *,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> int:
        """Operator maintenance: remove an unterminated trailing journal line.

        Reopening a registry whose journal ends in a torn line fails closed,
        and nothing repairs it automatically.  This explicit entry point takes
        the exclusive registry lock without waiting (a registry in use is
        refused) and truncates only the bytes after the
        last newline, and only when every complete line chains to exactly the
        retained head under the retained identity.  It returns the number of
        bytes removed (``0`` when there is no torn tail); then reopen with the
        same retained values.
        """

        _require_registry_class_integrity(cls)
        return _recover_torn_journal_tail(
            _snapshot_path(root),
            expected_registry_id=expected_registry_id,
            expected_registry_epoch_sha256=expected_registry_epoch_sha256,
            expected_state_head_sha256=expected_state_head_sha256,
            parse_metadata=lambda content: contract_from_canonical_bytes(
                ProjectionPolicyRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                ProjectionPolicyJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=4 * 1024 * 1024,
            max_entries=MAX_REGISTERED_POLICIES,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=ProjectionPolicyRegistryUnsafe,
            label="projection policy registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: ProjectionPolicyRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_lock",
            "_validate_storage",
            "_publish",
            "_recover_temporary_objects",
            "_load_or_create_metadata",
            "_load_journal",
            "_append_journal",
            "_accept_observed_head",
            "_load_state",
            "_require_final_head",
            "register_policy",
            "resolve",
            "list_selectors",
            "backup_bytes",
            "restore",
            "recover_torn_journal_tail",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not ProjectionPolicyRegistry or any(
        ProjectionPolicyRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry callable changed"
        )


def _require_registry_integrity(registry: ProjectionPolicyRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry callable changed"
        )
    if any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority callable changed"
        )
    instance = object.__getattribute__(registry, "__dict__")
    initialized_names = (
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    initialized = tuple(name in instance for name in initialized_names)
    if not any(initialized):
        return
    if not all(initialized):
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise ProjectionPolicyRegistryUnsafe(
            "projection policy registry authority state changed"
        )


_PP_CONSTRUCT = ProjectionPolicyRegistry
_PP_CLOSE = ProjectionPolicyRegistry.close
_PP_LOCK = ProjectionPolicyRegistry._lock
_PP_VALIDATE_STORAGE = ProjectionPolicyRegistry._validate_storage
_PP_PUBLISH = ProjectionPolicyRegistry._publish
_PP_RECOVER_TEMPORARY_OBJECTS = ProjectionPolicyRegistry._recover_temporary_objects
_PP_LOAD_OR_CREATE_METADATA = ProjectionPolicyRegistry._load_or_create_metadata
_PP_LOAD_JOURNAL = ProjectionPolicyRegistry._load_journal
_PP_APPEND_JOURNAL = ProjectionPolicyRegistry._append_journal
_PP_ACCEPT_OBSERVED_HEAD = ProjectionPolicyRegistry._accept_observed_head
_PP_LOAD_STATE = ProjectionPolicyRegistry._load_state
_PP_REQUIRE_FINAL_HEAD = ProjectionPolicyRegistry._require_final_head
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another policy.
_PP_RECEIPT = ProjectionPolicyRegistrationReceipt
_PP_RESOLVED = ResolvedProjectionPolicy
_PP_SELECTOR_RECORD = ProjectionPolicySelectorRecord
_PP_SELECTOR_PAGE = ProjectionPolicySelectorPage
_PP_SELECTOR_ID = _selector_id
_PP_POLICY_SHA256 = projection_policy_sha256
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_PP_CONSTRUCT",
            "_PP_CLOSE",
            "_PP_LOCK",
            "_PP_VALIDATE_STORAGE",
            "_PP_PUBLISH",
            "_PP_RECOVER_TEMPORARY_OBJECTS",
            "_PP_LOAD_OR_CREATE_METADATA",
            "_PP_LOAD_JOURNAL",
            "_PP_APPEND_JOURNAL",
            "_PP_ACCEPT_OBSERVED_HEAD",
            "_PP_LOAD_STATE",
            "_PP_REQUIRE_FINAL_HEAD",
            "_PP_RECEIPT",
            "_PP_RESOLVED",
            "_PP_SELECTOR_RECORD",
            "_PP_SELECTOR_PAGE",
            "_PP_SELECTOR_ID",
            "_PP_POLICY_SHA256",
        )
    }
)


__all__ = [
    "CellOriginProjectionComponent",
    "CellOriginProjectionPolicyV1",
    "CellOriginStatistic",
    "CnaChromosomeProjectionComponent",
    "CnaChromosomeProjectionPolicyV1",
    "CnaChromosomeStatistic",
    "CnaSegmentCoordinate",
    "CnaSegmentProjectionComponent",
    "CnaSegmentProjectionPolicyV1",
    "CnaSegmentStatistic",
    "FragmentBinCoordinate",
    "FragmentProjectionComponent",
    "FragmentProjectionPolicyV1",
    "FragmentStatistic",
    "ProjectionFamily",
    "ProjectionMeasurementBinding",
    "ProjectionPolicy",
    "ProjectionPolicyBackup",
    "ProjectionPolicyBackupObject",
    "ProjectionPolicyJournalEntry",
    "ProjectionPolicyRegistrationReceipt",
    "ProjectionPolicyRegistry",
    "ProjectionPolicyRegistryConflict",
    "ProjectionPolicyRegistryError",
    "ProjectionPolicyRegistryMetadata",
    "ProjectionPolicyRegistryUnsafe",
    "ProjectionPolicySelectorPage",
    "ProjectionPolicySelectorRecord",
    "ProjectionSelectionRule",
    "RegisteredProjectionPolicyObject",
    "ResolvedProjectionPolicy",
    "StatisticUnit",
    "cna_coordinate_grid_sha256",
    "projection_policy_backup_from_bytes",
    "projection_policy_sha256",
    "registered_projection_object_bytes",
    "registered_projection_object_from_bytes",
    "require_projection_policy_binding",
]
