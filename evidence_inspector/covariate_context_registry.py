"""Protected append-only registry for live D10 covariate contexts.

The registry is provider-local and is the ``d10_context_registry`` the E12
builder requires.  It stores only the inputs a live D10 context is derived
from: the caller's opaque covariate tokens, one D09 policy selector/version,
one D03 series selector, and the D02 anchor-policy pin.  Callers never supply
a context.  Registration derives one through the pinned live builder to prove
the inputs bind current D09 and D03 authority, and every protected read
rebuilds the context from the live D09 policy registry and D03 decision
registry before returning it.  The separate selector projection carries only
opaque selectors, digests, aggregate classification, counts, and authority
state.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.covariate_context as d10_module
import evidence_inspector.denominator_policy_registry as d09_registry_module
import evidence_inspector.longitudinal_decision_registry as d03_registry_module
from evidence_inspector.covariate_context import (
    MAX_COVARIATE_MEMBERS,
    CovariateClassification,
    LiveCovariateContext,
    LiveCovariateMemberValues,
    build_live_covariate_context,
    capture_live_covariates,
    covariate_context_result_sha256,
    live_covariate_context_bytes,
)
from evidence_inspector.denominator_policy_registry import (
    DenominatorPolicyRegistry,
    DenominatorPolicyRegistryConflict,
)
from evidence_inspector.longitudinal_decision_registry import (
    LongitudinalDecisionRegistry,
    LongitudinalDecisionRegistryConflict,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.registry_storage import (
    begin_staged_root as _begin_staged_root,
    bound_name as _bound_name,
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

MAX_REGISTERED_CONTEXTS = 10_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 16
MAX_OBJECT_GRAPH_NODES = 64 * MAX_COVARIATE_MEMBERS + 256
MAX_OBJECT_COLLECTION_ITEMS = MAX_COVARIATE_MEMBERS
MAX_OBJECT_STRING_BYTES = 4096
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_BUILD_LIVE = build_live_covariate_context
_PINNED_LIVE_BYTES = live_covariate_context_bytes
_PINNED_CAPTURE_COVARIATES = capture_live_covariates
_PINNED_CONTEXT_SHA256 = covariate_context_result_sha256
_PINNED_D09_INTEGRITY = d09_registry_module._require_registry_integrity
_PINNED_D03_INTEGRITY = d03_registry_module._require_registry_integrity

RegistryId = Annotated[str, StringConstraints(pattern=r"^d10_registry_[0-9a-f]{32}$")]
ContextSelectorId = Annotated[
    str, StringConstraints(pattern=r"^d10_context_[0-9a-f]{40}$")
]
D09RegistryId = Annotated[
    str, StringConstraints(pattern=r"^d09_registry_[0-9a-f]{32}$")
]
D09SelectorId = Annotated[str, StringConstraints(pattern=r"^d09_policy_[0-9a-f]{40}$")]
D03RegistryId = Annotated[
    str, StringConstraints(pattern=r"^d03_registry_[0-9a-f]{32}$")
]
D03SelectorId = Annotated[str, StringConstraints(pattern=r"^d03_series_[0-9a-f]{40}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CovariateContextRegistryError(RuntimeError):
    """Sanitized registry failure."""


class CovariateContextRegistryConflict(CovariateContextRegistryError):
    pass


class CovariateContextRegistryStale(CovariateContextRegistryConflict):
    """A registered context no longer rebuilds from live D09/D03 authority."""


class CovariateContextRegistryUnsafe(CovariateContextRegistryError):
    pass


class ContextAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class CovariateContextRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.d10-context-registry-metadata.v1"] = (
        "traceback.d10-context-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    d09_registry_id: D09RegistryId
    d09_registry_epoch_sha256: Sha256
    d03_registry_id: D03RegistryId
    d03_registry_epoch_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    CovariateContextRegistryMetadata
)


class RegisteredCovariateContextObject(RegistryContract):
    """Protected stored D10 inputs and the D09/D03 selections they bind."""

    schema_version: Literal["traceback.d10-registered-context-object.v1"] = (
        "traceback.d10-registered-context-object.v1"
    )
    d09_registry_id: D09RegistryId
    d09_registry_epoch_sha256: Sha256
    d09_selector_id: D09SelectorId
    d09_policy_version: int = Field(ge=1, le=100_000, strict=True)
    d03_registry_id: D03RegistryId
    d03_registry_epoch_sha256: Sha256
    d03_series_selector_id: D03SelectorId
    d02_anchor_policy_sha256: Sha256
    covariates: tuple[LiveCovariateMemberValues, ...] = Field(
        max_length=MAX_COVARIATE_MEMBERS
    )

    @model_validator(mode="after")
    def canonical_covariates(self) -> RegisteredCovariateContextObject:
        members = tuple(item.member_sha256 for item in self.covariates)
        if members != tuple(sorted(set(members))):
            raise ValueError("registered covariates must be uniquely sorted")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredCovariateContextObject
)


class CovariateContextJournalEntry(RegistryContract):
    schema_version: Literal["traceback.d10-context-journal-entry.v1"] = (
        "traceback.d10-context-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_CONTEXTS, strict=True)
    previous_entry_sha256: Sha256
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class CovariateContextRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.d10-context-registration-receipt.v1"] = (
        "traceback.d10-context-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_CONTEXTS)
    state_head_sha256: Sha256
    selector_id: ContextSelectorId
    object_sha256: Sha256
    context_sha256: Sha256

    @model_validator(mode="after")
    def exact_selector(self) -> CovariateContextRegistrationReceipt:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("D10 receipt selector does not match its object")
        return self


class RegisteredLiveCovariateContext(RegistryContract):
    """Protected D10 context rebuilt from live D09 and D03 authority on read."""

    schema_version: Literal["traceback.d10-registered-live-context.v1"] = (
        "traceback.d10-registered-live-context.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_CONTEXTS)
    state_head_sha256: Sha256
    selector_id: ContextSelectorId
    object_sha256: Sha256
    context_sha256: Sha256
    live: LiveCovariateContext
    rebuilt_against_live_authority: Literal[True] = True
    synthetic_only: Literal[True] = True
    protected_local_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredLiveCovariateContext:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256, self.object_sha256
        ):
            raise ValueError("registered D10 selector does not match its object")
        if self.context_sha256 != _PINNED_CONTEXT_SHA256(self.live.context):
            raise ValueError("registered D10 context digest is invalid")
        return self


class CovariateContextSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.d10-context-selector-record.v1"] = (
        "traceback.d10-context-selector-record.v1"
    )
    selector_id: ContextSelectorId
    object_sha256: Sha256
    authority_state: ContextAuthorityState
    context_sha256: Sha256 | None
    classification: CovariateClassification | None
    included_member_count: int | None = Field(ge=0, le=MAX_COVARIATE_MEMBERS)
    group_count: int | None = Field(ge=0, le=MAX_COVARIATE_MEMBERS)

    @model_validator(mode="after")
    def live_values_only_when_current(self) -> CovariateContextSelectorRecord:
        live = (
            self.context_sha256,
            self.classification,
            self.included_member_count,
            self.group_count,
        )
        if self.authority_state is ContextAuthorityState.STALE:
            if any(item is not None for item in live):
                raise ValueError("a stale D10 selector cannot carry live values")
            return self
        if any(item is None for item in live):
            raise ValueError("a current D10 selector requires live values")
        if self.group_count > self.included_member_count:
            raise ValueError("D10 selector group count exceeds membership")
        return self


class CovariateContextSelectorPage(RegistryContract):
    schema_version: Literal["traceback.d10-context-selector-page.v1"] = (
        "traceback.d10-context-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_CONTEXTS)
    state_head_sha256: Sha256
    records: tuple[CovariateContextSelectorRecord, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: ContextSelectorId | None


class CovariateContextBackupObject(RegistryContract):
    schema_version: Literal["traceback.d10-context-backup-object.v1"] = (
        "traceback.d10-context-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class CovariateContextBackup(RegistryContract):
    schema_version: Literal["traceback.d10-context-backup.v1"] = (
        "traceback.d10-context-backup.v1"
    )
    metadata: CovariateContextRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_CONTEXTS)
    state_head_sha256: Sha256
    journal: tuple[CovariateContextJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_CONTEXTS
    )
    objects: tuple[CovariateContextBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_CONTEXTS
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(CovariateContextBackup)


def registered_context_object_bytes(value: RegisteredCovariateContextObject) -> bytes:
    """Return exact bounded canonical bytes for one stored D10 object."""

    return exact_model_bytes(
        value,
        RegisteredCovariateContextObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_context_object_from_bytes(
    content: bytes,
) -> RegisteredCovariateContextObject:
    try:
        # The bounded parse enforces every structural budget before validation;
        # strict D10 contracts then validate in JSON mode from the same bytes.
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredCovariateContextObject.model_validate_json(content)
        if registered_context_object_bytes(value) != content:
            raise ValueError("registered D10 object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered D10 object is not canonical") from None


def _canonical_backup_bytes(backup: CovariateContextBackup) -> bytes:
    return exact_model_bytes(
        backup,
        CovariateContextBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_CONTEXTS,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("D10 context registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("D10 context registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "D10 context registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("D10 context registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("D10 context registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_prefixed_hex(value: object, prefix: str, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == len(prefix) + length
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry object exceeds its bound"
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry object digest is invalid"
        )
    return content


def _selector_id(epoch: str, object_sha256: str) -> str:
    digest = hashlib.sha256(
        b"traceback-d10-context-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + object_sha256.encode("ascii")
    ).hexdigest()
    return f"d10_context_{digest[:40]}"


def _journal_entry_sha256(entry: CovariateContextJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-d10-context-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: CovariateContextRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-d10-context-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *, sequence: int, previous_entry_sha256: str, object_sha256: str, object_bytes: int
) -> CovariateContextJournalEntry:
    placeholder = CovariateContextJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return CovariateContextJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _object_binds_metadata(
    value: RegisteredCovariateContextObject,
    metadata: CovariateContextRegistryMetadata,
) -> bool:
    return (
        value.d09_registry_id,
        value.d09_registry_epoch_sha256,
        value.d03_registry_id,
        value.d03_registry_epoch_sha256,
    ) == (
        metadata.d09_registry_id,
        metadata.d09_registry_epoch_sha256,
        metadata.d03_registry_id,
        metadata.d03_registry_epoch_sha256,
    )


def _validate_backup(backup: CovariateContextBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise CovariateContextRegistryConflict(
            "D10 context registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise CovariateContextRegistryConflict(
                "D10 context registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            value = registered_context_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise CovariateContextRegistryConflict(
                "D10 context registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise CovariateContextRegistryConflict(
                "D10 context registry backup digest is invalid"
            )
        if not _object_binds_metadata(value, backup.metadata):
            raise CovariateContextRegistryConflict(
                "D10 context registry backup object is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise CovariateContextRegistryConflict(
            "D10 context registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise CovariateContextRegistryConflict(
            "D10 context registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise CovariateContextRegistryConflict(
                "D10 context registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise CovariateContextRegistryConflict(
            "D10 context registry backup state is invalid"
        )


def covariate_context_backup_from_bytes(content: bytes) -> CovariateContextBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise CovariateContextRegistryConflict(
            "D10 context registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_CONTEXTS,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = CovariateContextBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("D10 context registry backup is not canonical")
    except (TypeError, ValueError):
        raise CovariateContextRegistryConflict(
            "D10 context registry backup is invalid"
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


def _bound_authority_identity(
    d09_registry: object, decision_registry: object
) -> tuple[str, str, str, str]:
    """Return the exact D09/D03 registry identity this registry binds.

    Both registries must read the same D01 linkage store: a live context is
    only derivable when the D09 rebuild and the D03 replay observe one linkage
    snapshot.
    """

    if type(d09_registry) is not DenominatorPolicyRegistry:
        raise TypeError("D10 context registry requires the exact D09 registry type")
    if type(decision_registry) is not LongitudinalDecisionRegistry:
        raise TypeError("D10 context registry requires the exact D03 registry type")
    try:
        _PINNED_D09_INTEGRITY(d09_registry)
        _PINNED_D03_INTEGRITY(decision_registry)
        d09_state = object.__getattribute__(d09_registry, "__dict__")
        d03_state = object.__getattribute__(decision_registry, "__dict__")
        cohort_registry = d09_state["_cohort_registry"]
        d09_linkage = object.__getattribute__(cohort_registry, "_linkage_store")
        d09_metadata = d09_state["_metadata"]
        d03_metadata = d03_state["_metadata"]
    except Exception:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry D09/D03 authority is invalid"
        ) from None
    if d03_state.get("_linkage_store") is not d09_linkage:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry D09/D03 authority does not share one linkage store"
        )
    identity = (
        d09_metadata.registry_id,
        d09_metadata.registry_epoch_sha256,
        d03_metadata.registry_id,
        d03_metadata.registry_epoch_sha256,
    )
    if (
        not _is_prefixed_hex(identity[0], "d09_registry_", 32)
        or not _is_sha256(identity[1])
        or not _is_prefixed_hex(identity[2], "d03_registry_", 32)
        or not _is_sha256(identity[3])
    ):
        raise CovariateContextRegistryUnsafe(
            "D10 context registry D09/D03 authority is invalid"
        )
    return identity


def _registry_instance_snapshot(
    registry: CovariateContextRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_d09_registry",
        "_decision_registry",
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
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            CovariateContextRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
            ) from None
        if persisted != metadata_bytes:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
            )
        root_descriptor = instance.get("_root_fd")
        if type(root_descriptor) is not int:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
            )
        try:
            root_observed = os.fstat(root_descriptor)
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry authority state changed"
            )
    if (
        type(instance["_d09_registry"]) is not DenominatorPolicyRegistry
        or type(instance["_decision_registry"]) is not LongitudinalDecisionRegistry
    ):
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        )
    return (
        id(instance["root"]),
        id(instance["_d09_registry"]),
        id(instance["_decision_registry"]),
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


def _seal_registry_instance(registry: CovariateContextRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class CovariateContextRegistry:
    """Descriptor-relative immutable D10 context publication with live rebuild."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "backup_bytes",
            "close",
            "list_selectors",
            "register_context",
            "resolve",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        d09_registry: DenominatorPolicyRegistry,
        decision_registry: LongitudinalDecisionRegistry,
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
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
                or not _is_prefixed_hex(expected_registry_id, "d10_registry_", 32)
                or not _is_sha256(expected_registry_epoch_sha256)
                or not _is_sha256(expected_state_head_sha256)
            ):
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry expected identity or head is invalid"
                )
        bound_identity = _bound_authority_identity(d09_registry, decision_registry)
        self.root = _snapshot_path(root)
        self._d09_registry = d09_registry
        self._decision_registry = decision_registry
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry root must be private"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry root changed"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry objects are unsafe"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry lock is unsafe"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry journal is unsafe"
                )
            self._journal_identity = (
                journal_metadata.st_dev,
                journal_metadata.st_ino,
            )
            with _CR_LOCK(self, exclusive=True):
                self._metadata = _CR_LOAD_OR_CREATE_METADATA(
                    self, bound_identity, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._root_identity[0],
                    self._root_identity[1],
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _CR_RECOVER_TEMPORARY_OBJECTS(self)
                _, head = _CR_LOAD_STATE(self, check_trusted_head=False)
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise CovariateContextRegistryUnsafe(
                            "new D10 context registry cannot inherit an expected "
                            "identity"
                        )
                elif any(item is None for item in expected_values):
                    raise CovariateContextRegistryUnsafe(
                        "D10 context registry expected identity and head are required"
                    )
                if not root_created and expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise CovariateContextRegistryUnsafe(
                        "D10 context registry expected identity or head is invalid"
                    )
                if staged_root is not None:
                    _commit_staged_root(staged_root, final_root, self._root_fd)
                    self.root = final_root
                self._trusted_head_sha256 = head
                _CR_ACCEPT_OBSERVED_HEAD(
                    self, _CR_LOAD_JOURNAL(self), head, check_instance=False
                )
                _seal_registry_instance(self)
                # Until here a failure still removes the new root by inode.
                staged_root = None
        except BaseException:
            if staged_root is not None:
                _discard_staged_root(staged_root, final_root, self._root_fd)
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

    def __enter__(self) -> CovariateContextRegistry:
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
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise CovariateContextRegistryUnsafe("D10 context registry is closed")
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
            raise CovariateContextRegistryUnsafe("D10 context registry is closed")
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry storage changed"
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry storage changed"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry storage changed"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root or objects directory is always unlinked under the exclusive
        # lock; a directory under that name makes unlink fail, so recovery
        # fails closed.
        if self._root_fd is None or self._objects_fd is None:
            raise CovariateContextRegistryUnsafe("D10 context registry is closed")
        try:
            for directory_fd in (self._root_fd, self._objects_fd):
                _remove_owned_temporaries(directory_fd)
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self,
        bound_identity: tuple[str, str, str, str],
        *,
        allow_create: bool,
    ) -> CovariateContextRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry metadata is missing"
                ) from None
            metadata = CovariateContextRegistryMetadata(
                registry_id=f"d10_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                d09_registry_id=bound_identity[0],
                d09_registry_epoch_sha256=bound_identity[1],
                d03_registry_id=bound_identity[2],
                d03_registry_epoch_sha256=bound_identity[3],
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
            return _CR_LOAD_OR_CREATE_METADATA(
                self, bound_identity, allow_create=False
            )
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                CovariateContextRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise CovariateContextRegistryUnsafe(
                "D10 context registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        if (
            metadata.d09_registry_id,
            metadata.d09_registry_epoch_sha256,
            metadata.d03_registry_id,
            metadata.d03_registry_epoch_sha256,
        ) != bound_identity:
            os.close(descriptor)
            self._metadata_fd = None
            raise CovariateContextRegistryUnsafe(
                "D10 context registry D09/D03 authority changed"
            )
        return metadata

    def _load_journal(self) -> tuple[CovariateContextJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise CovariateContextRegistryUnsafe("D10 context registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, 4 * 1024 * 1024)
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise CovariateContextRegistryUnsafe(
                "D10 context registry journal is incomplete"
            )
        entries: list[CovariateContextJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_CONTEXTS:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    CovariateContextJournalEntry, line
                )
            except Exception:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: CovariateContextJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise CovariateContextRegistryUnsafe("D10 context registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry journal append failed"
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
            raise CovariateContextRegistryUnsafe(
                "D10 context registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[CovariateContextJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
        *,
        check_trusted_head: bool = True,
    ) -> tuple[dict[str, tuple[RegisteredCovariateContextObject, bytes]], str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise CovariateContextRegistryUnsafe("D10 context registry is closed")
        journal = _CR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_CONTEXTS + 1:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise CovariateContextRegistryUnsafe(
                "D10 context registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise CovariateContextRegistryUnsafe(
                "D10 context registry committed objects are inconsistent"
            )
        loaded: dict[str, tuple[RegisteredCovariateContextObject, bytes]] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry journal binding is invalid"
                )
            try:
                value = registered_context_object_from_bytes(content)
            except ValueError:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry object is invalid"
                ) from None
            if not _object_binds_metadata(value, self._metadata):
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry object binding is invalid"
                )
            loaded[entry.object_sha256] = (value, content)
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _CR_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {
                self._genesis_head_sha256,
                *(item.entry_sha256 for item in journal),
            }
            if process_head is not None and process_head not in chain:
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry state rollback detected"
                )
        return loaded, head

    def _build_live(
        self, value: RegisteredCovariateContextObject
    ) -> LiveCovariateContext:
        """Derive one live context through the pinned D10 builder.

        The builder reads the D09 policy registry (which takes the D01, D05,
        D06 and result-trust fences itself) and then the D03 registry (which
        takes the D01 fence itself).  Neither can run inside a caller-held D01
        or D06 fence, so the D10 lock is the outer lock.  Any builder failure
        or binding drift means the stored inputs no longer derive a context
        from current authority.
        """

        try:
            live = _PINNED_BUILD_LIVE(
                value.covariates,
                d09_registry=self._d09_registry,
                d09_selector_id=value.d09_selector_id,
                d09_policy_version=value.d09_policy_version,
                decision_registry=self._decision_registry,
                series_selector_id=value.d03_series_selector_id,
                expected_d02_anchor_policy_sha256=value.d02_anchor_policy_sha256,
            )
            live = LiveCovariateContext.model_validate_json(_PINNED_LIVE_BYTES(live))
        except (
            DenominatorPolicyRegistryConflict,
            LongitudinalDecisionRegistryConflict,
            TypeError,
            ValueError,
        ):
            # Unsafe (tamper/integrity) failures from D09 or D03 propagate.
            raise CovariateContextRegistryStale(
                "D10 context no longer derives from live authority"
            ) from None
        population = live.d09_population
        series = live.d03_series
        if (
            population.registry_id != value.d09_registry_id
            or population.registry_epoch_sha256 != value.d09_registry_epoch_sha256
            or population.selector_id != value.d09_selector_id
            or population.policy_version != value.d09_policy_version
            or series.registry_id != value.d03_registry_id
            or series.registry_epoch_sha256 != value.d03_registry_epoch_sha256
            or series.selector_id != value.d03_series_selector_id
            or live.context.d02_anchor_policy_sha256 != value.d02_anchor_policy_sha256
        ):
            raise CovariateContextRegistryStale(
                "D10 context no longer derives from live authority"
            )
        return live

    def register_context(
        self,
        covariates: tuple[LiveCovariateMemberValues, ...],
        *,
        d09_selector_id: str,
        d09_policy_version: int,
        d03_series_selector_id: str,
        expected_d02_anchor_policy_sha256: str,
    ) -> CovariateContextRegistrationReceipt:
        """Bind one exact covariate set to one live D09 and D03 selection.

        No context is accepted.  One is derived through the pinned live
        builder to prove the inputs cover the live D09 population and its D03
        decisions before anything is published.
        """

        _require_registry_integrity(self)
        if (
            not _is_prefixed_hex(d09_selector_id, "d09_policy_", 40)
            or type(d09_policy_version) is not int
            or not 1 <= d09_policy_version <= 100_000
            or not _is_prefixed_hex(d03_series_selector_id, "d03_series_", 40)
            or not _is_sha256(expected_d02_anchor_policy_sha256)
        ):
            raise CovariateContextRegistryConflict(
                "D10 context registration selection is invalid"
            )
        try:
            captured = RegisteredCovariateContextObject(
                d09_registry_id=self._metadata.d09_registry_id,
                d09_registry_epoch_sha256=self._metadata.d09_registry_epoch_sha256,
                d09_selector_id=d09_selector_id,
                d09_policy_version=d09_policy_version,
                d03_registry_id=self._metadata.d03_registry_id,
                d03_registry_epoch_sha256=self._metadata.d03_registry_epoch_sha256,
                d03_series_selector_id=d03_series_selector_id,
                d02_anchor_policy_sha256=expected_d02_anchor_policy_sha256,
                covariates=_PINNED_CAPTURE_COVARIATES(covariates),
            )
            content = registered_context_object_bytes(captured)
            captured = registered_context_object_from_bytes(content)
        except Exception:
            raise CovariateContextRegistryConflict(
                "D10 context inputs are not exact canonical contracts"
            ) from None
        digest = hashlib.sha256(content).hexdigest()
        epoch = self._metadata.registry_epoch_sha256
        selector_id = _CR_SELECTOR_ID(epoch, digest)
        with _CR_LOCK(self, exclusive=True):
            try:
                live = _CR_BUILD_LIVE(self, captured)
            except CovariateContextRegistryStale:
                raise CovariateContextRegistryConflict(
                    "D10 context does not derive from the live D09/D03 selection"
                ) from None
            _CR_RECOVER_TEMPORARY_OBJECTS(self)
            loaded, head = _CR_LOAD_STATE(self)
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
                    raise CovariateContextRegistryConflict(
                        "D10 context object digest conflicts"
                    )
            else:
                if len(loaded) >= MAX_REGISTERED_CONTEXTS:
                    raise CovariateContextRegistryConflict(
                        "D10 context registry is full"
                    )
                if (
                    sum(len(item[1]) for item in loaded.values()) + len(content)
                    > MAX_TOTAL_OBJECT_BYTES
                ):
                    raise CovariateContextRegistryConflict(
                        "D10 context registry byte bound would be exceeded"
                    )
                try:
                    _CR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                except FileExistsError:
                    if _read_exact_object(self._objects_fd, digest) != content:
                        raise CovariateContextRegistryConflict(
                            "D10 context publication conflicts"
                        ) from None
                _CR_APPEND_JOURNAL(
                    self,
                    _build_journal_entry(
                        sequence=len(loaded) + 1,
                        previous_entry_sha256=head,
                        object_sha256=digest,
                        object_bytes=len(content),
                    ),
                )
            final, final_head = _CR_LOAD_STATE(self)
            if digest not in final or final[digest][1] != content:
                raise CovariateContextRegistryUnsafe(
                    "D10 context publication is unproven"
                )
            return _CR_RECEIPT(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(final),
                state_head_sha256=final_head,
                selector_id=selector_id,
                object_sha256=digest,
                context_sha256=_PINNED_CONTEXT_SHA256(live.context),
            )

    def resolve(self, selector_id: str) -> RegisteredLiveCovariateContext:
        """Return one context rebuilt from live D09/D03 authority, never a cache."""

        _require_registry_integrity(self)
        if not _is_prefixed_hex(selector_id, "d10_context_", 40):
            raise CovariateContextRegistryConflict("D10 context selector is invalid")
        with _CR_LOCK(self, exclusive=False):
            loaded, head = _CR_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            matches = [
                (digest, value)
                for digest, (value, _) in loaded.items()
                if _CR_SELECTOR_ID(epoch, digest) == selector_id
            ]
            if len(matches) != 1:
                raise CovariateContextRegistryConflict(
                    "D10 context selector is unavailable"
                )
            digest, value = matches[0]
            live = _CR_BUILD_LIVE(self, value)
            return _CR_RESOLVED(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                selector_id=selector_id,
                object_sha256=digest,
                context_sha256=_PINNED_CONTEXT_SHA256(live.context),
                live=live,
            )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        limit: int = 50,
    ) -> CovariateContextSelectorPage:
        """Return one bounded privacy-safe page with live authority state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise CovariateContextRegistryConflict(
                "D10 context selector page bound is invalid"
            )
        if after_selector_id is not None and not _is_prefixed_hex(
            after_selector_id, "d10_context_", 40
        ):
            raise CovariateContextRegistryConflict(
                "D10 context selector cursor is invalid"
            )
        with _CR_LOCK(self, exclusive=False):
            loaded, head = _CR_LOAD_STATE(self)
            epoch = self._metadata.registry_epoch_sha256
            ordered = sorted(
                (_CR_SELECTOR_ID(epoch, digest), digest, value)
                for digest, (value, _) in loaded.items()
            )
            if after_selector_id is not None:
                ordered = [item for item in ordered if item[0] > after_selector_id]
            selected = ordered[:limit]
            rows: list[CovariateContextSelectorRecord] = []
            for selector_id, digest, value in selected:
                try:
                    live = _CR_BUILD_LIVE(self, value)
                except CovariateContextRegistryStale:
                    live = None
                context = live.context if live is not None else None
                rows.append(
                    _CR_SELECTOR_RECORD(
                        selector_id=selector_id,
                        object_sha256=digest,
                        authority_state=(
                            ContextAuthorityState.STALE
                            if context is None
                            else ContextAuthorityState.CURRENT
                        ),
                        context_sha256=(
                            _PINNED_CONTEXT_SHA256(context)
                            if context is not None
                            else None
                        ),
                        classification=(
                            context.classification if context is not None else None
                        ),
                        included_member_count=(
                            len(context.included_member_sha256s)
                            if context is not None
                            else None
                        ),
                        group_count=(
                            len(context.groups) if context is not None else None
                        ),
                    )
                )
            more = len(ordered) > len(selected)
            return _CR_SELECTOR_PAGE(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=epoch,
                state_version=len(loaded),
                state_head_sha256=head,
                records=tuple(rows),
                next_after_selector_id=rows[-1].selector_id if more and rows else None,
            )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _CR_LOCK(self, exclusive=False):
            loaded, head = _CR_LOAD_STATE(self)
            backup = CovariateContextBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_CR_LOAD_JOURNAL(self),
                objects=tuple(
                    CovariateContextBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise CovariateContextRegistryConflict(
                    "D10 context registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        d09_registry: DenominatorPolicyRegistry,
        decision_registry: LongitudinalDecisionRegistry,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> CovariateContextRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        backup = covariate_context_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise CovariateContextRegistryConflict(
                "D10 context registry backup expected head is invalid"
            )
        bound_identity = _bound_authority_identity(d09_registry, decision_registry)
        if (
            backup.metadata.d09_registry_id,
            backup.metadata.d09_registry_epoch_sha256,
            backup.metadata.d03_registry_id,
            backup.metadata.d03_registry_epoch_sha256,
        ) != bound_identity:
            raise CovariateContextRegistryConflict(
                "D10 context registry backup authority is invalid"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry restore parent changed"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry restore root changed"
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
                raise CovariateContextRegistryUnsafe(
                    "D10 context registry restore objects changed"
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
            # Reopen through the normal descriptor, inode, history, and D09/D03
            # binding checks before the restore counts as complete, so a target
            # that cannot open is removed instead of blocking a retry.
            restored = _CR_CONSTRUCT(
                target,
                d09_registry=d09_registry,
                decision_registry=decision_registry,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise CovariateContextRegistryConflict(
                "D10 context registry restore target already exists"
            ) from None
        except OSError:
            raise CovariateContextRegistryUnsafe(
                "D10 context registry restore failed"
            ) from None
        finally:
            if (
                created
                and not completed
                and root_fd is not None
                and parent_fd is not None
            ):
                staging_name = _bound_name(
                    parent_fd, root_fd, staging_name, target.name
                )
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
                CovariateContextRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                CovariateContextJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=4 * 1024 * 1024,
            max_entries=MAX_REGISTERED_CONTEXTS,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=CovariateContextRegistryUnsafe,
            label="D10 context registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: CovariateContextRegistry.__dict__[name]
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
            "_build_live",
            "register_context",
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
    if cls is not CovariateContextRegistry or any(
        CovariateContextRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise CovariateContextRegistryUnsafe("D10 context registry callable changed")


def _require_registry_integrity(registry: CovariateContextRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise CovariateContextRegistryUnsafe("D10 context registry callable changed")
    authority_sources = {
        "_PINNED_BUILD_LIVE": d10_module.build_live_covariate_context,
        "_PINNED_LIVE_BYTES": d10_module.live_covariate_context_bytes,
        "_PINNED_CAPTURE_COVARIATES": d10_module.capture_live_covariates,
        "_PINNED_CONTEXT_SHA256": d10_module.covariate_context_result_sha256,
        "_PINNED_D09_INTEGRITY": d09_registry_module._require_registry_integrity,
        "_PINNED_D03_INTEGRITY": d03_registry_module._require_registry_integrity,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority callable changed"
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
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise CovariateContextRegistryUnsafe(
            "D10 context registry authority state changed"
        )


_CR_CONSTRUCT = CovariateContextRegistry
_CR_CLOSE = CovariateContextRegistry.close
_CR_LOCK = CovariateContextRegistry._lock
_CR_VALIDATE_STORAGE = CovariateContextRegistry._validate_storage
_CR_PUBLISH = CovariateContextRegistry._publish
_CR_RECOVER_TEMPORARY_OBJECTS = CovariateContextRegistry._recover_temporary_objects
_CR_LOAD_OR_CREATE_METADATA = CovariateContextRegistry._load_or_create_metadata
_CR_LOAD_JOURNAL = CovariateContextRegistry._load_journal
_CR_APPEND_JOURNAL = CovariateContextRegistry._append_journal
_CR_ACCEPT_OBSERVED_HEAD = CovariateContextRegistry._accept_observed_head
_CR_LOAD_STATE = CovariateContextRegistry._load_state
_CR_BUILD_LIVE = CovariateContextRegistry._build_live
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another object's context.
_CR_RECEIPT = CovariateContextRegistrationReceipt
_CR_RESOLVED = RegisteredLiveCovariateContext
_CR_SELECTOR_RECORD = CovariateContextSelectorRecord
_CR_SELECTOR_PAGE = CovariateContextSelectorPage
_CR_SELECTOR_ID = _selector_id
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_BUILD_LIVE": _PINNED_BUILD_LIVE,
        "_PINNED_LIVE_BYTES": _PINNED_LIVE_BYTES,
        "_PINNED_CAPTURE_COVARIATES": _PINNED_CAPTURE_COVARIATES,
        "_PINNED_CONTEXT_SHA256": _PINNED_CONTEXT_SHA256,
        "_PINNED_D09_INTEGRITY": _PINNED_D09_INTEGRITY,
        "_PINNED_D03_INTEGRITY": _PINNED_D03_INTEGRITY,
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
            "_CR_BUILD_LIVE",
            "_CR_RECEIPT",
            "_CR_RESOLVED",
            "_CR_SELECTOR_RECORD",
            "_CR_SELECTOR_PAGE",
            "_CR_SELECTOR_ID",
        )
    }
)


__all__ = [
    "ContextAuthorityState",
    "CovariateContextBackup",
    "CovariateContextBackupObject",
    "CovariateContextJournalEntry",
    "CovariateContextRegistrationReceipt",
    "CovariateContextRegistry",
    "CovariateContextRegistryConflict",
    "CovariateContextRegistryError",
    "CovariateContextRegistryMetadata",
    "CovariateContextRegistryStale",
    "CovariateContextRegistryUnsafe",
    "CovariateContextSelectorPage",
    "CovariateContextSelectorRecord",
    "RegisteredCovariateContextObject",
    "RegisteredLiveCovariateContext",
    "covariate_context_backup_from_bytes",
    "registered_context_object_bytes",
    "registered_context_object_from_bytes",
]
