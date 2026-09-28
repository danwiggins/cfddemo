"""Content-addressed, offline registry for synthetic release assets.

The package format is intentionally not an archive.  It contains one bounded,
canonical JSON header followed by one exact, uncompressed payload.  This keeps
paths, links, special files, file-count expansion, and decompression outside the
accepted input language.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import struct
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import Field

from .contracts import RunnerContract
from .qualification import (
    AssetAuthorizationDecision,
    AssetLifecycleStatus,
    AuthorityStatus,
    QualificationBinding,
    QualificationTrustPolicy,
    ReleaseAuthorityHead,
    ReleaseEvidenceEnvelope,
    verify_release_asset_authorization,
)
from .release_evidence import AssetReference, DigestDomain, domain_digest
from .serialization import canonical_json_bytes, canonical_model_from_bytes
from .signing import TrustStore

PACKAGE_MAGIC = b"TRACEBACK-ASSET\x00V1\n"
_LENGTH = struct.Struct(">I")
_MAX_HEADER_BYTES = 256 * 1024
_MAX_PAYLOAD_BYTES = 2 * 1024**4
_COPY_CHUNK_BYTES = 1024 * 1024
_REGISTRATION_RESERVE_BYTES = 64 * 1024


class AssetError(ValueError):
    """Base class for expected asset failures."""


class AssetPackageError(AssetError):
    """The supplied package is malformed, unsafe, or inconsistent."""


class AssetAuthorityError(AssetError):
    """Independent release authority did not authorize this asset."""


class AssetCapacityError(AssetError):
    """Installation would violate the configured free-space floor."""


class AssetConflictError(AssetError):
    """The requested identifier/version is already bound differently."""


class AssetIntegrityError(AssetError):
    """Installed or staged bytes do not match their content identity."""


class AssetFilesystemError(AssetError):
    """A filesystem object has an unsafe or unsupported shape."""


class AssetPackageHeader(RunnerContract):
    schema_version: Literal["traceback.synthetic-asset-package.v1"] = (
        "traceback.synthetic-asset-package.v1"
    )
    encoding: Literal["identity"] = "identity"
    reference: AssetReference
    asset_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InstalledAssetRecord(RunnerContract):
    schema_version: Literal["traceback.installed-asset.v1"] = (
        "traceback.installed-asset.v1"
    )
    reference: AssetReference
    asset_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size_bytes: int = Field(gt=0)


class IntegrityStatus(StrEnum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


class CapacityEstimate(RunnerContract):
    payload_size_bytes: int = Field(gt=0)
    registration_reserve_bytes: int = Field(ge=0)
    peak_required_bytes: int = Field(ge=0)
    filesystem_total_bytes: int = Field(gt=0)
    filesystem_available_bytes: int = Field(ge=0)
    minimum_free_bytes: int = Field(ge=0)
    predicted_free_bytes: int = Field(ge=0)
    admitted: bool


class InstalledAsset(RunnerContract):
    asset_id: str
    version: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_size_bytes: int = Field(gt=0)
    asset_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    newly_registered: bool
    execution_authorized: Literal[False] = False
    qualification_established: Literal[False] = False


class AssetVerification(RunnerContract):
    asset_id: str
    version: str
    installed: bool
    integrity_status: IntegrityStatus
    authority_status: str
    lifecycle_status: str
    authority_failure: str
    registered_reference_sha256: str | None = None
    current_reference_sha256: str
    content_sha256: str | None = None
    content_size_bytes: int | None = None
    registration_matches_current_reference: bool = False
    execution_authorized: Literal[False] = False
    qualification_established: Literal[False] = False


@dataclass(frozen=True)
class ReleaseAuthorization:
    """External trust inputs required for every install and verification."""

    envelope: ReleaseEvidenceEnvelope
    trust_store: TrustStore
    role_policy: QualificationTrustPolicy
    authority_head: ReleaseAuthorityHead | None
    expected_binding: QualificationBinding
    expected_package_sha256: str
    now: datetime | Callable[[], datetime]


@dataclass(frozen=True)
class _ParsedPackage:
    path: Path
    header: AssetPackageHeader
    payload_offset: int
    package_size: int


def _reference_digest(reference: AssetReference) -> str:
    return domain_digest(DigestDomain.ASSET_REFERENCE, reference)


def _safe_regular_reader(path: Path) -> BinaryIO:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AssetFilesystemError("asset input cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AssetFilesystemError("asset input must be a regular file")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _read_exact(stream: BinaryIO, length: int, label: str) -> bytes:
    value = stream.read(length)
    if len(value) != length:
        raise AssetPackageError(f"asset package is truncated in {label}")
    return value


def _parse_package(path: str | Path) -> _ParsedPackage:
    package_path = Path(path)
    with _safe_regular_reader(package_path) as stream:
        metadata = os.fstat(stream.fileno())
        magic = _read_exact(stream, len(PACKAGE_MAGIC), "magic")
        if magic != PACKAGE_MAGIC:
            raise AssetPackageError("asset package magic/version is invalid")
        header_size = _LENGTH.unpack(
            _read_exact(stream, _LENGTH.size, "header length")
        )[0]
        if header_size == 0 or header_size > _MAX_HEADER_BYTES:
            raise AssetPackageError("asset package header exceeds its bounded limit")
        header_bytes = _read_exact(stream, header_size, "header")
        try:
            header = canonical_model_from_bytes(AssetPackageHeader, header_bytes)
        except Exception as exc:
            raise AssetPackageError(
                "asset package header is not canonical or valid"
            ) from exc
        if _reference_digest(header.reference) != header.asset_reference_sha256:
            raise AssetPackageError(
                "asset reference digest does not match package header"
            )
        payload_size = header.reference.content.content_size_bytes
        if payload_size > _MAX_PAYLOAD_BYTES:
            raise AssetPackageError("asset payload exceeds the bounded package limit")
        payload_offset = len(PACKAGE_MAGIC) + _LENGTH.size + header_size
        expected_size = payload_offset + payload_size
        if metadata.st_size != expected_size:
            raise AssetPackageError(
                "asset package has truncated or trailing payload bytes"
            )
    return _ParsedPackage(package_path, header, payload_offset, expected_size)


def build_synthetic_asset_package(
    destination: str | Path,
    *,
    reference: AssetReference,
    payload: bytes,
) -> Path:
    """Build a tiny development fixture package; never downloads asset bytes."""

    identity = reference.content
    if len(payload) != identity.content_size_bytes:
        raise AssetPackageError("payload size does not match the asset reference")
    if hashlib.sha256(payload).hexdigest() != identity.content_sha256:
        raise AssetPackageError("payload digest does not match the asset reference")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise AssetPackageError("asset payload exceeds the bounded package limit")
    header = AssetPackageHeader(
        reference=reference,
        asset_reference_sha256=_reference_digest(reference),
    )
    header_bytes = canonical_json_bytes(header)
    if len(header_bytes) > _MAX_HEADER_BYTES:
        raise AssetPackageError("asset package header exceeds its bounded limit")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(PACKAGE_MAGIC)
            stream.write(_LENGTH.pack(len(header_bytes)))
            stream.write(header_bytes)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(target.parent)
    except Exception:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


class AssetRegistry:
    """POSIX offline registry whose commit point is atomic reference publication."""

    def __init__(
        self,
        root: str | Path,
        *,
        min_free_fraction: float = 0.20,
        capacity_provider: Callable[[Path], tuple[int, int]] | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        try:
            minimum_free_ratio = Fraction(str(min_free_fraction))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("min_free_fraction must be a finite ratio") from exc
        if not Fraction(1, 5) <= minimum_free_ratio < 1:
            raise ValueError("min_free_fraction must retain at least the 20% floor")
        self.root = Path(root)
        self.min_free_fraction = min_free_fraction
        self._minimum_free_ratio = minimum_free_ratio
        self._capacity_provider = capacity_provider or _filesystem_capacity
        self._fault_injector = fault_injector
        self.objects = self.root / "objects" / "sha256"
        self.references = self.root / "references"
        self.staging = self.root / ".staging"
        self._initialize()

    @classmethod
    def open_existing(cls, root: str | Path) -> AssetRegistry:
        """Open an initialized registry for read-only verification."""

        registry = cls.__new__(cls)
        registry.root = Path(root)
        registry.min_free_fraction = 0.20
        registry._minimum_free_ratio = Fraction(1, 5)
        registry._capacity_provider = _filesystem_capacity
        registry._fault_injector = None
        registry.objects = registry.root / "objects" / "sha256"
        registry.references = registry.root / "references"
        registry.staging = registry.root / ".staging"
        registry._assert_registry_layout()
        return registry

    def _initialize(self) -> None:
        if self.root.is_symlink():
            raise AssetFilesystemError("registry root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (
            self.root / "objects",
            self.objects,
            self.references,
            self.staging,
        ):
            _ensure_private_directory(directory)
        lock = self.root / ".install.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock, flags, 0o600)
        os.close(descriptor)

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def plan_install(self, package_path: str | Path) -> CapacityEstimate:
        self._assert_registry_layout()
        parsed = _parse_package(package_path)
        digest = parsed.header.reference.content.content_sha256
        object_path = self._object_path(digest)
        if object_path.parent.is_symlink():
            raise AssetFilesystemError("asset object parent cannot be a symlink")
        object_present = object_path.exists() and not object_path.is_symlink()
        required = _REGISTRATION_RESERVE_BYTES
        if not object_present:
            required += parsed.header.reference.content.content_size_bytes
        return self._capacity(
            required, parsed.header.reference.content.content_size_bytes
        )

    def install(
        self,
        package_path: str | Path,
        *,
        asset_id: str,
        version: str,
        authorization: ReleaseAuthorization,
    ) -> InstalledAsset:
        self._assert_registry_layout()
        parsed = _parse_package(package_path)
        identity = parsed.header.reference.content
        if (identity.asset_id, identity.version) != (asset_id, version):
            raise AssetPackageError(
                "asset package identifier/version does not match selection"
            )
        decision = self._authorize(
            authorization,
            asset_id=asset_id,
            version=version,
            reference_sha256=parsed.header.asset_reference_sha256,
        )
        if (
            decision.authority_status != AuthorityStatus.VERIFIED
            or decision.lifecycle_status != AssetLifecycleStatus.ACTIVE
            or decision.authorized_reference != parsed.header.reference
        ):
            raise AssetAuthorityError(
                f"asset authority blocked install: {decision.authority_status.value}/"
                f"{decision.lifecycle_status.value}/{decision.failure.value}"
            )

        record = InstalledAssetRecord(
            reference=parsed.header.reference,
            asset_reference_sha256=parsed.header.asset_reference_sha256,
            object_sha256=identity.content_sha256,
            object_size_bytes=identity.content_size_bytes,
        )
        with self._install_guard():
            decision = self._authorize(
                authorization,
                asset_id=asset_id,
                version=version,
                reference_sha256=parsed.header.asset_reference_sha256,
            )
            self._require_active_authority(decision, parsed.header.reference)
            existing = self._read_record(asset_id, version, missing_ok=True)
            if existing is not None:
                if existing != record:
                    raise AssetConflictError(
                        "asset identifier/version is already registered differently"
                    )
                self._verify_object(existing)
                return self._installed(existing, newly_registered=False)

            object_path = self._object_path(identity.content_sha256)
            object_present = object_path.exists() or object_path.is_symlink()
            required = _REGISTRATION_RESERVE_BYTES + (
                0 if object_present else identity.content_size_bytes
            )
            estimate = self._capacity(required, identity.content_size_bytes)
            if not estimate.admitted:
                raise AssetCapacityError(
                    "install would violate the minimum free-space floor"
                )

            stage = Path(tempfile.mkdtemp(prefix="install-", dir=self.staging))
            stage.chmod(0o700)
            staged_object = stage / "payload"
            staged_record = stage / "registration.json"
            try:
                if object_present:
                    self._verify_path(
                        object_path,
                        identity.content_sha256,
                        identity.content_size_bytes,
                    )
                else:
                    self._stage_payload(parsed, staged_object)
                    self._fault("after_staging_fsync")
                    if not self._capacity(
                        _REGISTRATION_RESERVE_BYTES, identity.content_size_bytes
                    ).admitted:
                        raise AssetCapacityError(
                            "capacity changed during staging and now violates the free-space floor"
                        )
                    decision = self._authorize(
                        authorization,
                        asset_id=asset_id,
                        version=version,
                        reference_sha256=parsed.header.asset_reference_sha256,
                    )
                    self._require_active_authority(decision, parsed.header.reference)
                    _ensure_private_directory(object_path.parent)
                    self._fault("before_object_publish")
                    os.replace(staged_object, object_path)
                    object_path.chmod(0o444)
                    _fsync_directory(object_path.parent)
                    self._fault("after_object_publish")

                record_bytes = canonical_json_bytes(record)
                self._write_staged(staged_record, record_bytes)
                reference_path = self._reference_path(asset_id, version)
                _ensure_private_directory(reference_path.parent)
                if (
                    object_present
                    and not self._capacity(
                        _REGISTRATION_RESERVE_BYTES, identity.content_size_bytes
                    ).admitted
                ):
                    raise AssetCapacityError(
                        "capacity changed before registration and violates the free-space floor"
                    )
                decision = self._authorize(
                    authorization,
                    asset_id=asset_id,
                    version=version,
                    reference_sha256=parsed.header.asset_reference_sha256,
                )
                self._require_active_authority(decision, parsed.header.reference)
                self._fault("before_registration_publish")
                os.replace(staged_record, reference_path)
                reference_path.chmod(0o444)
                _fsync_directory(reference_path.parent)
                _fsync_directory(self.references)
                _fsync_directory(self.root)
                self._fault("after_registration_publish")
            finally:
                _remove_private_stage(stage)
            return self._installed(record, newly_registered=True)

    def verify(
        self,
        *,
        asset_id: str,
        version: str,
        authorization: ReleaseAuthorization,
    ) -> AssetVerification:
        self._assert_registry_layout()
        current = _find_reference(authorization.envelope, asset_id, version)
        current_digest = _reference_digest(current)
        decision = self._authorize(
            authorization,
            asset_id=asset_id,
            version=version,
            reference_sha256=current_digest,
        )
        record = self._read_record(asset_id, version, missing_ok=True)
        if record is None:
            return self._verification(
                decision,
                current_digest=current_digest,
                integrity=IntegrityStatus.ABSENT,
                record=None,
            )
        try:
            self._verify_object(record)
        except (AssetIntegrityError, AssetFilesystemError):
            integrity = IntegrityStatus.INVALID
        else:
            integrity = IntegrityStatus.VALID
        return self._verification(
            decision,
            current_digest=current_digest,
            integrity=integrity,
            record=record,
        )

    def _verification(
        self,
        decision: AssetAuthorizationDecision,
        *,
        current_digest: str,
        integrity: IntegrityStatus,
        record: InstalledAssetRecord | None,
    ) -> AssetVerification:
        return AssetVerification(
            asset_id=decision.asset_id,
            version=decision.asset_version,
            installed=record is not None,
            integrity_status=integrity,
            authority_status=decision.authority_status.value,
            lifecycle_status=decision.lifecycle_status.value,
            authority_failure=decision.failure.value,
            registered_reference_sha256=(
                record.asset_reference_sha256 if record else None
            ),
            current_reference_sha256=current_digest,
            content_sha256=(record.object_sha256 if record else None),
            content_size_bytes=(record.object_size_bytes if record else None),
            registration_matches_current_reference=(
                record is not None and record.asset_reference_sha256 == current_digest
            ),
        )

    def _authorize(
        self,
        authorization: ReleaseAuthorization,
        *,
        asset_id: str,
        version: str,
        reference_sha256: str,
    ) -> AssetAuthorizationDecision:
        now = authorization.now() if callable(authorization.now) else authorization.now
        return verify_release_asset_authorization(
            authorization.envelope,
            authorization.trust_store,
            authorization.role_policy,
            authorization.authority_head,
            expected_binding=authorization.expected_binding,
            expected_package_sha256=authorization.expected_package_sha256,
            expected_asset_id=asset_id,
            expected_asset_version=version,
            expected_asset_reference_sha256=reference_sha256,
            now=now,
        )

    @staticmethod
    def _require_active_authority(
        decision: AssetAuthorizationDecision, reference: AssetReference
    ) -> None:
        if (
            decision.authority_status != AuthorityStatus.VERIFIED
            or decision.lifecycle_status != AssetLifecycleStatus.ACTIVE
            or decision.authorized_reference != reference
        ):
            raise AssetAuthorityError(
                f"asset authority blocked install: {decision.authority_status.value}/"
                f"{decision.lifecycle_status.value}/{decision.failure.value}"
            )

    def _capacity(self, required: int, payload_size: int) -> CapacityEstimate:
        total, available = self._capacity_provider(self.root)
        if total <= 0 or available < 0 or available > total:
            raise AssetFilesystemError("filesystem capacity values are invalid")
        minimum = (
            total * self._minimum_free_ratio.numerator
            + self._minimum_free_ratio.denominator
            - 1
        ) // self._minimum_free_ratio.denominator
        predicted = max(0, available - required)
        return CapacityEstimate(
            payload_size_bytes=payload_size,
            registration_reserve_bytes=_REGISTRATION_RESERVE_BYTES,
            peak_required_bytes=required,
            filesystem_total_bytes=total,
            filesystem_available_bytes=available,
            minimum_free_bytes=minimum,
            predicted_free_bytes=predicted,
            admitted=available >= required and predicted >= minimum,
        )

    def _stage_payload(self, parsed: _ParsedPackage, destination: Path) -> None:
        digest = hashlib.sha256()
        copied = 0
        with _safe_regular_reader(parsed.path) as source:
            source.seek(parsed.payload_offset)
            with destination.open("xb") as output:
                while copied < parsed.header.reference.content.content_size_bytes:
                    chunk = source.read(
                        min(
                            _COPY_CHUNK_BYTES,
                            parsed.header.reference.content.content_size_bytes - copied,
                        )
                    )
                    if not chunk:
                        raise AssetPackageError(
                            "asset payload became truncated while staging"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                if source.read(1):
                    raise AssetPackageError("asset payload grew while staging")
                output.flush()
                os.fsync(output.fileno())
        identity = parsed.header.reference.content
        if (
            copied != identity.content_size_bytes
            or digest.hexdigest() != identity.content_sha256
        ):
            raise AssetIntegrityError(
                "staged payload does not match its content identity"
            )
        _fsync_directory(destination.parent)

    @staticmethod
    def _write_staged(path: Path, content: bytes) -> None:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(path.parent)

    def _verify_object(self, record: InstalledAssetRecord) -> None:
        self._verify_path(
            self._object_path(record.object_sha256),
            record.object_sha256,
            record.object_size_bytes,
        )

    @staticmethod
    def _verify_path(path: Path, expected_digest: str, expected_size: int) -> None:
        if path.parent.is_symlink():
            raise AssetFilesystemError("asset object parent cannot be a symlink")
        digest = hashlib.sha256()
        size = 0
        with _safe_regular_reader(path) as stream:
            while True:
                chunk = stream.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size:
                    raise AssetIntegrityError("asset object exceeds registered size")
                digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_digest:
            raise AssetIntegrityError(
                "asset object does not match registered content identity"
            )

    def _read_record(
        self, asset_id: str, version: str, *, missing_ok: bool
    ) -> InstalledAssetRecord | None:
        path = self._reference_path(asset_id, version)
        if path.parent.is_symlink():
            raise AssetFilesystemError("asset reference parent cannot be a symlink")
        if not path.exists() and not path.is_symlink():
            if missing_ok:
                return None
            raise AssetIntegrityError("asset registration is absent")
        with _safe_regular_reader(path) as stream:
            content = stream.read(_MAX_HEADER_BYTES + 1)
        if len(content) > _MAX_HEADER_BYTES:
            raise AssetIntegrityError("asset registration exceeds its bounded limit")
        try:
            record = canonical_model_from_bytes(InstalledAssetRecord, content)
        except Exception as exc:
            raise AssetIntegrityError(
                "asset registration is not canonical or valid"
            ) from exc
        identity = record.reference.content
        if (identity.asset_id, identity.version) != (asset_id, version):
            raise AssetIntegrityError(
                "asset registration identifier/version is inconsistent"
            )
        if (
            record.asset_reference_sha256 != _reference_digest(record.reference)
            or record.object_sha256 != identity.content_sha256
            or record.object_size_bytes != identity.content_size_bytes
        ):
            raise AssetIntegrityError("asset registration identity is inconsistent")
        return record

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def _reference_path(self, asset_id: str, version: str) -> Path:
        # The frozen Identifier grammar excludes separators and traversal.
        return self.references / asset_id / f"{version}.json"

    def _assert_registry_layout(self) -> None:
        for directory in (
            self.root,
            self.root / "objects",
            self.objects,
            self.references,
            self.staging,
        ):
            if directory.is_symlink() or not directory.is_dir():
                raise AssetFilesystemError(
                    "registry managed directories must remain real directories"
                )

    @contextmanager
    def _install_guard(self) -> Iterator[None]:
        self._assert_registry_layout()
        path = self.root / ".install.lock"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _installed(
        record: InstalledAssetRecord, *, newly_registered: bool
    ) -> InstalledAsset:
        identity = record.reference.content
        return InstalledAsset(
            asset_id=identity.asset_id,
            version=identity.version,
            content_sha256=identity.content_sha256,
            content_size_bytes=identity.content_size_bytes,
            asset_reference_sha256=record.asset_reference_sha256,
            newly_registered=newly_registered,
        )


def _find_reference(
    envelope: ReleaseEvidenceEnvelope, asset_id: str, version: str
) -> AssetReference:
    reference = next(
        (
            item
            for item in envelope.package.assets
            if (item.content.asset_id, item.content.version) == (asset_id, version)
        ),
        None,
    )
    if reference is None:
        raise AssetAuthorityError("selected asset is absent from release evidence")
    return reference


def _filesystem_capacity(path: Path) -> tuple[int, int]:
    values = os.statvfs(path)
    unit = values.f_frsize
    return values.f_blocks * unit, values.f_bavail * unit


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise AssetFilesystemError("registry directories cannot be symlinks")
    existed = path.exists()
    path.mkdir(exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise AssetFilesystemError("registry path is not a real directory")
    if not existed:
        _fsync_directory(path)
        _fsync_directory(path.parent)


def _remove_private_stage(stage: Path) -> None:
    """Remove only the known private files created for one installation."""

    for name in ("payload", "registration.json"):
        try:
            (stage / name).unlink()
        except FileNotFoundError:
            pass
    try:
        stage.rmdir()
    except FileNotFoundError:
        pass


__all__ = [
    "AssetAuthorityError",
    "AssetCapacityError",
    "AssetConflictError",
    "AssetError",
    "AssetFilesystemError",
    "AssetIntegrityError",
    "AssetPackageError",
    "AssetRegistry",
    "AssetVerification",
    "CapacityEstimate",
    "InstalledAsset",
    "IntegrityStatus",
    "ReleaseAuthorization",
    "build_synthetic_asset_package",
]
