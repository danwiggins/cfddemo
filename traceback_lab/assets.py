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
from typing import Any

from pydantic import ValidationError
from traceback_runner.serialization import canonical_json_bytes

from .asset_contracts import AssetDescriptor
from .store import LabStore

REGISTRATION_SCHEMA = "traceback.lab.asset-registration.v1"
PMD_KIND = "pmd"
PMD_FILENAME = "PMD_coordinates_hg38.bed.gz"
PMD_SHA256 = "e6be0642b7b8dd530247c0c43cbc63165092aa9b9ada632710cedfbdc9b8e3dc"
_BLOCK = 16 << 20


class AssetError(ValueError):
    """An asset is absent, unsafe, altered, or not the pinned version."""


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


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(_BLOCK):
                digest.update(block)
    except OSError as exc:
        raise AssetError("asset file is unreadable or changed while hashing") from exc
    return digest.hexdigest()


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
    path = directory / PMD_FILENAME
    manifest_path = directory / "manifest.json"
    _check_contained(store, path)
    _check_contained(store, manifest_path)
    _regular_file(path)
    digest = _digest(path)
    if digest != (expected_sha256 or PMD_SHA256):
        raise AssetError("PMD file differs from the pinned SHA-256; it was not used")
    size = path.stat().st_size
    if not manifest_path.exists():
        legacy = _descriptor(
            size, "Legacy pinned PMD; source unrecorded", "unknown", None, sha256=digest,
        )
        return AssetRecord(legacy, path, True)
    _regular_file(manifest_path)
    try:
        with manifest_path.open("rb") as stream:
            raw = stream.read(65_537)
    except OSError as exc:
        raise AssetError("Lab asset manifest is unreadable") from exc
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
    digest = _digest(source)
    if digest != PMD_SHA256:
        raise AssetError("source PMD file does not match the pinned SHA-256")
    descriptor = _descriptor(source.stat().st_size, source_label, terms, license_id)
    parent = directory.parent
    _check_contained(store, parent)
    if directory.exists() or directory.is_symlink():
        existing = inspect_asset(store, kind)
        if not existing.legacy:
            if existing.descriptor != descriptor:
                raise AssetError("asset already registered with different provenance or terms")
            return existing
        record = AssetRecord(descriptor, existing.file, False)
        manifest_path = directory / "manifest.json"
        _check_contained(store, manifest_path)
        staging: Path | None = None
        try:
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=directory))
            staged_manifest = staging / "manifest.json"
            with staged_manifest.open("xb") as stream:
                stream.write(canonical_json_bytes(_manifest(record)))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(staged_manifest, manifest_path)
            parent_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise AssetError("could not publish PMD metadata safely") from exc
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        return record

    def build(staging: Path) -> None:
        target = staging / PMD_FILENAME
        copied = hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            while block := reader.read(_BLOCK):
                writer.write(block)
                copied.update(block)
        if copied.hexdigest() != PMD_SHA256:
            raise AssetError("source PMD file changed while being copied")
        (staging / "manifest.json").write_bytes(
            canonical_json_bytes(_manifest(AssetRecord(descriptor, target, False)))
        )

    store.publish_directory(parent, kind, build)
    return inspect_asset(store, kind)
