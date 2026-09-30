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
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

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
    CatalogResultRef,
    ResultBundleReaderRegistry,
    ResultCatalog,
)
from traceback_runner.serialization import canonical_json_bytes

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
_PINNED_READER_SELECT = ResultBundleReaderRegistry.select
_PINNED_VALIDATE_MANIFEST = validate_manifest_against_linkage_store


class CohortImportError(RuntimeError):
    """Sanitized record-import failure for local operator surfaces."""


class CohortImportConflict(CohortImportError):
    pass


class CohortImportFilesystemError(CohortImportError):
    pass


class CohortRecordBinding(RegistryContract):
    """Immutable link from one trusted aggregate record to one D05 member."""

    schema_version: Literal["traceback.cohort-record-binding.v1"] = (
        "traceback.cohort-record-binding.v1"
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


def _binding_id(
    *,
    cohort_manifest_sha256: str,
    provider_namespace: str,
    analysis_record_id: str,
    result_id: str,
) -> str:
    payload = canonical_json_bytes(
        {
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "provider_namespace": provider_namespace,
            "analysis_record_id": analysis_record_id,
            "result_id": result_id,
        }
    )
    return (
        "binding_"
        + hashlib.sha256(b"traceback-cohort-record-binding-v1\0" + payload).hexdigest()
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

    def __init__(
        self,
        root: str | Path,
        *,
        result_catalog: ResultCatalog,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
        reader_registry: ResultBundleReaderRegistry,
    ) -> None:
        if type(result_catalog) is not ResultCatalog:
            raise TypeError("cohort import requires the exact result catalog type")
        for name, pinned in (
            ("import_bundle", _PINNED_RESULT_IMPORT),
            ("verify_reference", _PINNED_RESULT_VERIFY),
            ("query", _PINNED_RESULT_QUERY),
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
        self.result_catalog = result_catalog
        self.linkage_store = linkage_store
        self.expected_trust_snapshot_sha256_by_provider = pins
        self.reader_registry = normalized_registry
        self._reader_registry_bytes = canonical_json_bytes(normalized_registry)
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
        if (
            type(self.reader_registry) is not ResultBundleReaderRegistry
            or "select" in vars(self.reader_registry)
            or ResultBundleReaderRegistry.select is not _PINNED_READER_SELECT
        ):
            raise CohortImportError("cohort import reader registry is invalid")
        try:
            current = canonical_json_bytes(self.reader_registry)
            reparsed = ResultBundleReaderRegistry.model_validate_json(current)
        except Exception:
            raise CohortImportError(
                "cohort import reader registry is invalid"
            ) from None
        if (
            current != self._reader_registry_bytes
            or reparsed != self.reader_registry
            or reparsed != self.result_catalog.reader_registry
        ):
            raise CohortImportError("cohort import reader registry changed")

    def _read(self, name: str) -> CohortRecordBinding:
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
        except Exception:
            raise CohortImportFilesystemError(
                "cohort record binding file is invalid"
            ) from None
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
        if name != (f"{binding.cohort_manifest_sha256}.{binding.binding_id}.json"):
            raise CohortImportFilesystemError("cohort record binding name is invalid")
        return binding

    def _names_unlocked(self) -> tuple[str, ...]:
        self._validate_root()
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
        """Verify, import, and bind one aggregate record; no caller trust shortcut."""

        history = _canonical_history(manifest_history)
        manifest = history[-1]
        self._validate_reader_registry()
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
        try:
            _PINNED_VALIDATE_MANIFEST(
                manifest,
                self.linkage_store,
                expected_trust_snapshot_sha256_by_provider=(
                    self.expected_trust_snapshot_sha256_by_provider
                ),
            )
        except Exception as exc:
            raise CohortImportError(
                "cohort manifest is not current and trusted"
            ) from exc
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
                (
                    member.provider_namespace,
                    member.collection_token,
                ),
            ),
        )
        reference = _PINNED_RESULT_IMPORT(
            self.result_catalog,
            root_id=root_id,
            relative_path=relative_path,
            registry=registry,
            authority_head=authority_head,
            expected_authority_head_sha256=expected_authority_head_sha256,
            capability=capability,
            aliases=aliases,
        )
        _, reader = _PINNED_RESULT_VERIFY(self.result_catalog, reference)
        try:
            _PINNED_VALIDATE_MANIFEST(
                manifest,
                self.linkage_store,
                expected_trust_snapshot_sha256_by_provider=(
                    self.expected_trust_snapshot_sha256_by_provider
                ),
            )
        except Exception as exc:
            raise CohortImportError("cohort manifest changed during import") from exc

        manifest_digest = cohort_manifest_sha256(manifest)
        binding = CohortRecordBinding(
            binding_id=_binding_id(
                cohort_manifest_sha256=manifest_digest,
                provider_namespace=member.provider_namespace,
                analysis_record_id=member.analysis_record_id,
                result_id=reference.result_id,
            ),
            cohort_id=manifest.cohort_id,
            cohort_version=manifest.version,
            cohort_manifest_sha256=manifest_digest,
            provider_namespace=member.provider_namespace,
            analysis_record_id=member.analysis_record_id,
            member_sha256=_member_sha256(member),
            lineage_role=member.lineage_role,
            denominator_contribution=member.denominator_contribution,
            measurement_anchor_sha256=manifest.measurement_anchor.measurement_definition_sha256,
            result=reference,
            reader_registry_sha256=_reader_registry_sha256(self.reader_registry),
            reader_id=reader.reader_id,
            reader_minimum_version=reader.minimum_version,
            reader_maximum_version=reader.maximum_version,
        )
        content = canonical_contract_bytes(binding)
        name = f"{manifest_digest}.{binding.binding_id}.json"
        with _PROCESS_LOCK:
            self._validate_root()
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            try:
                names = self._names_unlocked()
                existing = tuple(
                    self._read(existing_name)
                    for existing_name in names
                    if existing_name.startswith(f"{manifest_digest}.")
                )
                for item in existing:
                    same_member = (
                        item.cohort_manifest_sha256 == binding.cohort_manifest_sha256
                        and item.provider_namespace == binding.provider_namespace
                        and item.analysis_record_id == binding.analysis_record_id
                    )
                    same_result = (
                        item.cohort_manifest_sha256 == binding.cohort_manifest_sha256
                        and item.result.result_id == binding.result.result_id
                    )
                    if same_member or same_result:
                        if item == binding:
                            return item
                        raise CohortImportConflict("cohort record binding conflicts")
                if len(names) >= MAX_BINDINGS:
                    raise CohortImportFilesystemError(
                        "cohort record index exceeds its bound"
                    )
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
                    if self._read(name) == binding:
                        return binding
                    raise CohortImportConflict(
                        "cohort record binding conflicts"
                    ) from None
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
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        return binding

    def bindings_for_manifest(
        self, manifest_history: Sequence[CohortManifest]
    ) -> tuple[CohortRecordBinding, ...]:
        """Return only bindings still valid under live linkage and current trust."""

        history = _canonical_history(manifest_history)
        manifest = history[-1]
        self._validate_reader_registry()
        try:
            _PINNED_VALIDATE_MANIFEST(
                manifest,
                self.linkage_store,
                expected_trust_snapshot_sha256_by_provider=(
                    self.expected_trust_snapshot_sha256_by_provider
                ),
            )
        except Exception as exc:
            raise CohortImportError(
                "cohort manifest is not current and trusted"
            ) from exc
        digest = cohort_manifest_sha256(manifest)
        members = {
            (item.provider_namespace, item.analysis_record_id): item
            for item in manifest.members
        }
        with _PROCESS_LOCK:
            self._validate_root()
            fcntl.flock(self._root_fd, fcntl.LOCK_SH)
            try:
                selected = tuple(
                    self._read(name)
                    for name in self._names_unlocked()
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
                self.reader_registry
            ):
                raise CohortImportConflict("cohort record reader registry changed")
            _, reader = _PINNED_RESULT_VERIFY(self.result_catalog, binding.result)
            if (
                reader.reader_id != binding.reader_id
                or reader.minimum_version != binding.reader_minimum_version
                or reader.maximum_version != binding.reader_maximum_version
            ):
                raise CohortImportConflict("cohort record reader binding changed")
        return tuple(
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


__all__ = [
    "CohortImportConflict",
    "CohortImportError",
    "CohortImportFilesystemError",
    "CohortRecordBinding",
    "CohortRecordCatalog",
    "MAX_BINDINGS",
]
