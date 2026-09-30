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
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    ValidationError,
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
    RevocationTarget,
    replay_current_capability,
)
from traceback_runner.bundles import VerifiedBundle, verify_bundle
from traceback_runner.contracts import ResultBundleManifestV2
from traceback_runner.filesystem import rename_directory_exclusive_at
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import TrustStore

CATALOG_SCHEMA_VERSION = 1
MAX_IMPORT_ROOTS = 8
MAX_IMPORT_DEPTH = 4
MAX_QUERY_LIMIT = 100
MAX_FILTER_VALUES = 32

_SQLITE_OPEN_LOCK = threading.RLock()


def _normalize_schema_sql(statement: str) -> str:
    return "".join(statement.split()).casefold()


_CATALOG_SCHEMA_SQL = {
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
        self.root = Path(root).absolute()
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

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

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
        for attribute in ("_database_fd", "_objects_fd", "_root_fd"):
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
        self._validate_storage()
        self._bind_database_descriptor()
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
                self._bind_database_descriptor()
            if (
                not stat.S_ISREG(os.fstat(self._database_fd).st_mode)
                or _inode_identity(os.fstat(self._database_fd)) != observed_identity
            ):
                raise CatalogFilesystemError("catalog database changed")
            self._database_identity = observed_identity
            self._validate_storage()
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
                    self._connection = self._open_sqlite_connection()
            self._validate_storage()
            try:
                yield self._connection
            finally:
                self._validate_storage()

    def _initialize(self) -> None:
        with self._connection_lock, _SQLITE_OPEN_LOCK, self._connect() as connection:
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
        self._validate_storage()

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
        self._validate_storage()
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
            self._fault("after_bundle_snapshot")
            self._validate_storage()
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
        except OSError:
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
        temporary, bundle_sha256, manifest_sha256 = self._capture(
            root_id, relative_path
        )
        object_path = self._bound_objects / bundle_sha256
        try:
            verified = verify_bundle(temporary, self.trust_store)
            if not isinstance(verified.manifest, ResultBundleManifestV2):
                raise CatalogUnsupportedSchema("bundle schema is unsupported")
            if (
                verified.manifest.method.method_id != capability.method_ref.method_id
                or verified.manifest.method.version != capability.method_ref.version
                or verified.manifest.method.method_definition_sha256
                != capability.method_definition_sha256
            ):
                raise CatalogConflict("bundle method identity conflicts")
            reference = self._reference(
                verified,
                bundle_sha256=bundle_sha256,
                manifest_sha256=manifest_sha256,
                capability=capability,
            )
            _fsync_tree(temporary)
            _seal_tree(temporary)
            self._validate_storage()
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
                self._validate_storage()
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
                        self._validate_storage()
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
                    self._fault("before_catalog_commit")
                    connection.commit()
                    self._validate_storage()
                except BaseException:
                    connection.rollback()
                    raise
            return reference
        except BaseException:
            if temporary.exists():
                _remove_tree(temporary)
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
            query
            if isinstance(query, CatalogQuery)
            else CatalogQuery.model_validate(query)
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
            catalog_has_results = bool(rows)
            if not rows:
                catalog_has_results = (
                    connection.execute("SELECT 1 FROM results LIMIT 1").fetchone()
                    is not None
                )
        self._validate_storage()
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
        """Reload and re-verify E04 storage, bundle trust, and live authority."""

        return bind_catalog_live_reader(self).get_verified(result_id, context)

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


_CATALOG_VALIDATE_STORAGE = ResultCatalog._validate_storage
_CATALOG_QUERY = ResultCatalog.query
_CATALOG_REFERENCE = ResultCatalog._reference
_VERIFY_CATALOG_BUNDLE = verify_bundle
_CATALOG_PROTECTED_NAMES = (
    "get_verified",
    "_connect",
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
        or catalog._connection_lock is not reader._connection_lock
        or catalog.trust_store is not reader._trust_store
        or id(catalog.trust_store._keys) != reader._trust_keys_identity
        or tuple(sorted(catalog.trust_store._keys.items())) != reader._trust_snapshot
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
        "_trust_keys_identity",
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
        _query_catalog: Callable[[ResultCatalog, CatalogQuery], CatalogPage] = (
            _CATALOG_QUERY
        ),
        _reference_verified: Callable[..., CatalogResultRef] = _CATALOG_REFERENCE,
        _replay_capability: Callable[..., None] = replay_current_capability,
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
        self._root_fd = catalog._root_fd
        self._objects_fd = catalog._objects_fd
        self._query_catalog = _query_catalog
        self._reference_verified = _reference_verified
        self._replay_capability = _replay_capability
        self._result_ref_from_json = _result_ref_from_json
        self._database_fd = catalog._database_fd
        self._sqlite_database_fd = catalog._sqlite_database_fd
        self._connection_lock = catalog._connection_lock
        self._trust_store = catalog.trust_store
        self._trust_keys_identity = id(catalog.trust_store._keys)
        self._trust_snapshot = tuple(sorted(catalog.trust_store._keys.items()))
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
        self._assert_live(self)
        normalized_context = self._verification_context_from_json(
            self._canonicalize(context)
        )
        with self._connection_lock:
            self._assert_live(self)
            assert self._connection is not None
            row = self._connection.execute(
                "SELECT ref_json FROM results WHERE result_id=?", (result_id,)
            ).fetchone()
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
            self._trust_store,
        )
        current = self._reference_verified(
            verified,
            bundle_sha256=stored.bundle_sha256,
            manifest_sha256=stored.bundle_manifest_sha256,
            capability=normalized_context.capability,
        )
        self._assert_live(self)
        if current != stored:
            raise CatalogError("catalog result authority or bundle identity changed")
        return current


def bind_catalog_live_reader(catalog: ResultCatalog) -> CatalogLiveReader:
    return CatalogLiveReader(catalog)


__all__ = [
    "CatalogAliases",
    "CatalogConflict",
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
    "ResultCatalog",
    "TrustState",
    "bind_catalog_live_reader",
]
