"""Protected, forward-only result-trust authority for development result keys.

The registry is an append-only, hash-chained journal of two trust events:
adding one public development result key, and revoking one key ID.  Revocation
is permanent: a revoked key ID can never be re-added or un-revoked, and a
journal that tries either fails closed on load.  Reopening requires the
independently retained registry ID, epoch, and head, and a process-wide head
fence rejects any older journal for the same registry identity, so an old
trust state cannot be replayed as current.

Readers take ``read_fence``, which holds the registry's shared lock while the
caller derives and returns its result, and receive one
``ResultTrustSnapshot`` binding registry identity, state version, head, and the
current public ``DevelopmentTrustDocument``.
"""

from __future__ import annotations

import base64
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

import traceback_runner.signing as signing_module
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.repeatability_comparison import MAX_RESULT_TRUST_KEYS
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
from traceback_runner.signing import (
    DevelopmentTrustDocument,
    KeyPurpose,
    PublicTrustedKey,
    SigningError,
    TrustedKey,
    TrustNamespace,
    TrustStore,
    development_trust_document_bytes,
    load_development_trust,
    trusted_key_id,
)

# D07 bounds one result trust document to MAX_RESULT_TRUST_KEYS keys; the
# registry never grows a document past that, revoked keys included.
MAX_TRUST_KEYS = MAX_RESULT_TRUST_KEYS
MAX_TRUST_EVENTS = 256
# Revocations of never-added IDs (tombstones) have their own bound, so they can
# never use up the journal capacity reserved for adding and then revoking every
# key: 32 additions + 32 revocations + 192 tombstones = 256 events.
MAX_TRUST_TOMBSTONES = MAX_TRUST_EVENTS - 2 * MAX_TRUST_KEYS
MAX_JOURNAL_BYTES = 256 * 1024
MAX_BACKUP_BYTES = 512 * 1024
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()
# Keyed by registry identity alone, not by root inode: a restored copy of the
# same registry in another directory is held to the same forward-only head.
_REGISTRY_PROCESS_HEADS: dict[tuple[str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_LOCK_DEPTH = threading.local()
_PINNED_TRUSTED_KEY_ID = trusted_key_id
_PINNED_TRUST_DOCUMENT_BYTES = development_trust_document_bytes
_PINNED_LOAD_TRUST = load_development_trust

RegistryId = Annotated[
    str, StringConstraints(pattern=r"^result_trust_registry_[0-9a-f]{32}$")
]
ResultKeyId = Annotated[str, StringConstraints(pattern=r"^dev-result-[0-9a-f]{24}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PublicKeyBase64 = Annotated[str, StringConstraints(min_length=44, max_length=44)]
_REGISTRY_ID_PREFIX = "result_trust_registry_"


class ResultTrustRegistryError(RuntimeError):
    """Sanitized result-trust registry failure."""


class ResultTrustRegistryConflict(ResultTrustRegistryError):
    pass


class ResultTrustRegistryUnsafe(ResultTrustRegistryError):
    pass


class ResultTrustEventKind(StrEnum):
    ADD_KEY = "add_key"
    REVOKE_KEY = "revoke_key"


class ResultTrustRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.result-trust-registry-metadata.v1"] = (
        "traceback.result-trust-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    ResultTrustRegistryMetadata
)


def _decoded_public_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("result trust public key encoding is invalid") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("result trust public key encoding is invalid")
    return decoded


class ResultTrustJournalEntry(RegistryContract):
    """One committed trust event; revocations carry no public key."""

    schema_version: Literal["traceback.result-trust-journal-entry.v1"] = (
        "traceback.result-trust-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_TRUST_EVENTS, strict=True)
    previous_entry_sha256: Sha256
    event: ResultTrustEventKind
    namespace: Literal[TrustNamespace.DEVELOPMENT_SYNTHETIC] = (
        TrustNamespace.DEVELOPMENT_SYNTHETIC
    )
    purpose: Literal[KeyPurpose.RESULT] = KeyPurpose.RESULT
    key_id: ResultKeyId
    public_key_base64: PublicKeyBase64 | None
    entry_sha256: Sha256

    @model_validator(mode="after")
    def exact_event(self) -> ResultTrustJournalEntry:
        if self.event is ResultTrustEventKind.REVOKE_KEY:
            if self.public_key_base64 is not None:
                raise ValueError("a revocation carries no public key")
            return self
        if self.public_key_base64 is None:
            raise ValueError("a key addition requires its public key")
        public_key = _decoded_public_key(self.public_key_base64)
        if self.key_id != trusted_key_id(
            public_key, KeyPurpose.RESULT, namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC
        ):
            raise ValueError("result trust key identifier does not match its key")
        return self


class ResultTrustEventReceipt(RegistryContract):
    schema_version: Literal["traceback.result-trust-event-receipt.v1"] = (
        "traceback.result-trust-event-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_TRUST_EVENTS)
    state_head_sha256: Sha256
    event: ResultTrustEventKind
    key_id: ResultKeyId
    applied: bool
    document_sha256: Sha256


class ResultTrustSnapshot(RegistryContract):
    """Current public result trust at one exact forward-only registry head.

    ``document_sha256`` is the SHA-256 of
    ``development_trust_document_bytes(document)``, the exact bytes
    ``load_development_trust`` accepts.
    """

    schema_version: Literal["traceback.result-trust-snapshot.v1"] = (
        "traceback.result-trust-snapshot.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_TRUST_EVENTS)
    state_head_sha256: Sha256
    document: DevelopmentTrustDocument
    document_sha256: Sha256
    synthetic_only: Literal[True] = True

    @model_validator(mode="after")
    def exact_document(self) -> ResultTrustSnapshot:
        key_ids = tuple(key.key_id for key in self.document.keys)
        if (
            len(key_ids) > MAX_TRUST_KEYS
            or key_ids != tuple(sorted(set(key_ids)))
            or any(key.purpose is not KeyPurpose.RESULT for key in self.document.keys)
        ):
            raise ValueError("result trust snapshot keys are invalid")
        if self.document_sha256 != _document_sha256(self.document):
            raise ValueError("result trust snapshot digest is invalid")
        return self


class ResultTrustBackup(RegistryContract):
    schema_version: Literal["traceback.result-trust-backup.v1"] = (
        "traceback.result-trust-backup.v1"
    )
    metadata: ResultTrustRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_TRUST_EVENTS)
    state_head_sha256: Sha256
    journal: tuple[ResultTrustJournalEntry, ...] = Field(max_length=MAX_TRUST_EVENTS)


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(ResultTrustBackup)


def _document_sha256(document: DevelopmentTrustDocument) -> str:
    return hashlib.sha256(_PINNED_TRUST_DOCUMENT_BYTES(document)).hexdigest()


def project_result_trust_document(
    document: DevelopmentTrustDocument, key_ids: tuple[str, ...]
) -> DevelopmentTrustDocument:
    """Return ``document`` restricted to ``key_ids``.

    Signature verification resolves only the key a signature names, so a
    document restricted to the keys a record's signatures name verifies that
    record exactly as the whole document does.  Binding the projection lets a
    trust event make stale only the records whose keys it changed.
    """

    wanted = set(key_ids)
    return DevelopmentTrustDocument(
        keys=tuple(key for key in document.keys if key.key_id in wanted)
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("result trust registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("result trust registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError(
            "result trust registry path must be an exact string or platform path"
        )
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("result trust registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("result trust registry path is invalid")
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
        and len(value) == len(_REGISTRY_ID_PREFIX) + 32
        and value.startswith(_REGISTRY_ID_PREFIX)
        and all(
            character in "0123456789abcdef"
            for character in value[len(_REGISTRY_ID_PREFIX) :]
        )
    )


def _is_result_key_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == len("dev-result-") + 24
        and value.startswith("dev-result-")
        and all(character in "0123456789abcdef" for character in value[11:])
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
            raise ResultTrustRegistryUnsafe("result trust registry file exceeds its bound")
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


def _journal_entry_sha256(entry: ResultTrustJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-result-trust-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: ResultTrustRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-result-trust-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *,
    sequence: int,
    previous_entry_sha256: str,
    event: ResultTrustEventKind,
    key_id: str,
    public_key_base64: str | None,
) -> ResultTrustJournalEntry:
    placeholder = ResultTrustJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        event=event,
        namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC,
        purpose=KeyPurpose.RESULT,
        key_id=key_id,
        public_key_base64=public_key_base64,
        entry_sha256="0" * 64,
    )
    return ResultTrustJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _fold_events(
    journal: tuple[ResultTrustJournalEntry, ...], genesis: str
) -> tuple[dict[str, tuple[str, bool]], frozenset[str], str]:
    """Replay the chain and the forward-only rules; any violation fails closed.

    Returns active and revoked added keys by ID, every revoked key ID
    (including revocations of never-added IDs), and the head.  No-op events
    are never journaled, so a re-add, a re-revoke, or an add after revocation
    in the journal is invalid rather than ignored.
    """

    keys: dict[str, tuple[str, bool]] = {}
    revoked: set[str] = set()
    previous = genesis
    for sequence, entry in enumerate(journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
        ):
            raise ValueError("result trust journal chain is invalid")
        if entry.event is ResultTrustEventKind.ADD_KEY:
            assert entry.public_key_base64 is not None
            if entry.key_id in revoked or entry.key_id in keys:
                raise ValueError("result trust journal re-adds a key")
            if len(keys) >= MAX_TRUST_KEYS:
                raise ValueError("result trust journal exceeds its key bound")
            keys[entry.key_id] = (entry.public_key_base64, False)
        else:
            if entry.key_id in revoked:
                raise ValueError("result trust journal re-revokes a key")
            if entry.key_id not in keys and len(revoked - keys.keys()) >= (
                MAX_TRUST_TOMBSTONES
            ):
                raise ValueError("result trust journal exceeds its tombstone bound")
            revoked.add(entry.key_id)
            if entry.key_id in keys:
                keys[entry.key_id] = (keys[entry.key_id][0], True)
        previous = entry.entry_sha256
    return keys, frozenset(revoked), previous


def _document_from_keys(keys: dict[str, tuple[str, bool]]) -> DevelopmentTrustDocument:
    return DevelopmentTrustDocument(
        keys=tuple(
            PublicTrustedKey(
                key_id=key_id,
                purpose=KeyPurpose.RESULT,
                public_key_base64=public_key,
                revoked=revoked,
            )
            for key_id, (public_key, revoked) in sorted(keys.items())
        )
    )


def _canonical_backup_bytes(backup: ResultTrustBackup) -> bytes:
    return exact_model_bytes(
        backup,
        ResultTrustBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_TRUST_EVENTS * 16 + 64,
        max_depth=8,
        max_collection_items=MAX_TRUST_EVENTS,
        max_string_bytes=256,
    )


def result_trust_backup_from_bytes(content: bytes) -> ResultTrustBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise ResultTrustRegistryConflict("result trust registry backup exceeds its bound")
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=8,
            max_nodes=MAX_TRUST_EVENTS * 16 + 64,
            max_collection_items=MAX_TRUST_EVENTS,
            max_string_bytes=256,
        )
        backup = ResultTrustBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("result trust registry backup is not canonical")
        if backup.state_version != len(backup.journal):
            raise ValueError("result trust registry backup count is invalid")
        _, _, head = _fold_events(
            backup.journal, _metadata_genesis_sha256(backup.metadata)
        )
        if head != backup.state_head_sha256:
            raise ValueError("result trust registry backup head is invalid")
    except (TypeError, ValueError):
        raise ResultTrustRegistryConflict(
            "result trust registry backup is invalid"
        ) from None
    return backup


def _validated_public_key(key: object) -> PublicTrustedKey:
    """Return an exact, re-validated public result key or raise a conflict."""

    if type(key) is not PublicTrustedKey:
        raise ResultTrustRegistryConflict("result trust key must be a PublicTrustedKey")
    try:
        encoded = key.model_dump_json()
        replayed = PublicTrustedKey.model_validate_json(encoded)
        if replayed != key or replayed.model_dump_json() != encoded:
            raise ValueError("result trust key does not replay")
    except (TypeError, ValueError):
        raise ResultTrustRegistryConflict("result trust key is invalid") from None
    if replayed.namespace is not TrustNamespace.DEVELOPMENT_SYNTHETIC:
        raise ResultTrustRegistryConflict("result trust key namespace is invalid")
    if replayed.purpose is not KeyPurpose.RESULT:
        raise ResultTrustRegistryConflict("result trust key purpose must be result")
    if replayed.revoked:
        raise ResultTrustRegistryConflict("a revoked result trust key cannot be added")
    try:
        public_key = _decoded_public_key(replayed.public_key_base64)
        # TrustedKey rechecks length and the namespace- and purpose-bound ID.
        TrustedKey(
            key_id=replayed.key_id,
            purpose=replayed.purpose,
            public_key_bytes=public_key,
            namespace=replayed.namespace,
        )
        if replayed.key_id != _PINNED_TRUSTED_KEY_ID(
            public_key, KeyPurpose.RESULT, namespace=TrustNamespace.DEVELOPMENT_SYNTHETIC
        ):
            raise ValueError("result trust key identifier is invalid")
    except (TypeError, ValueError, SigningError):
        raise ResultTrustRegistryConflict(
            "result trust key identifier does not match its public key"
        ) from None
    return replayed


def _registry_instance_snapshot(registry: ResultTrustRegistry) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_root_identity",
        "_lock_identity",
        "_journal_identity",
        "_metadata_identity",
        "_process_lock",
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    unsafe = ResultTrustRegistryUnsafe("result trust registry authority state changed")
    if type(instance) is not dict or any(name not in instance for name in required):
        raise unsafe
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            ResultTrustRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise unsafe from None
    descriptors = tuple(
        instance.get(name) for name in ("_root_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    descriptor = instance.get("_metadata_fd")
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise unsafe
    elif type(descriptor) is not int or type(instance.get("_root_fd")) is not int:
        raise unsafe
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
            root_observed = os.fstat(instance["_root_fd"])
            metadata_observed = os.fstat(descriptor)
        except OSError:
            raise unsafe from None
        if persisted != metadata_bytes:
            raise unsafe
        if (
            instance["_root_identity"] != (root_observed.st_dev, root_observed.st_ino)
            or instance["_metadata_identity"]
            != (metadata_observed.st_dev, metadata_observed.st_ino)
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"]
            != (metadata.registry_id, metadata.registry_epoch_sha256)
        ):
            raise unsafe
    return (
        id(instance["root"]),
        instance["_root_identity"],
        instance["_lock_identity"],
        instance["_journal_identity"],
        instance["_metadata_identity"],
        id(instance["_process_lock"]),
        metadata_bytes,
        instance["_genesis_head_sha256"],
        instance["_head_key"],
        instance["_trusted_head_sha256"],
    )


def _seal_registry_instance(registry: ResultTrustRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


class ResultTrustRegistry:
    """Descriptor-relative, append-only, forward-only result-trust journal."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "add_key",
            "backup_bytes",
            "close",
            "current_trust",
            "current_trust_store",
            "read_fence",
            "revoke_key",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise ResultTrustRegistryUnsafe("result trust registry callable changed")
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
        if any(item is not None for item in expected_values) and (
            any(item is None for item in expected_values)
            or not _is_registry_id(expected_registry_id)
            or not _is_sha256(expected_registry_epoch_sha256)
            or not _is_sha256(expected_state_head_sha256)
        ):
            raise ResultTrustRegistryUnsafe(
                "result trust registry expected identity or head is invalid"
            )
        self.root = _snapshot_path(root)
        self._root_fd: int | None = None
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
                raise ResultTrustRegistryUnsafe(
                    "result trust registry root must be private"
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
                raise ResultTrustRegistryUnsafe("result trust registry root changed")
            self._root_identity = (bound.st_dev, bound.st_ino)
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
                raise ResultTrustRegistryUnsafe("result trust registry lock is unsafe")
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
                raise ResultTrustRegistryUnsafe(
                    "result trust registry journal is unsafe"
                )
            self._journal_identity = (journal_metadata.st_dev, journal_metadata.st_ino)
            with _RT_LOCK(self, exclusive=True):
                self._metadata = _RT_LOAD_OR_CREATE_METADATA(
                    self, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _RT_RECOVER_TEMPORARY_FILES(self)
                journal = _RT_LOAD_JOURNAL(self)
                head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise ResultTrustRegistryUnsafe(
                            "new result trust registry cannot inherit an expected "
                            "identity"
                        )
                elif expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise ResultTrustRegistryUnsafe(
                        "result trust registry expected identity and head are "
                        "required and must match"
                    )
                if staged_root is not None:
                    _commit_staged_root(staged_root, final_root, self._root_fd)
                    self.root = final_root
                self._trusted_head_sha256 = head
                _RT_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=False)
                _seal_registry_instance(self)
                # Until here a failure still removes the new root by inode.
                staged_root = None
        except BaseException:
            if staged_root is not None:
                _discard_staged_root(staged_root, final_root, self._root_fd)
            for name in ("_journal_fd", "_metadata_fd", "_lock_fd", "_root_fd"):
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
            for name in ("_journal_fd", "_metadata_fd", "_lock_fd", "_root_fd"):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)

    def __enter__(self) -> ResultTrustRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _RT_CLOSE(self)

    def __del__(self) -> None:
        try:
            _RT_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        # flock is per open file description and converts in place, so a nested
        # acquisition on one thread would silently upgrade or release the outer
        # lock.  Refuse it instead.
        if getattr(_LOCK_DEPTH, "value", 0):
            raise ResultTrustRegistryUnsafe(
                "result trust registry lock is not reentrant"
            )
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise ResultTrustRegistryUnsafe("result trust registry is closed")
            _LOCK_DEPTH.value = 1
            try:
                try:
                    fcntl.flock(
                        descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    )
                except OSError:
                    raise ResultTrustRegistryUnsafe(
                        "result trust registry lock is unavailable"
                    ) from None
                try:
                    _RT_VALIDATE_STORAGE(self)
                    yield
                    _RT_VALIDATE_STORAGE(self)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                _LOCK_DEPTH.value = 0

    def _validate_storage(self) -> None:
        if self._root_fd is None or self._lock_fd is None or self._journal_fd is None:
            raise ResultTrustRegistryUnsafe("result trust registry is closed")
        names = [
            (".registry.lock", self._lock_fd, self._lock_identity),
            ("registry-journal.jsonl", self._journal_fd, self._journal_identity),
        ]
        if self._metadata_fd is not None:
            names.append(
                ("registry-metadata.json", self._metadata_fd, self._metadata_identity)
            )
        try:
            root_path = os.stat(self.root, follow_symlinks=False)
            root_bound = os.fstat(self._root_fd)
            observed = [
                (
                    os.stat(name, dir_fd=self._root_fd, follow_symlinks=False),
                    os.fstat(descriptor),
                    identity,
                )
                for name, descriptor, identity in names
            ]
        except OSError:
            raise ResultTrustRegistryUnsafe(
                "result trust registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (root_bound.st_dev, root_bound.st_ino) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
            or any(
                not stat.S_ISREG(path.st_mode)
                or (path.st_dev, path.st_ino) != identity
                or (bound.st_dev, bound.st_ino) != identity
                or stat.S_IMODE(bound.st_mode) != 0o600
                or bound.st_uid != os.geteuid()
                for path, bound, identity in observed
            )
        ):
            raise ResultTrustRegistryUnsafe("result trust registry storage changed")

    def _recover_temporary_files(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root is always unlinked under the exclusive lock; a directory under
        # that name makes unlink fail, so recovery fails closed.
        if self._root_fd is None:
            raise ResultTrustRegistryUnsafe("result trust registry is closed")
        try:
            _remove_owned_temporaries(self._root_fd)
        except OSError:
            raise ResultTrustRegistryUnsafe(
                "result trust registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> ResultTrustRegistryMetadata:
        assert self._root_fd is not None
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise ResultTrustRegistryUnsafe(
                    "result trust registry metadata is missing"
                ) from None
            metadata = ResultTrustRegistryMetadata(
                registry_id=f"{_REGISTRY_ID_PREFIX}{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
            )
            try:
                _publish_file(
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return _RT_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise ResultTrustRegistryUnsafe(
                    "result trust registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
            metadata = contract_from_canonical_bytes(
                ResultTrustRegistryMetadata, content
            )
        except ResultTrustRegistryUnsafe:
            os.close(descriptor)
            raise
        except Exception:
            os.close(descriptor)
            raise ResultTrustRegistryUnsafe(
                "result trust registry metadata is invalid"
            ) from None
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        return metadata

    def _load_journal(self) -> tuple[ResultTrustJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ResultTrustRegistryUnsafe("result trust registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, MAX_JOURNAL_BYTES)
        except OSError:
            raise ResultTrustRegistryUnsafe(
                "result trust registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise ResultTrustRegistryUnsafe("result trust registry journal is incomplete")
        lines = content.splitlines()
        if len(lines) > MAX_TRUST_EVENTS:
            raise ResultTrustRegistryUnsafe(
                "result trust registry journal bound exceeded"
            )
        try:
            journal = tuple(
                contract_from_canonical_bytes(ResultTrustJournalEntry, line)
                for line in lines
            )
            _fold_events(journal, self._genesis_head_sha256)
        except Exception:
            raise ResultTrustRegistryUnsafe(
                "result trust registry journal is invalid"
            ) from None
        return journal

    def _append_journal(self, entry: ResultTrustJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ResultTrustRegistryUnsafe("result trust registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise ResultTrustRegistryUnsafe(
                "result trust registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException as error:
            # Remove any torn suffix so the committed chain stays readable,
            # including when an interrupt lands between partial writes.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            if not isinstance(error, OSError):
                raise
            raise ResultTrustRegistryUnsafe(
                "result trust registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[ResultTrustJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise ResultTrustRegistryUnsafe(
                "result trust registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise ResultTrustRegistryUnsafe(
                "result trust registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self,
    ) -> tuple[
        tuple[ResultTrustJournalEntry, ...],
        dict[str, tuple[str, bool]],
        frozenset[str],
        str,
    ]:
        journal = _RT_LOAD_JOURNAL(self)
        keys, revoked, head = _fold_events(journal, self._genesis_head_sha256)
        _RT_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        return journal, keys, revoked, head

    def _snapshot_locked(self) -> ResultTrustSnapshot:
        journal, keys, _, head = _RT_LOAD_STATE(self)
        document = _document_from_keys(keys)
        return _RT_SNAPSHOT(
            registry_id=self._metadata.registry_id,
            registry_epoch_sha256=self._metadata.registry_epoch_sha256,
            state_version=len(journal),
            state_head_sha256=head,
            document=document,
            document_sha256=_document_sha256(document),
        )

    def _append_event(
        self,
        event: ResultTrustEventKind,
        key_id: str,
        public_key_base64: str | None,
    ) -> ResultTrustEventReceipt:
        with _RT_LOCK(self, exclusive=True):
            _RT_RECOVER_TEMPORARY_FILES(self)
            journal, keys, revoked, head = _RT_LOAD_STATE(self)
            if event is ResultTrustEventKind.ADD_KEY:
                if key_id in revoked:
                    raise ResultTrustRegistryConflict(
                        "result trust key is revoked and cannot be re-added"
                    )
                existing = keys.get(key_id)
                if existing is not None and existing != (public_key_base64, False):
                    raise ResultTrustRegistryConflict(
                        "result trust key conflicts with its existing entry"
                    )
                applied = existing is None
                if applied and len(keys) >= MAX_TRUST_KEYS:
                    raise ResultTrustRegistryConflict("result trust registry is full")
            else:
                applied = key_id not in revoked
                if (
                    applied
                    and key_id not in keys
                    and len(revoked - keys.keys()) >= MAX_TRUST_TOMBSTONES
                ):
                    # Revoking an added key always fits; only tombstones for
                    # never-added IDs are refused at their own bound.
                    raise ResultTrustRegistryConflict(
                        "result trust registry tombstone bound reached"
                    )
            if applied:
                if len(journal) >= MAX_TRUST_EVENTS:
                    raise ResultTrustRegistryConflict(
                        "result trust registry event bound reached"
                    )
                _RT_APPEND_JOURNAL(
                    self,
                    _build_journal_entry(
                        sequence=len(journal) + 1,
                        previous_entry_sha256=head,
                        event=event,
                        key_id=key_id,
                        public_key_base64=public_key_base64,
                    ),
                )
            snapshot = _RT_SNAPSHOT_LOCKED(self)
            return _RT_RECEIPT(
                registry_id=snapshot.registry_id,
                registry_epoch_sha256=snapshot.registry_epoch_sha256,
                state_version=snapshot.state_version,
                state_head_sha256=snapshot.state_head_sha256,
                event=event,
                key_id=key_id,
                applied=applied,
                document_sha256=snapshot.document_sha256,
            )

    def add_key(self, key: PublicTrustedKey) -> ResultTrustEventReceipt:
        """Append one active public result key; identical re-adds are no-ops.

        A revoked key ID can never be re-added, and any other entry for an
        existing key ID is rejected.  Only ``development-synthetic`` result
        keys are accepted.
        """

        _require_registry_integrity(self)
        validated = _validated_public_key(key)
        return _RT_APPEND_EVENT(
            self,
            ResultTrustEventKind.ADD_KEY,
            validated.key_id,
            validated.public_key_base64,
        )

    def revoke_key(self, key_id: str) -> ResultTrustEventReceipt:
        """Permanently revoke one result key ID; needs no further authority.

        Revoking an already revoked ID is a no-op.  Revoking an ID that was
        never added records a permanent tombstone, so it can never be added.
        """

        _require_registry_integrity(self)
        if not _is_result_key_id(key_id):
            raise ResultTrustRegistryConflict("result trust key identifier is invalid")
        return _RT_APPEND_EVENT(self, ResultTrustEventKind.REVOKE_KEY, key_id, None)

    @contextmanager
    def read_fence(self) -> Iterator[ResultTrustSnapshot]:
        """Hold the shared registry lock while the caller uses current trust.

        No trust event can commit until the ``with`` body exits, so a caller
        that derives and constructs its return value inside the body returns a
        value consistent with the yielded head.
        """

        _require_registry_integrity(self)
        with _RT_LOCK(self, exclusive=False):
            yield _RT_SNAPSHOT_LOCKED(self)
            _require_registry_integrity(self)

    def current_trust(self) -> ResultTrustSnapshot:
        """Return the current trust snapshot, built under the registry lock."""

        _require_registry_integrity(self)
        with _RT_LOCK(self, exclusive=False):
            return _RT_SNAPSHOT_LOCKED(self)

    def current_trust_store(self) -> tuple[ResultTrustSnapshot, TrustStore]:
        """Return a fresh ``TrustStore`` and the snapshot it was built from."""

        _require_registry_integrity(self)
        with _RT_LOCK(self, exclusive=False):
            snapshot = _RT_SNAPSHOT_LOCKED(self)
            store = _PINNED_LOAD_TRUST(_PINNED_TRUST_DOCUMENT_BYTES(snapshot.document))
            return snapshot, store

    def backup_bytes(self) -> bytes:
        """Return one canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _RT_LOCK(self, exclusive=False):
            journal, _, _, head = _RT_LOAD_STATE(self)
            backup = ResultTrustBackup(
                metadata=self._metadata,
                state_version=len(journal),
                state_head_sha256=head,
                journal=journal,
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise ResultTrustRegistryConflict(
                    "result trust registry backup exceeds its bound"
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
    ) -> ResultTrustRegistry:
        """Restore a verified bundle into one new private registry root.

        The restored registry keeps its identity, so the process-wide head
        fence rejects restoring a backup older than a head this process saw.
        """

        _require_registry_class_integrity(cls)
        backup = result_trust_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise ResultTrustRegistryConflict(
                "result trust registry backup expected head is invalid"
            )
        target = _snapshot_path(root)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd: int | None = None
        root_fd: int | None = None
        created = False
        staging_name = target.name
        completed = False
        try:
            parent_lstat = os.stat(target.parent, follow_symlinks=False)
            parent_fd = os.open(target.parent, directory_flags)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise ResultTrustRegistryUnsafe(
                    "result trust registry restore parent changed"
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
                raise ResultTrustRegistryUnsafe(
                    "result trust registry restore root changed"
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
            _publish_file(
                root_fd,
                "registry-journal.jsonl",
                b"".join(
                    canonical_contract_bytes(entry) + b"\n" for entry in backup.journal
                ),
            )
            os.fsync(root_fd)
            os.fsync(parent_fd)
            # The staged root becomes the target only once it is complete.
            _commit_staging_directory(parent_fd, staging_name, target.name, root_fd)
            staging_name = target.name
            # Construction rechecks the process head fence; if it fails, the
            # published target is removed below so the restore can be retried.
            restored = _RT_CONSTRUCT(
                target,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise ResultTrustRegistryConflict(
                "result trust registry restore target already exists"
            ) from None
        except OSError:
            raise ResultTrustRegistryUnsafe("result trust registry restore failed") from None
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
                try:
                    if root_fd is not None:
                        for entry in os.listdir(root_fd):
                            os.unlink(entry, dir_fd=root_fd)
                    if parent_fd is not None:
                        os.rmdir(staging_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except OSError:
                    pass
            for descriptor in (root_fd, parent_fd):
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
                ResultTrustRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                ResultTrustJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=MAX_JOURNAL_BYTES,
            max_entries=MAX_TRUST_EVENTS,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=ResultTrustRegistryUnsafe,
            label="result trust registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: ResultTrustRegistry.__dict__[name]
        for name in (
            "__getattribute__",
            "__init__",
            "__enter__",
            "__exit__",
            "_lock",
            "_validate_storage",
            "_recover_temporary_files",
            "_load_or_create_metadata",
            "_load_journal",
            "_append_journal",
            "_accept_observed_head",
            "_load_state",
            "_snapshot_locked",
            "_append_event",
            "add_key",
            "revoke_key",
            "read_fence",
            "current_trust",
            "current_trust_store",
            "backup_bytes",
            "restore",
            "recover_torn_journal_tail",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not ResultTrustRegistry or any(
        ResultTrustRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise ResultTrustRegistryUnsafe("result trust registry callable changed")


def _require_registry_integrity(registry: ResultTrustRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise ResultTrustRegistryUnsafe("result trust registry callable changed")
    authority_sources = {
        "_PINNED_TRUSTED_KEY_ID": signing_module.trusted_key_id,
        "_PINNED_TRUST_DOCUMENT_BYTES": signing_module.development_trust_document_bytes,
        "_PINNED_LOAD_TRUST": signing_module.load_development_trust,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise ResultTrustRegistryUnsafe(
            "result trust registry authority callable changed"
        )
    instance = object.__getattribute__(registry, "__dict__")
    initialized_names = (
        "_metadata",
        "_genesis_head_sha256",
        "_head_key",
        "_trusted_head_sha256",
    )
    # A trust event updates the trusted head and then the instance seal under
    # this lock; compare them under it too, or a concurrent reader on another
    # thread can observe the head without its seal and fail spuriously.
    with _REGISTRY_PROCESS_LOCK:
        initialized = tuple(name in instance for name in initialized_names)
        if not any(initialized):
            return
        if not all(initialized):
            raise ResultTrustRegistryUnsafe(
                "result trust registry authority state changed"
            )
        expected = _REGISTRY_INSTANCE_SEALS.get(registry)
        if expected is None or _registry_instance_snapshot(registry) != expected:
            raise ResultTrustRegistryUnsafe(
                "result trust registry authority state changed"
            )


_RT_CONSTRUCT = ResultTrustRegistry
_RT_CLOSE = ResultTrustRegistry.close
_RT_LOCK = ResultTrustRegistry._lock
_RT_VALIDATE_STORAGE = ResultTrustRegistry._validate_storage
_RT_RECOVER_TEMPORARY_FILES = ResultTrustRegistry._recover_temporary_files
_RT_LOAD_OR_CREATE_METADATA = ResultTrustRegistry._load_or_create_metadata
_RT_LOAD_JOURNAL = ResultTrustRegistry._load_journal
_RT_APPEND_JOURNAL = ResultTrustRegistry._append_journal
_RT_ACCEPT_OBSERVED_HEAD = ResultTrustRegistry._accept_observed_head
_RT_LOAD_STATE = ResultTrustRegistry._load_state
_RT_SNAPSHOT_LOCKED = ResultTrustRegistry._snapshot_locked
_RT_APPEND_EVENT = ResultTrustRegistry._append_event
# Result constructors are sealed so a module-global replacement cannot pair a
# head with another document.
_RT_SNAPSHOT = ResultTrustSnapshot
_RT_RECEIPT = ResultTrustEventReceipt
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_TRUSTED_KEY_ID": _PINNED_TRUSTED_KEY_ID,
        "_PINNED_TRUST_DOCUMENT_BYTES": _PINNED_TRUST_DOCUMENT_BYTES,
        "_PINNED_LOAD_TRUST": _PINNED_LOAD_TRUST,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_RT_CONSTRUCT",
            "_RT_CLOSE",
            "_RT_LOCK",
            "_RT_VALIDATE_STORAGE",
            "_RT_RECOVER_TEMPORARY_FILES",
            "_RT_LOAD_OR_CREATE_METADATA",
            "_RT_LOAD_JOURNAL",
            "_RT_APPEND_JOURNAL",
            "_RT_ACCEPT_OBSERVED_HEAD",
            "_RT_LOAD_STATE",
            "_RT_SNAPSHOT_LOCKED",
            "_RT_APPEND_EVENT",
            "_RT_SNAPSHOT",
            "_RT_RECEIPT",
        )
    }
)


__all__ = [
    "MAX_TRUST_EVENTS",
    "MAX_TRUST_KEYS",
    "MAX_TRUST_TOMBSTONES",
    "ResultTrustBackup",
    "ResultTrustEventKind",
    "ResultTrustEventReceipt",
    "ResultTrustJournalEntry",
    "ResultTrustRegistry",
    "ResultTrustRegistryConflict",
    "ResultTrustRegistryError",
    "ResultTrustRegistryMetadata",
    "ResultTrustRegistryUnsafe",
    "ResultTrustSnapshot",
    "project_result_trust_document",
    "result_trust_backup_from_bytes",
]
