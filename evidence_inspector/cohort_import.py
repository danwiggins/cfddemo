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
import stat
import threading
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

import evidence_inspector.cohort_manifest as cohort_manifest_module
import evidence_inspector.result_catalog as result_catalog_module
from evidence_inspector.cohort_manifest import (
    CohortManifest,
    CohortMember,
    MemberLineageRole,
    cohort_manifest_bytes,
    cohort_manifest_from_bytes,
    cohort_manifest_sha256,
    validate_manifest_against_linkage_store,
    validate_manifest_history,
)
from evidence_inspector.fault_controller import NO_FAULTS, DeterministicFaultController
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
from evidence_inspector.provider_linkage_store import ProviderLinkageStore
from evidence_inspector.result_catalog import (
    CatalogAliases,
    CatalogAuthoritySnapshot,
    CatalogResultRef,
    PendingCatalogPublication,
    PreparedCatalogImport,
    PublicationId,
    ResultBundleReaderRegistry,
    ResultCatalog,
    _authority_value_fingerprint,
    catalog_authority_sha256,
)
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import RevokedKeyError

MAX_BINDINGS = 100_000
MAX_BINDING_BYTES = 128 * 1024
BindingId = Annotated[str, StringConstraints(pattern=r"^binding_[0-9a-f]{64}$")]
_PROVIDER_NAMESPACE = TypeAdapter(ProviderNamespace)
_ANALYSIS_RECORD_ID = TypeAdapter(AnalysisRecordId)
_SHA256 = TypeAdapter(Sha256)

_PROCESS_LOCK = threading.RLock()
_PINNED_RESULT_IMPORT = ResultCatalog.import_bundle
_PINNED_RESULT_VERIFY = ResultCatalog.verify_reference
_PINNED_RESULT_QUERY = ResultCatalog.query
_PINNED_RESULT_AUTHORITY = ResultCatalog.authority_snapshot
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
_PINNED_READER_SELECT = ResultBundleReaderRegistry.select
_PINNED_VALIDATE_MANIFEST = validate_manifest_against_linkage_store
_PINNED_RESULT_RUNTIME_ASSERT = result_catalog_module._RC_ASSERT_RUNTIME
_PINNED_RESULT_MODULE_VERIFY = result_catalog_module._PINNED_VERIFY_BUNDLE
_PINNED_RESULT_MODULE_TRUST_RESOLVE = result_catalog_module._PINNED_TRUST_RESOLVE
_PINNED_MANIFEST_ACTIVE_SNAPSHOT = cohort_manifest_module._PINNED_ACTIVE_SNAPSHOT
_PINNED_MANIFEST_STORE_CALLABLES = cohort_manifest_module._PINNED_STORE_CALLABLES


class CohortImportError(RuntimeError):
    """Sanitized record-import failure for local operator surfaces."""


class CohortImportConflict(CohortImportError):
    pass


class CohortImportFilesystemError(CohortImportError):
    pass


class CohortRecordBinding(RegistryContract):
    """Immutable link from one trusted aggregate record to one D05 member."""

    schema_version: Literal["traceback.cohort-record-binding.v2"] = (
        "traceback.cohort-record-binding.v2"
    )
    binding_id: BindingId
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
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
            cohort_manifest_sha256=self.cohort_manifest_sha256,
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
        return self


class CohortRecordAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    WITHHELD = "withheld"


class CohortRecordWithheldReason(StrEnum):
    RESULT_KEY_REVOKED = "result_key_revoked"


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
    schema_version: Literal["traceback.cohort-manifest-record-status.v1"] = (
        "traceback.cohort-manifest-record-status.v1"
    )
    cohort_id: str = Field(pattern=r"^cohort_[0-9a-f]{32}$")
    cohort_version: int = Field(ge=1, le=100_000)
    cohort_manifest_sha256: Sha256
    linkage_snapshot_sha256: Sha256
    catalog_authority_sha256: Sha256
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
        return self


