"""Protected family-source artifact discovery for E12 standalone source values.

The E12 family adapters project standalone values from an E07, E08 or E09
artifact, but nothing durable locates that artifact for a given E06 source.
This registry is that discovery store (``measurement_source_artifact_registry``
in ``docs/E12-INTEGRATION-PLAN.md``).

Callers never supply an artifact.  Registration names one E06 source selector
and version; the registry resolves it through the live E06 registry, then,
under the live D06 record-status fence, re-reads both E06 members' exact E04
bundles, derives the E07 ``FragmentExplorerView`` with fixed canonical
controls, and stores it immutably under an opaque selector.  Every protected
read repeats the E06 resolve and the whole derivation and requires the stored
object to be byte-identical before returning it.

Only the E07 fragment family is applicable today.  E08 cell-origin and E09 CNA
artifacts cannot be bound to an E06 source without a product decision; see
``docs/MEASUREMENT-SOURCE-ARTIFACT-REGISTRY.md``.
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
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

import evidence_inspector.compatibility as e05_module
import evidence_inspector.fragment_explorer as e07_module
from evidence_inspector.cohort_import import (
    CohortImportError,
    CohortManifestRecordStatus,
    CohortRecordAvailability,
    CohortRecordBinding,
    CohortRecordCatalog,
)
from evidence_inspector.cohort_registry import CohortRegistry
from evidence_inspector.compatibility import (
    CompatibilityContractError,
    CompatibilityDecision,
    CompatibilityPolicy,
    CompatibilityRequest,
    ExecutionState,
    InformationState,
    TrustState,
    VerifiedMeasurementRecord,
    compatibility_policy_sha256,
    replay_compatibility_decision,
)
from evidence_inspector.fragment_explorer import (
    ExplorerControls,
    ExplorerMethodSelection,
    ExplorerSourceState,
    FragmentExplorerError,
    FragmentExplorerRequest,
    FragmentExplorerView,
    FragmentQuantity,
    PanelId,
    VerifiedFragmentSource,
    build_fragment_explorer_state,
    build_fragment_explorer_view,
    fragment_source_from_verified_bundle,
    replay_fragment_explorer_view,
)
from evidence_inspector.method_registry import (
    MethodFamily,
    RegistryContract,
    canonical_contract_bytes,
    contract_from_canonical_bytes,
)
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.result_catalog import (
    CatalogError,
    CatalogResultRef,
    ResultCatalog,
)
from evidence_inspector.result_view_source_registry import (
    CALLER_ASSERTED_FIELDS,
    CallerAssertedFields,
    RegisteredResultViewSource,
    ResultViewSourceRegistry,
    ResultViewSourceRegistryConflict,
    ResultViewSourceRegistryError,
    ResultViewSourceRegistryMetadata,
    ResultViewSourceRegistryStale,
)
from evidence_inspector.registry_storage import (
    begin_staged_root as _begin_staged_root,
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
from traceback_runner.bundles import VerifiedBundle
from traceback_runner.serialization import canonical_json_bytes

MAX_REGISTERED_ARTIFACTS = 10_000
MAX_SELECTOR_PAGE = 100
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 256 * 1024 * 1024
MAX_BACKUP_BYTES = 320 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_OBJECT_GRAPH_DEPTH = 64
MAX_OBJECT_GRAPH_NODES = 2_000_000
MAX_OBJECT_COLLECTION_ITEMS = 8_192
MAX_OBJECT_STRING_BYTES = 4_096
MAX_BACKUP_GRAPH_DEPTH = 64
MAX_BACKUP_GRAPH_NODES = 1_000_000
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256

_REGISTRY_PROCESS_LOCK = threading.RLock()
_REGISTRY_PROCESS_HEADS: dict[tuple[int, int, str, str], str] = {}
_REGISTRY_INSTANCE_SEALS: weakref.WeakKeyDictionary[
    object, tuple[object, ...]
] = weakref.WeakKeyDictionary()
_PINNED_E06_RESOLVE = ResultViewSourceRegistry.resolve
_PINNED_STATUS_FENCE = CohortRecordCatalog.record_status_authority_fence
_PINNED_VERIFY_REFERENCE = ResultCatalog.verify_reference
_PINNED_REPLAY_DECISION = replay_compatibility_decision
_PINNED_FRAGMENT_SOURCE = fragment_source_from_verified_bundle
_PINNED_BUILD_STATE = build_fragment_explorer_state
_PINNED_BUILD_VIEW = build_fragment_explorer_view
_PINNED_REPLAY_VIEW = replay_fragment_explorer_view

RegistryId = Annotated[
    str, StringConstraints(pattern=r"^familysrc_registry_[0-9a-f]{32}$")
]
ArtifactSelectorId = Annotated[
    str, StringConstraints(pattern=r"^familysrc_artifact_[0-9a-f]{40}$")
]
E06RegistryId = Annotated[str, StringConstraints(pattern=r"^e06_registry_[0-9a-f]{32}$")]
E06SelectorId = Annotated[str, StringConstraints(pattern=r"^e06_source_[0-9a-f]{40}$")]
CohortSelectorId = Annotated[
    str, StringConstraints(pattern=r"^cohort_selector_[0-9a-f]{40}$")
]
CohortRegistryId = Annotated[
    str, StringConstraints(pattern=r"^cohort_registry_[0-9a-f]{32}$")
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MeasurementSourceArtifactRegistryError(RuntimeError):
    """Sanitized registry failure."""


class MeasurementSourceArtifactRegistryConflict(MeasurementSourceArtifactRegistryError):
    pass


class MeasurementSourceArtifactNotApplicable(MeasurementSourceArtifactRegistryConflict):
    """The E06 source has no applicable, exactly bound family artifact."""


class MeasurementSourceArtifactRegistryStale(MeasurementSourceArtifactRegistryConflict):
    """A registered artifact no longer verifies against live authority."""


class MeasurementSourceArtifactRegistryUnsafe(MeasurementSourceArtifactRegistryError):
    pass


class SourceArtifactFamily(StrEnum):
    """Closed family vocabulary.  Only E07 binds to an E06 source today."""

    FRAGMENT = "fragment"


class ArtifactAuthorityState(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class MeasurementSourceArtifactRegistryMetadata(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-registry-metadata.v1"] = (
        "traceback.e12-family-source-registry-metadata.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    e06_registry_id: E06RegistryId
    e06_registry_epoch_sha256: Sha256
    # Digest of the E06 metadata, which binds the cohort registry, the D01
    # linkage store, the E04 catalog storage and reader registry, and the D06
    # record-catalog scope.
    e06_metadata_sha256: Sha256


_METADATA_MODEL_TYPES, _METADATA_ENUM_TYPES = contract_type_graph(
    MeasurementSourceArtifactRegistryMetadata
)


def _full_window(source: VerifiedFragmentSource) -> ExplorerControls:
    chart = source.chart
    if chart is None or not chart.rows:
        raise ValueError("fragment source has no verified chart rows")
    return ExplorerControls(
        bin_start_inclusive=0,
        bin_end_exclusive=len(chart.rows),
        minimum_count_inclusive=0,
    )


class RegisteredFragmentSourceArtifactObject(RegistryContract):
    """Stored live commitments, the two caller inputs, and the derived E07 view.

    The subject result is always panel A, the E06 counterpart panel B, and the
    controls are the fixed unlinked full-window, zero-threshold controls.
    """

    schema_version: Literal["traceback.e12-fragment-source-artifact-object.v1"] = (
        "traceback.e12-fragment-source-artifact-object.v1"
    )
    family: Literal[SourceArtifactFamily.FRAGMENT] = SourceArtifactFamily.FRAGMENT
    e06_registry_id: E06RegistryId
    e06_registry_epoch_sha256: Sha256
    e06_selector_id: E06SelectorId
    e06_source_version: int = Field(ge=1, le=16, strict=True)
    e06_object_sha256: Sha256
    e06_source_sha256: Sha256
    cohort_registry_id: CohortRegistryId
    cohort_registry_epoch_sha256: Sha256
    cohort_selector_id: CohortSelectorId
    cohort_version: int = Field(ge=1, le=100_000, strict=True)
    cohort_manifest_sha256: Sha256
    catalog_authority_sha256: Sha256
    member_sha256: Sha256
    binding_sha256: Sha256
    catalog_result_sha256: Sha256
    counterpart_member_sha256: Sha256
    counterpart_binding_sha256: Sha256
    counterpart_catalog_result_sha256: Sha256
    subject_panel: Literal[PanelId.A] = PanelId.A
    counterpart_record: VerifiedMeasurementRecord
    compatibility_policy: CompatibilityPolicy
    artifact: FragmentExplorerView

    @model_validator(mode="after")
    def fixed_parameters(self) -> RegisteredFragmentSourceArtifactObject:
        view = self.artifact
        state = view.state
        by_id = {item.record.result_id: item for item in view.request.sources}
        if len(view.request.sources) != 2:
            raise ValueError("fragment artifact must hold exactly two sources")
        if state.right.result_id != self.counterpart_record.result_id:
            raise ValueError("fragment artifact panel B is not the E06 counterpart")
        if state.left.result_id == self.counterpart_record.result_id:
            raise ValueError("fragment artifact subject must be panel A")
        if view.request.policy != self.compatibility_policy:
            raise ValueError("fragment artifact policy is not the stored policy")
        if (
            state.filters_linked
            or state.left_controls != _full_window(by_id[state.left.result_id])
            or state.right_controls != _full_window(by_id[state.right.result_id])
        ):
            raise ValueError("fragment artifact controls are not the fixed controls")
        if self.member_sha256 == self.counterpart_member_sha256:
            raise ValueError("fragment artifact counterpart must be another member")
        return self


_OBJECT_MODEL_TYPES, _OBJECT_ENUM_TYPES = contract_type_graph(
    RegisteredFragmentSourceArtifactObject
)


class MeasurementSourceArtifactJournalEntry(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-journal-entry.v1"] = (
        "traceback.e12-family-source-journal-entry.v1"
    )
    sequence: int = Field(ge=1, le=MAX_REGISTERED_ARTIFACTS, strict=True)
    previous_entry_sha256: Sha256
    selector_id: ArtifactSelectorId
    object_sha256: Sha256
    object_bytes: int = Field(ge=1, le=MAX_OBJECT_BYTES, strict=True)
    entry_sha256: Sha256


class SourceArtifactRegistrationReceipt(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-registration-receipt.v1"] = (
        "traceback.e12-family-source-registration-receipt.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_ARTIFACTS)
    state_head_sha256: Sha256
    selector_id: ArtifactSelectorId
    family: SourceArtifactFamily
    object_sha256: Sha256
    artifact_sha256: Sha256


class RegisteredFragmentSourceArtifact(RegistryContract):
    """Protected E07 artifact that re-verified against live E06/D06/E04 authority."""

    schema_version: Literal["traceback.e12-registered-fragment-source-artifact.v1"] = (
        "traceback.e12-registered-fragment-source-artifact.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=1, le=MAX_REGISTERED_ARTIFACTS)
    state_head_sha256: Sha256
    selector_id: ArtifactSelectorId
    object_sha256: Sha256
    family: Literal[SourceArtifactFamily.FRAGMENT] = SourceArtifactFamily.FRAGMENT
    e06_registry_id: E06RegistryId
    e06_registry_epoch_sha256: Sha256
    e06_state_head_sha256: Sha256
    e06_selector_id: E06SelectorId
    e06_source_version: int = Field(ge=1, le=16)
    e06_object_sha256: Sha256
    e06_source_sha256: Sha256
    e06_source_replay_sha256: Sha256
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
    counterpart_catalog_result_sha256: Sha256
    subject_panel: Literal[PanelId.A] = PanelId.A
    artifact_sha256: Sha256
    artifact: FragmentExplorerView
    # The E06 record fields no live authority verifies; the artifact embeds
    # both records, so it inherits exactly E06's list.
    caller_asserted_fields: tuple[CallerAssertedFields, ...] = CALLER_ASSERTED_FIELDS
    # E07 binds a record's bundle digest to the canonical E02 manifest; E06
    # binds it to the E04 bundle-tree digest.  The registry substitutes E04's
    # own ``bundle_manifest_sha256`` and requires the E06 result digest to equal
    # the canonical E04 measurement digest.
    e07_bundle_sha256_source: Literal["e04_bundle_manifest_sha256"] = (
        "e04_bundle_manifest_sha256"
    )
    e06_result_sha256_matches_e04_measurement: Literal[True] = True
    method_authority_head_current_verified: Literal[False] = False
    replayed_against_live_authority: Literal[True] = True
    synthetic_only: Literal[True] = True
    clinical_use_authorized: Literal[False] = False
    artifact_replay_sha256: Sha256

    @model_validator(mode="after")
    def exact_identity(self) -> RegisteredFragmentSourceArtifact:
        if self.selector_id != _selector_id(
            self.registry_epoch_sha256,
            self.family,
            self.e06_registry_id,
            self.e06_selector_id,
            self.e06_source_version,
        ):
            raise ValueError("registered artifact selector does not match its source")
        if self.artifact_sha256 != _artifact_sha256(self.artifact):
            raise ValueError("registered artifact digest is invalid")
        if self.caller_asserted_fields != CALLER_ASSERTED_FIELDS:
            raise ValueError("registered artifact unverified-field list is invalid")
        if self.artifact_replay_sha256 != _artifact_replay_sha256(self):
            raise ValueError("registered artifact replay digest is invalid")
        return self


class SourceArtifactSelectorRecord(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-selector-record.v1"] = (
        "traceback.e12-family-source-selector-record.v1"
    )
    selector_id: ArtifactSelectorId
    family: SourceArtifactFamily
    object_sha256: Sha256
    artifact_sha256: Sha256
    e06_source_sha256: Sha256
    authority_state: ArtifactAuthorityState


class SourceArtifactSelectorPage(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-selector-page.v1"] = (
        "traceback.e12-family-source-selector-page.v1"
    )
    registry_id: RegistryId
    registry_epoch_sha256: Sha256
    state_version: int = Field(ge=0, le=MAX_REGISTERED_ARTIFACTS)
    state_head_sha256: Sha256
    records: tuple[SourceArtifactSelectorRecord, ...] = Field(
        max_length=MAX_SELECTOR_PAGE
    )
    next_after_selector_id: ArtifactSelectorId | None


class MeasurementSourceArtifactBackupObject(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-backup-object.v1"] = (
        "traceback.e12-family-source-backup-object.v1"
    )
    object_sha256: Sha256
    object_json: Annotated[str, StringConstraints(max_length=MAX_OBJECT_BYTES)]


class MeasurementSourceArtifactBackup(RegistryContract):
    schema_version: Literal["traceback.e12-family-source-backup.v1"] = (
        "traceback.e12-family-source-backup.v1"
    )
    metadata: MeasurementSourceArtifactRegistryMetadata
    state_version: int = Field(ge=0, le=MAX_REGISTERED_ARTIFACTS)
    state_head_sha256: Sha256
    journal: tuple[MeasurementSourceArtifactJournalEntry, ...] = Field(
        max_length=MAX_REGISTERED_ARTIFACTS
    )
    objects: tuple[MeasurementSourceArtifactBackupObject, ...] = Field(
        max_length=MAX_REGISTERED_ARTIFACTS
    )


_BACKUP_MODEL_TYPES, _BACKUP_ENUM_TYPES = contract_type_graph(
    MeasurementSourceArtifactBackup
)
_CAPTURE_TYPES = {
    model: contract_type_graph(model)
    for model in (VerifiedMeasurementRecord, CompatibilityPolicy)
}


def registered_artifact_object_bytes(
    value: RegisteredFragmentSourceArtifactObject,
) -> bytes:
    """Return exact bounded canonical bytes for one stored artifact object."""

    return exact_model_bytes(
        value,
        RegisteredFragmentSourceArtifactObject,
        model_types=_OBJECT_MODEL_TYPES,
        enum_types=_OBJECT_ENUM_TYPES,
        max_bytes=MAX_OBJECT_BYTES,
        max_nodes=MAX_OBJECT_GRAPH_NODES,
        max_depth=MAX_OBJECT_GRAPH_DEPTH,
        max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
        max_string_bytes=MAX_OBJECT_STRING_BYTES,
    )


def registered_artifact_object_from_bytes(
    content: bytes,
) -> RegisteredFragmentSourceArtifactObject:
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_OBJECT_BYTES,
            max_depth=MAX_OBJECT_GRAPH_DEPTH,
            max_nodes=MAX_OBJECT_GRAPH_NODES,
            max_collection_items=MAX_OBJECT_COLLECTION_ITEMS,
            max_string_bytes=MAX_OBJECT_STRING_BYTES,
        )
        value = RegisteredFragmentSourceArtifactObject.model_validate_json(content)
        if registered_artifact_object_bytes(value) != content:
            raise ValueError("registered artifact object is not canonical")
        return value
    except (TypeError, ValueError):
        raise ValueError("registered artifact object is not canonical") from None


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
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source inputs are not exact canonical contracts"
        ) from None


def _canonical_backup_bytes(backup: MeasurementSourceArtifactBackup) -> bytes:
    return exact_model_bytes(
        backup,
        MeasurementSourceArtifactBackup,
        model_types=_BACKUP_MODEL_TYPES,
        enum_types=_BACKUP_ENUM_TYPES,
        max_bytes=MAX_BACKUP_BYTES,
        max_nodes=MAX_BACKUP_GRAPH_NODES,
        max_depth=MAX_BACKUP_GRAPH_DEPTH,
        max_collection_items=MAX_REGISTERED_ARTIFACTS,
        max_string_bytes=MAX_OBJECT_BYTES,
    )


def _snapshot_path(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is type(Path()):
        try:
            parts = object.__getattribute__(value, "_parts")
        except AttributeError:
            raise TypeError("family-source registry path is invalid") from None
        if (
            type(parts) is not list
            or not 1 <= len(parts) <= MAX_PATH_PARTS
            or any(type(part) is not str for part in parts)
        ):
            raise TypeError("family-source registry path is invalid")
        raw = os.path.join(*tuple(parts))
    else:
        raise TypeError("family-source registry path must be an exact string or path")
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("family-source registry path is invalid")
    path = Path(os.path.abspath(raw))
    if len(path.parts) > MAX_PATH_PARTS:
        raise ValueError("family-source registry path is invalid")
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
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry file exceeds its bound"
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
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry object is unsafe"
            )
        content = _read_bounded(descriptor, MAX_OBJECT_BYTES)
    except OSError:
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry object is unsafe"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != digest:
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry object digest is invalid"
        )
    return content


def _selector_id(
    epoch: str,
    family: str,
    e06_registry_id: str,
    e06_selector_id: str,
    e06_source_version: int,
) -> str:
    digest = hashlib.sha256(
        b"traceback-e12-family-source-selector-v1\0"
        + "\0".join(
            (
                epoch,
                str(family),
                e06_registry_id,
                e06_selector_id,
                str(e06_source_version),
            )
        ).encode("ascii")
    ).hexdigest()
    return f"familysrc_artifact_{digest[:40]}"


def _object_selector_id(
    epoch: str, value: RegisteredFragmentSourceArtifactObject
) -> str:
    return _selector_id(
        epoch,
        value.family,
        value.e06_registry_id,
        value.e06_selector_id,
        value.e06_source_version,
    )


def _contract_sha256(value: RegistryContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(value)).hexdigest()


def _artifact_sha256(view: FragmentExplorerView) -> str:
    return hashlib.sha256(canonical_json_bytes(view)).hexdigest()


def _artifact_replay_sha256(value: RegistryContract) -> str:
    """Digest every returned commitment except the digest itself.

    The artifact is covered through ``artifact_sha256``, which the validator
    binds to the embedded artifact, so no field can change without failing
    validation.
    """

    payload = value.model_dump(
        mode="json", exclude={"artifact", "artifact_replay_sha256"}
    )
    return hashlib.sha256(
        b"traceback-e12-family-source-replay-v1\0"
        + json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _journal_entry_sha256(entry: MeasurementSourceArtifactJournalEntry) -> str:
    placeholder = entry.model_copy(update={"entry_sha256": "0" * 64})
    return hashlib.sha256(
        b"traceback-e12-family-source-journal-v1\0"
        + canonical_contract_bytes(placeholder)
    ).hexdigest()


def _metadata_genesis_sha256(
    metadata: MeasurementSourceArtifactRegistryMetadata,
) -> str:
    return hashlib.sha256(
        b"traceback-e12-family-source-registry-genesis-v1\0"
        + canonical_contract_bytes(metadata)
    ).hexdigest()


def _build_journal_entry(
    *,
    sequence: int,
    previous_entry_sha256: str,
    selector_id: str,
    object_sha256: str,
    object_bytes: int,
) -> MeasurementSourceArtifactJournalEntry:
    placeholder = MeasurementSourceArtifactJournalEntry.model_construct(
        sequence=sequence,
        previous_entry_sha256=previous_entry_sha256,
        selector_id=selector_id,
        object_sha256=object_sha256,
        object_bytes=object_bytes,
        entry_sha256="0" * 64,
    )
    return MeasurementSourceArtifactJournalEntry(
        **placeholder.model_dump(mode="python", exclude={"entry_sha256"}),
        entry_sha256=_journal_entry_sha256(placeholder),
    )


def _validate_journal_semantics(
    epoch: str,
    journal: tuple[MeasurementSourceArtifactJournalEntry, ...],
    objects: dict[str, RegisteredFragmentSourceArtifactObject],
) -> None:
    """Bind each entry's selector to its object; one object per selector."""

    seen: set[str] = set()
    for entry in journal:
        selector = _object_selector_id(epoch, objects[entry.object_sha256])
        if entry.selector_id != selector or selector in seen:
            raise ValueError("journal selector does not match its object")
        seen.add(selector)


