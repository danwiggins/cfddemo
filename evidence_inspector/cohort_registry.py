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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

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

MAX_REGISTERED_MANIFESTS = 100_000
MAX_SELECTOR_PAGE = 100
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_PATH_PARTS = 256
_REGISTRY_PROCESS_LOCK = threading.RLock()

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


def _selector_id(epoch: str, cohort_id: str) -> str:
    digest = hashlib.sha256(
        b"traceback-cohort-selector-v1\0"
        + epoch.encode("ascii")
        + b"\0"
        + cohort_id.encode("ascii")
    ).hexdigest()
    return f"cohort_selector_{digest[:40]}"


def _state_head(manifests: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256(b"traceback-cohort-registry-state-v1\0")
    for manifest_sha256, content in manifests:
        digest.update(manifest_sha256.encode("ascii") + b"\0")
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


class CohortRegistry:
    """Descriptor-relative immutable manifest publication and safe projection."""

    def __init__(
        self,
        root: str | Path,
        *,
        linkage_store: ProviderLinkageStore,
        expected_trust_snapshot_sha256_by_provider: Mapping[str, str],
    ) -> None:
        if type(linkage_store) is not ProviderLinkageStore:
            raise TypeError("cohort registry requires the exact linkage store type")
        self.root = _snapshot_path(root)
        self._linkage_store = linkage_store
        self._trust_pins = capture_expected_trust_pins(
            expected_trust_snapshot_sha256_by_provider
        )
        if (
            self._linkage_store.active_snapshot().trust_pins_sha256
            != trust_pins_sha256(self._trust_pins)
        ):
            raise CohortRegistryUnsafe("cohort registry trust pins are invalid")
        self._root_fd: int | None = None
        self._objects_fd: int | None = None
        self._lock_fd: int | None = None
        self._metadata_fd: int | None = None
        self._process_lock = threading.RLock()
        try:
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            except FileExistsError:
                pass
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
            try:
                os.mkdir("objects", 0o700, dir_fd=self._root_fd)
            except FileExistsError:
                pass
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
                | os.O_CREAT
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
            with self._lock(exclusive=True):
                self._metadata = self._load_or_create_metadata()
                self._recover_temporary_objects()
                self._load_state()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            return
        with lock:
            for name in ("_metadata_fd", "_lock_fd", "_objects_fd", "_root_fd"):
                descriptor = getattr(self, name, None)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, name, None)

    def __enter__(self) -> CohortRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = self._lock_fd
        if descriptor is None:
            raise CohortRegistryUnsafe("cohort registry is closed")
        with _REGISTRY_PROCESS_LOCK, self._process_lock:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                self._validate_storage()
                yield
                self._validate_storage()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _validate_storage(self) -> None:
        if (
            self._root_fd is None
            or self._objects_fd is None
            or self._lock_fd is None
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

    def _load_or_create_metadata(self) -> CohortRegistryMetadata:
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
            snapshot = self._linkage_store.active_snapshot()
            metadata = CohortRegistryMetadata(
                registry_id=f"cohort_registry_{secrets.token_hex(16)}",
                registry_epoch_sha256=secrets.token_hex(32),
                linkage_store_id=snapshot.store_id,
                linkage_store_epoch_sha256=snapshot.store_epoch_sha256,
                linkage_storage_identity_sha256=snapshot.storage_identity_sha256,
                linkage_trust_pins_sha256=snapshot.trust_pins_sha256,
            )
            try:
                self._publish(
                    self._root_fd,
                    "registry-metadata.json",
                    canonical_contract_bytes(metadata),
                )
            except FileExistsError:
                pass
            return self._load_or_create_metadata()
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
        snapshot = self._linkage_store.active_snapshot()
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

    def _load_state(
        self,
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
        cohorts: dict[str, list[CohortManifest]] = {}
        for manifest, _ in loaded.values():
            cohorts.setdefault(manifest.cohort_id, []).append(manifest)
        try:
            for history in cohorts.values():
                history.sort(key=lambda item: item.version)
                validate_manifest_history(tuple(history))
        except (TypeError, ValueError):
            raise CohortRegistryUnsafe("cohort registry history is invalid") from None
        ordered = tuple((digest, loaded[digest][1]) for digest in sorted(loaded))
        return loaded, _state_head(ordered)

    def register(self, manifest: CohortManifest) -> CohortRegistrationReceipt:
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
        with self._lock(exclusive=True):
            self._recover_temporary_objects()
            loaded, _ = self._load_state()
            histories: dict[str, list[CohortManifest]] = {}
            for existing, _ in loaded.values():
                histories.setdefault(existing.cohort_id, []).append(existing)
            history = sorted(
                histories.get(captured.cohort_id, []), key=lambda item: item.version
            )
            if digest in loaded:
                if loaded[digest][1] != content:
                    raise CohortRegistryConflict("cohort manifest digest conflicts")
            else:
                if len(loaded) >= MAX_REGISTERED_MANIFESTS:
                    raise CohortRegistryConflict("cohort registry is full")
                try:
                    validate_manifest_history(tuple((*history, captured)))
                    validate_manifest_against_linkage_store(
                        captured,
                        self._linkage_store,
                        expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                    )
                except Exception:
                    raise CohortRegistryConflict(
                        "cohort manifest cannot extend current registry history"
                    ) from None
                assert self._objects_fd is not None
                try:
                    self._publish(self._objects_fd, f"{digest}.json", content)
                except FileExistsError:
                    raise CohortRegistryConflict(
                        "cohort manifest publication conflicts"
                    ) from None
            final, head = self._load_state()
            if digest not in final or final[digest][1] != content:
                raise CohortRegistryUnsafe("cohort manifest publication is unproven")
            return CohortRegistrationReceipt(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                state_version=len(final),
                state_head_sha256=head,
                cohort_id=captured.cohort_id,
                cohort_version=captured.version,
                manifest_sha256=digest,
                previous_manifest_sha256=captured.previous_manifest_sha256,
            )

    def resolve(
        self, selector_id: str, cohort_version: int
    ) -> RegisteredCohortManifest:
        if (
            type(selector_id) is not str
            or len(selector_id) != 56
            or not selector_id.startswith("cohort_selector_")
            or type(cohort_version) is not int
            or not 1 <= cohort_version <= 100_000
        ):
            raise CohortRegistryConflict("cohort selector is invalid")
        with self._lock(exclusive=False):
            loaded, head = self._load_state()
            matches = [
                (digest, manifest)
                for digest, (manifest, _) in loaded.items()
                if manifest.version == cohort_version
                and _selector_id(
                    self._metadata.registry_epoch_sha256, manifest.cohort_id
                )
                == selector_id
            ]
            if len(matches) != 1:
                raise CohortRegistryConflict("cohort selector is unavailable")
            digest, manifest = matches[0]
            try:
                validate_manifest_against_linkage_store(
                    manifest,
                    self._linkage_store,
                    expected_trust_snapshot_sha256_by_provider=self._trust_pins,
                )
            except Exception:
                raise CohortRegistryConflict(
                    "cohort selector authority is stale"
                ) from None
            return RegisteredCohortManifest(
                registry_id=self._metadata.registry_id,
                registry_epoch_sha256=self._metadata.registry_epoch_sha256,
                state_version=len(loaded),
                state_head_sha256=head,
                manifest_sha256=digest,
                manifest=manifest,
            )

    def list_selectors(
        self,
        *,
        after_selector_id: str | None = None,
        after_version: int | None = None,
        limit: int = 50,
    ) -> CohortSelectorPage:
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
        with self._lock(exclusive=False):
            loaded, head = self._load_state()
            ordered = sorted(
                (
                    _selector_id(
                        self._metadata.registry_epoch_sha256, manifest.cohort_id
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
                    validate_manifest_against_linkage_store(
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
                        inclusion_policy_sha256=manifest.policies.inclusion_sha256,
                        exclusion_policy_sha256=manifest.policies.exclusion_sha256,
                        missingness_policy_sha256=manifest.policies.missingness_sha256,
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
                next_after_version=(rows[-1].cohort_version if more and rows else None),
            )


__all__ = [
    "CohortAuthorityState",
    "CohortRegistrationReceipt",
    "CohortRegistry",
    "CohortRegistryConflict",
    "CohortRegistryError",
    "CohortRegistryUnsafe",
    "CohortSelectorPage",
    "CohortSelectorRecord",
    "RegisteredCohortManifest",
]