def _binding_id(
    *,
    cohort_manifest_sha256: str,
    provider_namespace: str,
    analysis_record_id: str,
    result_id: str,
    publication_id: str,
    catalog_authority_sha256: str,
) -> str:
    payload = canonical_json_bytes(
        {
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "provider_namespace": provider_namespace,
            "analysis_record_id": analysis_record_id,
            "result_id": result_id,
            "publication_id": publication_id,
            "catalog_authority_sha256": catalog_authority_sha256,
        }
    )
    return (
        "binding_"
        + hashlib.sha256(b"traceback-cohort-record-binding-v2\0" + payload).hexdigest()
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


def _canonical_history(history: Sequence[CohortManifest]) -> tuple[CohortManifest, ...]:
    if not history or len(history) > 100_000:
        raise CohortImportError("cohort manifest history count is invalid")
    try:
        normalized = tuple(
            cohort_manifest_from_bytes(cohort_manifest_bytes(item)) for item in history
        )
        validate_manifest_history(normalized)
    except Exception as exc:
        raise CohortImportError("cohort manifest history is invalid") from exc
    return normalized


def _canonical_contract(
    model: type[RegistryContract], value: object
) -> RegistryContract:
    try:
        return contract_from_canonical_bytes(model, canonical_contract_bytes(value))
    except Exception as exc:
        raise CohortImportError("import authority contract is invalid") from exc


class CohortRecordCatalog:
    """Append-only protected binding index over the immutable result catalog."""

    _PINNED_FIELDS = frozenset(
        {
            "_result_catalog",
            "_result_catalog_identity",
            "_result_trust_store",
            "_result_reader_registry",
            "_catalog_storage_identity_sha256",
            "_catalog_reader_identity_sha256",
            "_linkage_store",
            "_linkage_store_identity",
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
        ):
            if (
                name in vars(result_catalog)
                or getattr(ResultCatalog, name) is not pinned
            ):
                raise TypeError("result catalog authority callable was shadowed")
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("cohort import requires the exact linkage store type")
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
            pins = {
                _PROVIDER_NAMESPACE.validate_python(provider): _SHA256.validate_python(
                    digest
                )
                for provider, digest in expected_trust_snapshot_sha256_by_provider.items()
            }
        except Exception as exc:
            raise CohortImportError("provider trust pins are invalid") from exc
        if not pins or len(pins) > 256:
            raise CohortImportError("provider trust pin count is invalid")
        authority = _PINNED_RESULT_AUTHORITY(result_catalog)
        self._result_catalog = result_catalog
        self._result_catalog_identity = id(result_catalog)
        self._result_trust_store = result_catalog.trust_store
        self._result_reader_registry = result_catalog.reader_registry
        self._catalog_storage_identity_sha256 = authority.storage_identity_sha256
        self._catalog_reader_identity_sha256 = authority.reader_registry_sha256
        self._linkage_store = linkage_store
        self._linkage_store_identity = id(linkage_store)
        self._expected_trust_snapshot_sha256_by_provider = MappingProxyType(pins)
        self._reader_registry = normalized_registry
        self._reader_registry_bytes = canonical_json_bytes(normalized_registry)
        if type(fault_controller) is not DeterministicFaultController:
            raise TypeError("fault controller must be exact")
        self._fault_controller = fault_controller
        self._fault_controller_identity = id(fault_controller)
        self._fault_controller_configuration = fault_controller.configuration
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

    def _recover_pending(self) -> None:
        """Reconcile durable journals without ever removing shared objects."""

        with _PROCESS_LOCK:
            _CC_VALIDATE_ROOT(self)
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            try:
                entries = tuple(sorted(os.listdir(self._root_fd)))
                if len(entries) > MAX_BINDINGS * 2:
                    raise CohortImportFilesystemError(
                        "cohort record recovery inventory exceeds its bound"
                    )

                def purge_files(publication_id: str, journal_name: str) -> None:
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
                        if name.startswith(".pending."):
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
                        matches_publication = False
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
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
                        if same_inode or matches_publication:
                            os.unlink(name, dir_fd=self._root_fd)
                    if journal_stat is not None:
                        os.unlink(journal_name, dir_fd=self._root_fd)
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
                    sorted(
                        name
                        for name in os.listdir(self._root_fd)
                        if name.startswith(".pending.")
                    )
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
                    state = _PINNED_RESULT_RECOVER(
                        self._result_catalog,
                        publication_id=binding.publication_id,
                        reference=binding.result,
                        recovery_scope_sha256=self._recovery_scope_sha256,
                    )
                    keep = state == "adopted"
                    final_exists = False
                    try:
                        os.stat(final_name, dir_fd=self._root_fd, follow_symlinks=False)
                        final_exists = True
                    except FileNotFoundError:
                        pass
                    if keep and final_exists:
                        pending_stat = os.stat(
                            pending_name,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                        final_stat = os.stat(
                            final_name,
                            dir_fd=self._root_fd,
                            follow_symlinks=False,
                        )
                        keep = (
                            (pending_stat.st_dev, pending_stat.st_ino)
                            == (final_stat.st_dev, final_stat.st_ino)
                            and pending_stat.st_nlink == 2
                            and final_stat.st_nlink == 2
                        )
                    if keep and final_exists:
                        os.unlink(pending_name, dir_fd=self._root_fd)
                        os.fsync(self._root_fd)
                        if _CC_READ(self, final_name) != binding:
                            raise CohortImportConflict(
                                "recovered cohort publication conflicts"
                            )
                        continue
                    if state == "adopted":
                        _PINNED_RESULT_RECOVER(
                            self._result_catalog,
                            publication_id=binding.publication_id,
                            reference=binding.result,
                            recovery_scope_sha256=self._recovery_scope_sha256,
                            retain_adopted=False,
                        )
                    if final_exists:
                        os.unlink(final_name, dir_fd=self._root_fd)
                    os.unlink(pending_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                valid_publications: set[str] = set()
                valid_result_ids: set[str] = set()
                for name in tuple(sorted(os.listdir(self._root_fd))):
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
            or catalog.reader_registry is not self._result_reader_registry
        ):
            raise CohortImportError("result catalog authority changed")
        for name, pinned in (
            ("verify_reference", _PINNED_RESULT_VERIFY),
            ("query", _PINNED_RESULT_QUERY),
            ("authority_snapshot", _PINNED_RESULT_AUTHORITY),
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
        ):
            if name in vars(catalog) or getattr(ResultCatalog, name) is not pinned:
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
        if (
            type(controller) is not DeterministicFaultController
            or id(controller) != self._fault_controller_identity
            or controller.configuration != self._fault_controller_configuration
        ):
            raise CohortImportError("cohort fault controller changed")
        controller.hit(point)

    def _validate_manifest(
        self, manifest: CohortManifest, *, changed: bool = False
    ) -> None:
        _CC_VALIDATE_CATALOG_AUTHORITY(self)
        try:
            _PINNED_VALIDATE_MANIFEST(
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
        names = tuple(sorted(os.listdir(self._root_fd)))
        if len(names) > MAX_BINDINGS:
            raise CohortImportFilesystemError("cohort record index exceeds its bound")
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
        ignored = {"binding_id", "publication_id", "catalog_result_preexisting"}
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

    def _revalidate_publication(
        self,
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
        manifest_history: Sequence[CohortManifest],
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

        history = _canonical_history(manifest_history)
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
        registry = _canonical_contract(MethodRegistry, registry)
        authority_head = _canonical_contract(AuthorityHead, authority_head)
        capability = _canonical_contract(CurrentMethodCapability, capability)
        assert isinstance(registry, MethodRegistry)
        assert isinstance(authority_head, AuthorityHead)
        assert isinstance(capability, CurrentMethodCapability)
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
        final_published = False
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
            _, reader = _PINNED_RESULT_VERIFY_PREPARED(self._result_catalog, prepared)
            manifest_digest = cohort_manifest_sha256(manifest)
            binding = CohortRecordBinding(
                binding_id=_binding_id(
                    cohort_manifest_sha256=manifest_digest,
                    provider_namespace=member.provider_namespace,
                    analysis_record_id=member.analysis_record_id,
                    result_id=prepared.reference.result_id,
                    publication_id=prepared.publication_id,
                    catalog_authority_sha256=prepared.authority_sha256,
                ),
                cohort_id=manifest.cohort_id,
                cohort_version=manifest.version,
                cohort_manifest_sha256=manifest_digest,
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
                            if _CC_BINDING_EQUIVALENT(item, binding):
                                _CC_FAULT(self, "before_idempotent_return")
                                _CC_REVALIDATE_PUBLICATION(
                                    self,
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
                    _CC_FAULT(self, "after_result_stage")
                    _CC_REVALIDATE_PUBLICATION(
                        self, manifest, prepared, binding, None, changed=True
                    )
                    _CC_FAULT(self, "before_binding_publish")
                    _CC_REVALIDATE_PUBLICATION(
                        self, manifest, prepared, binding, None, changed=True
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
                        self, manifest, prepared, binding, final_name, changed=True
                    )

                    def revalidate(point: str) -> None:
                        _CC_FAULT(self, point)
                        _CC_REVALIDATE_PUBLICATION(
                            self, manifest, prepared, binding, final_name, changed=True
                        )

                    _PINNED_RESULT_ADOPT(
                        self._result_catalog,
                        prepared,
                        revalidate=revalidate,
                    )
                    _CC_FAULT(self, "after_visibility_commit")
                    _CC_REVALIDATE_PUBLICATION(
                        self, manifest, prepared, binding, final_name, changed=True
                    )
                    _PINNED_RESULT_VERIFY(self._result_catalog, prepared.reference)
                    os.unlink(temporary_name, dir_fd=self._root_fd)
                    temporary_name = None
                    os.fsync(self._root_fd)
                    _CC_VALIDATE_ROOT(self)
                    if _CC_READ(self, final_name) != binding:
                        raise CohortImportConflict("cohort record binding changed")
                    _PINNED_RESULT_FINISH(self._result_catalog, prepared)
                    prepared = None
                    return binding
                finally:
                    fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        except BaseException:
            cleanup_errors: list[BaseException] = []
            if prepared is not None:
                try:
                    _PINNED_RESULT_COMPENSATE(self._result_catalog, prepared)
                except Exception as exc:  # noqa: BLE001 - surface failed compensation
                    cleanup_errors.append(exc)
            if final_published and final_name is not None:
                try:
                    os.unlink(final_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_errors.append(exc)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self._root_fd)
                    os.fsync(self._root_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise CohortImportError(
                    "cohort import compensation failed"
                ) from cleanup_errors[0]
            raise

    def bindings_for_manifest(
        self, manifest_history: Sequence[CohortManifest]
    ) -> tuple[CohortRecordBinding, ...]:
        """Return only bindings still valid under live linkage and current trust."""

        history = _canonical_history(manifest_history)
        manifest = history[-1]
        _CC_VALIDATE_READER_REGISTRY(self)
        _CC_VALIDATE_MANIFEST(self, manifest)
        authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        digest = cohort_manifest_sha256(manifest)
        members = {
            (item.provider_namespace, item.analysis_record_id): item
            for item in manifest.members
        }
        with _PROCESS_LOCK:
            _CC_VALIDATE_ROOT(self)
            fcntl.flock(self._root_fd, fcntl.LOCK_SH)
            try:
                selected = tuple(
                    _CC_READ(self, name)
                    for name in _CC_NAMES_UNLOCKED(self)
                    if name.startswith(f"{digest}.")
                )
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        for binding in selected:
            member = members.get(
                (binding.provider_namespace, binding.analysis_record_id)
            )
            if (
                member is None
                or binding.cohort_id != manifest.cohort_id
                or binding.cohort_version != manifest.version
                or _member_sha256(member) != binding.member_sha256
                or binding.lineage_role != member.lineage_role
                or binding.denominator_contribution != member.denominator_contribution
                or binding.measurement_anchor_sha256
                != manifest.measurement_anchor.measurement_definition_sha256
            ):
                raise CohortImportConflict(
                    "cohort record binding conflicts with manifest"
                )
            if binding.reader_registry_sha256 != _reader_registry_sha256(
                self._reader_registry
            ):
                raise CohortImportConflict("cohort record reader registry changed")
            bound_authority = CatalogAuthoritySnapshot(
                storage_identity_sha256=binding.catalog_storage_identity_sha256,
                trust_snapshot_sha256=binding.result_trust_snapshot_sha256,
                reader_registry_sha256=authority.reader_registry_sha256,
            )
            if (
                binding.catalog_authority_sha256
                != catalog_authority_sha256(bound_authority)
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
        if _CC_VALIDATE_CATALOG_AUTHORITY(self) != authority:
            raise CohortImportConflict("cohort record catalog authority changed")
        return ordered

    def record_status_for_manifest(
        self, manifest_history: Sequence[CohortManifest]
    ) -> CohortManifestRecordStatus:
        """Return complete canonical availability without exposing withheld results."""

        history = _canonical_history(manifest_history)
        manifest = history[-1]
        _CC_VALIDATE_READER_REGISTRY(self)
        _CC_VALIDATE_MANIFEST(self, manifest)
        authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        digest = cohort_manifest_sha256(manifest)
        members = {
            (item.provider_namespace, item.analysis_record_id): item
            for item in manifest.members
        }
        with _PROCESS_LOCK:
            _CC_VALIDATE_ROOT(self)
            fcntl.flock(self._root_fd, fcntl.LOCK_SH)
            try:
                selected = tuple(
                    _CC_READ(self, name)
                    for name in _CC_NAMES_UNLOCKED(self)
                    if name.startswith(f"{digest}.")
                )
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        indexed: dict[tuple[str, str], CohortRecordBinding] = {}
        for binding in selected:
            key = (binding.provider_namespace, binding.analysis_record_id)
            member = members.get(key)
            if (
                member is None
                or key in indexed
                or binding.cohort_id != manifest.cohort_id
                or binding.cohort_version != manifest.version
                or binding.cohort_manifest_sha256 != digest
                or _member_sha256(member) != binding.member_sha256
                or binding.lineage_role != member.lineage_role
                or binding.denominator_contribution != member.denominator_contribution
                or binding.measurement_anchor_sha256
                != manifest.measurement_anchor.measurement_definition_sha256
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
            bound_authority = CatalogAuthoritySnapshot(
                storage_identity_sha256=binding.catalog_storage_identity_sha256,
                trust_snapshot_sha256=binding.result_trust_snapshot_sha256,
                reader_registry_sha256=authority.reader_registry_sha256,
            )
            if (
                binding.catalog_authority_sha256
                != catalog_authority_sha256(bound_authority)
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
        final_authority = _CC_VALIDATE_CATALOG_AUTHORITY(self)
        if final_authority != authority:
            raise CohortImportConflict("cohort record catalog authority changed")
        linkage_snapshot = _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store)
        _CC_VALIDATE_MANIFEST(self, manifest, changed=True)
        if _PINNED_MANIFEST_ACTIVE_SNAPSHOT(self._linkage_store) != linkage_snapshot:
            raise CohortImportConflict("cohort linkage authority changed")
        linkage_snapshot_sha256 = hashlib.sha256(
            canonical_json_bytes(linkage_snapshot.model_dump(mode="json"))
        ).hexdigest()
        payload = {
            "schema_version": "traceback.cohort-manifest-record-status.v1",
            "cohort_id": manifest.cohort_id,
            "cohort_version": manifest.version,
            "cohort_manifest_sha256": digest,
            "linkage_snapshot_sha256": linkage_snapshot_sha256,
            "catalog_authority_sha256": catalog_authority_sha256(final_authority),
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


_COHORT_METHOD_SEAL = MappingProxyType(
    {
        name: getattr(CohortRecordCatalog, name)
        for name in (
            "_binding_equivalent",
            "_fault",
            "_names_unlocked",
            "_read",
            "_read_pending",
            "_recover_pending",
            "_revalidate_publication",
            "_validate_catalog_authority",
            "_validate_manifest",
            "_validate_reader_registry",
            "_validate_root",
            "_write_pending",
            "bindings_for_manifest",
            "import_bundle",
            "record_status_for_manifest",
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
        "manifest_validator": _authority_value_fingerprint(_PINNED_VALIDATE_MANIFEST),
        "result_assert": _authority_value_fingerprint(_PINNED_RESULT_RUNTIME_ASSERT),
        "active_snapshot": _authority_value_fingerprint(
            _PINNED_MANIFEST_ACTIVE_SNAPSHOT
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
    expected_manifest_validator: object = _PINNED_VALIDATE_MANIFEST,
    expected_result_assert: object = _PINNED_RESULT_RUNTIME_ASSERT,
) -> None:
    if type(catalog) is not CohortRecordCatalog:
        raise CohortImportError("cohort authority type changed")
    for name, expected in expected_methods.items():
        current = getattr(CohortRecordCatalog, name)
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
        globals().get("_PINNED_VALIDATE_MANIFEST") is not expected_manifest_validator
        or globals().get("_PINNED_RESULT_RUNTIME_ASSERT") is not expected_result_assert
        or _authority_value_fingerprint(_PINNED_VALIDATE_MANIFEST)
        != _COHORT_PINNED_FINGERPRINTS["manifest_validator"]
        or _authority_value_fingerprint(_PINNED_RESULT_RUNTIME_ASSERT)
        != _COHORT_PINNED_FINGERPRINTS["result_assert"]
        or _authority_value_fingerprint(_PINNED_MANIFEST_ACTIVE_SNAPSHOT)
        != _COHORT_PINNED_FINGERPRINTS["active_snapshot"]
        or any(
            _authority_value_fingerprint(value)
            != _COHORT_PINNED_FINGERPRINTS[f"store:{name}"]
            for name, value in _PINNED_MANIFEST_STORE_CALLABLES.items()
        )
    ):
        raise CohortImportError("cohort module authority changed")


_CC_ASSERT_RUNTIME = _assert_cohort_runtime
_CC_BINDING_EQUIVALENT = CohortRecordCatalog._binding_equivalent
_CC_FAULT = CohortRecordCatalog._fault
_CC_NAMES_UNLOCKED = CohortRecordCatalog._names_unlocked
_CC_READ = CohortRecordCatalog._read
_CC_READ_PENDING = CohortRecordCatalog._read_pending
_CC_RECOVER_PENDING = CohortRecordCatalog._recover_pending
_CC_REVALIDATE_PUBLICATION = CohortRecordCatalog._revalidate_publication
_CC_VALIDATE_CATALOG_AUTHORITY = CohortRecordCatalog._validate_catalog_authority
_CC_VALIDATE_MANIFEST = CohortRecordCatalog._validate_manifest
_CC_VALIDATE_READER_REGISTRY = CohortRecordCatalog._validate_reader_registry
_CC_VALIDATE_ROOT = CohortRecordCatalog._validate_root
_CC_WRITE_PENDING = CohortRecordCatalog._write_pending
_COHORT_ALIAS_SEAL = MappingProxyType(
    {
        name: globals()[name]
        for name in (
            "_CC_BINDING_EQUIVALENT",
            "_CC_FAULT",
            "_CC_NAMES_UNLOCKED",
            "_CC_READ",
            "_CC_READ_PENDING",
            "_CC_RECOVER_PENDING",
            "_CC_REVALIDATE_PUBLICATION",
            "_CC_VALIDATE_CATALOG_AUTHORITY",
            "_CC_VALIDATE_MANIFEST",
            "_CC_VALIDATE_READER_REGISTRY",
            "_CC_VALIDATE_ROOT",
            "_CC_WRITE_PENDING",
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
    "CohortManifestRecordStatus",
    "CohortMemberRecordStatus",
    "CohortRecordAvailability",
    "CohortRecordBinding",
    "CohortRecordCatalog",
    "CohortRecordWithheldReason",
]