def _validate_backup(backup: MeasurementSourceArtifactBackup) -> None:
    if backup.state_version != len(backup.journal) or len(backup.objects) != len(
        backup.journal
    ):
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup count is invalid"
        )
    sizes: dict[str, int] = {}
    values: dict[str, RegisteredFragmentSourceArtifactObject] = {}
    previous_digest = ""
    for item in backup.objects:
        if item.object_sha256 <= previous_digest:
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup order is invalid"
            )
        previous_digest = item.object_sha256
        try:
            content = item.object_json.encode("utf-8")
            values[item.object_sha256] = registered_artifact_object_from_bytes(content)
        except (UnicodeError, ValueError):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup object is invalid"
            ) from None
        if hashlib.sha256(content).hexdigest() != item.object_sha256:
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup digest is invalid"
            )
        sizes[item.object_sha256] = len(content)
    if sum(sizes.values()) > MAX_TOTAL_OBJECT_BYTES:
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup exceeds its bound"
        )
    if {entry.object_sha256 for entry in backup.journal} != set(sizes):
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup journal is invalid"
        )
    previous = _metadata_genesis_sha256(backup.metadata)
    for sequence, entry in enumerate(backup.journal, start=1):
        if (
            entry.sequence != sequence
            or entry.previous_entry_sha256 != previous
            or entry.entry_sha256 != _journal_entry_sha256(entry)
            or entry.object_bytes != sizes[entry.object_sha256]
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup journal is invalid"
            )
        previous = entry.entry_sha256
    if previous != backup.state_head_sha256:
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup state is invalid"
        )
    try:
        _validate_journal_semantics(
            backup.metadata.registry_epoch_sha256, backup.journal, values
        )
    except ValueError:
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup journal is invalid"
        ) from None


