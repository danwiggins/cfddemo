"""Runner-owned immutable input snapshots.

Snapshots are intentionally local-only.  Their manifests contain relative
locators and ordinary SHA-256 digests and must never be exported.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from evidence_inspector.models import canonical_json_bytes


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
