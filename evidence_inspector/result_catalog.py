"""Immutable local catalog and deterministic selector read model.

Bundle paths, private identifiers, and alias mappings never enter result refs,
query pages, exports, or error text. Imports copy only the fixed bundle schema
through descriptor-relative, no-follow opens before invoking the merged verifier.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from evidence_inspector.fault_controller import (
    NO_FAULTS,
    DeterministicFaultController,
    fault_controller_snapshot,
)
from evidence_inspector.method_registry import (
    AuthorityHead,
    AuthorityScope,
    CurrentMethodCapability,
    DisplayRole,
    MethodReference,
    MethodRegistry,
    QualificationState,
    RevocationTarget,
    replay_current_capability,
)
from evidence_inspector.result_trust_registry import (
    ResultTrustRegistry,
    ResultTrustRegistryError,
    ResultTrustSnapshot,
)
from traceback_runner.bundles import VerifiedBundle, verify_bundle
from traceback_runner.contracts import ResultBundleManifestV2
from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import (
    TrustStore,
    development_trust_document_bytes,
    load_development_trust,
)

_PINNED_VERIFY_BUNDLE = verify_bundle
_PINNED_TRUST_RESOLVE = TrustStore.resolve
_PINNED_TRUST_READ_FENCE = ResultTrustRegistry.read_fence
_PINNED_LOAD_TRUST = load_development_trust
_PINNED_TRUST_DOCUMENT_BYTES = development_trust_document_bytes
_PINNED_TRUST_READ_FENCE_SEAL = _PINNED_TRUST_READ_FENCE
_PINNED_LOAD_TRUST_SEAL = _PINNED_LOAD_TRUST
_PINNED_TRUST_DOCUMENT_BYTES_SEAL = _PINNED_TRUST_DOCUMENT_BYTES
_PINNED_FAULT_SNAPSHOT = fault_controller_snapshot
_PINNED_FAULT_HIT = DeterministicFaultController.hit

CATALOG_SCHEMA_VERSION = 3
MAX_IMPORT_ROOTS = 8
MAX_IMPORT_DEPTH = 4
MAX_QUERY_LIMIT = 100
MAX_FILTER_VALUES = 32

_SQLITE_OPEN_LOCK = threading.RLock()


def _normalize_schema_sql(statement: str) -> str:
    return "".join(statement.split()).casefold()


_CATALOG_SCHEMA_SQL_V1 = {
    ("table", "metadata"): (
        "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    ),
    ("table", "results"): """CREATE TABLE results(
        result_id TEXT PRIMARY KEY,
        bundle_sha256 TEXT NOT NULL UNIQUE,
        bundle_record_id TEXT NOT NULL UNIQUE,
        method_id TEXT NOT NULL,
        method_version TEXT NOT NULL,
        execution_state TEXT NOT NULL,
        information_state TEXT NOT NULL,
        trust_state TEXT NOT NULL,
        qualification_state TEXT NOT NULL,
        ref_json BLOB NOT NULL
    )""",
    ("table", "opaque_aliases"): """CREATE TABLE opaque_aliases(
        result_id TEXT PRIMARY KEY REFERENCES results(result_id),
        display_alias TEXT NOT NULL UNIQUE,
        run_alias TEXT NOT NULL,
        timepoint_alias TEXT NOT NULL
    )""",
    ("index", "results_method"): (
        "CREATE INDEX results_method ON results(method_id, method_version, result_id)"
    ),
    ("index", "results_states"): """CREATE INDEX results_states ON results(
        execution_state,
        information_state,
        trust_state,
        qualification_state,
        result_id
    )""",
    ("index", "aliases_run"): (
        "CREATE INDEX aliases_run ON opaque_aliases(run_alias, result_id)"
    ),
    ("index", "aliases_timepoint"): (
        "CREATE INDEX aliases_timepoint ON opaque_aliases(timepoint_alias, result_id)"
    ),
}

_CATALOG_SCHEMA_SQL = {
    **_CATALOG_SCHEMA_SQL_V1,
    ("table", "result_publications"): """CREATE TABLE result_publications(
        publication_id TEXT PRIMARY KEY,
        result_id TEXT NOT NULL REFERENCES results(result_id) ON DELETE CASCADE,
        recovery_scope_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','adopted')),
        UNIQUE(result_id, recovery_scope_sha256)
    )""",
    ("table", "coordinated_results"): """CREATE TABLE coordinated_results(
        result_id TEXT PRIMARY KEY REFERENCES results(result_id) ON DELETE CASCADE
    )""",
    ("index", "result_publications_state"): (
        "CREATE INDEX result_publications_state ON result_publications(state, result_id)"
    ),
    ("table", "coordinated_candidates"): """CREATE TABLE coordinated_candidates(
        operation_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL,
        result_id TEXT NOT NULL,
        recovery_scope_sha256 TEXT NOT NULL,
        candidate_json BLOB NOT NULL,
        FOREIGN KEY(publication_id) REFERENCES result_publications(publication_id)
            ON DELETE CASCADE
    )""",
    ("index", "coordinated_candidates_scope"): (
        "CREATE INDEX coordinated_candidates_scope ON coordinated_candidates("
        "recovery_scope_sha256, operation_id)"
    ),
}

_CATALOG_SCHEMA_SQL_V2 = {
    key: value
    for key, value in _CATALOG_SCHEMA_SQL.items()
    if key
    not in {
        ("table", "coordinated_candidates"),
        ("index", "coordinated_candidates_scope"),
    }
}

_CATALOG_SCHEMA_SIGNATURE_V1 = {
    key: _normalize_schema_sql(statement)
    for key, statement in _CATALOG_SCHEMA_SQL_V1.items()
}

_CATALOG_SCHEMA_SIGNATURE_V2 = {
    key: _normalize_schema_sql(statement)
    for key, statement in _CATALOG_SCHEMA_SQL_V2.items()
}

_CATALOG_SCHEMA_SIGNATURE = {
    key: _normalize_schema_sql(statement)
    for key, statement in _CATALOG_SCHEMA_SQL.items()
}

_FIXED_FILES = (
    "bundle-manifest.json",
    "bundle.sig",
    "charts/fragment-length.v1.json",
    "checksums.sha256",
    "limitations.json",
    "measurements/fragment-length.v1.json",
    "provenance.json",
    "report.html",
)
_TOP_LEVEL = frozenset(
    {
        "bundle-manifest.json",
        "bundle.sig",
        "charts",
        "checksums.sha256",
        "limitations.json",
        "measurements",
        "provenance.json",
        "report.html",
    }
)
_NESTED = {
    "charts": frozenset({"fragment-length.v1.json"}),
    "measurements": frozenset({"fragment-length.v1.json"}),
}
_MAX_FILE_BYTES = {
    "bundle-manifest.json": 256 * 1024,
    "bundle.sig": 16 * 1024,
    "charts/fragment-length.v1.json": 16 * 1024 * 1024,
    "checksums.sha256": 64 * 1024,
    "limitations.json": 64 * 1024,
    "measurements/fragment-length.v1.json": 16 * 1024 * 1024,
    "provenance.json": 1024 * 1024,
    "report.html": 2 * 1024 * 1024,
}
_MAX_TOTAL_BYTES = 36 * 1024 * 1024

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ResultId = Annotated[str, StringConstraints(pattern=r"^result_[0-9a-f]{40}$")]
RootId = Annotated[str, StringConstraints(pattern=r"^root_[a-z0-9]+(?:_[a-z0-9]+)*$")]
PublicationId = Annotated[
    str, StringConstraints(pattern=r"^publication_[0-9a-f]{16}_[0-9a-f]{64}$")
]
DisplayAlias = Annotated[str, StringConstraints(pattern=r"^dsp_[a-z0-9]{8,32}$")]
RunAlias = Annotated[str, StringConstraints(pattern=r"^rnx_[a-z0-9]{8,32}$")]
TimepointAlias = Annotated[str, StringConstraints(pattern=r"^tpt_[a-z0-9]{8,32}$")]

_ROOT_ID = TypeAdapter(RootId)


class CatalogModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class CatalogError(RuntimeError):
    """Sanitized catalog failure safe for local status surfaces."""


class CatalogFilesystemError(CatalogError):
    pass


class CatalogConflict(CatalogError):
    pass


class CatalogUnsupportedSchema(CatalogError):
    pass


class ResultBundleReader(CatalogModel):
    """One explicit, bounded reader range for independently verified bundles."""

    reader_id: str = Field(pattern=r"^reader_[a-z0-9]+(?:_[a-z0-9]+)*$")
    bundle_family: Literal["traceback.result-bundle"] = "traceback.result-bundle"
    minimum_version: int = Field(ge=1, le=1_000)
    maximum_version: int = Field(ge=1, le=1_000)
    measurement_schema_versions: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def coherent_range(self) -> ResultBundleReader:
        if self.minimum_version > self.maximum_version:
            raise ValueError("reader version range is inverted")
        if self.measurement_schema_versions != tuple(
            sorted(set(self.measurement_schema_versions))
        ):
            raise ValueError("reader measurement schemas must be unique and sorted")
        return self


class ResultBundleReaderRegistry(CatalogModel):
    """Closed reader registry; unsupported schema versions fail before indexing."""

    schema_version: Literal["traceback.result-bundle-reader-registry.v1"] = (
        "traceback.result-bundle-reader-registry.v1"
    )
    readers: tuple[ResultBundleReader, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def ranges_are_non_overlapping(self) -> ResultBundleReaderRegistry:
        order = [
            (
                item.bundle_family,
                item.minimum_version,
                item.maximum_version,
                item.reader_id,
            )
            for item in self.readers
        ]
        if order != sorted(order) or len(
            {item.reader_id for item in self.readers}
        ) != len(self.readers):
            raise ValueError("reader registry must be uniquely sorted")
        previous: ResultBundleReader | None = None
        for reader in self.readers:
            if (
                previous is not None
                and previous.bundle_family == reader.bundle_family
                and reader.minimum_version <= previous.maximum_version
            ):
                raise ValueError("reader version ranges cannot overlap")
            previous = reader
        return self

    def select(self, verified: VerifiedBundle) -> ResultBundleReader:
        manifest = verified.manifest
        schema = manifest.schema_version
        prefix = "traceback.result-bundle.v"
        if not schema.startswith(prefix) or not schema[len(prefix) :].isdigit():
            raise CatalogUnsupportedSchema("bundle schema is unsupported")
        version = int(schema[len(prefix) :])
        candidates = tuple(
            reader
            for reader in self.readers
            if reader.bundle_family == "traceback.result-bundle"
            and reader.minimum_version <= version <= reader.maximum_version
        )
        if len(candidates) != 1:
            raise CatalogUnsupportedSchema("bundle schema is unsupported")
        reader = candidates[0]
        if (
            tuple(manifest.measurement_schema_versions)
            != reader.measurement_schema_versions
        ):
            raise CatalogUnsupportedSchema("measurement schema is unsupported")
        return reader


DEFAULT_RESULT_BUNDLE_READER_REGISTRY = ResultBundleReaderRegistry(
    readers=(
        ResultBundleReader(
            reader_id="reader_result_bundle_v2",
            minimum_version=2,
            maximum_version=2,
            measurement_schema_versions=("traceback.fragment-measurement.v1",),
        ),
    )
)

_PINNED_READER_SELECT = ResultBundleReaderRegistry.select


CATALOG_AUTHORITY_SCHEMA_V1 = "traceback.catalog-authority.v1"
CATALOG_AUTHORITY_SCHEMA_V2 = "traceback.catalog-authority.v2"
# Every catalog authority version a retained digest may name, oldest first.
CATALOG_AUTHORITY_SCHEMA_VERSIONS = (
    CATALOG_AUTHORITY_SCHEMA_V1,
    CATALOG_AUTHORITY_SCHEMA_V2,
)


class CatalogAuthoritySnapshot(CatalogModel):
    """Opaque identity of the exact catalog storage and verification authority.

    ``v1``: ``trust_snapshot_sha256`` hashes the keys of a caller-held
    ``TrustStore``. ``v2``: it binds a protected ``ResultTrustRegistry`` by
    registry ID, epoch, state version, head, and current document digest, so
    every trust event changes the catalog authority digest.
    """

    schema_version: Literal[
        "traceback.catalog-authority.v1", "traceback.catalog-authority.v2"
    ] = CATALOG_AUTHORITY_SCHEMA_V1
    storage_identity_sha256: Sha256
    trust_snapshot_sha256: Sha256
    reader_registry_sha256: Sha256


def catalog_authority_sha256(snapshot: CatalogAuthoritySnapshot) -> str:
    return hashlib.sha256(
        b"traceback-catalog-authority-v1\0" + canonical_json_bytes(snapshot)
    ).hexdigest()


CATALOG_CONTENT_SCHEMA_V1 = "traceback.catalog-content.v1"
CATALOG_CONTENT_LOCK_NAME = "catalog-content.lock"


class CatalogContentSnapshot(CatalogModel):
    """Digest of every committed catalog row, read under the content lock.

    ``content_sha256`` covers the exact rows of every catalog table (results,
    aliases, publications, coordinated rows and candidates) and the schema
    metadata, in primary-key order.  Any committed import, staging,
    adoption, compensation, discard, candidate change or recovery changes it.
    """

    schema_version: Literal["traceback.catalog-content.v1"] = CATALOG_CONTENT_SCHEMA_V1
    content_sha256: Sha256


def catalog_dependency_head_sha256(
    authority: CatalogAuthoritySnapshot, content: CatalogContentSnapshot
) -> str:
    """E04 dependency head for saved-head schema v2: authority plus content.

    Saved-head schema v1 used ``catalog_authority_sha256`` alone, which does
    not change when catalog rows change.
    """

    if (
        type(authority) is not CatalogAuthoritySnapshot
        or type(content) is not CatalogContentSnapshot
    ):
        raise CatalogError("catalog dependency head inputs are invalid")
    return hashlib.sha256(
        b"traceback-e04-dependency-head-v2\0"
        + canonical_json_bytes(
            {
                "catalog_authority_sha256": catalog_authority_sha256(authority),
                "catalog_content_sha256": content.content_sha256,
            }
        )
    ).hexdigest()


# Every catalog table in content-digest order, with its exact columns and key.
_CONTENT_TABLES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("metadata", ("key", "value"), "key"),
    (
        "results",
        (
            "result_id",
            "bundle_sha256",
            "bundle_record_id",
            "method_id",
            "method_version",
            "execution_state",
            "information_state",
            "trust_state",
            "qualification_state",
            "ref_json",
        ),
        "result_id",
    ),
    (
        "opaque_aliases",
        ("result_id", "display_alias", "run_alias", "timepoint_alias"),
        "result_id",
    ),
    (
        "result_publications",
        ("publication_id", "result_id", "recovery_scope_sha256", "state"),
        "publication_id",
    ),
    ("coordinated_results", ("result_id",), "result_id"),
    (
        "coordinated_candidates",
        (
            "operation_id",
            "publication_id",
            "result_id",
            "recovery_scope_sha256",
            "candidate_json",
        ),
        "operation_id",
    ),
)


def _content_value_bytes(value: object) -> bytes:
    if value is None:
        return b"n"
    if type(value) is int:
        encoded = str(value).encode("ascii")
        return b"i" + len(encoded).to_bytes(8, "big") + encoded
    if type(value) is str:
        encoded = value.encode("utf-8")
        return b"s" + len(encoded).to_bytes(8, "big") + encoded
    if type(value) is bytes:
        return b"b" + len(value).to_bytes(8, "big") + value
    raise CatalogUnsupportedSchema("catalog content value is unsupported")


def registry_trust_snapshot_sha256(snapshot: ResultTrustSnapshot) -> str:
    """Bind one exact result-trust registry head for a v2 catalog authority."""

    return hashlib.sha256(
        b"traceback-catalog-result-trust-registry-v1\0"
        + canonical_json_bytes(
            {
                "registry_id": snapshot.registry_id,
                "registry_epoch_sha256": snapshot.registry_epoch_sha256,
                "state_version": snapshot.state_version,
                "state_head_sha256": snapshot.state_head_sha256,
                "document_sha256": snapshot.document_sha256,
            }
        )
    ).hexdigest()


def bound_catalog_authority(
    *,
    storage_identity_sha256: str,
    trust_snapshot_sha256: str,
    reader_registry_sha256: str,
    expected_catalog_authority_sha256: str,
) -> CatalogAuthoritySnapshot | None:
    """Rebuild the retained authority whose digest is exactly the one given.

    The schema version is inside the digest, so at most one version matches.
    """

    for version in CATALOG_AUTHORITY_SCHEMA_VERSIONS:
        candidate = CatalogAuthoritySnapshot(
            schema_version=version,
            storage_identity_sha256=storage_identity_sha256,
            trust_snapshot_sha256=trust_snapshot_sha256,
            reader_registry_sha256=reader_registry_sha256,
        )
        if catalog_authority_sha256(candidate) == expected_catalog_authority_sha256:
            return candidate
    return None


def _trust_registry_identity(registry: ResultTrustRegistry) -> tuple[str, str]:
    """Read a trust registry's immutable identity without invoking its hooks."""

    try:
        metadata = object.__getattribute__(registry, "__dict__")["_metadata"]
        identity = (metadata.registry_id, metadata.registry_epoch_sha256)
    except (AttributeError, KeyError, TypeError):
        raise CatalogError("catalog trust registry is unsupported") from None
    if not all(type(item) is str for item in identity):
        raise CatalogError("catalog trust registry is unsupported")
    return identity