def measurement_source_artifact_backup_from_bytes(
    content: bytes,
) -> MeasurementSourceArtifactBackup:
    if type(content) is not bytes or len(content) > MAX_BACKUP_BYTES:
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup exceeds its bound"
        )
    try:
        bounded_json_loads(
            content,
            max_bytes=MAX_BACKUP_BYTES,
            max_depth=MAX_BACKUP_GRAPH_DEPTH,
            max_nodes=MAX_BACKUP_GRAPH_NODES,
            max_collection_items=MAX_REGISTERED_ARTIFACTS,
            max_string_bytes=MAX_OBJECT_BYTES,
        )
        backup = MeasurementSourceArtifactBackup.model_validate_json(content)
        if _canonical_backup_bytes(backup) != content:
            raise ValueError("family-source registry backup is not canonical")
    except (TypeError, ValueError):
        raise MeasurementSourceArtifactRegistryConflict(
            "family-source registry backup is invalid"
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


def _new_metadata(authority: MappingProxyType) -> MeasurementSourceArtifactRegistryMetadata:
    return MeasurementSourceArtifactRegistryMetadata(
        registry_id=f"familysrc_registry_{secrets.token_hex(16)}",
        registry_epoch_sha256=secrets.token_hex(32),
        e06_registry_id=authority["_e06_registry_id"],
        e06_registry_epoch_sha256=authority["_e06_registry_epoch_sha256"],
        e06_metadata_sha256=authority["_e06_metadata_sha256"],
    )


def _metadata_authority(
    metadata: MeasurementSourceArtifactRegistryMetadata,
) -> tuple[str, str, str]:
    return (
        metadata.e06_registry_id,
        metadata.e06_registry_epoch_sha256,
        metadata.e06_metadata_sha256,
    )


def _live_authority(authority: MappingProxyType | dict) -> tuple[str, str, str]:
    return (
        authority["_e06_registry_id"],
        authority["_e06_registry_epoch_sha256"],
        authority["_e06_metadata_sha256"],
    )


_AUTHORITY_OBJECTS = (
    "_e06_registry",
    "_record_catalog",
    "_cohort_registry",
    "_linkage_store",
    "_result_catalog",
)


def _authority_identity(e06_registry: ResultViewSourceRegistry) -> dict[str, object]:
    """Read the E06 registry's own pinned D06/E04 authority without invoking hooks."""

    if type(e06_registry) is not ResultViewSourceRegistry:
        raise TypeError("family-source registry requires the exact E06 source registry")
    state = object.__getattribute__(e06_registry, "__dict__")
    if type(state) is not dict or any(
        name not in state for name in ("_record_catalog", "_metadata")
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "E06 source registry identity is invalid"
        )
    record_catalog = state["_record_catalog"]
    metadata = state["_metadata"]
    if (
        type(record_catalog) is not CohortRecordCatalog
        or type(metadata) is not ResultViewSourceRegistryMetadata
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "E06 source registry identity is invalid"
        )
    catalog_state = object.__getattribute__(record_catalog, "__dict__")
    names = ("_cohort_registry", "_linkage_store", "_result_catalog")
    if type(catalog_state) is not dict or any(
        name not in catalog_state for name in names
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "D06 record catalog identity is invalid"
        )
    if (
        type(catalog_state["_cohort_registry"]) is not CohortRegistry
        or type(catalog_state["_linkage_store"]) is not ProviderLinkageStore
        or type(catalog_state["_result_catalog"]) is not ResultCatalog
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "D06 record catalog identity is invalid"
        )
    return {
        "_e06_registry": e06_registry,
        "_record_catalog": record_catalog,
        "_cohort_registry": catalog_state["_cohort_registry"],
        "_linkage_store": catalog_state["_linkage_store"],
        "_result_catalog": catalog_state["_result_catalog"],
        "_e06_registry_id": metadata.registry_id,
        "_cohort_registry_id": metadata.cohort_registry_id,
        "_e06_registry_epoch_sha256": metadata.registry_epoch_sha256,
        "_e06_metadata_sha256": hashlib.sha256(
            canonical_contract_bytes(metadata)
        ).hexdigest(),
    }


def _binding_for_member(
    status: CohortManifestRecordStatus, member_sha256: str, binding_sha256: str
) -> CohortRecordBinding:
    """Return the live available binding the E06 source committed to."""

    matches = [item for item in status.members if item.member_sha256 == member_sha256]
    if len(matches) != 1:
        raise MeasurementSourceArtifactRegistryStale(
            "family-source member is not exactly one live member"
        )
    member = matches[0]
    if (
        member.availability is not CohortRecordAvailability.AVAILABLE
        or member.binding is None
        or member.binding.member_sha256 != member_sha256
        or _contract_sha256(member.binding) != binding_sha256
    ):
        raise MeasurementSourceArtifactRegistryStale(
            "family-source member binding is not current"
        )
    return member.binding


def _fragment_source(
    bundle: VerifiedBundle,
    record: VerifiedMeasurementRecord,
    reference: CatalogResultRef,
) -> VerifiedFragmentSource:
    """Build the one E07 source the verified E04 bundle and E06 record admit.

    E07 requires the record's result digest to be the canonical measurement
    digest and its bundle digest to be the canonical manifest digest.  E06
    pins the bundle digest to the E04 bundle tree, so the E07 record takes E04's
    own ``bundle_manifest_sha256`` and nothing else changes.  The quantity and
    source state are not chosen here: exactly one pair validates under E07.
    """

    if (
        type(bundle) is not VerifiedBundle
        or record.result_id != reference.result_id
        or record.result_sha256
        != hashlib.sha256(canonical_json_bytes(bundle.measurement)).hexdigest()
    ):
        raise MeasurementSourceArtifactNotApplicable(
            "E06 result digest does not bind the E04 fragment measurement"
        )
    try:
        payload = record.model_dump(mode="python")
        payload["bundle_sha256"] = reference.bundle_manifest_sha256
        e07_record = VerifiedMeasurementRecord.model_validate(payload)
    except ValueError:
        raise MeasurementSourceArtifactNotApplicable(
            "E06 record has no exact E07 identity"
        ) from None
    candidates: list[VerifiedFragmentSource] = []
    for quantity in FragmentQuantity:
        for state in ExplorerSourceState:
            try:
                candidate = _PINNED_FRAGMENT_SOURCE(
                    bundle, record=e07_record, quantity=quantity, state=state
                )
            except (ValueError, FragmentExplorerError):
                continue
            if type(candidate) is not VerifiedFragmentSource:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "E07 source type changed"
                )
            candidates.append(candidate)
    if len(candidates) != 1:
        raise MeasurementSourceArtifactNotApplicable(
            "E04 bundle is not exactly one applicable E07 fragment source"
        )
    return candidates[0]


