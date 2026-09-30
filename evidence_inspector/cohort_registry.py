"""Protected append-only registry for exact D05 cohort-manifest versions.

The registry is provider-local.  It stores canonical manifests behind a
private directory boundary and exposes a separate privacy-bounded selector
projection.  Selector rows never contain subject, collection, specimen, run,
analysis, provider, path, or free-text values.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

import evidence_inspector.cohort_manifest as cohort_manifest_module
from evidence_inspector.cohort_manifest import (
    CohortManifest,
    capture_expected_trust_pins,
    cohort_manifest_bytes,
    cohort_manifest_from_bytes,
    cohort_manifest_sha256,
    trust_pins_sha256,
    validate_manifest_against_linkage_store,
    validate_manifest_history,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_REGISTERED_MANIFESTS = 100_000
MAX_SELECTOR_PAGE = 100
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 8_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_AUTHORITY_READ_FENCE = ProviderLinkageStore.authority_read_fence
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_VALIDATE_MANIFEST_IN_FENCE = (
    cohort_manifest_module._validate_manifest_against_linkage_store_in_fence
)

RegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
SelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CohortRegistryError(RuntimeError):
    """Sanitized registry failure."""


class CohortRegistryConflict(CohortRegistryError):
    pass


class CohortRegistryUnsafe(CohortRegistryError):
    pass


class CohortAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class CohortRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.cohort-registry-metadata.v1"] = (
        "traceback.cohort-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    linkage_trust_pins_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    CohortRegistryMetadata
)


class CohortRegistryJournalEntry(RegistryContract):
    schema_version: Literal["traceback.cohort-registry-journal-entry.v1"] = (
        "traceback.cohort-registry-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_MANIFESTS, strict=True)
    previous_entry_sha256: Sha256
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    manifest_sha256: Sha256
    previous_manifest_sha256: Sha256 | None
    entry_sha256: Sha256


class CohortRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.cohort-registration-receipt.v1"] = (
        "traceback.cohort-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_MANIFESTS)
    state_head_sha256: Sha256
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    manifest_sha256: Sha256
    previous_manifest_sha256: Sha256 | None


class RegisteredCohortManifest(RegistryContract):
    schema_version: Literal["traceback.registered-cohort-manifest.v1"] = (
        "traceback.registered-cohort-manifest.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_MANIFESTS)
    state_head_sha256: Sha256
    manifest_sha256: Sha256
    manifest: CohortManifest


class RegisteredCohortHistory(RegistryContract):
    schema_version: Literal["traceback.registered-cohort-history.v1"] = (
        "traceback.registered-cohort-history.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_MANIFESTS)
    state_head_sha256: Sha256
    selected_manifest_sha256: Sha256
    manifests: tuple[CohortManifest, ...] = Field(min_length=1, max_length=100_000)


class CohortSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.cohort-selector-record.v1"] = (
        "traceback.cohort-selector-record.v1"
    )
    selector_id: SelectorId
    cohort_version: int = Field(ge=1, le=100_000)
    manifest_sha256: Sha256
    authority_state: CohortAuthorityState
    member_count: int = Field(ge=1, le=10_000)
    denominator_count: int = Field(ge=1, le=10_000)
    inclusion_policy_sha256: Sha256
    exclusion_policy_sha256: Sha256
    missingness_policy_sha256: Sha256
    measurement_definition_sha256: Sha256
    anchor_definition_sha256: Sha256
    anchor_authority_sha256: Sha256


class CohortSelectorPage(RegistryContract):
    schema_version: Literal["traceback.cohort-selector-page.v1"] = (
        "traceback.cohort-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_MANIFESTS)
    state_head_sha256: Sha256
    records: tuple[CohortSelectorRecord, ...] = Field(max_length=MAX_SELECTOR_PAGE)
    next_after_selector_id: SelectorId | None
    next_after_version: int | None = Field(default=None, ge=1, le=100_000)


class CohortRegistryBackupObject(RegistryContract):
    schema_version: Literal["traceback.cohort-registry-backup-object.v1"] = (
        "traceback.cohort-registry-backup-object.v1"
    )
    manifest_sha256: Sha256
    manifest_json: Annotated[str, StringConstraints(max_length=MAX_MANIFEST_BYTES)]


class CohortRegistryBackup(RegistryContract):
    schema_version: Literal["traceback.cohort-registry-backup.v1"] = (
        "traceback.cohort-registry-backup.v1"
    )
    metadata: CohortRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_MANIFESTS)
    state_head_sha256: Sha256
    journal: tuple[CohortRegistryJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_MANIFESTS
    )
    objects: tuple[CohortRegistryBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_MANIFESTS
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(CohortRegistryBackup)


def _canonical_backup_bytes(backup: CohortRegistryBackup) -> bytes:
    return exact_model_bytes(
        backup,
        CohortRegistryBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_MANIFESTS,
        max_string_bytes=MAX_MANIFEST_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("cohort registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("cohort registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError("cohort registry path must be an exact string or platform path")
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("cohort registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("cohort registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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
            raise CohortRegistryUnsafe("cohort registry object exceeds its bound")
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
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
        ):
            raise CohortRegistryUnsafe("cohort registry object is unsafe")
        content = _read_bounded(descriptor, MAX_MANIFEST_BYTES)
    except OSError:
        raise CohortRegistryUnsafe("cohort registry object is unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise CohortRegistryUnsafe("cohort registry object digest is invalid")
    return content


def _selector_id(epoch: str, cohort_id: str) -> str:
    digest = hashlib.sha256(
        b"traceback-cohort-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + cohort_id.encode("ascii")
    ).hexdigest()
    return f"cohort_selector_{digest[:40]}"


def _journal_entry_sha256(entry: CohortRegistryJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-cohort-registry-journal-v1\0"
        + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: CohortRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-cohort-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *,
    sequence: int,
    previous_entry_sha256: str,
    manifest: CohortManifest,
    manifest_sha256: str,
) -> CohortRegistryJournalEntry:
    placeholder = CohortRegistryJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        cohort_id=manifest.cohort_id,
        cohort_version=manifest.version,
        manifest_sha256=manifest_sha256,
        previous_manifest_sha256=manifest.previous_manifest_sha256,
        entry_sha256="0" * 64,
    )
    return CohortRegistryJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_backup(backup: CohortRegistryBackup) -> None:
    if backup.state_version != len(backup.journal):
        raise CohortRegistryConflict("cohort registry backup count is invalid")
    cohorts: dict[str, list[CohortManifest]] = {}
    manifests_by_digest: dict[str, CohortManifest] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.manifest_sha256 <= previous_digest:
            raise CohortRegistryConflict("cohort registry backup order is invalid")
        previous_digest = item.manifest_sha256
        try:
            content = item.manifest_json.encode("utf-8")
            manifest = cohort_manifest_from_bytes(content)
        except (UnicodeError, ValueError):
            raise CohortRegistryConflict(
                "cohort registry backup manifest is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.manifest_sha256:
            raise CohortRegistryConflict("cohort registry backup digest is invalid")
        manifests_by_digest[item.manifest_sha256] = manifest
        cohorts.setdefault(manifest.cohort_id, []).append(manifest)
    try:
        for history in cohorts.values():
            history.sort(key=lambda item: item.version)
            validate_manifest_history(tuple(history))
    except (TypeError, ValueError):
        raise CohortRegistryConflict("cohort registry backup history is invalid") from None
    entries_by_digest = {item.manifest_sha256: item for item in backup.journal}
    if len(entries_by_digest) != len(backup.journal) or set(entries_by_digest) != {
        item.manifest_sha256 for item in backup.objects
    }:
        raise CohortRegistryConflict("cohort registry backup journal is invalid")
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        manifest = manifests_by_digest[entry.manifest_sha256]
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.cohort_id != manifest.cohort_id
            or entry.cohort_version != manifest.version
            or entry.previous_manifest_sha256
            != manifest.previous_manifest_sha256
        ):
            raise CohortRegistryConflict("cohort registry backup journal is invalid")
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise CohortRegistryConflict("cohort registry backup state is invalid")


def cohort_registry_backup_from_bytes(content: bytes) -> CohortRegistryBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise CohortRegistryConflict("cohort registry backup exceeds its bound")
    try:
        decoded = bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_MANIFESTS,
            max_string_bytes=MAX_MANIFEST_BYTES,
        )
        backup = CohortRegistryBackup.model_validate(decoded)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("cohort registry backup is not canonical")
    except (TypeError, ValueError):
        raise CohortRegistryConflict("cohort registry backup is invalid") from None
    _validate_backup(backup)
    return backup


def _registry_instance_snapshot(registry: CohortRegistry) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_linkage_store",
        "_trust_pins",
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
        raise CohortRegistryUnsafe("cohort registry authority state changed")
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            CohortRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise CohortRegistryUnsafe(
            "cohort registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in (
            "_root_fd",
            "_objects_fd",
            "_lock_fd",
            "_metadata_fd",
            "_journal_fd",
        )
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise CohortRegistryUnsafe("cohort registry authority state changed")
    elif type(descriptor) is not int:
        raise CohortRegistryUnsafe("cohort registry authority state changed")
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise CohortRegistryUnsafe(
                "cohort registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise CohortRegistryUnsafe("cohort registry authority state changed")
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise CohortRegistryUnsafe("cohort registry authority state changed")
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise CohortRegistryUnsafe(
                "cohort registry authority state changed"
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
            or instance["_genesis_head_sha256"]
            != _metadata_genesis_sha256(metadata)
            or instance["_head_key"] != derived_head_key
        ):
            raise CohortRegistryUnsafe("cohort registry authority state changed")
    pins = instance["_trust_pins"]
    if type(pins) is not dict:
        raise CohortRegistryUnsafe("cohort registry authority state changed")
    try:
        captured_pins = capture_expected_trust_pins(pins)
    except (TypeError, ValueError):
        raise CohortRegistryUnsafe(
            "cohort registry authority state changed"
        ) from None
    return (
        id(instance["root"]),
        id(instance["_linkage_store"]),
        tuple(sorted(captured_pins.items())),
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


def _seal_registry_instance(registry: CohortRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class CohortRegistry:
    """Descriptor-relative immutable manifest publication and safe projection."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "backup_bytes",
            "close",
            "list_selectors",
            "register",
            "resolve",
            "resolve_history",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise CohortRegistryUnsafe("cohort registry callable changed")
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("cohort registry requires the exact linkage store type")
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values):
            if (
                any(item is None for item in expected_values)
                or type(expected_registry_id) is not str
                or len(expected_registry_id) != 48
                or not expected_registry_id.startswith("cohort_registry_")
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_registry_id[16:]
                )
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise CohortRegistryUnsafe(
                    "cohort registry expected identity or head is invalid"
                )
        self.root = _snapshot_path(root)
        self._linkage_store = linkage_store
        self._trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        if (
            _PINNED_ACTIVE_SNAPSHOT(self._linkage_store).trust_pins_sha256
            != trust_pins_sha256(self._trust_pins)
        ):
            raise CohortRegistryUnsafe("cohort registry trust pins are invalid")
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        try:
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            except FileExistsError:
                root_created = False
            else:
                root_created = True
            root_lstat = os.stat(self.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or stat.S_IMODE(root_lstat.st_mode) != 0o700
                or root_lstat.st_uid != os.geteuid()
            ):
                raise CohortRegistryUnsafe("cohort registry root must be private")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            self._root_fd = os.open(self.root, flags)
            bound = os.fstat(self._root_fd)
            if (bound.st_dev, bound.st_ino) != (
                root_lstat.st_dev,
                root_lstat.st_ino,
            ):
                raise CohortRegistryUnsafe("cohort registry root changed")
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
                raise CohortRegistryUnsafe("cohort registry objects are unsafe")
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
                raise CohortRegistryUnsafe("cohort registry lock is unsafe")
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
                raise CohortRegistryUnsafe("cohort registry journal is unsafe")
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
                with _CR_LOCK(self, exclusive=True):
                    self._metadata = _CR_LOAD_OR_CREATE_METADATA(
                        self, allow_create=root_created
                    )
                    self._genesis_head_sha256 = _metadata_genesis_sha256(
                        self._metadata
                    )
                    self._head_key = (
                        self._root_identity[0],
                        self._root_identity[1],
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                    )
                    _CR_RECOVER_TEMPORARY_OBJECTS(self)
                    loaded, head = _CR_LOAD_STATE(self, check_trusted_head=False)
                    if root_created:
                        if any(item is not None for item in expected_values):
                            raise CohortRegistryUnsafe(
                                "new cohort registry cannot inherit expected identity"
                            )
                    elif any(item is None for item in expected_values):
                        raise CohortRegistryUnsafe(
                            "cohort registry expected identity and head are required"
                        )
                    if not root_created and expected_values != (
                        self._metadata.registry_id,
                        self._metadata.registry_epoch_sha256,
                        head,
                    ):
                        raise CohortRegistryUnsafe(
                            "cohort registry expected identity or head is invalid"
                        )
                    self._trusted_head_sha256 = head
                    _CR_ACCEPT_OBSERVED_HEAD(
                        self, _CR_LOAD_JOURNAL(self), head, check_instance=False
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

    def __enter__(self) -> CohortRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _CR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _CR_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _CR_VALIDATE_STORAGE(self)
                yield
                _CR_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise CohortRegistryUnsafe("cohort registry is closed")
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
            raise CohortRegistryUnsafe("cohort registry storage changed") from None
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
            or (journal_path.st_dev, journal_path.st_ino)
            != self._journal_identity
            or (journal_bound.st_dev, journal_bound.st_ino)
            != self._journal_identity
            or stat.S_IMODE(journal_bound.st_mode) != 0o600
            or journal_bound.st_uid != os.geteuid()
        ):
            raise CohortRegistryUnsafe("cohort registry storage changed")
        if self._metadata_fd is not None:
            try:
                metadata_path = os.stat(
                    "registry-metadata.json",
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                metadata_bound = os.fstat(self._metadata_fd)
            except OSError:
                raise CohortRegistryUnsafe("cohort registry storage changed") from None
            if (
                not stat.S_ISREG(metadata_path.st_mode)
                or (metadata_path.st_dev, metadata_path.st_ino)
                != self._metadata_identity
                or (metadata_bound.st_dev, metadata_bound.st_ino)
                != self._metadata_identity
                or stat.S_IMODE(metadata_bound.st_mode) != 0o600
                or metadata_bound.st_uid != os.geteuid()
            ):
                raise CohortRegistryUnsafe("cohort registry storage changed")

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        if self._objects_fd is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise CohortRegistryUnsafe(
                "cohort registry objects are unavailable"
            ) from None
        for name in names:
            if (
                type(name) is str
                and len(name) == 37
                and name.startswith(".tmp-")
                and all(character in "0123456789abcdef" for character in name[5:])
            ):
                try:
                    os.unlink(name, dir_fd=self._objects_fd)
                except OSError:
                    raise CohortRegistryUnsafe(
                        "cohort registry recovery is unsafe"
                    ) from None
        os.fsync(self._objects_fd)

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> CohortRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise CohortRegistryUnsafe(
                    "cohort registry metadata is missing"
                ) from None
            snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
            metadata = CohortRegistryMetadata(
                registry_id=f"cohort_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                linkage_store_id=snapshot.store_id,
                linkage_store_epoch_sha256=snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=snapshot.storage_identity_sha256,
                linkage_trust_pins_sha256=snapshot.trust_pins_sha256,
            )
            try:
                _CR_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _CR_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise CohortRegistryUnsafe("cohort registry metadata is unsafe")
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(CohortRegistryMetadata, content)
        except Exception:
            os.close(descriptor)
            raise CohortRegistryUnsafe("cohort registry metadata is invalid") from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        snapshot = _PINNED_ACTIVE_SNAPSHOT(self._linkage_store)
        if (
            metadata.linkage_store_id,
            metadata.linkage_store_epoch_sha256,
            metadata.linkage_storage_identity_sha256,
            metadata.linkage_trust_pins_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
        ):
            os.close(descriptor)
            self._metadata_fd = None
            raise CohortRegistryUnsafe("cohort registry linkage authority changed")
        return metadata

    def _load_journal(self) -> tuple[CohortRegistryJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, MAX_BACKUP_BYTES)
        except OSError:
            raise CohortRegistryUnsafe("cohort registry journal is unavailable") from None
        if content and not content.endswith(b"\n"):
            raise CohortRegistryUnsafe("cohort registry journal is incomplete")
        entries: list[CohortRegistryJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_manifests: set[str] = set()
        seen_versions: set[tuple[str, int]] = set()
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_MANIFESTS:
                raise CohortRegistryUnsafe("cohort registry journal bound exceeded")
            try:
                entry = contract_from_canonical_bytes(
                    CohortRegistryJournalEntry, line
                )
            except Exception:
                raise CohortRegistryUnsafe(
                    "cohort registry journal is invalid"
                ) from None
            version_key = (entry.cohort_id, entry.cohort_version)
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.manifest_sha256 in seen_manifests
                or version_key in seen_versions
            ):
                raise CohortRegistryUnsafe("cohort registry journal is invalid")
            entries.append(entry)
            previous = entry.entry_sha256
            seen_manifests.add(entry.manifest_sha256)
            seen_versions.add(version_key)
        return tuple(entries)

    def _append_journal(self, entry: CohortRegistryJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError:
            raise CohortRegistryUnsafe("cohort registry journal append failed") from None

    def _accept_observed_head(
        self,
        journal: tuple[CohortRegistryJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise CohortRegistryUnsafe("cohort registry state rollback detected")
        if check_instance:
            trusted_head = self._trusted_head_sha256
            if trusted_head not in chain:
                raise CohortRegistryUnsafe("cohort registry state rollback detected")
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[CohortManifest, bytes]], str]:
        if self._objects_fd is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise CohortRegistryUnsafe(
                "cohort registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_MANIFESTS:
            raise CohortRegistryUnsafe("cohort registry object bound exceeded")
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise CohortRegistryUnsafe("cohort registry contains an invalid object")
        loaded: dict[str, tuple[CohortManifest, bytes]] = {}
        for name in sorted(names):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._objects_fd,
                )
                observed = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(observed.st_mode)
                    or stat.S_IMODE(observed.st_mode) != 0o600
                    or observed.st_uid != os.geteuid()
                    or observed.st_nlink != 1
                ):
                    raise CohortRegistryUnsafe("cohort registry object is unsafe")
                content = _read_bounded(descriptor, MAX_MANIFEST_BYTES)
            except OSError:
                raise CohortRegistryUnsafe("cohort registry object is unsafe") from None
            finally:
                if "descriptor" in locals():
                    os.close(descriptor)
                    del descriptor
            digest = hashlib.sha256(content).hexdigest()
            if name != f"{digest}.json":
                raise CohortRegistryUnsafe("cohort registry object digest is invalid")
            try:
                manifest = cohort_manifest_from_bytes(content)
            except ValueError:
                raise CohortRegistryUnsafe(
                    "cohort registry manifest is invalid"
                ) from None
            loaded[digest] = (manifest, content)
        journal = _CR_LOAD_JOURNAL(self)
        committed: dict[str, tuple[CohortManifest, bytes]] = {}
        for entry in journal:
            item = loaded.get(entry.manifest_sha256)
            if item is None:
                raise CohortRegistryUnsafe(
                    "cohort registry committed object is missing"
                )
            manifest, _ = item
            if (
                entry.cohort_id != manifest.cohort_id
                or entry.cohort_version != manifest.version
                or entry.previous_manifest_sha256
                != manifest.previous_manifest_sha256
            ):
                raise CohortRegistryUnsafe(
                    "cohort registry journal binding is invalid"
                )
            committed[entry.manifest_sha256] = item
        cohorts: dict[str, list[CohortManifest]] = {}
        for manifest, _ in committed.values():
            cohorts.setdefault(manifest.cohort_id, []).append(manifest)
        try:
            for history in cohorts.values():
                history.sort(key=lambda item: item.version)
                validate_manifest_history(tuple(history))
        except (TypeError, ValueError):
            raise CohortRegistryUnsafe("cohort registry history is invalid") from None
        head = (
            journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        )
        if check_trusted_head:
            _CR_ACCEPT_OBSERVED_HEAD(
                self, journal, head, check_instance=True
            )
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise CohortRegistryUnsafe(
                    "cohort registry state rollback detected"
                )
        return committed, head

    def register(self, manifest: CohortManifest) -> CohortRegistrationReceipt:
        _require_registry_integrity(self)
        try:
            content = cohort_manifest_bytes(manifest)
            captured = cohort_manifest_from_bytes(content)
            digest = cohort_manifest_sha256(captured)
            validate_manifest_against_linkage_store(
                captured,
                self._linkage_store,
                expected_trust_snapshot_sha256_by_provider=self._trust_pins,
            )
        except Exception:
            raise CohortRegistryConflict(
                "cohort manifest is not current and valid"
            ) from None
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            try:
                _PINNED_VALIDATE_MANIFEST_IN_FENCE(
                    captured,
                    self._linkage_store,
                    expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                )
            except Exception:
                raise CohortRegistryConflict(
                    "cohort manifest is not current and valid"
                ) from None
            with _CR_LOCK(self, exclusive=True):
                _CR_RECOVER_TEMPORARY_OBJECTS(self)
                loaded, head = _CR_LOAD_STATE(self)
                histories: dict[str, list[CohortManifest]] = {}
                for existing, _ in loaded.values():
                    histories.setdefault(existing.cohort_id, []).append(existing)
                history = sorted(
                    histories.get(captured.cohort_id, []),
                    key=lambda item: item.version,
                )
                if digest in loaded:
                    if loaded[digest][1] != content:
                        raise CohortRegistryConflict(
                            "cohort manifest digest conflicts"
                        )
                else:
                    if len(loaded) >= MAX_REGISTERED_MANIFESTS:
                        raise CohortRegistryConflict("cohort registry is full")
                    try:
                        validate_manifest_history(tuple((*history, captured)))
                    except Exception:
                        raise CohortRegistryConflict(
                            "cohort manifest cannot extend current registry history"
                        ) from None
                    assert self._objects_fd is not None
                    try:
                        _CR_PUBLISH(
                            self, self._objects_fd, f"{digest}.json", content
                        )
                    except FileExistsError:
                        existing = _read_exact_object(self._objects_fd, digest)
                        if existing != content:
                            raise CohortRegistryConflict(
                                "cohort manifest publication conflicts"
                            ) from None
                    entry = _build_journal_entry(
                        sequence=len(loaded) + 1,
                        previous_entry_sha256=head,
                        manifest=captured,
                        manifest_sha256=digest,
                    )
                    _CR_APPEND_JOURNAL(self, entry)
                final, final_head = _CR_LOAD_STATE(self)
                if digest not in final or final[digest][1] != content:
                    raise CohortRegistryUnsafe(
                        "cohort manifest publication is unproven"
                    )
                return CohortRegistrationReceipt(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(final),
                    state_head_sha256=final_head,
                    cohort_id=captured.cohort_id,
                    cohort_version=captured.version,
                    manifest_sha256=digest,
                    previous_manifest_sha256=captured.previous_manifest_sha256,
                )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _CR_LOCK(self, exclusive=False):
            loaded, head = _CR_LOAD_STATE(self)
            journal = _CR_LOAD_JOURNAL(self)
            backup = CohortRegistryBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=journal,
                objects=tuple(
                    CohortRegistryBackupObject(
                        manifest_sha256=digest,
                        manifest_json=content.decode("utf-8"),
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                content = _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise CohortRegistryConflict(
                    "cohort registry backup exceeds its bound"
                ) from None
            return content

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> CohortRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("cohort registry requires the exact linkage store type")
        backup = cohort_registry_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256
            != backup.metadata.registry_epoch_sha256
            or type(expected_state_head_sha256) is not str
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise CohortRegistryConflict(
                "cohort registry backup expected head is invalid"
            )
        pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        snapshot = _PINNED_ACTIVE_SNAPSHOT(linkage_store)
        if snapshot.trust_pins_sha256 != trust_pins_sha256(pins) or (
            backup.metadata.linkage_store_id,
            backup.metadata.linkage_store_epoch_sha256,
            backup.metadata.linkage_storage_identity_sha256,
            backup.metadata.linkage_trust_pins_sha256,
        ) != (
            snapshot.store_id,
            snapshot.store_epoch_sha256,
            snapshot.storage_identity_sha256,
            snapshot.trust_pins_sha256,
        ):
            raise CohortRegistryConflict(
                "cohort registry backup authority is invalid"
            )
        target = _snapshot_path(root)
        parent = target.parent
        try:
            parent_lstat = os.stat(parent, follow_symlinks=False)
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            parent_bound = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_lstat.st_mode)
                or (parent_lstat.st_dev, parent_lstat.st_ino)
                != (parent_bound.st_dev, parent_bound.st_ino)
            ):
                raise CohortRegistryUnsafe("cohort registry restore parent changed")
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            root_lstat = os.stat(
                target.name, dir_fd=parent_fd, follow_symlinks=False
            )
            root_fd = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or (root_lstat.st_dev, root_lstat.st_ino)
                != (root_bound.st_dev, root_bound.st_ino)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise CohortRegistryUnsafe("cohort registry restore root changed")
            os.mkdir("objects", 0o700, dir_fd=root_fd)
            objects_lstat = os.stat(
                "objects", dir_fd=root_fd, follow_symlinks=False
            )
            objects_fd = os.open(
                "objects",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            objects_bound = os.fstat(objects_fd)
            if (
                not stat.S_ISDIR(objects_lstat.st_mode)
                or (objects_lstat.st_dev, objects_lstat.st_ino)
                != (objects_bound.st_dev, objects_bound.st_ino)
                or stat.S_IMODE(objects_bound.st_mode) != 0o700
                or objects_bound.st_uid != os.geteuid()
            ):
                raise CohortRegistryUnsafe("cohort registry restore objects changed")
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
                    f"{item.manifest_sha256}.json",
                    item.manifest_json.encode("utf-8"),
                )
            journal_content = b"".join(
                canonical_contract_bytes(entry) + b"\n" for entry in backup.journal
            )
            _publish_file(root_fd, "registry-journal.jsonl", journal_content)
            os.fsync(objects_fd)
            os.fsync(root_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            raise CohortRegistryConflict(
                "cohort registry restore target already exists"
            ) from None
        except OSError:
            raise CohortRegistryUnsafe("cohort registry restore failed") from None
        finally:
            for name in ("objects_fd", "root_fd", "parent_fd"):
                descriptor = locals().get(name)
                if type(descriptor) is int:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        return _CR_CONSTRUCT(
            target,
            linkage_store=linkage_store,
            expected_trust_snapshot_sha256_by_provider=pins,
            expected_registry_id=expected_registry_id,
            expected_registry_epoch_sha256=expected_registry_epoch_sha256,
            expected_state_head_sha256=expected_state_head_sha256,
        )

    def resolve(
        self, selector_id: str, cohort_version: int
    ) -> RegisteredCohortManifest:
        _require_registry_integrity(self)
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _CR_LOCK(self, exclusive=False):
                history = _CR_RESOLVE_HISTORY_IN_FENCE(
                    self, selector_id, cohort_version
                )
                return RegisteredCohortManifest(
                    registry_id=history.registry_id,
                    registry_epoch_sha256=history.registry_epoch_sha256,
                    state_version=history.state_version,
                    state_head_sha256=history.state_head_sha256,
                    manifest_sha256=history.selected_manifest_sha256,
                    manifest=history.manifests[-1],
                )

    def resolve_history(
        self, selector_id: str, cohort_version: int
    ) -> RegisteredCohortHistory:
        _require_registry_integrity(self)
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _CR_LOCK(self, exclusive=False):
                return _CR_RESOLVE_HISTORY_IN_FENCE(
                    self, selector_id, cohort_version
                )

    def _resolve_history_in_fence(
        self, selector_id: str, cohort_version: int
    ) -> RegisteredCohortHistory:
        if (
            type(selector_id) is not str
            or len(selector_id) != 56
            or not selector_id.startswith("cohort_selector_")
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise CohortRegistryConflict("cohort selector is invalid")
        loaded, head = _CR_LOAD_STATE(self)
        matches = [
            (manifest.version, digest, manifest)
            for digest, (manifest, _) in loaded.items()
            if manifest.version <= cohort_version
            and _selector_id(
                self._metadata.registry_epoch_sha256, manifest.cohort_id
            )
            == selector_id
        ]
        matches.sort(key=lambda item: item[0])
        if (
            len(matches) != cohort_version
            or [item[0] for item in matches]
            != list(range(1, cohort_version + 1))
        ):
            raise CohortRegistryConflict("cohort selector is unavailable")
        _, digest, manifest = matches[-1]
        try:
            _PINNED_VALIDATE_MANIFEST_IN_FENCE(
                manifest,
                self._linkage_store,
                expected_trust_snapshot_sha256_by_provider=self._trust_pins,
            )
        except Exception:
            raise CohortRegistryConflict(
                "cohort selector authority is stale"
            ) from None
        return RegisteredCohortHistory(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=self._metadata.registry_epoch_sha256,
            state_version=len(loaded),
            state_head_sha256=head,
            selected_manifest_sha256=digest,
            manifests=tuple(item[2] for item in matches),
        )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        after_version: int | None = None,
        limit: int = 50,
    ) -> CohortSelectorPage:
        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise CohortRegistryConflict("cohort selector page bound is invalid")
        if (after_selector_id is None) != (after_version is None):
            raise CohortRegistryConflict("cohort selector cursor is incomplete")
        if after_selector_id is not None and (
            type(after_selector_id) is not str
            or not after_selector_id.startswith("cohort_selector_")
            or len(after_selector_id) != 56
            or type(after_version) is not int
            or not 1 <= after_version <= 100_000
        ):
            raise CohortRegistryConflict("cohort selector cursor is invalid")
        with _PINNED_AUTHORITY_READ_FENCE(self._linkage_store):
            with _CR_LOCK(self, exclusive=False):
                loaded, head = _CR_LOAD_STATE(self)
                ordered = sorted(
                    (
                        _selector_id(
                            self._metadata.registry_epoch_sha256,
                            manifest.cohort_id,
                        ),
                        manifest.version,
                        digest,
                        manifest,
                    )
                    for digest, (manifest, _) in loaded.items()
                )
                if after_selector_id is not None:
                    cursor = (after_selector_id, after_version)
                    ordered = [item for item in ordered if item[:2] > cursor]
                selected = ordered[:limit]
                rows: list[CohortSelectorRecord] = []
                for selector_id, version, digest, manifest in selected:
                    try:
                        _PINNED_VALIDATE_MANIFEST_IN_FENCE(
                            manifest,
                            self._linkage_store,
                            expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                        )
                    except Exception:
                        authority_state = CohortAuthorityState.STALE
                    else:
                        authority_state = CohortAuthorityState.CURRENT
                    rows.append(
                        CohortSelectorRecord(
                            selector_id=selector_id,
                            cohort_version=version,
                            manifest_sha256=digest,
                            authority_state=authority_state,
                            member_count=len(manifest.members),
                            denominator_count=sum(
                                member.denominator_contribution
                                for member in manifest.members
                            ),
                            inclusion_policy_sha256=(
                                manifest.policies.inclusion_sha256
                            ),
                            exclusion_policy_sha256=(
                                manifest.policies.exclusion_sha256
                            ),
                            missingness_policy_sha256=(
                                manifest.policies.missingness_sha256
                            ),
                            measurement_definition_sha256=(
                                manifest.measurement_anchor.measurement_definition_sha256
                            ),
                            anchor_definition_sha256=(
                                manifest.measurement_anchor.anchor_definition_sha256
                            ),
                            anchor_authority_sha256=(
                                manifest.measurement_anchor.authority_sha256
                            ),
                        )
                    )
                more = len(ordered) > len(selected)
                return CohortSelectorPage(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    records=tuple(rows),
                    next_after_selector_id=(
                        rows[-1].selector_id if more and rows else None
                    ),
                    next_after_version=(
                        rows[-1].cohort_version if more and rows else None
                    ),
                )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: CohortRegistry.__dict__[name]
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
            "_resolve_history_in_fence",
            "register",
            "backup_bytes",
            "restore",
            "resolve",
            "resolve_history",
            "list_selectors",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not CohortRegistry or any(
        CohortRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise CohortRegistryUnsafe("cohort registry callable changed")


def _require_registry_integrity(registry: CohortRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise CohortRegistryUnsafe("cohort registry callable changed")
    authority_sources = {
        "_PINNED_AUTHORITY_READ_FENCE": ProviderLinkageStore.authority_read_fence,
        "_PINNED_ACTIVE_SNAPSHOT": ProviderLinkageStore.active_snapshot,
        "_PINNED_VALIDATE_MANIFEST_IN_FENCE": (
            cohort_manifest_module._validate_manifest_against_linkage_store_in_fence
        ),
    }
    if any(
        globals().get(name) is not expected
        or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise CohortRegistryUnsafe("cohort registry authority callable changed")
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
        raise CohortRegistryUnsafe("cohort registry authority state changed")
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise CohortRegistryUnsafe("cohort registry authority state changed")


_CR_CONSTRUCT = CohortRegistry
_CR_CLOSE = CohortRegistry.close
_CR_LOCK = CohortRegistry._lock
_CR_VALIDATE_STORAGE = CohortRegistry._validate_storage
_CR_PUBLISH = CohortRegistry._publish
_CR_RECOVER_TEMPORARY_OBJECTS = CohortRegistry._recover_temporary_objects
_CR_LOAD_OR_CREATE_METADATA = CohortRegistry._load_or_create_metadata
_CR_LOAD_JOURNAL = CohortRegistry._load_journal
_CR_APPEND_JOURNAL = CohortRegistry._append_journal
_CR_ACCEPT_OBSERVED_HEAD = CohortRegistry._accept_observed_head
_CR_LOAD_STATE = CohortRegistry._load_state
_CR_RESOLVE_HISTORY_IN_FENCE = CohortRegistry._resolve_history_in_fence
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_AUTHORITY_READ_FENCE": _PINNED_AUTHORITY_READ_FENCE,
        "_PINNED_ACTIVE_SNAPSHOT": _PINNED_ACTIVE_SNAPSHOT,
        "_PINNED_VALIDATE_MANIFEST_IN_FENCE": (
            _PINNED_VALIDATE_MANIFEST_IN_FENCE
        ),
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_CR_CONSTRUCT",
            "_CR_CLOSE",
            "_CR_LOCK",
            "_CR_VALIDATE_STORAGE",
            "_CR_PUBLISH",
            "_CR_RECOVER_TEMPORARY_OBJECTS",
            "_CR_LOAD_OR_CREATE_METADATA",
            "_CR_LOAD_JOURNAL",
            "_CR_APPEND_JOURNAL",
            "_CR_ACCEPT_OBSERVED_HEAD",
            "_CR_LOAD_STATE",
            "_CR_RESOLVE_HISTORY_IN_FENCE",
        )
    }
)


__all__ = [
    "CohortAuthorityState",
    "CohortRegistrationReceipt",
    "CohortRegistry",
    "CohortRegistryBackup",
    "CohortRegistryBackupObject",
    "CohortRegistryConflict",
    "CohortRegistryError",
    "CohortRegistryUnsafe",
    "CohortSelectorPage",
    "CohortSelectorRecord",
    "RegisteredCohortManifest",
    "RegisteredCohortHistory",
    "cohort_registry_backup_from_bytes",
]
