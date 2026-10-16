"""Local Lab assets with exact-byte resolution and atomic publication.

PMD bytes are supplied by an operator. This module never downloads them,
redistributes them, or infers rights from the presence of a file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from traceback_runner.serialization import canonical_json_bytes

from .asset_contracts import AssetDescriptor
if TYPE_CHECKING:
    from .store import LabStore

REGISTRATION_SCHEMA = "traceback.lab.asset-registration.v1"
PMD_KIND = "pmd"
PMD_FILENAME = "PMD_coordinates_hg38.bed.gz"
PMD_SHA256 = "e6be0642b7b8dd530247c0c43cbc63165092aa9b9ada632710cedfbdc9b8e3dc"
_BLOCK = 16 << 20


class AssetError(ValueError):
    """An asset is absent, unsafe, altered, or not the pinned version."""


class AssetPublishUncertain(AssetError):
    """Metadata is visible, but durable publication was not confirmed."""


@dataclass(frozen=True)
class AssetRecord:
    descriptor: AssetDescriptor
    file: Path
    legacy: bool

    @property
    def asset_id(self) -> str:
        return self.descriptor.asset_id

    @property
    def sha256(self) -> str:
        return self.descriptor.sha256

    def public(self) -> dict[str, Any]:
        return {
            **self.descriptor.model_dump(by_alias=True),
            "asset_id": self.asset_id,
            "file": str(self.file),
            "legacy": self.legacy,
        }


def _directory(store: LabStore, kind: str) -> Path:
    if kind != PMD_KIND:
        raise AssetError(f"unknown Lab asset {kind!r}")
    return store.lab / "assets" / PMD_KIND


def _check_contained(store: LabStore, path: Path) -> None:
    root = store.root
    target = os.path.abspath(path)
    try:
        contained = os.path.commonpath((os.path.abspath(root), target)) == os.path.abspath(root)
    except ValueError:
        contained = False
    if not contained:
        raise AssetError("asset path is outside the Lab ROOT")
    current = root
    if current.is_symlink():
        raise AssetError("Lab ROOT is a symlink")
    for part in Path(target).relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise AssetError("asset path contains a symlink")


def _regular_source(path: Path) -> Path:
    source = Path(os.path.abspath(path))
    for ancestor in (source, *source.parents):
        if ancestor.is_symlink():
            raise AssetError("asset source path contains a symlink")
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise AssetError("asset source is absent or unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AssetError("asset source must be a regular file")
    return source


def _regular_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise AssetError("asset file is absent or unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AssetError("asset must be a regular file, without symlinks")


def _open_regular(path: Path) -> int:
    """Open without following path-component symlinks where dirfd is available."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return os.open(path, flags)
    absolute = Path(os.path.abspath(path))
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(absolute.name, flags, dir_fd=directory)
    finally:
        os.close(directory)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _digest(path: Path) -> tuple[str, int]:
    """Hash one regular file handle and reject changes during the read.

    The path is checked again after hashing so a replacement cannot make a
    digest of the old inode look like verification of the new pathname.
    """

    digest = hashlib.sha256()
    try:
        descriptor = _open_regular(path)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetError("asset must be a regular file, without symlinks")
            while block := stream.read(_BLOCK):
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = os.lstat(path)
    except OSError as exc:
        raise AssetError("asset file is unreadable or changed while hashing") from exc
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
        raise AssetError("asset file changed while hashing; inspect it again")
    if not stat.S_ISREG(current.st_mode):
        raise AssetError("asset must be a regular file, without symlinks")
    return digest.hexdigest(), after.st_size


def _descriptor(
    size_bytes: int, source_label: str, terms: str, license_id: str | None,
    *, sha256: str | None = None,
) -> AssetDescriptor:
    if (
        source_label != source_label.strip()
        or any(char in source_label for char in ("/", "\\"))
        or any(ord(char) < 32 for char in source_label)
    ):
        raise AssetError("source label must be a short description, not a path")
    try:
        return AssetDescriptor(
            kind=PMD_KIND,
            sha256=sha256 or PMD_SHA256,
            size_bytes=size_bytes,
            origin="user",
            source_label=source_label,
            terms=terms,
            license_id=license_id,
        )
    except ValidationError as exc:
        raise AssetError("invalid asset descriptor: source, terms, or license") from exc