def _reproduces_decision(
    record: VerifiedMeasurementRecord,
    counterpart_record: VerifiedMeasurementRecord,
    information_state: InformationState,
    policy: CompatibilityPolicy,
    decision: CompatibilityDecision,
) -> bool:
    """Return whether E05 replays ``decision`` with this counterpart state."""

    try:
        candidate = VerifiedMeasurementRecord.model_validate(
            {
                **counterpart_record.model_dump(mode="python"),
                "information_state": information_state,
            }
        )
        _PINNED_REPLAY_DECISION(
            CompatibilityRequest(
                left=record,
                right=candidate,
                policy=policy,
                trusted_policy_sha256=decision.binding.trusted_policy_sha256,
                trusted_authority_head_sha256=decision.binding.authority_head_sha256,
            ),
            decision,
        )
    except (CompatibilityContractError, ValueError):
        return False
    return True


def _decision_semantics(decision: object) -> tuple[object, ...]:
    return (
        decision.outcome,
        decision.mismatch_keys,
        decision.missing_fields,
        decision.delta_allowed,
        decision.shared_axis_allowed,
        decision.remediation_code,
    )


def _registry_instance_snapshot(
    registry: MeasurementSourceArtifactRegistry,
) -> tuple[object, ...]:
    """Capture authority-critical state without invoking caller-owned hooks."""

    instance = object.__getattribute__(registry, "__dict__")
    required = (
        "root",
        "_e06_registry",
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
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )
    metadata = instance["_metadata"]
    try:
        metadata_bytes = exact_model_bytes(
            metadata,
            MeasurementSourceArtifactRegistryMetadata,
            model_types=_METADATA_MODEL_TYPES,
            enum_types=_METADATA_ENUM_TYPES,
            max_bytes=4096,
            max_nodes=64,
            max_depth=8,
            max_collection_items=16,
            max_string_bytes=256,
        )
    except (TypeError, ValueError):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        ) from None
    descriptor = instance.get("_metadata_fd")
    descriptors = tuple(
        instance.get(name)
        for name in ("_root_fd", "_objects_fd", "_lock_fd", "_metadata_fd", "_journal_fd")
    )
    if descriptor is None:
        if any(item is not None for item in descriptors):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry authority state changed"
            )
    elif type(descriptor) is not int:
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )
    else:
        try:
            persisted = os.pread(descriptor, 4097, 0)
            root_observed = os.fstat(instance["_root_fd"])
            metadata_observed = os.fstat(descriptor)
        except (OSError, TypeError):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry authority state changed"
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
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry authority state changed"
            )
    authority = instance["_authority"]
    if type(authority) is not MappingProxyType:
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )
    live = _authority_identity(instance["_e06_registry"])
    if (
        set(authority) != set(live)
        or any(authority[name] is not live[name] for name in _AUTHORITY_OBJECTS)
        or any(
            authority[name] != live[name]
            for name in live
            if name not in _AUTHORITY_OBJECTS
        )
        or _metadata_authority(metadata) != _live_authority(live)
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )
    return (
        id(instance["root"]),
        id(instance["_e06_registry"]),
        tuple(
            (name, id(value) if name in _AUTHORITY_OBJECTS else value)
            for name, value in sorted(live.items())
        ),
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


def _seal_registry_instance(registry: MeasurementSourceArtifactRegistry) -> None:
    _REGISTRY_INSTANCE_SEALS[registry] = _registry_instance_snapshot(registry)


_LoadedState = dict[str, tuple[RegisteredFragmentSourceArtifactObject, bytes]]


class MeasurementSourceArtifactRegistry:
    """Immutable family-source artifact discovery with live re-verification."""

    def __getattribute__(self, name: str) -> object:
        # Keep this list literal: consulting a mutable module global here would let
        # an attacker disable the guard before installing an instance shadow.
        if name in (
            "recover_torn_journal_tail",
            "backup_bytes",
            "close",
            "list_selectors",
            "register_fragment_artifact",
            "resolve",
            "selector_for_e06_source",
        ):
            instance = object.__getattribute__(self, "__dict__")
            if name in instance:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry callable changed"
                )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        result_view_source_registry: ResultViewSourceRegistry,
        expected_state_head_sha256: str | None = None,
        expected_registry_id: str | None = None,
        expected_registry_epoch_sha256: str | None = None,
    ) -> None:
        _require_registry_integrity(self)
        if type(result_view_source_registry) is not ResultViewSourceRegistry:
            raise TypeError(
                "family-source registry requires the exact E06 source registry"
            )
        expected_values = (
            expected_registry_id,
            expected_registry_epoch_sha256,
            expected_state_head_sha256,
        )
        if any(item is not None for item in expected_values) and (
            any(item is None for item in expected_values)
            or not _is_token(expected_registry_id, "familysrc_registry_", 32)
            or not _is_sha256(expected_registry_epoch_sha256)
            or not _is_sha256(expected_state_head_sha256)
        ):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry expected identity or head is invalid"
            )
        self.root = _snapshot_path(root)
        self._e06_registry = result_view_source_registry
        self._authority = MappingProxyType(
            _authority_identity(result_view_source_registry)
        )
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry root must be private"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry root changed"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry objects are unsafe"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry lock is unsafe"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry journal is unsafe"
                )
            self._journal_identity = (journal_metadata.st_dev, journal_metadata.st_ino)
            with _MS_LOCK(self, exclusive=True):
                self._metadata = _MS_LOAD_OR_CREATE_METADATA(
                    self, allow_create=root_created
                )
                self._genesis_head_sha256 = _metadata_genesis_sha256(self._metadata)
                self._head_key = (
                    self._root_identity[0],
                    self._root_identity[1],
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                )
                _MS_RECOVER_TEMPORARY_OBJECTS(self)
                _, head = _MS_LOAD_STATE(self, check_trusted_head=False)
                if root_created:
                    if any(item is not None for item in expected_values):
                        raise MeasurementSourceArtifactRegistryUnsafe(
                            "new family-source registry cannot inherit an expected"
                            " identity"
                        )
                elif any(item is None for item in expected_values):
                    raise MeasurementSourceArtifactRegistryUnsafe(
                        "family-source registry expected identity and head are required"
                    )
                if not root_created and expected_values != (
                    self._metadata.registry_id,
                    self._metadata.registry_epoch_sha256,
                    head,
                ):
                    raise MeasurementSourceArtifactRegistryUnsafe(
                        "family-source registry expected identity or head is invalid"
                    )
                if staged_root is not None:
                    _commit_staged_root(staged_root, final_root, self._root_fd)
                    self.root = final_root
                    staged_root = None
                self._trusted_head_sha256 = head
                _MS_ACCEPT_OBSERVED_HEAD(
                    self, _MS_LOAD_JOURNAL(self), head, check_instance=False
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
            if staged_root is not None:
                _discard_staged_root(staged_root)
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

    def __enter__(self) -> MeasurementSourceArtifactRegistry:
        _require_registry_integrity(self)
        return self

    def __exit__(self, *_: object) -> None:
        _MS_CLOSE(self)

    def __del__(self) -> None:
        try:
            _MS_CLOSE(self)
        except Exception:
            pass

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            # Read the descriptor only under the process lock, which close()
            # also holds, so a concurrent close cannot hand us a reused number.
            descriptor = self._lock_fd
            if descriptor is None:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry is closed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                _MS_VALIDATE_STORAGE(self)
                yield
                _MS_VALIDATE_STORAGE(self)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
            or self._journal_fd is None
        ):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry is closed"
            )
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
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry storage changed"
            ) from None
        if (
            not stat.S_ISDIR(root_path.st_mode)
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (root_bound.st_dev, root_bound.st_ino) != self._root_identity
            or stat.S_IMODE(root_bound.st_mode) != 0o700
            or root_bound.st_uid != os.geteuid()
        ):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry storage changed"
            )
        for path_stat, bound_stat, identity, kind, mode in observed:
            if (
                not kind(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino) != identity
                or (bound_stat.st_dev, bound_stat.st_ino) != identity
                or stat.S_IMODE(bound_stat.st_mode) != mode
                or bound_stat.st_uid != os.geteuid()
            ):
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry storage changed"
                )

    def _publish(self, directory_fd: int, name: str, content: bytes) -> None:
        _publish_file(directory_fd, name, content)

    def _recover_temporary_objects(self) -> None:
        # D05 rule: an owned ``.tmp-<32 hex>`` name in the registry's private
        # root or objects directory is always unlinked under the exclusive
        # lock; a directory under that name makes unlink fail, so recovery
        # fails closed.
        if self._root_fd is None or self._objects_fd is None:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry is closed"
            )
        try:
            for directory_fd in (self._root_fd, self._objects_fd):
                _remove_owned_temporaries(directory_fd)
        except OSError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry recovery is unsafe"
            ) from None

    def _load_or_create_metadata(
        self, *, allow_create: bool
    ) -> MeasurementSourceArtifactRegistryMetadata:
        assert self._root_fd is not None
        authority = self._authority
        try:
            descriptor = os.open(
                "registry-metadata.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            if not allow_create:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry metadata is missing"
                ) from None
            try:
                _MS_PUBLISH(
                    self,
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(_new_metadata(authority)),
                )
            except FileExistsError:
                pass
            return _MS_LOAD_OR_CREATE_METADATA(self, allow_create=False)
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
            ):
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry metadata is unsafe"
                )
            content = _read_bounded(descriptor, 4096)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            metadata = contract_from_canonical_bytes(
                MeasurementSourceArtifactRegistryMetadata, content
            )
        except Exception:
            os.close(descriptor)
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry metadata is invalid"
            ) from None
        if _metadata_authority(metadata) != _live_authority(authority):
            os.close(descriptor)
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry E06 authority changed"
            )
        self._metadata_fd = descriptor
        self._metadata_identity = (observed.st_dev, observed.st_ino)
        return metadata

    def _load_journal(self) -> tuple[MeasurementSourceArtifactJournalEntry, ...]:
        descriptor = self._journal_fd
        if descriptor is None:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry is closed"
            )
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = _read_bounded(descriptor, MAX_JOURNAL_BYTES)
        except OSError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry journal is unavailable"
            ) from None
        if content and not content.endswith(b"\n"):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry journal is incomplete"
            )
        entries: list[MeasurementSourceArtifactJournalEntry] = []
        previous = self._genesis_head_sha256
        seen_objects: set[str] = set()
        total_bytes = 0
        for sequence, line in enumerate(content.splitlines(), start=1):
            if sequence > MAX_REGISTERED_ARTIFACTS:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry journal bound exceeded"
                )
            try:
                entry = contract_from_canonical_bytes(
                    MeasurementSourceArtifactJournalEntry, line
                )
            except Exception:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry journal is invalid"
                ) from None
            total_bytes += entry.object_bytes
            if (
                entry.sequence != sequence
                or entry.previous_entry_sha256 != previous
                or entry.entry_sha256 != _journal_entry_sha256(entry)
                or entry.object_sha256 in seen_objects
                or total_bytes > MAX_TOTAL_OBJECT_BYTES
            ):
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry journal is invalid"
                )
            entries.append(entry)
            previous = entry.entry_sha256
            seen_objects.add(entry.object_sha256)
        return tuple(entries)

    def _append_journal(self, entry: MeasurementSourceArtifactJournalEntry) -> None:
        descriptor = self._journal_fd
        if descriptor is None:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry is closed"
            )
        content = canonical_contract_bytes(entry) + b"\n"
        try:
            committed_size = os.fstat(descriptor).st_size
        except OSError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry journal append failed"
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
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry journal append failed"
            ) from None

    def _accept_observed_head(
        self,
        journal: tuple[MeasurementSourceArtifactJournalEntry, ...],
        head: str,
        *,
        check_instance: bool,
    ) -> None:
        chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
        process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
        if process_head is not None and process_head not in chain:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry state rollback detected"
            )
        if check_instance and self._trusted_head_sha256 not in chain:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry state rollback detected"
            )
        _REGISTRY_PROCESS_HEADS[self._head_key] = head
        if check_instance:
            self._trusted_head_sha256 = head
            _seal_registry_instance(self)

    def _load_state(
        self, *, check_trusted_head: bool = True
    ) -> tuple[_LoadedState, str]:
        """Load only journal-committed objects; extra or missing files fail closed."""

        if self._objects_fd is None:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry is closed"
            )
        journal = _MS_LOAD_JOURNAL(self)
        try:
            names = os.listdir(self._objects_fd)
        except OSError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry objects are unavailable"
            ) from None
        if len(names) > MAX_REGISTERED_ARTIFACTS + 1:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry object bound exceeded"
            )
        if any(
            type(name) is not str
            or len(name) != 69
            or not name.endswith(".json")
            or any(character not in "0123456789abcdef" for character in name[:64])
            for name in names
        ):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry contains an invalid object"
            )
        committed_names = {f"{entry.object_sha256}.json" for entry in journal}
        uncommitted = set(names) - committed_names
        # Publication writes the object before its journal entry, so at most one
        # exact uncommitted object can exist after an interrupted registration.
        if len(uncommitted) > 1 or not committed_names <= set(names):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry committed objects are inconsistent"
            )
        values: dict[str, RegisteredFragmentSourceArtifactObject] = {}
        contents: dict[str, bytes] = {}
        for entry in journal:
            content = _read_exact_object(self._objects_fd, entry.object_sha256)
            if len(content) != entry.object_bytes:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry journal binding is invalid"
                )
            try:
                value = registered_artifact_object_from_bytes(content)
            except ValueError:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry object is invalid"
                ) from None
            values[entry.object_sha256] = value
            contents[entry.object_sha256] = content
        try:
            _validate_journal_semantics(
                self._metadata.registry_epoch_sha256, journal, values
            )
        except ValueError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry journal binding is invalid"
            ) from None
        loaded: _LoadedState = {
            entry.object_sha256: (
                values[entry.object_sha256],
                contents[entry.object_sha256],
            )
            for entry in journal
        }
        head = journal[-1].entry_sha256 if journal else self._genesis_head_sha256
        if check_trusted_head:
            _MS_ACCEPT_OBSERVED_HEAD(self, journal, head, check_instance=True)
        else:
            process_head = _REGISTRY_PROCESS_HEADS.get(self._head_key)
            chain = {self._genesis_head_sha256, *(item.entry_sha256 for item in journal)}
            if process_head is not None and process_head not in chain:
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry state rollback detected"
                )
        return loaded, head

    def _resolve_e06(
        self,
        e06_selector_id: str,
        e06_source_version: int,
        *,
        expected_member_sha256: str,
        expected_result_id: str,
        stale_on_conflict: bool,
    ) -> RegisteredResultViewSource:
        """Resolve one E06 source; E06 takes and releases D06 and its own lock.

        This never runs while the D06 fence or this registry's lock is held:
        the D06 fence is not reentrant and E06 acquires it itself.
        """

        try:
            resolved = _PINNED_E06_RESOLVE(
                self._authority["_e06_registry"],
                e06_selector_id,
                e06_source_version,
                expected_member_sha256=expected_member_sha256,
                expected_result_id=expected_result_id,
            )
        except ResultViewSourceRegistryStale:
            raise MeasurementSourceArtifactRegistryStale(
                "E06 source is not current"
            ) from None
        except ResultViewSourceRegistryConflict:
            if stale_on_conflict:
                raise MeasurementSourceArtifactRegistryStale(
                    "E06 source is not current"
                ) from None
            raise MeasurementSourceArtifactRegistryConflict(
                "E06 source selector is unavailable"
            ) from None
        except ResultViewSourceRegistryError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "E06 source registry is unsafe"
            ) from None
        metadata = self._metadata
        if (
            type(resolved) is not RegisteredResultViewSource
            or resolved.registry_id != metadata.e06_registry_id
            or resolved.registry_epoch_sha256 != metadata.e06_registry_epoch_sha256
            or resolved.selector_id != e06_selector_id
            or resolved.source_version != e06_source_version
        ):
            raise MeasurementSourceArtifactRegistryUnsafe(
                "E06 source registry identity changed"
            )
        return resolved

    def _derive_in_fence(
        self,
        status: CohortManifestRecordStatus,
        resolved: RegisteredResultViewSource,
        *,
        counterpart_record: VerifiedMeasurementRecord,
        policy: CompatibilityPolicy,
    ) -> RegisteredFragmentSourceArtifactObject:
        """Derive one E07 artifact from fenced D06/E04 authority and the E06 source."""

        # The E06 source was verified against one exact D06 status.  Requiring
        # this fence to observe that same status closes the gap between the E06
        # resolve and this fence: any D01/D05/D06/E04 change alters the digest.
        if (
            status.status_sha256 != resolved.record_status_sha256
            or status.registry_id != resolved.cohort_registry_id
            or status.registry_epoch_sha256 != resolved.cohort_registry_epoch_sha256
            or status.selector_id != resolved.cohort_selector_id
            or status.cohort_version != resolved.cohort_version
            or status.cohort_manifest_sha256 != resolved.cohort_manifest_sha256
            or status.catalog_authority_sha256 != resolved.catalog_authority_sha256
        ):
            raise MeasurementSourceArtifactRegistryStale(
                "D06 authority changed after the E06 source was verified"
            )
        subject = _binding_for_member(
            status, resolved.member_sha256, resolved.binding_sha256
        )
        counterpart = _binding_for_member(
            status,
            resolved.counterpart_member_sha256,
            resolved.counterpart_binding_sha256,
        )
        record = resolved.source.record
        if (
            subject.result.result_id != record.result_id
            or _contract_sha256(subject.result) != resolved.catalog_result_sha256
        ):
            raise MeasurementSourceArtifactRegistryStale(
                "E06 source result is not the live member result"
            )
        if counterpart.result.result_id != counterpart_record.result_id:
            raise MeasurementSourceArtifactRegistryConflict(
                "counterpart record is not the E06 counterpart member"
            )
        decision = resolved.source.compatibility_decision
        # E06 admits a counterpart only when its live catalog result is complete
        # and verified, so those two states are fixed.  The decision binds every
        # other counterpart field except ``information_state``; E05 itself (the
        # pinned replay) decides whether the decision determines it.  If more
        # than one value reproduces the decision, the caller could choose panel
        # B's state, so the source has no deterministic artifact.
        if (
            counterpart_record.execution_state is not ExecutionState.COMPLETE
            or counterpart_record.trust_state is not TrustState.VERIFIED
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "counterpart record is not the E06-verified counterpart"
            )
        reproducing = tuple(
            state
            for state in InformationState
            if _reproduces_decision(
                record, counterpart_record, state, policy, decision
            )
        )
        if counterpart_record.information_state not in reproducing:
            raise MeasurementSourceArtifactRegistryConflict(
                "counterpart record and policy do not reproduce the E06 decision"
            )
        if len(reproducing) != 1:
            raise MeasurementSourceArtifactNotApplicable(
                "E06 decision does not determine the counterpart record"
            )
        if record.method.family is not MethodFamily.FRAGMENT_MEASUREMENT:
            raise MeasurementSourceArtifactNotApplicable(
                "E06 source is not a fragment measurement"
            )
        if decision.binding.trusted_policy_sha256 != compatibility_policy_sha256(policy):
            raise MeasurementSourceArtifactNotApplicable(
                "E06 decision does not pin the exact compatibility policy"
            )
        result_catalog = self._authority["_result_catalog"]
        try:
            subject_bundle, _ = _PINNED_VERIFY_REFERENCE(result_catalog, subject.result)
            counterpart_bundle, _ = _PINNED_VERIFY_REFERENCE(
                result_catalog, counterpart.result
            )
        except CatalogError:
            raise MeasurementSourceArtifactRegistryStale(
                "E04 result no longer verifies"
            ) from None
        subject_source = _fragment_source(subject_bundle, record, subject.result)
        counterpart_source = _fragment_source(
            counterpart_bundle, counterpart_record, counterpart.result
        )
        try:
            state = _PINNED_BUILD_STATE(
                left=ExplorerMethodSelection(
                    result_id=record.result_id, method_ref=record.method.method_ref
                ),
                right=ExplorerMethodSelection(
                    result_id=counterpart_record.result_id,
                    method_ref=counterpart_record.method.method_ref,
                ),
                filters_linked=False,
                left_controls=_full_window(subject_source),
                right_controls=_full_window(counterpart_source),
            )
            request = _MS_FRAGMENT_REQUEST(
                sources=tuple(
                    sorted(
                        (subject_source, counterpart_source),
                        key=lambda item: item.record.result_id,
                    )
                ),
                policy=policy,
                trusted_policy_sha256=decision.binding.trusted_policy_sha256,
                trusted_authority_head_sha256=decision.binding.authority_head_sha256,
                state=state,
            )
            view = _PINNED_BUILD_VIEW(request)
        except (ValueError, FragmentExplorerError):
            raise MeasurementSourceArtifactNotApplicable(
                "E06 source does not admit an E07 fragment artifact"
            ) from None
        if type(view) is not FragmentExplorerView:
            raise MeasurementSourceArtifactRegistryUnsafe("E07 view type changed")
        # E07 decides on records whose bundle digest is the manifest digest;
        # the comparison semantics must still be exactly E06's.
        if _decision_semantics(view.compatibility) != _decision_semantics(decision):
            raise MeasurementSourceArtifactRegistryConflict(
                "E07 comparison does not reproduce the E06 decision"
            )
        try:
            value = RegisteredFragmentSourceArtifactObject(
                e06_registry_id=resolved.registry_id,
                e06_registry_epoch_sha256=resolved.registry_epoch_sha256,
                e06_selector_id=resolved.selector_id,
                e06_source_version=resolved.source_version,
                e06_object_sha256=resolved.object_sha256,
                e06_source_sha256=resolved.source_sha256,
                cohort_registry_id=resolved.cohort_registry_id,
                cohort_registry_epoch_sha256=resolved.cohort_registry_epoch_sha256,
                cohort_selector_id=resolved.cohort_selector_id,
                cohort_version=resolved.cohort_version,
                cohort_manifest_sha256=resolved.cohort_manifest_sha256,
                catalog_authority_sha256=resolved.catalog_authority_sha256,
                member_sha256=resolved.member_sha256,
                binding_sha256=resolved.binding_sha256,
                catalog_result_sha256=resolved.catalog_result_sha256,
                counterpart_member_sha256=resolved.counterpart_member_sha256,
                counterpart_binding_sha256=resolved.counterpart_binding_sha256,
                counterpart_catalog_result_sha256=_contract_sha256(counterpart.result),
                counterpart_record=counterpart_record,
                compatibility_policy=policy,
                artifact=view,
            )
            return registered_artifact_object_from_bytes(
                registered_artifact_object_bytes(value)
            )
        except (TypeError, ValueError):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source artifact is not a valid registered object"
            ) from None

    @contextmanager
    def _cohort_fence(
        self, cohort_selector_id: str, cohort_version: int
    ) -> Iterator[CohortManifestRecordStatus]:
        """Hold the live D06 status fence; D06 failures become stale errors."""

        authority = self._authority
        try:
            with _PINNED_STATUS_FENCE(
                authority["_record_catalog"],
                cohort_selector_id,
                cohort_version,
                expected_registry=authority["_cohort_registry"],
                expected_linkage_store=authority["_linkage_store"],
            ) as (_, status):
                if type(status) is not CohortManifestRecordStatus:
                    raise MeasurementSourceArtifactRegistryStale(
                        "family-source cohort authority is unavailable"
                    )
                yield status
        except MeasurementSourceArtifactRegistryError:
            raise
        except CohortImportError:
            raise MeasurementSourceArtifactRegistryStale(
                "family-source cohort authority is not current"
            ) from None

    def _find(
        self, loaded: _LoadedState, selector_id: str
    ) -> tuple[str, RegisteredFragmentSourceArtifactObject]:
        epoch = self._metadata.registry_epoch_sha256
        matches = [
            (digest, value)
            for digest, (value, _) in loaded.items()
            if _MS_OBJECT_SELECTOR_ID(epoch, value) == selector_id
        ]
        if len(matches) != 1:
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source artifact selector is unavailable"
            )
        return matches[0]

    def selector_for_e06_source(
        self, e06_selector_id: str, e06_source_version: int
    ) -> str:
        """Return the opaque fragment-artifact selector for one E06 source (no I/O)."""

        _require_registry_integrity(self)
        if (
            not _is_token(e06_selector_id, "e06_source_", 40)
            or type(e06_source_version) is not int
            or not 1 <= e06_source_version <= 16
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "E06 source selector is invalid"
            )
        return _MS_SELECTOR_ID(
            self._metadata.registry_epoch_sha256,
            SourceArtifactFamily.FRAGMENT,
            self._metadata.e06_registry_id,
            e06_selector_id,
            e06_source_version,
        )

    def register_fragment_artifact(
        self,
        *,
        e06_selector_id: str,
        e06_source_version: int,
        expected_member_sha256: str,
        expected_result_id: str,
        counterpart_record: VerifiedMeasurementRecord,
        policy: CompatibilityPolicy,
    ) -> SourceArtifactRegistrationReceipt:
        """Derive one E07 artifact for one live E06 source and publish it.

        The caller supplies only the E06 selector/version and its expected
        member and result, plus the counterpart record and policy, which must
        reproduce the E06 decision exactly.  The artifact is always derived.
        """

        _require_registry_integrity(self)
        if (
            not _is_token(e06_selector_id, "e06_source_", 40)
            or type(e06_source_version) is not int
            or not 1 <= e06_source_version <= 16
            or not _is_sha256(expected_member_sha256)
            or not _is_token(expected_result_id, "result_", 40)
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registration input is invalid"
            )
        counterpart_record = _capture(counterpart_record, VerifiedMeasurementRecord)
        policy = _capture(policy, CompatibilityPolicy)
        resolved = _MS_RESOLVE_E06(
            self,
            e06_selector_id,
            e06_source_version,
            expected_member_sha256=expected_member_sha256,
            expected_result_id=expected_result_id,
            stale_on_conflict=False,
        )
        with _MS_COHORT_FENCE(
            self, resolved.cohort_selector_id, resolved.cohort_version
        ) as status:
            captured = _MS_DERIVE_IN_FENCE(
                self,
                status,
                resolved,
                counterpart_record=counterpart_record,
                policy=policy,
            )
            content = registered_artifact_object_bytes(captured)
            digest = hashlib.sha256(content).hexdigest()
            selector = _MS_OBJECT_SELECTOR_ID(
                self._metadata.registry_epoch_sha256, captured
            )
            with _MS_LOCK(self, exclusive=True):
                _MS_RECOVER_TEMPORARY_OBJECTS(self)
                loaded, head = _MS_LOAD_STATE(self)
                assert self._objects_fd is not None
                # The journal is the commit point: an object without an entry is
                # the remnant of an interrupted registration and is never adopted
                # unless its exact bytes are being registered again.
                for name in os.listdir(self._objects_fd):
                    if name[:64] not in loaded and name != f"{digest}.json":
                        _read_exact_object(self._objects_fd, name[:64])
                        os.unlink(name, dir_fd=self._objects_fd)
                os.fsync(self._objects_fd)
                epoch = self._metadata.registry_epoch_sha256
                existing = [
                    other_digest
                    for other_digest, (other, _) in loaded.items()
                    if _MS_OBJECT_SELECTOR_ID(epoch, other) == selector
                ]
                if existing:
                    if existing != [digest] or loaded[digest][1] != content:
                        raise MeasurementSourceArtifactRegistryConflict(
                            "E06 source already has a different family artifact"
                        )
                else:
                    if len(loaded) >= MAX_REGISTERED_ARTIFACTS:
                        raise MeasurementSourceArtifactRegistryConflict(
                            "family-source registry is full"
                        )
                    if (
                        sum(len(item[1]) for item in loaded.values()) + len(content)
                        > MAX_TOTAL_OBJECT_BYTES
                    ):
                        raise MeasurementSourceArtifactRegistryConflict(
                            "family-source registry byte bound would be exceeded"
                        )
                    try:
                        _MS_PUBLISH(self, self._objects_fd, f"{digest}.json", content)
                    except FileExistsError:
                        if _read_exact_object(self._objects_fd, digest) != content:
                            raise MeasurementSourceArtifactRegistryConflict(
                                "family-source publication conflicts"
                            ) from None
                    _MS_APPEND_JOURNAL(
                        self,
                        _build_journal_entry(
                            sequence=len(loaded) + 1,
                            previous_entry_sha256=head,
                            selector_id=selector,
                            object_sha256=digest,
                            object_bytes=len(content),
                        ),
                    )
                final, final_head = _MS_LOAD_STATE(self)
                if digest not in final or final[digest][1] != content:
                    raise MeasurementSourceArtifactRegistryUnsafe(
                        "family-source publication is unproven"
                    )
                return _MS_RECEIPT(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=epoch,
                    state_version=len(final),
                    state_head_sha256=final_head,
                    selector_id=selector,
                    family=captured.family,
                    object_sha256=digest,
                    artifact_sha256=_MS_ARTIFACT_SHA256(captured.artifact),
                )

    def resolve(
        self,
        selector_id: str,
        *,
        expected_e06_selector_id: str,
        expected_e06_source_version: int,
        expected_member_sha256: str,
        expected_result_id: str,
    ) -> RegisteredFragmentSourceArtifact:
        """Return one artifact only after it re-derives from live authority.

        Lock order: the object is located under a shared registry lock that is
        then released; the E06 source is resolved (E06 takes the D06 fence and
        its own lock and releases both); the D06 fence is taken and must observe
        the exact status E06 verified; then the shared registry lock is taken
        again and held with the D06 fence through construction of the result.
        """

        _require_registry_integrity(self)
        if (
            not _is_token(selector_id, "familysrc_artifact_", 40)
            or not _is_token(expected_e06_selector_id, "e06_source_", 40)
            or type(expected_e06_source_version) is not int
            or not 1 <= expected_e06_source_version <= 16
            or not _is_sha256(expected_member_sha256)
            or not _is_token(expected_result_id, "result_", 40)
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source artifact selector is invalid"
            )
        with _MS_LOCK(self, exclusive=False):
            loaded, _ = _MS_LOAD_STATE(self)
            located_digest, located = _MS_FIND(self, loaded, selector_id)
        if (
            located.e06_selector_id != expected_e06_selector_id
            or located.e06_source_version != expected_e06_source_version
            or located.member_sha256 != expected_member_sha256
            or located.artifact.state.left.result_id != expected_result_id
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source artifact does not bind the expected E06 source"
            )
        resolved = _MS_RESOLVE_E06(
            self,
            located.e06_selector_id,
            located.e06_source_version,
            expected_member_sha256=located.member_sha256,
            expected_result_id=expected_result_id,
            stale_on_conflict=True,
        )
        with _MS_COHORT_FENCE(
            self, resolved.cohort_selector_id, resolved.cohort_version
        ) as status:
            try:
                derived = _MS_DERIVE_IN_FENCE(
                    self,
                    status,
                    resolved,
                    counterpart_record=located.counterpart_record,
                    policy=located.compatibility_policy,
                )
            except MeasurementSourceArtifactRegistryConflict:
                raise MeasurementSourceArtifactRegistryStale(
                    "family-source artifact no longer derives from live authority"
                ) from None
            with _MS_LOCK(self, exclusive=False):
                loaded, head = _MS_LOAD_STATE(self)
                digest, value = _MS_FIND(self, loaded, selector_id)
                if digest != located_digest:
                    raise MeasurementSourceArtifactRegistryUnsafe(
                        "family-source registry object changed"
                    )
                if registered_artifact_object_bytes(derived) != loaded[digest][1]:
                    raise MeasurementSourceArtifactRegistryStale(
                        "family-source artifact no longer derives from live authority"
                    )
                try:
                    _PINNED_REPLAY_VIEW(value.artifact.request, value.artifact)
                except (ValueError, FragmentExplorerError):
                    raise MeasurementSourceArtifactRegistryStale(
                        "family-source artifact does not replay"
                    ) from None
                payload = dict(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    selector_id=selector_id,
                    object_sha256=digest,
                    e06_registry_id=resolved.registry_id,
                    e06_registry_epoch_sha256=resolved.registry_epoch_sha256,
                    e06_state_head_sha256=resolved.state_head_sha256,
                    e06_selector_id=resolved.selector_id,
                    e06_source_version=resolved.source_version,
                    e06_object_sha256=resolved.object_sha256,
                    e06_source_sha256=resolved.source_sha256,
                    e06_source_replay_sha256=resolved.source_replay_sha256,
                    cohort_registry_id=value.cohort_registry_id,
                    cohort_registry_epoch_sha256=value.cohort_registry_epoch_sha256,
                    cohort_selector_id=value.cohort_selector_id,
                    cohort_version=value.cohort_version,
                    cohort_manifest_sha256=value.cohort_manifest_sha256,
                    record_status_sha256=status.status_sha256,
                    catalog_authority_sha256=value.catalog_authority_sha256,
                    member_sha256=value.member_sha256,
                    binding_sha256=value.binding_sha256,
                    catalog_result_sha256=value.catalog_result_sha256,
                    counterpart_member_sha256=value.counterpart_member_sha256,
                    counterpart_binding_sha256=value.counterpart_binding_sha256,
                    counterpart_catalog_result_sha256=(
                        value.counterpart_catalog_result_sha256
                    ),
                    artifact_sha256=_MS_ARTIFACT_SHA256(value.artifact),
                    artifact=value.artifact,
                )
                placeholder = _MS_RESOLVED.model_construct(
                    **payload, artifact_replay_sha256="0" * 64
                )
                return _MS_RESOLVED(
                    **payload,
                    artifact_replay_sha256=_MS_ARTIFACT_REPLAY_SHA256(placeholder),
                )

    def list_selectors(
        self,
        cohort_selector_id: str,
        cohort_version: int,
        *,
        after_selector_id: str | None = None,
        limit: int = 50,
    ) -> SourceArtifactSelectorPage:
        """Return one bounded privacy-safe page for one cohort with live state.

        Each row's E06 source is resolved first (outside every fence); the page
        is then evaluated under one D06 fence that must observe the status each
        current E06 source was verified against, and the selection must be
        unchanged under the final registry lock.
        """

        _require_registry_integrity(self)
        if type(limit) is not int or not 1 <= limit <= MAX_SELECTOR_PAGE:
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source selector page bound is invalid"
            )
        if (
            not _is_token(cohort_selector_id, "cohort_selector_", 40)
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source cohort is invalid"
            )
        if after_selector_id is not None and not _is_token(
            after_selector_id, "familysrc_artifact_", 40
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source selector cursor is invalid"
            )

        def select(loaded: _LoadedState) -> list[tuple[str, str]]:
            epoch = self._metadata.registry_epoch_sha256
            ordered = sorted(
                (_MS_OBJECT_SELECTOR_ID(epoch, value), digest)
                for digest, (value, _) in loaded.items()
                if value.cohort_registry_id == self._authority["_cohort_registry_id"]
                and value.cohort_selector_id == cohort_selector_id
                and value.cohort_version == cohort_version
            )
            if after_selector_id is not None:
                ordered = [item for item in ordered if item[0] > after_selector_id]
            return ordered

        with _MS_LOCK(self, exclusive=False):
            loaded, _ = _MS_LOAD_STATE(self)
            ordered = select(loaded)
            selected = ordered[:limit]
            values = {digest: loaded[digest][0] for _, digest in selected}
        resolved: dict[str, RegisteredResultViewSource | None] = {}
        for _, digest in selected:
            value = values[digest]
            try:
                resolved[digest] = _MS_RESOLVE_E06(
                    self,
                    value.e06_selector_id,
                    value.e06_source_version,
                    expected_member_sha256=value.member_sha256,
                    expected_result_id=value.artifact.state.left.result_id,
                    stale_on_conflict=True,
                )
            except MeasurementSourceArtifactRegistryStale:
                resolved[digest] = None
        with _MS_COHORT_FENCE(self, cohort_selector_id, cohort_version) as status:
            states: dict[str, ArtifactAuthorityState] = {}
            for _, digest in selected:
                source = resolved[digest]
                if source is not None and (
                    source.record_status_sha256 != status.status_sha256
                ):
                    raise MeasurementSourceArtifactRegistryStale(
                        "family-source authority changed during the page read"
                    )
                state = ArtifactAuthorityState.STALE
                if source is not None:
                    value = values[digest]
                    try:
                        derived = _MS_DERIVE_IN_FENCE(
                            self,
                            status,
                            source,
                            counterpart_record=value.counterpart_record,
                            policy=value.compatibility_policy,
                        )
                    except MeasurementSourceArtifactRegistryConflict:
                        pass
                    else:
                        if registered_artifact_object_bytes(
                            derived
                        ) == registered_artifact_object_bytes(value):
                            state = ArtifactAuthorityState.CURRENT
                states[digest] = state
            with _MS_LOCK(self, exclusive=False):
                loaded, head = _MS_LOAD_STATE(self)
                final_ordered = select(loaded)
                if final_ordered[:limit] != selected:
                    raise MeasurementSourceArtifactRegistryStale(
                        "family-source registry changed during the page read"
                    )
                rows = [
                    _MS_SELECTOR_RECORD(
                        selector_id=selector_id,
                        family=values[digest].family,
                        object_sha256=digest,
                        artifact_sha256=_MS_ARTIFACT_SHA256(values[digest].artifact),
                        e06_source_sha256=values[digest].e06_source_sha256,
                        authority_state=states[digest],
                    )
                    for selector_id, digest in selected
                ]
                more = len(final_ordered) > len(selected)
                return _MS_SELECTOR_PAGE(
                    registry_id=self._metadata.registry_id,
                    registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                    state_version=len(loaded),
                    state_head_sha256=head,
                    records=tuple(rows),
                    next_after_selector_id=(
                        rows[-1].selector_id if more and rows else None
                    ),
                )

    def backup_bytes(self) -> bytes:
        """Return one protected, canonical, consistent registry backup bundle."""

        _require_registry_integrity(self)
        with _MS_LOCK(self, exclusive=False):
            loaded, head = _MS_LOAD_STATE(self)
            backup = MeasurementSourceArtifactBackup(
                metadata=self._metadata,
                state_version=len(loaded),
                state_head_sha256=head,
                journal=_MS_LOAD_JOURNAL(self),
                objects=tuple(
                    MeasurementSourceArtifactBackupObject(
                        object_sha256=digest, object_json=content.decode("utf-8")
                    )
                    for digest, (_, content) in sorted(loaded.items())
                ),
            )
            try:
                return _canonical_backup_bytes(backup)
            except (TypeError, ValueError):
                raise MeasurementSourceArtifactRegistryConflict(
                    "family-source registry backup exceeds its bound"
                ) from None

    @classmethod
    def restore(
        cls,
        root: str | Path,
        backup_content: bytes,
        *,
        result_view_source_registry: ResultViewSourceRegistry,
        expected_registry_id: str,
        expected_registry_epoch_sha256: str,
        expected_state_head_sha256: str,
    ) -> MeasurementSourceArtifactRegistry:
        """Restore a verified bundle into one new private registry root."""

        _require_registry_class_integrity(cls)
        if type(result_view_source_registry) is not ResultViewSourceRegistry:
            raise TypeError(
                "family-source registry requires the exact E06 source registry"
            )
        backup = measurement_source_artifact_backup_from_bytes(backup_content)
        if (
            type(expected_registry_id) is not str
            or type(expected_registry_epoch_sha256) is not str
            or type(expected_state_head_sha256) is not str
            or expected_registry_id != backup.metadata.registry_id
            or expected_registry_epoch_sha256 != backup.metadata.registry_epoch_sha256
            or expected_state_head_sha256 != backup.state_head_sha256
        ):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup expected head is invalid"
            )
        authority = _authority_identity(result_view_source_registry)
        if _metadata_authority(backup.metadata) != _live_authority(authority):
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry backup authority is invalid"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry restore parent changed"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry restore root changed"
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
                raise MeasurementSourceArtifactRegistryUnsafe(
                    "family-source registry restore objects changed"
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
            # Reopen through the normal checks before the restore counts as
            # complete, so a target that cannot open is removed, not left to
            # block a retry.
            restored = _MS_CONSTRUCT(
                target,
                result_view_source_registry=result_view_source_registry,
                expected_registry_id=expected_registry_id,
                expected_registry_epoch_sha256=expected_registry_epoch_sha256,
                expected_state_head_sha256=expected_state_head_sha256,
            )
            completed = True
        except FileExistsError:
            raise MeasurementSourceArtifactRegistryConflict(
                "family-source registry restore target already exists"
            ) from None
        except OSError:
            raise MeasurementSourceArtifactRegistryUnsafe(
                "family-source registry restore failed"
            ) from None
        finally:
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
                MeasurementSourceArtifactRegistryMetadata, content
            ),
            genesis_sha256=_metadata_genesis_sha256,
            parse_entry=lambda line: contract_from_canonical_bytes(
                MeasurementSourceArtifactJournalEntry, line
            ),
            entry_sha256=_journal_entry_sha256,
            max_journal_bytes=MAX_JOURNAL_BYTES,
            max_entries=MAX_REGISTERED_ARTIFACTS,
            process_lock=_REGISTRY_PROCESS_LOCK,
            error=MeasurementSourceArtifactRegistryUnsafe,
            label="family-source registry",
        )


