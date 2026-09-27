"""Runner-owned immutable input snapshots.

Snapshots are intentionally local-only.  Their manifests contain relative
locators and ordinary SHA-256 digests and must never be exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .serialization import canonical_json_bytes


class SnapshotError(RuntimeError):
    """Base class for snapshot failures."""


class SnapshotViolation(SnapshotError):
    """The requested source is unsafe or changed while being captured."""


@dataclass(frozen=True)
class SnapshotFile:
    relative_path: str
    size_bytes: int
    sha256_local: str


@dataclass(frozen=True)
class InputSnapshot:
    snapshot_id: str
    path: Path
    files: tuple[SnapshotFile, ...]
    content_sha256: str
    manifest_sha256: str

    def summary(self) -> dict[str, object]:
        """Return a scoped view that does not reveal a host path."""

        return {
            "snapshot_id": self.snapshot_id,
            "content_sha256": self.content_sha256,
            "manifest_sha256": self.manifest_sha256,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "sha256_local": item.sha256_local,
                }
                for item in self.files
            ],
        }


def _safe_relative(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        raise SnapshotViolation(f"unsafe snapshot locator: {value!r}")
    return candidate


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str, os.stat_result]:
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotViolation(f"snapshot source is not a regular file: {source.name}")
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return size, digest.hexdigest(), before


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _seal_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def capture_snapshot(
    source_root: Path,
    relative_files: Iterable[str],
    snapshot_root: Path,
    *,
    snapshot_id: str | None = None,
    after_copy: Callable[[Path], None] | None = None,
) -> InputSnapshot:
    """Copy stable regular files into an atomically sealed private snapshot."""

    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise SnapshotViolation("snapshot source root is not a directory")
    names = tuple(sorted({_safe_relative(item).as_posix() for item in relative_files}))
    if not names:
        raise SnapshotViolation("snapshot must contain at least one file")

    identifier = snapshot_id or uuid.uuid4().hex
    destination = snapshot_root / identifier
    if destination.exists():
        raise SnapshotViolation(f"snapshot already exists: {identifier}")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    temporary = snapshot_root / f".{identifier}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    captured: list[SnapshotFile] = []
    try:
        for name in names:
            source = root.joinpath(*PurePosixPath(name).parts)
            if source.is_symlink():
                raise SnapshotViolation(f"symlinks are not accepted: {name}")
            try:
                resolved = source.resolve(strict=True)
            except FileNotFoundError as exc:
                raise SnapshotViolation(f"snapshot source is missing: {name}") from exc
            if not resolved.is_relative_to(root):
                raise SnapshotViolation(f"snapshot source escapes configured root: {name}")
            target = temporary / name
            size, digest, before = _copy_and_hash(source, target)
            if after_copy is not None:
                after_copy(source)
            after = source.stat(follow_symlinks=False)
            if not _same_file(before, after) or size != after.st_size:
                raise SnapshotViolation(f"snapshot source changed during capture: {name}")
            captured.append(SnapshotFile(name, size, digest))

        content_bytes = canonical_json_bytes(
            {
                "schema_version": "traceback.input-tree.v1",
                "files": [item.__dict__ for item in captured],
            }
        )
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        manifest = {
            "schema_version": "traceback.input-snapshot.v1",
            "snapshot_id": identifier,
            "content_sha256": content_sha256,
            "files": [item.__dict__ for item in captured],
        }
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_path = temporary / "input-manifest.local.json"
        with manifest_path.open("xb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        _seal_tree(temporary)
        os.replace(temporary, destination)
        directory_fd = os.open(snapshot_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return InputSnapshot(
            snapshot_id=identifier,
            path=destination,
            files=tuple(captured),
            content_sha256=content_sha256,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def input_tree_sha256(source_root: Path, relative_files: Iterable[str]) -> str:
    """Calculate the stable tree identity expected in ``JobRequest``.

    This helper performs the same safety and post-read stability checks as
    capture, but does not create a snapshot.  Capture always verifies the
    value again; callers cannot use this preflight hash as a trust shortcut.
    """

    root = source_root.resolve(strict=True)
    records: list[SnapshotFile] = []
    for name in sorted({_safe_relative(item).as_posix() for item in relative_files}):
        source = root.joinpath(*PurePosixPath(name).parts)
        if source.is_symlink():
            raise SnapshotViolation(f"symlinks are not accepted: {name}")
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise SnapshotViolation(f"snapshot source escapes configured root: {name}")
        before = source.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotViolation(f"snapshot source is not a regular file: {name}")
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        after = source.stat(follow_symlinks=False)
        if not _same_file(before, after):
            raise SnapshotViolation(f"snapshot source changed while hashing: {name}")
        records.append(SnapshotFile(name, size, digest.hexdigest()))
    if not records:
        raise SnapshotViolation("snapshot must contain at least one file")
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "traceback.input-tree.v1",
                "files": [item.__dict__ for item in records],
            }
        )
    ).hexdigest()


def verify_snapshot(
    snapshot_path: Path, *, expected_manifest_sha256: str | None = None
) -> InputSnapshot:
    """Rehash every sealed byte and reconstruct a verified snapshot."""

    if snapshot_path.is_symlink() or not snapshot_path.is_dir():
        raise SnapshotViolation("snapshot directory is missing or unsafe")
    manifest_path = snapshot_path / "input-manifest.local.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SnapshotViolation("snapshot manifest is missing or unsafe")
        raw = manifest_path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SnapshotViolation("snapshot manifest must be an object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotViolation("snapshot manifest is missing or malformed") from exc
    if raw != canonical_json_bytes(payload):
        raise SnapshotViolation("snapshot manifest is not canonical")
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise SnapshotViolation("snapshot manifest digest changed")
    try:
        if payload.pop("schema_version") != "traceback.input-snapshot.v1":
            raise SnapshotViolation("unsupported snapshot schema")
        snapshot_id = payload.pop("snapshot_id")
        content_sha256 = payload.pop("content_sha256")
        files = tuple(SnapshotFile(**item) for item in payload.pop("files"))
        if payload:
            raise SnapshotViolation("snapshot manifest contains unknown fields")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SnapshotViolation):
            raise
        raise SnapshotViolation("snapshot manifest fields are invalid") from exc
    if not files or any(
        not isinstance(item.relative_path, str)
        or type(item.size_bytes) is not int or item.size_bytes < 0
        or not isinstance(item.sha256_local, str)
        or len(item.sha256_local) != 64
        or any(c not in "0123456789abcdef" for c in item.sha256_local)
        for item in files
    ):
        raise SnapshotViolation("snapshot file declarations are invalid")
    names = tuple(item.relative_path for item in files)
    if names != tuple(sorted(set(names))):
        raise SnapshotViolation("snapshot file declarations are duplicate or unordered")
    if snapshot_id != snapshot_path.name:
        raise SnapshotViolation("snapshot identity does not match its directory")

    expected_locators = {item.relative_path for item in files}
    expected_directories = {
        parent.as_posix()
        for name in expected_locators
        for parent in _safe_relative(name).parents
        if parent.as_posix() != "."
    }
    actual_locators: set[str] = set()
    for path in snapshot_path.rglob("*"):
        relative = path.relative_to(snapshot_path).as_posix()
        if path.is_symlink():
            raise SnapshotViolation(f"snapshot contains a symlink: {relative}")
        if path.is_dir():
            if relative not in expected_directories:
                raise SnapshotViolation("snapshot directory inventory changed")
        elif path.is_file():
            if relative != "input-manifest.local.json":
                actual_locators.add(relative)
        else:
            raise SnapshotViolation("snapshot contains a non-regular entry")
    if actual_locators != expected_locators:
        raise SnapshotViolation("snapshot file inventory changed")

    for item in files:
        relative = _safe_relative(item.relative_path)
        path = snapshot_path.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise SnapshotViolation(f"snapshot file is missing or unsafe: {item.relative_path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size != item.size_bytes or digest.hexdigest() != item.sha256_local:
            raise SnapshotViolation(f"snapshot file digest changed: {item.relative_path}")

    calculated_content = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": "traceback.input-tree.v1",
                "files": [item.__dict__ for item in files],
            }
        )
    ).hexdigest()
    if calculated_content != content_sha256:
        raise SnapshotViolation("snapshot content identity is inconsistent")
    return InputSnapshot(
        snapshot_id=snapshot_id,
        path=snapshot_path,
        files=files,
        content_sha256=content_sha256,
        manifest_sha256=manifest_sha256,
    )