def _manifest(record: AssetRecord) -> dict[str, Any]:
    return {
        "schema": REGISTRATION_SCHEMA,
        "descriptor": record.descriptor.model_dump(by_alias=True),
        "file": PMD_FILENAME,
        "registered_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def inspect_asset(
    store: LabStore, kind: str = PMD_KIND, *, expected_sha256: str | None = None
) -> AssetRecord:
    """Rehash registered bytes; accept the historical exact pinned PMD location."""

    directory = _directory(store, kind)
    _check_contained(store, directory)
    if directory.exists() and not directory.is_dir():
        raise AssetError("asset directory is not a directory; inspect it without replacing it")
    path = directory / PMD_FILENAME
    manifest_path = directory / "manifest.json"
    _check_contained(store, path)
    _check_contained(store, manifest_path)
    _regular_file(path)
    digest, size = _digest(path)
    _check_contained(store, path)
    if digest != (expected_sha256 or PMD_SHA256):
        raise AssetError("PMD file differs from the pinned SHA-256; it was not used")
    if not manifest_path.exists():
        legacy = _descriptor(
            size, "Legacy pinned PMD; source unrecorded", "unknown", None, sha256=digest,
        )
        return AssetRecord(legacy, path, True)
    _regular_file(manifest_path)
    try:
        with os.fdopen(_open_regular(manifest_path), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetError("Lab asset manifest must be a regular file")
            raw = stream.read(65_537)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise AssetError("Lab asset manifest is unreadable") from exc
    _check_contained(store, manifest_path)
    try:
        current = os.lstat(manifest_path)
    except OSError as exc:
        raise AssetError("Lab asset manifest changed while being read; inspect it again") from exc
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(current)
    ):
        raise AssetError("Lab asset manifest changed while being read; inspect it again")
    if len(raw) > 65_536:
        raise AssetError("Lab asset manifest exceeds 64 KiB")
    try:
        manifest = json.loads(raw)
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "descriptor", "file", "registered_utc"}
            or manifest["schema"] != REGISTRATION_SCHEMA
            or manifest["file"] != PMD_FILENAME
        ):
            raise ValueError("unexpected asset manifest fields")
        descriptor = AssetDescriptor.model_validate(manifest["descriptor"])
    except (OSError, ValueError, ValidationError, KeyError, TypeError) as exc:
        raise AssetError("Lab asset manifest is unreadable or invalid") from exc
    if (
        descriptor.kind != kind
        or descriptor.sha256 != digest
        or descriptor.size_bytes != size
        or descriptor.origin != "user"
    ):
        raise AssetError("Lab asset manifest does not match its pinned bytes")
    return AssetRecord(descriptor, path, False)


def _fsync_asset_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def add_asset(
    store: LabStore,
    kind: str,
    source: Path,
    *,
    source_label: str,
    terms: str,
    license_id: str | None = None,
) -> AssetRecord:
    """Copy pinned local bytes under the caller's held Lab writer lock.

    Existing unknown contents are never replaced. A correctly pinned legacy
    PMD can gain metadata without moving or rewriting its bytes.
    """

    directory = _directory(store, kind)
    source = _regular_source(source)
    digest, source_size = _digest(source)
    if digest != PMD_SHA256:
        raise AssetError("source PMD file does not match the pinned SHA-256")
    descriptor = _descriptor(source_size, source_label, terms, license_id)
    parent = directory.parent
    _check_contained(store, parent)
    if directory.exists() or directory.is_symlink():
        existing = inspect_asset(store, kind)
        if not existing.legacy:
            if existing.descriptor != descriptor:
                raise AssetError("asset already registered with different provenance or terms")
            return existing
        descriptor = _descriptor(existing.descriptor.size_bytes, source_label, terms, license_id)
        record = AssetRecord(descriptor, existing.file, False)
        manifest_path = directory / "manifest.json"
        _check_contained(store, manifest_path)
        staging: Path | None = None
        linked = False
        try:
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=directory))
            staged_manifest = staging / "manifest.json"
            with staged_manifest.open("xb") as stream:
                stream.write(canonical_json_bytes(_manifest(record)))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(staged_manifest, manifest_path)
            linked = True
            _fsync_asset_directory(directory)
        except OSError as exc:
            if linked:
                raise AssetPublishUncertain(
                    "PMD metadata is visible but durability is unconfirmed; inspect before retry"
                ) from exc
            raise AssetError("could not publish PMD metadata safely") from exc
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        return record

    def build(staging: Path) -> None:
        target = staging / PMD_FILENAME
        copied = hashlib.sha256()
        try:
            with os.fdopen(_open_regular(source), "rb") as reader, target.open("xb") as writer:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise AssetError("asset source must be a regular file")
                while block := reader.read(_BLOCK):
                    writer.write(block)
                    copied.update(block)
                after = os.fstat(reader.fileno())
            current = os.lstat(source)
        except OSError as exc:
            raise AssetError("source PMD file changed while being copied") from exc
        _regular_source(source)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            raise AssetError("source PMD file changed while being copied")
        if copied.hexdigest() != PMD_SHA256 or target.stat().st_size != descriptor.size_bytes:
            raise AssetError("source PMD file changed while being copied")
        (staging / "manifest.json").write_bytes(
            canonical_json_bytes(_manifest(AssetRecord(descriptor, target, False)))
        )

    store.publish_directory(parent, kind, build)
    return inspect_asset(store, kind)