_REGISTRY_METHOD_SEAL = MappingProxyType(
    {
        name: MeasurementSourceArtifactRegistry.__dict__[name]
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
            "_resolve_e06",
            "_derive_in_fence",
            "_cohort_fence",
            "_find",
            "selector_for_e06_source",
            "register_fragment_artifact",
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
    if cls is not MeasurementSourceArtifactRegistry or any(
        MeasurementSourceArtifactRegistry.__dict__.get(name) is not expected
        for name, expected in _REGISTRY_METHOD_SEAL.items()
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry callable changed"
        )


def _require_registry_integrity(registry: MeasurementSourceArtifactRegistry) -> None:
    _require_registry_class_integrity(type(registry))
    if any(name in vars(registry) for name in _REGISTRY_METHOD_SEAL):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry callable changed"
        )
    authority_sources = {
        "_PINNED_E06_RESOLVE": ResultViewSourceRegistry.__dict__.get("resolve"),
        "_PINNED_STATUS_FENCE": CohortRecordCatalog.__dict__.get(
            "record_status_authority_fence"
        ),
        "_PINNED_VERIFY_REFERENCE": ResultCatalog.__dict__.get("verify_reference"),
        "_PINNED_REPLAY_DECISION": e05_module.replay_compatibility_decision,
        "_PINNED_FRAGMENT_SOURCE": e07_module.fragment_source_from_verified_bundle,
        "_PINNED_BUILD_STATE": e07_module.build_fragment_explorer_state,
        "_PINNED_BUILD_VIEW": e07_module.build_fragment_explorer_view,
        "_PINNED_REPLAY_VIEW": e07_module.replay_fragment_explorer_view,
    }
    if any(
        globals().get(name) is not expected or authority_sources[name] is not expected
        for name, expected in _REGISTRY_AUTHORITY_SEAL.items()
    ) or any(
        globals().get(name) is not expected
        for name, expected in _REGISTRY_ALIAS_SEAL.items()
    ):
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority callable changed"
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
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )
    expected = _REGISTRY_INSTANCE_SEALS.get(registry)
    if expected is None or _registry_instance_snapshot(registry) != expected:
        raise MeasurementSourceArtifactRegistryUnsafe(
            "family-source registry authority state changed"
        )