class ExecutionState(StrEnum):
    COMPLETE = "complete"


class InformationState(StrEnum):
    AVAILABLE = "available"


class TrustState(StrEnum):
    DEVELOPMENT_SIGNATURE_VERIFIED = "development_signature_verified"


class CatalogQualificationState(StrEnum):
    UNKNOWN = "unknown"
    DEVELOPMENT_UNQUALIFIED = "development_unqualified"
    QUALIFIED = "qualified"


class CatalogAliases(CatalogModel):
    """Controlled opaque labels; no protected-identifier mapping is accepted."""

    display_alias: DisplayAlias
    run_alias: RunAlias
    timepoint_alias: TimepointAlias


class CatalogResultRef(CatalogModel):
    """Closed immutable read reference with orthogonal state dimensions."""

    schema_version: Literal["traceback.catalog-result-ref.v1"] = (
        "traceback.catalog-result-ref.v1"
    )
    result_id: ResultId
    bundle_sha256: Sha256
    bundle_record_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    bundle_manifest_sha256: Sha256
    workflow_release_id: str = Field(min_length=1, max_length=128)
    method_ref: MethodReference
    method_definition_sha256: Sha256
    registry_sha256: Sha256
    registry_version: int = Field(ge=1)
    authority_head_sha256: Sha256
    authority_revision: int = Field(ge=0)
    authority_scope: AuthorityScope
    capability_as_of: datetime
    execution_state: Literal[ExecutionState.COMPLETE] = ExecutionState.COMPLETE
    information_state: Literal[InformationState.AVAILABLE] = InformationState.AVAILABLE
    trust_state: Literal[TrustState.DEVELOPMENT_SIGNATURE_VERIFIED] = (
        TrustState.DEVELOPMENT_SIGNATURE_VERIFIED
    )
    qualification_state: CatalogQualificationState
    display_role: DisplayRole | None
    research_inspectable: bool
    current_provider_eligible: bool

    @model_validator(mode="after")
    def identity_is_derived(self) -> CatalogResultRef:
        identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "bundle_sha256": self.bundle_sha256,
                    "method_definition_sha256": self.method_definition_sha256,
                }
            )
        ).hexdigest()
        if self.result_id != f"result_{identity[:40]}":
            raise ValueError("catalog result identity is invalid")
        return self


class PreparedCatalogImport(CatalogModel):
    """Opaque in-process preparation; only its originating catalog may adopt it."""

    schema_version: Literal["traceback.prepared-catalog-import.v1"] = (
        "traceback.prepared-catalog-import.v1"
    )
    publication_id: PublicationId
    recovery_scope_sha256: Sha256
    reference: CatalogResultRef
    aliases: CatalogAliases
    authority: CatalogAuthoritySnapshot
    authority_sha256: Sha256
    reader_id: str = Field(pattern=r"^reader_[a-z0-9]+(?:_[a-z0-9]+)*$")
    already_visible: bool
    already_owned: bool

    @model_validator(mode="after")
    def authority_digest_matches(self) -> PreparedCatalogImport:
        if self.authority_sha256 != catalog_authority_sha256(self.authority):
            raise ValueError("prepared catalog authority digest is invalid")
        return self


class PendingCatalogPublication(CatalogModel):
    """Durable recovery key for a hidden catalog publication."""

    publication_id: PublicationId
    reference: CatalogResultRef
    state: Literal["pending", "adopted"]


class CoordinatedCatalogCandidate(CatalogModel):
    """Durable identity of one incomplete coordinator publication attempt."""

    schema_version: Literal["traceback.coordinated-catalog-candidate.v1"] = (
        "traceback.coordinated-catalog-candidate.v1"
    )
    operation_id: Annotated[str, StringConstraints(pattern=r"^candidate_[0-9a-f]{64}$")]
    publication_id: PublicationId
    result_id: ResultId
    recovery_scope_sha256: Sha256
    cohort_manifest_sha256: Sha256
    binding_id: Annotated[str, StringConstraints(pattern=r"^binding_[0-9a-f]{64}$")]
    final_name: str = Field(min_length=1, max_length=160)
    binding_sha256: Sha256
    marker_sha256: Sha256


class CatalogOrder(StrEnum):
    RESULT_ID_ASC = "result_id_asc"
    RESULT_ID_DESC = "result_id_desc"


