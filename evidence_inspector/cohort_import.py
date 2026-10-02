"""Verified D06 aggregate-record import bound to D05 cohort membership.

The index is append-only, local, and contains protected lineage identifiers. It
never accepts caller assertions about bundle trust: the immutable result catalog
performs exact-inventory verification against independently provisioned trust,
then this layer binds the verified record to a live D05 manifest member.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

import evidence_inspector.cohort_manifest as cohort_manifest_module
import evidence_inspector.cohort_registry as cohort_registry_module
import evidence_inspector.result_catalog as result_catalog_module
from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MemberLineageRole,
    capture_expected_trust_pins,
    cohort_manifest_sha256,
)
from evidence_inspector.cohort_registry import (
    CohortRegistry,
    RegisteredCohortHistory,
)
from evidence_inspector.fault_controller import (
    NO_FAULTS,
    DeterministicFaultController,
    fault_controller_snapshot,
)
from evidence_inspector.method_registry import (
    AuthorityHead,
    CurrentMethodCapability,
    MethodRegistry,
    RegistryContract,
    Sha256,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage import AnalysisRecordId, ProviderNamespace
from evidence_inspector.provider_linkage_store import (
    ActiveLinkageSnapshot,
    ProviderLinkageStore,
)
from evidence_inspector.result_catalog import (
    CatalogAliases,
    CatalogAuthoritySnapshot,
    CatalogResultRef,
    CoordinatedCatalogCandidate,
    PendingCatalogPublication,
    PreparedCatalogImport,
    PublicationId,
    ResultBundleReaderRegistry,
    ResultCatalog,
    ResultId,
    _authority_value_fingerprint,
    bound_catalog_authority,
    catalog_authority_sha256,
)
from evidence_inspector.result_trust_registry import ResultTrustSnapshot
from evidence_inspector.safe_ingress import contract_type_graph, exact_model_bytes
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import RevokedKeyError

MAX_BINDINGS = 100_000
MAX_BINDING_BYTES = 128 * 1024
MAX_ROLLBACK_MARKER_BYTES = 1024
MAX_REGISTERED_HISTORY_BYTES = 64 * 1024 * 1024
MAX_REGISTERED_HISTORY_DEPTH = 96
MAX_REGISTERED_HISTORY_NODES = 2_000_000
MAX_AUTHORITY_CAPTURE_ITEMS = 100_000
MAX_IMPORT_AUTHORITY_BYTES = 64 * 1024 * 1024
MAX_IMPORT_AUTHORITY_DEPTH = 96
MAX_IMPORT_AUTHORITY_NODES = 2_000_000
BindingId = Annotated[str, StringConstraints(pattern=r"^binding_[0-9a-f]{64}$")]
_PROVIDER_NAMESPACE = TypeAdapter(ProviderNamespace)
_ANALYSIS_RECORD_ID = TypeAdapter(AnalysisRecordId)
_SHA256 = TypeAdapter(Sha256)

_PROCESS_LOCK = threading.RLock()
_COHORT_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
# Holder (process, thread) of each catalog's ``record_status_read_fence``.
_STATUS_FENCE_HOLDERS: weakref.WeakKeyDictionary[object, tuple[int, int]] = (
    weakref.WeakKeyDictionary()
)
_PINNED_RESULT_IMPORT = ResultCatalog.import_bundle
_PINNED_RESULT_VERIFY = ResultCatalog.verify_reference
_PINNED_RESULT_QUERY = ResultCatalog.query
_PINNED_RESULT_AUTHORITY = ResultCatalog.authority_snapshot
_PINNED_RESULT_TRUST_FENCE = ResultCatalog.trust_authority_fence
_PINNED_RESULT_PREPARE = ResultCatalog.prepare_bundle_import
_PINNED_RESULT_STAGE = ResultCatalog.stage_prepared_import
_PINNED_RESULT_ADOPT = ResultCatalog.adopt_prepared_import
_PINNED_RESULT_FINISH = ResultCatalog.finish_prepared_import
_PINNED_RESULT_COMPENSATE = ResultCatalog.compensate_prepared_import
_PINNED_RESULT_RECOVER = ResultCatalog.recover_pending_publication
_PINNED_RESULT_PENDING = ResultCatalog.pending_publications
_PINNED_RESULT_PUBLICATION = ResultCatalog.publication_for_recovery
_PINNED_RESULT_RECOVERY_ROWS = ResultCatalog.recovery_publications
_PINNED_RESULT_VERIFY_PREPARED = ResultCatalog.verify_prepared_object
_PINNED_RESULT_REGISTER_CANDIDATE = ResultCatalog.register_coordinated_candidate
_PINNED_RESULT_CANDIDATES = ResultCatalog.coordinated_candidates
_PINNED_RESULT_FINISH_CANDIDATE = ResultCatalog.finish_coordinated_candidate
_PINNED_READER_SELECT = ResultBundleReaderRegistry.select
_PINNED_RESULT_RUNTIME_ASSERT = result_catalog_module._RC_ASSERT_RUNTIME
_PINNED_RESULT_MODULE_VERIFY = result_catalog_module._PINNED_VERIFY_BUNDLE
_PINNED_RESULT_MODULE_TRUST_RESOLVE = result_catalog_module._PINNED_TRUST_RESOLVE
_PINNED_MANIFEST_ACTIVE_SNAPSHOT = cohort_manifest_module._PINNED_ACTIVE_SNAPSHOT
_PINNED_MANIFEST_STORE_CALLABLES = cohort_manifest_module._PINNED_STORE_CALLABLES
_PINNED_VALIDATE_MANIFEST_IN_FENCE = (
    cohort_manifest_module._validate_manifest_against_linkage_store_in_fence
)
_PINNED_LINKAGE_AUTHORITY_FENCE = ProviderLinkageStore.authority_read_fence
_PINNED_REGISTRY_REQUIRE_INTEGRITY = (
    cohort_registry_module._require_registry_integrity
)
_PINNED_REGISTRY_LOCK = CohortRegistry._lock
_PINNED_REGISTRY_RESOLVE_IN_FENCE = CohortRegistry._resolve_history_in_fence
_PINNED_REGISTRY_LOAD_JOURNAL = CohortRegistry._load_journal
_PINNED_REGISTRY_HEAD_IN_FENCE = CohortRegistry.head_in_fence
_PINNED_REGISTRY_LIST = CohortRegistry.list_selectors
_PINNED_EXACT_MODEL_BYTES = exact_model_bytes
_PINNED_FAULT_SNAPSHOT = fault_controller_snapshot
_PINNED_FAULT_HIT = DeterministicFaultController.hit


class CohortImportError(RuntimeError):
    """Sanitized record-import failure for local operator surfaces."""


class CohortImportConflict(CohortImportError):
    pass


class CohortImportFilesystemError(CohortImportError):
    pass


class CohortRecordBinding(RegistryContract):
    """Immutable link from one trusted aggregate record to one D05 member."""

    schema_version: Literal["traceback.cohort-record-binding.v3"] = (
        "traceback.cohort-record-binding.v3"
    )
    binding_id: BindingId
    registry_id: str = Field(pattern=r"^cohort_registry_[0-9a-f]{32}$")
    registry_epoch_sha256: Sha256
    registry_state_version: int = Field(ge=1, le=100_000)
    registry_state_head_sha256: Sha256
    selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=1)
    linkage_state_head_sha256: Sha256
    linkage_snapshot_sha256: Sha256
    inclusion_policy_sha256: Sha256
    exclusion_policy_sha256: Sha256
    missingness_policy_sha256: Sha256
    record_status_policy_sha256: Sha256
    provider_namespace: ProviderNamespace
    analysis_record_id: AnalysisRecordId
    member_sha256: Sha256
    lineage_role: MemberLineageRole
    denominator_contribution: bool
    measurement_anchor_sha256: Sha256
    result: CatalogResultRef
    publication_id: PublicationId
    catalog_authority_sha256: Sha256
    catalog_storage_identity_sha256: Sha256
    result_trust_snapshot_sha256: Sha256
    catalog_result_preexisting: bool
    reader_registry_sha256: Sha256
    reader_id: str = Field(pattern=r"^reader_[a-z0-9]+(?:_[a-z0-9]+)*$")
    reader_minimum_version: int = Field(ge=1, le=1_000)
    reader_maximum_version: int = Field(ge=1, le=1_000)
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def identity_is_bound(self) -> CohortRecordBinding:
        expected = _binding_id(
            registry_id=self.registry_id,
            registry_epoch_sha256=self.registry_epoch_sha256,
            registry_state_version=self.registry_state_version,
            registry_state_head_sha256=self.registry_state_head_sha256,
            selector_id=self.selector_id,
            cohort_manifest_sha256=self.cohort_manifest_sha256,
            linkage_snapshot_sha256=self.linkage_snapshot_sha256,
            inclusion_policy_sha256=self.inclusion_policy_sha256,
            exclusion_policy_sha256=self.exclusion_policy_sha256,
            missingness_policy_sha256=self.missingness_policy_sha256,
            record_status_policy_sha256=self.record_status_policy_sha256,
            provider_namespace=self.provider_namespace,
            analysis_record_id=self.analysis_record_id,
            result_id=self.result.result_id,
            publication_id=self.publication_id,
            catalog_authority_sha256=self.catalog_authority_sha256,
        )
        if self.binding_id != expected:
            raise ValueError("cohort record binding identity is invalid")
        if self.reader_minimum_version > self.reader_maximum_version:
            raise ValueError("cohort record reader range is inverted")
        if self.measurement_anchor_sha256 != self.result.method_definition_sha256:
            raise ValueError(
                "cohort measurement anchor does not bind the result method"
            )
        if self.record_status_policy_sha256 != COHORT_RECORD_STATUS_POLICY_SHA256:
            raise ValueError("cohort record status policy identity is invalid")
        return self


class CohortImportRollbackMarker(RegistryContract):
    """Durable rollback intent bound to one exact incomplete binding."""

    schema_version: Literal["traceback.cohort-import-rollback.v3"] = (
        "traceback.cohort-import-rollback.v3"
    )
    operation_id: Annotated[str, StringConstraints(pattern=r"^candidate_[0-9a-f]{64}$")]
    publication_id: PublicationId
    recovery_scope_sha256: Sha256
    cohort_manifest_sha256: Sha256
    binding_id: BindingId
    result_id: ResultId

    @property
    def final_name(self) -> str:
        return f"{self.cohort_manifest_sha256}.{self.binding_id}.json"


class CohortRecordAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    WITHHELD = "withheld"


class CohortRecordWithheldReason(StrEnum):
    RESULT_KEY_REVOKED = "result_key_revoked"


COHORT_RECORD_STATUS_POLICY_SHA256 = hashlib.sha256(
    b"traceback-cohort-record-status-policy-v1\0"
    + canonical_json_bytes(
        {
            "availability": ["available", "missing", "withheld"],
            "withheld_reasons": ["result_key_revoked"],
            "execution_states_remain_distinct": [
                "complete",
                "failed",
                "not_run",
            ],
            "compatibility_states_remain_distinct": [
                "comparable",
                "different_quantity",
                "incompatible",
                "unknown",
            ],
            "structural_or_authority_failure": "fail_closed",
        }
    )
).hexdigest()


class CohortMemberRecordStatus(RegistryContract):
    schema_version: Literal["traceback.cohort-member-record-status.v1"] = (
        "traceback.cohort-member-record-status.v1"
    )
    provider_namespace: ProviderNamespace
    analysis_record_id: AnalysisRecordId
    member_sha256: Sha256
    availability: CohortRecordAvailability
    binding: CohortRecordBinding | None = None
    withheld_reason: CohortRecordWithheldReason | None = None

    @model_validator(mode="after")
    def state_is_consistent(self) -> CohortMemberRecordStatus:
        if self.availability is CohortRecordAvailability.AVAILABLE:
            if self.binding is None or self.withheld_reason is not None:
                raise ValueError("available status requires only a binding")
        elif self.availability is CohortRecordAvailability.WITHHELD:
            if self.binding is not None or self.withheld_reason is None:
                raise ValueError("withheld status requires only a safe reason")
        elif self.binding is not None or self.withheld_reason is not None:
            raise ValueError("missing status cannot carry result details")
        return self


class CohortManifestRecordStatus(RegistryContract):
    schema_version: Literal["traceback.cohort-manifest-record-status.v2"] = (
        "traceback.cohort-manifest-record-status.v2"
    )
    registry_id: str = Field(pattern=r"^cohort_registry_[0-9a-f]{32}$")
    registry_epoch_sha256: Sha256
    registry_state_version: int = Field(ge=1, le=100_000)
    registry_state_head_sha256: Sha256
    selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_state_version: int = Field(ge=1)
    linkage_state_head_sha256: Sha256
    linkage_snapshot_sha256: Sha256
    catalog_authority_sha256: Sha256
    inclusion_policy_sha256: Sha256
    exclusion_policy_sha256: Sha256
    missingness_policy_sha256: Sha256
    record_status_policy_sha256: Sha256
    members: tuple[CohortMemberRecordStatus, ...] = Field(
        min_length=1, max_length=MAX_BINDINGS
    )
    status_sha256: Sha256

    @model_validator(mode="after")
    def digest_is_canonical(self) -> CohortManifestRecordStatus:
        payload = self.model_dump(mode="json", exclude={"status_sha256"})
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if self.status_sha256 != expected:
            raise ValueError("cohort record status digest is invalid")
        if self.record_status_policy_sha256 != COHORT_RECORD_STATUS_POLICY_SHA256:
            raise ValueError("cohort record status policy identity is invalid")
        return self


class CohortManifestBindings(RegistryContract):
    schema_version: Literal["traceback.cohort-manifest-bindings.v1"] = (
        "traceback.cohort-manifest-bindings.v1"
    )
    registry_id: str = Field(pattern=r"^cohort_registry_[0-9a-f]{32}$")
    registry_epoch_sha256: Sha256
    registry_state_version: int = Field(ge=1, le=100_000)
    registry_state_head_sha256: Sha256
    selector_id: str = Field(pattern=r"^cohort_selector_[0-9a-f]{40}$")
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    linkage_snapshot_sha256: Sha256
    catalog_authority_sha256: Sha256
    inclusion_policy_sha256: Sha256
    exclusion_policy_sha256: Sha256
    missingness_policy_sha256: Sha256
    record_status_policy_sha256: Sha256
    bindings: tuple[CohortRecordBinding, ...] = Field(max_length=MAX_BINDINGS)


def _binding_id(
    *,
    registry_id: str,
    registry_epoch_sha256: str,
    registry_state_version: int,
    registry_state_head_sha256: str,
    selector_id: str,
    cohort_manifest_sha256: str,
    linkage_snapshot_sha256: str,
    inclusion_policy_sha256: str,
    exclusion_policy_sha256: str,
    missingness_policy_sha256: str,
    record_status_policy_sha256: str,
    provider_namespace: str,
    analysis_record_id: str,
    result_id: str,
    publication_id: str,
    catalog_authority_sha256: str,
) -> str:
    payload = canonical_json_bytes(
        {
            "registry_id": registry_id,
            "registry_epoch_sha256": registry_epoch_sha256,
            "registry_state_version": registry_state_version,
            "registry_state_head_sha256": registry_state_head_sha256,
            "selector_id": selector_id,
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "linkage_snapshot_sha256": linkage_snapshot_sha256,
            "inclusion_policy_sha256": inclusion_policy_sha256,
            "exclusion_policy_sha256": exclusion_policy_sha256,
            "missingness_policy_sha256": missingness_policy_sha256,
            "record_status_policy_sha256": record_status_policy_sha256,
            "provider_namespace": provider_namespace,
            "analysis_record_id": analysis_record_id,
            "result_id": result_id,
            "publication_id": publication_id,
            "catalog_authority_sha256": catalog_authority_sha256,
        }
    )
    return (
        "binding_"
        + hashlib.sha256(b"traceback-cohort-record-binding-v3\0" + payload).hexdigest()
    )


def _member_sha256(member: CohortMember) -> str:
    return hashlib.sha256(canonical_contract_bytes(member)).hexdigest()


def _file_identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _reader_registry_sha256(registry: ResultBundleReaderRegistry) -> str:
    return hashlib.sha256(canonical_json_bytes(registry)).hexdigest()


def _opaque_alias(prefix: str, domain: bytes, values: object) -> str:
    digest = hashlib.sha256(domain + b"\0" + canonical_json_bytes(values)).hexdigest()
    return f"{prefix}_{digest[:16]}"


_METHOD_REGISTRY_MODEL_TYPES, _METHOD_REGISTRY_ENUM_TYPES = contract_type_graph(
    MethodRegistry
)
_AUTHORITY_HEAD_MODEL_TYPES, _AUTHORITY_HEAD_ENUM_TYPES = contract_type_graph(
    AuthorityHead
)
_CURRENT_CAPABILITY_MODEL_TYPES, _CURRENT_CAPABILITY_ENUM_TYPES = (
    contract_type_graph(CurrentMethodCapability)
)


def _capture_import_authority_contract(
    value: object,
    expected_type: type[MethodRegistry]
    | type[AuthorityHead]
    | type[CurrentMethodCapability],
) -> MethodRegistry | AuthorityHead | CurrentMethodCapability:
    graphs = {
        MethodRegistry: (_METHOD_REGISTRY_MODEL_TYPES, _METHOD_REGISTRY_ENUM_TYPES),
        AuthorityHead: (_AUTHORITY_HEAD_MODEL_TYPES, _AUTHORITY_HEAD_ENUM_TYPES),
        CurrentMethodCapability: (
            _CURRENT_CAPABILITY_MODEL_TYPES,
            _CURRENT_CAPABILITY_ENUM_TYPES,
        ),
    }
    try:
        model_types, enum_types = graphs[expected_type]
        content = _PINNED_EXACT_MODEL_BYTES(
            value,
            expected_type,
            model_types=model_types,
            enum_types=enum_types,
            max_bytes=MAX_IMPORT_AUTHORITY_BYTES,
            max_nodes=MAX_IMPORT_AUTHORITY_NODES,
            max_depth=MAX_IMPORT_AUTHORITY_DEPTH,
            max_collection_items=MAX_AUTHORITY_CAPTURE_ITEMS,
            max_string_bytes=16_384,
            allow_aliases=False,
        )
        captured = expected_type.model_validate_json(content)
        if (
            _PINNED_EXACT_MODEL_BYTES(
                captured,
                expected_type,
                model_types=model_types,
                enum_types=enum_types,
                max_bytes=MAX_IMPORT_AUTHORITY_BYTES,
                max_nodes=MAX_IMPORT_AUTHORITY_NODES,
                max_depth=MAX_IMPORT_AUTHORITY_DEPTH,
                max_collection_items=MAX_AUTHORITY_CAPTURE_ITEMS,
                max_string_bytes=16_384,
                allow_aliases=False,
            )
            != content
        ):
            raise ValueError("import authority contract is not canonical")
    except Exception:
        raise CohortImportError("import authority contract is invalid") from None
    return captured


_REGISTERED_HISTORY_MODEL_TYPES, _REGISTERED_HISTORY_ENUM_TYPES = (
    contract_type_graph(RegisteredCohortHistory)
)
_LINKAGE_SNAPSHOT_MODEL_TYPES, _LINKAGE_SNAPSHOT_ENUM_TYPES = contract_type_graph(
    ActiveLinkageSnapshot
)


def _capture_registered_history(value: object) -> RegisteredCohortHistory:
    try:
        content = _PINNED_EXACT_MODEL_BYTES(
            value,
            RegisteredCohortHistory,
            model_types=_REGISTERED_HISTORY_MODEL_TYPES,
            enum_types=_REGISTERED_HISTORY_ENUM_TYPES,
            max_bytes=MAX_REGISTERED_HISTORY_BYTES,
            max_nodes=MAX_REGISTERED_HISTORY_NODES,
            max_depth=MAX_REGISTERED_HISTORY_DEPTH,
            max_collection_items=MAX_AUTHORITY_CAPTURE_ITEMS,
            max_string_bytes=16_384,
        )
        captured = RegisteredCohortHistory.model_validate_json(content)
        if canonical_contract_bytes(captured) != content:
            raise ValueError("cohort registry history is not canonical")
    except Exception:
        raise CohortImportError("cohort registry history is invalid") from None
    assert isinstance(captured, RegisteredCohortHistory)
    return captured


def _capture_linkage_snapshot(value: object) -> ActiveLinkageSnapshot:
    try:
        content = _PINNED_EXACT_MODEL_BYTES(
            value,
            ActiveLinkageSnapshot,
            model_types=_LINKAGE_SNAPSHOT_MODEL_TYPES,
            enum_types=_LINKAGE_SNAPSHOT_ENUM_TYPES,
            max_bytes=MAX_REGISTERED_HISTORY_BYTES,
            max_nodes=MAX_REGISTERED_HISTORY_NODES,
            max_depth=MAX_REGISTERED_HISTORY_DEPTH,
            max_collection_items=MAX_AUTHORITY_CAPTURE_ITEMS,
            max_string_bytes=16_384,
        )
        captured = ActiveLinkageSnapshot.model_validate_json(content)
        if canonical_contract_bytes(captured) != content:
            raise ValueError("cohort linkage snapshot is not canonical")
    except Exception:
        raise CohortImportError("cohort linkage snapshot is invalid") from None
    assert isinstance(captured, ActiveLinkageSnapshot)
    return captured


def _linkage_snapshot_sha256(snapshot: ActiveLinkageSnapshot) -> str:
    return hashlib.sha256(canonical_contract_bytes(snapshot)).hexdigest()


class CohortRecordCatalog:
    """Append-only protected binding index over the immutable result catalog."""

    _PINNED_FIELDS = frozenset(
        {
            "_result_catalog",
            "_result_catalog_identity",
            "_result_trust_store",
            "_result_trust_registry",
            "_result_trust_lock",
            "_result_trust_lock_identity",
            "_result_reader_registry",
            "_catalog_storage_identity_sha256",
            "_catalog_reader_identity_sha256",
            "_catalog_connection_lock",
            "_catalog_connection_lock_identity",
            "_linkage_store",
            "_linkage_store_identity",
            "_cohort_registry",
            "_cohort_registry_identity",
            "_cohort_registry_id",
            "_cohort_registry_epoch_sha256",
            "_expected_trust_snapshot_sha256_by_provider",
            "_reader_registry",
            "_reader_registry_bytes",
            "_recovery_scope_sha256",
            "_fault_controller",
            "_fault_controller_identity",
            "_fault_controller_configuration",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._PINNED_FIELDS and name in vars(self):
            raise AttributeError(f"{name} is read-only")
        super().__setattr__(name, value)

    def __init__(
        self,
        root: str | Path,
        *,
        result_catalog: ResultCatalog,
        linkage_store: ProviderLinkageStore,
        cohort_registry: CohortRegistry,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        reader_registry: ResultBundleReaderRegistry,
        fault_controller: DeterministicFaultController = NO_FAULTS,
    ) -> None:
        if type(result_catalog) is not ResultCatalog:
            raise TypeError("cohort import requires the exact result catalog type")
        for name, pinned in (
            ("verify_reference", _PINNED_RESULT_VERIFY),
            ("query", _PINNED_RESULT_QUERY),
            ("authority_snapshot", _PINNED_RESULT_AUTHORITY),
            ("trust_authority_fence", _PINNED_RESULT_TRUST_FENCE),
            ("prepare_bundle_import", _PINNED_RESULT_PREPARE),
            ("stage_prepared_import", _PINNED_RESULT_STAGE),
            ("adopt_prepared_import", _PINNED_RESULT_ADOPT),
            ("finish_prepared_import", _PINNED_RESULT_FINISH),
            ("compensate_prepared_import", _PINNED_RESULT_COMPENSATE),
            ("recover_pending_publication", _PINNED_RESULT_RECOVER),
            ("pending_publications", _PINNED_RESULT_PENDING),
            ("publication_for_recovery", _PINNED_RESULT_PUBLICATION),
            ("recovery_publications", _PINNED_RESULT_RECOVERY_ROWS),
            ("verify_prepared_object", _PINNED_RESULT_VERIFY_PREPARED),
            ("register_coordinated_candidate", _PINNED_RESULT_REGISTER_CANDIDATE),
            ("coordinated_candidates", _PINNED_RESULT_CANDIDATES),
            ("finish_coordinated_candidate", _PINNED_RESULT_FINISH_CANDIDATE),
        ):
            if (
                name in vars(result_catalog)
                or ResultCatalog.__dict__.get(name) is not pinned
            ):
                raise TypeError("result catalog authority callable was shadowed")
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("cohort import requires the exact linkage store type")
        try:
            _PINNED_REGISTRY_REQUIRE_INTEGRITY(cohort_registry)
        except Exception:
            raise TypeError("cohort import requires the exact cohort registry") from None
        if (
            type(cohort_registry) is not CohortRegistry
            or "resolve_history" in vars(cohort_registry)
            or "list_selectors" in vars(cohort_registry)
        ):
            raise TypeError("cohort import requires the exact cohort registry")
        registry_identity = _PINNED_REGISTRY_LIST(cohort_registry)
        if object.__getattribute__(cohort_registry, "_linkage_store") is not linkage_store:
            raise CohortImportError(
                "cohort registry and import linkage authority do not match"
            )
        if type(reader_registry) is not ResultBundleReaderRegistry:
            raise TypeError("cohort import requires the exact reader registry type")
        if (
            "select" in vars(reader_registry)
            or ResultBundleReaderRegistry.select is not _PINNED_READER_SELECT
        ):
            raise TypeError("cohort import reader registry callable was shadowed")
        try:
            normalized_registry = ResultBundleReaderRegistry.model_validate_json(
                canonical_json_bytes(reader_registry)
            )
        except Exception as exc:
            raise CohortImportError("cohort import reader registry is invalid") from exc
        if normalized_registry != result_catalog.reader_registry:
            raise CohortImportError("result catalog reader registry does not match")
        try:
            pins = capture_expected_trust_pins(
                expected_trust_snapshot_sha256_by_provider
            )
        except ValueError as exc:
            raise CohortImportError("provider trust pins are invalid") from exc
        registry_pins = object.__getattribute__(cohort_registry, "_trust_pins")
        if type(registry_pins) is not dict or registry_pins != pins:
            raise CohortImportError(
                "cohort registry and import trust authority do not match"
            )
        authority = _PINNED_RESULT_AUTHORITY(result_catalog)
        self._result_catalog = result_catalog
        self._result_catalog_identity = id(result_catalog)
        # Exactly one of the two is set: a caller-held TrustStore (whose lock
        # this fence holds) or the protected result-trust registry (whose read
        # fence the catalog's trust_authority_fence holds).
        self._result_trust_store = result_catalog.trust_store
        self._result_trust_registry = result_catalog.result_trust_registry
        trust_lock = (
            None
            if result_catalog.trust_store is None
            else result_catalog.trust_store._lock
        )
        self._result_trust_lock = trust_lock
        self._result_trust_lock_identity = (
            None if trust_lock is None else id(trust_lock)
        )
        self._result_reader_registry = result_catalog.reader_registry
        self._catalog_storage_identity_sha256 = authority.storage_identity_sha256
        self._catalog_reader_identity_sha256 = authority.reader_registry_sha256
        self._catalog_connection_lock = result_catalog._connection_lock
        self._catalog_connection_lock_identity = id(result_catalog._connection_lock)
        self._linkage_store = linkage_store
        self._linkage_store_identity = id(linkage_store)
        self._cohort_registry = cohort_registry
        self._cohort_registry_identity = id(cohort_registry)
        self._cohort_registry_id = registry_identity.registry_id
        self._cohort_registry_epoch_sha256 = (
            registry_identity.registry_epoch_sha256
        )
        self._expected_trust_snapshot_sha256_by_provider = MappingProxyType(pins)
        self._reader_registry = normalized_registry
        self._reader_registry_bytes = canonical_json_bytes(normalized_registry)
        if type(fault_controller) is not DeterministicFaultController:
            raise TypeError("fault controller must be exact")
        self._fault_controller = fault_controller
        self._fault_controller_identity = id(fault_controller)
        self._fault_controller_configuration = _PINNED_FAULT_SNAPSHOT(fault_controller)
        self.root = Path(root).absolute()
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise CohortImportFilesystemError("cohort record index root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError:
            raise CohortImportFilesystemError(
                "cohort record index root is unsafe"
            ) from None
        metadata = os.fstat(self._root_fd)
        if metadata.st_uid != os.geteuid():
            os.close(self._root_fd)
            self._root_fd = None
            raise CohortImportFilesystemError(
                "cohort record index root ownership is unsafe"
            )
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        self._recovery_scope_sha256 = hashlib.sha256(
            b"traceback-cohort-recovery-scope-v1\0"
            + canonical_json_bytes(
                {
                    "configured_root_sha256": hashlib.sha256(
                        os.fsencode(self.root)
                    ).hexdigest(),
                    "root_identity": self._root_identity,
                }
            )
        ).hexdigest()
        try:
            _seal_cohort_instance(self)
            with self._catalog_connection_lock:
                _CC_RECOVER_PENDING(self)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._root_fd = None

    def __del__(self) -> None:
        self.close()

    def _validate_root(self) -> None:
        _CC_ASSERT_RUNTIME(self)
        if self._root_fd is None:
            raise CohortImportFilesystemError("cohort record index is closed")
        try:
            path_stat = os.stat(self.root, follow_symlinks=False)
            fd_stat = os.fstat(self._root_fd)
        except OSError:
            raise CohortImportFilesystemError(
                "cohort record index root changed"
            ) from None
        expected = self._root_identity
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != expected
            or (fd_stat.st_dev, fd_stat.st_ino) != expected
            or path_stat.st_uid != os.geteuid()
            or stat.S_IMODE(path_stat.st_mode) != 0o700
        ):
            raise CohortImportFilesystemError("cohort record index root changed")

    def _validate_reader_registry(self) -> None:
        _CC_VALIDATE_CATALOG_AUTHORITY(self)
        if (
            type(self._reader_registry) is not ResultBundleReaderRegistry
            or "select" in vars(self._reader_registry)
            or ResultBundleReaderRegistry.select is not _PINNED_READER_SELECT
        ):
            raise CohortImportError("cohort import reader registry is invalid")
        try:
            current = canonical_json_bytes(self._reader_registry)
            reparsed = ResultBundleReaderRegistry.model_validate_json(current)
        except Exception:  # noqa: BLE001 - normalize hostile model state
            raise CohortImportError(
                "cohort import reader registry is invalid"
            ) from None
        if (
            current != self._reader_registry_bytes
            or reparsed != self._reader_registry
            or reparsed != self._result_catalog.reader_registry
        ):
            raise CohortImportError("cohort import reader registry changed")

    def _read_pending(self, name: str) -> CohortRecordBinding:
        parts = name.split(".")
        if (
            len(parts) != 4
            or parts[0] != ""
            or parts[1] != "pending"
            or parts[3] != "json"
        ):
            raise CohortImportFilesystemError("pending cohort publication is invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(name, flags, dir_fd=self._root_fd)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > MAX_BINDING_BYTES
                or before.st_uid != os.geteuid()
                or before.st_nlink not in (1, 2)
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise CohortImportFilesystemError(
                    "pending cohort publication is invalid"
                )
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read(MAX_BINDING_BYTES + 1)
                after = os.fstat(stream.fileno())
                descriptor = -1
            if (
                len(content) > MAX_BINDING_BYTES
                or len(content) != before.st_size
                or _file_identity(before) != _file_identity(after)
            ):
                raise CohortImportFilesystemError(
                    "pending cohort publication is invalid"
                )
            binding = contract_from_canonical_bytes(CohortRecordBinding, content)
        except CohortImportFilesystemError:
            raise
        except Exception:  # noqa: BLE001 - normalize hostile file content
            raise CohortImportFilesystemError(
                "pending cohort publication is invalid"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            publication_id = TypeAdapter(PublicationId).validate_python(parts[2])
        except Exception:  # noqa: BLE001 - normalize hostile journal name
            raise CohortImportFilesystemError(
                "pending cohort publication is invalid"
            ) from None
        if binding.publication_id != publication_id or not publication_id.startswith(
            f"publication_{self._recovery_scope_sha256[:16]}_"
        ):
            raise CohortImportFilesystemError("pending cohort publication is invalid")
        return binding

    def _read_rollback_marker(self, name: str) -> CohortImportRollbackMarker:
        parts = name.split(".")
        if (
            len(parts) != 4
            or parts[0] != ""
            or parts[1] != "rollback"
            or parts[3] != "json"
        ):
            raise CohortImportFilesystemError("rollback marker is invalid")
        operation_id = parts[2]
        if (
            type(operation_id) is not str
            or not operation_id.startswith("candidate_")
            or len(operation_id) != 74
            or any(char not in "0123456789abcdef" for char in operation_id[10:])
        ):
            raise CohortImportFilesystemError("rollback marker is invalid") from None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(name, flags, dir_fd=self._root_fd)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_ROLLBACK_MARKER_BYTES
            ):
                raise CohortImportFilesystemError("rollback marker is invalid")
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read(MAX_ROLLBACK_MARKER_BYTES + 1)
                after = os.fstat(stream.fileno())
                descriptor = -1
            marker = contract_from_canonical_bytes(CohortImportRollbackMarker, content)
            if _file_identity(before) != _file_identity(after):
                raise CohortImportFilesystemError("rollback marker is invalid")
        except CohortImportFilesystemError:
            raise
        except Exception:  # noqa: BLE001 - normalize hostile marker content
            raise CohortImportFilesystemError("rollback marker is invalid") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            marker.operation_id != operation_id
            or marker.recovery_scope_sha256 != self._recovery_scope_sha256
        ):
            raise CohortImportFilesystemError("rollback marker is invalid")
        return marker

    def _recover_pending(self) -> None:
        """Reconcile durable journals without ever removing shared objects."""

        with _PROCESS_LOCK:
            _CC_VALIDATE_ROOT(self)
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            try:
                recovered_entries: list[str] = []
                with os.scandir(self._root_fd) as iterator:
                    for entry in iterator:
                        if len(recovered_entries) >= MAX_BINDINGS * 4:
                            raise CohortImportFilesystemError(
                                "cohort record recovery inventory exceeds its bound"
                            )
                        recovered_entries.append(entry.name)
                entries = tuple(sorted(recovered_entries))

                def purge_files(
                    publication_id: str,
                    journal_name: str,
                    *,
                    candidate_final: str | None = None,
                    remove_all_publication_bindings: bool = True,
                ) -> None:
                    journal_inode: tuple[int, int] | None = None
                    try:
                        journal_stat = os.stat(
                            journal_name,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        journal_stat = None
                    if journal_stat is not None:
                        if stat.S_ISDIR(journal_stat.st_mode):
                            raise CohortImportFilesystemError(
                                "pending cohort publication is unsafe"
                            )
                        journal_inode = (journal_stat.st_dev, journal_stat.st_ino)
                    for name in entries:
                        if name.startswith((".pending.", ".rollback.")):
                            continue
                        try:
                            metadata = os.stat(
                                name,
                                dir_fd=self._root_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        same_inode = journal_inode == (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                        exact_candidate = name == candidate_final
                        if exact_candidate and not same_inode:
                            if (
                                not stat.S_ISREG(metadata.st_mode)
                                or metadata.st_uid != os.geteuid()
                                or metadata.st_nlink != 1
                                or stat.S_IMODE(metadata.st_mode) != 0o600
                                or metadata.st_size > MAX_BINDING_BYTES
                            ):
                                raise CohortImportFilesystemError(
                                    "rollback candidate binding is invalid"
                                )
                        matches_publication = False
                        if (
                            remove_all_publication_bindings
                            and stat.S_ISREG(metadata.st_mode)
                            and metadata.st_nlink == 1
                        ):
                            try:
                                matches_publication = (
                                    _CC_READ(self, name).publication_id
                                    == publication_id
                                )
                            except CohortImportFilesystemError:
                                removable_corrupt = (
                                    journal_stat is None
                                    and metadata.st_uid == os.geteuid()
                                    and stat.S_IMODE(metadata.st_mode) == 0o600
                                    and metadata.st_size <= MAX_BINDING_BYTES
                                )
                                if not same_inode and not removable_corrupt:
                                    raise
                                matches_publication = removable_corrupt
                        if same_inode or exact_candidate or matches_publication:
                            os.unlink(name, dir_fd=self._root_fd)
                    if journal_stat is not None:
                        os.unlink(journal_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)

                def has_committed_peer(
                    publication_id: PublicationId, candidate_final: str
                ) -> bool:
                    for name in entries:
                        if name == candidate_final or name.startswith("."):
                            continue
                        try:
                            peer = _CC_READ(self, name)
                        except CohortImportFilesystemError:
                            continue
                        if peer.publication_id == publication_id:
                            return True
                    return False

                candidates = _PINNED_RESULT_CANDIDATES(
                    self._result_catalog, self._recovery_scope_sha256
                )
                for candidate in candidates:
                    pending_name = f".pending.{candidate.publication_id}.json"
                    marker_name = f".rollback.{candidate.operation_id}.json"
                    candidate_binding: CohortRecordBinding | None = None
                    try:
                        observed = _CC_READ_PENDING(self, pending_name)
                        if (
                            hashlib.sha256(
                                canonical_contract_bytes(observed)
                            ).hexdigest()
                            == candidate.binding_sha256
                        ):
                            candidate_binding = observed
                    except CohortImportFilesystemError:
                        pass
                    if candidate_binding is not None and (
                        candidate_binding.publication_id != candidate.publication_id
                        or candidate_binding.result.result_id != candidate.result_id
                        or candidate_binding.cohort_manifest_sha256
                        != candidate.cohort_manifest_sha256
                        or candidate_binding.binding_id != candidate.binding_id
                    ):
                        raise CohortImportFilesystemError(
                            "catalog candidate does not match pending binding"
                        )
                    durable = _PINNED_RESULT_PUBLICATION(
                        self._result_catalog,
                        candidate.publication_id,
                        self._recovery_scope_sha256,
                    )
                    if (
                        durable is not None
                        and durable.reference.result_id != candidate.result_id
                    ):
                        raise CohortImportFilesystemError(
                            "catalog candidate does not match ownership"
                        )
                    retain_owner = has_committed_peer(
                        candidate.publication_id, candidate.final_name
                    )
                    if durable is not None:
                        _PINNED_RESULT_RECOVER(
                            self._result_catalog,
                            publication_id=durable.publication_id,
                            reference=durable.reference,
                            recovery_scope_sha256=self._recovery_scope_sha256,
                            retain_adopted=retain_owner,
                        )
                    # Replaceable marker bytes never authorize deletion. The durable
                    # candidate permits only this exact final binding identity.
                    try:
                        final_binding = _CC_READ(self, candidate.final_name)
                    except CohortImportFilesystemError:
                        final_binding = None
                    if (
                        final_binding is not None
                        and hashlib.sha256(
                            canonical_contract_bytes(final_binding)
                        ).hexdigest()
                        == candidate.binding_sha256
                    ):
                        os.unlink(candidate.final_name, dir_fd=self._root_fd)
                    for transient_name in (pending_name, marker_name):
                        try:
                            metadata = os.stat(
                                transient_name,
                                dir_fd=self._root_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or metadata.st_nlink not in {1, 2}
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_size > MAX_BINDING_BYTES
                        ):
                            raise CohortImportFilesystemError(
                                "candidate recovery file is unsafe"
                            )
                        os.unlink(transient_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                    _PINNED_RESULT_FINISH_CANDIDATE(self._result_catalog, candidate)

                # A marker without its durable candidate has no deletion authority.
                recovered_entries = []
                with os.scandir(self._root_fd) as iterator:
                    for entry in iterator:
                        if len(recovered_entries) >= MAX_BINDINGS * 4:
                            raise CohortImportFilesystemError(
                                "cohort record recovery inventory exceeds its bound"
                            )
                        recovered_entries.append(entry.name)
                entries = tuple(sorted(recovered_entries))
                for rollback_name in (
                    name for name in entries if name.startswith(".rollback.")
                ):
                    try:
                        metadata = os.stat(
                            rollback_name,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_size > MAX_ROLLBACK_MARKER_BYTES
                    ):
                        raise CohortImportFilesystemError("rollback marker is invalid")
                    os.unlink(rollback_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)

                pending_rows: tuple[PendingCatalogPublication, ...] = (
                    _PINNED_RESULT_PENDING(
                        self._result_catalog, self._recovery_scope_sha256
                    )
                )
                for pending in pending_rows:
                    pending_name = f".pending.{pending.publication_id}.json"
                    try:
                        binding = _CC_READ_PENDING(self, pending_name)
                    except CohortImportFilesystemError:
                        binding = None
                    if binding is None or binding.result != pending.reference:
                        _PINNED_RESULT_RECOVER(
                            self._result_catalog,
                            publication_id=pending.publication_id,
                            reference=pending.reference,
                            recovery_scope_sha256=self._recovery_scope_sha256,
                            retain_adopted=False,
                        )
                        purge_files(pending.publication_id, pending_name)

                pending_names = tuple(
                    name for name in entries if name.startswith(".pending.")
                )
                for pending_name in pending_names:
                    try:
                        binding = _CC_READ_PENDING(self, pending_name)
                    except CohortImportFilesystemError:
                        parts = pending_name.split(".")
                        publication_id = parts[2] if len(parts) == 4 else ""
                        durable = _PINNED_RESULT_PUBLICATION(
                            self._result_catalog,
                            publication_id,
                            self._recovery_scope_sha256,
                        )
                        if durable is not None:
                            _PINNED_RESULT_RECOVER(
                                self._result_catalog,
                                publication_id=durable.publication_id,
                                reference=durable.reference,
                                recovery_scope_sha256=self._recovery_scope_sha256,
                                retain_adopted=False,
                            )
                            purge_files(durable.publication_id, pending_name)
                            continue
                        try:
                            metadata = os.stat(
                                pending_name,
                                dir_fd=self._root_fd,
                                follow_symlinks=False,
                            )
                        except OSError:
                            raise CohortImportFilesystemError(
                                "pending cohort publication is invalid"
                            ) from None
                        if (
                            stat.S_ISREG(metadata.st_mode)
                            and metadata.st_uid == os.geteuid()
                            and metadata.st_nlink == 1
                            and stat.S_IMODE(metadata.st_mode) == 0o600
                            and metadata.st_size <= MAX_BINDING_BYTES
                        ):
                            # Staging begins only after a canonical journal fsync.
                            # A single-link malformed file is therefore a crashed
                            # pre-stage write and has no catalog row to compensate.
                            os.unlink(pending_name, dir_fd=self._root_fd)
                            os.fsync(self._root_fd)
                            continue
                        raise
                    final_name = (
                        f"{binding.cohort_manifest_sha256}.{binding.binding_id}.json"
                    )
                    retain_owner = has_committed_peer(
                        binding.publication_id, final_name
                    )
                    _PINNED_RESULT_RECOVER(
                        self._result_catalog,
                        publication_id=binding.publication_id,
                        reference=binding.result,
                        recovery_scope_sha256=self._recovery_scope_sha256,
                        retain_adopted=retain_owner,
                    )
                    # A retained journal is always an incomplete import. Remove
                    # only its hard-linked candidate; a committed peer may keep
                    # the pre-existing ownership row.
                    purge_files(
                        binding.publication_id,
                        pending_name,
                        candidate_final=final_name,
                        remove_all_publication_bindings=False,
                    )
                valid_publications: set[str] = set()
                valid_result_ids: set[str] = set()
                for name in entries:
                    parts = name.split(".")
                    looks_like_binding = (
                        len(parts) == 3
                        and len(parts[0]) == 64
                        and parts[1].startswith("binding_")
                        and len(parts[1]) == 72
                        and parts[2] == "json"
                    )
                    if not looks_like_binding:
                        continue
                    try:
                        recovered_binding = _CC_READ(self, name)
                        durable = _PINNED_RESULT_PUBLICATION(
                            self._result_catalog,
                            recovered_binding.publication_id,
                            self._recovery_scope_sha256,
                        )
                        if (
                            durable is None
                            or durable.state != "adopted"
                            or durable.reference != recovered_binding.result
                        ):
                            os.unlink(name, dir_fd=self._root_fd)
                            os.fsync(self._root_fd)
                            continue
                        valid_publications.add(recovered_binding.publication_id)
                        valid_result_ids.add(recovered_binding.result.result_id)
                    except CohortImportFilesystemError:
                        metadata = os.stat(
                            name,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_size > MAX_BINDING_BYTES
                        ):
                            raise
                        os.unlink(name, dir_fd=self._root_fd)
                        os.fsync(self._root_fd)
                for publication in _PINNED_RESULT_RECOVERY_ROWS(
                    self._result_catalog, self._recovery_scope_sha256
                ):
                    if (
                        publication.state == "adopted"
                        and publication.publication_id not in valid_publications
                        and publication.reference.result_id not in valid_result_ids
                    ):
                        _PINNED_RESULT_RECOVER(
                            self._result_catalog,
                            publication_id=publication.publication_id,
                            reference=publication.reference,
                            recovery_scope_sha256=self._recovery_scope_sha256,
                            retain_adopted=False,
                        )
                _CC_VALIDATE_ROOT(self)
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)

    def _validate_catalog_authority(self) -> CatalogAuthoritySnapshot:
        _CC_ASSERT_RUNTIME(self)
        catalog = self._result_catalog
        if (
            type(catalog) is not ResultCatalog
            or id(catalog) != self._result_catalog_identity
            or catalog.trust_store is not self._result_trust_store
            or catalog.result_trust_registry is not self._result_trust_registry
            or (self._result_trust_store is None)
            == (self._result_trust_registry is None)
            or (
                self._result_trust_store is not None
                and (
                    catalog.trust_store._lock is not self._result_trust_lock
                    or id(catalog.trust_store._lock)
                    != self._result_trust_lock_identity
                )
            )
            or catalog.reader_registry is not self._result_reader_registry
            or catalog._connection_lock is not self._catalog_connection_lock
            or id(catalog._connection_lock)
            != self._catalog_connection_lock_identity
        ):
            raise CohortImportError("result catalog authority changed")
        for name, pinned in (
            ("verify_reference", _PINNED_RESULT_VERIFY),
            ("query", _PINNED_RESULT_QUERY),
            ("authority_snapshot", _PINNED_RESULT_AUTHORITY),
            ("trust_authority_fence", _PINNED_RESULT_TRUST_FENCE),
            ("prepare_bundle_import", _PINNED_RESULT_PREPARE),
            ("stage_prepared_import", _PINNED_RESULT_STAGE),
            ("adopt_prepared_import", _PINNED_RESULT_ADOPT),
            ("finish_prepared_import", _PINNED_RESULT_FINISH),
            ("compensate_prepared_import", _PINNED_RESULT_COMPENSATE),
            ("recover_pending_publication", _PINNED_RESULT_RECOVER),
            ("pending_publications", _PINNED_RESULT_PENDING),
            ("publication_for_recovery", _PINNED_RESULT_PUBLICATION),
            ("recovery_publications", _PINNED_RESULT_RECOVERY_ROWS),
            ("verify_prepared_object", _PINNED_RESULT_VERIFY_PREPARED),
            ("register_coordinated_candidate", _PINNED_RESULT_REGISTER_CANDIDATE),
            ("coordinated_candidates", _PINNED_RESULT_CANDIDATES),
            ("finish_coordinated_candidate", _PINNED_RESULT_FINISH_CANDIDATE),
        ):
            if name in vars(catalog) or ResultCatalog.__dict__.get(name) is not pinned:
                raise CohortImportError("result catalog authority changed")
        if (
            type(self._linkage_store) is not ProviderLinkageStore
            or id(self._linkage_store) != self._linkage_store_identity
        ):
            raise CohortImportError("provider linkage authority changed")
        _PINNED_RESULT_RUNTIME_ASSERT(catalog)
        if (
            result_catalog_module._PINNED_VERIFY_BUNDLE
            is not _PINNED_RESULT_MODULE_VERIFY
            or result_catalog_module._PINNED_TRUST_RESOLVE
            is not _PINNED_RESULT_MODULE_TRUST_RESOLVE
            or result_catalog_module._RC_ASSERT_RUNTIME
            is not _PINNED_RESULT_RUNTIME_ASSERT
            or cohort_manifest_module._PINNED_ACTIVE_SNAPSHOT
            is not _PINNED_MANIFEST_ACTIVE_SNAPSHOT
            or cohort_manifest_module._PINNED_STORE_CALLABLES
            is not _PINNED_MANIFEST_STORE_CALLABLES
        ):
            raise CohortImportError("verification module authority changed")
        authority = _PINNED_RESULT_AUTHORITY(catalog)
        if (
            authority.storage_identity_sha256 != self._catalog_storage_identity_sha256
            or authority.reader_registry_sha256 != self._catalog_reader_identity_sha256
        ):
            raise CohortImportError("result catalog authority changed")
        return authority

    def _fault(self, point: str) -> None:
        controller = self._fault_controller
        try:
            snapshot = _PINNED_FAULT_SNAPSHOT(controller)
        except (TypeError, ValueError):
            raise CohortImportError("cohort fault controller changed") from None
        if id(controller) != self._fault_controller_identity or (
            snapshot != self._fault_controller_configuration
        ):
            raise CohortImportError("cohort fault controller changed")
        try:
            _PINNED_FAULT_HIT(controller, point)
        except (TypeError, ValueError):
            raise CohortImportError("cohort fault controller changed") from None

    def _validate_manifest(
        self, manifest: CohortManifest, *, changed: bool = False
    ) -> None:
        _CC_VALIDATE_CATALOG_AUTHORITY(self)
        try:
            _PINNED_VALIDATE_MANIFEST_IN_FENCE(
                manifest,
                self._linkage_store,
                expected_trust_snapshot_sha256_by_provider=(
                    self._expected_trust_snapshot_sha256_by_provider
                ),
            )
        except Exception as exc:
            message = (
                "cohort manifest changed during import"
                if changed
                else "cohort manifest is not current and trusted"
            )
            raise CohortImportError(message) from exc

    def _resolve_registered_history_in_fence(
        self,
        selector_id: str,
        cohort_version: int,
        *,
        expected: RegisteredCohortHistory | None = None,
    ) -> RegisteredCohortHistory:
        registry = self._cohort_registry
        if (
            type(registry) is not CohortRegistry
            or id(registry) != self._cohort_registry_identity
            or object.__getattribute__(registry, "_linkage_store")
            is not self._linkage_store
        ):
            raise CohortImportError("cohort registry authority changed")
        try:
            _PINNED_REGISTRY_REQUIRE_INTEGRITY(registry)
            history = _PINNED_REGISTRY_RESOLVE_IN_FENCE(
                registry, selector_id, cohort_version
            )
        except Exception:
            raise CohortImportError(
                "cohort registry selection is not current and trusted"
            ) from None
        history = _capture_registered_history(history)
        if (
            history.registry_id != self._cohort_registry_id
            or history.registry_epoch_sha256
            != self._cohort_registry_epoch_sha256
        ):
            raise CohortImportError("cohort registry authority changed")
        if expected is not None and history != expected:
            raise CohortImportConflict("cohort registry state changed")
        return history

    @contextmanager
    def _registered_authority_fence(
        self, selector_id: str, cohort_version: int
    ) -> Iterator[
        tuple[RegisteredCohortHistory, ActiveLinkageSnapshot, tuple[str, ...]]
    ]:
        """Fence linkage, registry and exact selector authority through return."""

        _CC_ASSERT_RUNTIME(self)
        if (
            type(selector_id) is not str
            or len(selector_id) != 56
            or not selector_id.startswith("cohort_selector_")
            or any(character not in "0123456789abcdef" for character in selector_id[16:])
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise CohortImportError("cohort registry selector is invalid")
        registry = self._cohort_registry
        stack = ExitStack()
        try:
            _PINNED_REGISTRY_REQUIRE_INTEGRITY(registry)
            stack.enter_context(_PINNED_LINKAGE_AUTHORITY_FENCE(self._linkage_store))
            stack.enter_context(_PINNED_REGISTRY_LOCK(registry, exclusive=False))
            # Lock order: linkage fence, D05 lock, catalog connection lock,
            # then result trust (the TrustStore lock, or the registry read
            # fence held by the catalog's trust_authority_fence).
            stack.enter_context(self._catalog_connection_lock)
            if self._result_trust_lock is not None:
                stack.enter_context(self._result_trust_lock)
            stack.enter_context(_PINNED_RESULT_TRUST_FENCE(self._result_catalog))
            history = _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                self, selector_id, cohort_version
            )
            linkage_snapshot = _capture_linkage_snapshot(
                _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store)
            )
            journal = _PINNED_REGISTRY_LOAD_JOURNAL(registry)
            registry_state_heads = (
                object.__getattribute__(registry, "_genesis_head_sha256"),
                *(item.entry_sha256 for item in journal),
            )
            if history.state_version != len(journal):
                raise CohortImportConflict("cohort registry state changed")
        except CohortImportError:
            stack.close()
            raise
        except Exception:
            stack.close()
            raise CohortImportError(
                "cohort registry selection is not current and trusted"
            ) from None
        try:
            yield history, linkage_snapshot, registry_state_heads
        finally:
            try:
                final_history = _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                    self,
                    selector_id,
                    cohort_version,
                    expected=history,
                )
                final_snapshot = _capture_linkage_snapshot(
                    _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store)
                )
                final_journal = _PINNED_REGISTRY_LOAD_JOURNAL(registry)
                final_heads = (
                    object.__getattribute__(registry, "_genesis_head_sha256"),
                    *(item.entry_sha256 for item in final_journal),
                )
                if (
                    final_history != history
                    or final_snapshot != linkage_snapshot
                    or final_heads != registry_state_heads
                ):
                    raise CohortImportConflict(
                        "cohort registry or linkage authority changed"
                    )
                _PINNED_REGISTRY_REQUIRE_INTEGRITY(registry)
            except CohortImportError:
                raise
            except Exception:
                raise CohortImportError(
                    "cohort registry selection is not current and trusted"
                ) from None
            finally:
                stack.close()

    def _read(self, name: str) -> CohortRecordBinding:
        _CC_VALIDATE_ROOT(self)
        parts = name.split(".")
        if (
            len(parts) != 3
            or len(parts[0]) != 64
            or any(char not in "0123456789abcdef" for char in parts[0])
            or len(parts[1]) != 72
            or not parts[1].startswith("binding_")
            or any(char not in "0123456789abcdef" for char in parts[1][8:])
            or parts[2] != "json"
        ):
            raise CohortImportFilesystemError(
                "cohort record index inventory is invalid"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(name, flags, dir_fd=self._root_fd)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_BINDING_BYTES
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise CohortImportFilesystemError(
                    "cohort record binding file is invalid"
                )
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read(MAX_BINDING_BYTES + 1)
                after = os.fstat(stream.fileno())
                descriptor = -1
            rebound = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if (
                len(content) > MAX_BINDING_BYTES
                or len(content) != metadata.st_size
                or _file_identity(metadata) != _file_identity(after)
                or _file_identity(metadata) != _file_identity(rebound)
            ):
                raise CohortImportFilesystemError(
                    "cohort record binding file is invalid"
                )
            binding = contract_from_canonical_bytes(CohortRecordBinding, content)
        except CohortImportFilesystemError:
            raise
        except Exception:  # noqa: BLE001 - normalize hostile file content
            raise CohortImportFilesystemError(
                "cohort record binding file is invalid"
            ) from None
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
        if name != (f"{binding.cohort_manifest_sha256}.{binding.binding_id}.json"):
            raise CohortImportFilesystemError("cohort record binding name is invalid")
        _CC_VALIDATE_ROOT(self)
        return binding

    def _names_unlocked(self) -> tuple[str, ...]:
        _CC_VALIDATE_ROOT(self)
        captured: list[str] = []
        with os.scandir(self._root_fd) as iterator:
            for entry in iterator:
                if len(captured) >= MAX_BINDINGS:
                    raise CohortImportFilesystemError(
                        "cohort record index exceeds its bound"
                    )
                captured.append(entry.name)
        names = tuple(sorted(captured))
        for name in names:
            parts = name.split(".")
            if (
                len(parts) != 3
                or len(parts[0]) != 64
                or any(char not in "0123456789abcdef" for char in parts[0])
                or len(parts[1]) != 72
                or not parts[1].startswith("binding_")
                or any(char not in "0123456789abcdef" for char in parts[1][8:])
                or parts[2] != "json"
            ):
                raise CohortImportFilesystemError(
                    "cohort record index inventory is invalid"
                )
        return names

    @staticmethod
    def _binding_equivalent(
        existing: CohortRecordBinding, candidate: CohortRecordBinding
    ) -> bool:
        ignored = {
            "binding_id",
            "publication_id",
            "catalog_result_preexisting",
            "registry_state_version",
            "registry_state_head_sha256",
        }
        return existing.model_dump(exclude=ignored) == candidate.model_dump(
            exclude=ignored
        )

    def _write_pending(self, name: str, content: bytes) -> None:
        _CC_VALIDATE_ROOT(self)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            raise CohortImportConflict("pending cohort publication conflicts") from None
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
            os.fsync(self._root_fd)
        except BaseException:
            try:
                os.unlink(name, dir_fd=self._root_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
        _CC_VALIDATE_ROOT(self)

    def _write_rollback_marker(self, marker: CohortImportRollbackMarker) -> None:
        if type(marker) is not CohortImportRollbackMarker:
            raise CohortImportFilesystemError("rollback marker is invalid")
        name = f".rollback.{marker.operation_id}.json"
        content = canonical_contract_bytes(marker)
        if len(content) > MAX_ROLLBACK_MARKER_BYTES:
            raise CohortImportFilesystemError("rollback marker exceeds its bound")
        _CC_VALIDATE_ROOT(self)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            if _CC_READ_ROLLBACK(self, name) != marker:
                raise CohortImportFilesystemError("rollback marker conflicts")
            return
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
            os.fsync(self._root_fd)
        except BaseException:
            try:
                os.unlink(name, dir_fd=self._root_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
        _CC_VALIDATE_ROOT(self)

    def _revalidate_publication(
        self,
        selector_id: str,
        registered_history: RegisteredCohortHistory,
        manifest: CohortManifest,
        prepared: PreparedCatalogImport,
        binding: CohortRecordBinding,
        final_name: str | None,
        *,
        changed: bool,
    ) -> None:
        _CC_VALIDATE_ROOT(self)
        authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        if (
            authority != prepared.authority
            or catalog_authority_sha256(authority) != binding.catalog_authority_sha256
            or authority.storage_identity_sha256
            != binding.catalog_storage_identity_sha256
            or authority.trust_snapshot_sha256 != binding.result_trust_snapshot_sha256
        ):
            raise CohortImportConflict("result catalog authority changed")
        _CC_VALIDATE_MANIFEST(self, manifest, changed=changed)
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            manifest.version,
            expected=registered_history,
        )
        _PINNED_RESULT_VERIFY_PREPARED(self._result_catalog, prepared)
        if final_name is not None:
            pending_name = f".pending.{binding.publication_id}.json"
            if _CC_READ_PENDING(self, pending_name) != binding:
                raise CohortImportConflict("cohort record binding changed")
            try:
                pending_stat = os.stat(
                    pending_name, dir_fd=self._root_fd, follow_symlinks=False
                )
                final_stat = os.stat(
                    final_name, dir_fd=self._root_fd, follow_symlinks=False
                )
            except OSError:
                raise CohortImportConflict("cohort record binding changed") from None
            if (
                (pending_stat.st_dev, pending_stat.st_ino)
                != (final_stat.st_dev, final_stat.st_ino)
                or pending_stat.st_nlink != 2
                or final_stat.st_nlink != 2
            ):
                raise CohortImportConflict("cohort record binding changed")
        _CC_VALIDATE_ROOT(self)

    def import_bundle(
        self,
        *,
        selector_id: str,
        cohort_version: int,
        provider_namespace: str,
        analysis_record_id: str,
        root_id: str,
        relative_path: str,
        registry: MethodRegistry,
        authority_head: AuthorityHead,
        expected_authority_head_sha256: str,
        capability: CurrentMethodCapability,
    ) -> CohortRecordBinding:
        """Verify and atomically bind one aggregate record to current D05 authority."""

        _CC_ASSERT_RUNTIME(self)
        registry = _CC_CAPTURE_IMPORT_AUTHORITY(registry, MethodRegistry)
        authority_head = _CC_CAPTURE_IMPORT_AUTHORITY(authority_head, AuthorityHead)
        capability = _CC_CAPTURE_IMPORT_AUTHORITY(
            capability, CurrentMethodCapability
        )
        assert isinstance(registry, MethodRegistry)
        assert isinstance(authority_head, AuthorityHead)
        assert isinstance(capability, CurrentMethodCapability)
        with _CC_REGISTERED_AUTHORITY_FENCE(
            self, selector_id, cohort_version
        ) as (registered_history, linkage_snapshot, registry_state_heads):
            with self._catalog_connection_lock:
                return _CC_IMPORT_BODY(
                    self,
                    selector_id=selector_id,
                    cohort_version=cohort_version,
                    registered_history=registered_history,
                    linkage_snapshot=linkage_snapshot,
                    registry_state_heads=registry_state_heads,
                    provider_namespace=provider_namespace,
                    analysis_record_id=analysis_record_id,
                    root_id=root_id,
                    relative_path=relative_path,
                    registry=registry,
                    authority_head=authority_head,
                    expected_authority_head_sha256=expected_authority_head_sha256,
                    capability=capability,
                )

    def _import_bundle_in_fence(
        self,
        *,
        selector_id: str,
        cohort_version: int,
        registered_history: RegisteredCohortHistory,
        linkage_snapshot: ActiveLinkageSnapshot,
        registry_state_heads: tuple[str, ...],
        provider_namespace: str,
        analysis_record_id: str,
        root_id: str,
        relative_path: str,
        registry: MethodRegistry,
        authority_head: AuthorityHead,
        expected_authority_head_sha256: str,
        capability: CurrentMethodCapability,
    ) -> CohortRecordBinding:
        if (
            registered_history.state_version >= len(registry_state_heads)
            or registry_state_heads[registered_history.state_version]
            != registered_history.state_head_sha256
        ):
            raise CohortImportConflict("cohort registry state changed")
        history = registered_history.manifests
        manifest = history[-1]
        _CC_VALIDATE_READER_REGISTRY(self)
        try:
            provider_namespace = _PROVIDER_NAMESPACE.validate_python(provider_namespace)
            analysis_record_id = _ANALYSIS_RECORD_ID.validate_python(analysis_record_id)
            expected_authority_head_sha256 = _SHA256.validate_python(
                expected_authority_head_sha256
            )
        except Exception as exc:
            raise CohortImportError("cohort record selector is invalid") from exc
        _CC_VALIDATE_MANIFEST(self, manifest)
        members = tuple(
            member
            for member in manifest.members
            if member.provider_namespace == provider_namespace
            and member.analysis_record_id == analysis_record_id
        )
        if len(members) != 1:
            raise CohortImportError("record is not one exact cohort member")
        member = members[0]
        if (
            manifest.measurement_anchor.measurement_definition_sha256
            != capability.method_definition_sha256
        ):
            raise CohortImportError(
                "record method does not match cohort measurement anchor"
            )
        aliases = CatalogAliases(
            display_alias=_opaque_alias(
                "dsp",
                b"traceback-cohort-display-alias-v1",
                (member.provider_namespace, member.analysis_record_id),
            ),
            run_alias=_opaque_alias(
                "rnx",
                b"traceback-cohort-run-alias-v1",
                (member.provider_namespace, member.run_token),
            ),
            timepoint_alias=_opaque_alias(
                "tpt",
                b"traceback-cohort-timepoint-alias-v1",
                (member.provider_namespace, member.collection_token),
            ),
        )
        prepared: PreparedCatalogImport | None = None
        binding: CohortRecordBinding | None = None
        temporary_name: str | None = None
        final_name: str | None = None
        rollback_name: str | None = None
        candidate: CoordinatedCatalogCandidate | None = None
        final_published = False
        cleanup_attempted_under_lock = False

        def cleanup_import() -> None:
            nonlocal candidate, prepared, rollback_name, temporary_name, final_published
            cleanup_errors: list[BaseException] = []
            binding_removed = not final_published
            if final_published and final_name is not None:
                try:
                    os.unlink(final_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                    final_published = False
                    binding_removed = True
                except FileNotFoundError:
                    final_published = False
                    binding_removed = True
                except OSError as exc:
                    cleanup_errors.append(exc)
            # Never remove a visible result while its binding could remain. The
            # retained ownership row and journal let startup recovery retry safely.
            rollback_complete = binding_removed
            if prepared is not None and binding_removed:
                try:
                    _PINNED_RESULT_COMPENSATE(self._result_catalog, prepared)
                    prepared = None
                except Exception as exc:  # noqa: BLE001 - surface failed compensation
                    cleanup_errors.append(exc)
                    rollback_complete = False
            if prepared is not None and binding is not None and not rollback_complete:
                try:
                    _CC_WRITE_ROLLBACK(self, binding)
                except Exception as exc:  # noqa: BLE001 - preserve original failure
                    cleanup_errors.append(exc)
            # The pending journal is durable rollback intent. Remove it only
            # after both the binding and exact catalog ownership are gone.
            if temporary_name is not None and rollback_complete:
                try:
                    os.unlink(temporary_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                    temporary_name = None
                except FileNotFoundError:
                    temporary_name = None
                except OSError as exc:
                    cleanup_errors.append(exc)
            if rollback_name is not None and rollback_complete:
                try:
                    os.unlink(rollback_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                    rollback_name = None
                except FileNotFoundError:
                    rollback_name = None
                except OSError as exc:
                    cleanup_errors.append(exc)
            if candidate is not None and rollback_complete:
                try:
                    _PINNED_RESULT_FINISH_CANDIDATE(self._result_catalog, candidate)
                    candidate = None
                except Exception as exc:  # noqa: BLE001 - retry during recovery
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise CohortImportError(
                    "cohort import compensation failed"
                ) from cleanup_errors[0]

        try:
            prepared = _PINNED_RESULT_PREPARE(
                self._result_catalog,
                root_id=root_id,
                relative_path=relative_path,
                registry=registry,
                authority_head=authority_head,
                expected_authority_head_sha256=expected_authority_head_sha256,
                capability=capability,
                aliases=aliases,
                recovery_scope_sha256=self._recovery_scope_sha256,
            )
            _CC_FAULT(self, "after_preflight")
            authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
            if authority != prepared.authority:
                raise CohortImportConflict("result catalog authority changed")
            _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
            _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                self,
                selector_id,
                cohort_version,
                expected=registered_history,
            )
            _, reader = _PINNED_RESULT_VERIFY_PREPARED(self._result_catalog, prepared)
            manifest_digest = cohort_manifest_sha256(manifest)
            binding = CohortRecordBinding(
                binding_id=_binding_id(
                    registry_id=registered_history.registry_id,
                    registry_epoch_sha256=(
                        registered_history.registry_epoch_sha256
                    ),
                    registry_state_version=registered_history.state_version,
                    registry_state_head_sha256=(
                        registered_history.state_head_sha256
                    ),
                    selector_id=selector_id,
                    cohort_manifest_sha256=manifest_digest,
                    linkage_snapshot_sha256=_linkage_snapshot_sha256(
                        linkage_snapshot
                    ),
                    inclusion_policy_sha256=manifest.policies.inclusion_sha256,
                    exclusion_policy_sha256=manifest.policies.exclusion_sha256,
                    missingness_policy_sha256=manifest.policies.missingness_sha256,
                    record_status_policy_sha256=(
                        COHORT_RECORD_STATUS_POLICY_SHA256
                    ),
                    provider_namespace=member.provider_namespace,
                    analysis_record_id=member.analysis_record_id,
                    result_id=prepared.reference.result_id,
                    publication_id=prepared.publication_id,
                    catalog_authority_sha256=prepared.authority_sha256,
                ),
                registry_id=registered_history.registry_id,
                registry_epoch_sha256=registered_history.registry_epoch_sha256,
                registry_state_version=registered_history.state_version,
                registry_state_head_sha256=(
                    registered_history.state_head_sha256
                ),
                selector_id=selector_id,
                cohort_id=manifest.cohort_id,
                cohort_version=manifest.version,
                cohort_manifest_sha256=manifest_digest,
                linkage_store_id=linkage_snapshot.store_id,
                linkage_store_epoch_sha256=linkage_snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=(
                    linkage_snapshot.storage_identity_sha256
                ),
                linkage_state_version=linkage_snapshot.state_version,
                linkage_state_head_sha256=linkage_snapshot.state_head_sha256,
                linkage_snapshot_sha256=_linkage_snapshot_sha256(linkage_snapshot),
                inclusion_policy_sha256=manifest.policies.inclusion_sha256,
                exclusion_policy_sha256=manifest.policies.exclusion_sha256,
                missingness_policy_sha256=manifest.policies.missingness_sha256,
                record_status_policy_sha256=COHORT_RECORD_STATUS_POLICY_SHA256,
                provider_namespace=member.provider_namespace,
                analysis_record_id=member.analysis_record_id,
                member_sha256=_member_sha256(member),
                lineage_role=member.lineage_role,
                denominator_contribution=member.denominator_contribution,
                measurement_anchor_sha256=(
                    manifest.measurement_anchor.measurement_definition_sha256
                ),
                result=prepared.reference,
                publication_id=prepared.publication_id,
                catalog_authority_sha256=prepared.authority_sha256,
                catalog_storage_identity_sha256=(
                    prepared.authority.storage_identity_sha256
                ),
                result_trust_snapshot_sha256=(prepared.authority.trust_snapshot_sha256),
                catalog_result_preexisting=prepared.already_visible,
                reader_registry_sha256=_reader_registry_sha256(self._reader_registry),
                reader_id=reader.reader_id,
                reader_minimum_version=reader.minimum_version,
                reader_maximum_version=reader.maximum_version,
            )
            content = canonical_contract_bytes(binding)
            final_name = f"{manifest_digest}.{binding.binding_id}.json"
            temporary_name = f".pending.{prepared.publication_id}.json"
            operation_id = (
                "candidate_"
                + hashlib.sha256(
                    b"traceback-cohort-import-candidate-v1\0" + content
                ).hexdigest()
            )
            marker = CohortImportRollbackMarker(
                operation_id=operation_id,
                publication_id=binding.publication_id,
                recovery_scope_sha256=self._recovery_scope_sha256,
                cohort_manifest_sha256=binding.cohort_manifest_sha256,
                binding_id=binding.binding_id,
                result_id=binding.result.result_id,
            )
            marker_bytes = canonical_contract_bytes(marker)
            candidate = CoordinatedCatalogCandidate(
                operation_id=operation_id,
                publication_id=binding.publication_id,
                result_id=binding.result.result_id,
                recovery_scope_sha256=self._recovery_scope_sha256,
                cohort_manifest_sha256=binding.cohort_manifest_sha256,
                binding_id=binding.binding_id,
                final_name=final_name,
                binding_sha256=hashlib.sha256(content).hexdigest(),
                marker_sha256=hashlib.sha256(marker_bytes).hexdigest(),
            )
            rollback_name = f".rollback.{operation_id}.json"
            with _PROCESS_LOCK:
                _CC_VALIDATE_ROOT(self)
                fcntl.flock(self._root_fd, fcntl.LOCK_EX)
                try:
                    names = _CC_NAMES_UNLOCKED(self)
                    existing = tuple(
                        _CC_READ(self, name)
                        for name in names
                        if name.startswith(f"{manifest_digest}.")
                    )
                    for item in existing:
                        same_member = (
                            item.provider_namespace == binding.provider_namespace
                            and item.analysis_record_id == binding.analysis_record_id
                        )
                        same_result = item.result.result_id == binding.result.result_id
                        if same_member or same_result:
                            if (
                                item.registry_state_version
                                >= len(registry_state_heads)
                                or registry_state_heads[item.registry_state_version]
                                != item.registry_state_head_sha256
                            ):
                                raise CohortImportConflict(
                                    "cohort record registry authority changed"
                                )
                            if _CC_BINDING_EQUIVALENT(item, binding):
                                _CC_FAULT(self, "before_idempotent_return")
                                _CC_REVALIDATE_PUBLICATION(
                                    self,
                                    selector_id,
                                    registered_history,
                                    manifest,
                                    prepared,
                                    item,
                                    None,
                                    changed=True,
                                )
                                prepared_authority = prepared.authority
                                _PINNED_RESULT_COMPENSATE(
                                    self._result_catalog, prepared
                                )
                                prepared = None
                                _CC_VALIDATE_ROOT(self)
                                _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
                                _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                                    self,
                                    selector_id,
                                    cohort_version,
                                    expected=registered_history,
                                )
                                if (
                                    _CC_VALIDATE_CATALOG_AUTHORITY(self)
                                    != prepared_authority
                                ):
                                    raise CohortImportConflict(
                                        "result catalog authority changed"
                                    )
                                existing_name = (
                                    f"{item.cohort_manifest_sha256}."
                                    f"{item.binding_id}.json"
                                )
                                if _CC_READ(self, existing_name) != item:
                                    raise CohortImportConflict(
                                        "cohort record binding changed"
                                    )
                                _PINNED_RESULT_VERIFY(self._result_catalog, item.result)
                                _CC_VALIDATE_ROOT(self)
                                _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
                                _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                                    self,
                                    selector_id,
                                    cohort_version,
                                    expected=registered_history,
                                )
                                return item
                            raise CohortImportConflict(
                                "cohort record binding conflicts"
                            )
                    if len(names) >= MAX_BINDINGS:
                        raise CohortImportFilesystemError(
                            "cohort record index exceeds its bound"
                        )
                    _CC_WRITE_PENDING(self, temporary_name, content)
                    _PINNED_RESULT_STAGE(self._result_catalog, prepared)
                    _PINNED_RESULT_REGISTER_CANDIDATE(
                        self._result_catalog, prepared, candidate
                    )
                    _CC_FAULT(self, "after_result_stage")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        None,
                        changed=True,
                    )
                    _CC_FAULT(self, "before_binding_publish")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        None,
                        changed=True,
                    )
                    _CC_VALIDATE_ROOT(self)
                    try:
                        os.link(
                            temporary_name,
                            final_name,
                            src_dir_fd=self._root_fd,
                            dst_dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        raise CohortImportConflict(
                            "cohort record binding conflicts"
                        ) from None
                    final_published = True
                    os.fsync(self._root_fd)
                    _CC_VALIDATE_ROOT(self)
                    _CC_FAULT(self, "after_binding_publish")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        final_name,
                        changed=True,
                    )

                    # Rollback intent must be durable before visibility. If this
                    # write fails, the result remains hidden in pending state.
                    _CC_WRITE_ROLLBACK(self, marker)
                    _CC_FAULT(self, "before_visibility")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        final_name,
                        changed=True,
                    )
                    _PINNED_RESULT_ADOPT(self._result_catalog, prepared)
                    _CC_FAULT(self, "after_visibility_staged")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        final_name,
                        changed=True,
                    )
                    _CC_FAULT(self, "after_visibility_commit")
                    _CC_REVALIDATE_PUBLICATION(
                        self,
                        selector_id,
                        registered_history,
                        manifest,
                        prepared,
                        binding,
                        final_name,
                        changed=True,
                    )
                    _PINNED_RESULT_VERIFY(self._result_catalog, prepared.reference)
                    os.unlink(temporary_name, dir_fd=self._root_fd)
                    temporary_name = None
                    os.fsync(self._root_fd)
                    _CC_VALIDATE_ROOT(self)
                    if _CC_READ(self, final_name) != binding:
                        raise CohortImportConflict("cohort record binding changed")
                    os.unlink(rollback_name, dir_fd=self._root_fd)
                    rollback_name = None
                    os.fsync(self._root_fd)
                    _CC_VALIDATE_ROOT(self)
                    _PINNED_RESULT_FINISH_CANDIDATE(self._result_catalog, candidate)
                    candidate = None
                    _PINNED_RESULT_FINISH(self._result_catalog, prepared)
                    prepared = None
                    return binding
                except BaseException:
                    # Publication and its compensation share the root/process
                    # critical section. Readers and recovery can therefore never
                    # observe state which this operation may still roll back.
                    cleanup_attempted_under_lock = True
                    cleanup_import()
                    raise
                finally:
                    fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        except BaseException:
            # Preflight failures have no published binding or durable catalog
            # row. Serialize their in-memory/object cleanup for the same root.
            if not cleanup_attempted_under_lock and (
                prepared is not None
                or temporary_name is not None
                or rollback_name is not None
                or candidate is not None
                or final_published
            ):
                with _PROCESS_LOCK:
                    _CC_VALIDATE_ROOT(self)
                    fcntl.flock(self._root_fd, fcntl.LOCK_EX)
                    try:
                        cleanup_import()
                    finally:
                        fcntl.flock(self._root_fd, fcntl.LOCK_UN)
            raise

    def bindings_for_manifest(
        self, selector_id: str, cohort_version: int
    ) -> CohortManifestBindings:
        """Return bindings while a shared root lock fences recovery/publication."""

        with _CC_REGISTERED_AUTHORITY_FENCE(
            self, selector_id, cohort_version
        ) as (registered_history, linkage_snapshot, registry_state_heads):
            with self._catalog_connection_lock:
                with _PROCESS_LOCK:
                    _CC_VALIDATE_ROOT(self)
                    descriptor = os.open(
                        ".",
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=self._root_fd,
                    )
                    try:
                        if _file_identity(os.fstat(descriptor)) != _file_identity(
                            os.fstat(self._root_fd)
                        ):
                            raise CohortImportFilesystemError(
                                "cohort record root changed"
                            )
                        fcntl.flock(descriptor, fcntl.LOCK_SH)
                        return _CC_BINDINGS_BODY(
                            self,
                            selector_id,
                            cohort_version,
                            registered_history,
                            linkage_snapshot,
                            registry_state_heads,
                        )
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        os.close(descriptor)

    def _bindings_for_manifest_body(
        self,
        selector_id: str,
        cohort_version: int,
        registered_history: RegisteredCohortHistory,
        linkage_snapshot: ActiveLinkageSnapshot,
        registry_state_heads: tuple[str, ...],
    ) -> CohortManifestBindings:
        """Return only bindings still valid under live linkage and current trust."""

        manifest = registered_history.manifests[-1]
        _CC_VALIDATE_READER_REGISTRY(self)
        _CC_VALIDATE_MANIFEST(self, manifest)
        authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        digest = cohort_manifest_sha256(manifest)
        linkage_digest = _linkage_snapshot_sha256(linkage_snapshot)
        members = {
            (item.provider_namespace, item.analysis_record_id): item
            for item in manifest.members
        }
        selected = tuple(
            _CC_READ(self, name)
            for name in _CC_NAMES_UNLOCKED(self)
            if name.startswith(f"{digest}.")
        )
        for binding in selected:
            member = members.get(
                (binding.provider_namespace, binding.analysis_record_id)
            )
            if (
                member is None
                or binding.registry_id != registered_history.registry_id
                or binding.registry_epoch_sha256
                != registered_history.registry_epoch_sha256
                or binding.registry_state_version >= len(registry_state_heads)
                or registry_state_heads[binding.registry_state_version]
                != binding.registry_state_head_sha256
                or binding.selector_id != selector_id
                or binding.cohort_id != manifest.cohort_id
                or binding.cohort_version != manifest.version
                or _member_sha256(member) != binding.member_sha256
                or binding.lineage_role != member.lineage_role
                or binding.denominator_contribution != member.denominator_contribution
                or binding.measurement_anchor_sha256
                != manifest.measurement_anchor.measurement_definition_sha256
                or (
                    binding.linkage_store_id,
                    binding.linkage_store_epoch_sha256,
                    binding.linkage_storage_identity_sha256,
                    binding.linkage_state_version,
                    binding.linkage_state_head_sha256,
                    binding.linkage_snapshot_sha256,
                )
                != (
                    linkage_snapshot.store_id,
                    linkage_snapshot.store_epoch_sha256,
                    linkage_snapshot.storage_identity_sha256,
                    linkage_snapshot.state_version,
                    linkage_snapshot.state_head_sha256,
                    linkage_digest,
                )
                or (
                    binding.inclusion_policy_sha256,
                    binding.exclusion_policy_sha256,
                    binding.missingness_policy_sha256,
                    binding.record_status_policy_sha256,
                )
                != (
                    manifest.policies.inclusion_sha256,
                    manifest.policies.exclusion_sha256,
                    manifest.policies.missingness_sha256,
                    COHORT_RECORD_STATUS_POLICY_SHA256,
                )
            ):
                raise CohortImportConflict(
                    "cohort record binding conflicts with manifest"
                )
            if binding.reader_registry_sha256 != _reader_registry_sha256(
                self._reader_registry
            ):
                raise CohortImportConflict("cohort record reader registry changed")
            bound_authority = bound_catalog_authority(
                storage_identity_sha256=binding.catalog_storage_identity_sha256,
                trust_snapshot_sha256=binding.result_trust_snapshot_sha256,
                reader_registry_sha256=authority.reader_registry_sha256,
                expected_catalog_authority_sha256=binding.catalog_authority_sha256,
            )
            if (
                bound_authority is None
                or binding.catalog_storage_identity_sha256
                != authority.storage_identity_sha256
            ):
                raise CohortImportConflict("cohort record catalog authority changed")
            publication = _PINNED_RESULT_PUBLICATION(
                self._result_catalog,
                binding.publication_id,
                self._recovery_scope_sha256,
            )
            if (
                publication is None
                or publication.state != "adopted"
                or publication.reference != binding.result
            ):
                raise CohortImportConflict(
                    "cohort record publication ownership changed"
                )
            _, reader = _PINNED_RESULT_VERIFY(self._result_catalog, binding.result)
            if (
                reader.reader_id != binding.reader_id
                or reader.minimum_version != binding.reader_minimum_version
                or reader.maximum_version != binding.reader_maximum_version
            ):
                raise CohortImportConflict("cohort record reader binding changed")
        ordered = tuple(
            sorted(
                selected,
                key=lambda item: (
                    members[
                        (item.provider_namespace, item.analysis_record_id)
                    ].time_coordinate,
                    item.provider_namespace,
                    item.analysis_record_id,
                ),
            )
        )
        _CC_FAULT(self, "before_read_return")
        _CC_VALIDATE_ROOT(self)
        _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            cohort_version,
            expected=registered_history,
        )
        if _CC_VALIDATE_CATALOG_AUTHORITY(self) != authority:
            raise CohortImportConflict("cohort record catalog authority changed")
        for item in ordered:
            exact_name = f"{item.cohort_manifest_sha256}.{item.binding_id}.json"
            if _CC_READ(self, exact_name) != item:
                raise CohortImportConflict("cohort record binding changed")
            publication = _PINNED_RESULT_PUBLICATION(
                self._result_catalog,
                item.publication_id,
                self._recovery_scope_sha256,
            )
            if (
                publication is None
                or publication.state != "adopted"
                or publication.reference != item.result
            ):
                raise CohortImportConflict(
                    "cohort record publication ownership changed"
                )
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            cohort_version,
            expected=registered_history,
        )
        return CohortManifestBindings(
            registry_id=registered_history.registry_id,
            registry_epoch_sha256=registered_history.registry_epoch_sha256,
            registry_state_version=registered_history.state_version,
            registry_state_head_sha256=registered_history.state_head_sha256,
            selector_id=selector_id,
            cohort_id=manifest.cohort_id,
            cohort_version=manifest.version,
            cohort_manifest_sha256=digest,
            linkage_snapshot_sha256=linkage_digest,
            catalog_authority_sha256=catalog_authority_sha256(authority),
            inclusion_policy_sha256=manifest.policies.inclusion_sha256,
            exclusion_policy_sha256=manifest.policies.exclusion_sha256,
            missingness_policy_sha256=manifest.policies.missingness_sha256,
            record_status_policy_sha256=COHORT_RECORD_STATUS_POLICY_SHA256,
            bindings=ordered,
        )

    @contextmanager
    def record_status_authority_fence(
        self,
        selector_id: str,
        cohort_version: int,
        *,
        expected_registry: CohortRegistry | None = None,
        expected_linkage_store: ProviderLinkageStore | None = None,
    ) -> Iterator[tuple[RegisteredCohortHistory, CohortManifestRecordStatus]]:
        """Yield one status while every registry/catalog/trust lock remains held.

        This protected composition boundary lets downstream derivations finish
        without reopening private D06 internals or introducing another lock.
        Callers must not invoke mutable authority APIs while the fence is held.
        """

        if (
            expected_registry is not None
            and expected_registry is not self._cohort_registry
        ):
            raise CohortImportError("cohort registry authority does not match catalog")
        if (
            expected_linkage_store is not None
            and expected_linkage_store is not self._linkage_store
        ):
            raise CohortImportError("provider linkage authority does not match catalog")
        with _CC_REGISTERED_AUTHORITY_FENCE(
            self, selector_id, cohort_version
        ) as (registered_history, linkage_snapshot, registry_state_heads):
            with self._catalog_connection_lock:
                with _PROCESS_LOCK:
                    _CC_VALIDATE_ROOT(self)
                    descriptor = os.open(
                        ".",
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=self._root_fd,
                    )
                    try:
                        if _file_identity(os.fstat(descriptor)) != _file_identity(
                            os.fstat(self._root_fd)
                        ):
                            raise CohortImportFilesystemError(
                                "cohort record root changed"
                            )
                        fcntl.flock(descriptor, fcntl.LOCK_SH)
                        status = _CC_STATUS_BODY(
                            self,
                            selector_id,
                            cohort_version,
                            registered_history,
                            linkage_snapshot,
                            registry_state_heads,
                        )
                        yield registered_history, status
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        os.close(descriptor)

    def record_status_for_manifest(
        self, selector_id: str, cohort_version: int
    ) -> CohortManifestRecordStatus:
        """Return status while a shared root lock fences recovery/publication."""

        with CohortRecordCatalog.record_status_authority_fence(
            self, selector_id, cohort_version
        ) as (_, status):
            return status

    def _require_composite_prefix_held(self) -> None:
        """Require the D01, D05 and E04/trust fences that precede the D06 root.

        Order: linkage ``authority_read_fence``, then the cohort registry's
        ``authority_read_fence``, then this catalog's E04
        ``trust_authority_fence`` on the result-trust-registry path, all held
        by this thread.  The TrustStore path has no cross-process trust fence
        and is not composable.
        """

        state = object.__getattribute__(self._linkage_store, "__dict__")
        connection = state.get("_connection") if type(state) is dict else None
        holder = state.get("_authority_fence_thread") if type(state) is dict else None
        thread = threading.get_ident()
        if (
            type(holder) is not tuple
            or holder != (os.getpid(), thread)
            or type(connection) is not sqlite3.Connection
            or not connection.in_transaction
        ):
            raise CohortImportError("record status fence requires the held linkage fence")
        try:
            _PINNED_REGISTRY_HEAD_IN_FENCE(self._cohort_registry)
        except Exception:
            raise CohortImportError(
                "record status fence requires the held cohort registry fence"
            ) from None
        catalog = self._result_catalog
        holders = object.__getattribute__(catalog, "__dict__").get(
            "_trust_fence_holders"
        )
        if (
            self._result_trust_registry is None
            or type(holders) is not dict
            or type(holders.get(thread)) is not ResultTrustSnapshot
        ):
            raise CohortImportError(
                "record status fence requires the held result trust fence"
            )

    @contextmanager
    def record_status_read_fence(self) -> Iterator[None]:
        """Hold the D06 record root shared for a composite authority fence.

        This is the D06 step of the E12 global lock order (D01 linkage, D05
        cohort registry, E04 catalog connection, result trust, then this
        root); the caller must already hold the first four on this thread.
        Import publication, recovery and cleanup need the exclusive root lock
        and cannot land, from any process, until this context exits.  Only
        ``record_status_in_fence`` may read inside it.  Not reentrant.
        """

        _CC_ASSERT_RUNTIME(self)
        _CC_REQUIRE_COMPOSITE_PREFIX(self)
        current = (os.getpid(), threading.get_ident())
        if _STATUS_FENCE_HOLDERS.get(self) == current:
            raise CohortImportError("record status fence is already held")
        with self._catalog_connection_lock:
            with _PROCESS_LOCK:
                _CC_VALIDATE_ROOT(self)
                descriptor = os.open(
                    ".",
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_fd,
                )
                try:
                    if _file_identity(os.fstat(descriptor)) != _file_identity(
                        os.fstat(self._root_fd)
                    ):
                        raise CohortImportFilesystemError("cohort record root changed")
                    fcntl.flock(descriptor, fcntl.LOCK_SH)
                    _STATUS_FENCE_HOLDERS[self] = current
                    try:
                        yield
                    finally:
                        _STATUS_FENCE_HOLDERS.pop(self, None)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def record_status_in_fence(
        self, selector_id: str, cohort_version: int
    ) -> CohortManifestRecordStatus:
        """Return one status while ``record_status_read_fence`` is held.

        The same derivation and final registry/linkage recheck as
        ``record_status_for_manifest``, reusing the caller's held fences
        instead of taking them, so it can run for several cohort versions in
        one composite hold.
        """

        _CC_ASSERT_RUNTIME(self)
        if _STATUS_FENCE_HOLDERS.get(self) != (os.getpid(), threading.get_ident()):
            raise CohortImportError("record status fence is absent")
        _CC_REQUIRE_COMPOSITE_PREFIX(self)
        if (
            type(selector_id) is not str
            or len(selector_id) != 56
            or not selector_id.startswith("cohort_selector_")
            or any(character not in "0123456789abcdef" for character in selector_id[16:])
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise CohortImportError("cohort registry selector is invalid")
        registry = self._cohort_registry
        try:
            _PINNED_REGISTRY_REQUIRE_INTEGRITY(registry)
            history = _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                self, selector_id, cohort_version
            )
            linkage_snapshot = _capture_linkage_snapshot(
                _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store)
            )
            journal = _PINNED_REGISTRY_LOAD_JOURNAL(registry)
            registry_state_heads = (
                object.__getattribute__(registry, "_genesis_head_sha256"),
                *(item.entry_sha256 for item in journal),
            )
            if history.state_version != len(journal):
                raise CohortImportConflict("cohort registry state changed")
            status = _CC_STATUS_BODY(
                self,
                selector_id,
                cohort_version,
                history,
                linkage_snapshot,
                registry_state_heads,
            )
            final_history = _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
                self, selector_id, cohort_version, expected=history
            )
            final_snapshot = _capture_linkage_snapshot(
                _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store)
            )
            final_journal = _PINNED_REGISTRY_LOAD_JOURNAL(registry)
            final_heads = (
                object.__getattribute__(registry, "_genesis_head_sha256"),
                *(item.entry_sha256 for item in final_journal),
            )
            if (
                final_history != history
                or final_snapshot != linkage_snapshot
                or final_heads != registry_state_heads
            ):
                raise CohortImportConflict(
                    "cohort registry or linkage authority changed"
                )
            _PINNED_REGISTRY_REQUIRE_INTEGRITY(registry)
        except CohortImportError:
            raise
        except Exception:
            raise CohortImportError(
                "cohort registry selection is not current and trusted"
            ) from None
        return status

    def _record_status_for_manifest_body(
        self,
        selector_id: str,
        cohort_version: int,
        registered_history: RegisteredCohortHistory,
        linkage_snapshot: ActiveLinkageSnapshot,
        registry_state_heads: tuple[str, ...],
    ) -> CohortManifestRecordStatus:
        """Return complete canonical availability without exposing withheld results."""

        manifest = registered_history.manifests[-1]
        _CC_VALIDATE_READER_REGISTRY(self)
        _CC_VALIDATE_MANIFEST(self, manifest)
        authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        digest = cohort_manifest_sha256(manifest)
        linkage_digest = _linkage_snapshot_sha256(linkage_snapshot)
        members = {
            (item.provider_namespace, item.analysis_record_id): item
            for item in manifest.members
        }
        selected = tuple(
            _CC_READ(self, name)
            for name in _CC_NAMES_UNLOCKED(self)
            if name.startswith(f"{digest}.")
        )
        indexed: dict[tuple[str, str], CohortRecordBinding] = {}
        for binding in selected:
            key = (binding.provider_namespace, binding.analysis_record_id)
            member = members.get(key)
            if (
                member is None
                or key in indexed
                or binding.registry_id != registered_history.registry_id
                or binding.registry_epoch_sha256
                != registered_history.registry_epoch_sha256
                or binding.registry_state_version >= len(registry_state_heads)
                or registry_state_heads[binding.registry_state_version]
                != binding.registry_state_head_sha256
                or binding.selector_id != selector_id
                or binding.cohort_id != manifest.cohort_id
                or binding.cohort_version != manifest.version
                or binding.cohort_manifest_sha256 != digest
                or _member_sha256(member) != binding.member_sha256
                or binding.lineage_role != member.lineage_role
                or binding.denominator_contribution != member.denominator_contribution
                or binding.measurement_anchor_sha256
                != manifest.measurement_anchor.measurement_definition_sha256
                or (
                    binding.linkage_store_id,
                    binding.linkage_store_epoch_sha256,
                    binding.linkage_storage_identity_sha256,
                    binding.linkage_state_version,
                    binding.linkage_state_head_sha256,
                    binding.linkage_snapshot_sha256,
                )
                != (
                    linkage_snapshot.store_id,
                    linkage_snapshot.store_epoch_sha256,
                    linkage_snapshot.storage_identity_sha256,
                    linkage_snapshot.state_version,
                    linkage_snapshot.state_head_sha256,
                    linkage_digest,
                )
                or (
                    binding.inclusion_policy_sha256,
                    binding.exclusion_policy_sha256,
                    binding.missingness_policy_sha256,
                    binding.record_status_policy_sha256,
                )
                != (
                    manifest.policies.inclusion_sha256,
                    manifest.policies.exclusion_sha256,
                    manifest.policies.missingness_sha256,
                    COHORT_RECORD_STATUS_POLICY_SHA256,
                )
            ):
                raise CohortImportConflict(
                    "cohort record binding conflicts with manifest"
                )
            indexed[key] = binding

        statuses: list[CohortMemberRecordStatus] = []
        for member in manifest.members:
            binding = indexed.get(
                (member.provider_namespace, member.analysis_record_id)
            )
            if binding is None:
                statuses.append(
                    CohortMemberRecordStatus(
                        provider_namespace=member.provider_namespace,
                        analysis_record_id=member.analysis_record_id,
                        member_sha256=_member_sha256(member),
                        availability=CohortRecordAvailability.MISSING,
                    )
                )
                continue
            bound_authority = bound_catalog_authority(
                storage_identity_sha256=binding.catalog_storage_identity_sha256,
                trust_snapshot_sha256=binding.result_trust_snapshot_sha256,
                reader_registry_sha256=authority.reader_registry_sha256,
                expected_catalog_authority_sha256=binding.catalog_authority_sha256,
            )
            if (
                bound_authority is None
                or binding.catalog_storage_identity_sha256
                != authority.storage_identity_sha256
                or binding.reader_registry_sha256
                != _reader_registry_sha256(self._reader_registry)
            ):
                raise CohortImportConflict("cohort record catalog authority changed")
            publication = _PINNED_RESULT_PUBLICATION(
                self._result_catalog,
                binding.publication_id,
                self._recovery_scope_sha256,
            )
            if (
                publication is None
                or publication.state != "adopted"
                or publication.reference != binding.result
            ):
                raise CohortImportConflict(
                    "cohort record publication ownership changed"
                )
            try:
                _, reader = _PINNED_RESULT_VERIFY(self._result_catalog, binding.result)
            except RevokedKeyError:
                statuses.append(
                    CohortMemberRecordStatus(
                        provider_namespace=member.provider_namespace,
                        analysis_record_id=member.analysis_record_id,
                        member_sha256=binding.member_sha256,
                        availability=CohortRecordAvailability.WITHHELD,
                        withheld_reason=(CohortRecordWithheldReason.RESULT_KEY_REVOKED),
                    )
                )
                continue
            if (
                reader.reader_id != binding.reader_id
                or reader.minimum_version != binding.reader_minimum_version
                or reader.maximum_version != binding.reader_maximum_version
            ):
                raise CohortImportConflict("cohort record reader binding changed")
            statuses.append(
                CohortMemberRecordStatus(
                    provider_namespace=member.provider_namespace,
                    analysis_record_id=member.analysis_record_id,
                    member_sha256=binding.member_sha256,
                    availability=CohortRecordAvailability.AVAILABLE,
                    binding=binding,
                )
            )

        _CC_FAULT(self, "before_status_return")
        _CC_VALIDATE_ROOT(self)
        _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            cohort_version,
            expected=registered_history,
        )
        final_authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        if final_authority != authority:
            raise CohortImportConflict("cohort record catalog authority changed")
        _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            cohort_version,
            expected=registered_history,
        )
        for binding in selected:
            exact_name = f"{binding.cohort_manifest_sha256}.{binding.binding_id}.json"
            if _CC_READ(self, exact_name) != binding:
                raise CohortImportConflict("cohort record binding changed")
            publication = _PINNED_RESULT_PUBLICATION(
                self._result_catalog,
                binding.publication_id,
                self._recovery_scope_sha256,
            )
            if (
                publication is None
                or publication.state != "adopted"
                or publication.reference != binding.result
            ):
                raise CohortImportConflict(
                    "cohort record publication ownership changed"
                )
        _CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE(
            self,
            selector_id,
            cohort_version,
            expected=registered_history,
        )
        payload = {
            "schema_version": "traceback.cohort-manifest-record-status.v2",
            "registry_id": registered_history.registry_id,
            "registry_epoch_sha256": registered_history.registry_epoch_sha256,
            "registry_state_version": registered_history.state_version,
            "registry_state_head_sha256": (
                registered_history.state_head_sha256
            ),
            "selector_id": selector_id,
            "cohort_id": manifest.cohort_id,
            "cohort_version": manifest.version,
            "cohort_manifest_sha256": digest,
            "linkage_store_id": linkage_snapshot.store_id,
            "linkage_store_epoch_sha256": linkage_snapshot.store_epoch_sha256,
            "linkage_storage_identity_sha256": (
                linkage_snapshot.storage_identity_sha256
            ),
            "linkage_state_version": linkage_snapshot.state_version,
            "linkage_state_head_sha256": linkage_snapshot.state_head_sha256,
            "linkage_snapshot_sha256": linkage_digest,
            "catalog_authority_sha256": catalog_authority_sha256(final_authority),
            "inclusion_policy_sha256": manifest.policies.inclusion_sha256,
            "exclusion_policy_sha256": manifest.policies.exclusion_sha256,
            "missingness_policy_sha256": manifest.policies.missingness_sha256,
            "record_status_policy_sha256": COHORT_RECORD_STATUS_POLICY_SHA256,
            "members": tuple(statuses),
        }
        digest_payload = {
            **payload,
            "members": tuple(item.model_dump(mode="json") for item in statuses),
        }
        return CohortManifestRecordStatus(
            **payload,
            status_sha256=hashlib.sha256(
                canonical_json_bytes(digest_payload)
            ).hexdigest(),
        )

def _cohort_instance_snapshot(catalog: CohortRecordCatalog) -> tuple[object, ...]:
    state = object.__getattribute__(catalog, "__dict__")
    pins = state.get("_expected_trust_snapshot_sha256_by_provider")
    return (
        id(state.get("_result_catalog")),
        state.get("_result_catalog_identity"),
        id(state.get("_result_trust_store")),
        id(state.get("_result_trust_registry")),
        id(state.get("_result_trust_lock")),
        state.get("_result_trust_lock_identity"),
        id(state.get("_result_reader_registry")),
        state.get("_catalog_storage_identity_sha256"),
        state.get("_catalog_reader_identity_sha256"),
        id(state.get("_catalog_connection_lock")),
        state.get("_catalog_connection_lock_identity"),
        id(state.get("_linkage_store")),
        state.get("_linkage_store_identity"),
        id(state.get("_cohort_registry")),
        state.get("_cohort_registry_identity"),
        state.get("_cohort_registry_id"),
        state.get("_cohort_registry_epoch_sha256"),
        tuple(sorted(pins.items())) if type(pins) is MappingProxyType else None,
        state.get("_reader_registry_bytes"),
        state.get("_root_identity"),
        state.get("_recovery_scope_sha256"),
        id(state.get("_fault_controller")),
        state.get("_fault_controller_identity"),
        state.get("_fault_controller_configuration"),
    )


def _seal_cohort_instance(catalog: CohortRecordCatalog) -> None:
    _COHORT_INSTANCE_SEALS[catalog] = _cohort_instance_snapshot(catalog)


_COHORT_METHOD_SEAL = MappingProxyType(
    {
        name: CohortRecordCatalog.__dict__[name]
        for name in (
            "__init__",
            "__setattr__",
            "_binding_equivalent",
            "_bindings_for_manifest_body",
            "_fault",
            "_names_unlocked",
            "_read",
            "_read_pending",
            "_read_rollback_marker",
            "_recover_pending",
            "_record_status_for_manifest_body",
            "_registered_authority_fence",
            "_resolve_registered_history_in_fence",
            "_revalidate_publication",
            "_validate_catalog_authority",
            "_validate_manifest",
            "_validate_reader_registry",
            "_validate_root",
            "_write_pending",
            "_write_rollback_marker",
            "_import_bundle_in_fence",
            "bindings_for_manifest",
            "close",
            "import_bundle",
            "record_status_for_manifest",
            "record_status_authority_fence",
            "_require_composite_prefix_held",
            "record_status_read_fence",
            "record_status_in_fence",
        )
    }
)
_COHORT_METHOD_FINGERPRINTS = MappingProxyType(
    {
        name: _authority_value_fingerprint(value)
        for name, value in _COHORT_METHOD_SEAL.items()
    }
)
_COHORT_PINNED_FINGERPRINTS = MappingProxyType(
    {
        "manifest_validator": _authority_value_fingerprint(
            _PINNED_VALIDATE_MANIFEST_IN_FENCE
        ),
        "result_assert": _authority_value_fingerprint(_PINNED_RESULT_RUNTIME_ASSERT),
        "active_snapshot": _authority_value_fingerprint(
            _PINNED_MANIFEST_ACTIVE_SNAPSHOT
        ),
        "linkage_fence": _authority_value_fingerprint(
            _PINNED_LINKAGE_AUTHORITY_FENCE
        ),
        "registry_integrity": _authority_value_fingerprint(
            _PINNED_REGISTRY_REQUIRE_INTEGRITY
        ),
        "registry_lock": _authority_value_fingerprint(_PINNED_REGISTRY_LOCK),
        "registry_resolve_history": _authority_value_fingerprint(
            _PINNED_REGISTRY_RESOLVE_IN_FENCE
        ),
        "registry_journal": _authority_value_fingerprint(
            _PINNED_REGISTRY_LOAD_JOURNAL
        ),
        "registry_list": _authority_value_fingerprint(_PINNED_REGISTRY_LIST),
        "exact_model_bytes": _authority_value_fingerprint(_PINNED_EXACT_MODEL_BYTES),
        "register_candidate": _authority_value_fingerprint(
            _PINNED_RESULT_REGISTER_CANDIDATE
        ),
        "candidates": _authority_value_fingerprint(_PINNED_RESULT_CANDIDATES),
        "finish_candidate": _authority_value_fingerprint(
            _PINNED_RESULT_FINISH_CANDIDATE
        ),
        "result_trust_fence": _authority_value_fingerprint(
            _PINNED_RESULT_TRUST_FENCE
        ),
        **{
            f"store:{name}": _authority_value_fingerprint(value)
            for name, value in _PINNED_MANIFEST_STORE_CALLABLES.items()
        },
    }
)


def _assert_cohort_runtime(
    catalog: CohortRecordCatalog,
    *,
    expected_methods: Mapping[str, object] = _COHORT_METHOD_SEAL,
    expected_manifest_validator: object = _PINNED_VALIDATE_MANIFEST_IN_FENCE,
    expected_result_assert: object = _PINNED_RESULT_RUNTIME_ASSERT,
) -> None:
    if type(catalog) is not CohortRecordCatalog:
        raise CohortImportError("cohort authority type changed")
    expected_state = _COHORT_INSTANCE_SEALS.get(catalog)
    if (
        expected_state is None
        or _cohort_instance_snapshot(catalog) != expected_state
    ):
        raise CohortImportError("cohort authority state changed")
    for name, expected in expected_methods.items():
        current = CohortRecordCatalog.__dict__.get(name)
        if (
            name in vars(catalog)
            or current is not expected
            or _authority_value_fingerprint(current)
            != _COHORT_METHOD_FINGERPRINTS[name]
        ):
            raise CohortImportError("cohort authority callable changed")
    for name, expected in _COHORT_ALIAS_SEAL.items():
        current = globals().get(name)
        if (
            current is not expected
            or _authority_value_fingerprint(current) != _COHORT_ALIAS_FINGERPRINTS[name]
        ):
            raise CohortImportError("cohort module authority changed")
    if (
        globals().get("_PINNED_VALIDATE_MANIFEST_IN_FENCE")
        is not expected_manifest_validator
        or globals().get("_PINNED_RESULT_RUNTIME_ASSERT") is not expected_result_assert
        or _authority_value_fingerprint(_PINNED_VALIDATE_MANIFEST_IN_FENCE)
        != _COHORT_PINNED_FINGERPRINTS["manifest_validator"]
        or _authority_value_fingerprint(_PINNED_RESULT_RUNTIME_ASSERT)
        != _COHORT_PINNED_FINGERPRINTS["result_assert"]
        or _authority_value_fingerprint(_PINNED_MANIFEST_ACTIVE_SNAPSHOT)
        != _COHORT_PINNED_FINGERPRINTS["active_snapshot"]
        or _authority_value_fingerprint(_PINNED_LINKAGE_AUTHORITY_FENCE)
        != _COHORT_PINNED_FINGERPRINTS["linkage_fence"]
        or _authority_value_fingerprint(_PINNED_REGISTRY_REQUIRE_INTEGRITY)
        != _COHORT_PINNED_FINGERPRINTS["registry_integrity"]
        or _authority_value_fingerprint(_PINNED_REGISTRY_LOCK)
        != _COHORT_PINNED_FINGERPRINTS["registry_lock"]
        or _authority_value_fingerprint(_PINNED_REGISTRY_RESOLVE_IN_FENCE)
        != _COHORT_PINNED_FINGERPRINTS["registry_resolve_history"]
        or _authority_value_fingerprint(_PINNED_REGISTRY_LOAD_JOURNAL)
        != _COHORT_PINNED_FINGERPRINTS["registry_journal"]
        or _authority_value_fingerprint(_PINNED_REGISTRY_LIST)
        != _COHORT_PINNED_FINGERPRINTS["registry_list"]
        or _authority_value_fingerprint(_PINNED_EXACT_MODEL_BYTES)
        != _COHORT_PINNED_FINGERPRINTS["exact_model_bytes"]
        or _authority_value_fingerprint(_PINNED_RESULT_REGISTER_CANDIDATE)
        != _COHORT_PINNED_FINGERPRINTS["register_candidate"]
        or _authority_value_fingerprint(_PINNED_RESULT_CANDIDATES)
        != _COHORT_PINNED_FINGERPRINTS["candidates"]
        or _authority_value_fingerprint(_PINNED_RESULT_FINISH_CANDIDATE)
        != _COHORT_PINNED_FINGERPRINTS["finish_candidate"]
        or _authority_value_fingerprint(_PINNED_RESULT_TRUST_FENCE)
        != _COHORT_PINNED_FINGERPRINTS["result_trust_fence"]
        or any(
            _authority_value_fingerprint(value)
            != _COHORT_PINNED_FINGERPRINTS[f"store:{name}"]
            for name, value in _PINNED_MANIFEST_STORE_CALLABLES.items()
        )
    ):
        raise CohortImportError("cohort module authority changed")


_CC_ASSERT_RUNTIME = _assert_cohort_runtime
_CC_BINDING_EQUIVALENT = CohortRecordCatalog._binding_equivalent
_CC_BINDINGS_BODY = CohortRecordCatalog._bindings_for_manifest_body
_CC_CAPTURE_IMPORT_AUTHORITY = _capture_import_authority_contract
_CC_FAULT = CohortRecordCatalog._fault
_CC_IMPORT_BODY = CohortRecordCatalog._import_bundle_in_fence
_CC_NAMES_UNLOCKED = CohortRecordCatalog._names_unlocked
_CC_READ = CohortRecordCatalog._read
_CC_READ_PENDING = CohortRecordCatalog._read_pending
_CC_READ_ROLLBACK = CohortRecordCatalog._read_rollback_marker
_CC_RECOVER_PENDING = CohortRecordCatalog._recover_pending
_CC_REGISTERED_AUTHORITY_FENCE = CohortRecordCatalog._registered_authority_fence
_CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE = CohortRecordCatalog._resolve_registered_history_in_fence
_CC_STATUS_BODY = CohortRecordCatalog._record_status_for_manifest_body
_CC_REVALIDATE_PUBLICATION = CohortRecordCatalog._revalidate_publication
_CC_VALIDATE_CATALOG_AUTHORITY = CohortRecordCatalog._validate_catalog_authority
_CC_VALIDATE_MANIFEST = CohortRecordCatalog._validate_manifest
_CC_VALIDATE_READER_REGISTRY = CohortRecordCatalog._validate_reader_registry
_CC_VALIDATE_ROOT = CohortRecordCatalog._validate_root
_CC_WRITE_PENDING = CohortRecordCatalog._write_pending
_CC_WRITE_ROLLBACK = CohortRecordCatalog._write_rollback_marker
_CC_REQUIRE_COMPOSITE_PREFIX = CohortRecordCatalog._require_composite_prefix_held
_COHORT_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_CC_BINDING_EQUIVALENT",
            "_CC_BINDINGS_BODY",
            "_CC_CAPTURE_IMPORT_AUTHORITY",
            "_CC_FAULT",
            "_CC_IMPORT_BODY",
            "_CC_NAMES_UNLOCKED",
            "_CC_READ",
            "_CC_READ_PENDING",
            "_CC_READ_ROLLBACK",
            "_CC_RECOVER_PENDING",
            "_CC_REGISTERED_AUTHORITY_FENCE",
            "_CC_RESOLVE_REGISTERED_HISTORY_IN_FENCE",
            "_CC_STATUS_BODY",
            "_CC_REVALIDATE_PUBLICATION",
            "_CC_VALIDATE_CATALOG_AUTHORITY",
            "_CC_VALIDATE_MANIFEST",
            "_CC_VALIDATE_READER_REGISTRY",
            "_CC_VALIDATE_ROOT",
            "_CC_WRITE_PENDING",
            "_CC_WRITE_ROLLBACK",
            "_CC_REQUIRE_COMPOSITE_PREFIX",
            "_PINNED_RESULT_REGISTER_CANDIDATE",
            "_PINNED_RESULT_CANDIDATES",
            "_PINNED_RESULT_FINISH_CANDIDATE",
            "_PINNED_LINKAGE_AUTHORITY_FENCE",
            "_PINNED_REGISTRY_REQUIRE_INTEGRITY",
            "_PINNED_REGISTRY_LOCK",
            "_PINNED_REGISTRY_RESOLVE_IN_FENCE",
            "_PINNED_REGISTRY_LOAD_JOURNAL",
            "_PINNED_REGISTRY_HEAD_IN_FENCE",
            "_PINNED_REGISTRY_LIST",
            "_PINNED_EXACT_MODEL_BYTES",
        )
    }
)
_COHORT_ALIAS_FINGERPRINTS = MappingProxyType(
    {
        name: _authority_value_fingerprint(value)
        for name, value in _COHORT_ALIAS_SEAL.items()
    }
)


__all__ = [
    "MAX_BINDINGS",
    "CohortImportConflict",
    "CohortImportError",
    "CohortImportFilesystemError",
    "CohortManifestBindings",
    "CohortManifestRecordStatus",
    "CohortMemberRecordStatus",
    "CohortRecordAvailability",
    "CohortRecordBinding",
    "CohortRecordCatalog",
    "CohortRecordWithheldReason",
]