_MS_CONSTRUCT = MeasurementSourceArtifactRegistry
_MS_CLOSE = MeasurementSourceArtifactRegistry.close
_MS_LOCK = MeasurementSourceArtifactRegistry._lock
_MS_VALIDATE_STORAGE = MeasurementSourceArtifactRegistry._validate_storage
_MS_PUBLISH = MeasurementSourceArtifactRegistry._publish
_MS_RECOVER_TEMPORARY_OBJECTS = (
    MeasurementSourceArtifactRegistry._recover_temporary_objects
)
_MS_LOAD_OR_CREATE_METADATA = MeasurementSourceArtifactRegistry._load_or_create_metadata
_MS_LOAD_JOURNAL = MeasurementSourceArtifactRegistry._load_journal
_MS_APPEND_JOURNAL = MeasurementSourceArtifactRegistry._append_journal
_MS_ACCEPT_OBSERVED_HEAD = MeasurementSourceArtifactRegistry._accept_observed_head
_MS_LOAD_STATE = MeasurementSourceArtifactRegistry._load_state
_MS_RESOLVE_E06 = MeasurementSourceArtifactRegistry._resolve_e06
_MS_DERIVE_IN_FENCE = MeasurementSourceArtifactRegistry._derive_in_fence
_MS_COHORT_FENCE = MeasurementSourceArtifactRegistry._cohort_fence
_MS_FIND = MeasurementSourceArtifactRegistry._find
# Result constructors and identity helpers are sealed so a module-global
# replacement cannot pair one selector with another source's artifact.
_MS_FRAGMENT_REQUEST = FragmentExplorerRequest
_MS_RECEIPT = SourceArtifactRegistrationReceipt
_MS_RESOLVED = RegisteredFragmentSourceArtifact
_MS_SELECTOR_RECORD = SourceArtifactSelectorRecord
_MS_SELECTOR_PAGE = SourceArtifactSelectorPage
_MS_SELECTOR_ID = _selector_id
_MS_OBJECT_SELECTOR_ID = _object_selector_id
_MS_ARTIFACT_SHA256 = _artifact_sha256
_MS_ARTIFACT_REPLAY_SHA256 = _artifact_replay_sha256
_REGISTRY_AUTHORITY_SEAL = MappingProxyType(
    {
        "_PINNED_E06_RESOLVE": _PINNED_E06_RESOLVE,
        "_PINNED_STATUS_FENCE": _PINNED_STATUS_FENCE,
        "_PINNED_VERIFY_REFERENCE": _PINNED_VERIFY_REFERENCE,
        "_PINNED_REPLAY_DECISION": _PINNED_REPLAY_DECISION,
        "_PINNED_FRAGMENT_SOURCE": _PINNED_FRAGMENT_SOURCE,
        "_PINNED_BUILD_STATE": _PINNED_BUILD_STATE,
        "_PINNED_BUILD_VIEW": _PINNED_BUILD_VIEW,
        "_PINNED_REPLAY_VIEW": _PINNED_REPLAY_VIEW,
    }
)
_REGISTRY_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_MS_CONSTRUCT",
            "_MS_CLOSE",
            "_MS_LOCK",
            "_MS_VALIDATE_STORAGE",
            "_MS_PUBLISH",
            "_MS_RECOVER_TEMPORARY_OBJECTS",
            "_MS_LOAD_OR_CREATE_METADATA",
            "_MS_LOAD_JOURNAL",
            "_MS_APPEND_JOURNAL",
            "_MS_ACCEPT_OBSERVED_HEAD",
            "_MS_LOAD_STATE",
            "_MS_RESOLVE_E06",
            "_MS_DERIVE_IN_FENCE",
            "_MS_COHORT_FENCE",
            "_MS_FIND",
            "_MS_FRAGMENT_REQUEST",
            "_MS_RECEIPT",
            "_MS_RESOLVED",
            "_MS_SELECTOR_RECORD",
            "_MS_SELECTOR_PAGE",
            "_MS_SELECTOR_ID",
            "_MS_OBJECT_SELECTOR_ID",
            "_MS_ARTIFACT_SHA256",
            "_MS_ARTIFACT_REPLAY_SHA256",
        )
    }
)


__all__ = [
    "ArtifactAuthorityState",
    "MeasurementSourceArtifactBackup",
    "MeasurementSourceArtifactBackupObject",
    "MeasurementSourceArtifactJournalEntry",
    "MeasurementSourceArtifactNotApplicable",
    "MeasurementSourceArtifactRegistry",
    "MeasurementSourceArtifactRegistryConflict",
    "MeasurementSourceArtifactRegistryError",
    "MeasurementSourceArtifactRegistryMetadata",
    "MeasurementSourceArtifactRegistryStale",
    "MeasurementSourceArtifactRegistryUnsafe",
    "RegisteredFragmentSourceArtifact",
    "RegisteredFragmentSourceArtifactObject",
    "SourceArtifactFamily",
    "SourceArtifactRegistrationReceipt",
    "SourceArtifactSelectorPage",
    "SourceArtifactSelectorRecord",
    "measurement_source_artifact_backup_from_bytes",
    "registered_artifact_object_bytes",
    "registered_artifact_object_from_bytes",
]