class CatalogQuery(CatalogModel):
    method_refs: tuple[MethodReference, ...] = Field(
        default=(), max_length=MAX_FILTER_VALUES
    )
    execution_states: tuple[ExecutionState, ...] = Field(
        default=(), max_length=MAX_FILTER_VALUES
    )
    information_states: tuple[InformationState, ...] = Field(
        default=(), max_length=MAX_FILTER_VALUES
    )
    trust_states: tuple[TrustState, ...] = Field(
        default=(), max_length=MAX_FILTER_VALUES
    )
    qualification_states: tuple[CatalogQualificationState, ...] = Field(
        default=(), max_length=MAX_FILTER_VALUES
    )
    display_alias: DisplayAlias | None = None
    run_alias: RunAlias | None = None
    timepoint_alias: TimepointAlias | None = None
    order: CatalogOrder = CatalogOrder.RESULT_ID_ASC
    limit: int = Field(default=50, ge=1, le=MAX_QUERY_LIMIT)
    cursor: ResultId | None = None

    @field_validator("method_refs", mode="before")
    @classmethod
    def normalize_methods(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        if len(value) > MAX_FILTER_VALUES:
            raise ValueError("method filter exceeds its value bound")
        methods = (MethodReference.model_validate(item) for item in value)
        unique = {(item.method_id, item.version): item for item in methods}
        return tuple(unique[key] for key in sorted(unique))

    @field_validator(
        "execution_states",
        "information_states",
        "trust_states",
        "qualification_states",
        mode="after",
    )
    @classmethod
    def normalize_filters(cls, value: object) -> object:
        return tuple(sorted(set(value), key=lambda item: item.value))


class CatalogEmptyReason(StrEnum):
    NO_IMPORTED_RESULTS = "no_imported_results"
    NO_MATCHES = "no_matches"


class CatalogPage(CatalogModel):
    schema_version: Literal["traceback.catalog-page.v1"] = "traceback.catalog-page.v1"
    results: tuple[CatalogResultRef, ...]
    next_cursor: ResultId | None = None
    empty: bool
    empty_reason: CatalogEmptyReason | None = None

    @model_validator(mode="after")
    def coherent_empty_state(self) -> CatalogPage:
        if self.empty != (len(self.results) == 0):
            raise ValueError("empty state must match result count")
        if self.empty != (self.empty_reason is not None):
            raise ValueError("empty reason must appear exactly for empty pages")
        return self


class CatalogVerificationContext(CatalogModel):
    """Current authority inputs required to re-verify one catalog result."""

    registry: MethodRegistry
    authority_head: AuthorityHead
    expected_authority_head_sha256: Sha256
    capability: CurrentMethodCapability

    @model_validator(mode="after")
    def current_authority_replays(self) -> CatalogVerificationContext:
        replay_current_capability(
            self.registry,
            self.authority_head,
            self.expected_authority_head_sha256,
            self.capability,
        )
        if _capability_is_revoked(self.registry, self.capability):
            raise ValueError("catalog verification authority is revoked")
        return self


def _qualification(value: QualificationState | None) -> CatalogQualificationState:
    if value is None:
        return CatalogQualificationState.UNKNOWN
    return CatalogQualificationState(value.value)


def _capability_is_revoked(
    registry: MethodRegistry,
    capability: CurrentMethodCapability,
) -> bool:
    qualification_refs = {
        item.record_ref
        for item in registry.qualification_records
        if item.method_ref == capability.method_ref
    }
    role_refs = {
        item.assignment_ref
        for item in registry.display_role_assignments
        if item.method_ref == capability.method_ref
        and item.authority_scope == capability.authority_scope
    }
    revoked_qualification = any(
        item.target == RevocationTarget.QUALIFICATION
        and item.qualification_record_ref in qualification_refs
        and item.effective_at <= capability.as_of
        for item in registry.revocations
    )
    revoked_role = any(
        item.target == RevocationTarget.DISPLAY_ROLE
        and item.display_role_assignment_ref in role_refs
        and item.effective_at <= capability.as_of
        for item in registry.revocations
    )
    return (capability.qualification_state is None and revoked_qualification) or (
        capability.display_role is None and revoked_role
    )


def _safe_relative(value: str) -> tuple[str, ...]:
    candidate = PurePosixPath(value)
    if (
        not value
        or len(value) > 256
        or candidate.is_absolute()
        or candidate.as_posix() != value
        or len(candidate.parts) > MAX_IMPORT_DEPTH
        or any(
            part in {"", ".", ".."}
            or len(part) > 64
            or not all(char.isalnum() or char in "_.-" for char in part)
            for part in candidate.parts
        )
    ):
        raise CatalogFilesystemError("catalog import path is invalid")
    return candidate.parts


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _descriptor_path(descriptor: int) -> Path:
    proc_path = Path("/proc/self/fd") / str(descriptor)
    if Path("/proc/self/fd").is_dir():
        return proc_path
    import fcntl

    raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
    return Path(raw.split(b"\0", 1)[0].decode())


def _descriptor_identity(value: os.stat_result) -> tuple[int, int, int]:
    return stat.S_IFMT(value.st_mode), value.st_dev, value.st_ino


def _open_descriptor_identities() -> dict[int, tuple[int, int, int]]:
    directory = Path("/proc/self/fd")
    if not directory.is_dir():
        directory = Path("/dev/fd")
    try:
        candidates = (
            int(item.name) for item in directory.iterdir() if item.name.isdigit()
        )
        opened = {}
        for descriptor in candidates:
            try:
                identity = _descriptor_identity(os.fstat(descriptor))
            except OSError:
                continue
            opened[descriptor] = identity
        return opened
    except OSError:
        raise CatalogFilesystemError(
            "database descriptor proof is unavailable"
        ) from None


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise OSError
    return descriptor


def _inventory(bundle_fd: int) -> None:
    if frozenset(os.listdir(bundle_fd)) != _TOP_LEVEL:
        raise CatalogFilesystemError("bundle inventory is not exact")
    for directory, expected in _NESTED.items():
        nested_fd = _open_directory_at(bundle_fd, directory)
        try:
            if frozenset(os.listdir(nested_fd)) != expected:
                raise CatalogFilesystemError("bundle inventory is not exact")
        finally:
            os.close(nested_fd)


def _open_file_at(bundle_fd: int, relative: str) -> tuple[int, int | None]:
    parts = relative.split("/")
    parent_fd = bundle_fd
    owned_parent: int | None = None
    if len(parts) == 2:
        owned_parent = _open_directory_at(bundle_fd, parts[0])
        parent_fd = owned_parent
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(parts[-1], flags, dir_fd=parent_fd)
    except Exception:
        if owned_parent is not None:
            os.close(owned_parent)
        raise
    return descriptor, owned_parent


def _copy_exact_bundle(
    bundle_fd: int, destination: Path | None
) -> tuple[str, str, dict[str, tuple[int, ...]]]:
    _inventory(bundle_fd)
    records: list[dict[str, object]] = []
    source_identities: dict[str, tuple[int, ...]] = {}
    total = 0
    for relative in _FIXED_FILES:
        descriptor, owned_parent = _open_file_at(bundle_fd, relative)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise CatalogFilesystemError("bundle contains a non-regular entry")
            limit = _MAX_FILE_BYTES[relative]
            if before.st_size > limit or total + before.st_size > _MAX_TOTAL_BYTES:
                raise CatalogFilesystemError("bundle exceeds its byte bound")
            digest = hashlib.sha256()
            copied = 0
            target = (
                destination.joinpath(*relative.split("/"))
                if destination is not None
                else None
            )
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                output = target.open("xb")
            else:
                output = None
            try:
                source = os.fdopen(descriptor, "rb", closefd=False)
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > limit or total + copied > _MAX_TOTAL_BYTES:
                        raise CatalogFilesystemError("bundle exceeds its byte bound")
                    digest.update(block)
                    if output is not None:
                        output.write(block)
                if output is not None:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output is not None:
                    output.close()
            after = os.fstat(descriptor)
            if (
                _stat_identity(before) != _stat_identity(after)
                or copied != before.st_size
            ):
                raise CatalogFilesystemError("bundle changed during import")
            total += copied
            source_identities[relative] = _stat_identity(after)
            records.append(
                {"path": relative, "sha256": digest.hexdigest(), "size": copied}
            )
        finally:
            os.close(descriptor)
            if owned_parent is not None:
                os.close(owned_parent)
    identity = canonical_json_bytes(
        {"schema_version": "traceback.catalog-bundle-tree.v1", "files": records}
    )
    manifest_digest = next(
        item["sha256"] for item in records if item["path"] == "bundle-manifest.json"
    )
    assert isinstance(manifest_digest, str)
    return hashlib.sha256(identity).hexdigest(), manifest_digest, source_identities


def _verify_source_identities(
    bundle_fd: int, expected: Mapping[str, tuple[int, ...]]
) -> None:
    _inventory(bundle_fd)
    for relative in _FIXED_FILES:
        descriptor, owned_parent = _open_file_at(bundle_fd, relative)
        try:
            if _stat_identity(os.fstat(descriptor)) != expected[relative]:
                raise CatalogFilesystemError("bundle changed during import")
        finally:
            os.close(descriptor)
            if owned_parent is not None:
                os.close(owned_parent)


def _seal_tree(root: Path) -> None:
    for item in sorted(root.rglob("*"), reverse=True):
        item.chmod(0o555 if item.is_dir() else 0o444)
    root.chmod(0o555)


def _fsync_tree(root: Path) -> None:
    for directory in (root / "charts", root / "measurements", root):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    for item in root.rglob("*"):
        try:
            item.chmod(0o700 if item.is_dir() else 0o600)
        except OSError:
            pass
    root.chmod(0o700)
    shutil.rmtree(root)


class ResultCatalog:
    """Transactional immutable result catalog with bounded deterministic queries."""

    _PINNED_FAULT_FIELDS = frozenset(
        {
            "_fault_controller",
            "_fault_controller_identity",
            "_fault_controller_configuration",
            "trust_store",
            "result_trust_registry",
            "_result_trust_identity",
            "_trust_fence_holders",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._PINNED_FAULT_FIELDS and name in vars(self):
            raise AttributeError(f"{name} is read-only")
        super().__setattr__(name, value)

    def __init__(
        self,
        root: str | Path,
        *,
        import_roots: Mapping[str, str | Path],
        trust_store: TrustStore | None = None,
        result_trust_registry: ResultTrustRegistry | None = None,
        reader_registry: ResultBundleReaderRegistry = DEFAULT_RESULT_BUNDLE_READER_REGISTRY,
        fault_controller: DeterministicFaultController = NO_FAULTS,
    ) -> None:
        """Open one catalog bound to exactly one result trust authority.

        ``result_trust_registry`` is the protected, forward-only path: every
        verification reads the registry's current trust under its read fence,
        so a revocation applies to the next verification without reopening.
        ``trust_store`` is the earlier caller-held store, kept for callers that
        build one directly.
        """
        if not import_roots or len(import_roots) > MAX_IMPORT_ROOTS:
            raise CatalogFilesystemError("catalog import root count is invalid")
        self.root = Path(root).absolute()
        self.import_roots = {
            _ROOT_ID.validate_python(root_id): Path(path)
            for root_id, path in import_roots.items()
        }
        if any(not path.is_absolute() for path in self.import_roots.values()):
            raise CatalogFilesystemError("catalog import roots must be absolute")
        if type(reader_registry) is not ResultBundleReaderRegistry:
            raise CatalogUnsupportedSchema("bundle reader registry is unsupported")
        if (
            "select" in vars(reader_registry)
            or ResultBundleReaderRegistry.select is not _PINNED_READER_SELECT
        ):
            raise CatalogUnsupportedSchema("bundle reader registry is unsupported")
        if (trust_store is None) == (result_trust_registry is None):
            raise CatalogError("catalog requires exactly one result trust authority")
        if trust_store is not None:
            if (
                type(trust_store) is not TrustStore
                or "resolve" in vars(trust_store)
                or TrustStore.resolve is not _PINNED_TRUST_RESOLVE
            ):
                raise CatalogError("catalog trust store is unsupported")
            self._result_trust_identity = None
        else:
            if (
                type(result_trust_registry) is not ResultTrustRegistry
                or ResultTrustRegistry.read_fence is not _PINNED_TRUST_READ_FENCE
            ):
                raise CatalogError("catalog trust registry is unsupported")
            self._result_trust_identity = _trust_registry_identity(
                result_trust_registry
            )
        self.trust_store = trust_store
        self.result_trust_registry = result_trust_registry
        # Thread ident -> the trust snapshot whose read fence that thread holds
        # through this catalog.  Only touched under ``_connection_lock``.
        self._trust_fence_holders: dict[int, ResultTrustSnapshot] = {}
        self._trust_high_water: tuple[int, str] | None = None
        try:
            self.reader_registry = ResultBundleReaderRegistry.model_validate_json(
                canonical_json_bytes(reader_registry)
            )
        except Exception:  # noqa: BLE001 - normalize hostile model state
            raise CatalogUnsupportedSchema(
                "bundle reader registry is unsupported"
            ) from None
        self._reader_registry_bytes = canonical_json_bytes(self.reader_registry)
        if type(fault_controller) is not DeterministicFaultController:
            raise TypeError("fault controller must be exact")
        self._fault_controller = fault_controller
        self._fault_controller_identity = id(fault_controller)
        self._fault_controller_configuration = _PINNED_FAULT_SNAPSHOT(fault_controller)
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise CatalogFilesystemError("catalog root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._root_fd = os.open(self.root, root_flags)
        except OSError:
            raise CatalogFilesystemError("catalog root is unsafe") from None
        self._root_identity = _inode_identity(os.fstat(self._root_fd))
        self.objects = self.root / "objects"
        try:
            os.mkdir("objects", mode=0o700, dir_fd=self._root_fd)
        except FileExistsError:
            pass
        try:
            self._objects_fd = _open_directory_at(self._root_fd, "objects")
        except OSError:
            os.close(self._root_fd)
            raise CatalogFilesystemError("catalog object store is unsafe") from None
        self._objects_identity = _inode_identity(os.fstat(self._objects_fd))
        os.fchmod(self._objects_fd, 0o700)
        self.database = self.root / "catalog.sqlite3"
        self._database_fd: int | None = None
        self._sqlite_database_fd: int | None = None
        self._database_identity: tuple[int, int] | None = None
        self._connection: sqlite3.Connection | None = None
        self._connection_lock = threading.RLock()
        # Cross-process catalog-content lock (``flock`` on a lock file in the
        # root).  Held only under ``_connection_lock``, so at most one thread
        # of this instance owns it: (pid, thread ident, exclusive) or None.
        self._content_lock_fd: int | None = None
        self._content_lock_identity: tuple[int, int] | None = None
        self._content_lock_owner: tuple[int, int, bool] | None = None
        self._prepared_imports: dict[str, PreparedCatalogImport] = {}
        try:
            self._open_content_lock()
        except BaseException:
            self.close()
            raise
        try:
            database_stat = os.stat(
                "catalog.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(database_stat.st_mode):
                self.close()
                raise CatalogFilesystemError("catalog database is unsafe")
            self._database_identity = _inode_identity(database_stat)
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _validate_verification_authority(self) -> None:
        registry = self.result_trust_registry
        if registry is None:
            if (
                type(self.trust_store) is not TrustStore
                or "resolve" in vars(self.trust_store)
                or TrustStore.resolve is not _PINNED_TRUST_RESOLVE
                or self._result_trust_identity is not None
            ):
                raise CatalogError("catalog trust store is unsupported")
        elif (
            self.trust_store is not None
            or type(registry) is not ResultTrustRegistry
            or ResultTrustRegistry.read_fence is not _PINNED_TRUST_READ_FENCE
            or _trust_registry_identity(registry) != self._result_trust_identity
        ):
            raise CatalogError("catalog trust registry is unsupported")
        if (
            type(self.reader_registry) is not ResultBundleReaderRegistry
            or "select" in vars(self.reader_registry)
            or ResultBundleReaderRegistry.select is not _PINNED_READER_SELECT
        ):
            raise CatalogUnsupportedSchema("bundle reader registry is unsupported")
        try:
            current = canonical_json_bytes(self.reader_registry)
            reparsed = ResultBundleReaderRegistry.model_validate_json(current)
        except Exception:  # noqa: BLE001 - normalize hostile model state
            raise CatalogUnsupportedSchema(
                "bundle reader registry is unsupported"
            ) from None
        if current != self._reader_registry_bytes or reparsed != self.reader_registry:
            raise CatalogUnsupportedSchema("bundle reader registry changed")

    def authority_snapshot(self) -> CatalogAuthoritySnapshot:
        """Return an opaque digest-bound snapshot of exact live catalog authority."""

        _RC_VALIDATE_STORAGE(self)
        _RC_VALIDATE_VERIFICATION_AUTHORITY(self)
        if self.result_trust_registry is not None:
            schema_version = CATALOG_AUTHORITY_SCHEMA_V2
            with _RC_TRUST_FENCE(self) as (_, trust):
                if type(trust) is not ResultTrustSnapshot:
                    raise CatalogError("catalog trust registry changed")
                trust_sha256 = registry_trust_snapshot_sha256(trust)
        else:
            schema_version = CATALOG_AUTHORITY_SCHEMA_V1
            try:
                with self.trust_store._lock:
                    keys = tuple(
                        {
                            "key_id": key.key_id,
                            "purpose": key.purpose.value,
                            "namespace": key.namespace.value,
                            "public_key_hex": key.public_key_bytes.hex(),
                            "revoked": key.revoked,
                        }
                        for key in sorted(
                            self.trust_store._keys.values(),
                            key=lambda item: item.key_id,
                        )
                    )
            except Exception:  # noqa: BLE001 - normalize hostile trust-store state
                raise CatalogError("catalog trust store is unsupported") from None
            trust_sha256 = hashlib.sha256(
                b"traceback-catalog-trust-v1\0" + canonical_json_bytes(keys)
            ).hexdigest()
        storage_sha256 = hashlib.sha256(
            b"traceback-catalog-storage-v1\0"
            + canonical_json_bytes(
                {
                    "configured_root_sha256": hashlib.sha256(
                        os.fsencode(self.root)
                    ).hexdigest(),
                    "root_identity": self._root_identity,
                    "objects_identity": self._objects_identity,
                    "database_identity": self._database_identity,
                }
            )
        ).hexdigest()
        return CatalogAuthoritySnapshot(
            schema_version=schema_version,
            storage_identity_sha256=storage_sha256,
            trust_snapshot_sha256=trust_sha256,
            reader_registry_sha256=hashlib.sha256(
                b"traceback-catalog-reader-registry-v1\0" + self._reader_registry_bytes
            ).hexdigest(),
        )

    @contextmanager
    def _trust_fence(self) -> Iterator[tuple[TrustStore, ResultTrustSnapshot | None]]:
        """Yield the trust store every verification in the body must use.

        Registry path: hold this catalog's ``_connection_lock`` and then the
        registry read fence through the body, and yield a fresh ``TrustStore``
        built from the yielded snapshot.  A nested entry on the same thread
        reuses the held snapshot (the registry lock is not reentrant), so one
        operation sees one trust head.  Lock order: ``_connection_lock``, then
        the trust read fence.

        TrustStore path: yields the caller-held store and holds nothing, as
        before.
        """

        registry = self.result_trust_registry
        if registry is None:
            yield self.trust_store, None
            return
        thread = threading.get_ident()
        with self._connection_lock:
            _RC_VALIDATE_VERIFICATION_AUTHORITY(self)
            held = self._trust_fence_holders.get(thread)
            if held is not None:
                yield (
                    _PINNED_LOAD_TRUST(_PINNED_TRUST_DOCUMENT_BYTES(held.document)),
                    held,
                )
                return
            phase = "enter"
            try:
                with _PINNED_TRUST_READ_FENCE(registry) as snapshot:
                    if (
                        type(snapshot) is not ResultTrustSnapshot
                        or (snapshot.registry_id, snapshot.registry_epoch_sha256)
                        != self._result_trust_identity
                    ):
                        raise CatalogError("catalog trust registry changed")
                    high_water = self._trust_high_water
                    if high_water is not None and (
                        snapshot.state_version < high_water[0]
                        or (
                            snapshot.state_version == high_water[0]
                            and snapshot.state_head_sha256 != high_water[1]
                        )
                    ):
                        raise CatalogError("catalog result trust rolled back")
                    store = _PINNED_LOAD_TRUST(
                        _PINNED_TRUST_DOCUMENT_BYTES(snapshot.document)
                    )
                    self._trust_high_water = (
                        snapshot.state_version,
                        snapshot.state_head_sha256,
                    )
                    self._trust_fence_holders[thread] = snapshot
                    phase = "body"
                    try:
                        yield store, snapshot
                    finally:
                        self._trust_fence_holders.pop(thread, None)
                    phase = "exit"
            except ResultTrustRegistryError:
                if phase == "body":
                    raise
                raise CatalogError("catalog result trust is unavailable") from None

    @contextmanager
    def trust_authority_fence(self) -> Iterator[ResultTrustSnapshot | None]:
        """Hold this catalog's trust authority for a composing caller's body.

        Lock order: ``_connection_lock``, then the catalog-content lock held
        shared (or the exclusive content lock this thread already holds),
        then the trust read fence.  On the registry path every catalog
        verification in the body uses the yielded snapshot and no trust event
        commits until the body exits; on both paths no E04 import, staging,
        adoption, compensation, recovery or other catalog-row write commits,
        from any process, until the body exits.  The TrustStore path yields
        ``None`` and holds no trust lock.  The body must not open a linkage
        fence, call a D07 registry, mutate the trust registry, or write this
        catalog (a shared content lock is never upgraded).
        """

        with (
            self._connection_lock,
            _RC_CONTENT_LOCK(self, exclusive=False),
            _RC_TRUST_FENCE(self) as (_, snapshot),
        ):
            yield snapshot

    def _open_content_lock(self) -> None:
        """Open (creating once) the root's private catalog-content lock file.

        Open-existing first, then exclusive create, retried a bounded number
        of times: concurrent ``O_CREAT`` opens of one name can fail with
        ``ENOENT`` on some filesystems.
        """

        flags = (
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        for _ in range(16):
            try:
                descriptor = os.open(
                    CATALOG_CONTENT_LOCK_NAME, flags, dir_fd=self._root_fd
                )
                break
            except FileNotFoundError:
                pass
            except OSError:
                raise CatalogFilesystemError("catalog content lock is unsafe") from None
            try:
                descriptor = os.open(
                    CATALOG_CONTENT_LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self._root_fd,
                )
                break
            except (FileExistsError, FileNotFoundError):
                continue
            except OSError:
                raise CatalogFilesystemError("catalog content lock is unsafe") from None
        if descriptor is None:
            raise CatalogFilesystemError("catalog content lock is unsafe")
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            os.close(descriptor)
            raise CatalogFilesystemError("catalog content lock is unsafe")
        self._content_lock_fd = descriptor
        self._content_lock_identity = _inode_identity(metadata)

    @contextmanager
    def _content_lock(self, *, exclusive: bool) -> Iterator[None]:
        """Hold the cross-process catalog-content lock through the body.

        Every catalog-row writer (import, preparation, staging, adoption,
        finish, compensation, discard, candidate registration/finish,
        recovery, schema initialization) holds it exclusively; composing
        readers (``trust_authority_fence``, ``content_authority_fence``) hold
        it shared.  It sits after ``_connection_lock`` and before the trust
        read fence (and before ``_SQLITE_OPEN_LOCK``).  Reentrant on the
        owning thread: a nested entry reuses the held mode, and an exclusive
        request under a held shared lock is refused, never upgraded.
        """

        if type(exclusive) is not bool:
            raise CatalogError("catalog content lock mode is invalid")
        with self._connection_lock:
            owner = self._content_lock_owner
            current = (os.getpid(), threading.get_ident())
            if owner is not None:
                if owner[:2] != current:
                    raise CatalogError("catalog content lock owner is invalid")
                if exclusive and not owner[2]:
                    raise CatalogConflict(
                        "catalog content lock cannot be upgraded to exclusive"
                    )
                yield
                return
            descriptor = self._content_lock_fd
            if descriptor is None:
                raise CatalogFilesystemError("catalog is closed")
            try:
                named = os.stat(
                    CATALOG_CONTENT_LOCK_NAME,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                held = os.fstat(descriptor)
            except OSError:
                raise CatalogFilesystemError("catalog content lock changed") from None
            if (
                not stat.S_ISREG(named.st_mode)
                or _inode_identity(named) != self._content_lock_identity
                or _inode_identity(held) != self._content_lock_identity
            ):
                raise CatalogFilesystemError("catalog content lock changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                self._content_lock_owner = (*current, exclusive)
                try:
                    yield
                finally:
                    self._content_lock_owner = None
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    @contextmanager
    def content_authority_fence(self, *, exclusive: bool = False) -> Iterator[None]:
        """Hold this catalog's content lock for a composing caller's body.

        Shared: no catalog-row write commits, from any process, until the
        body exits (other shared holders proceed).  Exclusive: for a
        composing writer (the D06 import) that will call this catalog's
        writers inside the body.  Lock order: after ``_connection_lock``,
        before the trust read fence; the composing caller takes it before
        ``trust_authority_fence``.  Reentrant on the owning thread; a shared
        hold is never upgraded.
        """

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=exclusive):
            yield

    def _content_sha256_locked(self) -> str:
        owner = self._content_lock_owner
        if owner is None or owner[:2] != (os.getpid(), threading.get_ident()):
            raise CatalogError("catalog content fence is absent")
        digest = hashlib.sha256(b"traceback-catalog-content-v1\0")
        with _RC_CONNECT(self) as connection:
            if connection.in_transaction:
                raise CatalogError("catalog content read is nested in a transaction")
            connection.execute("BEGIN")
            try:
                for table, columns, key in _CONTENT_TABLES:
                    name = table.encode("ascii")
                    digest.update(b"t" + len(name).to_bytes(8, "big") + name)
                    count = 0
                    cursor = connection.execute(
                        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {key}"
                    )
                    for row in cursor:
                        count += 1
                        digest.update(b"r")
                        for value in tuple(row):
                            digest.update(_content_value_bytes(value))
                    digest.update(b"c" + count.to_bytes(8, "big"))
                connection.commit()
            except BaseException as error:
                connection.rollback()
                if isinstance(error, sqlite3.DatabaseError):
                    raise CatalogUnsupportedSchema(
                        "catalog content is unreadable"
                    ) from None
                raise
        return digest.hexdigest()

    def content_head_in_fence(self) -> CatalogContentSnapshot:
        """Return the catalog content head; requires this thread's content lock.

        The composing caller holds ``trust_authority_fence`` or
        ``content_authority_fence``; this read takes no lock of its own.
        """

        with self._connection_lock:
            _RC_VALIDATE_STORAGE(self)
            return CatalogContentSnapshot(
                content_sha256=_RC_CONTENT_SHA256_LOCKED(self)
            )

    def content_snapshot(self) -> CatalogContentSnapshot:
        """Return the catalog content head under a shared content lock."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=False):
            return _RC_CONTENT_HEAD_IN_FENCE(self)

    def _fault(self, point: str) -> None:
        controller = self._fault_controller
        try:
            snapshot = _PINNED_FAULT_SNAPSHOT(controller)
        except (TypeError, ValueError):
            raise CatalogError("catalog fault controller changed") from None
        if id(controller) != self._fault_controller_identity or (
            snapshot != self._fault_controller_configuration
        ):
            raise CatalogError("catalog fault controller changed")
        try:
            _PINNED_FAULT_HIT(controller, point)
        except (TypeError, ValueError):
            raise CatalogError("catalog fault controller changed") from None

    @property
    def _bound_objects(self) -> Path:
        return _descriptor_path(self._objects_fd)

    def close(self) -> None:
        connection_lock = getattr(self, "_connection_lock", None)
        if connection_lock is None:
            with _SQLITE_OPEN_LOCK:
                self._close_unlocked()
            return
        with connection_lock, _SQLITE_OPEN_LOCK:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            except (sqlite3.Error, TypeError, AttributeError):
                pass
            self._connection = None
            self._sqlite_database_fd = None
        for attribute in (
            "_database_fd",
            "_content_lock_fd",
            "_objects_fd",
            "_root_fd",
        ):
            descriptor = getattr(self, attribute, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except (OSError, TypeError, AttributeError):
                    pass
                setattr(self, attribute, None)

    def __del__(self) -> None:
        self.close()

    def _validate_storage(self) -> None:
        _RC_ASSERT_RUNTIME(self)
        try:
            root_stat = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or _inode_identity(root_stat) != self._root_identity
            ):
                raise CatalogFilesystemError("catalog root changed")
            rebound_objects = _open_directory_at(self._root_fd, "objects")
            try:
                if _inode_identity(os.fstat(rebound_objects)) != self._objects_identity:
                    raise CatalogFilesystemError("catalog object store changed")
            finally:
                os.close(rebound_objects)
            if self._database_identity is not None:
                database_stat = os.stat(
                    "catalog.sqlite3",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(database_stat.st_mode)
                    or _inode_identity(database_stat) != self._database_identity
                ):
                    raise CatalogFilesystemError("catalog database changed")
                if self._database_fd is not None and (
                    _inode_identity(os.fstat(self._database_fd))
                    != self._database_identity
                ):
                    raise CatalogFilesystemError("catalog database changed")
                if self._sqlite_database_fd is not None and (
                    _inode_identity(os.fstat(self._sqlite_database_fd))
                    != self._database_identity
                ):
                    raise CatalogFilesystemError("catalog connection changed")
        except CatalogError:
            raise
        except (OSError, TypeError):
            raise CatalogFilesystemError("catalog storage changed") from None

    def _bind_database_descriptor(self) -> None:
        if self._database_identity is None or self._database_fd is not None:
            return
        database_flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open("catalog.sqlite3", database_flags, dir_fd=self._root_fd)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _inode_identity(metadata) != self._database_identity
        ):
            os.close(descriptor)
            raise CatalogFilesystemError("catalog database changed")
        self._database_fd = descriptor

    def _open_sqlite_connection(self) -> sqlite3.Connection:
        _RC_VALIDATE_STORAGE(self)
        _RC_BIND_DATABASE_DESCRIPTOR(self)
        descriptors_before = _open_descriptor_identities()
        try:
            connection = sqlite3.connect(
                self.database,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            database_stat = os.stat(
                "catalog.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
            if not stat.S_ISREG(database_stat.st_mode):
                raise CatalogFilesystemError("catalog database is unsafe")
            observed_identity = _inode_identity(database_stat)
            if (
                self._database_identity is not None
                and observed_identity != self._database_identity
            ):
                raise CatalogFilesystemError("catalog database changed")
            matching_descriptors = []
            for descriptor, identity in _open_descriptor_identities().items():
                if descriptors_before.get(descriptor) == identity:
                    continue
                if identity != (stat.S_IFREG, *observed_identity):
                    continue
                try:
                    metadata = os.fstat(descriptor)
                except OSError:
                    continue
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and _inode_identity(metadata) == observed_identity
                    and _descriptor_identity(metadata) == identity
                ):
                    matching_descriptors.append(descriptor)
            if len(matching_descriptors) != 1:
                raise CatalogFilesystemError(
                    "catalog database connection identity is unproven"
                )
            self._sqlite_database_fd = matching_descriptors[0]
            if self._database_identity is None:
                self._database_identity = observed_identity
                _RC_BIND_DATABASE_DESCRIPTOR(self)
            if (
                not stat.S_ISREG(os.fstat(self._database_fd).st_mode)
                or _inode_identity(os.fstat(self._database_fd)) != observed_identity
            ):
                raise CatalogFilesystemError("catalog database changed")
            self._database_identity = observed_identity
            _RC_VALIDATE_STORAGE(self)
        except BaseException as error:
            if "connection" in locals():
                connection.close()
            if isinstance(error, CatalogError):
                raise
            raise CatalogFilesystemError("catalog database changed") from None
        for path in (
            self.database,
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        ):
            if path.exists():
                path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._connection_lock:
            if self._connection is None:
                with _SQLITE_OPEN_LOCK:
                    self._connection = _RC_OPEN_SQLITE_CONNECTION(self)
            _RC_VALIDATE_STORAGE(self)
            try:
                yield self._connection
            finally:
                _RC_VALIDATE_STORAGE(self)

    def _initialize(self) -> None:
        # Lock order: connection lock, exclusive content lock, then the
        # module SQLite open lock (as every catalog writer's first connect).
        with (
            self._connection_lock,
            _RC_CONTENT_LOCK(self, exclusive=True),
            _SQLITE_OPEN_LOCK,
            _RC_CONNECT(self) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                objects = frozenset(
                    (row[0], row[1])
                    for row in connection.execute(
                        """SELECT type, name FROM sqlite_master
                           WHERE name NOT LIKE 'sqlite_%'
                           ORDER BY type, name"""
                    )
                )
                if not objects:
                    for statement in _CATALOG_SCHEMA_SQL.values():
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO metadata VALUES('schema_version', ?)",
                        (str(CATALOG_SCHEMA_VERSION),),
                    )
                else:
                    self._migrate_schema(connection)
                self._validate_schema(connection)
                connection.commit()
            except BaseException as error:
                connection.rollback()
                if isinstance(error, CatalogError):
                    raise
                if isinstance(error, sqlite3.DatabaseError):
                    raise CatalogUnsupportedSchema(
                        "catalog schema is unsupported"
                    ) from None
                raise
            os.chmod(self.database, 0o600)
        _RC_VALIDATE_STORAGE(self)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        try:
            schema = {
                (row[0], row[1]): _normalize_schema_sql(row[2])
                for row in connection.execute(
                    """SELECT type, name, sql FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_%'
                       ORDER BY type, name"""
                )
                if isinstance(row[2], str)
            }
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            metadata_count = connection.execute(
                "SELECT COUNT(*) FROM metadata"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            raise CatalogUnsupportedSchema("catalog schema is unsupported") from None
        if row is None or metadata_count != 1:
            raise CatalogUnsupportedSchema("catalog schema is unsupported")
        if row[0] == "1":
            if schema != _CATALOG_SCHEMA_SIGNATURE_V1:
                raise CatalogUnsupportedSchema("catalog schema is unsupported")
            connection.execute(_CATALOG_SCHEMA_SQL[("table", "result_publications")])
            connection.execute(_CATALOG_SCHEMA_SQL[("table", "coordinated_results")])
            connection.execute(
                _CATALOG_SCHEMA_SQL[("index", "result_publications_state")]
            )
            schema = _CATALOG_SCHEMA_SIGNATURE_V2
        elif row[0] == "2":
            if schema != _CATALOG_SCHEMA_SIGNATURE_V2:
                raise CatalogUnsupportedSchema("catalog schema is unsupported")
        elif row[0] == str(CATALOG_SCHEMA_VERSION):
            return
        else:
            raise CatalogUnsupportedSchema("catalog schema is unsupported")
        connection.execute(_CATALOG_SCHEMA_SQL[("table", "coordinated_candidates")])
        connection.execute(
            _CATALOG_SCHEMA_SQL[("index", "coordinated_candidates_scope")]
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(CATALOG_SCHEMA_VERSION),),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            schema = {
                (row[0], row[1]): _normalize_schema_sql(row[2])
                for row in connection.execute(
                    """SELECT type, name, sql FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_%'
                       ORDER BY type, name"""
                )
                if isinstance(row[2], str)
            }
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            metadata_count = connection.execute(
                "SELECT COUNT(*) FROM metadata"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            raise CatalogUnsupportedSchema("catalog schema is unsupported") from None
        if (
            schema != _CATALOG_SCHEMA_SIGNATURE
            or row is None
            or row[0] != str(CATALOG_SCHEMA_VERSION)
            or metadata_count != 1
        ):
            raise CatalogUnsupportedSchema("catalog schema is unsupported")

    def _capture(self, root_id: str, relative_path: str) -> tuple[Path, str, str]:
        if root_id not in self.import_roots:
            raise CatalogFilesystemError("catalog import root is not registered")
        parts = _safe_relative(relative_path)
        root_path = self.import_roots[root_id]
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        path_bindings: list[tuple[int, str, tuple[int, ...]]] = []
        _RC_VALIDATE_STORAGE(self)
        temporary_name = f".catalog-{uuid.uuid4().hex}"
        os.mkdir(temporary_name, mode=0o700, dir_fd=self._objects_fd)
        temporary = self._bound_objects / temporary_name
        try:
            root_fd = os.open(root_path, flags)
            descriptors.append(root_fd)
            root_stat = os.fstat(root_fd)
            current_fd = root_fd
            for part in parts:
                parent_fd = current_fd
                current_fd = _open_directory_at(parent_fd, part)
                descriptors.append(current_fd)
                path_bindings.append(
                    (parent_fd, part, _stat_identity(os.fstat(current_fd)))
                )
            bundle_sha256, manifest_sha256, source_identities = _copy_exact_bundle(
                current_fd, temporary
            )
            _RC_FAULT(self, "after_bundle_snapshot")
            _RC_VALIDATE_STORAGE(self)
            _verify_source_identities(current_fd, source_identities)
            for parent_fd, part, expected_identity in path_bindings:
                rebound_fd = _open_directory_at(parent_fd, part)
                try:
                    if _stat_identity(os.fstat(rebound_fd)) != expected_identity:
                        raise CatalogFilesystemError("catalog import path changed")
                finally:
                    os.close(rebound_fd)
            final_root = os.stat(root_path, follow_symlinks=False)
            if stat.S_ISLNK(final_root.st_mode) or (
                final_root.st_dev,
                final_root.st_ino,
            ) != (root_stat.st_dev, root_stat.st_ino):
                raise CatalogFilesystemError("catalog import root changed")
            return temporary, bundle_sha256, manifest_sha256
        except CatalogError:
            _remove_tree(self._bound_objects / temporary_name)
            raise
        except Exception:  # noqa: BLE001 - normalize hostile filesystem input
            _remove_tree(self._bound_objects / temporary_name)
            raise CatalogFilesystemError("catalog bundle import failed") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def import_bundle(
        self,
        *,
        root_id: str,
        relative_path: str,
        registry: MethodRegistry,
        authority_head: AuthorityHead,
        expected_authority_head_sha256: str,
        capability: CurrentMethodCapability,
        aliases: CatalogAliases,
    ) -> CatalogResultRef:
        replay_current_capability(
            registry,
            authority_head,
            expected_authority_head_sha256,
            capability,
        )
        if _capability_is_revoked(registry, capability):
            raise CatalogError("revoked method authority cannot be cataloged")
        temporary, bundle_sha256, manifest_sha256 = _RC_CAPTURE(
            self, root_id, relative_path
        )
        object_path = self._bound_objects / bundle_sha256
        try:
            # Lock order: connection lock, exclusive content lock, trust.
            with (
                self._connection_lock,
                _RC_CONTENT_LOCK(self, exclusive=True),
                _RC_TRUST_FENCE(self) as (trust_store, _),
            ):
                _RC_VALIDATE_VERIFICATION_AUTHORITY(self)
                verified = _PINNED_VERIFY_BUNDLE(temporary, trust_store)
                _PINNED_READER_SELECT(self.reader_registry, verified)
                if not isinstance(verified.manifest, ResultBundleManifestV2):
                    raise CatalogUnsupportedSchema("bundle schema is unsupported")
                if (
                    verified.manifest.method.method_id != capability.method_ref.method_id
                    or verified.manifest.method.version != capability.method_ref.version
                    or verified.manifest.method.method_definition_sha256
                    != capability.method_definition_sha256
                ):
                    raise CatalogConflict("bundle method identity conflicts")
                reference = _RC_REFERENCE(
                    verified,
                    bundle_sha256=bundle_sha256,
                    manifest_sha256=manifest_sha256,
                    capability=capability,
                )
                _fsync_tree(temporary)
                _seal_tree(temporary)
                _RC_VALIDATE_STORAGE(self)
                parent_fd = self._objects_fd
                try:
                    try:
                        rename_directory_exclusive_at(
                            parent_fd, temporary.name, object_path.name
                        )
                        os.fsync(parent_fd)
                    except FileExistsError:
                        try:
                            existing_fd = _open_directory_at(parent_fd, object_path.name)
                        except OSError:
                            raise CatalogConflict("catalog object identity conflicts")
                        try:
                            existing_sha256, existing_manifest, _ = _copy_exact_bundle(
                                existing_fd, None
                            )
                        finally:
                            os.close(existing_fd)
                        if (
                            existing_sha256 != bundle_sha256
                            or existing_manifest != manifest_sha256
                        ):
                            raise CatalogConflict("catalog object identity conflicts")
                        _remove_tree(temporary)
                finally:
                    _RC_VALIDATE_STORAGE(self)
                _RC_FAULT(self, "after_object_publish")
                with _RC_CONNECT(self) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        existing = connection.execute(
                            """SELECT r.ref_json,
                               EXISTS(SELECT 1 FROM coordinated_results c
                                      WHERE c.result_id=r.result_id),
                               EXISTS(SELECT 1 FROM result_publications p
                                      WHERE p.result_id=r.result_id AND p.state='adopted')
                               FROM results r
                               WHERE r.result_id=? OR r.bundle_sha256=? OR r.bundle_record_id=?""",
                            (
                                reference.result_id,
                                reference.bundle_sha256,
                                reference.bundle_record_id,
                            ),
                        ).fetchone()
                        if existing is not None:
                            if existing[1] and not existing[2]:
                                raise CatalogConflict(
                                    "catalog result publication is pending"
                                )
                            parsed = CatalogResultRef.model_validate_json(existing[0])
                            alias_row = connection.execute(
                                """SELECT display_alias, run_alias, timepoint_alias
                                   FROM opaque_aliases WHERE result_id=?""",
                                (parsed.result_id,),
                            ).fetchone()
                            if (
                                parsed != reference
                                or alias_row is None
                                or tuple(alias_row)
                                != (
                                    aliases.display_alias,
                                    aliases.run_alias,
                                    aliases.timepoint_alias,
                                )
                            ):
                                raise CatalogConflict("catalog identity conflict")
                            connection.commit()
                            _RC_VALIDATE_STORAGE(self)
                            return parsed
                        connection.execute(
                            """INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (
                                reference.result_id,
                                reference.bundle_sha256,
                                reference.bundle_record_id,
                                reference.method_ref.method_id,
                                reference.method_ref.version,
                                reference.execution_state.value,
                                reference.information_state.value,
                                reference.trust_state.value,
                                reference.qualification_state.value,
                                canonical_json_bytes(reference),
                            ),
                        )
                        try:
                            connection.execute(
                                "INSERT INTO opaque_aliases VALUES(?,?,?,?)",
                                (
                                    reference.result_id,
                                    aliases.display_alias,
                                    aliases.run_alias,
                                    aliases.timepoint_alias,
                                ),
                            )
                        except sqlite3.IntegrityError:
                            raise CatalogConflict(
                                "catalog alias identity conflict"
                            ) from None
                        _RC_FAULT(self, "before_catalog_commit")
                        connection.commit()
                        _RC_VALIDATE_STORAGE(self)
                    except BaseException:
                        connection.rollback()
                        raise
                return reference
        except BaseException:
            if temporary.exists():
                _remove_tree(temporary)
            raise

    def prepare_bundle_import(
        self,
        *,
        root_id: str,
        relative_path: str,
        registry: MethodRegistry,
        authority_head: AuthorityHead,
        expected_authority_head_sha256: str,
        capability: CurrentMethodCapability,
        aliases: CatalogAliases,
        recovery_scope_sha256: str,
    ) -> PreparedCatalogImport:
        """Verify and publish immutable object bytes without exposing a result row."""

        replay_current_capability(
            registry,
            authority_head,
            expected_authority_head_sha256,
            capability,
        )
        if _capability_is_revoked(registry, capability):
            raise CatalogError("revoked method authority cannot be cataloged")
        aliases = CatalogAliases.model_validate_json(canonical_json_bytes(aliases))
        if type(recovery_scope_sha256) is not str:
            raise CatalogConflict("catalog recovery scope is invalid")
        try:
            recovery_scope_sha256 = TypeAdapter(Sha256).validate_python(
                recovery_scope_sha256
            )
        except Exception:  # noqa: BLE001 - normalize hostile recovery scope
            raise CatalogConflict("catalog recovery scope is invalid") from None
        temporary, bundle_sha256, manifest_sha256 = _RC_CAPTURE(
            self, root_id, relative_path
        )
        object_path = self._bound_objects / bundle_sha256
        try:
            # Lock order: connection lock, exclusive content lock, trust.
            with (
                self._connection_lock,
                _RC_CONTENT_LOCK(self, exclusive=True),
                _RC_TRUST_FENCE(self) as (trust_store, _),
            ):
                authority = _RC_AUTHORITY_SNAPSHOT(self)
                verified = _PINNED_VERIFY_BUNDLE(temporary, trust_store)
                reader = _PINNED_READER_SELECT(self.reader_registry, verified)
                if not isinstance(verified.manifest, ResultBundleManifestV2):
                    raise CatalogUnsupportedSchema("bundle schema is unsupported")
                if (
                    verified.manifest.method.method_id != capability.method_ref.method_id
                    or verified.manifest.method.version != capability.method_ref.version
                    or verified.manifest.method.method_definition_sha256
                    != capability.method_definition_sha256
                ):
                    raise CatalogConflict("bundle method identity conflicts")
                reference = _RC_REFERENCE(
                    verified,
                    bundle_sha256=bundle_sha256,
                    manifest_sha256=manifest_sha256,
                    capability=capability,
                )
                _fsync_tree(temporary)
                _seal_tree(temporary)
                _RC_VALIDATE_STORAGE(self)
                try:
                    try:
                        rename_directory_exclusive_at(
                            self._objects_fd, temporary.name, object_path.name
                        )
                        os.fsync(self._objects_fd)
                    except FileExistsError:
                        try:
                            existing_fd = _open_directory_at(
                                self._objects_fd, object_path.name
                            )
                        except OSError:
                            raise CatalogConflict("catalog object identity conflicts")
                        try:
                            existing_sha256, existing_manifest, _ = _copy_exact_bundle(
                                existing_fd, None
                            )
                        finally:
                            os.close(existing_fd)
                        if (
                            existing_sha256 != bundle_sha256
                            or existing_manifest != manifest_sha256
                        ):
                            raise CatalogConflict("catalog object identity conflicts")
                        _remove_tree(temporary)
                finally:
                    _RC_VALIDATE_STORAGE(self)
                _RC_FAULT(self, "after_object_publish")
                already_visible = False
                already_owned = False
                publication_id = ""
                with _RC_CONNECT(self) as connection:
                    connection.execute("BEGIN")
                    try:
                        existing = connection.execute(
                            """SELECT r.ref_json, a.display_alias, a.run_alias,
                                  a.timepoint_alias,
                                  EXISTS(SELECT 1 FROM coordinated_results c
                                         WHERE c.result_id=r.result_id),
                                  EXISTS(SELECT 1 FROM result_publications p
                                         WHERE p.result_id=r.result_id
                                         AND p.state='adopted')
                           FROM results r
                           LEFT JOIN opaque_aliases a ON a.result_id=r.result_id
                           WHERE r.result_id=? OR r.bundle_sha256=? OR r.bundle_record_id=?""",
                            (
                                reference.result_id,
                                reference.bundle_sha256,
                                reference.bundle_record_id,
                            ),
                        ).fetchone()
                        owner = None
                        if existing is not None:
                            owner = connection.execute(
                                """SELECT publication_id, state FROM result_publications
                               WHERE result_id=? AND recovery_scope_sha256=?""",
                                (reference.result_id, recovery_scope_sha256),
                            ).fetchone()
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                if existing is not None:
                    parsed = CatalogResultRef.model_validate_json(existing[0])
                    if parsed != reference or tuple(existing[1:4]) != (
                        aliases.display_alias,
                        aliases.run_alias,
                        aliases.timepoint_alias,
                    ):
                        raise CatalogConflict("catalog identity conflict")
                    already_visible = not bool(existing[4]) or bool(existing[5])
                    if owner is not None:
                        if owner[1] != "adopted":
                            raise CatalogConflict("catalog result publication is pending")
                        publication_id = owner[0]
                        already_owned = True
                if not publication_id:
                    seed = canonical_json_bytes(
                        {
                            "nonce": uuid.uuid4().hex,
                            "result_id": reference.result_id,
                            "authority": catalog_authority_sha256(authority),
                        }
                    )
                    publication_id = (
                        f"publication_{recovery_scope_sha256[:16]}_"
                        + hashlib.sha256(seed).hexdigest()
                    )
                prepared = PreparedCatalogImport(
                    publication_id=publication_id,
                    recovery_scope_sha256=recovery_scope_sha256,
                    reference=reference,
                    aliases=aliases,
                    authority=authority,
                    authority_sha256=catalog_authority_sha256(authority),
                    reader_id=reader.reader_id,
                    already_visible=already_visible,
                    already_owned=already_owned,
                )
                self._prepared_imports[publication_id] = prepared
                return prepared
        except BaseException:
            if temporary.exists():
                _remove_tree(temporary)
            raise

    def _require_prepared(
        self,
        prepared: PreparedCatalogImport | Mapping[str, object],
        *,
        require_authority: bool = True,
    ) -> PreparedCatalogImport:
        try:
            normalized = PreparedCatalogImport.model_validate_json(
                canonical_json_bytes(prepared)
            )
        except Exception:  # noqa: BLE001 - normalize hostile preparation input
            raise CatalogConflict("catalog preparation is invalid") from None
        if self._prepared_imports.get(normalized.publication_id) != normalized:
            raise CatalogConflict("catalog preparation is not live")
        if require_authority and _RC_AUTHORITY_SNAPSHOT(self) != normalized.authority:
            raise CatalogConflict("catalog authority changed during import")
        return normalized

    def stage_prepared_import(
        self, prepared: PreparedCatalogImport | Mapping[str, object]
    ) -> None:
        """Create a durable pending row that catalog queries cannot observe."""

        with (
            self._connection_lock,
            _RC_CONTENT_LOCK(self, exclusive=True),
            _RC_TRUST_FENCE(self),
        ):
            normalized = _RC_REQUIRE_PREPARED(self, prepared)
            if normalized.already_owned:
                return
            reference, aliases = normalized.reference, normalized.aliases
            with _RC_CONNECT(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = connection.execute(
                        """SELECT r.ref_json, a.display_alias, a.run_alias,
                                  a.timepoint_alias
                           FROM results r
                           LEFT JOIN opaque_aliases a ON a.result_id=r.result_id
                           WHERE r.result_id=? OR r.bundle_sha256=? OR r.bundle_record_id=?""",
                        (
                            reference.result_id,
                            reference.bundle_sha256,
                            reference.bundle_record_id,
                        ),
                    ).fetchone()
                    if existing is not None:
                        if CatalogResultRef.model_validate_json(
                            existing[0]
                        ) != reference or tuple(existing[1:4]) != (
                            aliases.display_alias,
                            aliases.run_alias,
                            aliases.timepoint_alias,
                        ):
                            raise CatalogConflict("catalog identity conflict")
                    else:
                        connection.execute(
                            "INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                reference.result_id,
                                reference.bundle_sha256,
                                reference.bundle_record_id,
                                reference.method_ref.method_id,
                                reference.method_ref.version,
                                reference.execution_state.value,
                                reference.information_state.value,
                                reference.trust_state.value,
                                reference.qualification_state.value,
                                canonical_json_bytes(reference),
                            ),
                        )
                        connection.execute(
                            "INSERT INTO opaque_aliases VALUES(?,?,?,?)",
                            (
                                reference.result_id,
                                aliases.display_alias,
                                aliases.run_alias,
                                aliases.timepoint_alias,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO coordinated_results VALUES(?)",
                            (reference.result_id,),
                        )
                    connection.execute(
                        "INSERT INTO result_publications VALUES(?,?,?,?)",
                        (
                            normalized.publication_id,
                            reference.result_id,
                            normalized.recovery_scope_sha256,
                            "pending",
                        ),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def adopt_prepared_import(
        self,
        prepared: PreparedCatalogImport | Mapping[str, object],
    ) -> CatalogResultRef:
        """Atomically make a verified pending row visible.

        Cross-catalog authorities must validate immediately before and after this
        call while holding their own publication lock. This API deliberately
        accepts no callback: caller code is never executed in the SQLite
        transaction.
        """

        with (
            self._connection_lock,
            _RC_CONTENT_LOCK(self, exclusive=True),
            _RC_TRUST_FENCE(self),
        ):
            normalized = _RC_REQUIRE_PREPARED(self, prepared)
            if normalized.already_owned:
                _RC_VERIFY_REFERENCE(self, normalized.reference)
                return normalized.reference
            _RC_VERIFY_PREPARED_OBJECT(self, normalized)
            with _RC_CONNECT(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        """SELECT r.ref_json, p.state FROM results r
                           JOIN result_publications p ON p.result_id=r.result_id
                           WHERE r.result_id=? AND p.publication_id=?""",
                        (normalized.reference.result_id, normalized.publication_id),
                    ).fetchone()
                    if (
                        row is None
                        or CatalogResultRef.model_validate_json(row[0])
                        != normalized.reference
                        or row[1] != "pending"
                    ):
                        raise CatalogConflict("pending catalog publication is invalid")
                    _RC_VERIFY_PREPARED_OBJECT(self, normalized)
                    connection.execute(
                        """UPDATE result_publications SET state='adopted'
                           WHERE result_id=? AND publication_id=?
                           AND recovery_scope_sha256=? AND state='pending'""",
                        (
                            normalized.reference.result_id,
                            normalized.publication_id,
                            normalized.recovery_scope_sha256,
                        ),
                    )
                    _RC_VERIFY_PREPARED_OBJECT(self, normalized)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return normalized.reference

    def finish_prepared_import(
        self, prepared: PreparedCatalogImport | Mapping[str, object]
    ) -> None:
        """Forget an adopted preparation after its coordinator completes."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            normalized = _RC_REQUIRE_PREPARED(self, prepared, require_authority=False)
            if not normalized.already_owned:
                with _RC_CONNECT(self) as connection:
                    row = connection.execute(
                        """SELECT state FROM result_publications
                           WHERE result_id=? AND publication_id=?""",
                        (
                            normalized.reference.result_id,
                            normalized.publication_id,
                        ),
                    ).fetchone()
                if row is None or row[0] != "adopted":
                    raise CatalogConflict("catalog publication is not adopted")
            self._prepared_imports.pop(normalized.publication_id, None)

    def compensate_prepared_import(
        self, prepared: PreparedCatalogImport | Mapping[str, object]
    ) -> None:
        """Remove this operation's exact pending or adopted row after coordinator failure."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            normalized = _RC_REQUIRE_PREPARED(self, prepared, require_authority=False)
            if not normalized.already_owned:
                with _RC_CONNECT(self) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute(
                            """SELECT r.ref_json FROM results r
                               JOIN result_publications p ON p.result_id=r.result_id
                               WHERE r.result_id=? AND p.publication_id=?""",
                            (
                                normalized.reference.result_id,
                                normalized.publication_id,
                            ),
                        ).fetchone()
                        if row is not None:
                            if (
                                CatalogResultRef.model_validate_json(row[0])
                                != normalized.reference
                            ):
                                raise CatalogConflict(
                                    "catalog publication compensation conflicts"
                                )
                            _RC_REMOVE_OWNER(
                                connection,
                                normalized.publication_id,
                                normalized.reference.result_id,
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
            self._prepared_imports.pop(normalized.publication_id, None)

    def verify_prepared_object(
        self, prepared: PreparedCatalogImport | Mapping[str, object]
    ) -> tuple[VerifiedBundle, ResultBundleReader]:
        with _RC_TRUST_FENCE(self) as (trust_store, _):
            normalized = _RC_REQUIRE_PREPARED(self, prepared)
            reference = normalized.reference
            try:
                object_fd = _open_directory_at(self._objects_fd, reference.bundle_sha256)
            except OSError:
                raise CatalogFilesystemError("catalog object is unavailable") from None
            try:
                observed_sha256, manifest_sha256, _ = _copy_exact_bundle(object_fd, None)
                if (
                    observed_sha256 != reference.bundle_sha256
                    or manifest_sha256 != reference.bundle_manifest_sha256
                ):
                    raise CatalogConflict("catalog object identity conflicts")
                verified = _PINNED_VERIFY_BUNDLE(
                    _descriptor_path(object_fd), trust_store
                )
                reader = _PINNED_READER_SELECT(self.reader_registry, verified)
                if reader.reader_id != normalized.reader_id:
                    raise CatalogConflict("catalog reader changed during import")
                return verified, reader
            finally:
                os.close(object_fd)

    def discard_prepared_import(
        self, prepared: PreparedCatalogImport | Mapping[str, object]
    ) -> None:
        """Remove only this operation's invisible pending row; retain shared object bytes."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            normalized = _RC_REQUIRE_PREPARED(self, prepared, require_authority=False)
            if not normalized.already_owned:
                with _RC_CONNECT(self) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute(
                            """SELECT state FROM result_publications
                               WHERE result_id=? AND publication_id=?""",
                            (
                                normalized.reference.result_id,
                                normalized.publication_id,
                            ),
                        ).fetchone()
                        if row is not None:
                            if row[0] != "pending":
                                raise CatalogConflict(
                                    "adopted catalog publication cannot be discarded"
                                )
                            _RC_REMOVE_OWNER(
                                connection,
                                normalized.publication_id,
                                normalized.reference.result_id,
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
            self._prepared_imports.pop(normalized.publication_id, None)

    def register_coordinated_candidate(
        self,
        prepared: PreparedCatalogImport,
        candidate: CoordinatedCatalogCandidate,
    ) -> None:
        """Durably bind one exact coordinator attempt before filesystem visibility."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            if (
                type(prepared) is not PreparedCatalogImport
                or type(candidate) is not CoordinatedCatalogCandidate
            ):
                raise CatalogConflict("catalog candidate is invalid")
            normalized = _RC_REQUIRE_PREPARED(self, prepared)
            if (
                candidate.publication_id != normalized.publication_id
                or candidate.result_id != normalized.reference.result_id
                or candidate.recovery_scope_sha256 != normalized.recovery_scope_sha256
            ):
                raise CatalogConflict("catalog candidate conflicts with preparation")
            encoded = canonical_json_bytes(candidate)
            with _RC_CONNECT(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT candidate_json FROM coordinated_candidates WHERE operation_id=?",
                        (candidate.operation_id,),
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            "INSERT INTO coordinated_candidates VALUES(?,?,?,?,?)",
                            (
                                candidate.operation_id,
                                candidate.publication_id,
                                candidate.result_id,
                                candidate.recovery_scope_sha256,
                                encoded,
                            ),
                        )
                    elif bytes(row[0]) != encoded:
                        raise CatalogConflict("catalog candidate identity conflicts")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def coordinated_candidates(
        self, recovery_scope_sha256: str
    ) -> tuple[CoordinatedCatalogCandidate, ...]:
        """Enumerate bounded durable incomplete attempts for one coordinator root."""

        if type(recovery_scope_sha256) is not str:
            raise CatalogConflict("catalog recovery scope is invalid")
        try:
            scope = TypeAdapter(Sha256).validate_python(recovery_scope_sha256)
        except Exception:  # noqa: BLE001 - normalize hostile scope
            raise CatalogConflict("catalog recovery scope is invalid") from None
        with _RC_CONNECT(self) as connection:
            rows = connection.execute(
                """SELECT candidate_json FROM coordinated_candidates
                   WHERE recovery_scope_sha256=? ORDER BY operation_id LIMIT ?""",
                (scope, MAX_QUERY_LIMIT * 1000 + 1),
            ).fetchall()
        if len(rows) > MAX_QUERY_LIMIT * 1000:
            raise CatalogConflict("catalog candidate recovery bound exceeded")
        try:
            return tuple(
                CoordinatedCatalogCandidate.model_validate_json(row[0]) for row in rows
            )
        except Exception:  # noqa: BLE001 - normalize hostile database content
            raise CatalogConflict("catalog candidate is invalid") from None

    def finish_coordinated_candidate(
        self, candidate: CoordinatedCatalogCandidate
    ) -> None:
        """Remove the exact candidate row; this is the durable commit point."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            if type(candidate) is not CoordinatedCatalogCandidate:
                raise CatalogConflict("catalog candidate is invalid")
            encoded = canonical_json_bytes(candidate)
            with _RC_CONNECT(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT candidate_json FROM coordinated_candidates WHERE operation_id=?",
                        (candidate.operation_id,),
                    ).fetchone()
                    if row is not None:
                        if bytes(row[0]) != encoded:
                            raise CatalogConflict("catalog candidate identity conflicts")
                        connection.execute(
                            "DELETE FROM coordinated_candidates WHERE operation_id=?",
                            (candidate.operation_id,),
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def recover_pending_publication(
        self,
        *,
        publication_id: str,
        reference: CatalogResultRef,
        recovery_scope_sha256: str,
        retain_adopted: bool = True,
    ) -> Literal["absent", "pending_removed", "adopted"]:
        """Idempotently remove an exact pending publication after process recovery."""

        with self._connection_lock, _RC_CONTENT_LOCK(self, exclusive=True):
            if type(publication_id) is not str or type(recovery_scope_sha256) is not str:
                raise CatalogConflict("catalog publication identity is invalid")
            if type(retain_adopted) is not bool:
                raise CatalogConflict("catalog recovery disposition is invalid")
            if type(reference) is not CatalogResultRef:
                raise CatalogConflict("catalog reference is invalid")
            reference = CatalogResultRef.model_validate_json(
                canonical_json_bytes(reference)
            )
            try:
                scope = TypeAdapter(Sha256).validate_python(recovery_scope_sha256)
            except Exception:  # noqa: BLE001 - normalize hostile recovery scope
                raise CatalogConflict("catalog recovery scope is invalid") from None
            if not publication_id.startswith(f"publication_{scope[:16]}_"):
                raise CatalogConflict("catalog publication recovery scope conflicts")
            with _RC_CONNECT(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        """SELECT r.ref_json, p.state FROM results r
                           JOIN result_publications p ON p.result_id=r.result_id
                           WHERE r.result_id=? AND p.publication_id=?
                           AND p.recovery_scope_sha256=?""",
                        (reference.result_id, publication_id, scope),
                    ).fetchone()
                    if row is None:
                        connection.commit()
                        return "absent"
                    if CatalogResultRef.model_validate_json(row[0]) != reference:
                        raise CatalogConflict("recovered catalog publication conflicts")
                    if row[1] == "adopted" and retain_adopted:
                        connection.commit()
                        return "adopted"
                    _RC_REMOVE_OWNER(connection, publication_id, reference.result_id)
                    connection.commit()
                    return "pending_removed"
                except BaseException:
                    connection.rollback()
                    raise

    def pending_publications(
        self, recovery_scope_sha256: str
    ) -> tuple[PendingCatalogPublication, ...]:
        """Enumerate bounded hidden rows so a coordinator can recover without a journal."""

        if type(recovery_scope_sha256) is not str:
            raise CatalogConflict("catalog recovery scope is invalid")
        try:
            scope = TypeAdapter(Sha256).validate_python(recovery_scope_sha256)
        except Exception:  # noqa: BLE001 - normalize hostile recovery scope
            raise CatalogConflict("catalog recovery scope is invalid") from None
        prefix = f"publication_{scope[:16]}_%"
        with _RC_CONNECT(self) as connection:
            rows = connection.execute(
                """SELECT p.publication_id, r.ref_json
                   FROM result_publications p
                   JOIN results r ON r.result_id=p.result_id
                   WHERE p.state='pending' AND p.publication_id LIKE ?
                   AND p.recovery_scope_sha256=?
                   ORDER BY p.publication_id
                   LIMIT ?""",
                (prefix, scope, MAX_QUERY_LIMIT * 1000 + 1),
            ).fetchall()
        if len(rows) > MAX_QUERY_LIMIT * 1000:
            raise CatalogConflict("pending catalog publication bound exceeded")
        try:
            return tuple(
                PendingCatalogPublication(
                    publication_id=row[0],
                    reference=CatalogResultRef.model_validate_json(row[1]),
                    state="pending",
                )
                for row in rows
            )
        except Exception:  # noqa: BLE001 - normalize hostile database content
            raise CatalogConflict("pending catalog publication is invalid") from None

    def recovery_publications(
        self, recovery_scope_sha256: str
    ) -> tuple[PendingCatalogPublication, ...]:
        """Enumerate bounded coordinator-owned publication rows for reconciliation."""

        if type(recovery_scope_sha256) is not str:
            raise CatalogConflict("catalog recovery scope is invalid")
        try:
            scope = TypeAdapter(Sha256).validate_python(recovery_scope_sha256)
        except Exception:  # noqa: BLE001 - normalize hostile recovery scope
            raise CatalogConflict("catalog recovery scope is invalid") from None
        prefix = f"publication_{scope[:16]}_%"
        with _RC_CONNECT(self) as connection:
            rows = connection.execute(
                """SELECT p.publication_id, r.ref_json, p.state
                   FROM result_publications p
                   JOIN results r ON r.result_id=p.result_id
                   WHERE p.publication_id LIKE ? AND p.recovery_scope_sha256=?
                   ORDER BY p.publication_id
                   LIMIT ?""",
                (prefix, scope, MAX_QUERY_LIMIT * 1000 + 1),
            ).fetchall()
        if len(rows) > MAX_QUERY_LIMIT * 1000:
            raise CatalogConflict("catalog publication recovery bound exceeded")
        try:
            return tuple(
                PendingCatalogPublication(
                    publication_id=row[0],
                    reference=CatalogResultRef.model_validate_json(row[1]),
                    state=row[2],
                )
                for row in rows
            )
        except Exception:  # noqa: BLE001 - normalize hostile database content
            raise CatalogConflict("catalog publication recovery is invalid") from None

    def publication_for_recovery(
        self, publication_id: str, recovery_scope_sha256: str
    ) -> PendingCatalogPublication | None:
        """Resolve one durable publication identity without trusting journal bytes."""

        if type(publication_id) is not str or type(recovery_scope_sha256) is not str:
            raise CatalogConflict("catalog publication identity is invalid")
        try:
            publication_id = TypeAdapter(PublicationId).validate_python(publication_id)
            scope = TypeAdapter(Sha256).validate_python(recovery_scope_sha256)
        except Exception:  # noqa: BLE001 - normalize hostile recovery key
            raise CatalogConflict("catalog publication identity is invalid") from None
        if not publication_id.startswith(f"publication_{scope[:16]}_"):
            raise CatalogConflict("catalog publication recovery scope conflicts")
        with _RC_CONNECT(self) as connection:
            row = connection.execute(
                """SELECT r.ref_json, p.state FROM result_publications p
                   JOIN results r ON r.result_id=p.result_id
                   WHERE p.publication_id=? AND p.recovery_scope_sha256=?""",
                (publication_id, scope),
            ).fetchone()
        if row is None:
            return None
        try:
            return PendingCatalogPublication(
                publication_id=publication_id,
                reference=CatalogResultRef.model_validate_json(row[0]),
                state=row[1],
            )
        except Exception:  # noqa: BLE001 - normalize hostile database content
            raise CatalogConflict("catalog publication is invalid") from None

    def verify_reference(
        self, reference: CatalogResultRef | Mapping[str, object]
    ) -> tuple[VerifiedBundle, ResultBundleReader]:
        """Reverify one indexed immutable object against current offline trust."""

        with _RC_TRUST_FENCE(self) as (trust_store, _):
            try:
                normalized = CatalogResultRef.model_validate_json(
                    canonical_json_bytes(reference)
                )
            except Exception:  # noqa: BLE001 - normalize hostile reference input
                raise CatalogConflict("catalog reference is invalid") from None
            with _RC_CONNECT(self) as connection:
                row = connection.execute(
                    """SELECT r.ref_json FROM results r WHERE r.result_id=? AND
                       (NOT EXISTS(SELECT 1 FROM coordinated_results c
                                   WHERE c.result_id=r.result_id)
                        OR EXISTS(SELECT 1 FROM result_publications p
                                  WHERE p.result_id=r.result_id AND p.state='adopted'))""",
                    (normalized.result_id,),
                ).fetchone()
            if row is None or CatalogResultRef.model_validate_json(row[0]) != normalized:
                raise CatalogConflict("catalog reference is not indexed exactly")
            _RC_VALIDATE_STORAGE(self)
            try:
                object_fd = _open_directory_at(self._objects_fd, normalized.bundle_sha256)
            except OSError:
                raise CatalogFilesystemError("catalog object is unavailable") from None
            try:
                observed_sha256, manifest_sha256, _ = _copy_exact_bundle(object_fd, None)
                if (
                    observed_sha256 != normalized.bundle_sha256
                    or manifest_sha256 != normalized.bundle_manifest_sha256
                ):
                    raise CatalogConflict("catalog object identity conflicts")
                _RC_VALIDATE_VERIFICATION_AUTHORITY(self)
                verified = _PINNED_VERIFY_BUNDLE(
                    _descriptor_path(object_fd), trust_store
                )
                reader = _PINNED_READER_SELECT(self.reader_registry, verified)
                manifest = verified.manifest
                if not isinstance(manifest, ResultBundleManifestV2):
                    raise CatalogUnsupportedSchema("bundle schema is unsupported")
                if (
                    manifest.record_id != normalized.bundle_record_id
                    or manifest.workflow_release_id != normalized.workflow_release_id
                    or manifest.method.method_id != normalized.method_ref.method_id
                    or manifest.method.version != normalized.method_ref.version
                    or manifest.method.method_definition_sha256
                    != normalized.method_definition_sha256
                ):
                    raise CatalogConflict("catalog reference conflicts with bundle")
                return verified, reader
            finally:
                os.close(object_fd)

    @staticmethod
    def _reference(
        verified: VerifiedBundle,
        *,
        bundle_sha256: str,
        manifest_sha256: str,
        capability: CurrentMethodCapability,
    ) -> CatalogResultRef:
        identity = hashlib.sha256(
            canonical_json_bytes(
                {
                    "bundle_sha256": bundle_sha256,
                    "method_definition_sha256": capability.method_definition_sha256,
                }
            )
        ).hexdigest()
        return CatalogResultRef(
            result_id=f"result_{identity[:40]}",
            bundle_sha256=bundle_sha256,
            bundle_record_id=verified.manifest.record_id,
            bundle_manifest_sha256=manifest_sha256,
            workflow_release_id=verified.manifest.workflow_release_id,
            method_ref=capability.method_ref,
            method_definition_sha256=capability.method_definition_sha256,
            registry_sha256=capability.registry_sha256,
            registry_version=capability.registry_version,
            authority_head_sha256=capability.authority_head_sha256,
            authority_revision=capability.authority_revision,
            authority_scope=capability.authority_scope,
            capability_as_of=capability.as_of,
            qualification_state=_qualification(capability.qualification_state),
            display_role=capability.display_role,
            research_inspectable=capability.research_inspectable,
            current_provider_eligible=capability.current_provider_eligible,
        )

    def query(self, query: CatalogQuery | Mapping[str, object]) -> CatalogPage:
        normalized = (
            query
            if isinstance(query, CatalogQuery)
            else CatalogQuery.model_validate(query)
        )
        clauses: list[str] = [
            """(NOT EXISTS (SELECT 1 FROM coordinated_results c
            WHERE c.result_id=r.result_id) OR EXISTS
            (SELECT 1 FROM result_publications p WHERE p.result_id=r.result_id
            AND p.state='adopted'))"""
        ]
        parameters: list[object] = []
        joins = ""
        if any(
            (
                normalized.display_alias,
                normalized.run_alias,
                normalized.timepoint_alias,
            )
        ):
            joins = " JOIN opaque_aliases a ON a.result_id=r.result_id"
            for column, value in (
                ("display_alias", normalized.display_alias),
                ("run_alias", normalized.run_alias),
                ("timepoint_alias", normalized.timepoint_alias),
            ):
                if value is not None:
                    clauses.append(f"a.{column}=?")
                    parameters.append(value)
        _RC_APPEND_METHOD_FILTER(clauses, parameters, normalized.method_refs)
        for column, values in (
            ("execution_state", normalized.execution_states),
            ("information_state", normalized.information_states),
            ("trust_state", normalized.trust_states),
            ("qualification_state", normalized.qualification_states),
        ):
            if values:
                clauses.append(f"r.{column} IN ({','.join('?' for _ in values)})")
                parameters.extend(item.value for item in values)
        direction = "ASC" if normalized.order == CatalogOrder.RESULT_ID_ASC else "DESC"
        if normalized.cursor is not None:
            clauses.append(f"r.result_id {'>' if direction == 'ASC' else '<'} ?")
            parameters.append(normalized.cursor)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT r.ref_json FROM results r{joins}{where} "
            f"ORDER BY r.result_id {direction} LIMIT ?"
        )
        parameters.append(normalized.limit + 1)
        with _RC_CONNECT(self) as connection:
            rows = connection.execute(sql, parameters).fetchall()
            catalog_has_results = bool(rows)
            if not rows:
                catalog_has_results = (
                    connection.execute(
                        """SELECT 1 FROM results r WHERE
                           NOT EXISTS(SELECT 1 FROM coordinated_results c
                                      WHERE c.result_id=r.result_id)
                           OR EXISTS(SELECT 1 FROM result_publications p
                                     WHERE p.result_id=r.result_id
                                     AND p.state='adopted') LIMIT 1"""
                    ).fetchone()
                    is not None
                )
        _RC_VALIDATE_STORAGE(self)
        has_more = len(rows) > normalized.limit
        selected = rows[: normalized.limit]
        results = tuple(
            CatalogResultRef.model_validate_json(row[0]) for row in selected
        )
        return CatalogPage(
            results=results,
            next_cursor=results[-1].result_id if has_more and results else None,
            empty=not results,
            empty_reason=(
                CatalogEmptyReason.NO_IMPORTED_RESULTS
                if not results and not catalog_has_results
                else CatalogEmptyReason.NO_MATCHES
                if not results
                else None
            ),
        )

    def get_verified(
        self,
        result_id: str,
        context: CatalogVerificationContext,
    ) -> CatalogResultRef:
        """Reload and re-verify storage, bundle trust, and live authority."""

        return bind_catalog_live_reader(self).get_verified(result_id, context)

    @staticmethod
    def _remove_publication_owner(
        connection: sqlite3.Connection, publication_id: str, result_id: str
    ) -> None:
        """Remove one coordinator owner and only delete its last coordinated row."""

        connection.execute(
            "DELETE FROM result_publications WHERE publication_id=? AND result_id=?",
            (publication_id, result_id),
        )
        coordinated = connection.execute(
            "SELECT 1 FROM coordinated_results WHERE result_id=?", (result_id,)
        ).fetchone()
        owners = connection.execute(
            "SELECT 1 FROM result_publications WHERE result_id=? LIMIT 1", (result_id,)
        ).fetchone()
        if coordinated is not None and owners is None:
            connection.execute(
                "DELETE FROM opaque_aliases WHERE result_id=?", (result_id,)
            )
            connection.execute("DELETE FROM results WHERE result_id=?", (result_id,))

    @staticmethod
    def _append_method_filter(
        clauses: list[str],
        parameters: list[object],
        methods: Sequence[MethodReference],
    ) -> None:
        if not methods:
            return
        clauses.append(
            "("
            + " OR ".join("(r.method_id=? AND r.method_version=?)" for _ in methods)
            + ")"
        )
        for method in methods:
            parameters.extend((method.method_id, method.version))


_RESULT_METHOD_SEAL = MappingProxyType(
    {
        name: ResultCatalog.__dict__[name]
        for name in (
            "_append_method_filter",
            "_bind_database_descriptor",
            "_capture",
            "_connect",
            "_fault",
            "_open_sqlite_connection",
            "_reference",
            "_remove_publication_owner",
            "_require_prepared",
            "_trust_fence",
            "_validate_storage",
            "_validate_verification_authority",
            "adopt_prepared_import",
            "authority_snapshot",
            "compensate_prepared_import",
            "content_authority_fence",
            "content_head_in_fence",
            "content_snapshot",
            "_content_lock",
            "_content_sha256_locked",
            "_open_content_lock",
            "coordinated_candidates",
            "finish_prepared_import",
            "finish_coordinated_candidate",
            "pending_publications",
            "prepare_bundle_import",
            "publication_for_recovery",
            "query",
            "register_coordinated_candidate",
            "recover_pending_publication",
            "recovery_publications",
            "stage_prepared_import",
            "trust_authority_fence",
            "verify_prepared_object",
            "verify_reference",
        )
    }
)


def _authority_value_fingerprint(value: object, seen: set[int] | None = None) -> object:
    """Snapshot Python callable internals as a tamper diagnostic.

    This detects accidental/runtime monkeypatching inside an otherwise trusted
    process. Package/process integrity remains the authorization boundary.
    """

    if seen is None:
        seen = set()
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return (type(value).__qualname__, value)
    identity = id(value)
    if identity in seen:
        return ("cycle", identity)
    seen.add(identity)
    if isinstance(value, tuple):
        return ("tuple", tuple(_authority_value_fingerprint(v, seen) for v in value))
    if isinstance(value, list):
        return ("list", tuple(_authority_value_fingerprint(v, seen) for v in value))
    if isinstance(value, dict):
        return (
            "dict",
            tuple(
                sorted(
                    (
                        repr(key),
                        _authority_value_fingerprint(item, seen),
                    )
                    for key, item in value.items()
                )
            ),
        )
    code = getattr(value, "__code__", None)
    if code is not None:
        closure = getattr(value, "__closure__", None) or ()
        cells: list[object] = []
        for cell in closure:
            try:
                contents = cell.cell_contents
            except ValueError:
                contents = "<empty>"
            cells.append((id(cell), _authority_value_fingerprint(contents, seen)))
        code_material = repr(
            (
                code.co_code,
                code.co_consts,
                code.co_names,
                code.co_varnames,
                code.co_freevars,
                code.co_cellvars,
                code.co_argcount,
                code.co_posonlyargcount,
                code.co_kwonlyargcount,
                code.co_flags,
            )
        ).encode("utf-8")
        return (
            "callable",
            identity,
            hashlib.sha256(code_material).hexdigest(),
            _authority_value_fingerprint(getattr(value, "__defaults__", None), seen),
            _authority_value_fingerprint(getattr(value, "__kwdefaults__", None), seen),
            tuple(cells),
        )
    return (type(value).__module__, type(value).__qualname__, identity, repr(value))


_RESULT_METHOD_FINGERPRINTS = MappingProxyType(
    {
        name: _authority_value_fingerprint(value)
        for name, value in _RESULT_METHOD_SEAL.items()
    }
)
_RESULT_PINNED_FINGERPRINTS = MappingProxyType(
    {
        "verify_bundle": _authority_value_fingerprint(_PINNED_VERIFY_BUNDLE),
        "reader_select": _authority_value_fingerprint(_PINNED_READER_SELECT),
        "trust_resolve": _authority_value_fingerprint(_PINNED_TRUST_RESOLVE),
        "trust_read_fence": _authority_value_fingerprint(_PINNED_TRUST_READ_FENCE),
        "load_trust": _authority_value_fingerprint(_PINNED_LOAD_TRUST),
        "trust_document_bytes": _authority_value_fingerprint(
            _PINNED_TRUST_DOCUMENT_BYTES
        ),
    }
)


def _assert_result_runtime(
    catalog: ResultCatalog,
    *,
    expected_methods: Mapping[str, object] = _RESULT_METHOD_SEAL,
    expected_verify_bundle: object = _PINNED_VERIFY_BUNDLE,
    expected_reader_select: object = _PINNED_READER_SELECT,
    expected_trust_resolve: object = _PINNED_TRUST_RESOLVE,
) -> None:
    """Reject instance, class, or module replacement in the trust call chain."""

    if type(catalog) is not ResultCatalog:
        raise CatalogError("catalog authority type changed")
    for name, expected in expected_methods.items():
        current = ResultCatalog.__dict__.get(name)
        if (
            name in vars(catalog)
            or current is not expected
            or _authority_value_fingerprint(current)
            != _RESULT_METHOD_FINGERPRINTS[name]
        ):
            raise CatalogError(f"catalog authority callable changed: {name}")
    for name, expected in _RESULT_ALIAS_SEAL.items():
        current = globals().get(name)
        if (
            current is not expected
            or _authority_value_fingerprint(current) != _RESULT_ALIAS_FINGERPRINTS[name]
        ):
            raise CatalogError("catalog module authority changed")
    if (
        globals().get("_PINNED_VERIFY_BUNDLE") is not expected_verify_bundle
        or globals().get("_PINNED_READER_SELECT") is not expected_reader_select
        or globals().get("_PINNED_TRUST_RESOLVE") is not expected_trust_resolve
        or TrustStore.resolve is not expected_trust_resolve
        or (
            catalog.trust_store is not None and "resolve" in vars(catalog.trust_store)
        )
        or globals().get("_PINNED_TRUST_READ_FENCE") is not _PINNED_TRUST_READ_FENCE_SEAL
        or ResultTrustRegistry.read_fence is not _PINNED_TRUST_READ_FENCE_SEAL
        or globals().get("_PINNED_LOAD_TRUST") is not _PINNED_LOAD_TRUST_SEAL
        or globals().get("_PINNED_TRUST_DOCUMENT_BYTES")
        is not _PINNED_TRUST_DOCUMENT_BYTES_SEAL
        or _authority_value_fingerprint(_PINNED_TRUST_READ_FENCE)
        != _RESULT_PINNED_FINGERPRINTS["trust_read_fence"]
        or _authority_value_fingerprint(_PINNED_LOAD_TRUST)
        != _RESULT_PINNED_FINGERPRINTS["load_trust"]
        or _authority_value_fingerprint(_PINNED_TRUST_DOCUMENT_BYTES)
        != _RESULT_PINNED_FINGERPRINTS["trust_document_bytes"]
        or _authority_value_fingerprint(_PINNED_VERIFY_BUNDLE)
        != _RESULT_PINNED_FINGERPRINTS["verify_bundle"]
        or _authority_value_fingerprint(_PINNED_READER_SELECT)
        != _RESULT_PINNED_FINGERPRINTS["reader_select"]
        or _authority_value_fingerprint(TrustStore.resolve)
        != _RESULT_PINNED_FINGERPRINTS["trust_resolve"]
    ):
        raise CatalogError("catalog verification authority changed")


_RC_ASSERT_RUNTIME = _assert_result_runtime
_RC_APPEND_METHOD_FILTER = ResultCatalog._append_method_filter
_RC_AUTHORITY_SNAPSHOT = ResultCatalog.authority_snapshot
_RC_BIND_DATABASE_DESCRIPTOR = ResultCatalog._bind_database_descriptor
_RC_CAPTURE = ResultCatalog._capture
_RC_CONNECT = ResultCatalog._connect
_RC_CONTENT_LOCK = ResultCatalog._content_lock
_RC_CONTENT_SHA256_LOCKED = ResultCatalog._content_sha256_locked
_RC_CONTENT_HEAD_IN_FENCE = ResultCatalog.content_head_in_fence
_RC_FAULT = ResultCatalog._fault
_RC_OPEN_SQLITE_CONNECTION = ResultCatalog._open_sqlite_connection
_RC_REFERENCE = ResultCatalog._reference
_RC_REMOVE_OWNER = ResultCatalog._remove_publication_owner
_RC_REQUIRE_PREPARED = ResultCatalog._require_prepared
_RC_TRUST_FENCE = ResultCatalog._trust_fence
_RC_VALIDATE_STORAGE = ResultCatalog._validate_storage
_RC_VALIDATE_VERIFICATION_AUTHORITY = ResultCatalog._validate_verification_authority
_RC_VERIFY_PREPARED_OBJECT = ResultCatalog.verify_prepared_object
_RC_VERIFY_REFERENCE = ResultCatalog.verify_reference
_RESULT_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_RC_APPEND_METHOD_FILTER",
            "_RC_AUTHORITY_SNAPSHOT",
            "_RC_BIND_DATABASE_DESCRIPTOR",
            "_RC_CAPTURE",
            "_RC_CONNECT",
            "_RC_CONTENT_LOCK",
            "_RC_CONTENT_SHA256_LOCKED",
            "_RC_CONTENT_HEAD_IN_FENCE",
            "_RC_FAULT",
            "_RC_OPEN_SQLITE_CONNECTION",
            "_RC_REFERENCE",
            "_RC_REMOVE_OWNER",
            "_RC_REQUIRE_PREPARED",
            "_RC_TRUST_FENCE",
            "_RC_VALIDATE_STORAGE",
            "_RC_VALIDATE_VERIFICATION_AUTHORITY",
            "_RC_VERIFY_PREPARED_OBJECT",
            "_RC_VERIFY_REFERENCE",
        )
    }
)
_RESULT_ALIAS_FINGERPRINTS = MappingProxyType(
    {
        name: _authority_value_fingerprint(value)
        for name, value in _RESULT_ALIAS_SEAL.items()
    }
)


_CATALOG_VALIDATE_STORAGE = ResultCatalog._validate_storage
_CATALOG_QUERY = ResultCatalog.query
_CATALOG_REFERENCE = ResultCatalog._reference
_CATALOG_FAULT = ResultCatalog._fault
_CATALOG_TRUST_FENCE = ResultCatalog._trust_fence
_VERIFY_CATALOG_BUNDLE = verify_bundle
_CATALOG_PROTECTED_NAMES = (
    "get_verified",
    "_connect",
    "_trust_fence",
    "_validate_storage",
    "_open_sqlite_connection",
    "_reference",
    "_bound_objects",
)
_CATALOG_CLASS_IDENTITIES = {
    name: ResultCatalog.__dict__[name] for name in _CATALOG_PROTECTED_NAMES
}


def _assert_live_catalog_reader(reader: CatalogLiveReader) -> None:
    catalog = reader._catalog
    if type(catalog) is not ResultCatalog:
        raise CatalogFilesystemError("catalog reader identity changed")
    if any(name in catalog.__dict__ for name in _CATALOG_PROTECTED_NAMES):
        raise CatalogFilesystemError("catalog verification method is shadowed")
    if any(
        ResultCatalog.__dict__.get(name) is not expected
        for name, expected in _CATALOG_CLASS_IDENTITIES.items()
    ):
        raise CatalogFilesystemError("catalog verification class changed")
    if (
        globals().get("_assert_live_catalog_reader") is not reader._assert_live
        or _CATALOG_VALIDATE_STORAGE is not reader._validate_storage
        or _CATALOG_QUERY is not reader._query_catalog
        or _CATALOG_REFERENCE is not reader._reference_verified
        or _CATALOG_FAULT is not reader._fault_catalog
        or _VERIFY_CATALOG_BUNDLE is not reader._verify_bundle
        or _capability_is_revoked is not reader._capability_revoked
        or canonical_json_bytes is not reader._canonicalize
        or _descriptor_path is not reader._descriptor_resolver
        or replay_current_capability is not reader._replay_capability
        or catalog.root != reader._root_path
        or catalog.objects != reader._objects_path
        or catalog.database != reader._database_path
        or catalog._root_identity != reader._root_identity
        or catalog._objects_identity != reader._objects_identity
        or catalog._database_identity != reader._database_identity
        or catalog._root_fd != reader._root_fd
        or catalog._objects_fd != reader._objects_fd
        or catalog._database_fd != reader._database_fd
        or catalog._sqlite_database_fd != reader._sqlite_database_fd
        or _CATALOG_TRUST_FENCE is not reader._trust_fence_catalog
        or catalog._connection_lock is not reader._connection_lock
        or catalog.trust_store is not reader._trust_store
        or catalog.result_trust_registry is not reader._trust_registry
        or catalog._result_trust_identity != reader._trust_registry_identity
        or (reader._trust_store is None) == (reader._trust_registry is None)
        or (
            reader._trust_store is not None
            and (
                id(catalog.trust_store._keys) != reader._trust_keys_identity
                or tuple(sorted(catalog.trust_store._keys.items()))
                != reader._trust_snapshot
            )
        )
        or catalog._connection is not reader._connection
        or reader._connection is None
    ):
        raise CatalogFilesystemError("catalog reader binding changed")
    reader._validate_storage(catalog)


class CatalogLiveReader:
    """Sealed reader over one exact, open ResultCatalog installation."""

    __slots__ = (
        "_assert_live",
        "_canonicalize",
        "_capability_revoked",
        "_catalog",
        "_connection",
        "_connection_lock",
        "_database_fd",
        "_database_identity",
        "_database_path",
        "_descriptor_resolver",
        "_fault_catalog",
        "_objects_fd",
        "_objects_identity",
        "_objects_path",
        "_query_catalog",
        "_reference_verified",
        "_replay_capability",
        "_result_ref_from_json",
        "_root_fd",
        "_root_identity",
        "_root_path",
        "_sealed",
        "_sqlite_database_fd",
        "_trust_fence_catalog",
        "_trust_keys_identity",
        "_trust_registry",
        "_trust_registry_identity",
        "_trust_snapshot",
        "_trust_store",
        "_validate_storage",
        "_verification_context_from_json",
        "_verify_bundle",
    )

    def __init__(
        self,
        catalog: ResultCatalog,
        *,
        _assert_live: Callable[[CatalogLiveReader], None] = _assert_live_catalog_reader,
        _canonicalize: Callable[[object], bytes] = canonical_json_bytes,
        _capability_revoked: Callable[
            [MethodRegistry, CurrentMethodCapability], bool
        ] = _capability_is_revoked,
        _descriptor_resolver: Callable[[int], Path] = _descriptor_path,
        _fault_catalog: Callable[[ResultCatalog, str], None] = _CATALOG_FAULT,
        _query_catalog: Callable[[ResultCatalog, CatalogQuery], CatalogPage] = (
            _CATALOG_QUERY
        ),
        _reference_verified: Callable[..., CatalogResultRef] = _CATALOG_REFERENCE,
        _replay_capability: Callable[..., None] = replay_current_capability,
        _trust_fence_catalog: Callable[..., object] = _CATALOG_TRUST_FENCE,
        _result_ref_from_json: Callable[..., CatalogResultRef] = (
            CatalogResultRef.model_validate_json
        ),
        _validate_storage: Callable[[ResultCatalog], None] = _CATALOG_VALIDATE_STORAGE,
        _verification_context_from_json: Callable[..., CatalogVerificationContext] = (
            CatalogVerificationContext.model_validate_json
        ),
        _verify_bundle: Callable[[Path, TrustStore], VerifiedBundle] = (
            _VERIFY_CATALOG_BUNDLE
        ),
    ) -> None:
        if type(catalog) is not ResultCatalog:
            raise TypeError("live catalog reader requires an exact ResultCatalog")
        self._assert_live = _assert_live
        self._canonicalize = _canonicalize
        self._catalog = catalog
        self._capability_revoked = _capability_revoked
        self._root_path = catalog.root
        self._objects_path = catalog.objects
        self._database_path = catalog.database
        self._root_identity = catalog._root_identity
        self._objects_identity = catalog._objects_identity
        self._database_identity = catalog._database_identity
        self._descriptor_resolver = _descriptor_resolver
        self._fault_catalog = _fault_catalog
        self._root_fd = catalog._root_fd
        self._objects_fd = catalog._objects_fd
        self._query_catalog = _query_catalog
        self._reference_verified = _reference_verified
        self._replay_capability = _replay_capability
        self._result_ref_from_json = _result_ref_from_json
        self._database_fd = catalog._database_fd
        self._sqlite_database_fd = catalog._sqlite_database_fd
        self._connection_lock = catalog._connection_lock
        self._trust_fence_catalog = _trust_fence_catalog
        self._trust_store = catalog.trust_store
        self._trust_registry = catalog.result_trust_registry
        self._trust_registry_identity = catalog._result_trust_identity
        # TrustStore path: freeze the exact key map; a registry is read live.
        if catalog.trust_store is not None:
            self._trust_keys_identity = id(catalog.trust_store._keys)
            self._trust_snapshot = tuple(sorted(catalog.trust_store._keys.items()))
        else:
            self._trust_keys_identity = None
            self._trust_snapshot = None
        self._connection = catalog._connection
        self._validate_storage = _validate_storage
        self._verification_context_from_json = _verification_context_from_json
        self._verify_bundle = _verify_bundle
        self._assert_live(self)
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("catalog live reader is sealed")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise TypeError("catalog live reader is sealed")

    def query(self, query: CatalogQuery) -> CatalogPage:
        self._assert_live(self)
        page = self._query_catalog(self._catalog, query)
        self._assert_live(self)
        return page

    def get_verified(
        self,
        result_id: str,
        context: CatalogVerificationContext,
    ) -> CatalogResultRef:
        if (
            type(result_id) is not str
            or type(context) is not CatalogVerificationContext
        ):
            raise KeyError("catalog result is unavailable")
        self._assert_live(self)
        normalized_context = self._verification_context_from_json(
            self._canonicalize(context)
        )
        # Lock order: catalog connection lock, then the catalog trust fence
        # (the registry read fence on the registry path), held through return.
        with (
            self._connection_lock,
            self._trust_fence_catalog(self._catalog) as (trust_store, _),
        ):
            self._assert_live(self)
            assert self._connection is not None
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                visibility_sql = """SELECT r.ref_json FROM results r
                    WHERE r.result_id=? AND (
                        NOT EXISTS(SELECT 1 FROM coordinated_results c
                                   WHERE c.result_id=r.result_id)
                        OR EXISTS(SELECT 1 FROM result_publications p
                                  WHERE p.result_id=r.result_id
                                  AND p.state='adopted'))"""
                row = self._connection.execute(visibility_sql, (result_id,)).fetchone()
                self._assert_live(self)
                if row is None:
                    raise KeyError("catalog result is unavailable")
                content = bytes(row[0])
                try:
                    stored = self._result_ref_from_json(content)
                except (ValidationError, ValueError, TypeError) as exc:
                    raise CatalogError("catalog result reference is invalid") from exc
                if self._canonicalize(stored) != content:
                    raise CatalogError("catalog result reference is not canonical")
                self._replay_capability(
                    normalized_context.registry,
                    normalized_context.authority_head,
                    normalized_context.expected_authority_head_sha256,
                    normalized_context.capability,
                )
                if self._capability_revoked(
                    normalized_context.registry, normalized_context.capability
                ):
                    raise CatalogError("catalog result authority is revoked")
                if normalized_context.capability.method_ref != stored.method_ref:
                    raise CatalogError("catalog result authority is stale")
                self._assert_live(self)
                verified = self._verify_bundle(
                    self._descriptor_resolver(self._objects_fd) / stored.bundle_sha256,
                    trust_store,
                )
                current = self._reference_verified(
                    verified,
                    bundle_sha256=stored.bundle_sha256,
                    manifest_sha256=stored.bundle_manifest_sha256,
                    capability=normalized_context.capability,
                )
                self._fault_catalog(self._catalog, "before_live_reader_return")
                self._assert_live(self)
                final_row = self._connection.execute(
                    visibility_sql, (result_id,)
                ).fetchone()
                if final_row is None or bytes(final_row[0]) != content:
                    raise KeyError("catalog result is unavailable")
                if current != stored:
                    raise CatalogError(
                        "catalog result authority or bundle identity changed"
                    )
                self._connection.commit()
                return current
            except BaseException:
                self._connection.rollback()
                raise


def bind_catalog_live_reader(catalog: ResultCatalog) -> CatalogLiveReader:
    return CatalogLiveReader(catalog)


__all__ = [
    "CATALOG_AUTHORITY_SCHEMA_V1",
    "CATALOG_AUTHORITY_SCHEMA_V2",
    "CATALOG_AUTHORITY_SCHEMA_VERSIONS",
    "CATALOG_CONTENT_LOCK_NAME",
    "CATALOG_CONTENT_SCHEMA_V1",
    "DEFAULT_RESULT_BUNDLE_READER_REGISTRY",
    "CatalogAliases",
    "CatalogAuthoritySnapshot",
    "CatalogConflict",
    "CatalogContentSnapshot",
    "CatalogEmptyReason",
    "CatalogError",
    "CatalogFilesystemError",
    "CatalogLiveReader",
    "CatalogOrder",
    "CatalogPage",
    "CatalogQualificationState",
    "CatalogQuery",
    "CatalogResultRef",
    "CatalogUnsupportedSchema",
    "CatalogVerificationContext",
    "ExecutionState",
    "InformationState",
    "PendingCatalogPublication",
    "PreparedCatalogImport",
    "PublicationId",
    "ResultBundleReader",
    "ResultBundleReaderRegistry",
    "ResultCatalog",
    "TrustState",
    "bind_catalog_live_reader",
    "bound_catalog_authority",
    "catalog_authority_sha256",
    "catalog_dependency_head_sha256",
    "registry_trust_snapshot_sha256",
]
