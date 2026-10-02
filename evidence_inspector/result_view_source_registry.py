"""Protected append-only registry for live-verified E06 result-view sources.

The registry is provider-local.  Callers never supply a ``ResultViewSource``,
a compatibility decision, or a member commitment.  Registration takes exact
records, a compatibility policy and pin, a denominator ledger, and labels;
under the live D06 record-status fence it derives the members from the D06
bindings, checks both records against their E04 catalog references, derives
the E05 decision with the binding's authority head, and builds the E06 source.
Every protected read repeats those checks under a fresh D06 fence before it
returns a source.  The selector projection carries only opaque selectors,
digests, and states.

The denominator ledger, labels, compatibility policy, and several record fields
have no authority source in this codebase.  They are bound immutably by digest
but are not verified; see ``CALLER_ASSERTED_FIELDS`` and
``docs/RESULT-VIEW-SOURCE-REGISTRY.md``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
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
from typing import Annotated, Literal, get_args

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.compatibility as e05_module
import evidence_inspector.result_view as e06_module
from evidence_inspector.cohort_import import (
    CohortImportError,
    CohortManifestRecordStatus,
    CohortRecordAvailability,
    CohortRecordBinding,
    CohortRecordCatalog,
)
from evidence_inspector.compatibility import (
    CompatibilityDecision,
    CompatibilityOutcome,
    CompatibilityPolicy,
    CompatibilityRequest,
    ExecutionState,
    TrustState,
    VerifiedMeasurementRecord,
    decide_compatibility,
)
from evidence_inspector.method_registry import (
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.result_catalog import (
    CatalogQualificationState,
    CatalogResultRef,
)
from evidence_inspector.result_view import (
    DenominatorLedger,
    ResultViewSource,
    bind_result_view_source,
)
from evidence_inspector.safe_ingress import (
    bounded_json_loads,
    contract_type_graph,
    exact_model_bytes,
)

MAX_REGISTERED_SOURCES = 10_000
MAX_SOURCE_VERSIONS = 16
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 64
MAX_OBJECT_GRAPH_NODES = 200_000
MAX_OBJECT_COLLECTION_ITEMS = 1_024
MAX_OBJECT_STRING_BYTES = 4_096
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256

# Fields of a resolved source that no live authority in this codebase can
# re-derive.  They are bound by digest (alteration is detected) but are not
# verified.  The list is a literal so that the claim is part of the contract.
CallerAssertedFields = Literal[
    "accessible_label",
    "compatibility_policy",
    "compatibility_policy_pin",
    "counterpart.bundle_id",
    "counterpart.compatibility_key.assets",
    "counterpart.compatibility_key.registered_policy",
    "counterpart.compatibility_key.result_schema",
    "counterpart.compatibility_key.semantics",
    "counterpart.current_capability.effective_approval_ref",
    "counterpart.current_capability.qualification_state_unknown_versus_unassigned",
    "counterpart.information_state",
    "counterpart.result_sha256",
    "denominator",
    "qc_label",
    "record.bundle_id",
    "record.compatibility_key.assets",
    "record.compatibility_key.registered_policy",
    "record.compatibility_key.result_schema",
    "record.compatibility_key.semantics",
    "record.current_capability.effective_approval_ref",
    "record.current_capability.qualification_state_unknown_versus_unassigned",
    "record.information_state",
    "record.result_sha256",
]
# The counterpart entries name fields of the second record the E05
# decision depends on; they are as unverified as the subject's.
CALLER_ASSERTED_FIELDS: tuple[str, ...] = get_args(CallerAssertedFields)

_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_STATUS_FENCE = CohortRecordCatalog.record_status_authority_fence
_PINNED_ACTIVE_SNAPSHOT = ProviderLinkageStore.active_snapshot
_PINNED_DECIDE = decide_compatibility
_PINNED_BIND_SOURCE = bind_result_view_source

RegistryId = Annotated[str, StringConstraints(pattern=r"^e06_registry_[0-9a-f]{32}$")]
SourceSelectorId = Annotated[
    str, StringConstraints(pattern=r"^e06_source_[0-9a-f]{40}$")
]
CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ResultViewSourceRegistryError(RuntimeError):
    """Sanitized registry failure."""


class ResultViewSourceRegistryConflict(ResultViewSourceRegistryError):
    pass


class ResultViewSourceRegistryStale(ResultViewSourceRegistryConflict):
    """A registered source no longer verifies against live authority."""


class ResultViewSourceRegistryUnsafe(ResultViewSourceRegistryError):
    pass


class SourceAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class ResultViewSourceRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.e06-source-registry-metadata.v1"] = (
        "traceback.e06-source-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    linkage_store_id: str = Field(pattern=r"^store_[0-9a-f]{32}$")
    linkage_store_epoch_sha256: Sha256
    linkage_storage_identity_sha256: Sha256
    catalog_storage_identity_sha256: Sha256
    catalog_reader_registry_sha256: Sha256
    record_catalog_scope_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    ResultViewSourceRegistryMetadata
)


class ResultViewSourceRegistryIdentity(RegistryContract):
    """Immutable registry identity and its bound D05 cohort registry.

    These values are fixed when the registry is opened and never change for
    the instance, so the read takes no lock and is safe inside any fence.  It
    carries no state head; heads change and are read under the registry lock.
    """

    schema_version: Literal["traceback.e06-source-registry-identity.v1"] = (
        "traceback.e06-source-registry-identity.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256


class RegisteredResultViewSourceObject(RegistryContract):
    """Protected stored inputs plus the source the registry derived from them."""

    schema_version: Literal["traceback.e06-registered-source-object.v1"] = (
        "traceback.e06-registered-source-object.v1"
    )
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    catalog_authority_sha256: Sha256
    member_sha256: Sha256
    binding_sha256: Sha256
    counterpart_member_sha256: Sha256
    counterpart_binding_sha256: Sha256
    compatibility_request: CompatibilityRequest
    source: ResultViewSource

    @model_validator(mode="after")
    def exact_bindings(self) -> RegisteredResultViewSourceObject:
        request = self.compatibility_request
        if request.left != self.source.record:
            raise ValueError("registered source record does not match its request")
        if request.right.result_id == request.left.result_id:
            raise ValueError("registered source counterpart must be distinct")
        if self.member_sha256 == self.counterpart_member_sha256:
            raise ValueError("registered source counterpart must be another member")
        if (
            request.trusted_authority_head_sha256
            != request.left.current_capability.authority_head_sha256
        ):
            raise ValueError("registered source authority pin is not the binding head")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredResultViewSourceObject
)


class ResultViewSourceJournalEntry(RegistryContract):
    schema_version: Literal["traceback.e06-source-journal-entry.v1"] = (
        "traceback.e06-source-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_SOURCES, strict=True)
    previous_entry_sha256: Sha256
    selector_id: SourceSelectorId
    source_version: int = Field(ge=1, le=MAX_SOURCE_VERSIONS, strict=True)
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class SourceRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.e06-source-registration-receipt.v1"] = (
        "traceback.e06-source-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_SOURCES)
    state_head_sha256: Sha256
    selector_id: SourceSelectorId
    source_version: int = Field(ge=1, le=MAX_SOURCE_VERSIONS)
    object_sha256: Sha256
    source_sha256: Sha256


class RegisteredResultViewSource(RegistryContract):
    """Protected source that re-verified against live D06/E04 authority."""

    schema_version: Literal["traceback.e06-registered-source.v1"] = (
        "traceback.e06-registered-source.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_SOURCES)
    state_head_sha256: Sha256
    selector_id: SourceSelectorId
    source_version: int = Field(ge=1, le=MAX_SOURCE_VERSIONS)
    object_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    record_status_sha256: Sha256
    catalog_authority_sha256: Sha256
    member_sha256: Sha256
    binding_sha256: Sha256
    catalog_result_sha256: Sha256
    counterpart_member_sha256: Sha256
    counterpart_binding_sha256: Sha256
    source_sha256: Sha256
    compatibility_decision_sha256: Sha256
    denominator_ledger_sha256: Sha256
    source_replay_sha256: Sha256
    source: ResultViewSource
    caller_asserted_fields: tuple[CallerAssertedFields, ...] = CALLER_ASSERTED_FIELDS
    denominator_verified: Literal[False] = False
    method_authority_head_current_verified: Literal[False] = False
    replayed_against_live_authority: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredResultViewSource:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256,
            self.cohort_registry_id,
            self.cohort_selector_id,
            self.cohort_version,
            self.member_sha256,
        ):
            raise ValueError("registered source selector does not match its member")
        if self.source_sha256 != _contract_sha256(self.source):
            raise ValueError("registered source digest is invalid")
        if self.compatibility_decision_sha256 != (
            self.source.compatibility_decision.decision_sha256
        ):
            raise ValueError("registered source decision digest is invalid")
        if self.denominator_ledger_sha256 != _contract_sha256(self.source.denominator):
            raise ValueError("registered source ledger digest is invalid")
        if self.caller_asserted_fields != CALLER_ASSERTED_FIELDS:
            raise ValueError("registered source unverified-field list is invalid")
        if self.source_replay_sha256 != _source_replay_sha256(self):
            raise ValueError("registered source replay digest is invalid")
        return self


class SourceSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.e06-source-selector-record.v1"] = (
        "traceback.e06-source-selector-record.v1"
    )
    selector_id: SourceSelectorId
    source_version: int = Field(ge=1, le=MAX_SOURCE_VERSIONS, strict=True)
    object_sha256: Sha256
    source_sha256: Sha256
    denominator_ledger_sha256: Sha256
    compatibility_outcome: CompatibilityOutcome
    authority_state: SourceAuthorityState


class SourceSelectorPage(RegistryContract):
    schema_version: Literal["traceback.e06-source-selector-page.v1"] = (
        "traceback.e06-source-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_SOURCES)
    state_head_sha256: Sha256
    records: tuple[SourceSelectorRecord, ...] = Field(max_length=MAX_SELECTOR_PAGE)
    next_after_selector_id: SourceSelectorId | None
    next_after_source_version: int | None = Field(
        default=None, ge=1, le=MAX_SOURCE_VERSIONS
    )

    @model_validator(mode="after")
    def coherent_cursor(self) -> SourceSelectorPage:
        if (self.next_after_selector_id is None) != (
            self.next_after_source_version is None
        ):
            raise ValueError("selector page cursor is incomplete")
        return self


class ResultViewSourceBackupObject(RegistryContract):
    schema_version: Literal["traceback.e06-source-backup-object.v1"] = (
        "traceback.e06-source-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class ResultViewSourceBackup(RegistryContract):
    schema_version: Literal["traceback.e06-source-backup.v1"] = (
        "traceback.e06-source-backup.v1"
    )
    metadata: ResultViewSourceRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_SOURCES)
    state_head_sha256: Sha256
    journal: tuple[ResultViewSourceJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_SOURCES
    )
    objects: tuple[ResultViewSourceBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_SOURCES
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(ResultViewSourceBackup)
_CAPTURE_TYPES = {
    model: contract_type_graph(model)
    for model in (
        VerifiedMeasurementRecord,
        CompatibilityPolicy,
        DenominatorLedger,
    )
}


def registered_source_object_bytes(value: RegisteredResultViewSourceObject) -> bytes:
    """Return exact bounded canonical bytes for one stored source object."""

    return exact_model_bytes(
        value,
        RegisteredResultViewSourceObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_source_object_from_bytes(
    content: bytes,
) -> RegisteredResultViewSourceObject:
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredResultViewSourceObject.model_validate_json(content)
        if registered_source_object_bytes(value) != content:
            raise ValueError("registered source object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered source object is not canonical") from None


def _capture(value: object, model: type) -> object:
    """Capture one caller contract as exact canonical bytes and re-parse it."""

    model_types, enum_types = _CAPTURE_TYPES[model]
    bounds = {
        "model_types": model_types,
        "enum_types": enum_types,
        "max_bytes": MAX_OBJECT_BYTES,
        "max_nodes": MAX_OBJECT_GRAPH_NODES,
        "max_depth": MAX_OBJECT_GRAPH_DEPTH,
        "max_collection_items": MAX_OBJECT_COLLECTION_ITEMS,
        "max_string_bytes": MAX_OBJECT_STRING_BYTES,
    }
    try:
        content = exact_model_bytes(value, model, **bounds)
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        captured = model.model_validate_json(content)
        if exact_model_bytes(captured, model, **bounds) != content:
            raise ValueError("caller contract is not canonical")
        return captured
    except Exception:
        raise ResultViewSourceRegistryConflict(
            "E06 source inputs are not exact canonical contracts"
        ) from None


def _canonical_backup_bytes(backup: ResultViewSourceBackup) -> bytes:
    return exact_model_bytes(
        backup,
        ResultViewSourceBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_SOURCES,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("E06 source registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("E06 source registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError("E06 source registry path must be an exact string or path")
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("E06 source registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("E06 source registry path is invalid")
    return path


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_token(value: object, prefix: str, hex_length: int) -> bool:
    return (
        type(value) is str
        and len(value) == len(prefix) + hex_length
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _is_result_id(value: object) -> bool:
    return _is_token(value, "result_", 40)


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
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry file exceeds its bound"
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
            raise ResultViewSourceRegistryUnsafe("E06 source registry object is unsafe")
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry object digest is invalid"
        )
    return content


def _selector_id(
    epoch: str,
    cohort_registry_id: str,
    cohort_selector_id: str,
    cohort_version: int,
    member_sha256: str,
) -> str:
    digest = hashlib.sha256(
        b"traceback-e06-source-selector-v1\0"
        + "\0".join(
            (
                epoch,
                cohort_registry_id,
                cohort_selector_id,
                str(cohort_version),
                member_sha256,
            )
        ).encode("ascii")
    ).hexdigest()
    return f"e06_source_{digest[:40]}"


def _object_selector_id(epoch: str, value: RegisteredResultViewSourceObject) -> str:
    return _selector_id(
        epoch,
        value.cohort_registry_id,
        value.cohort_selector_id,
        value.cohort_version,
        value.member_sha256,
    )


def _contract_sha256(value: RegistryContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def _source_replay_sha256(value: RegistryContract) -> str:
    """Digest every returned commitment except the digest itself.

    The source is covered through ``source_sha256``, which the validator binds
    to the embedded source, so no field can change without failing validation.
    """

    payload = value.model_dump(
        mode="json", exclude={"source", "source_replay_sha256"}
    )
    return hashlib.sha256(
        b"traceback-e06-source-replay-v2\0"
        + json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _journal_entry_sha256(entry: ResultViewSourceJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-e06-source-journal-v1\0" + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(metadata: ResultViewSourceRegistryMetadata) -> str:
    return hashlib.sha256(
        b"traceback-e06-source-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *,
    sequence: int,
    previous_entry_sha256: str,
    selector_id: str,
    source_version: int,
    object_sha256: str,
    object_bytes: int,
) -> ResultViewSourceJournalEntry:
    placeholder = ResultViewSourceJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        selector_id=selector_id,
        source_version=source_version,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return ResultViewSourceJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_journal_semantics(
    epoch: str,
    journal: tuple[ResultViewSourceJournalEntry, ...],
    objects: dict[str, RegisteredResultViewSourceObject],
) -> None:
    """Bind each entry's selector/version to its object and enforce uniqueness."""

    versions: dict[str, int] = {}
    slots: dict[tuple[str, str, int], dict[str, str]] = {}
    for entry in journal:
        value = objects[entry.object_sha256]
        selector = _object_selector_id(epoch, value)
        versions[selector] = versions.get(selector, 0) + 1
        if entry.selector_id != selector or entry.source_version != versions[selector]:
            raise ValueError("journal selector or version does not match its object")
        cohort = (
            value.cohort_registry_id,
            value.cohort_selector_id,
            value.cohort_version,
        )
        results = slots.setdefault(cohort, {})
        result_id = value.source.record.result_id
        member = results.get(result_id)
        if member is not None and member != value.member_sha256:
            raise ValueError("one result cannot represent two members")
        if any(
            other_member == value.member_sha256 and other_result != result_id
            for other_result, other_member in results.items()
        ):
            raise ValueError("one member cannot carry two results")
        results[result_id] = value.member_sha256


