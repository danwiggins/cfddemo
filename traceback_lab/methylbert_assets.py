"""API write-once, Lab-owned registration of operator-trusted bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from traceback_runner.serialization import canonical_json_bytes

from .asset_contracts import AssetDescriptor
from .assets import AssetError, AssetPublishUncertain, AssetRecord, _check_contained, _file_identity, _open_regular, _regular_source
from .methylbert_bundle import MAX_ARCHIVE_BYTES, BundleError, verify_bundle

if TYPE_CHECKING:
    from .store import LabStore

KIND = "methylbert-bundle"
FILENAME = "model.tar"
MANIFEST = "manifest.json"
SCHEMA = "traceback.lab.methylbert-registration.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK = 16 << 20


class MethylbertPublishUncertain(AssetPublishUncertain):
    def __init__(self, sha256: str):
        self.sha256 = sha256
        super().__init__("MethylBERT bundle is visible but durability is unconfirmed; inspect before retry")


def _hash_archive(path: Path) -> tuple[str, int]:
    """Hash one stable regular handle without reading beyond the archive cap."""

    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(_open_regular(path), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_ARCHIVE_BYTES:
                raise AssetError("MethylBERT archive exceeds its size cap")
            while block := stream.read(_BLOCK):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise AssetError("MethylBERT archive exceeds its size cap")
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = os.lstat(path)
    except OSError as exc:
        raise AssetError("MethylBERT archive is absent or unreadable") from exc
    if (size != before.st_size or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(current)):
        raise AssetError("MethylBERT archive changed while hashing")
    return digest.hexdigest(), size


def _directory(store: LabStore, sha256: str) -> Path:
    if not _SHA256.fullmatch(sha256):
        raise AssetError("MethylBERT bundle SHA-256 is invalid")
    directory = store.lab / "assets" / KIND / sha256
    _check_contained(store, directory)
    return directory


def _descriptor(
    sha256: str, size: int, source_label: str, terms: str, license_id: str | None,
) -> AssetDescriptor:
    if (source_label != source_label.strip() or any(c in source_label for c in ("/", "\\"))
            or any(ord(c) < 32 for c in source_label)):
        raise AssetError("source label must describe provenance, not a path")
    try:
        return AssetDescriptor(
            kind=KIND, sha256=sha256, size_bytes=size, origin="user",
            source_label=source_label, terms=terms, license_id=license_id,
        )
    except ValidationError as exc:
        raise AssetError("invalid MethylBERT source or terms metadata") from exc


def inspect_methylbert_bundle(store: LabStore, sha256: str) -> AssetRecord:
    """Rehash both the registration and exact bundle bytes before use."""

    directory = _directory(store, sha256)
    archive = directory / FILENAME
    manifest_path = directory / MANIFEST
    for path in (archive, manifest_path):
        _check_contained(store, path)
    if directory.is_symlink() or not directory.is_dir():
        raise AssetError("MethylBERT bundle registration is missing or linked")
    try:
        with os.fdopen(_open_regular(manifest_path), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 65536:
                raise AssetError("MethylBERT registration manifest is invalid")
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
        _check_contained(store, manifest_path)
        current = os.lstat(manifest_path)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            raise AssetError("MethylBERT registration manifest changed while being read")
        if len(raw) > 65536:
            raise AssetError("MethylBERT registration manifest is invalid")
        metadata = json.loads(raw)
        if (not isinstance(metadata, dict) or set(metadata) != {"schema", "descriptor", "file"}
                or metadata["schema"] != SCHEMA or metadata["file"] != FILENAME):
            raise AssetError("MethylBERT registration manifest is invalid")
        descriptor = AssetDescriptor.model_validate(metadata["descriptor"])
    except (OSError, ValueError, TypeError, ValidationError, UnicodeError) as exc:
        raise AssetError("MethylBERT registration manifest is invalid") from exc
    if (descriptor.kind != KIND or descriptor.origin != "user" or descriptor.sha256 != sha256
            or not 1 <= descriptor.size_bytes <= MAX_ARCHIVE_BYTES):
        raise AssetError("MethylBERT registration identity is invalid")
    digest, size = _hash_archive(archive)
    if digest != sha256 or size != descriptor.size_bytes:
        raise AssetError("MethylBERT bundle differs from registration")
    try:
        verify_bundle(archive)
    except BundleError as exc:
        raise AssetError("registered MethylBERT bundle is malformed") from exc
    return AssetRecord(descriptor, archive, False)


def register_methylbert_bundle(
    store: LabStore, source: Path, *, source_label: str, terms: str,
    license_id: str | None = None,
) -> AssetRecord:
    """Validate and atomically publish a private copy under its SHA-256."""

    source = _regular_source(source)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise AssetError("MethylBERT source is absent or unreadable") from exc
    if not 1 <= size <= MAX_ARCHIVE_BYTES:
        raise AssetError("MethylBERT archive exceeds its size cap")
    try:
        expected_bundle = verify_bundle(source)
    except BundleError as exc:
        raise AssetError("MethylBERT bundle is malformed") from exc
    sha256, size = _hash_archive(source)
    descriptor = _descriptor(sha256, size, source_label, terms, license_id)
    directory = _directory(store, sha256)
    if directory.exists() or directory.is_symlink():
        existing = inspect_methylbert_bundle(store, sha256)
        if existing.descriptor != descriptor:
            raise AssetError("MethylBERT bundle already registered with different provenance or terms")
        return existing

    def build(staging: Path) -> None:
        staging.chmod(0o700)
        target = staging / FILENAME
        digest = hashlib.sha256()
        copied = 0
        try:
            with os.fdopen(_open_regular(source), "rb") as reader:
                before = os.fstat(reader.fileno())
                descriptor_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(descriptor_fd, "wb") as writer:
                    while block := reader.read(_BLOCK):
                        copied += len(block)
                        if copied > MAX_ARCHIVE_BYTES:
                            raise AssetError("MethylBERT archive exceeds its size cap")
                        digest.update(block)
                        writer.write(block)
                after = os.fstat(reader.fileno())
        except OSError as exc:
            raise AssetError("MethylBERT source changed during private copy") from exc
        if (copied != size or digest.hexdigest() != sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                   (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise AssetError("MethylBERT source changed during private copy")
        try:
            if verify_bundle(target) != expected_bundle:
                raise AssetError("MethylBERT bundle changed during private copy")
        except BundleError as exc:
            raise AssetError("MethylBERT bundle changed during private copy") from exc
        (staging / MANIFEST).write_bytes(canonical_json_bytes({
            "schema": SCHEMA, "descriptor": descriptor.model_dump(by_alias=True), "file": FILENAME,
        }))

    try:
        store.publish_directory(directory.parent, sha256, build)
    except FileExistsError as exc:
        raise AssetError("MethylBERT bundle was registered concurrently; inspect before retry") from exc
    except OSError as exc:
        if directory.is_dir():
            raise MethylbertPublishUncertain(sha256) from exc
        raise AssetError("MethylBERT bundle could not be published") from exc
    return inspect_methylbert_bundle(store, sha256)


def copy_methylbert_bundle(store: LabStore, sha256: str, destination: Path) -> Path:
    """Copy registered bytes to a new private file, checking the pinned digest."""

    record = inspect_methylbert_bundle(store, sha256)
    if destination.exists() or destination.is_symlink():
        raise AssetError("MethylBERT private destination must not exist")
    digest = hashlib.sha256()
    copied = 0
    created = False
    complete = False
    try:
        with os.fdopen(_open_regular(record.file), "rb") as reader:
            descriptor_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            created = True
            with os.fdopen(descriptor_fd, "wb") as writer:
                while block := reader.read(_BLOCK):
                    copied += len(block)
                    if copied > MAX_ARCHIVE_BYTES:
                        raise AssetError("MethylBERT registered bundle grew during copy")
                    digest.update(block)
                    writer.write(block)
        if copied != record.descriptor.size_bytes or digest.hexdigest() != sha256:
            raise AssetError("MethylBERT registered bundle changed during copy")
        try:
            verify_bundle(destination)
        except BundleError as exc:
            raise AssetError("MethylBERT private bundle is malformed") from exc
        complete = True
        return destination
    except OSError as exc:
        raise AssetError("MethylBERT registered bundle could not be copied") from exc
    finally:
        if created and not complete:
            destination.unlink(missing_ok=True)


__all__ = ["KIND", "copy_methylbert_bundle", "inspect_methylbert_bundle", "register_methylbert_bundle"]
