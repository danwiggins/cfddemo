"""Fail-closed browser projection over authoritative E04 and E06-E13 models."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import Field, model_validator

from evidence_inspector.cell_origin_explorer import (
    CellOriginExplorerArtifact,
    cell_origin_explorer_from_canonical_bytes,
)
from evidence_inspector.cna_explorer import CnaExplorerSnapshot
from evidence_inspector.compatibility import CompatibilityOutcome
from evidence_inspector.fragment_explorer import (
    FragmentExplorerView,
    fragment_explorer_from_canonical_bytes,
    replay_fragment_explorer_view,
)
from evidence_inspector.portable_view import PortableLocalView
from evidence_inspector.provenance_drawer import ProvenanceDrawer
from evidence_inspector.result_catalog import (
    CatalogLiveReader,
    CatalogPage,
    CatalogQuery,
    CatalogResultRef,
    CatalogVerificationContext,
    ResultCatalog,
    bind_catalog_live_reader,
)
from evidence_inspector.result_view import (
    ResultView,
    ResultViewRequest,
    canonical_result_view_bytes,
    replay_result_view,
    result_view_contract_from_canonical_bytes,
)
from evidence_inspector.sensitivity_comparison import (
    SensitivityComparisonArtifact,
    sensitivity_comparison_from_canonical_bytes,
)
from traceback_runner.contracts import ResultBundleManifestV2, RunnerContract
from traceback_runner.serialization import canonical_json_bytes, sha256_bytes
from traceback_runner.web.contracts import validate_public_projection

MAX_EXPLORER_PAGE_SIZE = 100
MAX_EXPLORER_ARTIFACT_BYTES = 64 * 1024 * 1024


def _flatten_strings(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_flatten_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_flatten_strings(item))
    return found


class CatalogAuthorityBinding(RunnerContract):
    result_id: str = Field(pattern=r"^result_[0-9a-f]{40}$")
    context: CatalogVerificationContext


class CatalogAuthorityIndex:
    """Installed current E04 authority, keyed by immutable result identity."""

    def __init__(self, bindings: Sequence[CatalogAuthorityBinding]) -> None:
        self._bindings: dict[str, CatalogVerificationContext] = {}
        for binding in bindings:
            replayed = CatalogAuthorityBinding.model_validate_json(
                canonical_json_bytes(binding)
            )
            if replayed.result_id in self._bindings:
                raise ValueError("catalog authority binding is duplicated")
            self._bindings[replayed.result_id] = replayed.context

    def context_for(self, result_id: str) -> CatalogVerificationContext:
        try:
            return self._bindings[result_id]
        except KeyError:
            raise KeyError("catalog authority is unavailable") from None

    def contains(self, result_id: str) -> bool:
        return result_id in self._bindings


class ExplorerArtifactRecord(RunnerContract):
    """Canonical inputs needed to replay one E06-E13 browser detail."""

    schema_version: Literal["traceback.explorer-artifact-record.v1"] = (
        "traceback.explorer-artifact-record.v1"
    )
    result_id: str = Field(pattern=r"^result_[0-9a-f]{40}$")
    result_view_request: ResultViewRequest
    result_view: ResultView
    fragment: FragmentExplorerView | None = None
    cell_origin: CellOriginExplorerArtifact | None = None
    cna: CnaExplorerSnapshot | None = None
    provenance: ProvenanceDrawer | None = None
    sensitivity: SensitivityComparisonArtifact | None = None
    portable: PortableLocalView | None = None
    longitudinal_state: Literal["unavailable_not_implemented"] = (
        "unavailable_not_implemented"
    )

    @model_validator(mode="after")
    def replays_and_contains_selected_result(self) -> ExplorerArtifactRecord:
        replay_result_view(self.result_view_request, self.result_view)
        source_ids = {
            item.record.result_id for item in self.result_view_request.sources
        }
        if self.result_id not in source_ids:
            raise ValueError("artifact record is absent from its E06 request")
        return self


class CanonicalExplorerArtifactRepository:
    """Immutable canonical bytes; every load reparses and replays all models."""

    def __init__(self, records: Sequence[ExplorerArtifactRecord]) -> None:
        self._records: dict[str, bytes] = {}
        for record in records:
            replayed = self._replay(record)
            content = canonical_json_bytes(replayed)
            if replayed.result_id in self._records:
                raise ValueError("explorer artifact record is duplicated")
            self._records[replayed.result_id] = content

    @staticmethod
    def _replay(record: ExplorerArtifactRecord) -> ExplorerArtifactRecord:
        base = ExplorerArtifactRecord.model_validate_json(canonical_json_bytes(record))
        request_bytes = canonical_result_view_bytes(base.result_view_request)
        view_bytes = canonical_result_view_bytes(base.result_view)
        request = result_view_contract_from_canonical_bytes(
            ResultViewRequest, request_bytes
        )
        view = result_view_contract_from_canonical_bytes(ResultView, view_bytes)
        replay_result_view(request, view)
        fragment = None
        if base.fragment is not None:
            fragment = fragment_explorer_from_canonical_bytes(
                FragmentExplorerView, canonical_json_bytes(base.fragment)
            )
            replay_fragment_explorer_view(fragment.request, fragment)
        cell_origin = None
        if base.cell_origin is not None:
            cell_origin = cell_origin_explorer_from_canonical_bytes(
                canonical_json_bytes(base.cell_origin)
            )
        cna = None
        if base.cna is not None:
            cna = CnaExplorerSnapshot.model_validate_json(
                canonical_json_bytes(base.cna)
            )
        provenance = None
        if base.provenance is not None:
            provenance = ProvenanceDrawer.model_validate_json(
                canonical_json_bytes(base.provenance)
            )
        sensitivity = None
        if base.sensitivity is not None:
            sensitivity = sensitivity_comparison_from_canonical_bytes(
                canonical_json_bytes(base.sensitivity)
            )
        portable = None
        if base.portable is not None:
            portable = PortableLocalView.model_validate_json(
                canonical_json_bytes(base.portable)
            )
        replayed = ExplorerArtifactRecord(
            result_id=base.result_id,
            result_view_request=request,
            result_view=view,
            fragment=fragment,
            cell_origin=cell_origin,
            cna=cna,
            provenance=provenance,
            sensitivity=sensitivity,
            portable=portable,
        )
        content = canonical_json_bytes(replayed)
        if len(content) > MAX_EXPLORER_ARTIFACT_BYTES:
            raise ValueError("explorer artifact violates its public byte boundary")
        validate_public_projection(replayed.model_dump(mode="json"))
        return replayed

    def contains(self, result_id: str) -> bool:
        return result_id in self._records

    def load(self, result_id: str) -> ExplorerArtifactRecord:
        try:
            content = self._records[result_id]
        except KeyError:
            raise KeyError("explorer artifact is unavailable") from None
        record = ExplorerArtifactRecord.model_validate_json(content)
        return self._replay(record)


class ExplorerReadModels(RunnerContract):
    schema_version: Literal["traceback.integrated-explorer-models.v2"] = (
        "traceback.integrated-explorer-models.v2"
    )
    catalog_ref: CatalogResultRef
    result_view_request: ResultViewRequest
    result_view: ResultView
    fragment: FragmentExplorerView | None = None
    cell_origin: CellOriginExplorerArtifact | None = None
    cna: CnaExplorerSnapshot | None = None
    provenance: ProvenanceDrawer | None = None
    sensitivity: SensitivityComparisonArtifact | None = None
    portable: PortableLocalView | None = None
    longitudinal_state: Literal["unavailable_not_implemented"] = (
        "unavailable_not_implemented"
    )

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
        replay_result_view(self.result_view_request, self.result_view)
        source = next(
            (
                item
                for item in self.result_view_request.sources
                if item.record.result_id == self.catalog_ref.result_id
            ),
            None,
        )
        if source is None:
            raise ValueError("integrated explorer result is absent from request")
        optional_models = (
            self.fragment,
            self.cell_origin,
            self.cna,
            self.provenance,
            self.sensitivity,
            self.portable,
        )
        if (
            row.result_identity.bundle_sha256 != self.catalog_ref.bundle_sha256
            or row.method_identity.method_ref != self.catalog_ref.method_ref
            or row.method_identity.method_definition_sha256
            != self.catalog_ref.method_definition_sha256
            or row.authority_identity.registry_sha256
            != self.catalog_ref.registry_sha256
            or row.authority_identity.registry_version
            != self.catalog_ref.registry_version
            or row.authority_identity.authority_head_sha256
            != self.catalog_ref.authority_head_sha256
            or row.authority_identity.authority_revision
            != self.catalog_ref.authority_revision
        ):
            raise ValueError("integrated explorer identities do not match catalog")
        measurement = source.record
        required = {
            measurement.result_id,
            measurement.result_sha256,
            measurement.bundle_id,
            measurement.bundle_sha256,
            measurement.method_definition_sha256,
            *(asset.content_sha256 for asset in measurement.method.assets),
        }
        for model in optional_models:
            if model is None:
                continue
            strings = _flatten_strings(model.model_dump(mode="json"))
            if not required.issubset(strings):
                raise ValueError(
                    "optional explorer model omits the exact result or asset identity"
                )
        return self


class ExplorerEligibility(RunnerContract):
    research_inspection_allowed: bool
    release_explorer_allowed: Literal[False] = False
    release_export_allowed: Literal[False] = False
    release_gate_decision_sha256: None = None
    release_state: Literal["disabled_no_installed_authority"] = (
        "disabled_no_installed_authority"
    )


class ExplorerCatalogItem(RunnerContract):
    ref: CatalogResultRef
    has_registered_view: bool
    eligibility: ExplorerEligibility


class ExplorerCatalogProjection(RunnerContract):
    schema_version: Literal["traceback.explorer-catalog-page.v2"] = (
        "traceback.explorer-catalog-page.v2"
    )
    query: CatalogQuery
    results: tuple[ExplorerCatalogItem, ...] = Field(max_length=MAX_EXPLORER_PAGE_SIZE)
    next_cursor: str | None = None
    empty: bool
    empty_reason: str | None = None


class ExplorerDocument(RunnerContract):
    schema_version: Literal["traceback.explorer-document.v2"] = (
        "traceback.explorer-document.v2"
    )
    models: ExplorerReadModels
    eligibility: ExplorerEligibility


class ExplorerComparison(RunnerContract):
    schema_version: Literal["traceback.explorer-comparison.v1"] = (
        "traceback.explorer-comparison.v1"
    )
    left_result_id: str
    right_result_id: str
    outcome: Literal[
        "comparable",
        "incompatible",
        "unknown",
    ]
    synchronized: bool
    delta_available: bool
    blocked_reason: str | None = None
    fragment: FragmentExplorerView | None = None


def explorer_eligibility(ref: CatalogResultRef) -> ExplorerEligibility:
    """Research remains usable; release stays off without installed authority."""

    return ExplorerEligibility(research_inspection_allowed=ref.research_inspectable)


def _bind_record_to_catalog(
    ref: CatalogResultRef, record: ExplorerArtifactRecord
) -> ExplorerReadModels:
    source = next(
        (
            item
            for item in record.result_view_request.sources
            if item.record.result_id == ref.result_id
        ),
        None,
    )
    if source is None:
        raise ValueError("E06 source is absent for current catalog result")
    measurement = source.record
    capability = measurement.current_capability
    expected_qualification = (
        capability.qualification_state.value
        if capability.qualification_state is not None
        else "unknown"
    )
    expected_role = (
        capability.display_role.value if capability.display_role is not None else None
    )
    if (
        measurement.bundle_sha256 != ref.bundle_sha256
        or measurement.method.method_ref != ref.method_ref
        or measurement.method_definition_sha256 != ref.method_definition_sha256
        or capability.registry_sha256 != ref.registry_sha256
        or capability.registry_version != ref.registry_version
        or capability.authority_head_sha256 != ref.authority_head_sha256
        or capability.authority_revision != ref.authority_revision
        or capability.authority_scope != ref.authority_scope
        or capability.as_of != ref.capability_as_of
        or expected_qualification != ref.qualification_state.value
        or expected_role != (ref.display_role.value if ref.display_role else None)
        or capability.research_inspectable != ref.research_inspectable
        or capability.current_provider_eligible != ref.current_provider_eligible
    ):
        raise ValueError("E06 source does not match current E04 identity")
    model_strings = _flatten_strings(record.model_dump(mode="json"))
    required = {
        measurement.result_id,
        measurement.result_sha256,
        measurement.bundle_id,
        measurement.bundle_sha256,
        measurement.method_definition_sha256,
        *(asset.content_sha256 for asset in measurement.method.assets),
    }
    if not required.issubset(model_strings):
        raise ValueError("explorer artifacts omit exact result or asset identity")
    return ExplorerReadModels(
        catalog_ref=ref,
        result_view_request=record.result_view_request,
        result_view=record.result_view,
        fragment=record.fragment,
        cell_origin=record.cell_origin,
        cna=record.cna,
        provenance=record.provenance,
        sensitivity=record.sensitivity,
        portable=record.portable,
    )


def _fragment_matches_selected_documents(
    fragment: FragmentExplorerView,
    left: ExplorerDocument,
    right: ExplorerDocument,
) -> bool:
    if (
        fragment.state.left.result_id != left.models.catalog_ref.result_id
        or fragment.state.right.result_id != right.models.catalog_ref.result_id
    ):
        return False
    documents = (left, right)
    selections = (fragment.state.left, fragment.state.right)
    sources_by_id = {item.record.result_id: item for item in fragment.request.sources}
    for document, selection in zip(documents, selections, strict=True):
        ref = document.models.catalog_ref
        e06_source = next(
            item
            for item in document.models.result_view_request.sources
            if item.record.result_id == ref.result_id
        )
        fragment_source = sources_by_id.get(ref.result_id)
        if fragment_source is None:
            return False
        manifest = fragment_source.manifest
        if (
            not isinstance(manifest, ResultBundleManifestV2)
            or fragment_source.record != e06_source.record
            or selection.method_ref != ref.method_ref
            or manifest.record_id != ref.bundle_record_id
            or manifest.workflow_release_id != ref.workflow_release_id
            or sha256_bytes(canonical_json_bytes(manifest))
            != ref.bundle_manifest_sha256
            or manifest.method.method_id != ref.method_ref.method_id
            or manifest.method.version != ref.method_ref.version
            or manifest.method.method_definition_sha256 != ref.method_definition_sha256
        ):
            return False
    return True


def _reverify_document(
    get_verified: Callable[[str, CatalogVerificationContext], CatalogResultRef],
    authority: CatalogAuthorityIndex,
    document: ExplorerDocument,
) -> None:
    result_id = document.models.catalog_ref.result_id
    current = get_verified(result_id, authority.context_for(result_id))
    if current != document.models.catalog_ref:
        raise ValueError("catalog result changed before explorer response")


class _SealedExplorerType(type):
    def __new__(
        cls, name: str, bases: tuple[type, ...], namespace: dict[str, object]
    ) -> _SealedExplorerType:
        if any(isinstance(base, _SealedExplorerType) for base in bases):
            raise TypeError("integrated explorer source cannot be subclassed")
        return super().__new__(cls, name, bases, namespace)

    def __setattr__(cls, name: str, value: object) -> None:
        raise TypeError("integrated explorer source class is sealed")

    def __delattr__(cls, name: str) -> None:
        raise TypeError("integrated explorer source class is sealed")


def _build_integrated_explorer_source_type(
    reader_factory: Callable[[ResultCatalog], CatalogLiveReader],
    reader_type: type[CatalogLiveReader],
    reader_get_verified: Callable[
        [CatalogLiveReader, str, CatalogVerificationContext], CatalogResultRef
    ],
    reader_query: Callable[[CatalogLiveReader, CatalogQuery], CatalogPage],
    reverify_document: Callable[
        [
            Callable[[str, CatalogVerificationContext], CatalogResultRef],
            CatalogAuthorityIndex,
            ExplorerDocument,
        ],
        None,
    ],
) -> type[IntegratedExplorerSource]:
    """Capture the installed reader chain outside mutable module dispatch."""

    protected_methods: dict[str, Callable[..., object]] = {}

    class IntegratedExplorerSource(metaclass=_SealedExplorerType):
        __slots__ = (
            "_artifacts",
            "_authority",
            "_get_verified",
            "_query",
            "_reader",
        )

        def __init__(
            self,
            *,
            catalog: ResultCatalog,
            authority: CatalogAuthorityIndex,
            artifacts: CanonicalExplorerArtifactRepository,
        ) -> None:
            if globals().get("bind_catalog_live_reader") is not reader_factory:
                raise TypeError("explorer requires the package-owned reader factory")
            reader = reader_factory(catalog)
            if (
                type(reader) is not reader_type
                or reader_type.get_verified is not reader_get_verified
                or reader_type.query is not reader_query
            ):
                raise TypeError("explorer requires the package-owned live reader")
            object.__setattr__(self, "_reader", reader)
            object.__setattr__(self, "_get_verified", reader.get_verified)
            object.__setattr__(self, "_query", reader.query)
            object.__setattr__(self, "_authority", authority)
            object.__setattr__(self, "_artifacts", artifacts)
            self._assert_installed_reader()

        def __getattribute__(self, name: str) -> object:
            protected = protected_methods.get(name)
            if protected is not None:
                return protected.__get__(self)
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: object) -> None:
            raise TypeError("integrated explorer source is sealed")

        def __delattr__(self, name: str) -> None:
            raise TypeError("integrated explorer source is sealed")

        def _assert_installed_reader(self) -> None:
            reader = object.__getattribute__(self, "_reader")
            get_verified = object.__getattribute__(self, "_get_verified")
            query_reader = object.__getattribute__(self, "_query")
            if any(
                IntegratedExplorerSource.__dict__.get(name) is not expected
                for name, expected in protected_methods.items()
            ):
                raise TypeError("integrated explorer source class changed")
            if (
                globals().get("bind_catalog_live_reader") is not reader_factory
                or globals().get("_reverify_document") is not reverify_document
                or type(reader) is not reader_type
                or reader_type.get_verified is not reader_get_verified
                or reader_type.query is not reader_query
                or get_verified.__self__ is not reader
                or get_verified.__func__ is not reader_get_verified
                or query_reader.__self__ is not reader
                or query_reader.__func__ is not reader_query
            ):
                raise TypeError("integrated explorer reader chain changed")

        def _reverify(self, document: ExplorerDocument) -> None:
            self._assert_installed_reader()
            reverify_document(self._get_verified, self._authority, document)
            self._assert_installed_reader()

        def query(self, query: CatalogQuery) -> ExplorerCatalogProjection:
            self._assert_installed_reader()
            page: CatalogPage = self._query(query)
            self._assert_installed_reader()
            return ExplorerCatalogProjection(
                query=query,
                results=tuple(
                    ExplorerCatalogItem(
                        ref=ref,
                        has_registered_view=(
                            self._artifacts.contains(ref.result_id)
                            and self._authority.contains(ref.result_id)
                        ),
                        eligibility=explorer_eligibility(ref),
                    )
                    for ref in page.results
                ),
                next_cursor=page.next_cursor,
                empty=page.empty,
                empty_reason=page.empty_reason.value if page.empty_reason else None,
            )

        def get(self, result_id: str) -> ExplorerDocument:
            self._assert_installed_reader()
            context = self._authority.context_for(result_id)
            ref = self._get_verified(result_id, context)
            record = self._artifacts.load(result_id)
            models = _bind_record_to_catalog(ref, record)
            document = ExplorerDocument(
                models=models,
                eligibility=explorer_eligibility(ref),
            )
            replayed = ExplorerDocument.model_validate_json(
                canonical_json_bytes(document)
            )
            validate_public_projection(replayed.model_dump(mode="json"))
            self._reverify(replayed)
            return replayed

        def compare(
            self, left_result_id: str, right_result_id: str
        ) -> ExplorerComparison:
            self._assert_installed_reader()
            return _compare_explorer_documents(self, left_result_id, right_result_id)

    protected_methods.update(
        {
            name: IntegratedExplorerSource.__dict__[name]
            for name in (
                "__getattribute__",
                "_assert_installed_reader",
                "_reverify",
                "compare",
                "get",
                "query",
            )
        }
    )
    return IntegratedExplorerSource


def _compare_explorer_documents(
    source: IntegratedExplorerSource,
    left_result_id: str,
    right_result_id: str,
) -> ExplorerComparison:
    if left_result_id == right_result_id:
        raise ValueError("comparison requires two distinct results")
    left = source.get(left_result_id)
    right = source.get(right_result_id)
    if (
        left.models.result_view.filters_sha256
        != right.models.result_view.filters_sha256
    ):
        comparison = ExplorerComparison(
            left_result_id=left_result_id,
            right_result_id=right_result_id,
            outcome="unknown",
            synchronized=False,
            delta_available=False,
            blocked_reason="Selected results use different exact filter contexts",
        )
        source._reverify(left)
        source._reverify(right)
        return comparison
    candidates = [
        item
        for item in (left.models.fragment, right.models.fragment)
        if item is not None
    ]
    canonical_candidates: dict[bytes, FragmentExplorerView] = {}
    for candidate in candidates:
        orientation = (
            candidate.state.left.result_id,
            candidate.state.right.result_id,
        )
        if orientation == (right_result_id, left_result_id):
            raise ValueError("comparison artifact orientation is reversed")
        if orientation != (left_result_id, right_result_id):
            continue
        if not _fragment_matches_selected_documents(candidate, left, right):
            raise ValueError("comparison artifact is not cross-bound to selections")
        canonical_candidates[canonical_json_bytes(candidate)] = candidate
    if len(canonical_candidates) > 1:
        raise ValueError("comparison artifacts are ambiguous")
    fragment = next(iter(canonical_candidates.values()), None)
    if fragment is None:
        comparison = ExplorerComparison(
            left_result_id=left_result_id,
            right_result_id=right_result_id,
            outcome="unknown",
            synchronized=False,
            delta_available=False,
            blocked_reason="No exact registered compatibility decision",
        )
        source._reverify(left)
        source._reverify(right)
        return comparison
    outcome = fragment.compatibility.outcome
    if outcome == CompatibilityOutcome.COMPARABLE:
        projected = "comparable"
    elif outcome == CompatibilityOutcome.INCOMPATIBLE:
        projected = "incompatible"
    else:
        projected = "unknown"
    synchronized = bool(projected == "comparable" and fragment.synchronized_comparison)
    comparison = ExplorerComparison(
        left_result_id=left_result_id,
        right_result_id=right_result_id,
        outcome=projected,
        synchronized=synchronized,
        delta_available=bool(synchronized and fragment.delta_rows),
        blocked_reason=(
            None
            if synchronized
            else "Exact compatibility does not permit synchronized comparison"
        ),
        fragment=fragment,
    )
    validate_public_projection(comparison.model_dump(mode="json"))
    source._reverify(left)
    source._reverify(right)
    return comparison


IntegratedExplorerSource = _build_integrated_explorer_source_type(
    bind_catalog_live_reader,
    CatalogLiveReader,
    CatalogLiveReader.get_verified,
    CatalogLiveReader.query,
    _reverify_document,
)


def _build_explorer_response_preparers(
    source_type: type[IntegratedExplorerSource],
    source_get: Callable[..., ExplorerDocument],
    source_reverify: Callable[..., None],
    document_replay: Callable[..., ExplorerDocument],
    comparison_replay: Callable[..., ExplorerComparison],
    canonicalize: Callable[[object], bytes],
    validate_public: Callable[..., None],
) -> tuple[Callable[..., dict[str, object]], Callable[..., dict[str, object]]]:
    """Capture the exact final-byte response boundary outside module dispatch."""

    expected = {"_reverify": source_reverify, "get": source_get}

    def checked(source: IntegratedExplorerSource) -> None:
        if type(source) is not source_type or any(
            source_type.__dict__.get(name) is not method
            for name, method in expected.items()
        ):
            raise TypeError("explorer response requires the installed source")

    def prepare_document(
        source: IntegratedExplorerSource,
        document: ExplorerDocument,
    ) -> dict[str, object]:
        checked(source)
        replayed = document_replay(canonicalize(document))
        source_reverify(source, replayed)
        checked(source)
        payload = replayed.model_dump(mode="json")
        validate_public(payload)
        return payload

    def prepare_comparison(
        source: IntegratedExplorerSource,
        comparison: ExplorerComparison,
    ) -> dict[str, object]:
        checked(source)
        source_get(source, comparison.left_result_id)
        source_get(source, comparison.right_result_id)
        checked(source)
        replayed = comparison_replay(canonicalize(comparison))
        payload = replayed.model_dump(mode="json")
        validate_public(payload)
        return payload

    return prepare_document, prepare_comparison


(
    prepare_explorer_document_response,
    prepare_explorer_comparison_response,
) = _build_explorer_response_preparers(
    IntegratedExplorerSource,
    IntegratedExplorerSource.get,
    IntegratedExplorerSource._reverify,
    ExplorerDocument.model_validate_json,
    ExplorerComparison.model_validate_json,
    canonical_json_bytes,
    validate_public_projection,
)
