"""Immutable local catalog and deterministic selector read model.

Bundle paths, private identifiers, and alias mappings never enter result refs,
query pages, exports, or error text. Imports copy only the fixed bundle schema
through descriptor-relative, no-follow opens before invoking the merged verifier.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from evidence_inspector.method_registry import (
    AuthorityHead,
    AuthorityScope,
    CurrentMethodCapability,
    DisplayRole,
    MethodReference,
    MethodRegistry,
    QualificationState,
    replay_current_capability,
)
from traceback_runner.bundles import VerifiedBundle, verify_bundle
from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import TrustStore

CATALOG_SCHEMA_VERSION = 1
MAX_IMPORT_ROOTS = 8
MAX_IMPORT_DEPTH = 4
MAX_QUERY_LIMIT = 100
MAX_FILTER_VALUES = 32

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
ResultId = Annotated[
    str, StringConstraints(pattern=r"^result_[0-9a-f]{40}$")
]
RootId = Annotated[
    str, StringConstraints(pattern=r"^root_[a-z0-9]+(?:_[a-z0-9]+)*$")
]
DisplayAlias = Annotated[
    str, StringConstraints(pattern=r"^dsp_[a-z0-9]{8,32}$")
]
RunAlias = Annotated[str, StringConstraints(pattern=r"^rnx_[a-z0-9]{8,32}$")]
TimepointAlias = Annotated[
    str, StringConstraints(pattern=r"^tpt_[a-z0-9]{8,32}$")
]

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
    schema_version: Literal["traceback.catalog-page.v1"] = (
        "traceback.catalog-page.v1"
    )
    query: CatalogQuery
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


def _qualification(value: QualificationState | None) -> CatalogQualificationState:
    if value is None:
        return CatalogQualificationState.UNKNOWN
    return CatalogQualificationState(value.value)


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
            if _stat_identity(before) != _stat_identity(after) or copied != before.st_size:
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

    def __init__(
        self,
        root: str | Path,
        *,
        import_roots: Mapping[str, str | Path],
        trust_store: TrustStore,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not import_roots or len(import_roots) > MAX_IMPORT_ROOTS:
            raise CatalogFilesystemError("catalog import root count is invalid")
        self.root = Path(root)
        self.import_roots = {
            _ROOT_ID.validate_python(root_id): Path(path)
            for root_id, path in import_roots.items()
        }
        if any(not path.is_absolute() for path in self.import_roots.values()):
            raise CatalogFilesystemError("catalog import roots must be absolute")
        self.trust_store = trust_store
        self.fault_injector = fault_injector
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise CatalogFilesystemError("catalog root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.objects = self.root / "objects"
        if self.objects.is_symlink() or (
            self.objects.exists() and not self.objects.is_dir()
        ):
            raise CatalogFilesystemError("catalog object store is unsafe")
        self.objects.mkdir(exist_ok=True)
        self.objects.chmod(0o700)
        self.database = self.root / "catalog.sqlite3"
        if self.database.is_symlink() or (
            self.database.exists() and not self.database.is_file()
        ):
            raise CatalogFilesystemError("catalog database is unsafe")
        self._initialize()

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
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

    def _initialize(self) -> None:
        new = not self.database.exists()
        with self._connect() as connection:
            if new:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=FULL;
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata VALUES('schema_version', '1');
                    CREATE TABLE results(
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
                    );
                    CREATE TABLE opaque_aliases(
                        result_id TEXT PRIMARY KEY REFERENCES results(result_id),
                        display_alias TEXT NOT NULL UNIQUE,
                        run_alias TEXT NOT NULL,
                        timepoint_alias TEXT NOT NULL
                    );
                    CREATE INDEX results_method ON results(method_id, method_version, result_id);
                    CREATE INDEX results_states ON results(
                        execution_state,
                        information_state,
                        trust_state,
                        qualification_state,
                        result_id
                    );
                    CREATE INDEX aliases_run ON opaque_aliases(run_alias, result_id);
                    CREATE INDEX aliases_timepoint ON opaque_aliases(timepoint_alias, result_id);
                    """
                )
                os.chmod(self.database, 0o600)
            else:
                try:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                except sqlite3.DatabaseError:
                    raise CatalogUnsupportedSchema(
                        "catalog schema is unsupported"
                    ) from None
                if row is None or row[0] != str(CATALOG_SCHEMA_VERSION):
                    raise CatalogUnsupportedSchema("catalog schema is unsupported")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")

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
        temporary = Path(tempfile.mkdtemp(prefix=".catalog-", dir=self.objects))
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
            self._fault("after_bundle_snapshot")
            _verify_source_identities(current_fd, source_identities)
            for parent_fd, part, expected_identity in path_bindings:
                rebound_fd = _open_directory_at(parent_fd, part)
                try:
                    if _stat_identity(os.fstat(rebound_fd)) != expected_identity:
                        raise CatalogFilesystemError("catalog import path changed")
                finally:
                    os.close(rebound_fd)
            final_root = os.stat(root_path, follow_symlinks=False)
            if (
                stat.S_ISLNK(final_root.st_mode)
                or (final_root.st_dev, final_root.st_ino)
                != (root_stat.st_dev, root_stat.st_ino)
            ):
                raise CatalogFilesystemError("catalog import root changed")
            return temporary, bundle_sha256, manifest_sha256
        except CatalogError:
            _remove_tree(temporary)
            raise
        except Exception:
            _remove_tree(temporary)
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
        temporary, bundle_sha256, manifest_sha256 = self._capture(
            root_id, relative_path
        )
        published = False
        object_path = self.objects / bundle_sha256
        try:
            verified = verify_bundle(temporary, self.trust_store)
            reference = self._reference(
                verified,
                bundle_sha256=bundle_sha256,
                manifest_sha256=manifest_sha256,
                capability=capability,
            )
            _fsync_tree(temporary)
            _seal_tree(temporary)
            parent_fd = os.open(
                self.objects,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                try:
                    rename_directory_exclusive_at(
                        parent_fd, temporary.name, object_path.name
                    )
                    published = True
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
                os.close(parent_fd)
            self._fault("after_object_publish")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = connection.execute(
                        """SELECT ref_json FROM results
                           WHERE result_id=? OR bundle_sha256=? OR bundle_record_id=?""",
                        (
                            reference.result_id,
                            reference.bundle_sha256,
                            reference.bundle_record_id,
                        ),
                    ).fetchone()
                    if existing is not None:
                        parsed = CatalogResultRef.model_validate_json(existing[0])
                        alias_row = connection.execute(
                            """SELECT display_alias, run_alias, timepoint_alias
                               FROM opaque_aliases WHERE result_id=?""",
                            (parsed.result_id,),
                        ).fetchone()
                        if parsed != reference or alias_row is None or tuple(alias_row) != (
                            aliases.display_alias,
                            aliases.run_alias,
                            aliases.timepoint_alias,
                        ):
                            raise CatalogConflict("catalog identity conflict")
                        connection.commit()
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
                        raise CatalogConflict("catalog alias identity conflict") from None
                    self._fault("before_catalog_commit")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return reference
        except BaseException:
            if temporary.exists():
                _remove_tree(temporary)
            if published:
                try:
                    _remove_tree(object_path)
                except OSError:
                    pass
            raise

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
            query if isinstance(query, CatalogQuery) else CatalogQuery.model_validate(query)
        )
        clauses: list[str] = []
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
        self._append_method_filter(clauses, parameters, normalized.method_refs)
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
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        has_more = len(rows) > normalized.limit
        selected = rows[: normalized.limit]
        results = tuple(
            CatalogResultRef.model_validate_json(row[0]) for row in selected
        )
        return CatalogPage(
            query=normalized,
            results=results,
            next_cursor=results[-1].result_id if has_more and results else None,
            empty=not results,
            empty_reason=(
                CatalogEmptyReason.NO_IMPORTED_RESULTS
                if not results and total == 0
                else CatalogEmptyReason.NO_MATCHES if not results else None
            ),
        )

    @staticmethod
    def _append_method_filter(
        clauses: list[str],
        parameters: list[object],
        methods: Sequence[MethodReference],
    ) -> None:
        if not methods:
            return
        clauses.append(
            "(" + " OR ".join("(r.method_id=? AND r.method_version=?)" for _ in methods) + ")"
        )
        for method in methods:
            parameters.extend((method.method_id, method.version))


__all__ = [
    "CatalogAliases",
    "CatalogConflict",
    "CatalogEmptyReason",
    "CatalogError",
    "CatalogFilesystemError",
    "CatalogOrder",
    "CatalogPage",
    "CatalogQualificationState",
    "CatalogQuery",
    "CatalogResultRef",
    "CatalogUnsupportedSchema",
    "ExecutionState",
    "InformationState",
    "ResultCatalog",
    "TrustState",
]