def _validate_backup(backup: ResultViewSourceBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    values: dict[str, RegisteredResultViewSourceObject] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            values[item.object_sha256] = registered_source_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup digest is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup state is invalid"
        )
    try:
        _validate_journal_semantics(
            backup.metadata.registry_epoch_sha256, backup.journal, values
        )
    except ValueError:
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup journal is invalid"
        ) from None


def result_view_source_backup_from_bytes(content: bytes) -> ResultViewSourceBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_SOURCES,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = ResultViewSourceBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("E06 source registry backup is not canonical")
    except (TypeError, ValueError):
        raise ResultViewSourceRegistryConflict(
            "E06 source registry backup is invalid"
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


def _new_metadata(
    authority: MappingProxyType, linkage: object
) -> ResultViewSourceRegistryMetadata:
    return ResultViewSourceRegistryMetadata(
        registry_id=f"e06_registry_{secrets.token_hex(16)}",
        registry_epoch_sha256=secrets.token_hex(32),
        cohort_registry_id=authority["_cohort_registry_id"],
        cohort_registry_epoch_sha256=authority["_cohort_registry_epoch_sha256"],
        linkage_store_id=linkage.store_id,
        linkage_store_epoch_sha256=linkage.store_epoch_sha256,
        linkage_storage_identity_sha256=linkage.storage_identity_sha256,
        catalog_storage_identity_sha256=authority["_catalog_storage_identity_sha256"],
        catalog_reader_registry_sha256=authority["_catalog_reader_identity_sha256"],
        record_catalog_scope_sha256=authority["_recovery_scope_sha256"],
    )


def _authority_identity(record_catalog: CohortRecordCatalog) -> dict[str, object]:
    """Read the D06 catalog's own pinned identities without invoking hooks."""

    if type(record_catalog) is not CohortRecordCatalog:
        raise TypeError("E06 source registry requires the exact D06 record catalog")
    state = object.__getattribute__(record_catalog, "__dict__")
    names = (
        "_cohort_registry",
        "_cohort_registry_id",
        "_cohort_registry_epoch_sha256",
        "_linkage_store",
        "_catalog_storage_identity_sha256",
        "_catalog_reader_identity_sha256",
        "_recovery_scope_sha256",
    )
    if type(state) is not dict or any(name not in state for name in names):
        raise ResultViewSourceRegistryUnsafe("D06 record catalog identity is invalid")
    if type(state["_linkage_store"]) is not ProviderLinkageStore:
        raise ResultViewSourceRegistryUnsafe("D06 record catalog identity is invalid")
    for name in names[1:3] + names[4:]:
        if type(state[name]) is not str:
            raise ResultViewSourceRegistryUnsafe(
                "D06 record catalog identity is invalid"
            )
    return {name: state[name] for name in names}


def _member_for_result(
    status: CohortManifestRecordStatus, result_id: str
) -> CohortRecordBinding:
    """Return the one live available binding for a result; never two members."""

    matches = [
        item
        for item in status.members
        if item.binding is not None and item.binding.result.result_id == result_id
    ]
    if len(matches) != 1:
        raise ResultViewSourceRegistryStale(
            "E06 source result is not bound to exactly one live member"
        )
    member = matches[0]
    if (
        member.availability is not CohortRecordAvailability.AVAILABLE
        or member.binding is None
        or member.binding.member_sha256 != member.member_sha256
    ):
        raise ResultViewSourceRegistryStale("E06 source member is not available")
    return member.binding


def _require_record_matches_catalog(
    record: VerifiedMeasurementRecord, catalog: CatalogResultRef
) -> None:
    capability = record.current_capability
    expected_qualification = (
        capability.qualification_state.value
        if capability.qualification_state is not None
        else CatalogQualificationState.UNKNOWN.value
    )
    if (
        record.result_id,
        record.bundle_sha256,
        record.method.method_ref,
        record.method_definition_sha256,
        capability.method_ref,
        capability.method_definition_sha256,
        capability.registry_sha256,
        capability.registry_version,
        capability.authority_head_sha256,
        capability.authority_revision,
        capability.authority_scope,
        capability.as_of,
        expected_qualification,
        capability.display_role,
        capability.research_inspectable,
        capability.current_provider_eligible,
        record.execution_state,
        record.trust_state,
    ) != (
        catalog.result_id,
        catalog.bundle_sha256,
        catalog.method_ref,
        catalog.method_definition_sha256,
        catalog.method_ref,
        catalog.method_definition_sha256,
        catalog.registry_sha256,
        catalog.registry_version,
        catalog.authority_head_sha256,
        catalog.authority_revision,
        catalog.authority_scope,
        catalog.capability_as_of,
        catalog.qualification_state.value,
        catalog.display_role,
        catalog.research_inspectable,
        catalog.current_provider_eligible,
        ExecutionState.COMPLETE,
        TrustState.VERIFIED,
    ):
        raise ResultViewSourceRegistryStale(
            "E06 source record does not match its live catalog result"
        )


def _registry_instance_snapshot(registry: ResultViewSourceRegistry) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_record_catalog",
        "_authority",
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
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            ResultViewSourceRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
            root_observed = os.fstat(instance["_root_fd"])
            metadata_observed = os.fstat(descriptor)
        except (OSError, TypeError):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry authority state changed"
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
            persisted != metadata_bytes
            or instance["_root_identity"] != root_identity
            or instance["_metadata_identity"] != metadata_identity
            or instance["_genesis_head_sha256"] != _metadata_genesis_sha256(metadata)
            or instance["_head_key"] != derived_head_key
        ):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry authority state changed"
            )
    authority = instance["_authority"]
    if type(authority) is not MappingProxyType:
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )
    live = _authority_identity(instance["_record_catalog"])
    if dict(authority) != live or (
        metadata.cohort_registry_id,
        metadata.cohort_registry_epoch_sha256,
        metadata.catalog_storage_identity_sha256,
        metadata.catalog_reader_registry_sha256,
        metadata.record_catalog_scope_sha256,
    ) != (
        live["_cohort_registry_id"],
        live["_cohort_registry_epoch_sha256"],
        live["_catalog_storage_identity_sha256"],
        live["_catalog_reader_identity_sha256"],
        live["_recovery_scope_sha256"],
    ):
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )
    return (
        id(instance["root"]),
        id(instance["_record_catalog"]),
        tuple((name, id(value)) for name, value in sorted(live.items())),
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


def _seal_registry_instance(registry: ResultViewSourceRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


_LoadedState = dict[str, tuple[RegisteredResultViewSourceObject, bytes, int]]


class ResultViewSourceRegistry:
    """Descriptor-relative immutable E06 source publication with live re-verification."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "backup_bytes",
            "close",
            "list_selectors",
            "register_source",
            "registry_identity",
            "resolve",
            "selector_for_member",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        record_catalog: CohortRecordCatalog,
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        if type(record_catalog) is not CohortRecordCatalog:
            raise TypeError("E06 source registry requires the exact D06 record catalog")
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values) and (
            any(item is None for item in expected_values)
            or not _is_token(expected_registry_id, "e06_registry_", 32)
            or not _is_sha256(expected_registry_epoch_sha256)
            or not _is_sha256(expected_state_head_sha256)
        ):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry expected identity or head is invalid"
            )
        self.root = _snapshot_path(root)
        self._record_catalog = record_catalog
        self._authority = MappingProxyType(_authority_identity(record_catalog))
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._journal_fd: int | None = None
        self._process_lock = threading.RLock()
        try:
            # The linkage snapshot is read before any registry lock: the D01
            # fence is not reentrant and must never be taken under this lock.
            linkage = _PINNED_ACTIVE_SNAPSHOT(self._authority["_linkage_store"])
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
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry root must be private"
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
                raise ResultViewSourceRegistryUnsafe("E06 source registry root changed")
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
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry objects are unsafe"
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
                raise ResultViewSourceRegistryUnsafe("E06 source registry lock is unsafe")
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
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry journal is unsafe"
                )
            self._journal_identity = (journal_metadata.st_dev, journal_metadata.st_ino)
            with _SR_LOCK(self, exclusive=True):
                self._metadata = _SR_LOAD_OR_CREATE_METADATA(
                    self, linkage, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._root_identity[0],
                    self._root_identity[1],
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _SR_RECOVER_TEMPORARY_OBJECTS(self)
                _, head = _SR_LOAD_STATE(self, check_trusted_head=False)
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise ResultViewSourceRegistryUnsafe(
                            "new E06 source registry cannot inherit an expected identity"
                        )
                elif any(item is None for item in expected_values):
                    raise ResultViewSourceRegistryUnsafe(
                        "E06 source registry expected identity and head are required"
                    )
                if not root_created and expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise ResultViewSourceRegistryUnsafe(
                        "E06 source registry expected identity or head is invalid"
                    )
                self._trusted_head_sha256 = head
                _SR_ACCEPT_OBSERVED_HEAD(
                    self, _SR_LOAD_JOURNAL(self), head, check_instance=False
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

    def __enter__(self) -> ResultViewSourceRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _SR_CLOSE(self)

    def __del__(self) -> None:
        try:
            _SR_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _SR_VALIDATE_STORAGE(self)
                yield
                _SR_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        checks = [
            ("objects", self._objects_fd, self._objects_identity, stat.S_ISDIR, 0o700),
            (".registry.lock", self._lock_fd, self._lock_identity, stat.S_ISREG, 0o600),
            (
                "registry-journal.jsonl",
                self._journal_fd,
                self._journal_identity,
                stat.S_ISREG,
                0o600,
            ),
        ]
        if self._metadata_fd is not None:
            checks.append(
                (
                    "registry-metadata.json",
                    self._metadata_fd,
                    self._metadata_identity,
                    stat.S_ISREG,
                    0o600,
                )
            )
        try:
            root_path = os.stat(self.root, follow_symlinks=False)
            root_bound = os.fstat(self._root_fd)
            observed = [
                (
                    os.stat(name, dir_fd=self._root_fd, follow_symlinks=False),
                    os.fstat(descriptor),
                    identity,
                    kind,
                    mode,
                )
                for name, descriptor, identity, kind, mode in checks
            ]
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (root_bound.st_dev, root_bound.st_ino) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
        ):
            raise ResultViewSourceRegistryUnsafe("E06 source registry storage changed")
        for path_stat, bound_stat, identity, kind, mode in observed:
            if (
                not kind(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino) != identity
                or (bound_stat.st_dev, bound_stat.st_ino) != identity
                or stat.S_IMODE(bound_stat.st_mode) != mode
                or bound_stat.st_uid != os.geteuid()
            ):
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        if self._objects_fd is None:
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry objects are unavailable"
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
                    raise ResultViewSourceRegistryUnsafe(
                        "E06 source registry recovery is unsafe"
                    ) from None
        os.fsync(self._objects_fd)

    def _load_or_create_metadata(
        self, linkage: object, *, allow_create: bool
    ) -> ResultViewSourceRegistryMetadata:
        assert self._root_fd is not None
        authority = self._authority
        expected = (
            authority["_cohort_registry_id"],
            authority["_cohort_registry_epoch_sha256"],
            linkage.store_id,
            linkage.store_epoch_sha256,
            linkage.storage_identity_sha256,
            authority["_catalog_storage_identity_sha256"],
            authority["_catalog_reader_identity_sha256"],
            authority["_recovery_scope_sha256"],
        )
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry metadata is missing"
                ) from None
            try:
                _SR_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(_new_metadata(authority, linkage)),
                )
            except FileExistsError:
                pass
            return _SR_LOAD_OR_CREATE_METADATA(self, linkage, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                ResultViewSourceRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry metadata is invalid"
            ) from None
        if (
            metadata.cohort_registry_id,
            metadata.cohort_registry_epoch_sha256,
            metadata.linkage_store_id,
            metadata.linkage_store_epoch_sha256,
            metadata.linkage_storage_identity_sha256,
            metadata.catalog_storage_identity_sha256,
            metadata.catalog_reader_registry_sha256,
            metadata.record_catalog_scope_sha256,
        ) != expected:
            os.close(descriptor)
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry D06/E04 authority changed"
            )
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        return metadata

    def _load_journal(self) -> tuple[ResultViewSourceJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, MAX_JOURNAL_BYTES)
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry journal is incomplete"
            )
        entries: list[ResultViewSourceJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_SOURCES:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    ResultViewSourceJournalEntry, line
                )
            except Exception:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: ResultViewSourceJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry journal append failed"
            ) from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except OSError:
            # Remove any torn suffix so the committed chain stays readable; the
            # object it named remains an uncommitted remnant for later cleanup.
            try:
                os.ftruncate(descriptor, committed_size)
                os.fsync(descriptor)
            except OSError:
                pass
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[ResultViewSourceJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self, *, check_trusted_head: bool = True
    ) -> tuple[_LoadedState, str]:
        """Load only journal-committed objects; extra or missing files fail closed.

        The result maps each object digest to its parsed object, exact bytes, and
        its source version within its member selector.
        """

        if self._objects_fd is None:
            raise ResultViewSourceRegistryUnsafe("E06 source registry is closed")
        journal = _SR_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_SOURCES + 1:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry committed objects are inconsistent"
            )
        values: dict[str, RegisteredResultViewSourceObject] = {}
        contents: dict[str, bytes] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry journal binding is invalid"
                )
            try:
                value = registered_source_object_from_bytes(content)
            except ValueError:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry object is invalid"
                ) from None
            values[entry.object_sha256] = value
            contents[entry.object_sha256] = content
        try:
            _validate_journal_semantics(
                self._metadata.registry_epoch_sha256, journal, values
            )
        except ValueError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry journal binding is invalid"
            ) from None
        loaded: _LoadedState = {
            entry.object_sha256: (
                values[entry.object_sha256],
                contents[entry.object_sha256],
                entry.source_version,
            )
            for entry in journal
        }
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _SR_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
            if process_head is not None and process_head not in chain:
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry state rollback detected"
                )
        return loaded, head

    def _derive_in_fence(
        self,
        status: CohortManifestRecordStatus,
        *,
        record: VerifiedMeasurementRecord,
        counterpart_record: VerifiedMeasurementRecord,
        policy: CompatibilityPolicy,
        expected_policy_sha256: str,
        denominator: DenominatorLedger,
        accessible_label: str,
        qc_label: str,
    ) -> RegisteredResultViewSourceObject:
        """Derive one source object from live D06/E04 authority and pure E05/E06."""

        metadata = self._metadata
        if (
            status.registry_id != metadata.cohort_registry_id
            or status.registry_epoch_sha256 != metadata.cohort_registry_epoch_sha256
        ):
            raise ResultViewSourceRegistryStale("E06 source cohort authority changed")
        if record.result_id == counterpart_record.result_id:
            raise ResultViewSourceRegistryConflict(
                "E06 source counterpart must be a distinct result"
            )
        binding = _member_for_result(status, record.result_id)
        counterpart = _member_for_result(status, counterpart_record.result_id)
        if binding.member_sha256 == counterpart.member_sha256:
            raise ResultViewSourceRegistryStale(
                "one result cannot represent two members"
            )
        _require_record_matches_catalog(record, binding.result)
        _require_record_matches_catalog(counterpart_record, counterpart.result)
        try:
            request = CompatibilityRequest(
                left=record,
                right=counterpart_record,
                policy=policy,
                trusted_policy_sha256=expected_policy_sha256,
                trusted_authority_head_sha256=binding.result.authority_head_sha256,
            )
            decision = _PINNED_DECIDE(request)
            if type(decision) is not CompatibilityDecision:
                raise TypeError("compatibility decision type changed")
            source = _PINNED_BIND_SOURCE(
                record=record,
                compatibility_decision=decision,
                denominator=denominator,
                accessible_label=accessible_label,
                qc_label=qc_label,
            )
            if type(source) is not ResultViewSource:
                raise TypeError("result view source type changed")
            value = RegisteredResultViewSourceObject(
                cohort_registry_id=status.registry_id,
                cohort_registry_epoch_sha256=status.registry_epoch_sha256,
                cohort_selector_id=status.selector_id,
                cohort_version=status.cohort_version,
                cohort_manifest_sha256=status.cohort_manifest_sha256,
                catalog_authority_sha256=status.catalog_authority_sha256,
                member_sha256=binding.member_sha256,
                binding_sha256=_contract_sha256(binding),
                counterpart_member_sha256=counterpart.member_sha256,
                counterpart_binding_sha256=_contract_sha256(counterpart),
                compatibility_request=request,
                source=source,
            )
            return registered_source_object_from_bytes(
                registered_source_object_bytes(value)
            )
        except ResultViewSourceRegistryError:
            raise
        except Exception:
            raise ResultViewSourceRegistryConflict(
                "E06 source inputs are not a valid result-view source"
            ) from None

    def _replay_in_fence(
        self,
        value: RegisteredResultViewSourceObject,
        status: CohortManifestRecordStatus,
    ) -> CohortRecordBinding:
        """Re-verify one stored object against the fenced live status."""

        if (
            status.selector_id != value.cohort_selector_id
            or status.cohort_version != value.cohort_version
            or status.cohort_manifest_sha256 != value.cohort_manifest_sha256
            or status.catalog_authority_sha256 != value.catalog_authority_sha256
        ):
            raise ResultViewSourceRegistryStale(
                "E06 source no longer verifies against live authority"
            )
        request = value.compatibility_request
        replayed = _SR_DERIVE_IN_FENCE(
            self,
            status,
            record=request.left,
            counterpart_record=request.right,
            policy=request.policy,
            expected_policy_sha256=request.trusted_policy_sha256,
            denominator=value.source.denominator,
            accessible_label=value.source.accessible_label,
            qc_label=value.source.qc_label,
        )
        if registered_source_object_bytes(replayed) != registered_source_object_bytes(
            value
        ):
            raise ResultViewSourceRegistryStale(
                "E06 source no longer verifies against live authority"
            )
        return _member_for_result(status, value.source.record.result_id)

    @contextmanager
    def _cohort_fence(
        self, cohort_selector_id: str, cohort_version: int
    ) -> Iterator[CohortManifestRecordStatus]:
        """Hold the live D06 status fence; D06 failures become stale errors."""

        authority = self._authority
        try:
            with _PINNED_STATUS_FENCE(
                self._record_catalog,
                cohort_selector_id,
                cohort_version,
                expected_registry=authority["_cohort_registry"],
                expected_linkage_store=authority["_linkage_store"],
            ) as (_, status):
                if type(status) is not CohortManifestRecordStatus:
                    raise ResultViewSourceRegistryStale(
                        "E06 source cohort authority is unavailable"
                    )
                yield status
        except ResultViewSourceRegistryError:
            raise
        except CohortImportError:
            raise ResultViewSourceRegistryStale(
                "E06 source cohort authority is not current"
            ) from None

    def selector_for_member(
        self, cohort_selector_id: str, cohort_version: int, member_sha256: str
    ) -> str:
        """Return the opaque selector for one D06 member slot (no I/O)."""

        _require_registry_integrity(self)
        if (
            not _is_token(cohort_selector_id, "cohort_selector_", 40)
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
            or not _is_sha256(member_sha256)
        ):
            raise ResultViewSourceRegistryConflict("E06 source member slot is invalid")
        return _SR_SELECTOR_ID(
            self._metadata.registry_epoch_sha256,
            self._metadata.cohort_registry_id,
            cohort_selector_id,
            cohort_version,
            member_sha256,
        )

    def register_source(
        self,
        *,
        cohort_selector_id: str,
        cohort_version: int,
        record: VerifiedMeasurementRecord,
        counterpart_record: VerifiedMeasurementRecord,
        policy: CompatibilityPolicy,
        expected_policy_sha256: str,
        denominator: DenominatorLedger,
        accessible_label: str,
        qc_label: str,
    ) -> SourceRegistrationReceipt:
        """Derive one E06 source under live D06/E04 authority and publish it.

        The caller supplies exact records, policy and pin, ledger, and labels.
        The members, the authority-head pin, the E05 decision, and the E06
        source are always derived here.
        """

        _require_registry_integrity(self)
        if (
            not _is_token(cohort_selector_id, "cohort_selector_", 40)
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
            or not _is_sha256(expected_policy_sha256)
            or type(accessible_label) is not str
            or type(qc_label) is not str
            or len(accessible_label) > 160
            or len(qc_label) > 160
        ):
            raise ResultViewSourceRegistryConflict(
                "E06 source registration input is invalid"
            )
        record = _capture(record, VerifiedMeasurementRecord)
        counterpart_record = _capture(counterpart_record, VerifiedMeasurementRecord)
        policy = _capture(policy, CompatibilityPolicy)
        denominator = _capture(denominator, DenominatorLedger)
        with _SR_COHORT_FENCE(self, cohort_selector_id, cohort_version) as status:
            captured = _SR_DERIVE_IN_FENCE(
                self,
                status,
                record=record,
                counterpart_record=counterpart_record,
                policy=policy,
                expected_policy_sha256=expected_policy_sha256,
                denominator=denominator,
                accessible_label=accessible_label,
                qc_label=qc_label,
            )
            content = registered_source_object_bytes(captured)
            digest = hashlib.sha256(content).hexdigest()
            _SR_REPLAY_IN_FENCE(self, captured, status)
            selector = _SR_OBJECT_SELECTOR_ID(
                self._metadata.registry_epoch_sha256, captured
            )
            with _SR_LOCK(self, exclusive=True):
                _SR_RECOVER_TEMPORARY_OBJECTS(self)
                loaded, head = _SR_LOAD_STATE(self)
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
                        raise ResultViewSourceRegistryConflict(
                            "E06 source object digest conflicts"
                        )
                else:
                    cohort = (
                        captured.cohort_registry_id,
                        captured.cohort_selector_id,
                        captured.cohort_version,
                    )
                    result_id = captured.source.record.result_id
                    for other, _, _ in loaded.values():
                        if (
                            other.cohort_registry_id,
                            other.cohort_selector_id,
                            other.cohort_version,
                        ) != cohort:
                            continue
                        same_result = other.source.record.result_id == result_id
                        same_member = other.member_sha256 == captured.member_sha256
                        if same_result != same_member:
                            raise ResultViewSourceRegistryConflict(
                                "one result cannot represent two members"
                            )
                    version = 1 + sum(
                        _SR_OBJECT_SELECTOR_ID(
                            self._metadata.registry_epoch_sha256, other
                        )
                        == selector
                        for other, _, _ in loaded.values()
                    )
                    if version > MAX_SOURCE_VERSIONS:
                        raise ResultViewSourceRegistryConflict(
                            "E06 source selector version bound exceeded"
                        )
                    if len(loaded) >= MAX_REGISTERED_SOURCES:
                        raise ResultViewSourceRegistryConflict(
                            "E06 source registry is full"
                        )
                    if (
                        sum(len(item[1]) for item in loaded.values()) + len(content)
                        > MAX_TOTAL_OBJECT_BYTES
                    ):
                        raise ResultViewSourceRegistryConflict(
                            "E06 source registry byte bound would be exceeded"
                        )
                    try:
                        _SR_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                    except FileExistsError:
                        if _read_exact_object(self._objects_fd, digest) != content:
                            raise ResultViewSourceRegistryConflict(
                                "E06 source publication conflicts"
                            ) from None
                    _SR_APPEND_JOURNAL(
                        self,
                        _build_journal_entry(
                            sequence=len(loaded) + 1,
                            previous_entry_sha256=head,
                            selector_id=selector,
                            source_version=version,
                            object_sha256=digest,
                            object_bytes=len(content),
                        ),
                    )
                final, final_head = _SR_LOAD_STATE(self)
                if digest not in final or final[digest][1] != content:
                    raise ResultViewSourceRegistryUnsafe(
                        "E06 source publication is unproven"
                    )
                return _SR_RECEIPT(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(final),
                    state_head_sha256=final_head,
                    selector_id=selector,
                    source_version=final[digest][2],
                    object_sha256=digest,
                    source_sha256=_SR_CONTRACT_SHA256(captured.source),
                )

    def _find(
        self, loaded: _LoadedState, selector_id: str, source_version: int
    ) -> tuple[str, RegisteredResultViewSourceObject]:
        epoch = self._metadata.registry_epoch_sha256
        matches = [
            (digest, value)
            for digest, (value, _, version) in loaded.items()
            if version == source_version
            and _SR_OBJECT_SELECTOR_ID(epoch, value) == selector_id
        ]
        if len(matches) != 1:
            raise ResultViewSourceRegistryConflict("E06 source selector is unavailable")
        return matches[0]

    def registry_identity(self) -> ResultViewSourceRegistryIdentity:
        """Return this registry's immutable identity and cohort-registry binding."""

        _require_registry_integrity(self)
        metadata = self._metadata
        return ResultViewSourceRegistryIdentity(
            registry_id=metadata.registry_id,
            registry_epoch_sha256=metadata.registry_epoch_sha256,
            cohort_registry_id=metadata.cohort_registry_id,
            cohort_registry_epoch_sha256=metadata.cohort_registry_epoch_sha256,
        )

    def resolve(
        self,
        selector_id: str,
        source_version: int,
        *,
        expected_member_sha256: str,
        expected_result_id: str,
    ) -> RegisteredResultViewSource:
        """Return one registered source only after it re-verifies against live authority.

        The selector is first located under a shared registry lock so that the
        D06 fence can be taken before the registry lock (the documented lock
        order).  Objects are immutable and append-only, so the located object is
        then re-read and re-verified with the D06 fence and the registry lock
        both held through construction of the return value.
        """

        _require_registry_integrity(self)
        if (
            not _is_token(selector_id, "e06_source_", 40)
            or type(source_version) is not int
            or not 1 <= source_version <= MAX_SOURCE_VERSIONS
            or not _is_sha256(expected_member_sha256)
            or not _is_result_id(expected_result_id)
        ):
            raise ResultViewSourceRegistryConflict("E06 source selector is invalid")
        with _SR_LOCK(self, exclusive=False):
            loaded, _ = _SR_LOAD_STATE(self)
            located_digest, located = _SR_FIND(self, loaded, selector_id, source_version)
        with _SR_COHORT_FENCE(
            self, located.cohort_selector_id, located.cohort_version
        ) as status:
            with _SR_LOCK(self, exclusive=False):
                loaded, head = _SR_LOAD_STATE(self)
                digest, value = _SR_FIND(self, loaded, selector_id, source_version)
                if digest != located_digest:
                    raise ResultViewSourceRegistryUnsafe(
                        "E06 source registry object changed"
                    )
                if (
                    value.member_sha256 != expected_member_sha256
                    or value.source.record.result_id != expected_result_id
                ):
                    raise ResultViewSourceRegistryConflict(
                        "E06 source does not bind the expected result and member"
                    )
                binding = _SR_REPLAY_IN_FENCE(self, value, status)
                source_sha256 = _SR_CONTRACT_SHA256(value.source)
                payload = dict(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    selector_id=selector_id,
                    source_version=source_version,
                    object_sha256=digest,
                    cohort_registry_id=value.cohort_registry_id,
                    cohort_registry_epoch_sha256=value.cohort_registry_epoch_sha256,
                    cohort_selector_id=value.cohort_selector_id,
                    cohort_version=value.cohort_version,
                    cohort_manifest_sha256=value.cohort_manifest_sha256,
                    record_status_sha256=status.status_sha256,
                    catalog_authority_sha256=value.catalog_authority_sha256,
                    member_sha256=value.member_sha256,
                    binding_sha256=value.binding_sha256,
                    catalog_result_sha256=_SR_CONTRACT_SHA256(binding.result),
                    counterpart_member_sha256=value.counterpart_member_sha256,
                    counterpart_binding_sha256=value.counterpart_binding_sha256,
                    source_sha256=source_sha256,
                    compatibility_decision_sha256=(
                        value.source.compatibility_decision.decision_sha256
                    ),
                    denominator_ledger_sha256=_SR_CONTRACT_SHA256(
                        value.source.denominator
                    ),
                    source=value.source,
                )
                placeholder = _SR_RESOLVED.model_construct(
                    **payload, source_replay_sha256="0" * 64
                )
                return _SR_RESOLVED(
                    **payload,
                    source_replay_sha256=_SR_SOURCE_REPLAY_SHA256(placeholder),
                )

    def list_selectors(
        self,
        cohort_selector_id: str,
        cohort_version: int,
        *,
        after_selector_id: str | None = None,
        after_source_version: int | None = None,
        limit: int = 50,
    ) -> SourceSelectorPage:
        """Return one bounded privacy-safe page for one cohort with live state."""

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise ResultViewSourceRegistryConflict(
                "E06 source selector page bound is invalid"
            )
        if (
            not _is_token(cohort_selector_id, "cohort_selector_", 40)
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise ResultViewSourceRegistryConflict("E06 source cohort is invalid")
        if (after_selector_id is None) != (after_source_version is None) or (
            after_selector_id is not None
            and (
                not _is_token(after_selector_id, "e06_source_", 40)
                or type(after_source_version) is not int
                or not 1 <= after_source_version <= MAX_SOURCE_VERSIONS
            )
        ):
            raise ResultViewSourceRegistryConflict(
                "E06 source selector cursor is invalid"
            )
        with _SR_COHORT_FENCE(self, cohort_selector_id, cohort_version) as status:
            with _SR_LOCK(self, exclusive=False):
                loaded, head = _SR_LOAD_STATE(self)
                epoch = self._metadata.registry_epoch_sha256
                ordered = sorted(
                    (
                        _SR_OBJECT_SELECTOR_ID(epoch, value),
                        version,
                        digest,
                        value,
                    )
                    for digest, (value, _, version) in loaded.items()
                    if value.cohort_registry_id == self._metadata.cohort_registry_id
                    and value.cohort_selector_id == cohort_selector_id
                    and value.cohort_version == cohort_version
                )
                if after_selector_id is not None:
                    ordered = [
                        item
                        for item in ordered
                        if (item[0], item[1]) > (after_selector_id, after_source_version)
                    ]
                selected = ordered[:limit]
                rows: list[SourceSelectorRecord] = []
                for selector_id, version, digest, value in selected:
                    try:
                        _SR_REPLAY_IN_FENCE(self, value, status)
                    except ResultViewSourceRegistryStale:
                        authority_state = SourceAuthorityState.STALE
                    else:
                        authority_state = SourceAuthorityState.CURRENT
                    rows.append(
                        _SR_SELECTOR_RECORD(
                            selector_id=selector_id,
                            source_version=version,
                            object_sha256=digest,
                            source_sha256=_SR_CONTRACT_SHA256(value.source),
                            denominator_ledger_sha256=_SR_CONTRACT_SHA256(
                                value.source.denominator
                            ),
                            compatibility_outcome=(
                                value.source.compatibility_decision.outcome
                            ),
                            authority_state=authority_state,
                        )
                    )
                more = len(ordered) > len(selected)
                return _SR_SELECTOR_PAGE(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=epoch,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    records=tuple(rows),
                    next_after_selector_id=(
                        rows[-1].selector_id if more and rows else None
                    ),
                    next_after_source_version=(
                        rows[-1].source_version if more and rows else None
                    ),
                )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _SR_LOCK(self, exclusive=False):
            loaded, head = _SR_LOAD_STATE(self)
            backup = ResultViewSourceBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_SR_LOAD_JOURNAL(self),
                objects=tuple(
                    ResultViewSourceBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content, _) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise ResultViewSourceRegistryConflict(
                    "E06 source registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        record_catalog: CohortRecordCatalog,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> ResultViewSourceRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        if type(record_catalog) is not CohortRecordCatalog:
            raise TypeError("E06 source registry requires the exact D06 record catalog")
        backup = result_view_source_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup expected head is invalid"
            )
        authority = _authority_identity(record_catalog)
        linkage = _PINNED_ACTIVE_SNAPSHOT(authority["_linkage_store"])
        if (
            backup.metadata.cohort_registry_id,
            backup.metadata.cohort_registry_epoch_sha256,
            backup.metadata.linkage_store_id,
            backup.metadata.linkage_store_epoch_sha256,
            backup.metadata.linkage_storage_identity_sha256,
            backup.metadata.catalog_storage_identity_sha256,
            backup.metadata.catalog_reader_registry_sha256,
            backup.metadata.record_catalog_scope_sha256,
        ) != (
            authority["_cohort_registry_id"],
            authority["_cohort_registry_epoch_sha256"],
            linkage.store_id,
            linkage.store_epoch_sha256,
            linkage.storage_identity_sha256,
            authority["_catalog_storage_identity_sha256"],
            authority["_catalog_reader_identity_sha256"],
            authority["_recovery_scope_sha256"],
        ):
            raise ResultViewSourceRegistryConflict(
                "E06 source registry backup authority is invalid"
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
        completed = False
        try:
            parent_lstat = os.stat(parent, follow_symlinks=False)
            parent_fd = os.open(parent, directory_flags)
            parent_bound = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_lstat.st_mode) or (
                parent_lstat.st_dev,
                parent_lstat.st_ino,
            ) != (parent_bound.st_dev, parent_bound.st_ino):
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry restore parent changed"
                )
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
            created = True
            root_lstat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(target.name, directory_flags, dir_fd=parent_fd)
            root_bound = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_lstat.st_mode)
                or (root_lstat.st_dev, root_lstat.st_ino)
                != (root_bound.st_dev, root_bound.st_ino)
                or stat.S_IMODE(root_bound.st_mode) != 0o700
                or root_bound.st_uid != os.geteuid()
            ):
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry restore root changed"
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
                raise ResultViewSourceRegistryUnsafe(
                    "E06 source registry restore objects changed"
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
            # Reopen through the normal checks before the restore counts as
            # complete, so a target that cannot open is removed, not left to
            # block a retry.
            restored = _SR_CONSTRUCT(
                target,
                record_catalog=record_catalog,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise ResultViewSourceRegistryConflict(
                "E06 source registry restore target already exists"
            ) from None
        except OSError:
            raise ResultViewSourceRegistryUnsafe(
                "E06 source registry restore failed"
            ) from None
        finally:
            if created and not completed:
                if root_fd is not None:
                    _remove_partial_restore(parent_fd, target.name, root_fd, objects_fd)
                elif parent_fd is not None:
                    try:
                        os.rmdir(target.name, dir_fd=parent_fd)
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


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: ResultViewSourceRegistry.__dict__[name]
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
            "_derive_in_fence",
            "_replay_in_fence",
            "_cohort_fence",
            "_find",
            "selector_for_member",
            "register_source",
            "registry_identity",
            "resolve",
            "list_selectors",
            "backup_bytes",
            "restore",
            "close",
        )
    }
)


