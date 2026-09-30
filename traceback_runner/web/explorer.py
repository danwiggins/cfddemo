"""Bounded browser projection over the authoritative E04 and E06-E13 models."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import Field, model_validator

from evidence_inspector.cell_origin_explorer import CellOriginExplorerArtifact
from evidence_inspector.cna_explorer import CnaExplorerSnapshot
from evidence_inspector.cohort_manifest import CohortManifest
from evidence_inspector.fragment_explorer import FragmentExplorerView
from evidence_inspector.portable_view import PortableLocalView
from evidence_inspector.provenance_drawer import ProvenanceDrawer
from evidence_inspector.result_catalog import (
    CatalogPage,
    CatalogQuery,
    CatalogResultRef,
    ResultCatalog,
)
from evidence_inspector.result_view import ResultView
from evidence_inspector.sensitivity_comparison import SensitivityComparisonArtifact
from traceback_runner.contracts import RunnerContract
from traceback_runner.serialization import canonical_json_bytes

MAX_EXPLORER_PAGE_SIZE = 100


class ReleaseGateDecisionLike(Protocol):
    capability_enabled: bool

    def model_dump(self, *, mode: str) -> dict[str, object]: ...


class ExplorerReadModels(RunnerContract):
    """Exact, already-validated read models registered for one catalog result."""

    schema_version: Literal["traceback.integrated-explorer-models.v1"] = (
        "traceback.integrated-explorer-models.v1"
    )
    catalog_ref: CatalogResultRef
    result_view: ResultView
    fragment: FragmentExplorerView | None = None
    cell_origin: CellOriginExplorerArtifact | None = None
    cna: CnaExplorerSnapshot | None = None
    provenance: ProvenanceDrawer | None = None
    sensitivity: SensitivityComparisonArtifact | None = None
    cohort: CohortManifest | None = None
    portable: PortableLocalView | None = None

    @model_validator(mode="after")
    def exact_catalog_binding(self) -> ExplorerReadModels:
        row = next(
            (
                item
                for item in self.result_view.rows
                if item.result_identity.result_id == self.catalog_ref.result_id
            ),
            None,
        )
        if row is None:
            raise ValueError("integrated explorer result is absent from result view")
        if (
            row.result_identity.bundle_sha256 != self.catalog_ref.bundle_sha256
            or row.method_identity.method_ref != self.catalog_ref.method_ref
            or row.method_identity.method_definition_sha256
            != self.catalog_ref.method_definition_sha256
            or row.authority_identity.registry_sha256
            != self.catalog_ref.registry_sha256
            or row.authority_identity.authority_head_sha256
            != self.catalog_ref.authority_head_sha256
        ):
            raise ValueError("integrated explorer identities do not match catalog")
        return self


class ExplorerEligibility(RunnerContract):
    research_inspection_allowed: bool
    release_explorer_allowed: bool
    release_export_allowed: bool
    release_gate_decision_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ExplorerCatalogItem(RunnerContract):
    ref: CatalogResultRef
    has_registered_view: bool
    eligibility: ExplorerEligibility


class ExplorerCatalogProjection(RunnerContract):
    schema_version: Literal["traceback.explorer-catalog-page.v1"] = (
        "traceback.explorer-catalog-page.v1"
    )
    results: tuple[ExplorerCatalogItem, ...] = Field(max_length=MAX_EXPLORER_PAGE_SIZE)
    next_cursor: str | None = None
    empty: bool
    empty_reason: str | None = None


class ExplorerDocument(RunnerContract):
    schema_version: Literal["traceback.explorer-document.v1"] = (
        "traceback.explorer-document.v1"
    )
    models: ExplorerReadModels
    eligibility: ExplorerEligibility


def _decision_sha256(decision: ReleaseGateDecisionLike | None) -> str | None:
    if decision is None:
        return None
    return hashlib.sha256(canonical_json_bytes(decision)).hexdigest()


def explorer_eligibility(
    ref: CatalogResultRef, decision: ReleaseGateDecisionLike | None
) -> ExplorerEligibility:
    """Keep research inspection independent while release surfaces fail closed."""

    released = bool(
        decision is not None
        and decision.capability_enabled
        and ref.current_provider_eligible
    )
    return ExplorerEligibility(
        research_inspection_allowed=ref.research_inspectable,
        release_explorer_allowed=released,
        release_export_allowed=released,
        release_gate_decision_sha256=_decision_sha256(decision),
    )


class IntegratedExplorerSource:
    """Adapter over a real immutable catalog and exact registered read models."""

    def __init__(
        self,
        *,
        catalog: ResultCatalog,
        read_models: Mapping[str, ExplorerReadModels] | None = None,
        release_gate_decision: ReleaseGateDecisionLike | None = None,
    ) -> None:
        self._catalog = catalog
        self._models = dict(read_models or {})
        self._decision = release_gate_decision
        for result_id, models in self._models.items():
            if result_id != models.catalog_ref.result_id:
                raise ValueError(
                    "explorer read-model key does not match result identity"
                )

    def query(self, query: CatalogQuery) -> ExplorerCatalogProjection:
        page: CatalogPage = self._catalog.query(query)
        return ExplorerCatalogProjection(
            results=tuple(
                ExplorerCatalogItem(
                    ref=ref,
                    has_registered_view=ref.result_id in self._models,
                    eligibility=explorer_eligibility(ref, self._decision),
                )
                for ref in page.results
            ),
            next_cursor=page.next_cursor,
            empty=page.empty,
            empty_reason=page.empty_reason.value if page.empty_reason else None,
        )

    def get(self, result_id: str) -> ExplorerDocument:
        try:
            models = self._models[result_id]
        except KeyError:
            raise KeyError("explorer result is unavailable") from None
        return ExplorerDocument(
            models=models,
            eligibility=explorer_eligibility(models.catalog_ref, self._decision),
        )