def _require_registry_class_integrity(cls: type[object]) -> None:
    if cls is not ResultViewSourceRegistry or any(
        ResultViewSourceRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise ResultViewSourceRegistryUnsafe("E06 source registry callable changed")


def _require_registry_integrity(registry: ResultViewSourceRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise ResultViewSourceRegistryUnsafe("E06 source registry callable changed")
    authority_sources = {
        "_PINNED_STATUS_FENCE": CohortRecordCatalog.__dict__.get(
            "record_status_authority_fence"
        ),
        "_PINNED_ACTIVE_SNAPSHOT": ProviderLinkageStore.__dict__.get("active_snapshot"),
        "_PINNED_DECIDE": e05_module.decide_compatibility,
        "_PINNED_BIND_SOURCE": e06_module.bind_result_view_source,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority callable changed"
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
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise ResultViewSourceRegistryUnsafe(
            "E06 source registry authority state changed"
        )


_SR_CONSTRUCT = ResultViewSourceRegistry
_SR_CLOSE = ResultViewSourceRegistry.close
_SR_LOCK = ResultViewSourceRegistry._lock
_SR_VALIDATE_STORAGE = ResultViewSourceRegistry._validate_storage
_SR_PUBLISH = ResultViewSourceRegistry._publish
_SR_RECOVER_TEMPORARY_OBJECTS = ResultViewSourceRegistry._recover_temporary_objects
_SR_LOAD_OR_CREATE_METADATA = ResultViewSourceRegistry._load_or_create_metadata
_SR_LOAD_JOURNAL = ResultViewSourceRegistry._load_journal
_SR_APPEND_JOURNAL = ResultViewSourceRegistry._append_journal
_SR_ACCEPT_OBSERVED_HEAD = ResultViewSourceRegistry._accept_observed_head
_SR_LOAD_STATE = ResultViewSourceRegistry._load_state
_SR_DERIVE_IN_FENCE = ResultViewSourceRegistry._derive_in_fence
_SR_REPLAY_IN_FENCE = ResultViewSourceRegistry._replay_in_fence
_SR_COHORT_FENCE = ResultViewSourceRegistry._cohort_fence
_SR_FIND = ResultViewSourceRegistry._find
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another member's source.
_SR_RECEIPT = SourceRegistrationReceipt
_SR_RESOLVED = RegisteredResultViewSource
_SR_SELECTOR_RECORD = SourceSelectorRecord
_SR_SELECTOR_PAGE = SourceSelectorPage
_SR_SELECTOR_ID = _selector_id
_SR_OBJECT_SELECTOR_ID = _object_selector_id
_SR_CONTRACT_SHA256 = _contract_sha256
_SR_SOURCE_REPLAY_SHA256 = _source_replay_sha256
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_STATUS_FENCE": _PINNED_STATUS_FENCE,
        "_PINNED_ACTIVE_SNAPSHOT": _PINNED_ACTIVE_SNAPSHOT,
        "_PINNED_DECIDE": _PINNED_DECIDE,
        "_PINNED_BIND_SOURCE": _PINNED_BIND_SOURCE,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_SR_CONSTRUCT",
            "_SR_CLOSE",
            "_SR_LOCK",
            "_SR_VALIDATE_STORAGE",
            "_SR_PUBLISH",
            "_SR_RECOVER_TEMPORARY_OBJECTS",
            "_SR_LOAD_OR_CREATE_METADATA",
            "_SR_LOAD_JOURNAL",
            "_SR_APPEND_JOURNAL",
            "_SR_ACCEPT_OBSERVED_HEAD",
            "_SR_LOAD_STATE",
            "_SR_DERIVE_IN_FENCE",
            "_SR_REPLAY_IN_FENCE",
            "_SR_COHORT_FENCE",
            "_SR_FIND",
            "_SR_RECEIPT",
            "_SR_RESOLVED",
            "_SR_SELECTOR_RECORD",
            "_SR_SELECTOR_PAGE",
            "_SR_SELECTOR_ID",
            "_SR_OBJECT_SELECTOR_ID",
            "_SR_CONTRACT_SHA256",
            "_SR_SOURCE_REPLAY_SHA256",
        )
    }
)


__all__ = [
    "CALLER_ASSERTED_FIELDS",
    "RegisteredResultViewSource",
    "RegisteredResultViewSourceObject",
    "ResultViewSourceBackup",
    "ResultViewSourceBackupObject",
    "ResultViewSourceJournalEntry",
    "ResultViewSourceRegistry",
    "ResultViewSourceRegistryConflict",
    "ResultViewSourceRegistryError",
    "ResultViewSourceRegistryIdentity",
    "ResultViewSourceRegistryMetadata",
    "ResultViewSourceRegistryStale",
    "ResultViewSourceRegistryUnsafe",
    "SourceAuthorityState",
    "SourceRegistrationReceipt",
    "SourceSelectorPage",
    "SourceSelectorRecord",
    "registered_source_object_bytes",
    "registered_source_object_from_bytes",
    "result_view_source_backup_from_bytes",
]
